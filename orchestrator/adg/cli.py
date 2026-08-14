"""`delegate` — the MVP orchestrator CLI."""

import argparse
import calendar
import os
import signal
import sys
import time

import json

from . import (cooldown, corpus, limits as lim, quota, router as routing,
               runtime, store, verify, workflow as wf, yamlite)


def _repo(path):
    p = os.path.abspath(path or ".")
    try:
        top = store.git(["rev-parse", "--show-toplevel"], p)
    except store.StoreError:
        sys.exit("not a git repository: %s" % p)
    return top


def _auto_approve(kind, text):
    print("\n" + "=" * 68)
    print(text)
    print("=" * 68)
    print("[--yes] auto-approving %s" % kind)
    return True


def _confirm(kind, text):
    print("\n" + "=" * 68)
    print(text)
    print("=" * 68)
    try:
        return input("Approve %s? [y/N] " % kind).strip().lower() in ("y", "yes")
    except EOFError:
        # Park, never decline. There is a real difference between "the human
        # said no" and "there was no human to ask", and recording the first when
        # the second happened puts a rejection nobody made into the permanent
        # gate history -- the one record that exists to count what humans
        # actually decided.
        #
        # It is also what makes the CLI usable as a bridge. The caller is now
        # expected to be the user's own agent shelling out, and an agent has no
        # tty either, so declining here would auto-reject every gate of every
        # run. Parking hands the question back instead: `delegate show` prints
        # it, the agent puts it to the user in prose, and `delegate approve` /
        # `reject` answers it -- with a note, which y/N never carried.
        from .machine import AwaitingApproval
        raise AwaitingApproval(kind, text, resume_status=None)


def _new_id(repo):
    n = len(store.Task.list(repo)) + 1
    return "T-%03d" % n


# The one place this program sleeps, named so a test can replace it.
_SLEEP = time.sleep


def _stamp(epoch):
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(float(epoch)))


