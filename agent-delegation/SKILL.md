---
name: agent-delegation
description: Use when you have already decided what the work is and want it run by several agents at once across more than one provider — dispatching a decomposition you wrote with the `delegate` CLI, answering the merge gate, checking what a run produced, or reading back why one stopped. It runs jobs; it does not plan, review or judge them, and nothing here is about how the work should be done.
---

# Agent delegation

`delegate` takes jobs you have already decomposed and runs them across whichever
enrolled agent seats can serve them — different providers, different models —
each in an isolated git worktree, metering the quota on each and keeping going
when one hits a wall.

**It is a wrapper, not a workflow.** It has no planner, no reviewer, no
classifier. Deciding what the work is, whether the result is any good, and what
to do about a job that got stuck are all yours — you have skills for that, and
duplicating them here would mean competing with you using less context than you
have.

**You run it. You are not in it.** It spawns fresh single-job agents that read a
task directory on disk; none of them sees your context and you never see theirs.

## Before anything: is there more than one seat?

Run `delegate init` and read the tier table. **If every tier resolves to one
provider, stop — do not delegate.** `init` says which case you are in, in as
many words. The whole value here is placing work across seats that empty at
different times. On one seat you are paying for subprocess indirection and
isolation you could get from your own subagents, more slowly and with no shared
prompt cache.

Measured, on a task with four independent jobs and no quota pressure. All of
these scored identically; only the bill differs:

| | delegating | one warm session | 4 cold agents, hand-rolled |
|---|---|---|---|
| Cost | ≈$3.0 | $3.10 | **$2.92** |
| Wall clock | 9.6 min | 6.8 min | **3.5 min** |

The third column is the one that should decide you. It is *this program's own
structure* — a worktree per job, cold parallel agents, disjoint scopes, merge —
as a short script with no `delegate` in it, on one provider. It beat delegating
on both axes. Isolation is not what you are paying for; the protocol each
dispatched agent loads is.

So on one seat you are buying **wall clock you could have had for free**. Pay it
when there is a second provider to route to, and not otherwise.

**Delegate when** two or more providers are enrolled *and* at least one holds:

- Your own seat is the constraint — a quota wall you are about to hit, or work
  better served by a model on another provider. This is the strong reason.
  Measured: a provider walled mid-job twice in one run, and the run still
  finished at full marks — each time the partial work was committed, the seat
  cooled, and the job continued on the other provider in the same worktree.
  Nothing you can write yourself in an afternoon does that.
- The jobs are genuinely independent and would run in parallel worktrees.
- You want the jobs isolated from each other's context on purpose — though note
  the table above: isolation alone is not worth the overhead.

**Do it yourself otherwise.** A handful of edits, a question, a conversation
still settling what is wanted — all delegate badly.

## Writing the jobs

Delegate reads a decomposition from a markdown file you write, passed with
`--plan`. One fenced YAML block, one entry per job:

```yaml
- id: st-1-buffers
  goal: Implement initialize_render — allocate and initialise the colour and depth buffers.
  file_scope: ["driver_state.cpp"]
  tier: t1
- id: st-2-raster
  goal: Implement rasterize_triangle — barycentric interpolation, z-buffer, the three interpolation modes.
  file_scope: ["raster.cpp"]
  depends_on: []
  tier: t3
```

| Field | Does |
|---|---|
| `id` | names the job, its branch and its worktree. Must be unique. |
| `goal` | what the agent is told to do. This is the whole brief — it gets no plan. |
| `file_scope` | its write boundary. **Measured, not enforced**: files touched outside it are recorded and reported back, never reverted. |
| `tier` | which capability band runs it (below). Omit and it draws the ordinary worker. |
| `depends_on` | job ids that must finish first. Ordering only. |
| `reads`, `frozen_interfaces`, `hotspots`, `acceptance` | carried into the agent's prompt and the record. |

**Disjoint `file_scope` is what buys parallelism.** Two jobs whose scopes
overlap are serialized, and a job left unscoped claims everything — which
collapses a wave to one agent. This is the single most common way to get no
parallelism while thinking you asked for it.

## Tiers, and which provider serves them

A job asks for a band; the registry decides the model and the seat. This is the
one routing decision you make:

| Tier | Model | Default provider | For |
|---|---|---|---|
| `t1` | `fast-cheap` | cursor-seat | mechanical, high volume, low judgement |
| `t2` | `balanced-coder` | cursor-seat | the workhorse — most jobs |
| `t3` | `opus-class-strong` | claude-seat | the hard one in the batch |

The provider column is this deployment's; `delegate init` prints the live table.
It is a **preference, not a pin** — a cooled or drawn-down seat still yields to
another, which is the entire point of the program.

Mixing tiers within one batch is normal and is where this earns its cost: the
hard job draws the strong model on one provider while the easy ones run
concurrently on another.

## Running it

```bash
delegate init                                  # which seat serves which tier
delegate run "<label>" --plan jobs.md          # dispatch
delegate status                                # one line per task
delegate show --brief                          # the merge-gate brief
delegate approve --note "..."                  # answer the gate
delegate resume --when-open                    # sleep out a quota window, then continue
delegate channels --clear <seat>               # release a cooldown by hand
```

Run from the repository being changed, by absolute path; `--repo <path>`
overrides. Useful flags: `--no-panes` (trades herdr's visible agent panes for
cost accounting, so `max_cost_usd` can bind), `--max-cost N` (lowers, never
raises), `--dry-run`, `--yes`.

**Exit code 1 does not mean it failed.** Only `done` exits 0; parked, waiting on
quota, declined and crashed all exit 1. Read the status.

## Reading what comes back

**A job that stopped hands you everything it knew** — its `signals` carry the
type, the detail, the evidence and what it already tried. Nothing here will
retry it on a dearer model or rewrite your plan; that decision is yours, and
`superpowers:subagent-driven-development` has the procedure for making it. The
worktree and its commits are left in place for whatever you decide.

**Nothing reviewed the work.** The merge-gate brief says so plainly. What you
get is a row per job — the files it touched, and which of them fell outside the
`file_scope` you gave it — plus the output of your checks and what each seat
cost. Judging the result against what you asked for is the step after this one:
with your own review skills, or by running a quality pass over the patch.
`delegate` runs none of its own, and one it ran would be a worse version of
yours behind a subprocess boundary.

**The patch is the deliverable.** Attended mode writes `integrate.patch` and
commits nothing to your branch. `--mode autonomous` pushes the task branch to
`origin` instead and stops there — it opens no pull request, because proposing
work that nothing reviewed is your call, not the dispatcher's.

## Checks

Optional `.adg.yaml` in the repository being worked on. These are the checks
**you** decide are ground truth; nothing here has an opinion about what a
passing change looks like:

```yaml
fast: ["make build"]          # after every attempt on a job
slow: ["make test-all"]       # at stage boundaries
ignore: ["build/*", "*.o"]    # generated paths, excluded from scope accounting
hotspots: ["schema.sql"]      # files no two jobs may hold at once
```

A check that always exits 0 is a check that always passes — make sure yours can
fail. `ignore` matters more than it looks: without it, agents are charged with
creating object files they did not write.
