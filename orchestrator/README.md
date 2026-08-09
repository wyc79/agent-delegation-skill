# delegate — MVP orchestrator

The deterministic half of the system. The skill tells agents *how to behave*;
this program decides *what runs next, on which model, and whether it is allowed
to* — see [`../DESIGN.md`](../DESIGN.md) §15.

Python 3.9+, standard library only. No install step, no dependencies (the YAML
subset the project uses is parsed by `adg/yamlite.py`, because PyYAML is not in
the stdlib and requiring an install would be a worse trade).

## Use

```bash
orchestrator/delegate init                      # what's detected, who gets which role
orchestrator/delegate run "add a subtract API"  # run a task
orchestrator/delegate status                    # tasks for this project
orchestrator/delegate show --brief              # the human-readable summary
orchestrator/delegate resume --id T-001         # continue a parked task
```

Useful flags: `--dry-run` drives the state machine with no agents, `--adapter
local|herdr|mock` overrides runtime selection, `--max-cost 5` lowers (never
raises) the cost cap, `--yes` auto-approves gates for unattended runs — merge
still never happens automatically.

## Project config

Optional `.adg.yaml` in the repository being worked on:

```yaml
fast:                       # run after every implementation attempt
  - "python3 -m pytest -q"
slow:                       # stage boundaries only (engine boots, full builds)
  - "python3 -m pytest -q --runslow"
hotspots:                   # force COMPLEX classification when mentioned
  - "src/combat/combat_system.gd"
ignore:                     # extra generated paths to exclude from scope accounting
  - "Build/*"
```

No config is legal — checks are then reported as *not run* rather than faked.

## Layout

| File | Role |
|---|---|
| `adg/machine.py` | The state machine. Every transition lives here. |
| `adg/store.py` | Task state outside the repo; project key from the git common dir. |
| `adg/router.py` | Capability scoring, enrollment, escalation ceiling. |
| `adg/limits.py` | Hard limits, checked before the action, failing closed. |
| `adg/runtime.py` | The seven-operation adapter: `local`, `herdr`, `mock`. |
| `adg/verify.py` | Deterministic checks and mechanical scope comparison. |
| `adg/brief.py` | Human-facing gate briefs, plus the jargon lint. |
| `adg/schema.py` | Report/verdict validation against the skill's schemas. |
| `adg/prompts.py` | Injects role, paths, scope, budget — and nothing else. |
| `adg/yamlite.py` | The YAML subset used by the registry and plan files. |

## Invariants worth not breaking

- **Nothing is written into the working repository.** Task state lives in
  `$XDG_STATE_HOME/agent-delegation/`; the project key comes from
  `git rev-parse --git-common-dir`, which is identical from every worktree.
- **Attended mode never commits to your branch**, and no mode merges. The
  terminal state is a patch file or a pushed branch. There is no commit path to
  the user's branch in this codebase — checkpoint commits happen only inside
  throwaway worktrees.
- **Limits fail closed.** A missing or unparseable limit parks the task rather
  than meaning "unlimited".
- **The top model tier needs two switches**: enrollment *and* the ceiling.
- **Liveness is not success.** An agent settling `idle` proves nothing; a
  schema-valid report plus real verify output does.
- **The skill names no model and no runtime.** If either leaks into
  `agent-delegation/`, the boundary has broken.

## Tests

```bash
python3 orchestrator/tests/test_orchestrator.py
```

The end-to-end tests drive the real state machine over a real git repository
with a scripted adapter, so the whole pipeline — plan, isolated implementation,
verify, review, integrate — is exercised without spending a token.

## MVP scope

Deliberately absent, per DESIGN.md §15: parallel subtasks (one worktree per
task, sequential), the Integrator role, an independent Test Author, live quota
metering, telemetry recalibration, and containers. Escalation is two signals
(`test_stuck`, `scope_overrun`) and a two-rung ladder.
