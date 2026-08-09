"""The task state machine (DESIGN.md §2).

Deterministic control flow. LLMs choose among edges the graph already offers,
through validated outputs -- they never invent a transition. Every run is
replayable from task.json.

MVP scope: sequential subtasks in one worktree, two escalation signals, a
two-rung ladder. Parallelism, the Integrator role, and an independent Test
Author are deliberately deferred (§15).
"""

import os
import re
import time

from . import brief, limits as lim, prompts, router as routing, schema, verify, yamlite
from .store import git

STAGES = ["intake", "classify", "plan", "implement", "verify", "review", "integrate", "done"]
TERMINAL = {"done", "abandoned", "needs_human"}

SIMPLE_HINTS = re.compile(
    r"\b(typo|rename|comment|bump|version|log|readme|format|lint)\b", re.I)
COMPLEX_HINTS = re.compile(
    r"\b(refactor|migrat|redesign|architect|rewrite|save format|schema|protocol|"
    r"across|integrat|system|multiple|api)\b", re.I)


class Halt(Exception):
    """Stop cleanly and leave the task resumable."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Orchestrator:
    def __init__(self, task, registry, adapter, gate, log=print, dry_run=False):
        self.task = task
        self.reg = registry
        self.router = routing.Router(registry)
        self.adapter = adapter
        self.gate = gate            # callable(kind, brief_text) -> bool
        self.log = log
        self.dry_run = dry_run
        self.budget = lim.Budget(task)
        self.repo = task.repo_path()
        self.vcfg = verify.load_project_config(self.repo)

    # ------------------------------------------------------------------ run
    def run(self):
        try:
            while True:
                status = self.task.state["status"]
                if status in TERMINAL:
                    return status
                handler = getattr(self, "_stage_" + status, None)
                if handler is None:
                    raise Halt("needs_human", "no handler for stage %r" % status)
                handler()
        except Halt as h:
            self.task.update(status=h.status)
            self.log("\n== %s: %s" % (h.status.upper(), h))
            return h.status
        except lim.LimitBreach as b:
            self.task.update(status="needs_human")
            self.log("\n== LIMIT (%s): %s" % (b.limit, b))
            return "needs_human"

    # --------------------------------------------------------------- stages
    def _stage_intake(self):
        self.log("intake: %s" % self.task.state["id"])
        self.task.update(status="classify")

    def _stage_classify(self):
        """Heuristics first; an LLM call only when they are ambiguous, and
        COMPLEX when still unsure -- under-planning costs more than
        over-planning (§2.2)."""
        text = self.task.read_text("task.md", "")
        simple, complex_ = SIMPLE_HINTS.search(text), COMPLEX_HINTS.search(text)
        hotspots = [h for h in (self.vcfg.get("hotspots") or []) if h and h in text]
        if hotspots or complex_ or len(text) > 1200:
            tier, why = "complex", "hotspot/keyword/length"
        elif simple and len(text) < 400:
            tier, why = "simple", "short and mechanical"
        else:
            tier, why = "complex", "ambiguous — defaulting to complex"
        self.log("classify: %s (%s)" % (tier, why))
        self.task.update(
            classification={"tier": tier, "by": "heuristic", "why": why},
            status="plan" if tier == "complex" else "implement")
        if tier == "simple":
            self._seed_single_subtask()

    def _seed_single_subtask(self):
        self.task.update(subtasks=[{
            "id": "st-1-main", "status": "pending",
            "goal": "Implement the request as described in task.md.",
            "planned_scope": ["**"], "acceptance": [], "actual_files": [],
        }])

    def _stage_plan(self):
        choice = self.router.select("planner", ceiling=self.budget.ceiling())
        self.log("plan: %s via %s" % (choice.model, choice.channel))
        self.budget.check_cost(0.0)
        self._invoke("planner", choice, cwd=self.repo,
                     extra="Write plan.md now. Use the template at "
                           "%s/templates/plan.md." % prompts.skill_path())
        subtasks = self._read_plan_subtasks()
        if not subtasks:
            raise Halt("needs_human", "planner produced no usable subtasks in plan.md")
        self.task.update(subtasks=[dict(s, status="pending", actual_files=[]) for s in subtasks])
        self.log("plan: %d subtask(s)" % len(subtasks))
        if self.budget.requires_approval("plan"):
            self._gate("plan", "Approve this plan? It splits the work into %d step(s)."
                       % len(subtasks))
        self.task.update(status="implement")

    def _read_plan_subtasks(self):
        """Parse the fenced YAML block the planner wrote. A plan we cannot
        parse is a protocol failure, not something to guess around."""
        text = self.task.read_text("plan.md", "")
        blocks = re.findall(r"```ya?ml\s*\n(.*?)```", text, re.S)
        for raw in blocks:
            try:
                data = yamlite.load(raw)
            except yamlite.YamlError:
                continue
            if isinstance(data, list) and data and isinstance(data[0], dict) and "id" in data[0]:
                out = []
                for s in data:
                    out.append({
                        "id": s["id"],
                        "goal": s.get("goal", ""),
                        "planned_scope": s.get("file_scope") or [],
                        "reads": s.get("reads") or [],
                        "acceptance": s.get("acceptance") or [],
                        "depends_on": s.get("depends_on") or [],
                    })
                return out
        return []

    def _stage_implement(self):
        state = self.task.state
        pending = [s for s in state["subtasks"] if s.get("status") != "complete"]
        if not pending:
            self.task.update(status="review")
            return
        sub = pending[0]
        base = self._ensure_worktree()
        role_choice = self.router.select("implementer", ceiling=self.budget.ceiling())
        attempts = 0
        while True:
            self.budget.check_attempt(sub["id"])
            self.budget.check_cost(0.0)
            attempts += 1
            self.log("implement %s: attempt %d (%s)" % (sub["id"], attempts, role_choice.model))
            self._invoke("implementer", role_choice, cwd=base, subtask=sub)
            self.budget.used_attempt(sub["id"])
            result = verify.run(self.task, self.repo, base, "fast")
            self.log("  verify: %s" % result.summary())
            if result.ok:
                self._checkpoint(base, "adg %s: %s" % (sub["id"], sub.get("goal", ""))[:72])
                break
            # Signal: test_stuck. Escalate one rung rather than letting the
            # same model try the same idea a fourth time (§6).
            threshold = int((self.reg["policy"].get("escalation_thresholds") or {})
                            .get("test_stuck_attempts", 3))
            if attempts >= threshold:
                stronger = self.router.escalate("implementer", role_choice,
                                                ceiling=self.budget.ceiling())
                if stronger is None:
                    raise Halt("needs_human",
                               "%s still failing after %d attempts and nothing stronger is "
                               "enrolled within the ceiling" % (sub["id"], attempts))
                self.log("  escalating to %s" % stronger.model)
                role_choice, attempts = stronger, 0
        self._finish_subtask(sub, base)

    def _checkpoint(self, cwd, message):
        """Commit the worktree state. Agents are told to checkpoint, but the
        pipeline must not depend on them remembering: without a commit there is
        no diff to review or land, and no salvage point after a crash. These
        commits live on a scratch branch and never touch the user's branch."""
        git(["add", "-A"], cwd, check=False)
        status = git(["status", "--porcelain"], cwd, check=False)
        if not status.strip():
            return False
        git(["-c", "user.email=orchestrator@agent-delegation", "-c", "user.name=adg",
             "commit", "-q", "-m", message], cwd, check=False)
        return True

    def _finish_subtask(self, sub, cwd):
        files = verify.changed_files(self.repo, cwd,
                                     self.task.state["repo"].get("base_commit", "HEAD"),
                                     ignore=self.vcfg.get("ignore"))
        violations = verify.scope_violations(files, sub.get("planned_scope") or ["**"])
        state = self.task.state
        for s in state["subtasks"]:
            if s["id"] == sub["id"]:
                s["status"] = "complete"
                s["actual_files"] = files
                s["scope_violations"] = violations
        self.task.write_json("task.json", state)
        if violations:
            self.log("  scope: %d file(s) outside declared scope" % len(violations))

    def _stage_review(self):
        self.budget.check_review_loop()
        base = self._ensure_worktree()
        result = verify.run(self.task, self.repo, base, "slow" if self.vcfg.get("slow") else "fast")
        if not result.ok:
            # Never review red code (D5). Send it back as work -- and reopen a
            # subtask, or implement would find nothing pending and bounce
            # straight back here forever. The attempt budget bounds the loop.
            self.log("review: skipped — checks are red (%s)" % result.summary())
            self._reopen_subtasks()
            self.task.update(status="implement")
            return
        choice = self.router.select("reviewer", ceiling=self.budget.ceiling())
        self.log("review: %s via %s" % (choice.model, choice.channel))
        self._invoke("reviewer", choice, cwd=base,
                     extra="Write your verdict to reports/review-reviewer.json, matching "
                           "%s/schemas/verdict.schema.json." % prompts.skill_path())
        verdict = self._read_verdict()
        self.log("review: %s" % verdict["verdict"])
        self.budget.used_review_loop()
        self._apply_verdict(verdict)

    def _read_verdict(self):
        path = os.path.join("reports", "review-reviewer.json")
        try:
            data = self.task.read_json(path)
        except (OSError, ValueError):
            raise Halt("needs_human", "reviewer wrote no valid verdict file")
        try:
            schema.validate_verdict(data)
        except schema.Invalid as e:
            raise Halt("needs_human", "reviewer verdict failed validation: %s" % e)
        return data

    def _apply_verdict(self, verdict):
        v = verdict["verdict"]
        if v == "APPROVE":
            self.task.update(status="integrate")
        elif v == "REQUEST_CHANGES":
            blocking = [f for f in verdict.get("findings", []) if f.get("severity") == "blocking"]
            owners = {f.get("suggested_owner") for f in blocking if f.get("suggested_owner")}
            self._reopen_subtasks(owners)
            self.log("  %d blocking finding(s) sent back" % len(blocking))
            self.task.update(status="implement")
        elif v == "REPLAN":
            self.budget.check_replan()
            self.budget.used_replan()
            self.task.update(status="plan")
        else:
            raise Halt("needs_human", "reviewer escalated: %s" %
                       (verdict.get("findings") or [{}])[0].get("claim", "no detail"))

    def _reopen_subtasks(self, owners=None):
        """Mark subtasks pending again. When the reviewer named owners, only
        those reopen -- reworking everything would discard green work."""
        state = self.task.state
        hit = False
        for s in state["subtasks"]:
            named = owners and any(s["id"] in o for o in owners)
            if not owners or named:
                s["status"] = "pending"
                hit = True
        if not hit and state["subtasks"]:
            state["subtasks"][0]["status"] = "pending"
        self.task.write_json("task.json", state)

    def _stage_integrate(self):
        state = self.task.state
        files = sorted({f for s in state["subtasks"] for f in s.get("actual_files", [])})
        result = verify.run(self.task, self.repo, self._ensure_worktree(), "fast")
        text, problems = brief.write(
            self.task, "merge",
            "Land this change? It is complete and its checks pass. In attended mode "
            "the diff is applied to your working tree, uncommitted, for you to commit.",
            files=files, verify=result)
        for p in problems:
            self.log("  brief lint: %s" % p)
        if self.budget.requires_approval("merge"):
            if not self.gate("merge", text):
                raise Halt("needs_human", "merge declined by human")
        self._land()
        self.task.update(status="done")

    def _land(self):
        """attended: apply an uncommitted diff to the user's checkout and stop.
        The orchestrator has no commit or push path at all (§9.2)."""
        state = self.task.state
        wt = state.get("worktree")
        if not wt or self.dry_run:
            self.log("integrate: nothing to land (dry run or no worktree)")
            return
        branch = state.get("branch")
        if state.get("mode") == "attended":
            diff = git(["diff", state["repo"].get("base_commit", "HEAD"), branch],
                       self.repo, check=False)
            if not diff.strip():
                self.log("integrate: no changes to apply")
                return
            patch = self.task.file("integrate.patch")
            with open(patch, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(diff + "\n")
            self.log("integrate: patch ready at %s" % patch)
            self.log("           apply with: git apply %s" % patch)
        else:
            self.log("integrate: branch %s is ready to push and open a PR" % branch)

    # ---------------------------------------------------------------- infra
    def _ensure_worktree(self):
        state = self.task.state
        if state.get("worktree") and os.path.isdir(state["worktree"]):
            return state["worktree"]
        base = git(["rev-parse", "HEAD"], self.repo)
        branch = "adg/%s/work" % state["id"]
        path = os.path.join(os.path.dirname(os.path.abspath(self.repo)),
                            ".adg-worktrees", "%s-work" % state["id"])
        self.log("worktree: %s (%s)" % (path, self.adapter.name))
        self.adapter.create_worktree(self.repo, branch, base, path)
        repo_info = dict(state["repo"], base_commit=base)
        self.task.update(worktree=path, branch=branch, repo=repo_info)
        return path

    def _invoke(self, role, choice, cwd, subtask=None, extra=None):
        """One agent session: compose, run, validate the report."""
        if self.dry_run:
            self.log("  [dry-run] would run %s as %s" % (choice.model, role))
            return None
        # Reports are matched by mtime after this point: on a rework loop the
        # previous attempt's report is still on disk, and accepting it would
        # read a stale success as a fresh one.
        started = time.time() - 1
        text = prompts.compose(role, self.task, subtask=subtask, extra=extra,
                               verify_cfg=self.vcfg)
        session = self.adapter.start_agent(role, choice.agent_kind, cwd,
                                           prompts.env_for(self.task))
        try:
            res = self.adapter.prompt(session, text, timeout=3600)
        finally:
            self.adapter.teardown(session)
        self._log_transcript(role, text, res)
        outcome = "complete" if res.get("settled") == "idle" else "blocked"
        report, problems = self._collect_report(role, subtask, since=started)
        if report is None:
            outcome = "blocked"
        self.task.record_delegation({
            "stage": self.task.state["status"], "role": role,
            "subtask": subtask.get("id") if subtask else None,
            "model": choice.model, "channel": choice.channel,
            "adapter": self.adapter.name, "outcome": outcome,
            "report_problems": problems or None,
        })
        if outcome == "blocked" and res.get("settled") == "timeout":
            raise Halt("needs_human", "%s agent timed out" % role)
        return report

    def _log_transcript(self, role, prompt, res):
        """Keep what the agent was asked and what it said. An agent that does
        nothing is otherwise invisible -- which is exactly when you need it."""
        name = os.path.join("agent-logs", "%s-%s.log" % (
            role, time.strftime("%Y%m%d-%H%M%S", time.gmtime())))
        os.makedirs(self.task.file("agent-logs"), exist_ok=True)
        self.task.write_text(name, "=== PROMPT ===\n%s\n\n=== SETTLED: %s (exit %s) ===\n%s\n"
                             % (prompt, res.get("settled"), res.get("code"),
                                (res.get("output") or "")[-8000:]))

    def _collect_report(self, role, subtask, since=0):
        """Find and validate this role's report from *this* invocation.
        Liveness is not success -- an agent can settle idle having done
        nothing (§4.6), so the report is the evidence, not the exit code."""
        for name, data in sorted(self.task.reports().items()):
            if role not in name and not (subtask and subtask["id"] in name):
                continue
            try:
                if os.path.getmtime(self.task.file("reports", name)) < since:
                    continue  # left over from an earlier attempt
            except OSError:
                continue
            if data is None:
                return None, ["%s is not valid JSON" % name]
            try:
                schema.validate_report(data)
            except schema.Invalid as e:
                return data, [str(e)]
            return data, None
        return None, ["no fresh report written by %s" % role]

    def _gate(self, kind, question):
        files = sorted({f for s in self.task.state["subtasks"] for f in s.get("actual_files", [])})
        text, problems = brief.write(self.task, kind, question, files=files)
        for p in problems:
            self.log("  brief lint: %s" % p)
        if not self.gate(kind, text):
            raise Halt("needs_human", "%s declined by human" % kind)
        self.task.record_gate(kind, "approved")
