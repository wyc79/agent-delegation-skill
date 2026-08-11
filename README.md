# Agent Delegation Skill

A reusable skill that lets AI coding agents participate in a **multi-agent,
multi-provider development workflow** — one agent plans, others implement in
isolation, another reviews against the plan, and the work integrates without
anyone losing track of what was actually asked.

The skill defines the **protocol only**: roles, artifacts, handoffs, escalation.
It names no provider and no model, so the same workflow runs on whatever mix of
agent CLIs and subscriptions you happen to have.

Two principles drive every design decision, in this order:

1. Use expensive reasoning where it materially improves decisions, and
   inexpensive models where the work is predictable.
2. Separate the workflow from the model/provider, so providers can be swapped
   without redesigning anything.

[`orchestrator/README.md`](orchestrator/README.md) is what is actually built.

`DESIGN.md` — the initial design, written before any code existed — was removed
on 2026-08-10. It described a workflow-quality thesis this project no longer
holds, so keeping it at the root would have made the repo's most prominent
document its most out-of-date one. Code comments still cite it by section
(`DESIGN.md §5.4`); those resolve through git history, where the file remains.

## Install

```bash
npx skills add git@github.com:wyc79/agent-delegation-skill.git
```

Or copy the `agent-delegation/` folder into your agent's skills directory.

No model configuration is needed to install the skill. Agents never choose
models — they receive a role and read artifacts. Model routing belongs to an
orchestrator, and `registry.default.yaml` is the config it would use.

## How it works

An orchestrator assigns a role and a task id; the agent loads this skill and
finds everything else on disk:

```text
User request
    ↓  classify           simple work skips straight to implementation
    ↓  design             complex attended work only — a spec you approve first
    ↓  plan               strong model writes a durable plan, not chat
    ↓  implement          cheaper models, one subtask each, isolated worktrees
    ↓  verify             build/test/lint — deterministic, before any LLM review
    ↓  review             requirements → plan → diff → evidence, then a verdict
    ↓  integrate          merge in dependency order, re-verify at each step
```

Agents never message each other. Everything moves through files in a task
directory that lives **outside the repository** — so orchestration state never
touches your git history, and every worktree shares one copy:

```text
~/.local/state/agent-delegation/projects/<project-key>/tasks/<task-id>/
```

`<project-key>` is derived from `git rev-parse --git-common-dir`, which resolves
identically from every worktree of a repo — so any agent, anywhere, finds the
same task state with no configuration. The orchestrator also injects
`$AGENT_DELEGATION_TASK_DIR` so the normal path is a single env read.

| File | Holds | Authority |
|---|---|---|
| `task.md` | The request and its numbered acceptance criteria | **What was asked** — outranks everything |
| `plan.md` | Approach plus one YAML block per subtask | **How it is being done** |
| `deviations.md` | Append-only log of departures from the plan | Amends `plan.md` |
| `decisions.md` | Append-only design decisions and why | Context |
| `reports/*.json` | One schema-validated handoff per agent | Evidence |
| `verify/` | Build, test, and lint output by run id | Evidence |
| `task.json` | Status, budgets, model assignments, delegation history | Orchestrator-owned |

The repository keeps source, tests, and durable documentation. None of this
system's state — no `.task/`, not even ignored — is ever written into it. (A
companion skill's own artifacts are its own business, under its own exclusion
rule; nothing here writes them.)

## What's in the repo

```text
agent-delegation/
└── SKILL.md              THE FRONT DOOR. For the agent a human is talking to:
                          when to call `delegate`, which command comes next,
                          how to answer a parked gate. No methodology.

orchestrator/workflows/default/
                          The protocol DISPATCHED agents follow — the bundled
                          default workflow, orchestrator-internal, not
                          something a user installs.
├── PROTOCOL.md           entry point (≤100 lines) — orientation and routing.
│                         No frontmatter: it is read by absolute path, not
│                         installed, and two files claiming the same skill
│                         name is a collision a loader resolves arbitrarily.
├── roles/                one card per role; an agent reads exactly one
├── references/           depth loaded only when its trigger fires
│   ├── task-dir.md       locating task state on Linux / macOS / Windows
│   ├── escalation.md     when to stop, and how to hand off a stuck task
│   ├── deviations.md     minor vs major departures from the plan
│   ├── parallelism.md    file scopes and worktree etiquette
│   ├── handover.md       continuing a turn another agent left part-way
│   ├── companions.md     which optional skills apply to which role
│   └── scratch-files.md  the narrow in-repo escape hatch
├── schemas/              JSON Schema for reports, verdicts, subtask blocks
└── templates/            copy-paste starting points for task/plan/deviations

registry.default.yaml     model scores, tier bands, routing policy (orchestrator-side)
```

