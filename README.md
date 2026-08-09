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

Agents never message each other. Everything moves through files in
`.task/<task-id>/`:

| File | Holds | Authority |
|---|---|---|
| `task.md` | The request and its numbered acceptance criteria | **What was asked** — outranks everything |
| `plan.md` | Approach plus one YAML block per subtask | **How it is being done** |
| `deviations.md` | Append-only log of departures from the plan | Amends `plan.md` |
| `decisions.md` | Append-only design decisions and why | Context |
| `reports/*.json` | One schema-validated handoff per agent | Evidence |
| `task.json` | Status, budgets, assignments | Orchestrator-owned |

## What's in the repo

```text
agent-delegation/
├── SKILL.md              entry point (≤100 lines) — orientation and routing
├── roles/                one card per role; an agent reads exactly one
├── references/           depth loaded only when its trigger fires
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

## Game development

Web-dev assumptions break in game repos, so the engine references cover what
actually bites: binary and semi-mergeable scene formats, Unity `.meta` GUID
coupling, generated files, serialized-field renames that silently drop designer
data, Blueprint changes invisible to the compiler, and the pure-vs-engine-bound
test split that keeps iteration from grinding on a five-minute engine boot.

## Status

The skill is complete and usable by hand today — you can assign roles yourself
and get the artifact discipline immediately. The orchestrator that automates
routing, worktrees, and gates is specified in `DESIGN.md` §15 but not yet built.

## License

MIT — see [LICENSE](LICENSE).
