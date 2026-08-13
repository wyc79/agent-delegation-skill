---
name: agent-delegation
description: Use when jobs you have already decomposed should run as parallel agents across more than one provider — dispatching them with the `delegate` CLI, surviving a quota wall or rate limit mid-job by failing over to another seat, running each job in its own git worktree, answering the merge gate, or reading back what a run produced and why one stopped. Trigger on delegate, multi-provider, quota exhausted, rate limited, seat, failover, parallel agents, parallel worktrees, jobs.md, merge gate. It runs jobs; it does not plan, review or judge them, and nothing here is about how the work should be done.
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
provider, think hard before delegating.** `init` says which case you are in, in
as many words. The whole value here is placing work across seats that empty at
different times.

**If `delegate` is not on PATH, go straight to `references/one-seat.md`.** It is
the pattern below by hand — git worktrees and your own agents — and it needs
nothing from this repository. Same if `init` runs but enrolls a single provider.

**On one seat, prefer your own parallel agents.** Measured head to head on the
same jobs and the same model, dispatching through `delegate` cost roughly double
a git worktree per job (`evidence/RESULTS.md`, arms D and F). What it buys is a
merge gate, a scope report and caps — worth the overhead only if you were going
to read them.

The exception is a seat you actually run into walls. When one lands mid-job
`delegate` commits what the agent had, records when the seat reopens, and
resumes from that commit; doing it yourself, the CLI dies and the job restarts
from nothing. If that is your situation, pay the overhead on one seat too.

**On one seat `tier` also stops meaning anything.** Nothing here pins a model, so
a band only routes differently when your *seats* expose different ones — and a
job you marked `t1` to keep it cheap runs on whatever that single seat's CLI is
configured with.

**Delegate when** two or more providers are enrolled *and* at least one holds:

- Your own seat is the constraint — a quota wall you are about to hit, or work
  better served by a model on another provider. This is the strong reason.
  Measured: two mid-run walls, still full marks (`evidence/RESULTS.md`, arm E2).
  Nothing you can write yourself in an afternoon does that.
- The jobs are genuinely independent and would run in parallel worktrees.
- You want the jobs isolated from each other's context on purpose — though note
  that isolation alone is not worth the overhead: you can have it from your own
  worktrees, without this.

**Do it yourself otherwise.** A handful of edits, a question, a conversation
still settling what is wanted — all delegate badly.

### On one seat, do this instead

The alternative is not "give up on parallelism" — it is the pattern that beats
delegating on a single seat, run with tools you already have.

**→ `references/one-seat.md`** has it in full: the exact git commands, the
prompt shape, why `superpowers:subagent-driven-development`'s "never dispatch
implementation subagents in parallel" does not apply once each agent has its own
worktree, and what you give up by not paying for the gate.

Read it when `init` showed one provider and you have independent jobs. Skip it
otherwise — on two seats this is the worse choice.

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
| `acceptance` | what counts as done. Read the warning below before leaving it out. |
| `required_checks` | shell commands that are **this job's** pass/fail, run in its worktree after every attempt. All must exit 0. Omit and nothing changes. |
| `briefing` | not a job field — a separate block at the top of the plan. Paths every agent must read first. See below. |
| `reads`, `frozen_interfaces`, `hotspots` | carried into the agent's prompt and the record. |

**How big the job is, is part of the brief — and if you leave it out an agent
will decide it for you.** Told to "implement `clip_triangle`", one agent built
two of the six clipping planes and **passed the whole test suite**; the run that
spelled out "the six clip-space faces" got all six. Nothing was careless about
it — the first was never told where the line was, so it drew one, and every
signal available said it had succeeded. (`evidence/RESULTS.md` has the pair.)

So: if a job could be satisfied by something smaller than you have in mind, the
checks will not tell you — they pass, and a scope report only covers files, not
how much of the file's job got done. Say how much you want, in `goal` where it
defines the work and in `acceptance` where it is the bar, or take whichever
reading the agent picks.

