"""`delegate` — the MVP orchestrator CLI (DESIGN.md §15)."""

import argparse
import os
import sys
import time

import json

from . import companions, limits as lim, router as routing, runtime, store, verify, winnow


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
        print("(no tty — declining; re-run interactively to approve)")
        return False


def _new_id(repo):
    n = len(store.Task.list(repo)) + 1
    return "T-%03d" % n


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

    installed = companions.detect()
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

    adapter = runtime.get(args.adapter or _default_adapter(reg))
    from .machine import Orchestrator
    gate = _auto_approve if args.yes else _confirm
    orch = Orchestrator(task, reg, adapter, gate, dry_run=args.dry_run)
    status = orch.run()
    print("\nfinal status: %s" % status)
    sys.exit(0 if status == "done" else 1)


def _default_adapter(reg):
    for chan in (reg.get("channels") or {}).values():
        if chan.get("adapter") == "herdr" and runtime.HerdrAdapter.available():
            return "herdr"
    return "local"


def cmd_resume(args):
    repo = _repo(args.repo)
    reg = routing.load_registry(args.registry)
    task = store.Task.open(repo, args.id)
    was = task.state["status"]
    # An explicit --stage is honoured whenever it is given. A task interrupted
    # mid-flight keeps the status of the stage that was running, so gating the
    # override on "parked" silently dropped the flag exactly when it was most
    # needed -- and re-ran a stage the user was trying to skip past.
    if args.stage:
        task.update(status=args.stage)
    elif was in ("needs_human", "abandoned"):
        task.update(status="implement")
    print("resuming %s from %s%s" % (task.state["id"], task.state["status"],
                                     "" if task.state["status"] == was else " (was %s)" % was))
    adapter = runtime.get(args.adapter or _default_adapter(reg))
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
        print("%-8s %-12s %2d/%-2d subtasks  $%.2f  %s" % (
            s["id"], s["status"], done, len(s.get("subtasks", [])),
            float(s.get("spent", {}).get("usd", 0)), t.path))


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
    rs.add_argument("--stage", help="stage to resume at")
    rs.add_argument("--adapter", choices=["herdr", "local", "mock"])
    rs.add_argument("--dry-run", action="store_true")
    rs.add_argument("--yes", action="store_true")
    rs.set_defaults(func=cmd_resume)

    st = sub.add_parser("status", help="list tasks for this project")
    st.set_defaults(func=cmd_status)

    sh = sub.add_parser("show", help="show task state or the latest brief")
    sh.add_argument("--id")
    sh.add_argument("--brief", action="store_true")
    sh.set_defaults(func=cmd_show)

    args = p.parse_args(argv)
    return args.func(args)