def _wait_until(epoch, clock=time.time, log=print):
    """Block until a quota window reopens. Deliberately not a daemon: the user
    started this and can stop it with ctrl-c, and nothing survives the shell."""
    while True:
        left = float(epoch) - clock()
        if left <= 0:
            return
        log("waiting %d min for the quota window to reopen (%s) — ctrl-c to stop"
            % (max(1, int(left // 60)), _stamp(epoch)))
        _SLEEP(min(left, 300))


def _quota_guard(task, when_open, clock=time.time, log=print):
    """A task parked on quota is not broken, it is early. Refuse quietly and
    say when, rather than starting a run every seat will reject.

    The **breaker file is the truth**; `park` only records why we stopped. Ask
    it which of the parked seats are still cooling, because the record in
    task.json is a snapshot: `channels --clear` and a window that simply reopened
    both leave it stale. Deciding from the snapshot alone made this guard's own
    advice ("clear the seat and re-run") a closed loop with no way out but
    hand-editing task.json."""
    park = task.state.get("park") or {}
    if park.get("reason") != "quota_all_exhausted":
        return
    now = clock()
    cools, _, warning = cooldown.read(now)
    if warning:
        log("warning: %s" % warning)
    still = {c: e for c, e in cools.items() if c in (park.get("channels") or ())}
    reopen = cooldown.earliest_reopen(still)
    if reopen and now < reopen:
        if not when_open:
            sys.exit(
                "%s is waiting on a provider quota window: %s reopens at %s.\n"
                "Re-run then, or `delegate resume --when-open --id %s` to wait "
                "here, or `delegate channels --clear <name>` if you believe a "
                "seat is already back."
                % (task.state["id"], ", ".join(sorted(still)), _stamp(reopen),
                   task.state["id"]))
        _wait_until(reopen, clock=clock, log=log)
    task.update(park=None)


def cmd_channels(args):
    """Cooldowns and quota draw per channel. Cross-project on purpose: the
    breaker belongs to the seat, so it is the same list from every repo."""
    reg = routing.load_registry(args.registry)
    now = time.time()
    cools, usage, warning = cooldown.read(now)
    if warning:
        print("warning: %s" % warning)
    if args.clear:
        if cooldown.clear(args.clear):
            print("cleared the cooldown on %s" % args.clear)
        else:
            print("%s is not cooling — nothing to clear" % args.clear)
        return
    print("state file: %s" % cooldown.path())
    print()
    for name, chan in sorted((reg.get("channels") or {}).items()):
        q = chan.get("quota") or {}
        util = cooldown.utilization(usage.get(name), q, now)
        entry = cools.get(name)
        state = ("cooling until %s (%s)" % (_stamp(entry["reopen_at"]), entry["reason"])
                 if entry else "ready")
        cap = quota.parse_capacity(q.get("est_capacity"))
        draw = ("%d%% of ~%g per %s" % (round(util * 100), cap, q.get("window"))
                if cap else "no capacity estimate")
        print("  %-14s %-38s %s" % (name, state, draw))
        if entry and entry.get("detail"):
            first = entry["detail"].strip().splitlines()
            if first:
                print("  %-14s   said: %s" % ("", first[0][:80]))
    print()
    print("Utilization is an estimate — providers expose no meter, so this "
          "counts one invocation as one unit.\nClear a cooldown with "
          "`delegate channels --clear <name>` if a seat is actually available.")


def _conventions_audit(repo, reg):
    """Which seats will run without this repository's standing conventions.

    A dispatched agent inherits the repo's agent-config file and NOTHING from
    the session that dispatched it. Whatever discipline the caller is following
    right now -- a skill it invoked, a convention it holds in context -- does
    not cross the subprocess boundary, so a seat with no config file on its side
    produces work that is correct and foreign.

    Reported, never blocking. Which conventions a repository should carry is not
    this program's business; knowing that half your seats cannot see them is.
    """
    kinds = {}
    for name, chan in (reg.get("channels") or {}).items():
        if any(reg["models"].get(m, {}).get("enrolled") for m in (chan.get("exposes") or [])):
            kinds.setdefault(chan.get("agent_kind", "claude"), []).append(name)
    if not kinds:
        return []
    out = ["", "repo conventions each seat will see:"]
    gaps = False
    for kind in sorted(kinds):
        seats = ", ".join(sorted(kinds[kind]))
        rel, tracked = runtime.conventions(repo, kind)
        if rel and tracked:
            note = "%s" % rel
        elif rel:
            # The case an existence check calls a pass. `init` runs in the main
            # checkout, where the file is right there; every job runs in a
            # worktree, which has only tracked files.
            note = ("%s is NOT tracked by git — worktrees check out tracked "
                    "files only, so jobs here run without it" % rel)
            gaps = True
        else:
            note = ("no %s in this repo — jobs routed here run without repo "
                    "conventions" % " or ".join(runtime.AGENT_CONFIG.get(kind, ["config"])))
            gaps = True
        print_kind = "%s (%s)" % (seats, kind)
        out.append("  %-28s %s" % (print_kind, note))
    if gaps:
        out += ["", "Nothing here is blocked by that. A convention an agent cannot read "
                    "is one it will not follow — put it in the repo, name it in the "
                    "plan's `briefing:`, or check for it at the gate."]
    return out


def cmd_init(args):
    repo = _repo(args.repo)
    reg = routing.load_registry(args.registry)
    print("project key : %s" % store.project_key(repo))
    print("state dir   : %s" % store.project_dir(repo))
    cfg = verify.load_project_config(repo)
    print("verify config: %s" % (cfg["_source"] or "none found (.adg.yaml) — "
                                 "checks will be skipped and reported as not run"))
    print("herdr       : %s" % ("available" if runtime.HerdrAdapter.available()
                                else "not detected — using the local adapter"))
    # Resolved once and read twice -- the table below and the verdict under it
    # are two readings of one routing decision, and asking the router twice is
    # how they come to disagree.
    #
    # The adapter is built directly rather than through `_make_adapter`: that
    # one prints the pane/cost note and reads flags `init` does not define.
    choices = _tier_choices(reg)
    seats = _tier_table(choices, runtime.get(_default_adapter(reg)))
    print("\ntier assignments within the ceiling %s:" %
          reg["policy"].get("escalation_ceiling", {}).get("max_tier"))
    # The tier table, not a role table. A job names a tier and the registry
    # decides the seat; roles stopped naming models when the protocol came out,
    # so a per-role listing showed five rows of the same answer -- three of them
    # for roles (planner, test-author, reviewer) this program no longer
    # dispatches at all.
    for tier, line in seats:
        print("  %-4s %s" % (tier, line))
    unused = [m for m, s in reg["models"].items() if not s.get("enrolled")]
    if unused:
        print("\npresent but deliberately not enrolled: %s" % ", ".join(unused))

    for line in _conventions_audit(repo, reg):
        print(line)

    # The one question the caller is told to answer here, answered rather than
    # left to be read out of the table. Delegating onto a single seat buys
    # subprocess indirection and worktree isolation the caller's own subagents
    # already provide, more slowly; the skill says to stop, so this says which
    # case the deployment is in.
    providers = {c.channel for _, c in choices if c}
    if len(providers) > 1:
        print("\n%d seats serve these tiers (%s) — work can move between them."
              % (len(providers), ", ".join(sorted(providers))))
    else:
        print("\nEvery tier resolves to %s. There is nowhere to fail over to, so "
              "delegating buys isolation your own subagents already give you — "
              "enroll a second provider first."
              % (", ".join(providers) or "nothing"))

    # `init` writes nothing, and there is no state for it to write. It used to
    # save a `config.json` holding the detected companion skills and chaff
    # scanner, both of which are gone, beside a registry path -- and nothing
    # ever read the file back. Configuration lives in `registry.default.yaml`
    # and `.adg.yaml`, which are edited by hand on purpose.
    print("\nRead the table above before delegating. Nothing was written: "
          "`registry.default.yaml` and `.adg.yaml` are this program's whole "
          "configuration.")


def _tier_choices(reg):
    """(tier, Choice or None) for every band, in order."""
    r = routing.Router(reg)
    out = []
    for tier in routing.TIERS:
        try:
            out.append((tier, r.select(tier=tier)))
        except routing.NoModelAvailable:
            out.append((tier, None))
    return out


def _tier_table(choices, adapter):
    """One printable line per band: which model serves it, on which seat, and
    whether that seat's agent CLI is actually installed.

    The installed check is the router's own blind spot -- it scores what the
    registry declares, and `machine._pick` is what filters by what is on PATH.
    A table that omitted it would name a seat every run then skips, which is the
    same lie in a quieter register.
    """
    out = []
    for tier, c in choices:
        if c is None:
            out.append((tier, "nothing enrolled serves this band"))
            continue
        usable = "" if adapter.can_run(c.agent_kind) else \
            "  [%s is not installed — this band falls back]" % c.agent_kind
        # `adapter.name` is what will actually run this band; `c.adapter` is only
        # what its channel declares. ONE adapter serves the whole run
        # (`_make_adapter`), chosen by `_default_adapter` from whether any
        # channel asks for herdr -- so on a registry that declares different
        # adapters per seat, exactly one of them wins and the rest are ignored.
        # Printing the declared value per row showed a per-seat control that
        # nothing per-seat reads, and hid the herdr-unavailable fallback too:
        # `runtime.get("herdr")` returns a LocalAdapter when herdr is missing,
        # which the old column still called herdr.
        mixed = "" if c.adapter == adapter.name else \
            "  [declares %s; one adapter serves the whole run]" % c.adapter
        out.append((tier, "%-18s via %-12s (%s)%s%s"
                    % (c.model, c.channel, adapter.name, mixed, usable)))
    return out


def cmd_run(args):
    repo = _repo(args.repo)
    reg = routing.load_registry(args.registry)
    defaults = (reg["policy"].get("limits") or {})
    ceiling = reg["policy"].get("escalation_ceiling") or {}
    merged, notes = lim.merge(dict(defaults, escalation_ceiling=ceiling),
                              {"max_cost_usd": args.max_cost} if args.max_cost else {})
    for n in notes:
        print("limit note: %s" % n)
    try:
        lim.validate(merged)
    except lim.LimitsInvalid as e:
        sys.exit("refusing to run: %s" % e)

    request = args.request
    if os.path.exists(request):
        with open(request, encoding="utf-8") as fh:
            request = fh.read()
    task_id = args.id or _new_id(repo)
    body = request if request.lstrip().startswith("#") else \
        "# Task %s\n\n## Request (verbatim)\n\n%s\n" % (task_id, request.strip())
    task = store.Task.create(repo, task_id, body, merged, mode=args.mode)
    if getattr(args, "plan", None):
        # `plan.md` is where `_stage_implement` looks for jobs when the state
        # has none -- the path a resumed run already takes to recover its
        # decomposition from disk. A caller supplying one is that same
        # situation arriving earlier, so it needs no new code in the machine.
        #
        # Not validated here beyond existing: `_read_plan_subtasks` is the one
        # parser, it already refuses a block it cannot read rather than guessing,
        # and a second check in a different place is how the two drift.
        if not os.path.isfile(args.plan):
            sys.exit("refusing to run: no plan at %s" % args.plan)
        with open(args.plan, encoding="utf-8") as fh:
            task.write_text("plan.md", fh.read())
        print("plan supplied by the caller: %s" % args.plan)
    print("task %s -> %s" % (task_id, task.path))

    adapter = _make_adapter(args, reg)
    from .machine import Orchestrator
    gate = _auto_approve if args.yes else _confirm
    orch = Orchestrator(task, reg, adapter, gate, dry_run=args.dry_run)
    status = orch.run()
    print("\nfinal status: %s" % status)
    sys.exit(0 if status == "done" else 1)


def _make_adapter(args, reg):
    name = args.adapter or _default_adapter(reg)
    if name == "herdr" and getattr(args, "no_panes", False):
        name = "herdr-quiet"
    adapter = runtime.get(name)
    if getattr(adapter, "panes", False):
        print("note: agents run in herdr panes, which report no cost — "
              "max_cost_usd cannot bind. Use --no-panes if you need the cap.")
    return adapter


def _default_adapter(reg):
    for chan in (reg.get("channels") or {}).values():
        if chan.get("adapter") == "herdr" and runtime.HerdrAdapter.available():
            return "herdr"
    return "local"


def cmd_approve(args):
    """Answer a gate that parked, then carry on.

    This is the other half of turning a gate from a prompt into a return value.
    `run` exits holding the question; the user's agent shows it to the human in
    whatever form reads well; this writes the answer back and continues.

    The decision is written to `pending_gate` rather than straight to the gate
    history because the machine has to *consume* it -- that is what lets an
    approved run skip past a question already answered instead of asking it
    again, and it is why `--note` survives into the record either way.
    """
    repo = _repo(args.repo)
    task = store.Task.open(repo, args.id)
    pend = task.state.get("pending_gate") or {}
    if not pend.get("kind"):
        sys.exit("task %s is not waiting on a gate (status: %s)"
                 % (task.state["id"], task.state["status"]))
    if pend.get("decision"):
        sys.exit("the %s gate was already answered %r" % (pend["kind"], pend["decision"]))

    decision = "approved" if args.decision == "approve" else "declined"
    # Recorded HERE, and only here. This is the moment the human decided, and it
    # is the site every gate passes through whether or not the machine is ever
    # re-entered to consume the pending decision below.
    task.record_gate(pend["kind"], decision, args.note)
    if decision == "approved":
        # Carried into the prompts of the agents that run after it. A qualified
        # yes approves something other than what was proposed, and whoever
        # builds it has to be told -- otherwise the note is a record of an
        # instruction nobody ever followed.
        #
        # Written even when empty, which CLEARS a previous one. Guarding this on
        # a non-empty note meant an unqualified approval at a later gate left the
        # earlier qualification flowing into every downstream prompt for the rest
        # of the run -- while `_human_note` promised it was "superseded by the
        # next approval".
        task.update(gate_note={"kind": pend["kind"], "note": args.note.strip()})
    # The merge gate keeps its pending decision because `_stage_integrate` is
    # re-entered and consumes it. A gate whose stage does not re-enter would
    # never have its consumed, and a stale `pending_gate` makes the next
    # `approve` refuse as already-answered.
    #
    # Only an APPROVAL is kept. A declined merge gate left the decision behind
    # forever: the human's agent fixed what was wrong, called `approve`, and got
    # "the merge gate was already answered 'declined'" with no way back. Worse,
    # a later run with `--yes` still found the stale decline and threw away all
    # the rework at a gate nobody was asked about.
    if pend["kind"] == "merge" and decision == "approved":
        task.update(pending_gate=dict(pend, decision=decision, note=args.note))
    else:
        task.update(pending_gate=None)
    print("%s gate: %s%s" % (pend["kind"], decision,
                             " — %s" % args.note if args.note else ""))
    if decision == "declined":
        # A decline ends the run by definition, so there is nothing to continue
        # into. It is already in the gate history above; nothing downstream has
        # to run for the rejection to have been recorded.
        task.update(status="needs_human")
        print("task parked at needs_human; `delegate resume --stage <stage>` to redirect")
        return 0
    # Written whether or not we continue now. `--no-continue` used to return
    # with the status still `awaiting_approval` and `pending_gate` already
    # cleared, which stranded the task: nothing handles that status, so `resume`
    # halted with "no handler for stage 'awaiting_approval'", and `approve`
    # refused because the gate was no longer pending. `resume_status` was gone,
    # so the run could only be restarted at a guessed stage -- and for a design
    # gate the guess is wrong, because `needs_human` resumes at `implement` and
    # skips planning entirely.
    resume_at = pend.get("resume_status")
    if not resume_at:
        sys.exit("the parked gate recorded no resume point; "
                 "use `delegate resume --stage <stage>`")
    task.update(status=resume_at)
    if args.no_continue:
        print("recorded; task set to %s, not resuming (--no-continue)" % resume_at)
        return 0
    return cmd_resume(argparse.Namespace(
        repo=args.repo, registry=args.registry, id=args.id, stage=None,
        adapter=args.adapter, no_panes=args.no_panes, dry_run=args.dry_run,
        yes=False, when_open=False))


def cmd_resume(args):
    repo = _repo(args.repo)
    reg = routing.load_registry(args.registry)
    task = store.Task.open(repo, args.id)
    # getattr, like _make_adapter does for --no-panes: cmd_resume is called
    # directly with hand-built args, and a missing flag must mean "off", not
    # an AttributeError two lines into a resume.
    _quota_guard(task, when_open=getattr(args, "when_open", False))
    was = task.state["status"]
    # An explicit --stage is honoured whenever it is given. A task interrupted
    # mid-flight keeps the status of the stage that was running, so gating the
    # override on "parked" silently dropped the flag exactly when it was most
    # needed -- and re-ran a stage the user was trying to skip past.
    if args.stage:
        # Checked here rather than as argparse `choices`, because cmd_resume is
        # also called directly with hand-built args. Writing first and finding
        # out at the run loop cost the user a persisted bad status and a
        # "no handler for stage" park for what is only ever a typo.
        from .machine import STAGES
        if args.stage not in STAGES:
            sys.stderr.write("unknown stage %r — expected one of: %s\n"
                             % (args.stage, ", ".join(STAGES)))
            sys.exit(2)
        task.update(status=args.stage)
    elif was == "awaiting_approval":
        # Not a stage, and not something `resume` can guess its way past. Left
        # to fall through, the run loop found no `_stage_awaiting_approval` and
        # parked with "no handler for stage", which reads as a crash for what is
        # an ordinary state: a question is waiting on a human. The gate still
        # holds its own resume point, so answering it is the only correct move.
        pend = task.state.get("pending_gate") or {}
        sys.exit("task %s is waiting on the %s gate, not parked. Answer it with "
                 "`delegate approve --note \"...\"` or `delegate reject --note "
                 "\"...\"`; `delegate show --brief` prints the question. To "
                 "abandon the question and restart elsewhere, pass an explicit "
                 "--stage." % (task.state["id"], pend.get("kind", "?")))
    elif was in ("needs_human", "abandoned"):
        task.update(status="implement")
    print("resuming %s from %s%s" % (task.state["id"], task.state["status"],
                                     "" if task.state["status"] == was else " (was %s)" % was))
    adapter = _make_adapter(args, reg)
    from .machine import Orchestrator
    gate = _auto_approve if args.yes else _confirm
    status = Orchestrator(task, reg, adapter, gate, dry_run=args.dry_run).run()
    print("\nfinal status: %s" % status)
    sys.exit(0 if status == "done" else 1)


def _seat_quota_lines(registry, cools, usage, now):
    """One line per enrolled seat: how much of each metered window is left, and
    when it reopens if it is shut.

    Read-only, and nothing here meters anything. The router already counts
    invocations against a window and prices a seat off the result -- that
    number decided which provider ran your code, and the only place it surfaced
    was `delegate channels`, a command a caller reaches for after something has
    gone wrong rather than before.

    It shows the windows THE STORE ACTUALLY METERS and no others. One `quota:`
    block per channel means one window per seat today; a seat whose registry
    entry carries no `est_capacity` has no meter at all, and says so rather than
    rendering the 0.0 that `cooldown.utilization` returns for "unknown" as
    "100% left". A fabricated headroom figure is worse than none: it is the
    number a caller would decide to dispatch a wave on.
    """
    seats = []
    for name, chan in sorted((registry.get("channels") or {}).items()):
        if not isinstance(chan, dict) or chan.get("disabled"):
            continue
        # The same "enrolled" test `_conventions_audit` uses: a seat exposing
        # nothing the router may pick is not a seat this deployment has.
        if not any((registry.get("models") or {}).get(m, {}).get("enrolled")
                   for m in (chan.get("exposes") or [])):
            continue
        q = chan.get("quota") or {}
        cap = quota.parse_capacity(q.get("est_capacity"))
        if cap:
            util = cooldown.utilization(usage.get(name), q, now)
            # Clamped: `utilization` is 0.0-1.0+, and "-12% left" reads as a
            # bug rather than as a seat that is well past its estimate.
            left = "%s window: %d%% left" % (
                q.get("window") or "5h", round(max(0.0, 1.0 - util) * 100))
        else:
            left = "%s window: no capacity estimate" % (q.get("window") or "5h")
        entry = cools.get(name)
        shut = ("  [cooling until %s]" % _stamp(entry["reopen_at"])) if entry else ""
        seats.append("%-14s %s%s" % (name, left, shut))
    if not seats:
        return []
    return ["", "seats:"] + ["  " + s for s in seats] + [
        "  (draw is estimated — providers expose no meter, so one invocation "
        "counts as one unit)"]


def _print_seat_quota(args, cools, usage, now):
    """The seat block, on every `status` including one with no tasks.

    Never fatal. `status` is a command a user reaches for when something is
    already wrong, so a registry that will not load costs them the seat lines
    and not the task lines -- the same rule `main` applies to a workflow
    manifest that will not parse.
    """
    try:
        reg = routing.load_registry(args.registry)
    except (routing.RoutingError, yamlite.YamlError, OSError) as e:
        print("\nseats: the registry could not be read (%s)" % e)
        return
    for line in _seat_quota_lines(reg, cools, usage, now):
        print(line)


def cmd_status(args):
    repo = _repo(args.repo)
    tasks = store.Task.list(repo)
    now = time.time()
    if not tasks:
        print("no tasks for %s" % store.project_key(repo))
        cools, usage, _ = cooldown.read(now)
        _print_seat_quota(args, cools, usage, now)
        return
    # The breaker file is the truth and `park` only records why we stopped, so
    # this asks the same question `_quota_guard` does rather than reading the
    # snapshot in task.json. That snapshot goes stale two ordinary ways --
    # `channels --clear`, and a window that simply reopened -- and reporting
    # from it left `status` saying "waiting on quota" beside a reopen time in
    # the past, for a task that `resume` would have run immediately.
    cools, usage, warning = cooldown.read(now)
    if warning:
        print("warning: %s" % warning)
    for t in tasks:
        s = t.state
        done = sum(1 for x in s.get("subtasks", []) if x.get("status") == "complete")
        status = s["status"]
        park = s.get("park") or {}
        reopen, still = None, {}
        if park.get("reason") == "quota_all_exhausted":
            still = {c: e for c, e in cools.items()
                     if c in (park.get("channels") or ())}
            reopen = cooldown.earliest_reopen(still)
            if reopen is None and warning:
                # The breaker could not be READ, which is not the same answer as
                # "those windows have reopened" -- and only the second one means
                # the task is runnable. Falling through here reported a task
                # that is merely early as one that needs a human to think, which
                # is the distinction the park exists to draw, and took away the
                # `resume --when-open` the paused-seat brief points at. So while
                # the file that supersedes the snapshot is unreadable, the
                # snapshot stands in.
                try:
                    reopen = float(park.get("reopen_at"))
                except (TypeError, ValueError):
                    reopen = None
                still = dict.fromkeys(park.get("channels") or ())
        if reopen and reopen > now:
            # Otherwise a task that is merely early is indistinguishable from one
            # that needs a human to think, which is the whole point of parking
            # it differently.
            status = "waiting on quota"
        print("%-8s %-17s %2d/%-2d subtasks  $%.2f  %s" % (
            s["id"], status, done, len(s.get("subtasks", [])),
            float(s.get("spent", {}).get("usd", 0)), t.path))
        if reopen and reopen > now:
            print("%-8s   %s reopens at %s" % (
                "", ", ".join(sorted(still) or ["the seat"]), _stamp(reopen)))
    _print_seat_quota(args, cools, usage, now)


BUNDLE_SECTIONS = ("task.json", "run.log", "brief.md", "corpus-capture.jsonl")


def _bundle_text(task, capture_lines):
    """The four things a diagnosis needs, in one file, all of it scrubbed.

    Concatenated text rather than a tarball on purpose: this is written to be
    pasted into a conversation with somebody -- or something -- that is going
    to read it, and asking that reader to unpack an archive first is a step
    that buys nothing. Section headers are fixed strings so the reader can find
    its way without being told the format.
    """
    out = []
    for name in BUNDLE_SECTIONS:
        out.append("=" * 72)
        out.append("=== %s" % name)
        out.append("=" * 72)
        if name == "corpus-capture.jsonl":
            body = "\n".join(capture_lines) or "(no provider walls during this run)"
        else:
            body = task.read_text(name, "(not written)")
        # Every section, including the ones this program wrote itself. A run log
        # quotes provider messages and agent output, and task.json holds
        # absolute paths under the user's home -- the bundle is built to be
        # shared, so nothing gets a pass for being ours.
        out.append(corpus.redact(body).rstrip("\n"))
        out.append("")
    return "\n".join(out) + "\n"


def _capture_lines_for(task):
    """Capture records stamped inside this run's window.

    The capture file is machine-wide and long-lived -- a seat's quota belongs
    to the seat, not to a repository -- so shipping the whole thing would put
    other projects' walls into this task's bundle. Bounded by the run log's own
    span, which is the only record of when this task was actually running.
    """
    try:
        with open(corpus.path(), encoding="utf-8") as fh:
            raw = [x.strip() for x in fh if x.strip()]
    except OSError:
        return []
    stamps = []
    for line in (task.read_text("run.log", "") or "").splitlines():
        head = line[:19]
        try:
            stamps.append(time.mktime(time.strptime(head, "%Y-%m-%d %H:%M:%S")))
        except ValueError:
            continue
    if not stamps:
        return []
    # A minute of slack each side: the log stamps local wall clock at the
    # moment a line is written, the capture stamps UTC at the moment a wall is
    # classified, and the two are not written by the same call.
    lo, hi = min(stamps) - 60, max(stamps) + 60
    keep = []
    for line in raw:
        try:
            at = json.loads(line).get("at")
            when = calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, TypeError, AttributeError):
            continue
        if lo <= when <= hi:
            keep.append(line)
    return keep


def cmd_bundle(args):
    """One shareable file holding everything a diagnosis needs."""
    repo = _repo(args.repo)
    task = store.Task.open(repo, args.id)
    text = _bundle_text(task, _capture_lines_for(task))
    out = args.out or task.file("bundle.txt")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    # The path and nothing else: this is a command whose output is a filename,
    # and anything else printed beside it has to be stripped by whoever pipes it.
    print(out)


def cmd_show(args):
    repo = _repo(args.repo)
    task = store.Task.open(repo, args.id)
    if args.brief:
        print(task.read_text("brief.md", "(no brief yet)"))
        return
    print(json.dumps(task.state, indent=2))


def _die_like_ctrl_c(signum, frame):
    """Turn a SIGTERM into the interrupt the machine already handles.

    `run()` drains every live agent session in a `finally`, and a `finally` runs
    for KeyboardInterrupt but not for a signal Python installs no handler for --
    the default SIGTERM disposition kills the interpreter outright and leaves
    the agent subprocesses to be reparented. Raising here means `kill <pid>` and
    Ctrl-C take the same path: sessions torn down, task left resumable.
    """
    raise KeyboardInterrupt("terminated (signal %s)" % signum)


def main(argv=None):
    try:
        signal.signal(signal.SIGTERM, _die_like_ctrl_c)
    except (ValueError, OSError, AttributeError):
        # Not the main thread, or a platform without SIGTERM. The drain in
        # `run()` still covers every in-process path; only the signal shortcut
        # is unavailable, and refusing to start over it would be absurd.
        pass
    p = argparse.ArgumentParser("delegate", description="Multi-agent task delegation.")
    p.add_argument("--repo", default=".", help="repository (default: cwd)")
    p.add_argument("--registry", default=None, help="path to registry.default.yaml")
    p.add_argument("--workflow", default=None, metavar="DIR",
                   help="directory holding a workflow.yaml (default: the "
                        "bundled orchestrator/workflows/default; also "
                        "$AGENT_DELEGATION_WORKFLOW)")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="show the detected setup and which seat "
                                    "serves which tier")
    i.set_defaults(func=cmd_init)

    r = sub.add_parser("run", help="run a new task")
    r.add_argument("request", help="the request, or a path to a file containing it")
    r.add_argument("--id", help="task id (default: next T-nnn)")
    r.add_argument("--mode", choices=["attended", "autonomous"], default="attended")
    r.add_argument("--adapter", choices=["herdr", "local", "mock"])
    r.add_argument("--no-panes", action="store_true",
                   help="run agents as subprocesses even under herdr: you lose the "
                        "visible panes and get cost accounting back, so max_cost_usd "
                        "can actually bind")
    r.add_argument("--max-cost", type=float, help="lower the cost cap for this task")
    r.add_argument("--dry-run", action="store_true", help="drive the machine without agents")
    r.add_argument("--plan", metavar="FILE",
                   help="the jobs to run, in plan.md's format. Not optional in "
                        "practice: nothing here decomposes work, so a task with "
                        "no jobs does nothing")
    r.add_argument("--yes", action="store_true",
                   help="auto-approve gates (unattended runs; merge still never happens)")
    r.set_defaults(func=cmd_run)

    rs = sub.add_parser("resume", help="continue a parked task")
    rs.add_argument("--id")
    rs.add_argument("--stage", help="stage to resume at (see machine.STAGES)")
    rs.add_argument("--adapter", choices=["herdr", "local", "mock"])
    rs.add_argument("--no-panes", action="store_true")
    rs.add_argument("--dry-run", action="store_true")
    rs.add_argument("--yes", action="store_true")
    rs.add_argument("--when-open", action="store_true",
                    help="if the task is waiting on a quota window, sleep here "
                         "until it reopens and then resume")
    rs.set_defaults(func=cmd_resume)

    for name, helptext in (("approve", "answer a waiting gate with yes"),
                           ("reject", "answer a waiting gate with no")):
        g = sub.add_parser(name, help=helptext)
        g.add_argument("--id")
        g.add_argument("--note", default="",
                       help="what the human actually said; carried into the "
                            "gate record and, on approval, into the run")
        g.add_argument("--adapter", choices=["herdr", "local", "mock"])
        g.add_argument("--no-panes", action="store_true")
        g.add_argument("--dry-run", action="store_true")
        g.add_argument("--no-continue", action="store_true",
                       help="record the decision without resuming the run")
        g.set_defaults(func=cmd_approve, decision=name)

    st = sub.add_parser("status", help="list tasks for this project")
    st.set_defaults(func=cmd_status)

    sh = sub.add_parser("show", help="show task state or the latest brief")
    sh.add_argument("--id")
    sh.add_argument("--brief", action="store_true")
    sh.set_defaults(func=cmd_show)

    bd = sub.add_parser("bundle", help="one shareable file: state, run log, "
                                       "brief and any provider walls")
    bd.add_argument("id", nargs="?", help="task id (default: the only one)")
    bd.add_argument("--out", metavar="FILE",
                    help="where to write it (default: bundle.txt in the task dir)")
    bd.set_defaults(func=cmd_bundle)

    ch = sub.add_parser("channels", help="quota cooldowns and draw per channel")
    ch.add_argument("--clear", metavar="NAME",
                    help="drop the cooldown on one channel")
    ch.set_defaults(func=cmd_channels)

    args = p.parse_args(argv)
    # Before any subcommand runs. The workflow decides which protocol and role
    # cards every agent is pointed at, so resolving it late would mean the
    # first stage of a run could be composed against a different manifest from
    # the rest. Failing here, with the path in the message, beats failing three
    # stages in with a missing file.
    #
    # Unconditionally, not only when `--workflow` is passed. The bundled default
    # and $AGENT_DELEGATION_WORKFLOW were left to `wf.current()`, which
    # `Orchestrator.__init__` calls from outside any handler -- so a bad env var
    # let `delegate run` create the task directory and then die with a raw
    # traceback, which is exactly the crash-instead-of-a-typo outcome this
    # early load exists to prevent.
    #
    # `yamlite.YamlError` as well as `WorkflowError`: a manifest that exists and
    # does not parse is the commonest typo of the two, and it is a sibling
    # ValueError rather than a subclass, so catching only the latter left the
    # traceback this exists to prevent.
    #
    # And it is fatal only for the commands that dispatch agents. `status`,
    # `show` and `channels --clear` are how a user works out what went wrong and
    # unsticks a seat; refusing to run them because of an unrelated
    # $AGENT_DELEGATION_WORKFLOW would take away the tools for the recovery.
    needs_workflow = args.cmd in ("run", "resume", "approve", "reject")
    try:
        wf.use(args.workflow) if getattr(args, "workflow", None) else wf.current()
    except (wf.WorkflowError, yamlite.YamlError) as e:
        if needs_workflow:
            sys.exit("refusing to run: %s" % e)
        print("warning: the workflow could not be loaded (%s). Reporting "
              "commands still work; `run` and `resume` will not." % e)
    try:
        return args.func(args)
    except store.StoreError as e:
        # `Task.open` refuses to guess between two tasks, which is right -- but
        # every command that takes `--id` is documented WITHOUT it, because with
        # one task the short form is the whole point. So a project's second run
        # turned the documented workflow into a traceback. Say which ids exist,
        # in the same spirit as the `--stage` typo check above: this is only
        # ever a missing flag.
        tasks = []
        try:
            tasks = [t.state["id"] for t in store.Task.list(_repo(args.repo))]
        except Exception:                      # noqa: BLE001 -- best effort
            pass
        hint = ("  --id one of: %s" % ", ".join(tasks)) if tasks else ""
        sys.exit("%s\n%s" % (e, hint) if hint else str(e))