**An agent inherits the repository and nothing from you.** It starts in a fresh
process, in a worktree, and reads whatever agent-config file its own CLI looks
for — `CLAUDE.md`, `AGENTS.md`, `.cursor/rules`. Everything on your side of the
boundary stays there: the skill you are following, the convention you just
agreed, the reason you are doing it this way. Work comes back correct and
foreign.

**And the config file only travels if git tracks it.** A worktree checks out
tracked files only, so an untracked or ignored `CLAUDE.md` is read by *your*
session, sits in your checkout, and is absent from every job's. `delegate init`
audits this per seat and says which ones are running blind — it is the one case
where looking in the repo and looking in the worktree give different answers.

So a discipline you want followed has three places it can live, and none of them
is your session: **committed** to the repo where the agents' own CLIs read it,
named in the plan's **`briefing:`** so every prompt points at it, or checked at
the **`gate:`** on the way out. Otherwise it does not happen.

```yaml
briefing: [docs/conventions.md, docs/testing.md]
```

A separate block from the job list, paths relative to the repo. Every job's
prompt lists them as required reading before it starts; the paths are validated
before anything is dispatched, so a wrong one costs you a message rather than N
confused agents. Paths, not contents — a pasted copy goes stale.

**They must be committed, and that is checked.** Same worktree rule as
`CLAUDE.md`: untracked is refused exactly like missing — `git add` it and
re-run.

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

**The band is a preference under failover too, and that is the sharp edge.** A
replacement is asked for at least the walled model's strength, but when no seat
left can hold that floor it is dropped and the job continues on whatever is best
remaining — **weaker than what you asked for.** Know where that shows: the run
log says *"no seat left at X's strength — continuing on Y, which is weaker."*
The merge brief does not. It reports spend per model, so the demotion is
inferable from the rows and stated by nothing.

The case that bites is a band only one seat can serve — `t3` where a single
provider exposes the strong model. There is then no equal replacement by
construction, so the demotion is the default outcome rather than an edge case.
If a job is `t3` because a cheaper model gets it *silently* wrong, check the log
before you trust the result.

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
retry it on a dearer model or rewrite your plan; that decision is yours —
`superpowers:subagent-driven-development` has a procedure for it if you have
that skill, and your own judgement about retrying, re-scoping or dropping the
job serves if you do not. The worktree and its commits are left in place for
whatever you decide.

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
gate: ["make lint"]           # once over the assembled change, before the brief
ignore: ["build/*", "*.o"]    # generated paths, excluded from scope accounting
hotspots: ["schema.sql"]      # files no two jobs may hold at once
```

**`gate:` is the last place a convention can be enforced.** It runs once against
the integration result, after every job is merged and before the brief is
written — so it is where a house style the agents could not read gets checked on
the way out. A non-zero exit **does not reject**: it is reported in the brief,
named as gate-stage, and you decide. Nothing here judges the work.

**These run for every job, which bounds what they can be.** A project-wide check
executes against a tree where this job's siblings are still stubs, so on work
that only functions assembled it is usually limited to "does it compile" — which
means a job can finish, pass, and be wrong in a way nothing looked for.

`required_checks` on a job is the other half: its own commands, run in its own
worktree after every attempt, straight after the `fast` ones. All must exit 0 or
the attempt failed, and the failure is recorded **against that job** rather than
against the batch. Use it for what only this job can assert — a probe over the
one function it owns, a fixture only its files satisfy.

Two things to know. The commands are never shown to the agent, deliberately:
naming a check in a prompt buys a turn spent reading it, so if the job should
also self-check before finishing, say that in `goal`. And a job that declares
none is reported as *"none declared"* in the merge brief, not as a pass — the
distinction is the point.

A check that always exits 0 is a check that always passes — make sure yours can
fail. `ignore` matters more than it looks: without it, agents are charged with
creating object files they did not write.
