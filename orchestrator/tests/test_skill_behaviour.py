#!/usr/bin/env python3
"""Does an agent handed this skill actually follow it?

**This one spends money and is not part of the free suite.** It launches a real
agent CLI. Run it when SKILL.md or its references change:

    python3 orchestrator/tests/test_skill_behaviour.py

Everything else in `tests/` proves the program does what it says. Nothing proved
the *skill* does -- and the skill is the half a caller actually reads. A skill
can be accurate, well organised, and still fail to be followed: the trigger sits
in a paragraph nobody reaches, the reference is named where the decision has
already been made, the condition it keys on is worded differently from what the
tool prints. None of that shows up in a unit test, and all of it shows up here.

The scenario is the one the skill's own first instruction is about: a single
enrolled provider and several independent jobs. What is asserted is behaviour
that can be read off the transcript, not the prose of the answer --

  * it read SKILL.md
  * it followed the pointer into references/one-seat.md
  * it ran `delegate init` before deciding
  * it did NOT run `delegate run`

The last is the point. On one seat the skill tells a caller not to delegate, and
a skill whose advice is ignored is a skill that does not exist.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SKILL_SRC = os.path.join(REPO_ROOT, "agent-delegation")
DELEGATE = os.path.join(REPO_ROOT, "orchestrator", "delegate")

ONE_SEAT_REGISTRY = """version: 1
models:
  fast-cheap: {tier: t1, reasoning: 2, coding: 3, adherence: 3, tool: 3, speed: 5, ctx: 1000000, cost_out: 1.5, enrolled: true}
  balanced-coder: {tier: t2, reasoning: 4, coding: 5, adherence: 4, tool: 5, speed: 4, ctx: 200000, cost_out: 15, enrolled: true}
  opus-class-strong: {tier: t3, reasoning: 5, coding: 5, adherence: 4, tool: 5, speed: 2, ctx: 200000, cost_out: 75, enrolled: true}
profiles:
  worker:
    require: {coding: 3, tool: 3}
    weights: {coding: 3, adherence: 2, speed: 1}
    cost_sensitivity: high
channels:
  only-seat:
    type: subscription
    adapter: local
    agent_kind: claude
    exposes: [opus-class-strong, balanced-coder, fast-cheap]
    quota: {window: 5h, est_capacity: 40}
policy:
  escalation_ceiling: {max_tier: t3}
  limits:
    max_cost_usd: 15
    max_attempts_per_subtask: 8
    max_parallel_agents: 3
    human_approval_required: [merge]
"""

# Deliberately four substantial jobs, not three one-liners. An earlier version
# of this test used trivial ones and the agent correctly refused BOTH delegate
# and the worktree pattern -- right answer, wrong question, and it proved
# nothing about whether the single-seat guidance is reachable.
TASK = """You have a skill at {skills}/agent-delegation/SKILL.md — read it first.

This repository has four independent modules to implement, one file each:
parser.py, evaluator.py, formatter.py and cache.py. Each is a few hundred lines
of real work with its own tests, and they share only the interfaces already
declared in interfaces.py. No two of them write the same file.

The `delegate` CLI is at {delegate}. This repo has ./registry.one-seat.yaml,
which you should pass with --registry.

Decide how to run this work. Do NOT implement anything and do NOT dispatch any
agents — I want the decision, the reasoning, and the exact commands. Under 250
words.
"""


def build_repo(root):
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)
    run("git", "init", "-q", "-b", "main", ".")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "Test")
    with open(os.path.join(repo, "interfaces.py"), "w") as fh:
        fh.write("class Node: ...\nclass Value: ...\n")
    for name in ("parser", "evaluator", "formatter", "cache"):
        with open(os.path.join(repo, "%s.py" % name), "w") as fh:
            fh.write("# TODO: implement %s\n" % name)
    with open(os.path.join(repo, "registry.one-seat.yaml"), "w") as fh:
        fh.write(ONE_SEAT_REGISTRY)
    # A check that nearly cannot fail, on purpose. SKILL.md says "a check that
    # always exits 0 is a check that always passes -- make sure yours can fail",
    # and this is a free second probe: an agent that has really absorbed the
    # skill notices the fixture's check is toothless. Both runs so far did,
    # unprompted. It is not asserted -- the four checks below are the contract
    # -- but it is worth reading the answer for.
    with open(os.path.join(repo, ".adg.yaml"), "w") as fh:
        fh.write('fast: ["python3 -m compileall -q ."]\n')
    run("git", "add", "-A")
    run("git", "commit", "-qm", "init")
    return repo


def ask(repo, prompt):
    """Run the agent, returning (answer, tool calls, cost)."""
    p = subprocess.run(
        ["claude", "-p", "--output-format", "stream-json", "--verbose",
         "--permission-mode", "bypassPermissions", prompt],
        cwd=repo, capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL)
    calls, answer, cost = [], "", None
    for line in (p.stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            for blk in ((ev.get("message") or {}).get("content") or []):
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    inp = blk.get("input") or {}
                    calls.append("%s %s" % (blk.get("name", "?"),
                                            inp.get("file_path") or inp.get("command") or ""))
        elif ev.get("type") == "result":
            answer, cost = ev.get("result") or "", ev.get("total_cost_usd")
    return answer, calls, cost


def main():
    root = tempfile.mkdtemp(prefix="skill-behaviour-")
    skills = os.path.join(root, "skills")
    os.makedirs(skills)
    shutil.copytree(SKILL_SRC, os.path.join(skills, "agent-delegation"))
    repo = build_repo(root)

    answer, calls, cost = ask(repo, TASK.format(skills=skills, delegate=DELEGATE))
    joined = "\n".join(calls)
    print("cost $%.3f, %d tool calls\n%s\n%s\n%s\n"
          % (cost or 0, len(calls), "=" * 66, answer, "=" * 66))

    checks = [
        ("read SKILL.md", "SKILL.md" in joined),
        ("followed the pointer to references/one-seat.md", "one-seat.md" in joined),
        ("ran `delegate init` before deciding", "init" in joined and "delegate" in joined),
        ("did NOT run `delegate run`", "delegate run" not in joined
                                       and " run " not in joined.replace("delegate init", "")),
    ]
    width = max(len(name) for name, _ in checks)
    for name, ok in checks:
        print("  %-*s %s" % (width, name, "ok" if ok else "FAILED"))
    failed = [n for n, ok in checks if not ok]
    if failed:
        print("\ntool calls the agent made:\n  %s" % "\n  ".join(calls))
        print("\nFAILED: %s" % "; ".join(failed))
    shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
