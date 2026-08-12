# Parallel jobs on a single seat, without `delegate`

Read this when `delegate init` said every tier resolves to one provider and you
still have several independent jobs to run.

**Independence is not the gate — size is.** Three one-line functions are three
`Write` calls in one message; a worktree apiece plus three cold agents is slower
than doing it yourself, however disjoint they are. This earns its setup when
each job is substantial enough that an agent would spend real turns on it. Below
that, write the code.

Measured on a four-job task, all arms scoring the same: on a single seat this
pattern beats dispatching the same jobs through `delegate`, on both money and
wall clock. What `delegate` adds there is a merge gate, a scope report and caps
— worth having if you will use them, and pure overhead if you will not.

## Why this is not the thing superpowers warns you off

`superpowers:subagent-driven-development` says, in its Red Flags:

> **Never:** Dispatch multiple implementation subagents in parallel (conflicts)

That is right, and it is right because SDD's subagents share **one working
tree** — two agents editing the same checkout collide, and the collision is
silent until something breaks. The warning is about the shared tree, not about
parallelism.

Give each agent its own git worktree and the premise is gone. There is no shared
checkout to conflict in; the only place two jobs can disagree is at merge, and
disjoint write scopes make that mechanical. So this composes two superpowers
skills that are documented separately and warned against jointly:
`using-git-worktrees` for the isolation, `dispatching-parallel-agents` for the
fan-out.

**The rule that replaces the warning:** two jobs may run at once only if their
write scopes do not overlap. Overlapping scopes go back to being sequential, and
SDD's advice applies again unchanged.

## The pattern

**1. A worktree per job, all cut from the same commit.**

```bash
base=$(git rev-parse HEAD)
for job in st-1-alpha st-2-beta st-3-gamma; do
  git worktree add "../wt/$job" -b "$job" "$base"
done
```

**2. Dispatch one agent per worktree, all in the same message** so they run
concurrently. Each gets its own goal, its write scope, and the frozen contracts
— nothing else. No plan file, no session history, no summary of what its
siblings are doing.

```text
You are implementing one file of <the thing>. Other agents are implementing
the others right now, in worktrees you cannot see. You cannot talk to them.

Your job: <id>
<goal — this is the whole brief>

Write ONLY these files: <scope>
You may read anything else, but do not modify it.

Frozen interfaces — the other agents are coding against these right now.
Match them exactly; changing one breaks work you cannot inspect:
  <exact signatures, invariants, coordinate conventions>

Verify with `<your check>` before you finish. Commit when it is green.
```

**3. Merge one at a time, checking after each**, so a break surfaces against the
smallest diff:

```bash
git checkout -b integration "$base"
for job in st-1-alpha st-2-beta st-3-gamma; do
  git merge --no-edit "$job" && <your check> || break
done
```

## The frozen contracts are the whole thing

Agents in separate worktrees cannot read each other's code. Every signature,
invariant or convention that two jobs share has to appear **verbatim in both
their prompts**. Get it wrong and the disagreement surfaces at merge — the one
place isolation cannot help you, because by then both sides are written.

On the measured task the contract that mattered was one sentence: *this vector
is in clip space, homogeneous, at every boundary; the perspective divide happens
exactly once, downstream.* Four agents, none of whom could see the others, all
built against it and the merge was mechanical. Leave it out and every one of
them makes a defensible local choice and they do not compose.

Write the contracts before you write the goals. If you cannot state them, the
jobs are not actually independent and you should not be running them in
parallel.

**Budget for writing them.** Measured on the same task twice: handed the
contracts, this pattern cost X; made to derive them, it cost roughly double, and
the extra was not setup — it was one agent merging work that looked right,
scoring below the bar on the two cases that turned on a single interpolation
rule, and having to bisect its way back to the cause. The contracts are not
paperwork you do before the real work. They *are* the part of the work that
cannot be parallelised, and skipping them moves the cost to the far end where it
is dearer.

## What you give up

- **No gate.** Nothing holds the work until you look at it.
- **No scope measurement.** An agent that edits a file outside its remit is
  invisible — nothing is watching, so nothing reports it.
- **No caps.** Nothing binds spend or attempts.
- **No checkpoint when a seat walls.** The CLI dies with your in-flight work
  uncommitted and that job restarts from nothing. `delegate` commits what it
  had, parks with the reopen time, and resumes from that commit.

That last one is the one people discover expensively. If you run your one seat
hard enough to hit walls, it is the reason to pay the overhead anyway.

## A worked implementation

`evidence/run-arm-d.py`, beside this skill's repository, is this pattern as ~150
lines of Python — worktree setup, concurrent dispatch, ordered merge, grade —
and it is what produced the measurements quoted above. It is a worked example,
not a supported tool: nothing in the test suite exercises it, its paths are the
ones it was run with, and it shells out to one specific agent CLI. Read it for
the shape; write your own for your setup.