**Progressive disclosure is a hard constraint, not a style.** `PROTOCOL.md` stays
at 100 lines or under; role cards under 130; references load only on a stated
condition ("tests failed 3 times → read `references/escalation.md`"). Context
spent on protocol is context not spent on the code.

The budget that matters is **per role, not per repo**. `PROTOCOL.md` is read by all
five roles, so a line there costs five times a line in a card — which is why
triggers that belong to one role live on that role's card. A reviewer never
loads the parallelism rules or the companion table, because it never writes code
or shares a worktree. Moving those rows out of the shared entry point lengthened
the cards and made every individual role cheaper.

## Design choices worth knowing before you adopt it

- **The orchestrator is deterministic code, not an LLM.** The state graph and its
  guards are authored and versioned; LLMs choose among edges the graph already
  offers, through schema-validated outputs. Every run is replayable.
- **Deterministic checks run before any LLM review.** Never pay reviewer tokens
  to discover a compile error.
- **Review is an evidence chain**, not a vibe check: every acceptance criterion
  gets a row and a verdict, and every blocking finding must cite a requirement,
  a plan line, or real output.
- **Escalation triggers on objective signals** — three consecutive failed
  checks, or a signal the agent raised with an artifact citation and real output
  attached — not on a model's self-reported confidence, which is a tiebreaker
  and cannot move anything on its own. Out-of-scope edits are measured
  mechanically and force a review rather than escalating.
- **Parallelism is pessimistic.** Disjoint declared write scopes in separate
  worktrees, with unmergeable files (engine scenes, prefabs, `.uasset`) locked
  to one agent. A merge conflict in a binary scene has no good resolution.
- **The top model tier is off by default.** Enrollment is fail-closed and the
  escalation ceiling is separate, so reaching the heaviest tier takes two
  deliberate edits rather than one runaway ladder.

## Companion skills

Optional, detected automatically, each attached where it belongs. Missing ones
are reported as missing rather than silently skipped.

