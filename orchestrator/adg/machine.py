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

from . import (brief, cooldown, limits as lim, prompts, quota,
               router as routing, schema, verify, winnow, workflow as wf,
               yamlite)
from .store import git

# Statuses the machine can actually be resumed at -- `cli.cmd_resume` validates
# `--stage` against this list. "verify" is deliberately absent: checks run inside
# `implement` and `review` rather than as a stage of their own, so there is no
# `_stage_verify`. Listing it let a resume pass validation and then park with
# "no handler for stage 'verify'", which is the exact outcome that validation was
# added to prevent.
# The whole graph. `delegate` places work on seats and integrates what comes
# back. Deciding WHAT the work is, and whether it was done well, belongs to the
# caller -- which has already made both judgements before invoking this. The
# role protocol that used to live here (intake, classify, brainstorm, plan,
# review) was measured against a caller doing the same job in one warm context
# and lost on every axis but isolation: 4x the money, 4x the wall clock, 4x the
# tokens, for an identical 31/31. What no model release makes redundant is the
# seat that empties at 3pm, so that is what is left.
STAGES = ["implement", "integrate", "done"]
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
        # The agent session currently in flight, so a failure between starting
        # one and handing it back can still tear it down. See `_invoke`.
        self._session = None
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
                # each handler, because a handler names its own successor and
                # only this loop knows whether that destination is switched on.
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
                handler()
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
                        # `tier` is the caller's routing decision and the only
                        # one it gets to make; `estimated_loc` lands in the
                        # record so a run shows what was asked for.
                        "tier": s.get("tier"),
                        "estimated_loc": s.get("estimated_loc"),
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
        try:
            return self._one_subtask_inner(sub, base)
        finally:
            # Every exit, not just the two that used to remember. A `blocked`
            # report, the changed-no-files guard, a LimitBreach out of the
            # budget, a crash inside `verify` -- each of them raised straight
            # past the teardown and left an agent running with nothing holding
            # its handle. Under the herdr adapter that is a visible orphaned
            # pane; under any adapter it is a process outliving the run.
            # Idempotent: the paths that close early null the handle, so this
            # is a no-op for them rather than a second teardown.
            self._close(self._session)
            self._session = None

    def _one_subtask_inner(self, sub, base):
        role_choice = self._pick_worker(sub)
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
                extra=self._with_note(None))
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
                # Handed straight back, with the signals intact. This used to
                # enter a ladder -- a stronger model, then a re-plan -- which is
                # the caller's decision and one it already makes:
                # `superpowers:subagent-driven-development` assesses a BLOCKED
                # report and is explicit that you must "never ignore an
                # escalation or force the same model to retry without changes".
                # A wrapper that runs its own ladder is competing with the
                # skill that called it, and doing it with less context.
                self._close(session)
                self._session = session = None
                raise Halt("needs_human", self._escalated(sub, report))
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
                self._session = session = None
                self._checkpoint(base, ("adg %s: %s" % (sub["id"], sub.get("goal", "")))[:72])
                break
            # Retried on the same seat, with what broke. Bounded by
            # `max_attempts_per_subtask`, which the caller sets: the checks are
            # the caller's, so how many times to re-run them is the caller's
            # too. What does NOT happen is a climb to a dearer model -- that is
            # a judgement about the work, and the caller owns it.
            failure = "\n".join(
                "$ %s\n%s" % (f["cmd"], f["output"][-1500:]) for f in result.failures())
        self._finish_subtask(sub, base)

    @staticmethod
    def _escalated(sub, report):
        """The agent's own account of being stuck, passed through whole.

        Signals survive the cut that removed the ladder. They stopped being
        something this program routes on and went back to being what the schema
        always called them -- evidence -- because the caller is the one that can
        act: it wrote the decomposition, it knows what else is in flight, and
        `superpowers:subagent-driven-development` already has the procedure for
        assessing a BLOCKED report. Summarising them away here would leave that
        procedure with nothing to read.
        """
        signals = [x for x in ((report or {}).get("signals") or [])
                   if isinstance(x, dict)]
        named = ", ".join(x.get("type", "?") for x in signals)
        out = ["%s stopped and asked for help%s"
               % (sub["id"], " (%s)" % named if named else "")]
        summary = (report or {}).get("summary", "").strip()
        if summary:
            out.append(summary[:300])
        for x in signals:
            detail = (x.get("detail") or "").strip()
            if detail:
                out.append("%s: %s" % (x.get("type", "?"), detail[:200]))
            for a in (x.get("attempted") or [])[:6]:
                out.append("  already tried: %s" % str(a)[:120])
        out.append("Its worktree and checkpoints are left in place.")
        return " | ".join(out)

    def _pick_worker(self, sub):
        """The seat for one job, from the tier the caller asked for.

        `tier` is the whole of the routing decision a caller gets to make, and
        it is deliberately the only one: it names a band, the registry says
        which model serves that band and which seat prefers it, and everything
        else -- cooldowns, headroom, failover -- is this program's business.

        Advisory on the way down. A tier nothing enrolled can serve must not
        park the run: the caller is judging difficulty from outside, and a guess
        that stops the work outright is worse than a guess that gets the
        ordinary worker. The demotion is logged, because a job running below the
        band it asked for is exactly what you want to see in the log when it
        later comes back stuck.
        """
        tier = sub.get("tier")
        if not tier:
            return self._pick()
        try:
            return self._pick(tier=tier)
        except routing.AllChannelsCooled:
            raise
        except routing.NoModelAvailable as e:
            choice = self._pick()
            self.log("  %s: asked for tier %s and nothing enrolled serves it — "
                     "running on %s (%s)" % (sub["id"], tier, choice.model, e))
            return choice

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
        # it must not outrank a sibling's routing outcome: raising it here would
        # swallow a quota park's reopen time, which is the exact "the sequential
        # and parallel paths disagreed about the same event" failure the
        # re-raise below exists to prevent.
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
            if isinstance(outcome, routing.AllChannelsCooled):
                # Re-raised, not flattened into a Halt: a quota park carries a
                # reopen time and drives `resume --when-open`, and downgrading it
                # gave the wave path a different answer from the sequential one
                # for an identical event.
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
            # The strongest enrolled band: reconciling two agents' work is the
            # hardest judgement this program asks for, and it is rare enough
            # that paying for it is right.
            choice = self._pick(tier=self._top_tier())
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
        # shown to the human at the merge
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
        self.task.mutate(mark)
        violations = [s.get("scope_violations") or [] for s in self.task.state["subtasks"]
                      if s["id"] == sub["id"]]
        if violations and violations[0]:
            self.log("  scope: %d file(s) outside declared scope" % len(violations[0]))

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

    def _stage_integrate(self):
        # Before anything expensive. Unlike the other two gates this one sits
        # MID-stage -- `_land()` follows it -- so an approved run re-enters here,
        # and re-entry must not re-run the verification or rebuild a brief
        # to ask a question that is already answered.
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
        base = self._ensure_worktree()
        result = verify.run(self.task, self.repo, base, "fast")
        # The chaff scan used to run inside `review`, but it is not a review: no
        # model reads it and nothing routes on it. It is a deterministic pass
        # over the diff whose output the merge brief prints, so it belongs on
        # the path that still exists. Left in `review` it would have gone with
        # the reviewer, and the brief would have quietly lost a section that
        # still had everything it needed to produce.
        self.task.update(winnow=self._winnow(base, result))
        # Said plainly, every time. `delegate` runs no reviewer: it places the
        # work the caller decomposed and integrates what comes back, and the
        # only judgement in the record is the caller's own plus the checks
        # below. A brief that did not say so would let a human read "complete,
        # checks pass" as "something looked at this".
        note = ("**Nothing reviewed this.** `delegate` dispatched the subtasks "
                "you supplied and merged what came back; the automated checks "
                "below, and the scope column above, are the whole of the "
                "evidence. Judging the work against what you asked for is "
                "yours, and this is the point to do it.")
        chaff = winnow.as_text(self.task.state.get("winnow"))
        if chaff:
            note += "\n\n" + chaff
        text, problems = brief.write(
            self.task, "merge",
            "Land this change? It is complete and its checks pass. Nothing has been "
            "committed: attended mode leaves a patch file for you to apply and commit "
            "yourself.",
            files=files, verify=result, extra="## Review\n\n" + note)
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
    def _top_tier(self):
        """The strongest band this deployment will actually use: the highest
        tier with something enrolled, clamped by `escalation_ceiling`. Asked
        rather than written down, so enrolling or retiring a model moves it."""
        cap = (self.budget.ceiling() or {}).get("max_tier") or routing.TIERS[-1]
        allowed = routing.TIERS[:routing.TIERS.index(cap) + 1]
        live = {m.get("tier") for m in (self.reg.get("models") or {}).values()
                if m.get("enrolled")}
        for t in reversed(allowed):
            if t in live:
                return t
        return None

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

    def _pick(self, role=None, boost=None, exclude=(), min_reasoning=None, tier=None):
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
                                          ignore_reserve=ignore_reserve, tier=tier)

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

    def _run_once(self, role, choice, cwd, **kw):
        """Invoke, close, and return the report. For roles that get one turn."""
        try:
            session, report, _ = self._invoke(role, choice, cwd, **kw)
        finally:
            # The integrator reaches this. `_invoke` tears down what it started
            # when it raises, but a one-turn role has no retry loop to fall back
            # into, so anything raised between the turn returning and the close
            # below -- a report that will not validate, most likely -- would
            # otherwise strand the agent.
            self._close(self._session)
            self._session = None
        self._close(session)
        return report

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
        try:
            return self._invoke_inner(role, choice, cwd, subtask, extra,
                                      session, failure)
        except BaseException:
            # An agent process must not outlive the call that started it. This
            # method creates sessions locally and only hands one back on the
            # way out, so anything raised in between -- a report that will not
            # parse, a limit breach, a KeyboardInterrupt -- left a live process
            # nobody had a handle to. `self._session` is where the current one
            # is parked so this handler can find it.
            self._close(self._session)
            self._session = None
            raise

    def _invoke_inner(self, role, choice, cwd, subtask=None, extra=None,
                      session=None, failure=None):
        cooled, base_extra = [], extra
        while True:
            # Snapshot the reports directory: on a rework loop the previous
            # attempt's file is still on disk at the same path, and accepting it
            # would read a stale success as a fresh one. Comparing against what
            # was there before the turn needs no clock and no tolerance.
            before = self._report_state()
            if session is None:
                text = prompts.compose(role, self.task, subtask=subtask,
                                       extra=extra,
                                       verify_cfg=self.vcfg)
                session = self._session = self.adapter.start_agent(
                    role, choice.agent_kind, cwd, prompts.env_for(self.task, role))
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
        # `self._session` deliberately still points at it. The caller holds the
        # session too, but only this handle survives the caller raising before
        # it gets to close -- which is most of the ways `_one_subtask` can end.
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

    def _decision_already_made(self, kind):
        """Consume a decision written by `delegate approve` / `reject`.

        True, False, or None when nobody has answered yet. Consumed *before* any
        brief is built, which is what keeps re-entry cheap: a brief goes through
        an LLM call, and rebuilding one to ask a question that has
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
