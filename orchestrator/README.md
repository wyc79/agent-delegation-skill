# delegate — MVP orchestrator

The deterministic half of the system. The skill tells agents *how to behave*;
this program decides *what runs next, on which model, and whether it is allowed
to*. **This file is what is actually built**; the `§` citations throughout point
in git history at `DESIGN.md`, the initial design, for why a decision went
the way it did — start at its §15.

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
`~/.cursor` and `~/.agents`, then walks `~/.claude/plugins/` for the same
`skills/code-winnow/scripts/scan.py` tail nested under a marketplace and a
plugin — so a code-winnow delivered as a plugin is found rather than reported
missing. Anywhere else (another runtime's skill directory, a bare clone) has to
be pointed at, either with this key or the `ADG_WINNOW_SCAN` environment
variable. Either one wins over autodetect, and a path that is set but wrong is
reported as a misconfiguration rather than quietly reported as "not installed".

## Quota failover

Subscription seats run out. When an agent CLI fails with its provider's
usage-limit shape — a 429, `usage limit reached`, `RESOURCE_EXHAUSTED` — the
orchestrator treats it as a *routing event*, not a failure (DESIGN.md §5.4,
§5.5): it opens a cooldown on that channel, re-selects the same role on another
enrolled channel, and carries on in the same worktree, so every checkpoint
commit the first agent made is still there.

**A quota failure does not consume an attempt.** `max_attempts_per_subtask`
bounds how many times an *approach* may be retried; an empty seat is not the
approach's fault. Escalation and failover stay separate and compose: a failover
target can still escalate later if the work itself is stuck.

The breaker lives in `$XDG_STATE_HOME/agent-delegation/channels.json` — beside
`projects/`, **not** inside one, because a quota belongs to a seat and two repos
driving the same subscription must see one cooldown. Expired entries are ignored
on read and pruned on write. A corrupt, wrongly-shaped, or unwritable file is
reported and treated as *no cooldowns*; it never invents one and never crashes a
run. Concurrency is honest about its limits: writes merge under an in-process
lock, which makes a lost breaker between two `delegate` processes recoverable
(the next call to that seat re-opens it) but does **not** make the file safe for
simultaneous writers in the strict sense — there is no cross-process lock.

`delegate channels --clear` is the override, and it really does unblock: the
breaker file is what `resume` consults, so clearing a seat lets a parked task
run again immediately rather than waiting out the recorded window.

```bash
orchestrator/delegate channels                       # cooldowns and quota draw
orchestrator/delegate channels --clear claude-seat   # override one
orchestrator/delegate resume --when-open --id T-001  # wait for the window, then resume
```

With every channel for a role cooled, the task parks as `quota_all_exhausted`
carrying the earliest reopen time, and `delegate resume` refuses until then
rather than starting a run every seat will reject.

### What is measured, and what is estimated

The cooldown's reopen time is the provider's own when it states one, and the
channel's configured `quota.window` otherwise — the fallback is logged, never
silent.

Utilization is **not** metered. Providers expose no counter, so the orchestrator
counts one invocation as one unit against `quota.est_capacity` inside the
window. That estimate drives the §5.4 shadow price — a subscription with
headroom costs ~0, and the same seat at 90% drawn prices itself above a metered
key — so cost-sensitive roles drift to the emptier seat before anything hits a
wall. `reserve_for` / `reserve_fraction` keep the declared share of a seat free
for the roles named there: a reserved role is never filtered out, a non-reserved
one is once the seat passes `1 − reserve_fraction`, *unless* that would leave it
with nowhere to go — then it gets the seat anyway and the demotion is logged. A
reservation that parks work it could have done is not worth having. "Nowhere to
go" is judged **after** the check for which agent CLI is actually installed, not
before: a candidate on an uninstalled CLI is not an alternative.

`weekly_cap: true` is **not** modelled on the utilization path: estimating a
weekly capacity from a five-hour `est_capacity` would be inventing a number. A
provider-stated weekly exhaustion still parks correctly, with the reset time the
provider gave.

## Layout

| File | Role |
|---|---|
| `adg/machine.py` | The state machine. Every transition lives here. |
| `adg/store.py` | Task state outside the repo; project key from the git common dir. |
| `adg/router.py` | Capability scoring, enrollment, escalation ceiling, quota shadow price. |
| `adg/quota.py` | Quota-exhaustion classification per agent kind; reset and window parsing. |
| `adg/cooldown.py` | Per-channel breakers and the invocation meter, shared across projects. |
| `adg/limits.py` | Hard limits, checked before the action, failing closed. |
| `adg/runtime.py` | The nine-operation adapter: `local`, `herdr` (agents run in visible panes), `mock`. |
| `adg/verify.py` | Deterministic checks and mechanical scope comparison. |
| `adg/winnow.py` | Optional [code-winnow](https://github.com/wyc79/code-winnow-skill) scanner — referenced, never vendored. Its six *judgment passes* and their merge are a separate, unwired thing: see [`winnow-passes.md`](winnow-passes.md). |
| `adg/companions.py` | Detects karpathy-guidelines and superpowers once, and declares them in `task.json`. |
| `adg/brief.py` | Human-facing gate briefs, plus the jargon lint. |
| `adg/schema.py` | Report/verdict validation against the skill's schemas. |
| `adg/prompts.py` | Injects role, paths, scope, budget — and nothing else. |
| `adg/yamlite.py` | The YAML subset used by the registry and plan files. |

## Invariants worth not breaking

- **None of this system's state is written into the working repository.** Task
  state lives in `$XDG_STATE_HOME/agent-delegation/`; the project key comes from
  `git rev-parse --git-common-dir`, which is identical from every worktree. An
  enrolled package that writes its own excluded directory is not an exception to
  this — see [`winnow-passes.md`](winnow-passes.md).
- **Attended mode never commits to your branch**, and no mode merges. The
  terminal state is a patch file or a pushed branch. There is no commit path to
  the user's branch in this codebase — checkpoint commits happen only inside
  throwaway worktrees.
- **Limits fail closed.** A missing or unparseable limit parks the task rather
  than meaning "unlimited".
- **The top model tier needs two switches**: enrollment *and* the ceiling.
- **Liveness is not success.** An agent settling `idle` proves nothing; a
  schema-valid report plus real verify output does.
- **A quota wall is a routing event, not an attempt.** Failing over to another
  seat never spends `max_attempts_per_subtask`, and never substitutes for
  escalation.
- **An honest `escalate` is routed, never punished.** An agent that stops at the
  threshold and attaches a signal must not end up worse off than one that fails
  in silence. If following `references/escalation.md` ever routes to a higher
  rung than saying nothing, the incentive has inverted and agents will learn it.
- **The protocol names no model and no runtime.** If either leaks into
  `workflows/default/`, the boundary has broken. It must also stand alone
  wherever the orchestrator unpacks it, so nothing under it may cite the repo
  root or anything else outside itself.

  This invariant belongs to the **protocol only**. The front-door skill at
  `agent-delegation/` is the opposite case by design: its whole job is to name
  the runtime, down to `--adapter herdr|local|mock`. Before the two audiences
  were split they shared a directory, and the rule read as though it bound
  both.
- **`AGENT_DELEGATION_ROLE` is a mandate, and only a role in `roles/` may carry
  it.** `classifier`, `intake` and `reporter` are this program's names for
  one-shot questions, not roles in the protocol; setting it for them made an
  agent load `PROTOCOL.md`, find no card and no task id, and answer with a `blocked`
  report instead of the verdict. `prompts.TEXT_REPLY_ROLES` is the one list, and
  `runtime.py` reads it rather than keeping a second copy.

## Tests

```bash
python3 orchestrator/tests/test_orchestrator.py
python3 orchestrator/tests/test_failover.py
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
accounting from the CLI, autonomous mode ending at an opened PR, model-rendered
briefs, quota-aware failover with per-channel cooldowns and a utilization shadow
price, and signal-routed escalation from an agent's own report.

A subtask's `capability_hint` raises the router's floors for that subtask, in one
direction only: a hint nothing enrolled can clear is logged and dropped rather
than parking the task, because the planner is guessing at difficulty and a guess
should not be able to stop a run.

Still absent: *live* quota metering (the draw above is estimated from invocation
counts, not read from a provider), weekly-cap modelling, telemetry-driven
registry recalibration, and container isolation. Rung 1 of the §6.2 ladder — "a
different model at the same tier" — is not built either: the router escalates by
raising a capability floor, which has no same-tier expression. `parallel_group`
is not read: waves are formed from `depends_on` plus disjoint write scopes and
hotspots, which is strictly safer than honouring a planner's grouping, and the
schema says so rather than promising otherwise.

### What escalates, and what only reports

One counter drives the ladder on its own: `test_stuck`, from consecutive failed
checks. `scope_overrun` is measured mechanically but does **not** escalate — it
records the out-of-scope files and forces an LLM review that a simple task would
otherwise have skipped. `edit_churn` has a configured threshold and no collector.

Everything else reaches the ladder through the agent, in `report.signals`, and
`ENTRY_RUNG` in `adg/machine.py` maps each to where it enters: `test_stuck` /
`edit_churn` / `scope_overrun` climb to a stronger model, `plan_conflict` and
`ambiguous_requirement` go straight back to the planner, `blocked_command` and
`missing_dependency` stop for a human because nothing below can help. An
`escalate` carrying no routable signal also stops for a human — `escalate` means
"a stronger model, a re-plan, or a human", the signal is what says which, and
guessing on the agent's behalf spends real money on a guess. `low_confidence` is
not routable on its own, which is D4 enforced rather than restated.

## The wave defect — every cause found, and none of them a race

The symptom was an implementer's `escalate` failing to reach the halt check, so
a wave subtask completed that should have parked. It was chased for a while as a
timing race, on the strength of an intermittent ~1-in-8 full-suite failure.

**It was not a race.** `_collect_report` identified a report by substring, and
when a subtask was named it accepted `role in name` as an *alternative* to the
subtask id. Every sibling in a wave runs as `implementer`, `PROTOCOL.md` permitted
`<stage>-<role>.json`, and `sorted()` then handed the first such file to all of
them. One substring match that cannot tell two concurrent agents apart — no
interleaving required, which is why rerunning the suite was the wrong instrument
and why it never reproduced in isolation.

Now a named subtask is identified by its id alone, and ids are unique, so no
thread can read another's report. `PROTOCOL.md` requires the id in the filename to
match. `TestWaveRaces.test_a_sibling_report_named_for_the_role_is_never_read_as_mine`
pins it deterministically: it fails on the old code every time, not one time in
eight.

The symptom was also worse than first recorded. With the escalating agent
leaving a file behind — which it normally has, since it checkpoints — the run
did not merely mis-mark a subtask: it reached `done` and emitted a patch built
on work whose own report said nothing had been verified.

**A second failure mode outlived that fix, and it was never in the
orchestrator.** The substring defect did not account for the intermittent
failure: the suite kept failing at roughly 1 run in 19, with a report on disk
that the log showed `_collect_report` never crediting. The cause was the same
defect class on the *test suite's* side of the boundary. The scripted mocks
decided which subtask they were playing by substring against the full worktree
path — `if "st-1" in cwd` — and `TempRepo` builds its sandbox with
`tempfile.mkdtemp(prefix="adg-test-")`. That prefix ends in `st-`, so whenever
the random suffix opened with a digit, every path in the run contained `st-1`
(`.../adg-test-1bnhfu9z/...`) and every implementer in the wave took the st-1
branch: alpha's report written by three hands, beta's escalate never written at
all, and a stray `alpha.py` making beta look like it had done work. The
arithmetic closes exactly: a suffix drawn from 37 characters opens with `1`
once in 37 draws, and two vulnerable tests rolled that die per run —
1 − (36/37)² ≈ 1 in 19. Contention never raised the rate; two concurrent
suites just rolled twice as many dice. Every dissected failing run showed the
orchestrator behaving correctly.

It is pinned the same way the first defect was — deterministically, not
statistically: point `TMPDIR` at a directory named like `adg-test-1w` and the
old mocks fail every run. `_tree_name()` now matches ids against the
worktree's own basename, and the full suite passes under `adg-test-1w`, `-2w`
and `-3w` alike.

**The same class was also latent on the product side.** `_collect_report`
matched a subtask's report by `id in name`. The ids today's tests use
(`st-1-alpha`) cannot collide, but the planner writes the ids, and a plan
naming siblings `st-1` and `st-11` would hand st-1 the sibling's report — the
parked-over confusion, back through another door. The match is now exact
against the `<stage>-<id>.json` contract (the stage token may be any word that
contains no `-`, so `implement-st-alpha.json` can never be claimed by a
subtask named plain `alpha`), and
`test_a_report_for_st_11_is_never_read_as_st_1s` fails on the substring code
every time.

`registry.default.yaml` now ships `max_parallel_agents: 3`. What that rests
on: three attribution defects — two in the product, one in the harness — each
fixed and pinned by a deterministic test, and the wave's concurrent invariants
(counters, worktree isolation, escalation propagation, quota parks) each held
by a test that actually opens its window. What it does not rest on: any wave
run with real agent CLIs, which the mock adapter cannot stand in for. The
first parallel deployment is still a maiden voyage, and worth watching like
one.

One thing ruled out by reading: that test's `Interleaved` hook used to wrap
`_invoke`, which *contains* the report read, so the window it documents was
never opened and it had been passing on the strength of a comment. The hook now
wraps `_collect_report`. It still passes in isolation, so the forced ordering is
not the trigger either.

The parallel tests still pin their own concurrency rather than inheriting the
registry's — `TestWaveRaces` at 3, because it plans three subtasks and a lower
cap never opens the window it exists to probe. A registry tuned back down must
not quietly turn the race tests into sequential runs that pass.