- **[code-winnow](https://github.com/wyc79/code-winnow-skill)** — its
  deterministic scanner runs at the review stage (stdlib, sub-second, no model
  call) and flags generated-code chaff, on tasks that skip LLM review too. It is
  fast enough that the cost never enters the decision: on this repo it reads a
  five-commit range in about 0.6s. The findings it returns are advisory, and it
  has been worth having — an early run against five commits of this repo turned
  up two dead locals and three near-duplicate tests, all real.
- **[`andrej-karpathy-skills:karpathy-guidelines`](https://github.com/multica-ai/andrej-karpathy-skills)** — 67 lines on the mistakes
  that make generated code fail review (overcomplication, unrequested scope,
  unstated assumptions). Read by agents *before* they write code.
- **[superpowers](https://github.com/obra/superpowers)** — `brainstorming`
  drives the optional design stage on complex attended tasks: it explores the
  code, weighs two or three approaches, and writes `spec.md`, which is gated to
  you and then handed to the planner as a settled approach. Also
  `systematic-debugging` at the second failed attempt. Its `writing-plans` is
  deliberately *not* used — a plan here is a machine-enforced contract, not
  prose ([`companions.md`](orchestrator/workflows/default/references/companions.md)).

Their findings are advisory. Authority stays with the acceptance criteria, the
plan, and the deterministic checks. See
[`orchestrator/workflows/default/references/companions.md`](orchestrator/workflows/default/references/companions.md)
and `DESIGN.md` §4.7.

## Status

**The skill is complete. Can one agent drive a task end to end with it today?**
Yes — with one caveat that matters.

Given a task and the skill, a capable agent can create the task directory, write
`task.md`, plan, decompose, implement subtask by subtask, run the project's
checks, review its own work against the plan, and integrate. The protocol is
self-contained: every step names the artifact it produces and the next role reads
it from disk. Nothing in the loop requires the orchestrator to exist.

**The caveat: one agent playing every role loses the independence the roles are
for.** A reviewer that shares context with the implementer already believes the
implementation is correct, and a test author that has seen the code writes tests
that mirror it. To get the real value, run each role in a **fresh session or
subagent** with only its artifacts as input — that is the part a human driving by
hand has to be disciplined about.

What you also give up without the orchestrator is *enforcement and routing*,
which is exactly what code is good at and prompts are not:

| Works with just the skill | Needs the orchestrator |
|---|---|
| Artifact discipline, handoffs, authority ordering | Different models per role (principle #1 is unrealized on one model) |
| Review as an evidence chain; independent test authoring | Escalating to a stronger model on failure |
| Deviation logging, honest `blocked` reporting | Mechanically detecting out-of-scope edits and forcing a review |
| Escalation *thresholds* as self-discipline | Counting iterations and enforcing budget caps |
| Sequential subtasks in one worktree | Parallel worktrees with lock/hotspot enforcement |
| Asking the human at obvious moments | Schema validation, cost tracking, jargon-free briefs |

**The orchestrator is built.** `adg` — the program in
[`orchestrator/`](orchestrator/) — automates the parts above that code does
better than prompts:

- choosing which model runs each role (`DESIGN.md` §5)
- creating and integrating worktrees (§7)
- **approval checkpoints** — pausing for a human before a plan is executed,
  before a branch is merged, or when cost or repeated failure crosses a
  threshold (§9)
- retrying and escalating failures up a ladder (§6)

## Running it

`delegate` is a command line program. **You do not talk to it — you run it, and
it launches the agents.** The one thing to get right is that the agent you are
chatting with is not the agent doing the work: it types the command, and `adg`
spawns fresh, single-role agents that read the task directory.

It runs **from the repo you want changed**, not from this one, so give it an
absolute path:

```bash
export ADG=/path/to/agent-delegation-skill/orchestrator/delegate
cd /path/to/your/repo          # the repo to be changed
$ADG init                      # what's detected, who gets which role
$ADG run "fix the failing auth test"
```

`init` prints what it detected — which agent CLIs are signed in, which model
gets which role — and writes it only if you pass `--write`. `run` then drives
the whole pipeline, stopping at each approval gate to ask you. Python 3.9+,
stdlib only, no install step. (`--repo` overrides the default of the current
directory, if you would rather not `cd`.)

### Driving it from a chat agent

Ask the agent you are already talking to (Claude Code, Cursor, whatever) to run
the command. It stays the **operator**: it runs `delegate`, reads what comes
back, and answers the gates with you. It is not the planner or the implementer —
those are separate processes with separate context, which is the whole point.

> Run `/path/to/agent-delegation-skill/orchestrator/delegate run "fix the
> failing auth test"` here, and show me the plan when it stops for approval.

Two modes, and the difference is who answers the gates:

| Mode | Flag | Gates | Ends at |
|---|---|---|---|
| **Attended** (default) | — | It stops and asks you | A patch file you apply yourself |
| **Autonomous** | `--mode autonomous --yes` | Auto-approved | An opened PR — **merge is never automatic in either mode** |

Attended is the right default the first few times: you see the plan before it is
executed and the diff before anything lands. Add `--dry-run` to walk the state
machine with no agents and no cost, which is the cheapest way to see the shape
of a run. Other flags — `--adapter`, `--no-panes`, `--max-cost`, `--review` — are
in [`orchestrator/README.md`](orchestrator/README.md).

Still from the repo being worked on:

```bash
$ADG status                    # tasks for this project
$ADG show --brief              # the human-readable summary
$ADG resume --id T-001         # continue a parked task
$ADG channels                  # quota cooldowns and draw per seat
```

And from a clone of *this* repo, the suites:

```bash
python3 orchestrator/tests/test_orchestrator.py  # 181 tests, no tokens spent
python3 orchestrator/tests/test_failover.py      #  98 more, same
```

They drive the real state machine over a real git repository with a scripted
adapter, so they need no agent CLI installed and spend nothing.

**Two limits to know before you rely on it.** Parallel waves are enabled
(`max_parallel_agents: 3`) on the strength of deterministic tests: the
long-intermittent wave failure turned out to be the test harness's own path
matching, not a race, and every report-attribution defect found on the way is
fixed and pinned — but no wave has yet run with real agent CLIs, so treat the
first as a shakedown. And live quota metering, telemetry-driven registry
recalibration, and container isolation are absent.
[`orchestrator/README.md`](orchestrator/README.md) has the configuration, the
full scope boundary, and the defect write-up; `DESIGN.md` §15 has the design it
was built from.

## License

MIT — see [LICENSE](LICENSE).
