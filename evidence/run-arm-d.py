#!/usr/bin/env python3
"""Arm D — Claude + superpowers, four agents, each with fresh isolated context.

delegate's structure without delegate: one git worktree per job, one headless
`claude -p` per job, all four concurrent, all on Claude. Each agent gets only
its own goal and the frozen contracts -- no shared session, no shared cache.

This is the arm that separates two things arm C could not: how much of
delegate's cost is ISOLATION (four cold contexts instead of one warm one) and
how much is delegate's own overhead (protocol, role card, report schema).

**Paths in this file are the ones it was run with** (a specific clone, a
specific results directory). It is a record of what produced the numbers in
`RESULTS.md`, not a tool: read it for the shape and point it at your own tree.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

REPO = os.path.expanduser("~/.claude/jobs/b4f27121/tmp/sp-arm")
WT = os.path.expanduser("~/.claude/jobs/b4f27121/tmp/sp-arm-wt")
JOBS = os.path.expanduser("~/Downloads/gpudriver-shakedown/jobs.md")
OUT = os.path.expanduser("~/.claude/jobs/b4f27121/tmp/arm-d-results.json")


def sh(args, cwd, check=True):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit("%s failed in %s: %s" % (args, cwd, p.stderr[:400]))
    return p.stdout.strip()


def load_jobs():
    sys.path.insert(0, os.path.expanduser(
        "~/Documents/GitHub/agent-delegation-skill/orchestrator"))
    from adg import yamlite
    block = re.findall(r"```ya?ml\s*\n(.*?)```", open(JOBS).read(), re.S)[0]
    return yamlite.load(block)


def prompt_for(job):
    """The same content delegate composes, minus anything delegate-specific.

    No protocol file, no role card, no report schema -- an agent here reports by
    finishing, not by writing JSON. That difference is the point of the arm.
    """
    lines = [
        "You are implementing one file of a four-stage software rasterizer.",
        "Three other agents are implementing the other three files right now, in",
        "worktrees you cannot see. You cannot talk to them.",
        "",
        "Your job: %s" % job["id"],
        "",
        job["goal"].strip(),
        "",
        "Write ONLY these files: %s" % ", ".join(job["file_scope"]),
        "You may read anything else in the repo, but do not modify it.",
    ]
    if job.get("reads"):
        lines.append("Worth reading: %s" % ", ".join(job["reads"]))
    if job.get("frozen_interfaces"):
        lines += ["",
                  "Frozen interfaces — the other agents are coding against these",
                  "right now. Match them exactly; changing one breaks work you",
                  "cannot inspect:"]
        lines += ["  %s" % f for f in job["frozen_interfaces"]]
    lines += ["",
              "Verify with `scons -Q` before you finish. Expect",
              "`sh check-grade.sh` to still score 0/31 with only your stage done —",
              "that is correct and not a failure of your job.",
              "Commit your work when the build is green."]
    return "\n".join(lines)


def run_one(job, results, lock):
    branch = "armd/%s" % job["id"]
    tree = os.path.join(WT, job["id"])
    started = time.time()
    p = subprocess.run(
        ["claude", "-p", "--output-format", "json",
         "--permission-mode", "bypassPermissions", prompt_for(job)],
        cwd=tree, capture_output=True, text=True, timeout=3600)
    elapsed = time.time() - started
    rec = {"id": job["id"], "branch": branch, "secs": round(elapsed, 1),
           "returncode": p.returncode, "stderr": p.stderr[-600:]}
    try:
        data = json.loads(p.stdout)
        rec["cost_usd"] = data.get("total_cost_usd")
        rec["duration_ms"] = data.get("duration_ms")
        rec["num_turns"] = data.get("num_turns")
        rec["usage"] = data.get("usage")
        rec["model"] = data.get("modelUsage") or data.get("model")
        rec["result_tail"] = (data.get("result") or "")[-400:]
    except ValueError:
        rec["parse_error"] = p.stdout[-600:]
    with lock:
        results.append(rec)
    print("  done %-14s %5.1fs  $%s" % (job["id"], elapsed, rec.get("cost_usd")),
          flush=True)


def main():
    jobs = load_jobs()
    print("arm D: %d jobs, isolated worktrees, all on Claude" % len(jobs), flush=True)

    subprocess.run(["rm", "-rf", WT], check=False)
    os.makedirs(WT, exist_ok=True)
    base = sh(["git", "rev-parse", "HEAD"], REPO)
    for job in jobs:
        branch = "armd/%s" % job["id"]
        sh(["git", "branch", "-f", branch, base], REPO)
        sh(["git", "worktree", "add", "-f", os.path.join(WT, job["id"]), branch], REPO)

    results, lock = [], threading.Lock()
    t0 = time.time()
    threads = [threading.Thread(target=run_one, args=(j, results, lock)) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t0
    print("all agents done in %.1fs" % wall, flush=True)

    # Merge, exactly as delegate's integrate stage does: one at a time, into a
    # branch cut from the same base.
    sh(["git", "branch", "-f", "armd/work", base], REPO)
    sh(["git", "checkout", "-q", "armd/work"], REPO)
    merges = []
    for job in jobs:
        p = subprocess.run(["git", "merge", "--no-edit", "armd/%s" % job["id"]],
                           cwd=REPO, capture_output=True, text=True)
        merges.append({"id": job["id"], "ok": p.returncode == 0,
                       "out": (p.stdout + p.stderr)[-300:]})
        if p.returncode != 0:
            subprocess.run(["git", "merge", "--abort"], cwd=REPO, capture_output=True)

    build = subprocess.run(["scons", "-Q"], cwd=REPO, capture_output=True, text=True)
    grade_t0 = time.time()
    grade = subprocess.run(["sh", "check-grade.sh"], cwd=REPO,
                           capture_output=True, text=True, timeout=1800)
    grade_secs = time.time() - grade_t0
    score = re.search(r"FINAL SCORE: (\d+)", grade.stdout or "")

    out = {"arm": "D", "wall_secs": round(wall, 1), "agents": results,
           "merges": merges, "build_ok": build.returncode == 0,
           "score": int(score.group(1)) if score else None,
           "grade_secs": round(grade_secs, 1),
           "grade_tail": (grade.stdout or "")[-400:]}
    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=2)
    print("SCORE: %s   wall %.1fs   grade %.1fs" % (out["score"], wall, grade_secs))
    print("wrote %s" % OUT)


if __name__ == "__main__":
    main()
