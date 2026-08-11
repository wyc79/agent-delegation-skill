# How work is dispatched here

You were started by `delegate`, which places jobs on whichever agent seat can
run them. It did not decide what the work is and it will not judge whether you
did it well — something else already made both of those calls and is waiting on
your result.

**Read this, then your card. Together they are about five minutes.**

## What you are inside

A **git worktree of your own**, on a branch of your own, cut from the task's
integration branch. Other agents may be working in their own worktrees on the
same repository at the same time. You cannot see their work and they cannot see
yours until it is merged.

Two consequences worth holding on to:

- The code you are reading may already be out of date, and the fix is not to go
  looking in other worktrees — you cannot — but to stay inside your scope so the
  merge stays mechanical.
- Nothing you do can break another agent mid-task, and nothing they do can break
  you. That is the whole reason for the isolation.

## The four rules

1. **Write only inside your file scope.** Your prompt names it. It is measured,
   not enforced: every file you touch outside it is recorded and reported back.
   Reading outside your scope is fine.
2. **Commit checkpoints often, inside your worktree.** They are free, and they
   are what lets an interrupted job resume instead of restarting. Never push,
   never switch branches, never check out another agent's branch.
3. **Keep the repository clean.** Anything that is not source, test or durable
   documentation goes in `$AGENT_DELEGATION_TASK_DIR`, not the working tree.
   `references/scratch-files.md` covers the narrow exception.
4. **Run the checks you were given.** They are in your prompt. Do not invent
   others and do not skip them because the change looks obviously right.

## Finishing

Write a report to `$AGENT_DELEGATION_TASK_DIR/reports/` at the path your prompt
names, matching the schema it names. The report is the only thing that leaves
this worktree besides your commits, so it has to carry:

- **status** — `complete` when you did the work and the checks pass, `blocked`
  when you cannot proceed, `escalate` when you are stuck and someone else should
  decide what to do.
- **summary** — what you actually changed, for a reader who has not seen this
  repository.
- **evidence** — the check output you are relying on. Not "tests pass": what you
  ran and what it said.

## When you are stuck

Say so and stop. `escalate` or `blocked` ends your job and hands everything you
know back to whoever dispatched you — including `signals`, where you put the
type of problem, the detail, the evidence, and what you already tried.

**Nothing here will retry you on a stronger model or rewrite the plan.** That
decision belongs to the caller, which wrote the decomposition and can see the
whole of it. Guessing on its behalf, or grinding on an approach you have already
shown does not work, spends real money to arrive somewhere worse. A clear
`escalate` with evidence is the most useful thing you can produce when the work
will not go.

Your worktree and its commits are left in place when that happens. They are what
the next attempt starts from.

## Your triggers

Read these only when the condition applies:

| Condition | Read |
|---|---|
| Other agents are working on this task at the same time, or you hit a merge conflict | `references/parallelism.md` |
| Something forces you to write a non-source file into the repository | `references/scratch-files.md` |
| Your prompt says a previous agent held this job and stopped part-way | `references/handover.md` |
| `$AGENT_DELEGATION_TASK_DIR` is not set and you must find the task directory | `references/task-dir.md` |
