# delegate — MVP orchestrator

The deterministic half of the system. The skill tells agents *how to behave*;
this program decides *what runs next, on which model, and whether it is allowed
to* — see [`../DESIGN.md`](../DESIGN.md) §15.

**`adg`** is short for *agent delegation*. It is the Python package name, and the
prefix on everything this tool creates so it is greppable and never mistaken for
your own: branches `adg/<task-id>/<subtask>`, worktrees under
`.adg-worktrees/<project-key>/`, project config `.adg.yaml`, and the
`ADG_WINNOW_SCAN` override.

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
local|herdr|mock` overrides runtime selection, `--no-panes` trades herdr's
visible agent panes back for cost accounting (pane sessions report no cost, so
`max_cost_usd` cannot bind while they are on), `--max-cost 5` lowers (never
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
brainstorm: auto            # auto: design dialogue on complex attended tasks | always | never
test_author: auto           # auto (default) | never — independent tests on complex tasks
winnow: auto                # auto (default) | never — deterministic chaff scan
winnow_scan: ~/.claude/skills/code-winnow/scripts/scan.py   # only if autodetect misses
```

No config is legal — checks are then reported as *not run* rather than faked.

`winnow_scan` is only needed when autodetect misses. It looks in
`.claude/skills/` and `.agents/skills/` under the repo, then `~/.claude`,
`~/.cursor` and `~/.agents` — so a code-winnow installed anywhere else (another
runtime's skill directory, a bare clone) has to be pointed at, either with this
key or the `ADG_WINNOW_SCAN` environment variable. Either one wins over
autodetect, and a path that is set but wrong is reported as a misconfiguration
rather than quietly reported as "not installed".

## Layout

| File | Role |
|---|---|
| `adg/machine.py` | The state machine. Every transition lives here. |
| `adg/store.py` | Task state outside the repo; project key from the git common dir. |
| `adg/router.py` | Capability scoring, enrollment, escalation ceiling. |
| `adg/limits.py` | Hard limits, checked before the action, failing closed. |
| `adg/runtime.py` | The seven-operation adapter: `local`, `herdr` (agents run in visible panes), `mock`. |
| `adg/verify.py` | Deterministic checks and mechanical scope comparison. |
| `adg/winnow.py` | Optional [code-winnow](https://github.com/wyc79/code-winnow-skill) scanner — referenced, never vendored. |
| `adg/companions.py` | Detects karpathy-guidelines and superpowers once, and declares them in `task.json`. |
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

No agent CLI has to be installed for the suite to pass. Tests that exercise
launch routing stub `can_run`, the single seam that decides whether a kind is
usable, rather than depending on a vendor binary being on PATH.

## Scope

Implemented: the full pipeline, parallel subtasks in separate worktrees (bounded
by `max_parallel_agents`, serialized on scope or hotspot overlap), the Integrator
on merge conflicts, an independent Test Author on complex tasks, real cost
accounting from the CLI, autonomous mode ending at an opened PR, and
model-rendered briefs.

Still absent: live quota metering per subscription window, telemetry-driven
registry recalibration, and container isolation. Escalation is two signals
(`test_stuck`, `scope_overrun`) and a two-rung ladder.

## Known defect

`TestWaveRaces.test_one_subtask_escalating_stops_the_run_even_as_a_sibling_succeeds`
and `TestParallelEndToEnd.test_two_subtasks_get_two_worktrees_and_both_land`
fail intermittently — roughly 1 run in 8 of the **full** suite, never
reproducible in isolation (12/12 clean runs of the test alone). Both are
parallel-path tests, and in the captured failure an implementer's `escalate`
report did not reach the halt check, so a wave subtask completed that should
have parked.

Serializing `task.json` mutations and making verify/transcript ids unique per
invocation reduced it from 2/8 to 1/8 without removing it, so at least one
shared-state race remains in the wave path. **Treat parallel mode as
unfinished**: run sequentially (one subtask per plan, or `max_parallel_agents:
1`) until this is closed. The sequential path is not affected.
