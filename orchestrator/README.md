# delegate — MVP orchestrator

The product. It decides *what runs next, on which model, on whose seat, and
whether it is allowed to* — and keeps deciding when a provider walls or hangs.
A **workflow** tells the agents it dispatches how to behave, and which workflow
is a `--workflow` away: the bundled one in `workflows/default/` is a default,
not the only option.

**This file is what is actually built**, and it is the design document: the
reasoning for a decision lives either here or in a comment beside the code it
explains, never in a separate spec that can drift out of step unnoticed.

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
raises) the cost cap, `--tier simple|complex` states outright whether the work
needs a plan instead of paying the classifier to judge (`auto` is the default,
so `delegate` run by hand still judges for itself), `--yes` auto-approves gates
for unattended runs — merge still never happens automatically.

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
orchestrator treats it as a *routing event*, not a failure: it opens a cooldown
on that channel, re-selects the same role on another
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
window. That estimate drives the shadow price — a subscription with
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
| `adg/cli.py` | The commands themselves. `orchestrator/delegate` is six lines that call its `main`. |
| `adg/machine.py` | The state machine. Every transition lives here. |
| `adg/workflow.py` | The manifest in force: which stages are enabled, which card a role reads, which discipline a stage borrows from an installed skill. |
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
  worktrees, which are removed when the task reaches `done` and deliberately
  kept when it parks, because a parked worktree is the salvage point.
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
registry recalibration, and container isolation. Rung 1 of the ladder — "a
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

## The continuity defects underneath the wave tests

Everything above is about **attribution** — which agent's report belongs to
which subtask. A cold read found a second family sitting under it, about
**continuity**: what a finished subtask leaves behind, and what the next one is
handed. None of it was a race; each defect reproduced on the first run and every
run, and `TestSubtaskContinuity` pins them.

It took three passes, and the middle one is the part worth keeping: the first
fix was reviewed by an agent told to attack it rather than confirm it, and that
review found three regressions *inside the fix*. A second such review found two
more. Each pass was smaller than the last, and each of the tests below is
mutation-checked — the fix is reverted, and the test that names it has to fail.

### Where a subtask works, and what its diff means

Both used to be decided in several places that disagreed.

A subtask worktree is cut from the **integration branch**, not from the task's
base commit. `references/parallelism.md` always said so and
`roles/implementer.md` tells the agent that `depends_on` "tells you what already
exists"; neither held, so a second wave could not see the first wave's merged
output and a dependant began by importing a file that was not there.
`depends_on` bought ordering and nothing else. The Test Author was the quieter
casualty: its failing tests are committed into the integration worktree during
`plan`, so a wave never contained them and the checks an implementer ran were
green because the requirement was absent.

A worktree that is **reused** is brought up to that branch first (`_catch_up`).
A subtask's own checkout is a snapshot of the moment it was cut, so a rework
after `REQUEST_CHANGES` ran against a tree missing everything its siblings had
landed — the checks were evidence about an incomplete tree, and a finding citing
a sibling's file pointed at a file the agent could not see. Where the branch is
already merged this is a fast-forward; where it is not — an interrupted subtask
carrying salvage commits — a real merge is attempted and abandoned cleanly if it
will not go, because a half-merged worktree is worse than a stale one.

Its **diff base is read from that tree, on every dispatch**. Recording the
commit we asked to cut from was wrong twice over: `create_worktree` reuses an
existing branch and ignores the base it is handed, and a replan reissuing an id
rebuilt the subtask from plan.md alone, so it recorded that day's integration
tip while its checkout sat where it always had. Both produced the same symptom —
a subtask credited with every file its siblings had landed, and those files
reported as scope violations it never committed. `actual_files` accumulates
across dispatches, so moving the base forward loses nothing.

The one path that had no per-subtask base at all was the subtask working
directly in the integration worktree, which is what a wave of one does — and any
dependency chain or overlapping scope produces waves of one at any cap. It
measured against the task base, so the second subtask in a sequential run
inherited its predecessors' files and the "changed no files" guard could never
fire, since a predecessor's work always looked like its own. That guard now
applies only to a subtask that has never produced anything: with the base
following the tree, a rework that changes nothing also shows an empty diff, and
halting the run there would be a new failure mode invented as a side effect —
an implementer ignoring its findings is what the reviewer and `max_review_loops`
are for.

