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
import subprocess
import time

from . import (brief, companions, cooldown, limits as lim, prompts, quota,
               router as routing, schema, verify, winnow, yamlite)
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


_LOG_SEQ = [0]
_LOG_LOCK = __import__("threading").Lock()


def _stamp(epoch):
    """A reopen time a human can act on, in their own zone."""
    if not epoch:
        return "an unknown time"
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(float(epoch)))


class Halt(Exception):
    """Stop cleanly and leave the task resumable."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Orchestrator:
    def __init__(self, task, registry, adapter, gate, log=print, dry_run=False,
                 clock=time.time):
        self.task = task
        self.reg = registry
        self.router = routing.Router(registry)
        self.adapter = adapter
        self.gate = gate            # callable(kind, brief_text) -> bool
        self.log = log
        self.dry_run = dry_run
        # The machine's only wall clock. Cooldown expiry, utilisation windows
        # and reopen times all derive from it, so a run replays under a fixed
        # clock exactly as it ran.
        self.clock = clock
        self._warned_channels = False
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
    CRITERIA_PROMPT = """Turn this request into acceptance criteria.

Reply with exactly this markdown and nothing else:

## Acceptance criteria

- **AC-1** — <one checkable statement>
- **AC-2** — ...

## Non-goals

- <something a reasonable reader might assume is included, and is not>

## Open questions

- <question> — *default:* <what you will assume if nobody answers>

Rules: each criterion is one observable claim someone could verify, not a
paragraph. Infer nothing grand — if the request is small, two criteria is a
complete answer. Omit the open-questions section entirely when the request
admits only one sensible reading.

--- REQUEST ---
%(request)s

--- REPOSITORY ---
%(facts)s
"""

    def _stage_intake(self):
        self.log("intake: %s" % self.task.state["id"])
        found = companions.detect()
        if any(found.values()):
            self.log("companions: %s" % ", ".join(k for k, v in found.items() if v))
        self.task.update(companions=found)
        self._derive_criteria()
        self.task.update(status="classify")

    def _derive_criteria(self):
        """Give every task numbered acceptance criteria, not just complex ones.
        The reviewer's whole evidence chain keys off AC-n; without them a review
        has nothing to check against and grades taste instead."""
        text = self.task.read_text("task.md", "")
        if re.search(r"^\s*[-*]?\s*\*\*AC-\d", text, re.M) or self.dry_run:
            return                      # already stated by whoever wrote the task
        try:
            choice = self._pick("classifier")   # cheap tier; this is not judgement
        except routing.NoModelAvailable:
            return
        res = self._direct("intake", choice, self.CRITERIA_PROMPT % {
            "request": text.strip()[:4000], "facts": self._repo_facts()})
        out = (res.get("output") or "").strip()
        if "AC-1" not in out:
            self.log("intake: no criteria derived — the reviewer will have less to check")
            return
        self.task.write_text("task.md", text.rstrip() + "\n\n" + out + "\n")
        self.log("intake: %d acceptance criteria" % len(re.findall(r"\*\*AC-\d+\*\*", out)))

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
        res = self._direct("classifier", choice, prompt)
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
        nxt = "implement"
        if tier == "complex":
            nxt = "brainstorm" if self._brainstorm_wanted() else "plan"
        self.task.update(classification={"tier": tier, "by": by, "why": why}, status=nxt)
        if tier == "simple":
            self._seed_single_subtask()

    def _seed_single_subtask(self):
        self.task.update(subtasks=[{
            "id": "st-1-main", "status": "pending",
            "goal": "Implement the request as described in task.md.",
            "planned_scope": ["**"], "acceptance": [], "actual_files": [],
        }])

    def _brainstorm_wanted(self):
        """Design dialogue is worth it on complex work, and only when a human is
        there to answer. Unattended, the planner's open-questions-with-defaults
        already covers the same ground without pretending to consult anyone."""
        mode = (self.vcfg.get("brainstorm") or "auto")
        if mode == "never":
            return False
        if mode == "always":
            return True
        return self.task.state.get("mode") == "attended"

    BRAINSTORM_PROMPT = """Design this change before anyone implements it.

%(discipline)s

