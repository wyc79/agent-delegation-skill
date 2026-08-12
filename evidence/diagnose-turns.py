#!/usr/bin/env python3
"""Where do delegate's extra turns go?

Same job, same model, same repo state, two prompts:

  A  exactly what `prompts.compose` builds for a dispatched implementer
  B  arm D's prompt -- goal, scope, contracts, verify, commit

Both run with `--output-format stream-json`, which emits every message, so each
turn can be attributed to the tool it called. Inlining the protocol was the
guess that the gap was file reads; it moved the turn count by zero. This stops
guessing and counts them.

**Paths in this file are the ones it was run with.** Same as its neighbour:
a record, not a tool.
"""
import collections
import json
import os
import re
import subprocess
import sys
import tempfile
import time

SKILL = os.path.expanduser("~/Documents/GitHub/agent-delegation-skill")
sys.path.insert(0, os.path.join(SKILL, "orchestrator"))
BASE = os.path.expanduser("~/.claude/jobs/b4f27121/tmp/sp-arm")
JOBS = os.path.expanduser("~/Downloads/gpudriver-shakedown/jobs.md")
OUT = os.path.expanduser("~/.claude/jobs/b4f27121/tmp/turn-diagnosis-%s.json" % os.environ.get("DIAG_JOB","st-2-render"))
JOB_ID = os.environ.get("DIAG_JOB", "st-2-render")


def sh(args, cwd):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if p.returncode:
        raise SystemExit("%s: %s" % (args, p.stderr[:300]))
    return p.stdout.strip()


def the_job():
    from adg import yamlite
    block = re.findall(r"```ya?ml\s*\n(.*?)```", open(JOBS).read(), re.S)[0]
    return [j for j in yamlite.load(block) if j["id"] == JOB_ID][0]


def delegate_prompt(job, tree):
    """What compose() actually builds, against a throwaway task."""
    from adg import prompts, store, verify
    job_state = dict(job, planned_scope=job["file_scope"])
    task = store.Task.create(tree, "T-DIAG", "# t\n\nrasterizer\n",
                             {"max_cost_usd": 15, "max_attempts_per_subtask": 8,
                              "max_parallel_agents": 3})
    return prompts.compose("implementer", task, subtask=job_state,
                           verify_cfg=verify.load_project_config(tree))


def handrolled_prompt(job):
    lines = [
        "You are implementing one file of a four-stage software rasterizer.",
        "Three other agents are implementing the other three files right now, in",
        "worktrees you cannot see. You cannot talk to them.",
        "", "Your job: %s" % job["id"], "", job["goal"].strip(), "",
        "Write ONLY these files: %s" % ", ".join(job["file_scope"]),
        "You may read anything else in the repo, but do not modify it.",
        "Worth reading: %s" % ", ".join(job.get("reads") or []),
        "", "Frozen interfaces — the other agents are coding against these",
        "right now. Match them exactly:",
    ]
    lines += ["  %s" % f for f in job.get("frozen_interfaces") or []]
    lines += ["", "Verify with `scons -Q` before you finish.",
              "Commit your work when the build is green."]
    return "\n".join(lines)


def run(label, prompt, tree):
    started = time.time()
    p = subprocess.run(
        ["claude", "-p", "--output-format", "stream-json", "--verbose",
         "--permission-mode", "bypassPermissions", prompt],
        cwd=tree, capture_output=True, text=True, timeout=3600)
    tools = collections.Counter()
    turns = cost = None
    assistant_msgs = 0
    for line in (p.stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            assistant_msgs += 1
            for blk in ((ev.get("message") or {}).get("content") or []):
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    name = blk.get("name", "?")
                    # Which file a read/write touched is the whole question.
                    inp = blk.get("input") or {}
                    path = inp.get("file_path") or inp.get("path") or ""
                    tag = os.path.basename(path) if path else ""
                    tools[("%s:%s" % (name, tag)) if tag else name] += 1
        elif ev.get("type") == "result":
            turns = ev.get("num_turns")
            cost = ev.get("total_cost_usd")
    rec = {"label": label, "turns": turns, "cost_usd": cost,
           "assistant_messages": assistant_msgs, "secs": round(time.time() - started, 1),
           "tools": dict(tools.most_common())}
    print("  %-12s turns=%-4s $%-8s tool calls=%d" % (
        label, turns, round(cost or 0, 3), sum(tools.values())), flush=True)
    return rec


def main():
    job = the_job()
    root = tempfile.mkdtemp(prefix="turns-")
    os.environ["XDG_STATE_HOME"] = os.path.join(root, "state")
    out = []
    for label, build in (("delegate", delegate_prompt), ("handrolled", None)):
        tree = os.path.join(root, label)
        sh(["git", "clone", "-q", BASE, tree], root)
        subprocess.run(["rm", "-rf", os.path.join(tree, "grading")], check=False)
        prompt = build(job, tree) if build else handrolled_prompt(job)
        open(os.path.join(root, "%s-prompt.txt" % label), "w").write(prompt)
        out.append(run(label, prompt, tree))
        out[-1]["prompt_chars"] = len(prompt)
    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=2)
    print("wrote %s" % OUT)


if __name__ == "__main__":
    main()
