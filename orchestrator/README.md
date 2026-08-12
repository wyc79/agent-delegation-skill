# delegate — the dispatcher

**It runs jobs somebody else decomposed, across seats that empty at different
times.** It decides which seat serves each job, meters the quota on each, moves
work when one walls, isolates every job in its own worktree, and merges what
comes back. It does not decide what the work is, and it does not judge the
result.

That is a narrower product than this repo used to hold. It used to run a role
protocol — a planner decomposed, a test author wrote from requirements, a
reviewer ruled — and that protocol was measured against a caller doing the same
work in one warm context. It cost **4.2x the money, 4.6x the wall clock and 4.4x
the tokens for an identical result**. A caller that already has planning and
review skills does not need worse copies of them behind a subprocess boundary.
What no model release makes redundant is the seat that empties at 3pm, so that
is what is left.

**This file is what is actually built**, and it is the design document: the
reasoning for a decision lives either here or in a comment beside the code it
explains, never in a separate spec that can drift out of step unnoticed.

**`adg`** is short for *agent delegation*. It is the Python package name, and the
prefix on everything this tool creates so it is greppable and never mistaken for
your own: branches `adg/<task-id>/<job>`, worktrees under
`.adg-worktrees/<project-key>/`, and project config `.adg.yaml`.

Python 3.9+, standard library only. No install step, no dependencies (the YAML
subset the project uses is parsed by `adg/yamlite.py`, because PyYAML is not in
the stdlib and requiring an install would be a worse trade).

## The graph

    STAGES = ["implement", "integrate", "done"]

`implement` forms waves of jobs whose write scopes are disjoint, runs each in its
own worktree on its own branch, and retries a job on its own seat while the
caller's attempt budget allows. `integrate` merges each branch into the task
branch, verifying after each so the branch is green at every step, and writes the
patch.

A manifest (`workflows/default/workflow.yaml`) says which of those run and which
instructions the agent it dispatches reads. **It cannot invent a stage** — the
machine owns the graph, and a manifest naming a stage the machine does not have
would read like policy and route nothing. It also cannot declare *method*: a
caller with its own protocol repoints `--workflow` at it, but nothing in a
manifest injects working style into a prompt.

## Jobs

The caller writes them; `--plan <file>` lands the file where the machine looks.
`workflows/default/schemas/subtask.schema.json` is the field list. The two that
decide behaviour:

- **`file_scope`** — the write boundary, and what makes concurrency safe. Two
  jobs with overlapping scopes are serialized; an unscoped job claims
  everything. Measured, never enforced: files touched outside it are recorded
  and reported, not reverted, because nothing here reverts a hunk and a prompt
  that threatens otherwise is asking to be found out.
- **`tier`** — `t1`, `t2` or `t3`, the capability band. Advisory: a band nothing
  enrolled serves is logged and demoted rather than parking the run, because a
  caller judging difficulty from outside must not be able to stop the work.

## Routing: tiers, not roles

A job names a band; the registry says which model serves it and which seat
prefers it.

| Tier | Model | Seat |
|---|---|---|
| `t1` | `fast-cheap` | cursor-seat |
| `t2` | `balanced-coder` | cursor-seat |
| `t3` | `opus-class-strong` | claude-seat |

**One model per band, and that is load-bearing.** While `balanced-coder` and
`opus-class-strong` were both t2, `tier: t2` named two models, the score took
the cheaper every time, and "the default provider for t2" had no answer.

Selection is an **exact band, never a floor**. A floor would be useless: the
worker profile ranks the workhorse above the cheap model on every axis it
weighs, so `tier: t1` with a floor resolves to t2 and the cheap seat never runs.

`prefers:` on a channel decides which seat serves a model that more than one
exposes. It is load-bearing for t2, where both do: without it the scores tie —
two subscription seats with headroom both price at ~0 — and the tie falls to the
alphabet, so every job lands on `claude-seat` and a two-provider deployment
quietly runs as one. The bonus is smaller than the gap between any two distinct
capability scores, so it breaks ties and can never buy a weaker model a job, and
a cooled or drawn-down seat still yields.

## What it will not do

Each of these was built, measured and removed. They are the caller's, and the
caller has skills for them:

- **Plan.** No planner. `--plan` is how a decomposition arrives.
- **Review.** No reviewer, no verdicts, no findings hand-off, and no quality
  pass of any other kind. The merge brief says plainly that nothing reviewed
  the work. A code-winnow chaff scan ran at the merge gate until it went the
  same way: choosing which pass runs over a change is the caller's, and a
  caller that wants one runs it over the patch — or dispatches its passes
  *through* here as jobs, which is the relationship this is built for.
- **Escalate.** No ladder. A job reporting `escalate` halts and hands back its
  `signals` whole — type, detail, evidence, what it already tried — because the
  caller wrote the decomposition and is the only thing that can revise it.
  `superpowers:subagent-driven-development` already has that procedure, and is
  explicit that you must never "force the same model to retry without changes".
- **Choose method.** No companion-skill detection, no method injected into any
  prompt. Picking how to work is the caller's.

What it keeps is what only it can do: worktree isolation, scope measurement,
dependency waves, quota metering, breakers, cross-provider failover, checkpoint
salvage, integration, and the merge gate.

