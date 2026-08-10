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

# Keyword matching on natural language does not work: "API" in "calculator API"
# forced a 12-line change down the full planning pipeline, and no keyword list
# recognises "add a subtract function" as simple. Classification is a judgement,
# so a cheap model makes it -- the call costs ~$0.0002 against a ~$2.60 mistake.
# Only non-negotiable facts stay hard-coded, below.
CLASSIFY_PROMPT = """Classify this software task as SIMPLE or COMPLEX.

SIMPLE: one coherent change a single competent engineer would finish in one
sitting without a written plan. A few files, no new architecture, no format or
interface that other code depends on.

COMPLEX: needs a plan first -- multiple independent parts, a new abstraction or
dependency, a change to a shared interface / schema / save format, wide blast
radius, or genuine ambiguity about what is wanted.

Bias: when the work is small and self-contained, say SIMPLE. Planning a small
task wastes far more than it saves.

--- REQUEST ---
%(request)s

--- REPOSITORY ---
%(facts)s

Reply with exactly one line:
VERDICT: SIMPLE|COMPLEX -- <one short clause of reasoning>
"""


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
        self._last_failure = None
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
        except KeyboardInterrupt:
            self.log("\n== interrupted; task is resumable with `delegate resume`")
            raise
        except Exception as e:  # noqa: BLE001 -- see below
            # An unexpected failure must park the task, not crash the process:
            # all progress so far is on disk and resumable, and a traceback on
            # stderr is not a state the user can act on (§12).
            import traceback
            self.task.write_text("crash.log", traceback.format_exc())
            self.task.update(status="needs_human")
            self.log("\n== CRASHED: %s: %s" % (type(e).__name__, e))
            self.log("   details in %s" % self.task.file("crash.log"))
            return "needs_human"

    # --------------------------------------------------------------- stages
    def _stage_intake(self):
        self.log("intake: %s" % self.task.state["id"])
        self.task.update(status="classify")

    def _repo_facts(self):
        """Cheap, factual context so the classifier judges the change against
        this repository rather than against the wording of the request."""
        files = git(["ls-files"], self.repo, check=False).splitlines()
        sizes = []
        for f in files[:400]:
            try:
                sizes.append((len(open(os.path.join(self.repo, f), "rb").read().splitlines()), f))
            except OSError:
                continue
        sizes.sort(reverse=True)
        lines = ["%d tracked files, %d total lines" % (len(files), sum(n for n, _ in sizes))]
        lines.append("largest: " + ", ".join("%s (%d lines)" % (f, n) for n, f in sizes[:8]))
        if self.vcfg.get("hotspots"):
            lines.append("declared hotspots: " + ", ".join(self.vcfg["hotspots"]))
        return "\n".join(lines)

    def _stage_classify(self):
        """A cheap model judges; only facts override it (§2.2)."""
        text = self.task.read_text("task.md", "")
        hotspots = [h for h in (self.vcfg.get("hotspots") or []) if h and h in text]
        if hotspots:
            # Not a judgement call: a hotspot is unmergeable or high-coupling by
            # declaration, so it always gets a plan.
            return self._classified("complex", "touches a declared hotspot: %s"
                                    % ", ".join(hotspots), "policy")
        tier, why, by = self._ask_classifier(text)
        self._classified(tier, why, by)

    def _ask_classifier(self, text):
        if self.dry_run:
            return "simple", "dry run", "stub"
        try:
            choice = self._pick("classifier")
        except routing.NoModelAvailable as e:
            return "complex", "no classifier model available (%s)" % e, "fallback"
        prompt = CLASSIFY_PROMPT % {"request": text.strip()[:4000],
                                    "facts": self._repo_facts()}
        session = self.adapter.start_agent("classifier", choice.agent_kind,
                                           self.repo, prompts.env_for(self.task))
        try:
            res = self.adapter.prompt(session, prompt, timeout=180)
        finally:
            self.adapter.teardown(session)
        out = (res.get("output") or "")
        m = re.search(r"VERDICT:\s*(SIMPLE|COMPLEX)\s*-*\s*(.*)", out, re.I)
        if not m:
            # Unparseable: fail safe. Over-planning a small task wastes time;
            # under-planning a large one wastes the whole run.
            return "complex", "classifier gave no usable verdict", "fallback"
        self.task.record_delegation({"stage": "classify", "role": "classifier",
                                     "model": choice.model, "channel": choice.channel,
                                     "adapter": self.adapter.name, "outcome": "complete"})
        return m.group(1).lower(), m.group(2).strip()[:200] or "no reason given", choice.model

    def _classified(self, tier, why, by):
        self.log("classify: %s (%s)" % (tier, why))
        self.task.update(classification={"tier": tier, "by": by, "why": why},
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
        choice = self._pick("planner")
        self.log("plan: %s via %s" % (choice.model, choice.channel))
        self.budget.check_cost(0.0)
        self._close(self._invoke("planner", choice, cwd=self.repo,
                    extra="Write plan.md now. Use the template at "
                          "%s/templates/plan.md." % prompts.skill_path()))
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
        if not state.get("subtasks"):
            # Resuming into implement with a plan already on disk: re-read it
            # rather than re-running (and re-paying for) the planner.
            recovered = self._read_plan_subtasks()
            if recovered:
                state = self.task.update(
                    subtasks=[dict(x, status="pending", actual_files=[]) for x in recovered])
                self.log("implement: recovered %d subtask(s) from plan.md" % len(recovered))
            else:
                raise Halt("needs_human", "no subtasks and no parseable plan.md")
        pending = [s for s in state["subtasks"] if s.get("status") != "complete"]
        if not pending:
            self.task.update(status="review")
            return
        sub = pending[0]
        base = self._ensure_worktree()
        role_choice = self._pick("implementer")
        attempts, session = 0, None
        while True:
            self.budget.check_attempt(sub["id"])
            self.budget.check_cost(0.0)
            attempts += 1
            self.log("implement %s: attempt %d (%s%s)" % (
                sub["id"], attempts, role_choice.model, "" if session is None else ", continued"))
            session = self._invoke("implementer", role_choice, cwd=base, subtask=sub,
                                   session=session, failure=self._last_failure)
            self.budget.used_attempt(sub["id"])
            result = verify.run(self.task, self.repo, base, "fast")
            self.log("  verify: %s" % result.summary())
            if result.ok:
                self._last_failure = None
                self._close(session)
                self._checkpoint(base, "adg %s: %s" % (sub["id"], sub.get("goal", ""))[:72])
                break
            self._last_failure = "\n".join(
                "$ %s\n%s" % (f["cmd"], f["output"][-1500:]) for f in result.failures())
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
                # A stronger model starts clean: the point of escalating is a
                # different approach, not more of the same context.
                self._close(session)
                role_choice, attempts, session = stronger, 0, None
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

    def _review_policy(self):
        """auto (default): independent review for complex work, deterministic
        checks alone for simple work. always / never force it either way."""
        state = self.task.state
        return (state.get("review")
                or (self.reg["policy"].get("review") if isinstance(self.reg.get("policy"), dict) else None)
                or "auto")

    def _skip_review(self, result):
        """Reasons to run the reviewer anyway, even on a simple task. Each is a
        deterministic signal that something happened nobody planned -- exactly
        what an automated check cannot judge."""
        mode = self._review_policy()
        if mode == "always":
            return None
        if mode == "never":
            return "review disabled for this task"
        if self.task.state.get("classification", {}).get("tier") != "simple":
            return None
        if not result.ok:
            return None
        violations = [v for s in self.task.state["subtasks"]
                      for v in (s.get("scope_violations") or [])]
        if violations:
            self.log("  review: running anyway — %d file(s) outside declared scope"
                     % len(violations))
            return None
        return "simple change, all checks green, nothing outside the declared scope"

    def _stage_review(self):
        base = self._ensure_worktree()
        result = verify.run(self.task, self.repo, base, "slow" if self.vcfg.get("slow") else "fast")
        skip = self._skip_review(result)
        if skip:
            self.log("review: skipped — %s" % skip)
            self.task.update(review_outcome={"reviewed": False, "why": skip},
                             status="integrate")
            return
        self.budget.check_review_loop()
        if not result.ok:
            # Never review red code (D5). Send it back as work -- and reopen a
            # subtask, or implement would find nothing pending and bounce
            # straight back here forever. The attempt budget bounds the loop.
            self.log("review: skipped — checks are red (%s)" % result.summary())
            self._reopen_subtasks()
            self.task.update(status="implement")
            return
        choice = self._pick("reviewer")
        self.log("review: %s via %s" % (choice.model, choice.channel))
        self._close(self._invoke("reviewer", choice, cwd=base,
                    extra="Write your verdict to reports/review-reviewer.json, matching "
                          "%s/schemas/verdict.schema.json." % prompts.skill_path()))
        verdict = self._read_verdict()
        self.log("review: %s" % verdict["verdict"])
        self.task.update(review_outcome={"reviewed": True, "verdict": verdict["verdict"]})
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
        ro = self.task.state.get("review_outcome") or {}
        if ro.get("reviewed"):
            note = "An independent reviewer checked this against the plan and approved it."
        else:
            note = ("**No independent review was run** (%s). The automated checks "
                    "below are the only evidence. Re-run with `--review always` if "
                    "you want a second opinion." % ro.get("why", "not requested"))
        text, problems = brief.write(
            self.task, "merge",
            "Land this change? It is complete and its checks pass. In attended mode "
            "the diff is applied to your working tree, uncommitted, for you to commit.",
            files=files, verify=result, extra="## Review\n\n" + note)
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
    def _pick(self, role, boost=None):
        """Best candidate this runtime can actually launch. The registry says
        what a deployment could use; only the adapter knows what is installed,
        so an uninstalled CLI is skipped rather than crashing the run."""
        candidates = self.router.candidates(role, ceiling=self.budget.ceiling(), boost=boost)
        for c in candidates:
            if self.adapter.can_run(c.agent_kind):
                return c
        raise routing.NoModelAvailable(
            "no runnable model for role %r: %s" % (
                role, ", ".join("%s needs %s" % (c.model, c.agent_kind)
                                for c in candidates) or "nothing enrolled"))

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

    def _invoke(self, role, choice, cwd, subtask=None, extra=None,
                session=None, failure=None):
        """Run one turn. With `session`, it continues that agent instead of
        starting a fresh one -- a retry keeps everything the agent already
        learned, which is most of the wall clock on a short task. Returns the
        session so a caller can continue it again."""
        if self.dry_run:
            self.log("  [dry-run] would run %s as %s" % (choice.model, role))
            return None
        # Reports are matched by mtime after this point: on a rework loop the
        # previous attempt's report is still on disk, and accepting it would
        # read a stale success as a fresh one.
        started = time.time() - 1
        if session is None:
            text = prompts.compose(role, self.task, subtask=subtask, extra=extra,
                                   verify_cfg=self.vcfg)
            session = self.adapter.start_agent(role, choice.agent_kind, cwd,
                                               prompts.env_for(self.task))
            res = self.adapter.prompt(session, text, timeout=3600)
        else:
            text = prompts.retry(failure)
            res = self.adapter.follow_up(session, text, timeout=3600)
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
            self._close(session)
            raise Halt("needs_human", "%s agent timed out" % role)
        return session

    def _close(self, session):
        if session is not None:
            try:
                self.adapter.teardown(session)
            except Exception as e:
                # Teardown must never mask the real outcome, but swallowing it
                # silently hides adapter bugs that only show up as leaked panes.
                self.log("  warning: teardown failed: %s: %s" % (type(e).__name__, e))

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
