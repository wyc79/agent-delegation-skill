"""The task state machine.

Deterministic control flow. LLMs choose among edges the graph already offers,
through validated outputs -- they never invent a transition. Every run is
replayable from task.json.

Scope: the full pipeline, including parallel subtasks in separate worktrees, the
Integrator, and an independent Test Author. The ladder walks rungs 0, 2, 3 and 4
 -- rung 1, "a different model at the same tier", is not built: the router
escalates by raising a capability floor, which has no same-tier expression.
"""

import os
import re
import subprocess
import time

from . import (brief, companions, cooldown, limits as lim, prompts, quota,
               router as routing, schema, verify, winnow, workflow as wf,
               yamlite)
from .store import git

# Statuses the machine can actually be resumed at -- `cli.cmd_resume` validates
# `--stage` against this list. "verify" is deliberately absent: checks run inside
# `implement` and `review` rather than as a stage of their own, so there is no
# `_stage_verify`. Listing it let a resume pass validation and then park with
# "no handler for stage 'verify'", which is the exact outcome that validation was
# added to prevent.
STAGES = ["intake", "classify", "brainstorm", "plan", "implement",
          "review", "integrate", "done"]
TERMINAL = {"done", "abandoned", "needs_human"}

# The orchestrator's identity for the commits it makes on its own account, on
# every command that writes one. `_checkpoint` always passed it, because the
# pipeline must not depend on the user having configured git -- but the merges
# did not, and a merge writes a commit too. On a machine with no committer
# identity (a fresh container, a CI image, a repo a tool cloned) a fast-forward
# still succeeds while a real merge exits non-zero with "Committer identity
# unknown". `_integrate_wave` reads any non-zero exit as a conflict: it paid an
# integrator to resolve one that did not exist, found no unmerged paths, ran the
# checks against a branch nothing had been merged into, and marked the subtask
# landed. A run that reported success, with none of that subtask's work in the
# patch.
IDENTITY = ["-c", "user.email=orchestrator@agent-delegation", "-c", "user.name=adg"]

# , signal -> the rung the ladder enters at. Only agent-raised
# signals reach this table; the counter-driven ones are read off verify.
#
# `low_confidence` is deliberately absent rather than mapped high. D4 makes it a
# tiebreaker, so on its own it carries no routing information -- and an absent
# key falls through to "nothing to route on", which is the honest answer.
# `merge_conflict_cross` is the Integrator's signal; arriving from an
# implementer it means something the implementer cannot fix either.
ENTRY_RUNG = {
    "test_stuck": 2,            # more capability on the same understanding
    "edit_churn": 2,
    "scope_overrun": 2,
    "plan_conflict": 3,         # a stronger implementer cannot fix a wrong plan
    "ambiguous_requirement": 3, # the planner owns it; rung 4 is reached by
                                # exhausting rung 3, never by skipping it
    "blocked_command": 4,       # no model and no plan lifts a sandbox refusal
    "missing_dependency": 4,    # ... or installs a package
    "merge_conflict_cross": 4,
}

# Keyword matching on natural language does not work: "API" in "calculator API"
# forced a 12-line change down the full planning pipeline, and no keyword list
# recognises "add a subtract function" as simple. Classification is a judgement,
# so a cheap model makes it -- the call costs ~$0.0002 against a ~$2.60 mistake.
# Only non-negotiable facts stay hard-coded, below.
#
# The frame is code and the judgement is not. `_classified` routes on the two
# tiers by name, so the vocabulary and the VERDICT line the parser reads have to
# stay here -- a workflow that invented a third tier would parse as neither and
# fall through to the safe default. What a workflow CAN say is which work counts
# as which, through `classify.criteria` in its manifest.
CLASSIFY_PROMPT = """Classify this task as SIMPLE or COMPLEX.

%(criteria)s

--- REQUEST ---
%(request)s

--- REPOSITORY ---
%(facts)s

Reply with exactly one line:
VERDICT: SIMPLE|COMPLEX -- <one short clause of reasoning>
"""

# Used when the workflow in force declares no `classify.criteria`. Silence in a
# manifest leaves a stage as the machine has it -- the same rule `enabled()`
# follows -- so a workflow that has no opinion about what counts as complex
# still gets a working classifier rather than a prompt with a hole in it.
DEFAULT_CLASSIFY_CRITERIA = """SIMPLE: one coherent change a single competent engineer would finish in one
sitting without a written plan. A few files, no new architecture, no format or
interface that other code depends on.

COMPLEX: needs a plan first -- multiple independent parts, a new abstraction or
dependency, a change to a shared interface / schema / save format, wide blast
radius, or genuine ambiguity about what is wanted.

Bias: when the work is small and self-contained, say SIMPLE. Planning a small
task wastes far more than it saves."""


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


class AwaitingApproval(Exception):
    """A gate has nobody to ask right now, so the task parks holding the
    question rather than answering it.

    Distinct from a decline, and the distinction is the whole point. `_confirm`
    used to hit `EOFError` with no tty, print "declining", and return False --
    which recorded a rejection the human never made and killed the run. That was
    tolerable while `delegate` was driven by a person at a terminal. It is fatal
    once the caller is the user's own agent shelling out, because an agent has
    no tty either: every gate would auto-decline.

    Parking instead turns the gate from a prompt into a return value. The CLI
    exits holding the brief and the question, the user's agent renders it in
    whatever way reads well, the human answers in prose, and `delegate approve
    --note "..."` / `reject --note "..."` writes the decision back. The note
    then flows into the next stage's prompt, which is strictly more than y/N
    ever carried.

    `resume_status` is where the run continues once approved, and it is not
    always the stage that asked: the design and plan gates sit at the END of
    their stage, so re-entering it would re-run the planner and re-author the
    tests for a question that has already been answered. The merge gate is
    mid-stage -- `_land()` follows it -- so that one does re-enter, and
    `_stage_integrate` consumes the recorded decision before building a brief
    it no longer needs.
    """

    def __init__(self, kind, brief, resume_status):
        super().__init__("awaiting approval at the %s gate" % kind)
        self.kind, self.brief, self.resume_status = kind, brief, resume_status


class Replan(Exception):
    """Rung 3: the model ladder is spent, so the plan is the suspect.

    Deliberately not a Halt. Rung 2 is documented as "skipped entirely if
    nothing enrolled sits above the current model", and the rung after it is
    the planner, not the human -- but the code went straight to needs_human,
    so a deployment with one strong seat had a two-rung ladder that ended in a
    shrug. Repeated failure by the best model available is evidence about the
    plan; handing that to a human without letting the planner see it wastes the
    one reader who can act on it cheaply.

    Becomes a halt on its own when `max_replans` is spent -- that is rung 4,
    reached by exhausting rung 3 rather than by skipping it.
    """

    def __init__(self, subtask, message):
        super().__init__(message)
        self.subtask = subtask