The test that holds this is `test_a_run_dispatches_nothing_but_the_work`: a
clean run's `delegation_history` contains implementer turns and nothing else.
An agent call is money, so a role that creeps back in is a bill the caller did
not ask for.

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
| `adg/workflow.py` | The manifest in force: which stages are enabled, and which card the role each dispatches reads. |
| `adg/store.py` | Task state outside the repo; project key from the git common dir. |
| `adg/router.py` | Capability scoring, enrollment, escalation ceiling, quota shadow price. |
| `adg/quota.py` | Quota-exhaustion classification per agent kind; reset and window parsing. |
| `adg/cooldown.py` | Per-channel breakers and the invocation meter, shared across projects. |
| `adg/limits.py` | Hard limits, checked before the action, failing closed. |
| `adg/runtime.py` | The nine-operation adapter: `local`, `herdr` (agents run in visible panes), `mock`. |
| `adg/verify.py` | The caller's checks, run and recorded; mechanical scope comparison. |
| `adg/brief.py` | Human-facing gate briefs, plus the jargon lint. |
| `adg/schema.py` | Report validation against the bundled schemas. |
| `adg/prompts.py` | Injects role, paths, scope, budget — and nothing else. |
| `adg/yamlite.py` | The YAML subset used by the registry and plan files. |

## Invariants worth not breaking

- **None of this system's state is written into the working repository.** Task
  state lives in `$XDG_STATE_HOME/agent-delegation/`; the project key comes from
  `git rev-parse --git-common-dir`, which is identical from every worktree.
- **Attended mode never commits to your branch**, and no mode merges or opens a
  pull request. The terminal state is a patch file or a pushed branch, and
  pushing is as far outward as a dispatcher goes: proposing work that nothing
  here reviewed is the caller's decision. There is no commit path to
  the user's branch in this codebase — checkpoint commits happen only inside
  worktrees, which are removed when the task reaches `done` and deliberately
  kept when it parks, because a parked worktree is the salvage point.
- **Limits fail closed.** A missing or unparseable limit parks the task rather
  than meaning "unlimited".
- **The top model tier needs two switches**: `enrolled: true` *and* the
  ceiling. Either alone leaves it unreachable.
- **Liveness is not success.** An agent settling `idle` proves nothing; a
  schema-valid report plus real verify output does.
- **A quota wall is a routing event, not an attempt.** Failing over to another
  seat never spends `max_attempts_per_subtask`: an empty seat is not the
  approach's fault.
- **An honest `escalate` reaches the caller whole.** An agent that stops and
  attaches a signal must not end up worse off than one that fails in silence.
  Nothing here summarises a signal away, rewrites the plan, or retries on a
  dearer model — the caller decides, and it can only decide on what it is given.
- **No agent process outlives the run that started it.** Every session is
  tracked and drained in a `finally`, and SIGTERM is turned into the interrupt
  that triggers it. An agent is a billed subprocess; leaking one costs money
  after the run has stopped.
- **The protocol names no model and no runtime.** If either leaks into
  `workflows/default/`, the boundary has broken. It must also stand alone
  wherever the orchestrator unpacks it, so nothing under it may cite the repo
  root or anything else outside itself.

  This invariant belongs to the **protocol only**. The front-door skill at
  `agent-delegation/` is the opposite case by design: its whole job is to name
  the runtime, down to `--adapter herdr|local|mock`. Before the two audiences
  were split they shared a directory, and the rule read as though it bound
  both.
- **A manifest may not invent a stage.** `machine.STAGES` is the graph. A
  manifest enables, disables, or repoints what is already there; naming
  something else would read like policy and route nothing.

## Tests

```bash
python3 orchestrator/tests/test_orchestrator.py
python3 orchestrator/tests/test_failover.py
```

262 tests, none of which spend a token: the end-to-end ones drive the real state
machine over a real git repository with a scripted adapter, so dispatch,
isolated worktrees, waves, verify, failover and integration are all exercised
for real.

The suite was 363 before the protocol came out. The drop is the point — the
deleted tests asserted a product this is not, and keeping them passing would
have meant keeping the code they pinned.

No agent CLI has to be installed for the suite to pass. Tests that exercise
launch routing stub `can_run`, the single seam that decides whether a kind is
usable, rather than depending on a vendor binary being on PATH.

## Use

```bash
orchestrator/delegate init                          # which seat serves which tier
orchestrator/delegate run "add subtract" --plan jobs.md
orchestrator/delegate status                        # tasks for this project
orchestrator/delegate show --brief                  # the merge-gate brief
orchestrator/delegate approve --note "..."          # answer it
orchestrator/delegate resume --id T-001             # continue a parked task
```

Flags worth knowing: `--plan FILE` supplies the jobs; `--dry-run` drives the
machine with no agents; `--adapter local|herdr|mock` overrides runtime
selection; `--no-panes` trades herdr's visible panes back for cost accounting
(pane sessions report no cost, so `max_cost_usd` cannot bind while they are on);
`--max-cost N` lowers, never raises; `--yes` auto-approves gates for unattended
runs — merge still never happens automatically.

Exit code 1 does not mean failure: only `done` exits 0, and parked, waiting on
quota, declined and crashed all exit 1. Read the status.

## Project config

Optional `.adg.yaml` in the repository being worked on:

```yaml
fast: ["python3 -m pytest -q"]        # after every attempt on a job
slow: ["python3 -m pytest -q --slow"] # stage boundaries only
ignore: ["build/*", "*.o"]            # generated paths, excluded from scope accounting
hotspots: ["src/schema.sql"]          # files no two jobs may hold at once
```

These are the caller's checks, and they are the only thing here that decides
whether a job's work is acceptable. No config is legal — checks are then
reported as *not run* rather than faked. A check that cannot fail is not a
check: `slow` commands that always exit 0 will report every job as passing.