Write **only** to %(spec)s. Do not write into the repository and do not commit
anything — this design is orchestration state, not project documentation.

Produce, in this order:
1. **Purpose** — what problem this solves, in the user's terms.
2. **Approaches** — two or three, each with its trade-off, and say which you
   recommend and why.
3. **Design** — the recommended approach in enough detail to plan against:
   the seams, the interfaces other code will touch, what stays unchanged.
4. **Risks** — what could make this design wrong, and the earliest signal.
5. **Questions for the human** — anything you genuinely cannot settle from the
   code. Each one gets a *proposed default* so silence is a safe answer. Omit
   the section if there is nothing real to ask; inventing questions to look
   thorough wastes the one round you get.

You are not implementing anything and you are not writing a task breakdown —
a planner does that next, from your design.

--- REQUEST ---
%(request)s
"""

    def _stage_brainstorm(self):
        try:
            choice = self._pick("planner")
        except routing.NoModelAvailable as e:
            self.log("brainstorm: skipped — %s" % e)
            self.task.update(status="plan")
            return
        installed = (self.task.state.get("companions") or {}).get("superpowers")
        discipline = ("Use the `superpowers:brainstorming` skill's discipline for this: "
                      "explore the code first, then reason about purpose, constraints and "
                      "success criteria before proposing anything. Ignore its instructions "
                      "about where to save files and about committing."
                      if installed else
                      "Explore the code before proposing anything.")
        self.log("brainstorm: %s via %s" % (choice.model, choice.channel))
        self._run_once(
            "planner", choice, cwd=self.repo,
            extra=self.BRAINSTORM_PROMPT % {
                "discipline": discipline,
                "spec": self.task.file("spec.md"),
                "request": self.task.read_text("task.md", "").strip()[:4000]})
        spec = self.task.read_text("spec.md", "")
        if not spec.strip():
            self.log("brainstorm: produced no design — planning from the request alone")
            self.task.update(status="plan")
            return
        self._gate("design", "Approve this design before a plan is written from it? "
                             "Answer any open questions below, or accept the "
                             "defaults by approving.")
        self.task.update(status="plan")

    def _stage_plan(self):
        choice = self._pick("planner")
        self._archive_reports("plan-")
        self.log("plan: %s via %s" % (choice.model, choice.channel))
        self.budget.check_cost(0.0)
        extra = ("Write plan.md now. Use the template at %s/templates/plan.md."
                 % prompts.skill_path())
        if self.task.read_text("spec.md", "").strip():
            extra += ("\n\nAn approved design is at %s. Plan against it: it settles "
                      "the approach, so decompose and scope rather than redesigning. "
                      "Departing from it is a decision to record in decisions.md."
                      % self.task.file("spec.md"))
        self._run_once("planner", choice, cwd=self.repo, extra=extra)
        subtasks = self._read_plan_subtasks()
        if not subtasks:
            raise Halt("needs_human", "planner produced no usable subtasks in plan.md")
        self.task.update(subtasks=[dict(s, status="pending", actual_files=[]) for s in subtasks])
        self.log("plan: %d subtask(s)" % len(subtasks))
        self._author_tests(subtasks)
        if self.budget.requires_approval("plan"):
            self._gate("plan", "Approve this plan? It splits the work into %d step(s)."
                       % len(subtasks))
        self.task.update(status="implement")

    def _author_tests(self, subtasks):
        """Write tests from the requirements, before any implementation exists.
        An implementer's own tests confirm what it built; these confirm what was
        asked, which is the only way a missing requirement shows up (D7)."""
        if (self.vcfg.get("test_author") or "auto") == "never":
            return
        try:
            choice = self._pick("test-author")
        except routing.NoModelAvailable as e:
            self.log("tests: skipped — %s" % e)
            return
        base = self._ensure_worktree()
        self.log("tests: %s via %s" % (choice.model, choice.channel))
        self._run_once(
            "test-author", choice, cwd=base,
            extra="Write failing tests for the acceptance criteria in task.md now. "
                  "Do not implement the feature and do not read any implementation. "
                  "Run them and capture the failure output as evidence.")
        # Red is the expected state here, so a failing run is not a problem --
        # but a *passing* one means the tests do not test the new requirement.
        result = verify.run(self.task, self.repo, base, "fast")
        if result.ok and result.steps:
            self.log("  warning: new tests pass before implementation — "
                     "they may not test the requirement")
        self._checkpoint(base, "adg %s: tests from requirements" % self.task.state["id"])

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
                        "hotspots": s.get("hotspots") or [],
                        "frozen_interfaces": s.get("frozen_interfaces") or [],
                    })
                return out
        return []

    def _ready(self, subtasks):
        """Subtasks whose dependencies are done. Ordering only -- safety is the
        scope check below."""
        done = {t["id"] for t in subtasks if t.get("status") == "complete"}
        return [t for t in subtasks if t.get("status") != "complete"
                and all(d in done for d in (t.get("depends_on") or []))]

    def _wave(self, subtasks):
        """The largest set of ready subtasks that may run at once. Two rules,
        both pessimistic on purpose: no two may write the same scope, and no two
        may touch the same hotspot. A wrong parallel decision costs a conflict
        nobody can merge; a wrong serial one costs wall clock."""
        cap = int(self.budget.limits["max_parallel_agents"])
        wave, claimed = [], []
        declared = [h for h in (self.vcfg.get("hotspots") or []) if h]
        for t in self._ready(subtasks):
            scope = list(t.get("planned_scope") or ["**"]) + list(t.get("hotspots") or [])
            # A file the project calls a hotspot is unmergeable regardless of who
            # declared it, so it claims exclusivity whenever a scope can reach it.
            scope += [h for h in declared
                      if verify.scopes_overlap([h], t.get("planned_scope") or ["**"])]
            if any(verify.scopes_overlap(scope, other) for other in claimed):
                continue
            wave.append(t)
            claimed.append(scope)
            if len(wave) >= cap:
                break
        return wave or self._ready(subtasks)[:1]

    def _stage_implement(self):
        # Establishes base_commit. Without it every diff below is taken against
        # HEAD *inside* a worktree that has already committed its checkpoint,
        # which reads as "nothing changed" and silently voids scope checking.
        self._ensure_worktree()
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
        wave = self._wave(state["subtasks"])
        if len(wave) > 1:
            self.log("implement: %d subtasks in parallel (%s)"
                     % (len(wave), ", ".join(t["id"] for t in wave)))
            self._run_wave(wave)
            return
        sub = wave[0]
        self._one_subtask(sub, self._ensure_worktree())

    def _one_subtask(self, sub, base):
        role_choice = self._pick("implementer")
        # All per-subtask state stays local: this method runs concurrently in a
        # wave, and anything on self is shared with the siblings.
        attempts, session, report, failure = 0, None, None, None
        while True:
            self.budget.check_attempt(sub["id"])
            self.budget.check_cost(0.0)
            attempts += 1
            self.log("implement %s: attempt %d (%s%s)" % (
                sub["id"], attempts, role_choice.model, "" if session is None else ", continued"))
            session, report = self._invoke(
                "implementer", role_choice, cwd=base, subtask=sub,
                session=session, failure=failure, extra=self._findings_brief(sub))
            self.budget.used_attempt(sub["id"])
            result = verify.run(self.task, self.repo, base, "fast")
            self.log("  verify: %s" % result.summary())
            claimed = (report or {}).get("status")
            if claimed and claimed != "complete":
                self.log("  %s: agent reported %s" % (sub["id"], claimed))
            if claimed in ("blocked", "escalate"):
                raise Halt("needs_human", "%s reported %s: %s" % (
                    sub["id"], claimed, (report or {}).get("summary", "")[:200]))
            if result.ok and not self._touched(base):
                raise Halt("needs_human",
                           "%s changed no files, and the checks were already green "
                           "before it ran — passing tests are not evidence of work"
                           % sub["id"])
            if result.ok:
                self._close(session)
                self._checkpoint(base, ("adg %s: %s" % (sub["id"], sub.get("goal", "")))[:72])
                break
            failure = "\n".join(
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

    def _run_wave(self, wave):
        """Each subtask in its own worktree, concurrently, then merged in
        dependency order. Threads are fine here: every agent is a subprocess."""
        import threading
        results = {}
        # Serial creation: concurrent `git worktree add` against one repo
        # contends on .git locks and fails intermittently on a healthy run.
        trees = {t["id"]: self._subtask_worktree(t) for t in wave}

        def work(sub):
            try:
                results[sub["id"]] = self._one_subtask(sub, trees[sub["id"]])
            except Exception as e:  # a failed branch must not kill its siblings
                results[sub["id"]] = e

        threads = [threading.Thread(target=work, args=(t,)) for t in wave]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for sub in wave:
            outcome = results.get(sub["id"])
            if isinstance(outcome, Exception):
                raise Halt("needs_human", "%s failed: %s" % (sub["id"], outcome))
        self._integrate_wave(wave)

    def _subtask_worktree(self, sub):
        state = self.task.state
        branch = "adg/%s/%s" % (state["id"], sub["id"])
        path = os.path.join(os.path.dirname(os.path.abspath(self.repo)),
                            ".adg-worktrees", state["project_key"],
                            "%s-%s" % (state["id"], sub["id"]))
        self.adapter.create_worktree(self.repo, branch,
                                     state["repo"].get("base_commit", "HEAD"), path)
        return path

    def _integrate_wave(self, wave):
        """Merge each green branch into the task branch, verifying after each --
        so the integration branch is always green and every merge is tested
        against everything already landed."""
        integration = self._ensure_worktree()
        for sub in wave:
            branch = "adg/%s/%s" % (self.task.state["id"], sub["id"])
            merge = subprocess.run(["git", "merge", "--no-edit", branch],
                                   cwd=integration, capture_output=True, text=True)
            if merge.returncode != 0:
                self.log("  conflict merging %s" % sub["id"])
                self._reconcile(sub, integration, merge.stdout + merge.stderr)
            result = verify.run(self.task, self.repo, integration, "fast")
            if not result.ok:
                raise Halt("needs_human",
                           "%s broke the integration branch once merged" % sub["id"])

    def _reconcile(self, sub, cwd, conflict_text):
        """A conflict two agents produced is judgement, not mechanism: which
        side matches the plan, and what did the other one mean."""
        try:
            choice = self._pick("integrator")
        except routing.NoModelAvailable as e:
            raise Halt("needs_human", "merge conflict in %s and no integrator: %s"
                       % (sub["id"], e))
        self.log("  integrator: %s" % choice.model)
        self._run_once(
            "integrator", choice, cwd=cwd, subtask=sub,
            extra="A merge conflict is in the working tree. Resolve it, then "
                  "`git add -A && git commit`. Conflict output:\n\n%s"
                  % conflict_text[-2000:])
        left = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"],
                              cwd=cwd, capture_output=True, text=True).stdout.strip()
        if left:
            raise Halt("needs_human", "conflict still unresolved in: %s" % left)

    def _touched(self, cwd):
        """Did anything actually change in this worktree?"""
        return bool(verify.changed_files(
            self.repo, cwd, self.task.state["repo"].get("base_commit", "HEAD"),
            ignore=self.vcfg.get("ignore")))

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
        def mark(state):
            for s in state["subtasks"]:
                if s["id"] == sub["id"]:
                    s["status"] = "complete"
                    s["actual_files"] = files
                    s["scope_violations"] = violations
        self.task.mutate(mark)
        if violations:
            self.log("  scope: %d file(s) outside declared scope" % len(violations))
        self.task.update(pending_findings=[])

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

    def _winnow(self, base, result):
        """Deterministic chaff scan, if code-winnow is installed. Free enough to
        run even when LLM review is skipped, which is exactly when the extra
        evidence is worth most."""
        if (self.vcfg.get("winnow") or "auto") == "never":
            return None
        configured = self.vcfg.get("winnow_scan")
        scan_py = winnow.find(self.repo, configured)
        if not scan_py:
            return {"ran": False,
                    "why": winnow.misconfigured(configured) or "code-winnow not installed"}
        summary = winnow.run(self.task, scan_py, base,
                             self.task.state["repo"].get("base_commit", "HEAD"),
                             result.run_id)
        if summary and summary.get("ran"):
            self.log("  chaff scan: %d note(s), %d notable"
                     % (summary["total"], len(summary["notable"])))
        return summary

    def _stage_review(self):
        base = self._ensure_worktree()
        result = verify.run(self.task, self.repo, base, "slow" if self.vcfg.get("slow") else "fast")
        wn = self._winnow(base, result)
        self.task.update(winnow=wn)
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
        self._archive_reports("review-")
        self.log("review: %s via %s" % (choice.model, choice.channel))
        extra = ("Write your verdict to reports/review-reviewer.json, matching "
                 "%s/schemas/verdict.schema.json." % prompts.skill_path())
        chaff = winnow.as_text(self.task.state.get("winnow"))
        if chaff:
            extra += ("\n\n%s\nRead these after the diff, not before. They may only "
                      "block if they independently land on authority — an acceptance "
                      "criterion, a plan line, or a stated non-goal. Otherwise record "
                      "them under `advisory`." % chaff)
        self._run_once("reviewer", choice, cwd=base, extra=extra)
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
        # The verdict rides inside a normal report as role_data.verdict. A bare
        # verdict file is still accepted: older reviewers and hand-driven runs
        # write one, and refusing it would discard a real review over shape.
        if "verdict" not in data:
            inner = (data.get("role_data") or {}).get("verdict")
            if inner is None:
                if data.get("status") in ("blocked", "escalate"):
                    raise Halt("needs_human", "reviewer could not review: %s"
                               % (data.get("summary", "")[:200]))
                raise Halt("needs_human", "reviewer report carries no verdict")
            data = inner
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
            # Carry the findings to the implementer. Reopening a subtask without
            # them sends an agent back to the same code with the same prompt and
            # no idea what was wrong -- it rebuilds what the reviewer rejected.
            self.task.update(pending_findings=blocking)
            self.log("  %d blocking finding(s) sent back" % len(blocking))
            self.task.update(status="implement")
        elif v == "REPLAN":
            self.budget.check_replan()
            self.budget.used_replan()
            self.task.update(status="plan")
        else:
            raise Halt("needs_human", "reviewer escalated: %s" %
                       (verdict.get("findings") or [{}])[0].get("claim", "no detail"))

    def _findings_brief(self, sub):
        """What the reviewer rejected, for the implementer that has to fix it."""
        findings = self.task.state.get("pending_findings") or []
        mine = [f for f in findings
                if not f.get("suggested_owner") or sub["id"] in f.get("suggested_owner", "")]
        if not mine:
            return None
        lines = ["A reviewer rejected the previous attempt. Address these before "
                 "anything else — each cites the requirement or plan line it comes from:"]
        for f in mine:
            where = " (%s%s)" % (f.get("file", ""),
                                 ":%s" % f["line"] if f.get("line") else "") if f.get("file") else ""
            lines.append("- [%s]%s %s" % (f.get("cite", "uncited"), where, f.get("claim", "")))
        lines.append("If you believe a finding is wrong, say so in your report with "
                     "evidence rather than silently ignoring it.")
        return "\n".join(lines)

    def _reopen_subtasks(self, owners=None):
        """Mark subtasks pending again. When the reviewer named owners, only
        those reopen -- reworking everything would discard green work."""
        def reopen(state):
            hit = False
            for s in state["subtasks"]:
                named = owners and any(s["id"] in o for o in owners)
                if not owners or named:
                    s["status"] = "pending"
                    hit = True
            if not hit and state["subtasks"]:
                state["subtasks"][0]["status"] = "pending"
        self.task.mutate(reopen)

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
        chaff = winnow.as_text(self.task.state.get("winnow"))
        if chaff:
            note += "\n\n" + chaff
        text, problems = brief.write(
            self.task, "merge",
            "Land this change? It is complete and its checks pass. Nothing has been "
            "committed: attended mode leaves a patch file for you to apply and commit "
            "yourself.",
            files=files, verify=result, extra="## Review\n\n" + note,
            polish=self._polish)
        for p in problems:
            self.log("  brief lint: %s" % p)
        if self.budget.requires_approval("merge"):
            if not self.gate("merge", text):
                raise Halt("needs_human", "merge declined by human")
        self._land()
        self.task.update(status="done")

    def _land(self):
        """attended: write the diff as a patch for the human to apply. The
        orchestrator has no path that commits to the user's branch (§9.2)."""
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
            self._push_and_open_pr(branch)

    def _push_and_open_pr(self, branch):
        """Autonomous mode ends at an opened PR. There is deliberately no merge
        path here (§9.2) -- the credential may even be branch-restricted."""
        import shutil as _shutil
        push = subprocess.run(["git", "push", "-u", "origin", branch],
                              cwd=self.repo, capture_output=True, text=True)
        if push.returncode != 0:
            raise Halt("needs_human", "could not push %s: %s"
                       % (branch, push.stderr.strip()[:200]))
        self.log("integrate: pushed %s" % branch)
        if _shutil.which("gh") is None:
            self.log("           gh not installed — open the PR yourself")
            return
        body = self.task.read_text("brief.md", "")
        pr = subprocess.run(
            ["gh", "pr", "create", "--head", branch, "--title",
             "%s: %s" % (self.task.state["id"], self._title()), "--body", body],
            cwd=self.repo, capture_output=True, text=True)
        if pr.returncode != 0:
            self.log("           gh pr create failed: %s" % pr.stderr.strip()[:200])
            return
        url = (pr.stdout or "").strip().splitlines()[-1:] or [""]
        self.log("integrate: opened %s" % url[0])
        self.task.update(pull_request=url[0])

    def _title(self):
        for line in self.task.read_text("task.md", "").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith(">"):
                return line[:70]
        return "delegated change"

    # ---------------------------------------------------------------- infra
    def _channel_state(self):
        """-> (now, cooled, utilization, entries). One clock read feeds routing,
        so every gate in a single selection sees the same instant."""
        now = self.clock()
        cools, usage, warning = cooldown.read(now)
        if warning and not self._warned_channels:
            # Once per run: a corrupt breaker file would otherwise repeat this
            # on every selection and bury the rest of the log.
            self._warned_channels = True
            self.log("channels: %s" % warning)
        util = {}
        for name, chan in (self.reg.get("channels") or {}).items():
            util[name] = cooldown.utilization(usage.get(name), chan.get("quota"), now)
        return now, set(cools), util, cools

    def _pick(self, role, boost=None, exclude=()):
        """Best candidate this runtime can actually launch. The registry says
        what a deployment could use; only the adapter knows what is installed,
        so an uninstalled CLI is skipped rather than crashing the run.

        Channels in a quota cooldown are filtered exactly like `disabled`, and
        when a cooldown is the *only* reason nothing is left, the caller gets an
        error that knows when the seats come back."""
        _, cooled, util, entries = self._channel_state()
        blocked = cooled | set(exclude or ())
        candidates = self.router.candidates(role, ceiling=self.budget.ceiling(),
                                            boost=boost, cooldowns=blocked,
                                            utilization=util)
        for c in candidates:
            if self.adapter.can_run(c.agent_kind):
                if c.demoted:
                    self.log("  reserve: %s on %s anyway — no alternative"
                             % (role, c.channel))
                return c
        if blocked:
            # Would this role have had somewhere to go with nothing filtered?
            # If so this is a quota event with a known reopen time, not a
            # registry mistake, and the two want different answers.
            free = [c for c in self.router.candidates(
                        role, ceiling=self.budget.ceiling(), boost=boost,
                        utilization=util)
                    if self.adapter.can_run(c.agent_kind)]
            hit = sorted({c.channel for c in free} & blocked)
            if hit:
                reopen = cooldown.earliest_reopen(entries, hit)
                raise routing.AllChannelsCooled(
                    role, hit, reopen,
                    "every channel enrolled for role %r is in a quota cooldown "
                    "(%s); the first reopens at %s. `delegate channels` shows "
                    "them, `delegate channels --clear <name>` overrides one."
                    % (role, ", ".join(hit), _stamp(reopen)))
        raise routing.NoModelAvailable(
            "no runnable model for role %r: %s" % (
                role, ", ".join("%s needs %s" % (c.model, c.agent_kind)
                                for c in candidates) or "nothing enrolled"))

    def _meter(self, choice):
        """Count one invocation against the channel's window (§5.4). An
        estimate by construction -- no provider exposes a meter -- kept so the
        router drifts off a filling seat before it hits the wall."""
        window = quota.parse_window((choice.chan_spec.get("quota") or {}).get("window"))
        cooldown.record_use(choice.channel, window, self.clock())

    def _reopen_at(self, choice, res):
        """The provider's own reset time, else the channel's configured window
        from now. A parse failure is logged, never silently swallowed."""
        stated = res.get("reset_at")
        if stated:
            return float(stated)
        spec = choice.chan_spec.get("quota") or {}
        window = quota.parse_window(spec.get("window"))
        self.log("  quota: %s stated no reset time — assuming its %s window"
                 % (choice.channel, spec.get("window") or "default 5h"))
        return self.clock() + window

    def _cool(self, choice, res):
        """Open the breaker for a channel that just said it is out."""
        at = self._reopen_at(choice, res)
        cooldown.open_breaker(choice.channel, "quota", at, self.clock(),
                              detail=(res.get("output") or "")[-200:])
        return at

    def _ensure_worktree(self):
        state = self.task.state
        if state.get("worktree") and os.path.isdir(state["worktree"]):
            return state["worktree"]
        base = git(["rev-parse", "HEAD"], self.repo)
        branch = "adg/%s/work" % state["id"]
        path = os.path.join(os.path.dirname(os.path.abspath(self.repo)),
                            ".adg-worktrees", state["project_key"], "%s-work" % state["id"])
        self.log("worktree: %s (%s)" % (path, self.adapter.name))
        self.adapter.create_worktree(self.repo, branch, base, path)
        repo_info = dict(state["repo"], base_commit=base)
        self.task.update(worktree=path, branch=branch, repo=repo_info)
        return path

    def _archive_reports(self, match):
        """Move a stage's previous reports aside before it runs again. Reviewers
        are told to check whether their earlier findings were addressed, which
        is impossible against a file the next round overwrites."""
        src = self.task.file("reports")
        if not os.path.isdir(src):
            return
        dest = self.task.file("reports", "archive")
        for name in sorted(os.listdir(src)):
            if not name.endswith(".json") or match not in name:
                continue
            os.makedirs(dest, exist_ok=True)
            n, target = 1, None
            while target is None or os.path.exists(target):
                target = os.path.join(dest, "%s-r%d.json" % (name[:-5], n))
                n += 1
            os.replace(os.path.join(src, name), target)

    def _run_once(self, role, choice, cwd, **kw):
        """Invoke, close, and return the report. For roles that get one turn."""
        session, report = self._invoke(role, choice, cwd, **kw)
        self._close(session)
        return report

    def _direct(self, role, choice, text, timeout=180):
        """One prompt, one answer, no report file -- the text-reply roles
        (§4.6). Shares the metering, billing and quota failover that `_invoke`
        gives every other role, which three hand-rolled copies of this dance
        did not."""
        session = self.adapter.start_agent(role, choice.agent_kind, self.repo,
                                           prompts.env_for(self.task))
        try:
            res = self.adapter.prompt(session, text, timeout=timeout)
        finally:
            self._close(session)
        if res.get("failure") == "quota_exhausted":
            at = self._cool(choice, res)
            nxt = self._pick(role, exclude=(choice.channel,))
            self.log("failover: %s %s -> %s (quota, reopens %s)"
                     % (role, choice.channel, nxt.channel, _stamp(at)))
            return self._direct(role, nxt, text, timeout)
        self._meter(choice)
        self._bill(res, choice)
        return res

    def _invoke(self, role, choice, cwd, subtask=None, extra=None,
                session=None, failure=None):
        """Run one turn. With `session`, it continues that agent instead of
        starting a fresh one -- a retry keeps everything the agent already
        learned, which is most of the wall clock on a short task. Returns the
        session so a caller can continue it again."""
        if self.dry_run:
            self.log("  [dry-run] would run %s as %s" % (choice.model, role))
            return None, None
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
        self._bill(res, choice)
        outcome = "complete" if res.get("settled") == "idle" else "blocked"
        report, problems = self._collect_report(role, subtask, since=started)

        if report is None:
            outcome = "blocked"
        self.task.record_delegation({
            "stage": self.task.state["status"], "role": role,
            "subtask": subtask.get("id") if subtask else None,
            "model": choice.model, "channel": choice.channel,
            "adapter": self.adapter.name, "outcome": outcome,
            "usd": res.get("cost_usd"),
            "usd_estimated": bool(res.get("cost_estimated")) or None,
            "report_problems": problems or None,
        })
        if outcome == "blocked" and res.get("settled") == "timeout":
            self._close(session)
            raise Halt("needs_human", "%s agent timed out" % role)
        # Returned, never stashed on self: wave threads share this object, and
        # a sibling overwriting the attribute is how an escalation gets read as
        # a success.
        return session, report

    def _close(self, session):
        if session is not None:
            try:
                self.adapter.teardown(session)
            except Exception as e:
                # Teardown must never mask the real outcome, but swallowing it
                # silently hides adapter bugs that only show up as leaked panes.
                self.log("  warning: teardown failed: %s: %s" % (type(e).__name__, e))

    def _bill(self, res, choice):
        """Charge a run. Some CLIs report money, some report only tokens; those
        get priced from our own registry and marked as an estimate, because a
        derived number and a billed one are different facts."""
        if res.get("cost_usd"):
            self.budget.spend(res["cost_usd"])
            return res["cost_usd"]
        tok = res.get("usage")
        if not tok or not choice:
            return None
        spec = choice.spec or {}
        usd = (tok["in"] / 1e6) * float(spec.get("cost_in", 0)) + \
              (tok["out"] / 1e6) * float(spec.get("cost_out", 0))
        if usd <= 0:
            return None
        res["cost_usd"], res["cost_estimated"] = round(usd, 6), True
        self.budget.spend(res["cost_usd"])
        return res["cost_usd"]

    def _log_transcript(self, role, prompt, res):
        """Keep what the agent was asked and what it said. An agent that does
        nothing is otherwise invisible -- which is exactly when you need it."""
        with _LOG_LOCK:
            _LOG_SEQ[0] += 1
            seq = _LOG_SEQ[0]
        name = os.path.join("agent-logs", "%s-%s-%03d.log" % (
            role, time.strftime("%Y%m%d-%H%M%S", time.gmtime()), seq))
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

    REPORT_PROMPT = """Rewrite the notes below as a short update for a competent
programmer who has never seen this repository. Keep the markdown headings.

Rules:
- Lead with the decision they must make. Do not bury it.
- Expand every internal id the first time you use it: write "the requirement
  that old saves still load (AC-2)", never a bare "AC-2". Never use the words
  rung, REPLAN, REQUEST_CHANGES, test_stuck or scope_overrun.
- Say what each changed file is for. They do not know this codebase.
- State plainly anything that was NOT verified.
- No praise, no filler, no restating the task twice.
- Reproduce any markdown table exactly as given. Those are facts, not prose.

--- NOTES ---
%s
"""

    def _polish(self, text):
        """Render the mechanical brief into plain language. Cheap, and the only
        part of the run a human actually reads -- but a failed or jargon-laden
        rewrite falls back to the template rather than replacing it."""
        if self.dry_run:
            return text
        try:
            choice = self._pick("reporter")
        except routing.NoModelAvailable:
            return text
        res = self._direct("reporter", choice, self.REPORT_PROMPT % text)
        out = (res.get("output") or "").strip()
        if len(out) < 80 or brief.lint(out):
            return text
        return out

    def _gate(self, kind, question):
        files = sorted({f for s in self.task.state["subtasks"] for f in s.get("actual_files", [])})
        extra = None
        if kind == "design":
            extra = self.task.read_text("spec.md", "")
        text, problems = brief.write(self.task, kind, question, files=files,
                                     extra=extra, polish=self._polish)
        for p in problems:
            self.log("  brief lint: %s" % p)
        if not self.gate(kind, text):
            raise Halt("needs_human", "%s declined by human" % kind)
        self.task.record_gate(kind, "approved")
