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

The full technical design — lifecycle, routing, escalation, parallelism,
security, failure recovery — is in [`DESIGN.md`](DESIGN.md).

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

The repository keeps source, tests, and durable documentation. Nothing about a
run — no `.task/`, not even ignored — is ever written into it.

## What's in the repo

```text
agent-delegation/
├── SKILL.md              entry point (≤100 lines) — orientation and routing
├── roles/                one card per role; an agent reads exactly one
├── references/           depth loaded only when its trigger fires
│   ├── task-dir.md       locating task state on Linux / macOS / Windows
│   ├── escalation.md     when to stop, and how to hand off a stuck task
│   ├── deviations.md     minor vs major departures from the plan
│   ├── parallelism.md    file scopes and worktree etiquette
│   ├── scratch-files.md  the narrow in-repo escape hatch
│   └── engines/          Godot / Unity / Unreal specifics
├── schemas/              JSON Schema for reports, verdicts, subtask blocks
└── templates/            copy-paste starting points for task/plan/deviations

registry.default.yaml     model scores, tier bands, routing policy (orchestrator-side)
DESIGN.md                 the full architecture and the reasoning behind it
```

**Progressive disclosure is a hard constraint, not a style.** `SKILL.md` stays
under 100 lines; role cards near 100; references load only on a stated
condition ("tests failed 3 times → read `references/escalation.md`"). Context
spent on protocol is context not spent on the code.

## Design choices worth knowing before you adopt it

- **The orchestrator is deterministic code, not an LLM.** The state graph and its
  guards are authored and versioned; LLMs choose among edges the graph already
  offers, through schema-validated outputs. Every run is replayable.
- **Deterministic checks run before any LLM review.** Never pay reviewer tokens
  to discover a compile error.
- **Review is an evidence chain**, not a vibe check: every acceptance criterion
  gets a row and a verdict, and every blocking finding must cite a requirement,
  a plan line, or real output.
- **Escalation triggers on objective signals** — three failed attempts, scope
  overruns, edit churn — not on a model's self-reported confidence.
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
  deterministic scanner runs as part of verification (stdlib, sub-second, no
  model call) and flags generated-code chaff. Evaluated on this repo's own
  orchestrator, it found two real defects in 0.4 seconds.
- **`andrej-karpathy-skills:karpathy-guidelines`** — 67 lines on the mistakes
  that make generated code fail review (overcomplication, unrequested scope,
  unstated assumptions). Read by agents *before* they write code.
- **[superpowers](https://github.com/obra/superpowers)** —
  `systematic-debugging` at the second failed attempt.

Their findings are advisory. Authority stays with the acceptance criteria, the
plan, and the deterministic checks. See
[`agent-delegation/references/companions.md`](agent-delegation/references/companions.md)
and `DESIGN.md` §4.7.

## Game development

Web-dev assumptions break in game repos, so the engine references cover what
actually bites: binary and semi-mergeable scene formats, Unity `.meta` GUID
coupling, generated files, serialized-field renames that silently drop designer
data, Blueprint changes invisible to the compiler, and the pure-vs-engine-bound
test split that keeps iteration from grinding on a five-minute engine boot.

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
| Deviation logging, honest `blocked` reporting | Mechanically reverting out-of-scope edits |
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

```bash
orchestrator/delegate init                       # what's detected, who gets which role
orchestrator/delegate run "fix the failing auth test"
python3 orchestrator/tests/test_orchestrator.py  # 119 tests, no tokens spent
```

Python 3.9+, stdlib only, no install step. The tests drive the real state
machine over a real git repository with a scripted adapter, so they need no
agent CLI installed and spend nothing.

**Two limits to know before you rely on it.** Parallel subtasks still carry a
shared-state race — roughly 1 full-suite run in 8 — so keep
`max_parallel_agents: 1` until it is closed; the sequential path is unaffected.
And live quota metering, telemetry-driven registry recalibration, and container
isolation are absent. [`orchestrator/README.md`](orchestrator/README.md) has the
configuration, the full scope boundary, and the defect write-up; `DESIGN.md` §15
has the design it was built from.

## License

MIT — see [LICENSE](LICENSE).
