---
name: agent-delegation
description: Use when taking part in a delegated multi-agent development workflow — when a prompt assigns you a role (planner, implementer, test author, reviewer, integrator) for a task id, when a `.task/<task-id>/` directory exists in the repo, or when asked to plan work that other agents will implement, implement one subtask of an existing plan, write tests from requirements for someone else's implementation, review an implementation against its plan, or hand results to a downstream agent. Defines the shared protocol — which artifacts to read and write, what constraints to respect, and when to stop and escalate instead of pushing through.
---

# Agent Delegation

You are **one role in a pipeline**, not the whole system. Another agent planned
this work, or will review it. You reach those agents only through files in
`.task/<task-id>/` — they never see your reasoning, only what you write down.

## Step 1 — Establish your assignment

Your invoking prompt should give you a **role** and a **task id**. If either is
missing, do not guess: list `.task/`, read `task.json` (its `status` names the
active stage), and ask rather than assume. Working the wrong role corrupts
artifacts other agents depend on.

## Step 2 — Read exactly one role card

| Your role | Read | Your job, in one line |
|---|---|---|
| Planner / Architect | `roles/planner.md` | Produce a plan a weaker model can execute without you |
| Implementer | `roles/implementer.md` | Make the plan true for one subtask, inside its file scope |
| Test Author | `roles/test-author.md` | Encode the requirements as tests, blind to the implementation |
| Reviewer | `roles/reviewer.md` | Check requirements → plan → diff → evidence, and rule |
| Integrator | `roles/integrator.md` | Reconcile conflicting subtask results with minimal change |

Follow your card's numbered steps in order. Do not read the other cards.

## The task directory

Everything lives in `.task/<task-id>/`:

| File | Holds | Written by |
|---|---|---|
| `task.json` | Status, budgets, assignments | Orchestrator only — **never edit** |
| `task.md` | The request and its numbered acceptance criteria (`AC-n`) | Intake; amended only by humans |
| `plan.md` | Approach plus one YAML block per subtask | Planner |
| `deviations.md` | Append-only log of departures from the plan | Anyone who departs |
| `decisions.md` | Append-only design decisions and their reasons | Anyone deciding |
| `reports/` | One JSON handoff per agent, per stage | Every agent, at exit |

**Authority when they disagree:** `task.md` (what was asked) outranks
`plan.md` + `deviations.md` (how it is being done) outranks the code. Never
"fix" a conflict silently in the direction of the code.

## Hard rules

1. **Stay inside your declared file scope.** Touching files outside it is a
   deviation at minimum and usually an escalation. Wanting to fix something
   nearby is not permission to fix it.
2. **Never `git push`, never switch branches, never touch another agent's
   worktree.** Commit checkpoints inside your own worktree freely.
3. **Log every departure from the plan** in `deviations.md` as *plan said →
   reality → what I did → severity*. An unlogged deviation reads as a defect.
4. **An honest `blocked` is a success state.** Reporting that you are stuck with
   evidence beats a plausible-looking result that does not work. Never fabricate
   test output, never claim verification you did not run.
5. **Do not expand scope.** Unrelated bugs, refactors, and cleanups belong in
   `decisions.md` as observations, not in your diff.
6. **Write your report before you exit** (Step 3). No report means you crashed.

## When to read more

Read these **only when the condition applies** — not preemptively:

| Condition | Read |
|---|---|
| You are stuck, tests failed 3+ times, or scope is ballooning | `references/escalation.md` |
| You must depart from the plan and are unsure how severe it is | `references/deviations.md` |
| You share files with another running agent, or hit a merge conflict | `references/parallelism.md` |
| The repo is a Godot / Unity / Unreal project | `references/engines/<engine>.md` |
| You are about to write an artifact and want the exact fields | `schemas/`, `templates/` |

## Step 3 — Report before you exit

Write `reports/<stage>-<role>.json` matching `schemas/report.schema.json`:
status (`complete` / `blocked` / `escalate`), a summary **written for the next
agent** (what changed, what surprised you, what they must know — no pleasantries,
no restating the task), artifacts written, deviations raised, signals fired, and
evidence (real command output). Your role card names the extra fields it needs.
