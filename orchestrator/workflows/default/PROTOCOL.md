# Agent delegation protocol

The workflow dispatched agents follow. **Not an installable skill** — the
orchestrator hands an agent this directory's absolute path in
`$AGENT_DELEGATION_SKILL_DIR`, so it is read from disk wherever it was
unpacked. The installable skill in this repo is `agent-delegation/` at the
root, which has a different audience: the agent a human is talking to.

# Agent Delegation

You are **one role in a pipeline**, not the whole system. Another agent planned
this work, or will review it, and reaches you only through files — nobody ever
sees your reasoning, only what you write down.

## Step 1 — Establish what you are, and where the files are

`$AGENT_DELEGATION_ROLE` names your role, and **its presence is what says this
protocol applies to you**. `$AGENT_DELEGATION_TASK_DIR` says only *where the
files are*: handed that path without a role, you are not in this workflow.

Task state lives **outside the repository** — shared by every worktree, never in
git history; that path is `$TASK_DIR` below. Unset, derive it with
`references/task-dir.md`, never by improvising one.

**Never create the task directory, and never write task artifacts into the
repository.** If you cannot find it, stop. (Tool-forced exception: rule 2.)

Your prompt must also carry a **task id**. Missing either, do not guess: write a
`blocked` report naming what is missing — nobody is listening for a question, so
the report is how you ask. The wrong role corrupts other agents' artifacts.

## Step 2 — Read exactly one role card

| Your role | Read | Your job, in one line |
|---|---|---|
| Planner / Architect | `roles/planner.md` | Produce a plan a weaker model can execute without you |
| Implementer | `roles/implementer.md` | Make the plan true for one subtask, inside its file scope |
| Test Author | `roles/test-author.md` | Encode the requirements as tests, blind to the implementation |
| Reviewer | `roles/reviewer.md` | Check requirements → plan → diff → evidence, and rule |
| Integrator | `roles/integrator.md` | Reconcile conflicting subtask results with minimal change |

Follow its numbered steps in order. **Do not read another role's card** — it
starts you from that role's frame, and the independence is the point of the split.

## The task directory, relative to `$TASK_DIR`

| File | Holds | Written by |
|---|---|---|
| `task.json` | Status, budgets, assignments, delegation history | Orchestrator only — **never edit** |
| `task.md` | The request and its numbered acceptance criteria (`AC-n`) | Intake or a human. The planner may add criteria if there are none; nobody else edits it |
| `spec.md` | The approved design: purpose, the approach chosen over the alternatives, risks | Brainstorm stage, then a human. Complex attended tasks only |
| `plan.md` | Approach plus one YAML block per subtask | Planner |
| `escalation.md` | Append-only. Why a subtask came back to the planner: failing checks, the agent's account and signals, completed work to disposition | Orchestrator, at rung 3 |
| `deviations.md` | Append-only log of departures from the plan | Anyone who departs |
| `decisions.md` | Append-only design decisions and their reasons | Anyone deciding |
| `reports/` | One JSON handoff per agent, per stage (`verify/` holds check output) | Every agent, at exit |

**Authority when they disagree:** `task.md` (what was asked) outranks `spec.md`
(the approach a human approved) outranks `plan.md` + `deviations.md` (how it is
being done) outranks the code — never resolve a conflict silently toward the
code. Re-opening `spec.md` is a `decisions.md` entry, not a quiet call; it
settles neither scope nor sequencing, and never outranks a criterion.

## Hard rules

1. **Stay inside your declared file scope.** Wanting to fix something nearby is
   not permission to fix it.
2. **Task artifacts never enter the repo.** A tool-forced in-repo path must be
   one `git check-ignore -q` accepts — see `references/scratch-files.md`.
3. **Never `git push`, never switch branches, never touch another agent's
   worktree.** Checkpoint inside your own often — uncommitted work is lost work.
4. **Log every departure** in `deviations.md` as *plan said → reality → what I
   did → severity*. An unlogged deviation reads as a defect.
5. **An honest `blocked` is a success state.** Never fabricate output or claim
   verification you did not run; a plausible result that fails is worse.
6. **Do not expand scope.** Unrelated bugs and cleanups go in `decisions.md` as
   observations, not in your diff.
7. **Write your report before you exit** (Step 3). No report means you crashed.

## When to read more — only once the condition applies

Shared triggers; your card carries its own, and nobody carries another's.

| Condition | Read |
|---|---|
| You are continuing work another agent started | `references/handover.md` |
| Stuck, checks failed 3+ times, or scope ballooning | `references/escalation.md` |
| Departing from the plan, unsure how severe it is | `references/deviations.md` |
| A tool or engine forces a write inside the repo | `references/scratch-files.md` |
| About to write an artifact and you want the exact fields | `schemas/`, `templates/` |

## Step 3 — Report before you exit

Write `$TASK_DIR/reports/<stage>-<subtask-id>.json`, or `<stage>-<role>.json`
with no subtask. **The id is not optional when you have one**, and must fill
everything after the stage word exactly — siblings share your role name, so a
role-named file and `implement-st-1-final.json` alike count as no report at all.
Match `schemas/report.schema.json`: status (`complete` / `blocked` / `escalate`),
a summary **for the next agent** (what changed, what surprised you, what they
must know), artifacts, deviations, signals, and real command output.

**`escalate` is routed by your `signals`, not your prose** — one that names its
type, cites an artifact and carries real output. Nothing routable stops the run.