### Stopping part-way through a wave

The invariant is **a subtask marked `complete` has its work on the integration
branch**, and it is what makes the merge brief's file list and the delivered
patch the same account of the change. Three things broke it.

`_integrate_wave` ran only once the whole wave came back clean, so one failing
member left every finished one unmerged: `_finish_subtask` had already marked
them complete, a complete subtask never rejoins a wave, and nothing else merges
a subtask branch. The run resumed, finished the survivor, reached `done`, and
delivered a patch missing work that `actual_files` and the brief both listed as
changed — a silent loss inside a run that reported success, which is the worst
shape a defect can take here. The wave now merges what finished *before*
reporting what did not.

Merging first then introduced the opposite fault: a `Halt` out of
`_integrate_wave` outranked a sibling's `Replan`, discarding rung 3 with its
escalation bundle already written, and would have done the same to a quota
park's reopen time — the exact "the sequential and parallel paths disagreed
about the same event" failure the re-raises exist to prevent. The integration
failure is now held and raised only if nothing else in the wave decides where
the run goes next.

And a merge can fail part-way through the wave. `_reconcile` could raise out of
an unfinished merge, and the run does not always end there: a sibling's `Replan`
continues at the plan stage, where `_author_tests` does a blind `git add -A &&
git commit` and committed the conflict markers as the resolution, shipping them
in the patch. A failed merge is aborted, and the members behind it — complete,
but never merged — are reopened rather than left to be delivered as done and
absent.

### The finding hand-off

`_finish_subtask` cleared `pending_findings` wholesale, so the first subtask to
go green disarmed the rework for every other one: a reviewer that rejected
`st-1` and `st-2` saw `st-1` fixed and `st-2` re-dispatched with nothing at all,
back to the same code with the same prompt. A subtask now consumes only the
findings addressed to it; an unowned finding stays, because it is addressed to
whoever is reworking; and a verdict replaces the list wholesale, so nothing
outlives the review that raised it.

Two schema-legal shapes reached nobody. `severity: minor` is legal, so a
`REQUEST_CHANGES` carrying no blocking finding named no owner, reopened every
subtask on the no-owners fallback, and gave them nothing to go on; it is refused
as the incoherent verdict it is. And a `suggested_owner` naming no subtask in
the plan reopened `subtasks[0]` and was then filtered out of that subtask's own
brief — an unmatched owner is dropped, which makes the finding unowned and so
addressed to whoever reworks.

### Smaller inconsistencies closed on the way

Conflicting role cards are refused when the manifest **loads** rather than when
the role is first dispatched, which is what the code claimed. A stage the
manifest never mentions is no longer reported as switched off while the run loop
ran it anyway — the machine owns the graph, and silence leaves a stage as the
machine has it; the one switch is `enabled: false`, and an empty body
(`review: {}`) means "declared, nothing to say". `machine.run` walks `STAGES`
when it skips a disabled stage, because walking the manifest's declared stages
alone could never route to an undeclared one: a manifest that disabled `review`
and never mentioned `integrate` ended the run without the stage that writes the
patch.

`cli.main` resolves the workflow before every subcommand rather than only when
`--workflow` is passed, and catches `yamlite.YamlError` beside `WorkflowError`
— a manifest that exists and does not parse is the commoner typo, and it is a
sibling `ValueError` rather than a subclass. It is fatal only for the commands
that dispatch agents: `status`, `show` and `channels --clear` are how a user
works out what went wrong and unsticks a seat, and refusing to run them over an
unrelated `$AGENT_DELEGATION_WORKFLOW` would take away the tools for the
recovery.

And `delegate status` asks the breaker file rather than the `park` snapshot in
task.json, the same correction `_quota_guard` already carries: that snapshot
goes stale through `channels --clear` and through a window that simply reopened,
and reading it left `status` reporting a quota wall beside a reopen time in the
past.
