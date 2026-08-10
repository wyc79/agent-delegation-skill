---
name: agent-delegation
description: Use when taking part in a delegated multi-agent development workflow — when a prompt assigns you a role (planner, implementer, test author, reviewer, integrator) for a task id, when `$AGENT_DELEGATION_TASK_DIR` is set in the environment, or when asked to plan work that other agents will implement, implement one subtask of an existing plan, write tests from requirements for someone else's implementation, review an implementation against its plan, or hand results to a downstream agent. Defines the shared protocol — which artifacts to read and write, what constraints to respect, and when to stop and escalate instead of pushing through.
---

# Agent Delegation

You are **one role in a pipeline**, not the whole system. Another agent planned
this work, or will review it. You reach those agents only through files in the
task directory — they never see your reasoning, only what you write down.

## Step 1 — Locate the task directory

Task state lives **outside the repository** — shared by every worktree, never in
git history. `$AGENT_DELEGATION_TASK_DIR` holds its absolute path; that value is
`$TASK_DIR` throughout this skill. If it is unset, derive it using
`references/task-dir.md` (it gives the exact recipe for Linux, macOS, and
Windows — do not improvise one, or you will read a different directory than the
rest of the pipeline writes).

**Never create the task directory yourself, and never write task artifacts into
the repository.** If you cannot find it, stop. (Tool-forced exception: rule 2.)

## Step 2 — Establish your assignment

Your prompt should give you a **role** and a **task id**. If either is missing,
do not guess and do not pick one: write a `blocked` report saying what was
missing. Nobody is listening for a question here — the report is how you ask.
Working the wrong role corrupts artifacts other agents depend on.

## Step 3 — Read exactly one role card

| Your role | Read | Your job, in one line |
|---|---|---|
| Planner / Architect | `roles/planner.md` | Produce a plan a weaker model can execute without you |
| Implementer | `roles/implementer.md` | Make the plan true for one subtask, inside its file scope |
| Test Author | `roles/test-author.md` | Encode the requirements as tests, blind to the implementation |
| Reviewer | `roles/reviewer.md` | Check requirements → plan → diff → evidence, and rule |
| Integrator | `roles/integrator.md` | Reconcile conflicting subtask results with minimal change |

Follow your card's numbered steps in order. Do not read the other cards.

## The task directory

Everything below is relative to `$TASK_DIR` from Step 1:

| File | Holds | Written by |
|---|---|---|
| `task.json` | Status, budgets, assignments, delegation history | Orchestrator only — **never edit** |
| `task.md` | The request and its numbered acceptance criteria (`AC-n`) | Intake or a human. The planner may add criteria if there are none; nobody else edits it |
| `plan.md` | Approach plus one YAML block per subtask | Planner |
| `deviations.md` | Append-only log of departures from the plan | Anyone who departs |
| `decisions.md` | Append-only design decisions and their reasons | Anyone deciding |
| `reports/` | One JSON handoff per agent, per stage (`verify/` holds check output) | Every agent, at exit |

**Authority when they disagree:** `task.md` (what was asked) outranks
`plan.md` + `deviations.md` (how it is being done) outranks the code. Never
"fix" a conflict silently in the direction of the code.

## Hard rules

1. **Stay inside your declared file scope.** Touching files outside it is a
   deviation at minimum and usually an escalation. Wanting to fix something
   nearby is not permission to fix it.
2. **Task artifacts never enter the repo** — they belong in `$TASK_DIR`. If a
   tool forces an in-repo path, it must be one `git check-ignore -q` already
   accepts, and `git status` must be clean of it — see `references/scratch-files.md`.
3. **Never `git push`, never switch branches, never touch another agent's
   worktree.** Checkpoint inside your own often — uncommitted work is lost work.
4. **Log every departure from the plan** in `deviations.md` as *plan said →
   reality → what I did → severity*. An unlogged deviation reads as a defect.
5. **An honest `blocked` is a success state.** Reporting that you are stuck with
   evidence beats a plausible-looking result that does not work. Never fabricate
   test output, never claim verification you did not run.
6. **Do not expand scope.** Unrelated bugs, refactors, and cleanups belong in
   `decisions.md` as observations, not in your diff.
7. **Write your report before you exit** (Step 4). No report means you crashed.

## When to read more

Read these **only when the condition applies** — not preemptively:

| Condition | Read |
|---|---|
| Your prompt says you are continuing work another agent started | `references/handover.md` |
| You are stuck, tests failed 3+ times, or scope is ballooning | `references/escalation.md` |
| You must depart from the plan and are unsure how severe it is | `references/deviations.md` |
| You share files with another running agent, or hit a merge conflict | `references/parallelism.md` |
| The repo is a Godot / Unity / Unreal project | `references/engines/<engine>.md` |
| A tool or engine forces you to write a file inside the repo | `references/scratch-files.md` |
| You are about to write code, or `task.json` lists `companions` | `references/companions.md` |
| You are about to write an artifact and want the exact fields | `schemas/`, `templates/` |

## Step 4 — Report before you exit

Write `$TASK_DIR/reports/<stage>-<role-or-subtask>.json` matching `schemas/report.schema.json`:
status (`complete` / `blocked` / `escalate`), a summary **written for the next
agent** (what changed, what surprised you, what they must know — no pleasantries,
no restating the task), artifacts written, deviations raised, signals fired, and
evidence (real command output). Your role card names the extra fields it needs.