class Orchestrator:
    def __init__(self, task, registry, adapter, gate, log=print, dry_run=False,
                 clock=time.time):
        self.task = task
        self.reg = registry
        self.router = routing.Router(registry)
        # The workflow in force. Read once per run and held, because two stages
        # of one task reading different manifests would be two different
        # workflows wearing one task id.
        self.workflow = wf.current()
        self.adapter = adapter
        self.gate = gate            # callable(kind, brief_text) -> bool
        self.log = log
        self.dry_run = dry_run
        # The machine's only wall clock. Cooldown expiry, utilisation windows
        # and reopen times all derive from it, so a run replays under a fixed
        # clock exactly as it ran.
        self.clock = clock
        self._warned_channels = False
        # Breaker-write failures already reported. `_meter` runs after every
        # single invocation, so an unwritable state dir would otherwise repeat
        # one line per call for the whole run; the fault is a property of the
        # file, not of the call that noticed it.
        self._warned_writes = set()
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
                # Switched off by the manifest. Checked here rather than inside
                # each handler because the handlers choose their own successor
                # -- `_stage_classify` sends simple work straight past
                # `brainstorm` -- so this only decides which of the
                # destinations they name is actually switched on.
                if not self.workflow.enabled(status):
                    # STAGES, not the manifest's own order: `enabled()` treats a
                    # stage the manifest never mentions as on, so the sequence
                    # walked to find the next one has to contain those too.
                    nxt = self.workflow.next_enabled(status, order=STAGES)
                    self.log("%s: disabled by workflow %r — skipping to %s"
                             % (status, self.workflow.data.get("name", "?"), nxt))
                    self.task.update(status=nxt)
                    continue
                handler = getattr(self, "_stage_" + status, None)
                if handler is None:
                    raise Halt("needs_human", "no handler for stage %r" % status)
                try:
                    handler()
                except Replan as r:
                    # Caught inside the loop, not beside it: rung 3 continues the
                    # run at the plan stage, and catching it out there would end
                    # the run instead -- the very thing this rung exists to stop.
                    self._rung3(r)
        except AwaitingApproval as a:
            # Not a Halt: `needs_human` means the run is over and something is
            # wrong, and a question waiting to be answered is neither. Keeping
            # them apart is what lets `delegate status` say "waiting on you"
            # instead of "broken", and what stops `resume` from restarting at
            # `implement` the way it does for a genuine park.
            self.task.update(status="awaiting_approval",
                             pending_gate={"kind": a.kind, "brief": a.brief,
                                           "resume_status": a.resume_status,
                                           "at": self.clock()})
            self.log("\n== AWAITING APPROVAL: %s gate" % a.kind)
            return "awaiting_approval"
        except routing.AllChannelsCooled as q:
            # Ahead of the generic handler: this is not a crash and not a
            # registry mistake, and reporting it as either sends the reader to
            # the wrong fix.
            self._quota_park(q)
            return "needs_human"
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
            # stderr is not a state the user can act on.
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
        found = companions.detect(self.repo)
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
        # Routed on the cheap classifier tier; this is not judgement.
        res = self._optional("intake", self.CRITERIA_PROMPT % {
            "request": text.strip()[:4000], "facts": self._repo_facts()},
            pick_as="classifier")
        if res is None:
            return
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
                with open(os.path.join(self.repo, f), "rb") as fh:
                    sizes.append((len(fh.read().splitlines()), f))
            except OSError:
                continue
        sizes.sort(reverse=True)
        lines = ["%d tracked files, %d total lines" % (len(files), sum(n for n, _ in sizes))]
        lines.append("largest: " + ", ".join("%s (%d lines)" % (f, n) for n, f in sizes[:8]))
        if self.vcfg.get("hotspots"):
            lines.append("declared hotspots: " + ", ".join(self.vcfg["hotspots"]))
        return "\n".join(lines)

    def _stage_classify(self):
        """A cheap model judges; facts and the caller override it."""
        text = self.task.read_text("task.md", "")
        hotspots = [h for h in (self.vcfg.get("hotspots") or []) if h and h in text]
        forced = self.task.state.get("tier")
        if forced in ("simple", "complex"):
            # `--tier`, and it is the most specific thing here. The caller is the
            # agent that already decided this work was worth delegating -- the
            # skill's "first decide whether to delegate at all" -- and one that
            # has designed the decomposition already knows its shape. Paying a
            # billed call to be told what it has decided, and to be told
            # differently often enough to matter, is the indirection this
            # removes. `auto` is still the default, so `delegate` run by hand
            # keeps judging for itself.
            if forced == "simple" and hotspots:
                # A hotspot is the PROJECT's standing declaration; this flag is
                # the operator's, for one run. The later and more specific
                # instruction wins, but not silently: the declaration exists
                # because these files merge badly when the work is not planned.
                self.log("classify: --tier simple overrides a declared hotspot "
                         "(%s) — this run is not getting a plan"
                         % ", ".join(hotspots))
            return self._classified(forced, "declared by the caller", "caller")
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
        prompt = CLASSIFY_PROMPT % {
            "criteria": self.workflow.criteria("classify") or DEFAULT_CLASSIFY_CRITERIA,
            "request": text.strip()[:4000],
            "facts": self._repo_facts()}
        res = self._optional("classifier", prompt)
        if res is None:
            # Fail safe, exactly as an unparseable verdict does: over-planning a
            # small task wastes time, under-planning a large one wastes the run.
            return "complex", "no classifier model available", "fallback"
        out = (res.get("output") or "")
        m = re.search(r"VERDICT:\s*(SIMPLE|COMPLEX)\s*-*\s*(.*)", out, re.I)
        if not m:
            # Unparseable: fail safe. Over-planning a small task wastes time;
            # under-planning a large one wastes the whole run.
            return "complex", "classifier gave no usable verdict", "fallback"
        # Carries what it spent, like every other step. The classifier is a real
        # billed call on a real seat; recording it without its cost made the
        # cheapest-looking step in the run the one nothing could account for.
        self.task.record_delegation({"stage": "classify", "role": "classifier",
                                     "model": res.get("model"), "channel": res.get("channel"),
                                     "adapter": self.adapter.name, "outcome": "complete",
                                     "usd": res.get("cost_usd"),
                                     "usd_estimated": bool(res.get("cost_estimated")) or None,
                                     "tokens": res.get("usage"),
                                     "elapsed_ms": res.get("elapsed_ms")})
        return (m.group(1).lower(),
                m.group(2).strip()[:200] or "no reason given", res.get("model"))

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
        # Asked of the manifest, not decided here. This was a hardcoded `if
        # superpowers installed` with both texts inline -- which meant hosting
        # any other design discipline was a patch to this function rather than
        # a workflow edit, and that is precisely what stopped this project being
        # a runtime that hosts a method instead of one that owns it.
        discipline = self.workflow.discipline(
            "brainstorm", self.task.state.get("companions"))
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
        # Guarded like the other two. It was unconditional, so `design` in
        # `human_approval_required` did nothing and removing it did nothing
        # either -- the one gate a policy could not turn off was the one whose
        # stage is already optional.
        if self.budget.requires_approval("design"):
            self._gate("design", "Approve this design before a plan is written from it? "
                                 "Answer any open questions below, or accept the "
                                 "defaults by approving.", resume_status="plan")
        self.task.update(status="plan")

    def _stage_plan(self):
        choice = self._pick("planner")
        self._archive_reports("plan-")
        self.log("plan: %s via %s" % (choice.model, choice.channel))
        self.budget.check_cost(0.0)
        # The template is the WORKFLOW's, unlike the report schemas, so it moves
        # with `--workflow` -- but only if it is there. Naming a file a hosted
        # workflow does not ship sends the planner hunting for it instead of
        # planning, which is the same objection as pointing it at a schemas
        # directory that does not exist.
        extra = "Write plan.md now."
        template = os.path.join(prompts.skill_path(), "templates", "plan.md")
        if os.path.exists(template):
            extra += " Use the template at %s." % template
        if self.task.read_text("spec.md", "").strip():
            extra += ("\n\nAn approved design is at %s. Plan against it: it settles "
                      "the approach, so decompose and scope rather than redesigning. "
                      "Departing from it is a decision to record in decisions.md."
                      % self.task.file("spec.md"))
        if self.task.read_text("escalation.md", "").strip():
            # Rung 3 arrived here. Replanning the same decomposition would spend
            # the replan budget reproducing the failure it was granted to escape.
            extra += (
                "\n\nThis is a REPLAN. An implementation escalated to you: read %s "
                "first. The previous decomposition is the suspect — a subtask "
                "failed repeatedly under the strongest model available, so "
                "reissuing the same shape will fail the same way. Change "
                "something structural: split it, re-scope it, resequence it, or "
                "state plainly in plan.md why it is still right and what the "
                "implementer should do differently.\n"
                "Every completed subtask listed there must be dispositioned in "
                "plan.md as keep, adapt or discard. Work that is already green is "
                "reset to pending when your plan lands, so anything you leave out "
                "will be built again." % self.task.file("escalation.md"))
        self._run_once("planner", choice, cwd=self.repo, extra=self._with_note(extra))
        subtasks = self._read_plan_subtasks()
        if not subtasks:
            raise Halt("needs_human", "planner produced no usable subtasks in plan.md")
        self.task.update(subtasks=self._reseed(subtasks))
        self.log("plan: %d subtask(s)" % len(subtasks))
        self._author_tests(subtasks)
        if self.budget.requires_approval("plan"):
            self._gate("plan", "Approve this plan? It splits the work into %d step(s)."
                       % len(subtasks), resume_status="implement")
        self.task.update(status="implement")

    def _reseed(self, planned):
        """Subtasks from a new plan, keeping what a surviving id already owns.

        A replan reissues ids, and `_read_plan_subtasks` builds every subtask
        from plan.md alone -- so `worktree` and `base_commit` were dropped while
        the checkout and the branch stayed on disk. `_subtask_worktree` then
        reused that checkout, which `create_worktree` does deliberately and
        which makes it ignore the `base` it is handed, and recorded TODAY's
        integration tip as the commit the subtask was cut from. The subtask's
        diff was then measured against a commit its worktree had never seen: it
        was credited with every file its siblings had landed and those files
        were reported as scope violations it never committed.

        The other half is the worktree a new plan simply drops. `_reap_worktrees`
        names paths out of `state["subtasks"]`, so a renamed subtask leaves a
        checkout nobody can name and the reaper walks past it forever.

        What a reissued id may NOT keep is a branch the plan it was written for
        has been thrown away with. Only subtasks that reach `_stage_plan` with
        `merged` set have their commits on the integration branch and so
        accounted for; an id reissued over an unmerged one -- the ordinary shape
        of a REPLAN, since it is the failing subtask that raised it -- carried a
        branch holding the discarded plan's attempt. `_prepare` re-reads HEAD
        *above* those commits, so `actual_files` and the scope check never saw
        them, while `_integrate_wave` merged the branch whole and landed them in
        the delivered patch: work for a goal the planner had abandoned, in the
        change, accounted for nowhere. Those are set aside by `_shelve_branch`
        and the id is cut fresh from the integration tip.
        """
        old = {s["id"]: s for s in (self.task.state.get("subtasks") or [])}
        out = []
        for s in planned:
            prev = old.pop(s["id"], None) or {}
            fresh = dict(s, status="pending", actual_files=[])
            if prev and not prev.get("merged"):
                prev = {} if self._shelve_branch(prev) else prev
            # `merged` deliberately does NOT travel. Whatever the previous
            # incarnation landed is on the integration branch already, and the
            # work this id is about to do under its new goal is not -- carrying
            # the flag would tell `_unfinish` the new commits were safe when a
            # later merge of them was aborted.
            for key in ("worktree", "base_commit"):
                if prev.get(key):
                    fresh[key] = prev[key]
            out.append(fresh)
        for gone in old.values():
            if not gone.get("worktree"):
                continue
            try:
                self.adapter.remove_worktree(self.repo, gone["worktree"])
                self.log("  worktree: removed %s — the new plan drops %s"
                         % (gone["worktree"], gone["id"]))
            except Exception as e:            # noqa: BLE001 -- best effort, as in _reap
                self.log("  worktree: left %s in place (%s)" % (gone["worktree"], e))
        return out

    def _shelve_branch(self, sub):
        """Set aside the checkout and branch of a subtask whose plan was
        discarded, so the id can be cut fresh. True when it is safe to forget.

        The branch is the point, not the directory. `create_worktree` is
        deliberately idempotent -- it reattaches an existing branch and ignores
        the base it is handed -- so removing the checkout alone would hand the
        next dispatch exactly the same commits back.

        Renamed rather than deleted. The commits are the record of an attempt
        the escalation bundle refers to, and this is the one place the pipeline
        would destroy work outright; `adg/<task>/<id>` is a name the machine
        owns, so moving it aside costs nothing a human might want. If the
        checkout will not go (the Windows lock case `_reap_worktrees` documents)
        nothing is renamed and the caller keeps the subtask as it was: a stale
        base is a smaller fault than a branch pointing at a worktree that is
        still on disk.
        """
        if sub.get("worktree"):
            try:
                self.adapter.remove_worktree(self.repo, sub["worktree"])
            except Exception as e:            # noqa: BLE001 -- best effort, as in _reap
                self.log("  worktree: left %s in place (%s) — %s keeps it"
                         % (sub["worktree"], e, sub["id"]))
                return False
        branch = "adg/%s/%s" % (self.task.state["id"], sub["id"])
        if not git(["rev-parse", "--verify", "--quiet", "refs/heads/" + branch],
                   self.repo, check=False):
            return True
        for n in range(1, 100):
            shelf = "%s.replaced-%d" % (branch, n)
            if git(["rev-parse", "--verify", "--quiet", "refs/heads/" + shelf],
                   self.repo, check=False):
                continue
            git(["branch", "-m", branch, shelf], self.repo, check=False)
            self.log("  %s: the new plan reissues it — its unmerged branch is "
                     "set aside as %s, and it starts from the integration branch"
                     % (sub["id"], shelf))
            return True
        return False

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
                        # Carried, not dropped. subtask.schema.json asks the
                        # planner for these and they were landing nowhere:
                        # `capability_hint` now routes (below), and the other two
                        # are read by the agent out of plan.md but belong in
                        # task.json so a run's record shows what was planned.
                        "capability_hint": s.get("capability_hint") or {},
                        "estimated_loc": s.get("estimated_loc"),
                        "parallel_group": s.get("parallel_group"),
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
                state = self.task.update(subtasks=self._reseed(recovered))
                self.log("implement: recovered %d subtask(s) from plan.md" % len(recovered))
            else:
                raise Halt("needs_human", "no subtasks and no parseable plan.md")
        pending = [s for s in state["subtasks"] if s.get("status") != "complete"]
        if not pending:
            self.task.update(status="review")
            return
        wave = self._wave(state["subtasks"])
        if not wave:
            # Nothing is ready and nothing is running: every pending subtask is
            # waiting on a dependency that will never complete -- a cycle, or a
            # `depends_on` naming an id the plan never defines. The planner
            # writes those ids, so this is an ordinary bad plan, and indexing
            # the empty wave below turned it into `IndexError: list index out of
            # range` and a crash.log. Say which subtask waits on what instead:
            # the fix is an edit to plan.md, and the reader has to know that.
            # `_wave` is empty only when `_ready` is, so every pending subtask
            # here has at least one dependency outstanding -- no empty-list case
            # to write around.
            done = {s["id"] for s in state["subtasks"] if s.get("status") == "complete"}
            known = {s["id"] for s in state["subtasks"]}
            stuck = ["%s waits on %s" % (s["id"], ", ".join(
                d if d in known else "%s (no such subtask)" % d
                for d in (s.get("depends_on") or []) if d not in done))
                for s in pending]
            raise Halt("needs_human",
                       "no subtask can start: every pending one is blocked by a "
                       "dependency that will never complete (%s). Fix depends_on "
                       "in plan.md, then `delegate resume --stage implement`."
                       % "; ".join(stuck))
        if len(wave) > 1:
            self.log("implement: %d subtasks in parallel (%s)"
                     % (len(wave), ", ".join(t["id"] for t in wave)))
            self._run_wave(wave)
            return
        sub = wave[0]
        tree = sub.get("worktree")
        if tree and os.path.isdir(tree):
            # It owns a worktree from an earlier wave. Continue there rather
            # than in the integration worktree: sending it elsewhere would
            # restart it from nothing and strand its checkpoints, including
            # whatever `_salvage` committed when a seat died mid-subtask, which
            # is the salvage point the keep-the-worktree-on-a-park rule is for.
            # `_catch_up` is what makes that safe -- it brings the checkout to
            # the integration branch first, so a rework is not answered against
            # a tree from before its siblings landed.
            self._dispatch(sub, tree)
            self._integrate_wave([sub])
            return
        self._dispatch(sub, self._ensure_worktree())

    def _prepare(self, sub, tree):
        """Make the tree current and settle what this subtask's diff is measured
        against. Called serially, never from a wave thread: `_catch_up` merges,
        which touches refs in the common git directory, and concurrent git
        against one repository is what the serial worktree creation below
        already exists to avoid."""
        self._catch_up(sub, tree)
        self._remember_base(sub, git(["rev-parse", "HEAD"], tree, check=False))
        return tree

    def _dispatch(self, sub, tree):
        """Run one subtask in `tree`, having settled what its diff is measured
        against.

        The base is read from the tree itself, every dispatch, rather than
        written once when a worktree is created. Recording the commit we *asked*
        to cut from was wrong twice over: `create_worktree` reuses an existing
        branch and ignores the base it is handed, and a rework may run in a
        different tree from the one the subtask started in. Reading HEAD after
        the checkout is prepared cannot disagree with the tree, whichever path
        arrived here.
        """
        self._one_subtask(sub, self._prepare(sub, tree))

    def _catch_up(self, sub, tree):
        """Bring a reused subtask worktree up to the integration branch.

        A subtask's own checkout is a snapshot of the moment it was cut. Coming
        back for a rework, it is missing everything its siblings landed in the
        meantime -- so the checks ran against an incomplete tree and a reviewer
        finding citing a sibling's file pointed at a file the agent could not
        see. Where the branch is already merged this is a fast-forward and
        cannot conflict; where it is not (an interrupted subtask carrying
        salvage commits) a real merge is attempted and abandoned cleanly if it
        will not go, because a half-merged worktree is worse than a stale one
        and the agent can still work from its own checkpoints.
        """
        state = self.task.state
        branch = state.get("branch")
        if not branch or self.dry_run:
            return
        tip = git(["rev-parse", "--verify", "--quiet", "refs/heads/" + branch],
                  self.repo, check=False)
        if not tip:
            return
        have = subprocess.run(["git", "merge-base", "--is-ancestor", tip, "HEAD"],
                              cwd=tree, capture_output=True)
        if have.returncode == 0:
            return                      # already has everything on the branch
        merge = subprocess.run(["git"] + IDENTITY + ["merge", "--no-edit", branch],
                               cwd=tree, capture_output=True, text=True)
        if merge.returncode != 0:
            subprocess.run(["git", "merge", "--abort"], cwd=tree,
                           capture_output=True)
            self.log("  %s: could not bring its worktree up to %s (%s) — it "
                     "continues from its own checkpoints"
                     % (sub["id"], branch,
                        (merge.stdout + merge.stderr).strip().splitlines()[-1:] or ["conflict"]))
            return
        self.log("  %s: worktree brought up to %s" % (sub["id"], branch))

    def _one_subtask(self, sub, base):
        role_choice = self._pick_implementer(sub)
        # All per-subtask state stays local: this method runs concurrently in a
        # wave, and anything on self is shared with the siblings.
        attempts, session, report, failure = 0, None, None, None
        while True:
            self.budget.check_attempt(sub["id"])
            self.budget.check_cost(0.0)
            attempts += 1
            self.log("implement %s: attempt %d (%s%s)" % (
                sub["id"], attempts, role_choice.model, "" if session is None else ", continued"))
            session, report, role_choice = self._invoke(
                "implementer", role_choice, cwd=base, subtask=sub,
                session=session, failure=failure,
                extra=self._with_note(self._findings_brief(sub)))
            self.budget.used_attempt(sub["id"])
            result = verify.run(self.task, self.repo, base, "fast")
            self.log("  verify: %s" % result.summary())
            claimed = (report or {}).get("status")
            if claimed and claimed != "complete":
                self.log("  %s: agent reported %s" % (sub["id"], claimed))
            if claimed == "blocked":
                # The agent's own account is that nothing further is possible.
                # That is rung 4 by definition, not a routing decision.
                raise Halt("needs_human", "%s reported blocked: %s" % (
                    sub["id"], (report or {}).get("summary", "")[:200]))
            if claimed == "escalate":
                rung, signals = self._entry_rung(report)
                named = ", ".join(s.get("type", "?") for s in signals)
                if rung >= 4:
                    raise Halt("needs_human", "%s reported escalate (%s): %s" % (
                        sub["id"], named or "no signal to route on",
                        (report or {}).get("summary", "")[:200]))
                self.log("  %s: escalate on %s — entering the ladder at rung %d"
                         % (sub["id"], named, rung))
                role_choice, attempts, session = self._climb(
                    sub, rung, role_choice, attempts, result, report, session, signals)
                failure = self._signal_context(signals)
                continue
            if result.ok and not sub.get("actual_files") and not self._touched(base, sub):
                # Only for a subtask that has never produced anything. Now that
                # the diff base follows the tree, a rework round that changes
                # nothing also shows an empty diff -- and halting the run there
                # would be a new failure mode invented as a side effect: an
                # implementer that ignores its findings is what the reviewer and
                # `max_review_loops` are for, and they end the run with a verdict
                # rather than a park. The case this guard exists for is the
                # first attempt on green checks, and that is unchanged.
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
            # same model try the same idea a fourth time.
            threshold = int((self.reg["policy"].get("escalation_thresholds") or {})
                            .get("test_stuck_attempts", 3))
            if attempts >= threshold:
                role_choice, attempts, session = self._climb(
                    sub, 2, role_choice, attempts, result, report, session)
        self._finish_subtask(sub, base)

    def _pick_implementer(self, sub):
        """The implementer for one subtask, raised by its `capability_hint`.

        The hint is the planner's read on difficulty, and subtask.schema.json has
        always promised it "raises the router's requirements for this subtask" --
        which nothing did, so a subtask marked `{reasoning: very_high}` drew the
        same model as a one-line rename.

        Advisory on the way down, though: a hint no enrolled model can clear must
        not park a task. The planner is guessing about difficulty, and a guess
        that stops the run outright is worse than a guess that gets the ordinary
        implementer. The demotion is logged, because a subtask running below the
        strength its plan asked for is exactly the thing you want to see in the
        log when it later escalates.
        """
        boost = routing.as_boost(sub.get("capability_hint"))
        if not boost:
            return self._pick("implementer")
        try:
            return self._pick("implementer", boost=boost)
        except routing.AllChannelsCooled:
            raise
        except routing.NoModelAvailable as e:
            choice = self._pick("implementer")
            self.log("  %s: plan asked for %s and nothing enrolled clears it — "
                     "running on %s (%s)"
                     % (sub["id"], sub.get("capability_hint"), choice.model, e))
            return choice

    def _entry_rung(self, report):
        """Which rung an agent's own `escalate` enters at, and the signals that
        decided it.

        **Highest rung wins.** An agent reporting `test_stuck` *and*
        `missing_dependency` needs the human; climbing to a stronger model first
        spends a seat on a package that is still not installed.

        **No routable signal is rung 4, deliberately.** `escalate` means "a
        stronger model, a re-plan, or a human" and the signals are what say
        which. With nothing to read, the orchestrator cannot pick on the agent's
        behalf, and guessing spends real money on a guess. This is also what
        keeps D4 honest: `low_confidence` is not in ENTRY_RUNG, so a report
        carrying only that lands here rather than escalating on a feeling."""
        signals = [s for s in ((report or {}).get("signals") or [])
                   if isinstance(s, dict)]
        routable = [s for s in signals if s.get("type") in ENTRY_RUNG]
        if not routable:
            return 4, signals
        return max(ENTRY_RUNG[s["type"]] for s in routable), routable

    def _climb(self, sub, rung, role_choice, attempts, result, report, session,
               signals=None):
        """One ladder for both entrances: the counter that fires `test_stuck`
        off verify, and an agent that reports the same thing itself. They differ
        in what noticed, never in where the ladder goes.

        Returns the next (choice, attempts, session), or raises Replan when the
        rung above is the planner."""
        if rung <= 2:
            _, cooled, util, _ = self._channel_state()
            stronger = self.router.escalate("implementer", role_choice,
                                            ceiling=self.budget.ceiling(),
                                            cooldowns=cooled, utilization=util)
            if stronger is not None:
                # Naming the new slot alone overstated this. Registry model
                # names are capability slots, and the runtime launches a CLI by
                # agent kind without pinning a model (runtime.LAUNCH), so
                # escalating within one agent kind changes the bookkeeping and
                # the session -- not the model that actually runs. Say which
                # happened: on a single-seat deployment it is always the latter,
                # and "escalating to <stronger model>" promised a heavier read
                # that nothing delivered.
                self.log("  escalating to %s via %s — %s"
                         % (stronger.model, stronger.channel,
                            "a different agent"
                            if stronger.agent_kind != role_choice.agent_kind else
                            "same agent kind, so this is a fresh session rather "
                            "than a heavier model"))
                # A stronger model starts clean: the point of escalating is a
                # different approach, not more of the same context. This half
                # happens either way, and is what makes the rung worth taking
                # when the model cannot change.
                self._close(session)
                return stronger, 0, None
            # Rung 2 is spent, so the next rung is 3 -- the planner -- not 4.
            why = ("%s still failing after %d attempts and nothing stronger is "
                   "enrolled within the ceiling" % (sub["id"], attempts))
        else:
            why = ("%s raised %s, which no stronger implementer can fix"
                   % (sub["id"], ", ".join(s.get("type", "?")
                                           for s in (signals or []))))
        # Write the bundle here, while the verify output and this agent's report
        # are still in hand; the planner runs in a later stage and a different
        # process.
        self._write_escalation_bundle(sub, attempts, result, report, signals)
        self._close(session)
        raise Replan(sub["id"], why)

    @staticmethod
    def _signal_context(signals):
        """What the replacement agent is told, so `attempted` stops being
        decoration: the next model is stronger, not clairvoyant
        (references/escalation.md)."""
        out = []
        for s in signals or []:
            out.append("The previous agent raised %s: %s"
                       % (s.get("type", "?"), (s.get("detail") or "").strip()))
            ev = (s.get("evidence") or "").strip()
            if ev:
                out.append(ev[:800])
            for a in (s.get("attempted") or [])[:6]:
                out.append("Already tried, and it failed: %s" % str(a)[:200])
            sug = (s.get("suggestion") or "").strip()
            if sug:
                out.append("Its suggestion, which is not binding on you: %s" % sug[:400])
        return "\n".join(out) or None

    def _write_escalation_bundle(self, sub, attempts, result, report, signals=None):
        """Escalation without context just repeats the failure expensively.

        Append-only, like `deviations.md`: `max_replans` can exceed one, and the
        second replan needs to see that the first already tried something.

        The completed-work inventory is the part that is easy to leave out and
        expensive to omit. `_stage_plan` resets every subtask to pending from
        the new plan, so a planner that does not know what is already green will
        cheerfully re-plan work that is finished.

        `signals` is present when the agent raised the escalation itself. The
        headline has to follow it: reporting an agent's `plan_conflict` under a
        hardcoded `test_stuck` sends the planner hunting a flaky test that was
        never the complaint.
        """
        state = self.task.state
        done = [s for s in (state.get("subtasks") or [])
                if s.get("status") == "complete"]
        if signals:
            headline = (
                "**Signal:** `%s`, raised by the implementer itself. It stopped at "
                "the threshold rather than pushing through, so this is evidence "
                "about the plan and not a failed attempt."
                % ", ".join(s.get("type", "?") for s in signals))
        else:
            headline = (
                "**Signal:** `test_stuck`. The same checks failed %d consecutive "
                "attempts, and no stronger model is enrolled within the ceiling, so "
                "the ladder has no rung left below you. Treat this as evidence about "
                "the plan, not about the agent." % attempts)
        lines = [
            "## %s — rung 3 after %d attempt(s)" % (sub["id"], attempts),
            "",
            headline,
            "",
            "**Goal as planned:** %s" % (sub.get("goal") or "(none recorded)"),
            "**Planned scope:** %s" % (", ".join(sub.get("planned_scope") or []) or "(none)"),
            "",
            # An agent can escalate over a plan conflict while every check is
            # green. Titling that "Failing checks" tells the planner to go
            # looking for a failure nobody reported.
            "### Failing checks" if not result.ok else
            "### Checks when it stopped — they were green",
            "```",
            (result.summary() or "").strip() or "(no summary)",
        ]
        for f in result.failures()[:3]:
            lines += ["", "$ %s" % f["cmd"], (f["output"] or "")[-1200:]]
        lines += ["```", ""]

        summary = ((report or {}).get("summary") or "").strip()
        lines += ["### The implementer's last word",
                  summary[:1000] or "(it wrote no report)", ""]

        if signals:
            # `attempted` is the field that stops the next reader repeating
            # ruled-out work, and it is worthless if it never leaves the report.
            lines.append("### What it raised, and what it already ruled out")
            for s in signals:
                lines.append("- **`%s`** — %s" % (s.get("type", "?"),
                                                  (s.get("detail") or "").strip()[:400]
                                                  or "(no detail given)"))
                ev = (s.get("evidence") or "").strip()
                if ev:
                    lines += ["", "  ```", "  " + ev[:800].replace("\n", "\n  "), "  ```"]
                for a in (s.get("attempted") or [])[:6]:
                    lines.append("  - tried, and it failed: %s" % str(a)[:200])
                sug = (s.get("suggestion") or "").strip()
                if sug:
                    lines.append("  - its suggestion, non-binding: %s" % sug[:300])
            lines.append("")

        # Disposition is the planner's job, but the list has to come from here:
        # it is the only place that still knows what was green before the reset.
        lines.append("### Completed work — disposition each one (keep / adapt / discard)")
        if done:
            lines += ["- `%s` — %s%s" % (
                s["id"], s.get("goal") or "(no goal)",
                " [files: %s]" % ", ".join(s.get("actual_files") or [])
                if s.get("actual_files") else "") for s in done]
        else:
            lines.append("- (nothing is complete yet — the whole plan is open)")
        lines.append("")

        dev = self.task.read_text("deviations.md", "").strip()
        if dev:
            lines += ["### Deviations logged so far", dev[-2000:], ""]

        prev = self.task.read_text("escalation.md", "")
        self.task.write_text("escalation.md",
                             (prev + "\n" if prev.strip() else "") + "\n".join(lines) + "\n")

    def _rung3(self, exc):
        """Send it back to the planner, or to the human when replans are spent.

        `check_replan` raises LimitBreach, which the run loop already turns into
        needs_human -- that is rung 4, and it is reached by exhausting rung 3
        rather than by skipping it.
        """
        self.budget.check_replan()
        self.budget.used_replan()
        self.log("  rung 3: %s — replanning (%s)"
                 % (exc, self.budget.summary()))
        self.task.update(status="plan", replan_reason=str(exc))

    def _run_wave(self, wave):
        """Each subtask in its own worktree, concurrently, then merged in
        dependency order. Threads are fine here: every agent is a subprocess."""
        import threading
        results = {}
        # Serial creation and preparation: concurrent git against one repository
        # contends on .git locks and fails intermittently on a healthy run, and
        # `_prepare` merges the integration branch into a reused checkout.
        trees = {t["id"]: self._prepare(t, self._subtask_worktree(t)) for t in wave}

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
        # Merge what finished BEFORE reporting what did not. `_integrate_wave`
        # used to run only after the whole wave came back clean, so one failing
        # sibling stranded every green one: `_finish_subtask` had already marked
        # them complete, and a complete subtask never rejoins a wave, so nothing
        # ever merged their branches. The run resumed, finished the survivor,
        # reached `done`, and delivered a patch missing work that `actual_files`
        # and the merge brief both listed as changed -- a silent loss inside a
        # run that reported success.
        #
        # The invariant this restores: a subtask marked `complete` has its work
        # on the integration branch. That is what makes the brief's file list
        # and the patch the same account of the change.
        finished = [s for s in wave
                    if not isinstance(results.get(s["id"]), Exception)]
        # Held, not raised on the spot. A merge that will not go in is real, but
        # it must not outrank a sibling's routing outcome: raising it here threw
        # away a `Replan` -- the ladder's rung 3, with its escalation bundle
        # already written -- and would swallow a quota park's reopen time, which
        # is the exact "the sequential and parallel paths disagreed about the
        # same event" failure the re-raises below exist to prevent.
        integration_error = None
        if finished:
            try:
                self._integrate_wave(finished)
            except (Halt, routing.AllChannelsCooled) as e:
                # `AllChannelsCooled` as well, now that `_reconcile` stops
                # flattening it: held under the same rule as a `Halt` so the
                # precedence below still decides. Raised on its own at the end
                # it reaches `run()`'s park handler with its reopen time intact.
                integration_error = e
        for sub in wave:
            outcome = results.get(sub["id"])
            if isinstance(outcome, (Replan, routing.AllChannelsCooled)):
                # Re-raised, not flattened into a Halt. A Replan continues the
                # run at the plan stage, and a quota park carries a reopen time
                # and drives `resume --when-open`; downgrading either gave the
                # wave path a different answer from the sequential one for an
                # identical event.
                if integration_error is not None:
                    self.log("  wave: the finished members did not integrate "
                             "cleanly (%s) — reporting %s's outcome first, "
                             "because that is what decides where the run goes "
                             "next" % (integration_error, sub["id"]))
                raise outcome
            if isinstance(outcome, Exception):
                if integration_error is not None:
                    self.log("  wave: the finished members did not integrate "
                             "cleanly either (%s)" % integration_error)
                raise Halt("needs_human", "%s failed: %s" % (sub["id"], outcome))
        if integration_error is not None:
            raise integration_error

    def _reap_worktrees(self):
        """Remove this task's worktrees, once and only once it is done.

        Nothing removed them before, so `.adg-worktrees/<project-key>/` grew one
        directory per subtask per task forever while two READMEs called them
        throwaway. On a repo of any size that is the largest thing this program
        leaves behind.

        **Only on `done`.** A parked or crashed task's worktree holds the
        salvaged, checkpointed work an agent was interrupted mid-way through --
        it is what a human resumes from, and what `_salvage` commits into before
        a failover hop. Reaping on any terminal state would delete exactly the
        cases where the files still matter. Landing has already happened by
        here: `_land` writes the patch out of the integration worktree before
        the status moves.

        Best-effort by construction. `remove_worktree` is documented as
        deferring to `git worktree prune` on Windows, where engines and
        antivirus hold locks, so a failure is logged and never raised -- a task
        that finished must not be reported as broken because a directory
        survived it.
        """
        state = self.task.state
        paths = [state.get("worktree")]
        paths += [s.get("worktree") for s in (state.get("subtasks") or [])]
        removed = 0
        for path in [p for p in paths if p]:
            try:
                self.adapter.remove_worktree(self.repo, path)
                removed += 1
            except Exception as e:            # noqa: BLE001 -- see the docstring
                self.log("  worktree: left %s in place (%s)" % (path, e))
        if removed:
            self.log("worktree: removed %d, the task is done" % removed)

    def _integration_tip(self):
        """The commit a new subtask worktree is cut from.

        The task's integration branch as it stands, NOT `base_commit`. Cutting
        from the task base gave every wave a checkout of the repository as it
        was before the task started, which broke two promises at once:
        `references/parallelism.md` says a subtask worktree is "cut from the
        task's integration branch", and `roles/implementer.md` tells the agent
        that `depends_on` "tells you what already exists". Neither held -- a
        second wave could not see the first wave's merged output, so a subtask
        that depended on one already finished began by importing a file that was
        not there. The test author's failing tests were invisible the same way:
        they are committed into the integration worktree during `plan`, and a
        wave cut from the base never contained them, so the checks an
        implementer ran were green because the requirement was absent.

        Falls back to the base when the branch does not exist yet, which is the
        first worktree of the task and the one case where they are the same
        commit anyway.
        """
        state = self.task.state
        branch = state.get("branch")
        if branch:
            tip = git(["rev-parse", "--verify", "--quiet", "refs/heads/" + branch],
                      self.repo, check=False)
            if tip:
                return tip
        return state["repo"].get("base_commit", "HEAD")

    def _subtask_base(self, sub=None):
        """What a subtask's diff is measured against.

        Its own worktree's starting commit, which is no longer the task base now
        that waves are cut from the integration branch: measuring against the
        task base would report every file an earlier subtask landed as this
        one's work, and then as a scope violation it never committed.

        A subtask working directly in the integration worktree has no recorded
        base and keeps the task's, which is what it has always used.
        """
        return ((sub or {}).get("base_commit")
                or self.task.state["repo"].get("base_commit", "HEAD"))

    def _subtask_worktree(self, sub):
        state = self.task.state
        branch = "adg/%s/%s" % (state["id"], sub["id"])
        path = os.path.join(os.path.dirname(os.path.abspath(self.repo)),
                            ".adg-worktrees", state["project_key"],
                            "%s-%s" % (state["id"], sub["id"]))
        self.adapter.create_worktree(self.repo, branch, self._integration_tip(), path)
        # Persisted, because `_run_wave` holds these in a local dict that is gone
        # long before the task reaches `done` -- and a reaper that cannot name
        # what it created is a reaper that leaves everything behind. The diff
        # base is NOT recorded here: `create_worktree` may reuse an existing
        # branch and ignore the base it was handed, so only reading HEAD back
        # off the prepared tree (`_prepare`) is guaranteed to match it.
        def _record(st):
            for t in st.get("subtasks") or []:
                if t.get("id") == sub["id"]:
                    t["worktree"] = path
                    sub["worktree"] = path
        self.task.mutate(_record)
        return path

    def _remember_base(self, sub, commit):
        """Record the commit this subtask's diff is measured against.

        Rewritten on every dispatch, not kept from the first. The base has to
        follow the tree: a rework runs in a worktree that has since been brought
        up to the integration branch, and holding the original commit measured
        the diff from before the siblings landed -- which credited the subtask
        with their files, called them scope violations it never committed, and
        disarmed the "changed no files" guard, since a sibling's work always
        looked like its own. `actual_files` stays cumulative across dispatches
        (`_finish_subtask`), so nothing is lost by moving the base forward.

        Written to the caller's dict as well as to task.json. `_run_wave` hands
        each thread the dict it read before the worktree was prepared, and
        `_finish_subtask` reads the diff base from that dict.
        """
        if not commit:
            return
        def _record(st):
            for t in st.get("subtasks") or []:
                if t.get("id") == sub["id"]:
                    t["base_commit"] = commit
                    sub["base_commit"] = commit
        self.task.mutate(_record)

    def _integrate_wave(self, wave):
        """Merge each green branch into the task branch, verifying after each --
        so the integration branch is always green and every merge is tested
        against everything already landed.

        Stopping part-way has to leave two things true, and neither used to be.
        The worktree must be clean: `_reconcile` could raise out of an
        unfinished merge, and the run does not always end there -- a sibling's
        `Replan` continues at the plan stage, where `_author_tests` runs
        `git add -A && git commit` and committed the conflict markers as the
        resolution, shipping them in the patch. And a subtask still marked
        `complete` must be on this branch: the members after the failure never
        merged, and a complete subtask never rejoins a wave, so their work was
        delivered as done and absent. They are reopened instead.
        """
        integration = self._ensure_worktree()
        for i, sub in enumerate(wave):
            branch = "adg/%s/%s" % (self.task.state["id"], sub["id"])
            merge = subprocess.run(["git"] + IDENTITY + ["merge", "--no-edit", branch],
                                   cwd=integration, capture_output=True, text=True)
            if merge.returncode != 0:
                self.log("  conflict merging %s" % sub["id"])
                try:
                    self._reconcile(sub, integration, merge.stdout + merge.stderr)
                except Exception:         # noqa: BLE001 -- re-raised below
                    # Any exception, not only `Halt`. `_reconcile` dispatches an
                    # agent, so it can raise `AllChannelsCooled` (every
                    # integrator seat walled mid-conflict) or an adapter
                    # `RuntimeError_` (the CLI vanished) -- neither is a `Halt`,
                    # and both used to unwind straight past this handler. The
                    # cleanup is what matters, not which failure skipped it: the
                    # tree is left mid-merge with MERGE_HEAD set and conflict
                    # markers in the files, and the run does not always end here.
                    # `_checkpoint` and `_author_tests` are blind
                    # `git add -A && git commit`, so the markers were committed
                    # as the resolution and shipped in the patch.
                    self._abort_merge(integration)
                    self._unfinish(wave[i:])
                    raise
            result = verify.run(self.task, self.repo, integration, "fast")
            if not result.ok:
                # The merge commit itself is sound and stays; what failed is the
                # combination. This subtask owns that, so it goes back to pending
                # along with everything behind it in the wave.
                self._unfinish(wave[i:])
                raise Halt("needs_human",
                           "%s broke the integration branch once merged" % sub["id"])
            # Landed, and recorded so `_reap_worktrees` and a later rework can
            # tell a merged branch from one still waiting.
            def _mark(state, sid=sub["id"]):
                for s in state.get("subtasks") or []:
                    if s.get("id") == sid:
                        s["merged"] = True
            self.task.mutate(_mark)
            sub["merged"] = True

    @staticmethod
    def _abort_merge(cwd):
        """Put the worktree back the way it was. A merge nobody resolved leaves
        MERGE_HEAD set and conflict markers in the files, and every later
        `_checkpoint` is a blind `git add -A && git commit`."""
        subprocess.run(["git", "merge", "--abort"], cwd=cwd, capture_output=True)

    def _unfinish(self, subs):
        """Send subtasks that did not make it onto the integration branch back
        to pending, so `complete` keeps meaning `its work is on the branch`.
        Their worktrees and branches are untouched -- the agent that picks one
        up again finds its own work already there."""
        ids = {s["id"] for s in subs}
        def reopen(state):
            for s in state.get("subtasks") or []:
                if s["id"] in ids and s.get("status") == "complete" \
                        and not s.get("merged"):
                    s["status"] = "pending"
        self.task.mutate(reopen)
        for s in subs:
            if not s.get("merged"):
                s["status"] = "pending"

    def _reconcile(self, sub, cwd, conflict_text):
        """A conflict two agents produced is judgement, not mechanism: which
        side matches the plan, and what did the other one mean."""
        try:
            choice = self._pick("integrator")
        except routing.AllChannelsCooled:
            # Re-raised, never flattened. `AllChannelsCooled` subclasses
            # `NoModelAvailable` precisely so a mandatory stage propagates it to
            # the park handler, which meant the broader handler below swallowed
            # the one exception carrying a reopen time: `run()`'s dedicated
            # `except routing.AllChannelsCooled` was never reached, `_quota_park`
            # never ran, and no `park` was written. `delegate status` showed a
            # bare `needs_human` and `resume --when-open` returned immediately,
            # so a run that was merely early read as broken.
            raise
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

    def _touched(self, cwd, sub=None):
        """Did anything actually change in this worktree?"""
        return bool(verify.changed_files(
            self.repo, cwd, self._subtask_base(sub),
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
        git(IDENTITY + ["commit", "-q", "-m", message], cwd, check=False)
        return True

    def _finish_subtask(self, sub, cwd):
        files = verify.changed_files(self.repo, cwd, self._subtask_base(sub),
                                     ignore=self.vcfg.get("ignore"))
        # Everything this tree still changes against the task's own base. The
        # union below has to be cumulative -- the diff base moves forward with
        # the tree on every dispatch, so a rework measured from the refreshed
        # base reports only what the rework touched, while the merge brief and
        # the scope check want everything this subtask has written. But a union
        # can only grow, so a file a rework DELETED could never leave it: the
        # deletion is itself a change, so the path came straight back in
        # `files`. It went on being reported as a scope violation, went on
        # blocking `_skip_review`, and was still shown to the human at the merge
        # gate as a changed file the patch does not contain. Intersecting keeps
        # the accumulation and drops the paths whose net effect is nothing. It
        # cannot pull in a sibling's file, because nothing enters this list
        # except through this subtask's own `files`.
        net = set(verify.changed_files(
            self.repo, cwd, self.task.state["repo"].get("base_commit", "HEAD"),
            ignore=self.vcfg.get("ignore")))
        def mark(state):
            for s in state["subtasks"]:
                if s["id"] == sub["id"]:
                    s["status"] = "complete"
                    s["actual_files"] = sorted(
                        (set(s.get("actual_files") or []) | set(files)) & net)
                    s["scope_violations"] = verify.scope_violations(
                        s["actual_files"], sub.get("planned_scope") or ["**"])
                    sub["actual_files"] = s["actual_files"]
            # Only the findings THIS subtask owned. Clearing the whole list let
            # the first subtask to finish disarm the rework for every other one:
            # a reviewer that rejected st-1 and st-2 saw st-1 fixed, and st-2 was
            # then re-dispatched by `_findings_brief` with nothing at all -- back
            # to the same code with the same prompt, rebuilding exactly what was
            # rejected, which is the failure the finding hand-off exists to stop.
            # Not a race, and not a wave problem at all: siblings in one wave are
            # dispatched before any of them finishes, so they all read the list
            # intact. It bites wherever subtasks go one after another -- a cap of
            # one, a dependency chain, or scopes that serialize them -- which is
            # the ordinary case.
            #
            # An unowned finding survives, because it is addressed to whoever is
            # reworking rather than to one subtask. The next verdict replaces the
            # list wholesale, so nothing outlives the review that raised it.
            state["pending_findings"] = [
                f for f in (state.get("pending_findings") or [])
                if not self._owned_by(f.get("suggested_owner"), sub["id"])]
        self.task.mutate(mark)
        violations = [s.get("scope_violations") or [] for s in self.task.state["subtasks"]
                      if s["id"] == sub["id"]]
        if violations and violations[0]:
            self.log("  scope: %d file(s) outside declared scope" % len(violations[0]))

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
        # The two inputs `roles/reviewer.md` step 1 calls "the diff, and the
        # verify output your prompt provides" -- and step 6 authorises a
        # `blocked` report when they never arrive. They never did: this prompt
        # named neither, so a card-compliant reviewer was entitled to stop the
        # run over a missing input the orchestrator was holding the whole time.
        # The diff is named rather than pasted; a large one does not belong in a
        # prompt, and the base commit is the part the reviewer cannot derive.
        extra = (
            "The change under review is everything between the task's base commit "
            "and HEAD in this worktree. Produce it yourself, and read it rather "
            "than any summary of it:\n"
            "  git diff %s\n\n"
            "The deterministic checks have already run: %s. Every command, exit "
            "code and full output is here, and it is the verify evidence your "
            "role card tells you to weigh:\n"
            "  %s\n"
            "Quote from it rather than re-running. If you do re-run something, "
            "say so and say why.\n\n"
            # The envelope, not the payload. `roles/reviewer.md` step 6, both
            # schemas and `_collect_report` all agree that a verdict rides
            # inside an ordinary report as `role_data.verdict` -- one envelope,
            # one thing to validate. This prompt was the last copy still naming
            # the verdict schema as the file to write, so a reviewer that obeyed
            # its prompt rather than its card produced a bare verdict object,
            # which `schema.validate_report` then rejected for every required
            # field it does not have. The card is pinned by a test; this string
            # was not, which is exactly how the two drifted.
            # Resolved against the RUNTIME's schemas directory, not the workflow
            # in force. `schema.schemas_dir()` says why -- the report envelope is
            # how this program reads a result, so it does not move with
            # `--workflow` and a hosted workflow need not ship a copy. Building
            # these two from `prompts.skill_path()` put a second, different
            # absolute path for the same file in the same prompt, and under any
            # workflow but the bundled one it named a file that need not exist.
            "Write reports/review-reviewer.json as an ordinary report matching "
            "%s, carrying your verdict under role_data.verdict, which must "
            "itself match %s."
            % (self.task.state["repo"].get("base_commit", "HEAD"),
               result.summary(),
               self.task.file("verify", result.run_id + ".json"),
               os.path.join(schema.schemas_dir(), "report.schema.json"),
               os.path.join(schema.schemas_dir(), "verdict.schema.json")))
        chaff = winnow.as_text(self.task.state.get("winnow"))
        if chaff:
            extra += ("\n\n%s\nRead these after the diff, not before. They may only "
                      "block if they independently land on authority — an acceptance "
                      "criterion, a plan line, or a stated non-goal. Otherwise record "
                      "them under `advisory`." % chaff)
        self._run_once("reviewer", choice, cwd=base, extra=self._with_note(extra))
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
        # A verdict is the authority on what is still outstanding, so each one
        # replaces the last one's findings rather than adding to them. The
        # branches below that do not carry findings forward clear them here:
        # `_finish_subtask` now only consumes the findings a subtask owned, so
        # an unowned one would otherwise outlive the review that raised it.
        if v == "APPROVE":
            self.task.update(pending_findings=[], status="integrate")
        elif v == "REQUEST_CHANGES":
            blocking = [f for f in verdict.get("findings", []) if f.get("severity") == "blocking"]
            if not blocking:
                # `severity: minor` is legal, so `blocking` can legally be
                # empty -- and then `owners` is empty, `_reopen_subtasks`
                # reopens EVERYTHING on its "no owners named" fallback, and
                # `pending_findings` is empty, so every green subtask is
                # rebuilt from scratch with no idea what was wrong. That is the
                # failure the finding hand-off exists to prevent, arriving
                # through a schema-legal verdict. verdict.schema.json defines
                # REQUEST_CHANGES as "blocking findings fixable within the
                # current plan"; one with none is incoherent, and guessing on
                # the reviewer's behalf costs a full rebuild.
                # The parentheses matter: `%` binds tighter than `or`, so
                # without them the fallback was unreachable and a verdict with
                # no findings at all trailed off after the colon.
                raise Halt("needs_human",
                           "the reviewer requested changes but raised no blocking "
                           "finding, so there is nothing to send back. Re-run the "
                           "review, or mark the finding blocking if it is: %s"
                           % (", ".join(f.get("claim", "?")[:80]
                                        for f in verdict.get("findings", [])[:3])
                              or "(it listed no findings at all)"))
            blocking = [self._addressable(f) for f in blocking]
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
            # Cleared, not carried: the planner is about to reissue the
            # decomposition, so findings citing subtask ids that may not survive
            # it would be handed to whoever inherited the id.
            self.task.update(pending_findings=[], status="plan")
        else:
            raise Halt("needs_human", "reviewer escalated: %s" %
                       (verdict.get("findings") or [{}])[0].get("claim", "no detail"))

    def _human_note(self):
        """The last thing a human said at a gate, formatted for a prompt.

        An approval with a qualification -- "yes, but keep the old endpoint
        working" -- is not approval of what was proposed. It approves something
        slightly different, and the agent that builds it has to be told, or the
        note is a record of an instruction nobody followed.

        It carries forward rather than being consumed by its first reader. A
        qualification on the plan applies to every subtask under that plan, not
        only the first one dispatched; and the reviewer needs it too, or it
        flags the retained endpoint as scope creep and rejects work that is
        doing exactly what the human asked for. Superseded by the next
        approval, never cleared on read.
        """
        gn = self.task.state.get("gate_note") or {}
        note = (gn.get("note") or "").strip()
        if not note:
            return ""
        return ("A human approved the %s with a qualification. It is part of what "
                "was approved, and where it conflicts with the written plan the "
                "human is right:\n\n  %s" % (gn.get("kind", "work"), note))

    def _with_note(self, extra):
        """Append the human's qualification to a prompt that may be empty."""
        note = self._human_note()
        if not note:
            return extra
        return "%s\n\n%s" % (extra, note) if extra else note

    @staticmethod
    def _owned_by(owner, subtask_id):
        """Does a reviewer's `suggested_owner` name this subtask?

        The schema documents the field as "Subtask id that should own the fix,
        e.g. impl:st-2", so the id may arrive bare or behind a `role:` prefix --
        which is why this was a substring test. But `"st-1" in "st-11"` is true,
        so a finding against `st-11` also reopened `st-1` and sent green work
        back to be rebuilt. Same defect class as the report-filename collision
        the protocol already guards against, arriving through the one field a
        reviewer writes by hand.

        Compared on the last `:`-separated token, which accepts both documented
        forms and neither more nor less.
        """
        return (owner or "").strip().split(":")[-1].strip() == subtask_id

    def _addressable(self, finding):
        """A finding whose owner this plan can actually deliver to.

        `suggested_owner` is written by hand by the reviewer, against ids the
        planner chose. One that names no subtask used to be worse than useless:
        `_reopen_subtasks` fell through to reopening `subtasks[0]`, and
        `_findings_brief` then filtered the finding out of that subtask's brief
        -- it has an owner, and this is not it -- so an agent was sent back to
        rebuild something with the reason withheld. Dropping the bad owner makes
        it unowned, which is the honest reading: it is addressed to whoever is
        reworking.
        """
        owner = finding.get("suggested_owner")
        if not owner:
            return finding
        known = [s["id"] for s in (self.task.state.get("subtasks") or [])]
        if any(self._owned_by(owner, sid) for sid in known):
            return finding
        self.log("  review: finding %s names %r, which is no subtask in this plan "
                 "— treating it as unaddressed so every reworking subtask sees it"
                 % (finding.get("id", "?"), owner))
        return dict(finding, suggested_owner=None)

    def _findings_brief(self, sub):
        """What the reviewer rejected, for the implementer that has to fix it."""
        findings = self.task.state.get("pending_findings") or []
        mine = [f for f in findings
                if not f.get("suggested_owner")
                or self._owned_by(f.get("suggested_owner"), sub["id"])]
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
        those reopen -- reworking everything would discard green work.

        `merged` is cleared with the status, because it describes the branch as
        it stood when it landed and a reopened subtask is about to add commits
        that have not. Leaving it set disarmed `_unfinish`, whose guard is
        `complete and not merged`: a rework whose own merge was aborted stayed
        `complete`, so on resume nothing was pending, the run fell through
        review to integrate, and `_land` delivered a patch and a `done` for work
        the reviewer had rejected and the implementer had redone. That is the
        silent-loss invariant `_unfinish` exists to hold -- `complete` means
        `its work is on the integration branch` -- defeated by a stale flag.
        """
        def reopen(state):
            hit = False
            for s in state["subtasks"]:
                named = owners and any(self._owned_by(o, s["id"]) for o in owners)
                if not owners or named:
                    s["status"] = "pending"
                    s.pop("merged", None)
                    hit = True
            if not hit and state["subtasks"]:
                state["subtasks"][0]["status"] = "pending"
                state["subtasks"][0].pop("merged", None)
        self.task.mutate(reopen)

    def _stage_integrate(self):
        # Before anything expensive. Unlike the other two gates this one sits
        # MID-stage -- `_land()` follows it -- so an approved run re-enters here,
        # and re-entry must not re-run the verification or rebuild a brief
        # through `_polish` to ask a question that is already answered.
        decided = self._decision_already_made("merge")
        if decided is False:
            raise Halt("needs_human", "merge declined by human")
        if decided:
            self._land()
            self.task.update(status="done")
            self._reap_worktrees()
            return

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
            # Recorded here rather than through _gate(), which this path has
            # never used: the merge gate carries a brief the others do not. Both
            # outcomes are written, so `gates` is a complete account of what a
            # human decided rather than a list of the times they said yes.
            try:
                approved = self.gate("merge", text)
            except AwaitingApproval as a:
                a.resume_status = "integrate"
                raise
            self.task.record_gate("merge", "approved" if approved else "declined")
            if not approved:
                raise Halt("needs_human", "merge declined by human")
        self._land()
        self.task.update(status="done")
        self._reap_worktrees()

    def _land(self):
        """attended: write the diff as a patch for the human to apply. The
        orchestrator has no path that commits to the user's branch."""
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
        path here -- the credential may even be branch-restricted."""
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

    def _pick(self, role, boost=None, exclude=(), min_reasoning=None):
        """Best candidate this runtime can actually launch. The registry says
        what a deployment could use; only the adapter knows what is installed,
        so an uninstalled CLI is skipped rather than crashing the run.

        Channels in a quota cooldown are filtered exactly like `disabled`, and
        when a cooldown is the *only* reason nothing is left, the caller gets an
        error that knows when the seats come back."""
        _, cooled, util, entries = self._channel_state()
        blocked = cooled | set(exclude or ())

        def ask(ignore_reserve):
            return self.router.candidates(role, ceiling=self.budget.ceiling(),
                                          boost=boost, cooldowns=blocked,
                                          utilization=util,
                                          ignore_reserve=ignore_reserve)

        # Two passes, and the order matters. A reservation only withholds a seat
        # while the role has somewhere else to go, and "somewhere else" means a
        # CLI this runtime can actually launch -- which the router cannot know.
        # Deciding it there withheld a healthy seat and killed the run instead.
        candidates = ask(False)
        pool = candidates + ask(True)
        if min_reasoning is not None:
            # A floor, not a `boost`: boost only strengthens capabilities the
            # role's profile already requires, and no profile requires
            # `reasoning` of its implementer -- so passing it there does nothing.
            strong = [c for c in pool
                      if int(c.spec.get("reasoning", 0) or 0) >= min_reasoning
                      and self.adapter.can_run(c.agent_kind)]
            if strong:
                return strong[0]
            raise routing.NoModelAvailable(
                "no runnable model for role %r at reasoning >= %s"
                % (role, min_reasoning))
        runnable = [c for c in pool if self.adapter.can_run(c.agent_kind)]
        if runnable:
            c = self._prefer_by_headroom(role, runnable, util)
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
            # Intersected with `cooled`, NOT with `blocked`. `blocked` also
            # carries `exclude` -- seats this run walked away from for reasons
            # that are not quota at all. A timeout hop appends to that list and
            # deliberately writes no breaker, so reporting those channels here
            # parked the task as `quota_all_exhausted` with ZERO open breakers:
            # `delegate status` said "waiting on quota", the brief told the
            # human their subscription was gone, and a provider wall that never
            # happened went into the record that exists to count them.
            hit = sorted({c.channel for c in free} & cooled)
            if hit:
                reopen = cooldown.earliest_reopen(entries, hit)
                raise routing.AllChannelsCooled(
                    role, hit, reopen,
                    "every channel enrolled for role %r is in a quota cooldown "
                    "(%s); the first reopens at %s. `delegate channels` shows "
                    "them, `delegate channels --clear <name>` overrides one."
                    % (role, ", ".join(hit), _stamp(reopen)))
        raise routing.NoModelAvailable(
            "no runnable model for role %r%s: %s" % (
                role,
                " (already tried and set aside this run: %s)"
                % ", ".join(sorted(exclude)) if exclude else "",
                ", ".join("%s needs %s" % (c.model, c.agent_kind)
                          for c in candidates) or "nothing enrolled"))

    # How many invocations the rest of this task needs, beyond the one being
    # dispatched. A LOWER bound and deliberately so: one implementer call per
    # unfinished subtask, plus a review and an integrate. Rework loops, test
    # authoring and escalations all push the real number up.
    #
    # Under-estimating is the safe direction here. This check only ever changes
    # which seat is preferred; a number that is too small prefers a seat
    # slightly too often, while one that is too large would walk away from
    # seats that could have finished the work.
    TAIL_CALLS = 2

    def _calls_remaining(self):
        pending = [s for s in (self.task.state.get("subtasks") or [])
                   if s.get("status") != "complete"]
        return max(1, len(pending)) + self.TAIL_CALLS

    def _headroom(self, channel, util):
        """Invocations left in this channel's window, or None when unknowable.

        None is not zero. A channel with no `est_capacity` has no meter and
        never had one -- treating that as "no room" would route away from every
        metered-key seat in the registry, which is exactly backwards.
        """
        chan = (self.reg.get("channels") or {}).get(channel) or {}
        cap = quota.parse_capacity((chan.get("quota") or {}).get("est_capacity"))
        if not cap:
            return None
        return cap * max(0.0, 1.0 - float(util.get(channel, 0.0)))

    def _prefer_by_headroom(self, role, runnable, util):
        """Among seats that can do the work, prefer one that can FINISH it.

        The router already prices draw -- a 90%-drawn seat costs more than a
        metered key -- but pricing is not the same question as "will this run
        get to the end here". A seat with four calls left is the cheapest
        option right up to the moment it walls mid-implementation.

        This never refuses work. If nothing has the headroom the run starts
        anyway on the best candidate and says so: a wrong estimate that parks a
        runnable task is worse than one that hops mid-run, because the hop
        already works and is tested.
        """
        need = self._calls_remaining()
        fits = [c for c in runnable
                if (self._headroom(c.channel, util) or float("inf")) >= need]
        if not fits:
            best = runnable[0]
            self.log("  headroom: no seat for %s has room for ~%d more call(s); "
                     "starting on %s anyway — a wall from here fails over"
                     % (role, need, best.channel))
            return best
        if fits[0] is not runnable[0]:
            self.log("  headroom: %s -> %s, %s has ~%d call(s) left and this task "
                     "needs ~%d"
                     % (role, fits[0].channel, runnable[0].channel,
                        self._headroom(runnable[0].channel, util) or 0, need))
        return fits[0]

    def _meter(self, choice):
        """Count one invocation against the channel's window. An
        estimate by construction -- no provider exposes a meter -- kept so the
        router drifts off a filling seat before it hits the wall."""
        window = quota.parse_window((choice.chan_spec.get("quota") or {}).get("window"))
        self._breaker_write(cooldown.record_use(choice.channel, window, self.clock()))

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

    # Two decisions, not one. Fusing them meant the only failure that could move
    # work to another seat was also the only failure that took a seat out of
    # service for five hours -- so a hung call had to either do both or do
    # nothing, and it did nothing.
    #
    #   quota_exhausted -> hop AND cool: the seat said it is out.
    #   timeout         -> hop, never cool: this call did not come back; the
    #                      seat is probably fine and cooling it on that evidence
    #                      hides a working provider for the afternoon.
    #   anything else   -> neither. An ordinary crash consumes an attempt. A
    #                      failover that fires on any error is worse than none,
    #                      because it hides real bugs behind provider hops.
    HOPS_TO_ANOTHER_SEAT = frozenset({"quota_exhausted", "timeout"})
    OPENS_THE_BREAKER = frozenset({"quota_exhausted"})

    def _cool(self, choice, res):
        """Open the breaker for a channel that just said it is out."""
        at = self._reopen_at(choice, res)
        self._breaker_write(
            cooldown.open_breaker(choice.channel, "quota", at, self.clock(),
                                  detail=(res.get("output") or "")[-200:]))
        return at

    def _breaker_write(self, warning):
        """Report a breaker file that could not be written.

        `cooldown._mutate` returns the reason precisely so a caller can say it
        -- its docstring is "it must not pretend the write happened" -- and both
        production callers dropped it on the floor, which made the promise in
        orchestrator/README.md that an unwritable file "is reported" false. A
        read-only or full `$XDG_STATE_HOME` then cost real money silently: the
        wall was never recorded, `delegate channels` showed every seat ready,
        and the next run picked the exhausted one again and paid for its
        refusal, every run, with nothing in the log to explain it.
        """
        if warning and warning not in self._warned_writes:
            self._warned_writes.add(warning)
            self.log("  warning: %s" % warning)

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
        # Recorded only when there was none. This runs again whenever the
        # checkout has gone missing -- a parked task whose worktree the user
        # deleted, which both READMEs invite by calling them throwaway -- and
        # `create_worktree` then reattaches the branch that already exists and
        # ignores the base it was handed. The branch still forks where it always
        # did, so overwriting the record with today's HEAD pointed every diff at
        # a commit the branch had never been cut from: `_land` wrote
        # `git diff <today> <branch>` into integrate.patch, which carries a
        # reverse-delta for every commit the user made while the task was
        # parked. Applying the patch this tool hands them would have reverted
        # their own work.
        repo_info = state["repo"]
        if not repo_info.get("base_commit"):
            repo_info = dict(repo_info, base_commit=base)
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
        session, report, _ = self._invoke(role, choice, cwd, **kw)
        self._close(session)
        return report

    def _optional(self, role, text, timeout=180, pick_as=None):
        """A text-reply role whose absence is survivable -> the result, or None.

        `pick_as` is the *capability profile* to route on when it differs from
        the agent's role name: intake is a classifier-tier job that the agent
        still runs as "intake", and only profiles named in the registry can be
        routed.

        Selection and invocation are guarded together on purpose. A seat that is
        already cooled and a seat that goes dark mid-call are the same fact to
        these callers, and guarding only the pick meant the second one escaped
        as a quota park -- parking a finished task because its brief could not
        be prettified."""
        try:
            choice = self._pick(pick_as or role)
            return self._direct(role, choice, text, timeout, pick_as=pick_as)
        except routing.NoModelAvailable as e:
            self.log("  %s: skipped — %s" % (role, e))
            return None

    def _direct(self, role, choice, text, timeout=180, pick_as=None, cooled=()):
        """One prompt, one answer, no report file -- the text-reply roles
       Shares the metering, billing and quota failover that `_invoke`
        gives every other role, which three hand-rolled copies of this dance
        did not. `pick_as` is the capability profile to re-route on, for roles
        whose name is not a profile (see `_optional`)."""
        session = self.adapter.start_agent(role, choice.agent_kind, self.repo,
                                           prompts.env_for(self.task, role))
        try:
            res = self.adapter.prompt(session, text, timeout=timeout)
        finally:
            self._close(session)
        reason = res.get("failure")
        if reason in self.HOPS_TO_ANOTHER_SEAT:
            at = self._cool(choice, res) if reason in self.OPENS_THE_BREAKER else None
            # Accumulated, exactly as `_invoke` does: if the breaker could not be
            # written -- an unwritable state dir, or a concurrent `--clear` --
            # excluding only the last seat lets this bounce A->B->A->B through
            # real, billed invocations until the stack runs out. That reasoning
            # binds harder for a timeout, which never writes a breaker at all,
            # so this list is the ONLY thing stopping the bounce.
            cooled = tuple(cooled) + (choice.channel,)
            nxt = self._pick(pick_as or role, exclude=cooled)
            self.log("failover: %s %s -> %s (%s%s)"
                     % (role, choice.channel, nxt.channel, reason,
                        ", reopens %s" % _stamp(at) if at else ", seat not cooled"))
            return self._direct(role, nxt, text, timeout, pick_as=pick_as,
                                cooled=cooled)
        self._meter(choice)
        self._bill(res, choice)
        # Who actually ran it. After a failover that is not the channel the
        # caller picked, and the delegation record must name the one that did.
        res["model"], res["channel"] = choice.model, choice.channel
        return res

    # A quota wall is the seat's fault, not the approach's, so the hop happens
    # here rather than in the callers: the failed invocation never returns, so
    # no caller can bill it against an attempt budget, and every role -- not
    # just the implementer -- gets failover for free.
    def _invoke(self, role, choice, cwd, subtask=None, extra=None,
                session=None, failure=None):
        """Run one turn. With `session`, it continues that agent instead of
        starting a fresh one -- a retry keeps everything the agent already
        learned, which is most of the wall clock on a short task. Returns the
        session and the channel that actually ran, so a caller that later
        escalates escalates from the right one."""
        if self.dry_run:
            self.log("  [dry-run] would run %s as %s" % (choice.model, role))
            return None, None, choice
        cooled, base_extra = [], extra
        while True:
            # Snapshot the reports directory: on a rework loop the previous
            # attempt's file is still on disk at the same path, and accepting it
            # would read a stale success as a fresh one. Comparing against what
            # was there before the turn needs no clock and no tolerance.
            before = self._report_state()
            if session is None:
                text = prompts.compose(role, self.task, subtask=subtask,
                                       extra=self._with_failure(extra, failure),
                                       verify_cfg=self.vcfg)
                session = self.adapter.start_agent(role, choice.agent_kind, cwd,
                                                   prompts.env_for(self.task, role))
                res = self.adapter.prompt(session, text, timeout=3600)
            else:
                text = prompts.retry(failure)
                res = self.adapter.follow_up(session, text, timeout=3600)
            self._log_transcript(role, text, res)
            reason = res.get("failure")
            if reason not in self.HOPS_TO_ANOTHER_SEAT:
                break
            # Close first: `_cool` touches a shared file, and letting it run
            # ahead of teardown meant a write failure leaked the session -- under
            # the herdr adapter, an orphaned pane.
            self._close(session)
            self._salvage(cwd, role, subtask)
            at = self._cool(choice, res) if reason in self.OPENS_THE_BREAKER else None
            # Appended whatever the reason: this list excludes the seat from THIS
            # task's remaining picks, which is a different thing from the global
            # breaker. A timed-out seat must not be handed the same call again on
            # the next iteration, but nothing about one hung call justifies
            # taking it away from every other task for five hours.
            cooled.append(choice.channel)
            self.task.record_delegation({
                "stage": self.task.state["status"], "role": role,
                "subtask": subtask.get("id") if subtask else None,
                "model": choice.model, "channel": choice.channel,
                "adapter": self.adapter.name, "outcome": reason,
                # A wall still costs wall-clock, and that time is exactly what
                # the failover path exists to shorten -- so it has to be on the
                # record even though the call bought nothing.
                "tokens": res.get("usage"), "elapsed_ms": res.get("elapsed_ms"),
                "reopens_at": at})
            # Excluded explicitly as well as through the breaker: if the state
            # file could not be written, the loop must still terminate.
            try:
                nxt = self._replacement(role, choice, cooled)
            except routing.NoModelAvailable:
                # Every seat has now been tried. Say what actually happened
                # rather than letting this reach the generic handler as a
                # crash: nothing is broken and nothing is out of quota, the
                # agents did not come back. This is the honest halt that the
                # old `outcome == "blocked" and settled == "timeout"` branch
                # used to raise before the hop existed.
                if reason == "timeout":
                    self._close(session)
                    raise Halt("needs_human",
                               "%s timed out on every enrolled seat (%s); no "
                               "breaker was opened -- the seats are not out of "
                               "quota, the calls did not return"
                               % (role, ", ".join(sorted(cooled))))
                raise
            self.log("failover: %s %s -> %s (%s%s)"
                     % (role, choice.channel, nxt.channel, reason,
                        ", reopens %s" % _stamp(at) if at else ", seat not cooled"))
            # A fresh session on the new seat, in the *same* worktree: every
            # checkpoint commit is still there, so the replacement continues
            # from the last one rather than restarting the subtask.
            # From the caller's `extra`, never from the previous hop's: on a
            # second hop, feeding the note back in appended it to a string that
            # already ended with it, and the replacement read the paragraph twice.
            extra = self._handover(base_extra, failure)
            choice, session, failure = nxt, None, None
        self._meter(choice)
        self._bill(res, choice)
        outcome = "complete" if res.get("settled") == "idle" else "blocked"
        report, problems = self._collect_report(role, subtask, before=before)

        if report is None:
            outcome = "blocked"
        self.task.record_delegation({
            "stage": self.task.state["status"], "role": role,
            "subtask": subtask.get("id") if subtask else None,
            "model": choice.model, "channel": choice.channel,
            "adapter": self.adapter.name, "outcome": outcome,
            "usd": res.get("cost_usd"),
            "usd_estimated": bool(res.get("cost_estimated")) or None,
            # Tokens and elapsed time are kept beside the money, not derived
            # from it. Only some CLIs report a cost at all -- where one does
            # not, `usd` is this program's own price table applied to these
            # token counts, so a run that stored only the money would be
            # comparing a measurement against an estimate with nothing on the
            # record to say which was which.
            "tokens": res.get("usage"),
            "elapsed_ms": res.get("elapsed_ms"),
            "report_problems": problems or None,
        })
        # No timeout branch here any more. Since the hop/cool split a timeout
        # never reaches this point: `failure` is "timeout", which is in
        # HOPS_TO_ANOTHER_SEAT, so the loop above hops instead of breaking out.
        # The honest halt this branch used to raise now lives at the hop site,
        # for the case where every seat has been tried.
        # Returned, never stashed on self: wave threads share this object, and
        # a sibling overwriting the attribute is how an escalation gets read as
        # a success.
        return session, report, choice

    def _replacement(self, role, choice, cooled):
        """The same rung on another seat.

        A quota hop must not walk back down the escalation ladder. If this
        subtask had already escalated to a stronger model, re-selecting on plain
        role requirements would quietly hand the work to a weaker one and log it
        as an ordinary failover -- the run would look like it was still climbing
        while it was descending. Ask for at least the current model's reasoning
        first; only drop below it when no seat can hold the rung, and say so."""
        floor = int(choice.spec.get("reasoning", 0) or 0)
        try:
            return self._pick(role, exclude=tuple(cooled), min_reasoning=floor)
        except routing.AllChannelsCooled:
            raise
        except routing.NoModelAvailable:
            nxt = self._pick(role, exclude=tuple(cooled))
            self.log("  note: no seat left at %s's strength — continuing on %s, "
                     "which is weaker" % (choice.model, nxt.model))
            return nxt

    def _salvage(self, cwd, role, subtask):
        """Commit whatever the killed agent left, so the replacement inherits a
        checkpoint rather than a dirty tree.

        A quota kill is involuntary -- the agent gets no turn to tidy up -- and
        `_checkpoint` otherwise runs only after a green verify, so a seat that
        dies on the first attempt leaves nothing committed at all. Without this
        the handover note below would be asserting something usually false.

        Guarded on the worktree: planner and brainstorm run with `cwd=self.repo`,
        and this program has no path that commits to the user's own branch."""
        if os.path.abspath(cwd) == os.path.abspath(self.repo):
            return False
        label = subtask.get("id") if subtask else role
        if self._checkpoint(cwd, "adg %s: salvaged before quota failover" % label):
            self.log("  salvaged %s's uncommitted work as a checkpoint" % label)
            return True
        return False

    @staticmethod
    def _with_failure(extra, failure):
        """What a *fresh* agent is told about the attempt it is replacing.

        A retry keeps its session, so `prompts.retry` carries the failure into
        the follow-up turn. An escalation does not, and cannot: `_climb` returns
        `session = None` by construction, because the whole point is a different
        model. So the one path that most needs the context was the one path that
        dropped it -- `_one_subtask` built `failure` (the signals, the evidence,
        and the `attempted` list from `_signal_context`, or the failing check
        output on a `test_stuck` climb), passed it to `_invoke`, and `_invoke`
        read it only when continuing a session or handing over on a quota hop.
        The stronger, dearer model opened on the plain role prompt and re-tried
        what its predecessor had already reported as failed.
        references/escalation.md: `attempted` prevents repetition, and the next
        model is smarter, not clairvoyant.

        Appended to the caller's `extra` rather than replacing it: that is the
        findings brief, and a rework that loses its findings is the failure the
        hand-off exists to stop.
        """
        if not failure:
            return extra
        note = ("The previous attempt on this did not succeed, and you are the "
                "replacement. Read this before you touch anything, then "
                "continue from the checkpoints already committed in this "
                "worktree — do not start over, and do not repeat what is listed "
                "as already tried:\n\n%s" % failure)
        return "%s\n\n%s" % (extra, note) if extra else note

    def _handover(self, extra, failure):
        """What the replacement agent is told. It inherits a worktree holding the
        predecessor's checkpoints, including the one `_salvage` just made — but
        it is told to look rather than to assume, because a hop can land after a
        turn that produced nothing at all."""
        note = ("The agent that started this ran out of its provider's quota "
                "part-way through. Whatever it finished is committed in this "
                "worktree — check `git log` before you do anything, read what is "
                "there, and continue from it. Do not start over, and do not "
                "revert work you find.")
        if failure:
            note += "\n\nThe checks it left failing:\n\n%s" % failure
        return "%s\n\n%s" % (extra, note) if extra else note

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

    def _report_state(self):
        """Fingerprint every report on disk, so "did this turn write it?" is
        answered by comparison rather than by a clock.

        (mtime, size) rather than mtime alone: a filesystem with coarse
        timestamps can stamp two writes inside one tick, and their sizes almost
        always differ. Identical bytes rewritten within one tick still read as
        unchanged -- which fails toward "no report", the safe direction, since
        the report is evidence and absent beats wrongly attributed."""
        out = {}
        try:
            names = os.listdir(self.task.file("reports"))
        except OSError:
            return out
        for name in names:
            try:
                st = os.stat(self.task.file("reports", name))
            except OSError:
                continue
            out[name] = (st.st_mtime, st.st_size)
        return out

    def _collect_report(self, role, subtask, before=None):
        """Find and validate this role's report from *this* invocation.
        Liveness is not success -- an agent can settle idle having done
        nothing, so the report is the evidence, not the exit code.

        `before` is `_report_state()` from the start of the turn."""
        for name, data in sorted(self.task.reports().items()):
            # When a subtask is named, its id is the ONLY thing that identifies
            # its report. Accepting `role in name` as an alternative could not
            # tell two concurrent implementers apart: every sibling in a wave
            # runs as "implementer", PROTOCOL.md permits `<stage>-<role>.json`, and
            # sorted() then handed the same file to all of them. An escalating
            # subtask read a sibling's `complete` and the run delivered a patch
            # built on work that had asked to stop. Subtask ids are unique, so
            # matching on the id alone cannot cross threads.
            #
            # And the id must fill the name, not merely appear in it. The
            # contract is `<stage>-<id>.json` (PROTOCOL.md step 3); a substring
            # test hands st-1 the report of a sibling named st-11, and the
            # planner -- not this code -- chooses the ids. The stage token is
            # not pinned to a word list, only forbidden from containing the
            # separator, so `implement-st-alpha.json` can never be claimed by
            # a subtask named plain `alpha`.
            if subtask:
                stem, want = name[:-len(".json")], subtask["id"]
                if not stem.endswith("-" + want):
                    continue
                if "-" in stem[:-len(want) - 1]:
                    continue
            elif role not in name:
                continue
            if before is not None:
                try:
                    st = os.stat(self.task.file("reports", name))
                except OSError:
                    continue
                if before.get(name) == (st.st_mtime, st.st_size):
                    continue  # untouched this turn: an earlier attempt's file
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
        res = self._optional("reporter", self.REPORT_PROMPT % text)
        if res is None:
            return text
        out = (res.get("output") or "").strip()
        if len(out) < 80 or brief.lint(out):
            return text
        return out

    def _decision_already_made(self, kind):
        """Consume a decision written by `delegate approve` / `reject`.

        True, False, or None when nobody has answered yet. Consumed *before* any
        brief is built, which is what keeps re-entry cheap: a brief goes through
        `_polish`, an LLM call, and rebuilding one to ask a question that has
        already been answered would spend a model call on nothing.

        The decision is NOT written to the gate history here. `delegate
        approve` already wrote it, at the moment the human actually made it --
        and that has to be the single recording site, because the design and
        plan gates resume *past* the stage that asked, so this method is never
        reached for them and an approval recorded only here would vanish. Which
        it did, until a test asked where it went.
        """
        pend = self.task.state.get("pending_gate") or {}
        if pend.get("kind") != kind or not pend.get("decision"):
            return None
        decision, note = pend["decision"], pend.get("note", "")
        self.task.update(pending_gate=None)
        self.log("gate %s: %s (answered out of band)%s"
                 % (kind, decision, " — %s" % note if note else ""))
        return decision == "approved"

    def _gate(self, kind, question, resume_status):
        decided = self._decision_already_made(kind)
        if decided is not None:
            if decided:
                return
            raise Halt("needs_human", "%s declined by human" % kind)
        files = sorted({f for s in self.task.state["subtasks"] for f in s.get("actual_files", [])})
        extra = None
        if kind == "design":
            extra = self.task.read_text("spec.md", "")
        text, problems = brief.write(self.task, kind, question, files=files,
                                     extra=extra, polish=self._polish)
        for p in problems:
            self.log("  brief lint: %s" % p)
        try:
            approved = self.gate(kind, text)
        except AwaitingApproval as a:
            # The injected gate keeps its `(kind, text) -> bool` signature: it
            # knows how to reach a human and nothing about the pipeline, so it
            # cannot know where the run continues afterwards. The caller does.
            a.resume_status = resume_status
            raise
        if not approved:
            # Recorded before the raise. A decline is the most informative thing
            # a human does to a run -- it is the one place the machine was about
            # to be wrong -- and logging only approvals left every rejection
            # missing from the history that exists to count them.
            self.task.record_gate(kind, "declined")
            raise Halt("needs_human", "%s declined by human" % kind)
        self.task.record_gate(kind, "approved")

    def _quota_park(self, exc):
        """Park on quota rather than on a generic 'no model available'. The
        difference matters to the reader: nothing is wrong with the task, and
        it will run again by itself once the window reopens."""
        self.task.update(park={"reason": "quota_all_exhausted",
                               "reopen_at": exc.reopen_at,
                               "channels": exc.channels,
                               "role": exc.role})
        when = _stamp(exc.reopen_at)
        tid = self.task.state["id"]
        files = sorted({f for s in self.task.state.get("subtasks") or []
                        for f in s.get("actual_files", [])})
        # Deliberately unpolished: the reporter runs on the same seats that just
        # went dark, and a second failure to render a brief helps nobody.
        text, problems = brief.write(
            self.task, "paused",
            "Every provider seat enrolled for the %s step has hit its usage "
            "limit, so this task is paused rather than failed. Nothing is wrong "
            "with the work: %s comes back at %s. Resume then with `delegate "
            "resume --id %s`, or leave `delegate resume --when-open --id %s` "
            "running and it will pick itself up."
            % (exc.role, " and ".join(exc.channels), when, tid, tid),
            files=files,
            extra="## Paused seats\n\n"
                  + "\n".join("- `%s`" % c for c in exc.channels)
                  + "\n\nOverride with `delegate channels --clear <name>` if you "
                    "believe a seat is actually available.")
        for p in problems:
            self.log("  brief lint: %s" % p)
        self.task.update(status="needs_human")
        self.log("\n== PAUSED (quota): %s" % exc)
