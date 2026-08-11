"""`delegate` — the MVP orchestrator CLI (DESIGN.md §15)."""

import argparse
import os
import sys
import time

import json

from . import (companions, cooldown, limits as lim, quota, router as routing,
               runtime, store, verify, winnow, workflow as wf)


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
    r = routing.Router(reg)
    print("\nrole assignments within the ceiling %s:" %
          reg["policy"].get("escalation_ceiling", {}).get("max_tier"))
    for role in ("planner", "implementer", "test-author", "reviewer"):
        try:
            c = r.select(role)
            print("  %-12s %s via %s (%s)" % (role, c.model, c.channel, c.adapter))
        except routing.NoModelAvailable as e:
            print("  %-12s UNAVAILABLE — %s" % (role, e))
    unused = [m for m, s in reg["models"].items() if not s.get("enrolled_roles")]
    if unused:
        print("\npresent but deliberately not enrolled: %s" % ", ".join(unused))

    installed = companions.detect(repo)
    print("\ncompanion skills: %s" % (", ".join(k for k, v in installed.items() if v)
                                       or "none detected"))
    print("chaff scanner  : %s" % (winnow.find(repo) or "not installed"))

    dest = os.path.join(store.project_dir(repo), "config.json")
    if os.path.exists(dest) and not args.force:
        print("\nconfig already at %s (pass --force to rewrite)" % dest)
        return
    if not args.write:
        print("\nnothing written. Re-run with --write to save this to\n  %s" % dest)
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump({"registry": os.path.abspath(args.registry) if args.registry else None,
                   "companions": installed,
                   "winnow_scan": winnow.find(repo)}, fh, indent=2)
    print("\nwrote %s" % dest)


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
    if args.review != "auto":
        task.update(review=args.review)
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
    # Recorded HERE, and only here. This is the moment the human decided, and
    # it is the only site every gate passes through: the design and plan gates
    # resume past the stage that asked them, so a machine-side recording would
    # silently lose those approvals entirely.
    task.record_gate(pend["kind"], decision, args.note)
    if decision == "approved":
        # Carried into the prompts of the planner, implementers and reviewer.
        # A qualified yes approves something other than what was proposed, and
        # the agents that build and check it have to be told -- otherwise the
        # note is a record of an instruction nobody ever followed.
        #
        # Written even when empty, which CLEARS a previous one. Guarding this on
        # a non-empty note meant an unqualified approval at a later gate left the
        # earlier qualification flowing into every downstream prompt for the rest
        # of the run -- while `_human_note` promised it was "superseded by the
        # next approval".
        task.update(gate_note={"kind": pend["kind"], "note": args.note.strip()})
    # The merge gate keeps its pending decision because `_stage_integrate` is
    # re-entered and consumes it. The design and plan gates resume PAST the
    # stage that asked, so nothing would ever consume theirs -- and a stale
    # `pending_gate` makes the next `approve` refuse as already-answered.
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


def cmd_status(args):
    repo = _repo(args.repo)
    tasks = store.Task.list(repo)
    if not tasks:
        print("no tasks for %s" % store.project_key(repo))
        return
    for t in tasks:
        s = t.state
        done = sum(1 for x in s.get("subtasks", []) if x.get("status") == "complete")
        status = s["status"]
        park = s.get("park") or {}
        if park.get("reason") == "quota_all_exhausted":
            # Otherwise a task that is merely early is indistinguishable from one
            # that needs a human to think, which is the whole point of parking
            # it differently.
            status = "waiting on quota"
        print("%-8s %-17s %2d/%-2d subtasks  $%.2f  %s" % (
            s["id"], status, done, len(s.get("subtasks", [])),
            float(s.get("spent", {}).get("usd", 0)), t.path))
        if park.get("reason") == "quota_all_exhausted" and park.get("reopen_at"):
            print("%-8s   %s reopens at %s" % (
                "", ", ".join(park.get("channels") or ["the seat"]),
                _stamp(park["reopen_at"])))


def cmd_show(args):
    repo = _repo(args.repo)
    task = store.Task.open(repo, args.id)
    if args.brief:
        print(task.read_text("brief.md", "(no brief yet)"))
        return
    import json
    print(json.dumps(task.state, indent=2))


def main(argv=None):
    p = argparse.ArgumentParser("delegate", description="Multi-agent task delegation.")
    p.add_argument("--repo", default=".", help="repository (default: cwd)")
    p.add_argument("--registry", default=None, help="path to registry.default.yaml")
    p.add_argument("--workflow", default=None, metavar="DIR",
                   help="directory holding a workflow.yaml (default: the "
                        "bundled orchestrator/workflows/default; also "
                        "$AGENT_DELEGATION_WORKFLOW)")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="show detected setup and role assignments")
    i.add_argument("--write", action="store_true", help="save the detected setup")
    i.add_argument("--force", action="store_true", help="overwrite an existing config")
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
    r.add_argument("--review", choices=["auto", "always", "never"], default="auto",
                   help="auto (default): independent LLM review for complex work, "
                        "deterministic checks alone for simple work")
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
    if getattr(args, "workflow", None):
        try:
            wf.use(args.workflow)
        except wf.WorkflowError as e:
            sys.exit("refusing to run: %s" % e)
    return args.func(args)
