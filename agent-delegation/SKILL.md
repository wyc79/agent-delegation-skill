---
name: agent-delegation
description: Use when you are the agent a human is talking to and the work in front of you should be handed to other agents instead of done in this session — starting or advancing a task with the `delegate` CLI, answering a gate that parked waiting on a human, checking what a running task has produced, or telling the human where it got to. Covers which command to run, in what order, and how to read what comes back; the dispatched agents carry their own instructions and nothing here is about how they should work.
---

# Agent delegation

`delegate` places each stage of a task on whichever enrolled agent seat can do it
— different providers, different models — meters the quota on each, and keeps the
run going when one of them hits a wall.

**You run it. You are not in it.** It spawns fresh single-role agents that read a
task directory on disk; none of them sees your context and you never see theirs.
You stay the operator: you type the commands, read what comes back, and stand
between the run and the human.

How the work gets planned, written, tested or reviewed belongs to the agents
`delegate` spawns. They load their own instructions. This file has no opinion.

## First decide whether to delegate at all

Delegation costs a classification call, a planning call, at least one gate
round-trip through the human, and minutes of wall clock before a line changes.
Most requests do not earn that.

**Do the work yourself** when you can finish it in a handful of edits, when you
already have the file open and know the change, when the request is a question
rather than work, or when the human is still deciding what they want — a
conversation delegates badly. A one-shot edit pushed through `delegate` is
slower, costs more, and arrives with less of the human in it.

**Delegate** when one of these holds:

- The work is larger than one session can hold, and is better as a written plan
  and separate subtasks than as one long thread.
- Subtasks are independent and would genuinely run in parallel worktrees.
- You want a reviewer that has not already convinced itself the code is right,
  or tests written by something that has not seen the implementation.
- Your own seat is the constraint — a quota wall you are about to hit, or a
  model better suited to part of the job than you are.

Run `init` before promising anything: it prints which agent CLIs are usable and
which model gets which role. **If every role resolves to one seat, you are paying
for indirection** — independence and parallelism are then the only reasons left.
Say that rather than delegating anyway.

## The commands

Run them from the repository being changed, by absolute path. `delegate`
identifies the project from `git rev-parse --git-common-dir`, so the working
directory matters; `--repo <path>` overrides it.

| Command | Does |
|---|---|
| `init [--write] [--force]` | Detected seats, role assignments, companion skills, verify config. Writes nothing without `--write`. |
| `run <request>` | Create a task and drive it. `<request>` is text, or a path to a file holding it. |
| `resume [--id] [--stage] [--when-open]` | Continue a parked task. `--stage` restarts at a named stage; `--when-open` sleeps out a quota window first. |
| `approve [--id] --note "…"` | Answer a waiting gate yes, then continue the run. |
| `reject [--id] --note "…"` | Answer it no. Records the decision and stops. |
| `status` | One line per task for this project, ending in its task directory. |
| `show [--id] [--brief]` | `--brief` prints the gate brief, already written for a human; without it, raw `task.json`. |
| `channels [--clear NAME]` | Per-seat cooldowns and estimated quota draw. |

`run` also takes `--id`, `--mode attended|autonomous`, `--adapter herdr|local|mock`,
`--no-panes`, `--max-cost N`, `--review auto|always|never`, `--dry-run`, `--yes`.
`resume`, `approve` and `reject` take the adapter flags too; `approve` and
`reject` also take `--no-continue`, which records the decision without resuming.
`--id` is optional only while the project has exactly one task.

Two to understand before using them:

- `--dry-run` walks the state machine with no agents and no spend. It is the
  cheapest way to show a human the shape of a run.
- `--yes` auto-approves **every** gate for the whole run. It exists for
  unattended runs with nobody present. Never reach for it because a gate is
  inconvenient — the gates are the human's only say in what gets built.

## Exit code 1 does not mean it failed

`run` and `resume` exit 0 only when the task reached `done`. Parked, waiting,
declined and crashed all exit 1. **Read the status, not the exit code**, and read
it again after every call — each one can park again.

| Status | Means |
|---|---|
| `awaiting_approval` | A gate is waiting on the human. Below. |
| `waiting on quota` (as `status` prints it) | Every seat for a role is cooling. `channels` says until when; `resume --when-open` waits it out. |
| `needs_human` | The run stopped and something needs deciding or fixing. |
| `done` | Finished. Attended mode leaves `integrate.patch` in the task directory for `git apply` — nothing was committed to the human's branch, and no mode merges. |
| a stage name | Interrupted mid-flight; `resume` picks it up. |

## A parked gate

Three gates exist: `design`, `plan`, `merge`. Reaching one with nobody at a
terminal does not decline it — the task parks, status `awaiting_approval`, and
`task.json` grows a `pending_gate` holding `kind`, `brief` and `resume_status`.
A question nobody answered is not a no, and the CLI will not record one as the
other. Answering it is your job:

1. **Read the brief.** `delegate show --id T-001 --brief`, the same text as
   `pending_gate.brief`.
2. **Put the question to the human in prose.** Not the JSON, and not the brief
   pasted whole unless they ask. Say what is being decided, what it changes in
   their repo, and what happens if they say no. Offer the plan or the diff.
3. **Take their answer with its qualifications.** "Yes, but keep the old
   endpoint" is not a yes.
4. **Record it.** `delegate approve --id T-001 --note "keep the old endpoint
   working"`, or `reject` with the same shape. The note lands in `gates[]` in
   `task.json` and is echoed in the run log when the machine consumes the
   decision. It is the record of what a human actually decided, which nothing
   else reconstructs — write what they said, not your summary of it.
5. **Approving continues the run in the same call**, from `resume_status`, which
   is deliberately past the stage that asked so an approved plan is not
   re-planned. It may park at the next gate. Go back to step 1.

Rejecting ends that run: the decision is recorded, the task parks at
`needs_human`, nothing continues. `delegate resume --stage <stage>` restarts from
a stage you name — `intake`, `classify`, `brainstorm`, `plan`, `implement`,
`review`, `integrate`.

**Never answer a gate the human has not answered.** Approving on their behalf
because the run is sitting there turns the checkpoint into a formality, and it is
the one thing here a later `resume` cannot undo.

## Reading what happened

The task directory is printed when `run` creates it and is the last column of
`status`. It is readable while the run is still going.

| File | Answers |
|---|---|
| `task.md` | What was asked, and the numbered acceptance criteria. |
| `spec.md` | The design that was approved, when there was a design stage. |
| `plan.md` | The approach, and one block per subtask with its file scope. |
| `brief.md` | The last gate brief. Already written for a human. |
| `reports/*.json` | One per agent per stage: what it did, what surprised it, what it could not do. |
| `verify/*.json` | Build, test and lint output. |
| `deviations.md`, `decisions.md`, `escalation.md` | Departures from the plan, decisions and their reasons, and why a subtask went back to the planner. |
| `agent-logs/*.log` | Each agent's prompt and its output — where to look when one appears to have done nothing. |
| `task.json` | Status, spend, gate history, which model ran what. Orchestrator-owned; never edit it. |

Relay from these in prose. "The planner split it into three subtasks, two are
done, the third is waiting on you to approve the merge" is the deliverable. A
pasted `task.json` is not.
