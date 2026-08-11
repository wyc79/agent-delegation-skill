# Agent delegation

**`delegate` runs jobs you have already decomposed across seats that empty at
different times** — different providers, different models — each in an isolated
git worktree, metering the quota on each and moving work when one hits a wall.

It is a **wrapper, not a workflow**. There is no planner, no reviewer, no
classifier, and no quality pass of its own. A skill or agent that has already
decided what the work is hands it a decomposition; it decides which seat serves
each job, runs them, and merges what comes back. Judging the result is the
caller's, and so is deciding what to do about a job that got stuck. A clean run
dispatches one kind of agent — the one doing a job you wrote — and a test
fails if any other appears.

It is a command line program, not a model and not a prompt. It spawns agent CLIs
(`claude`, `codex`, `cursor-agent`, `gemini` today) as subprocesses, hands each
one a job and a task directory on disk, and decides what runs where. The state
graph and its guards are authored code; LLMs choose among edges the graph
already offers, through schema-validated outputs.

Two pieces ship here:

- **[`orchestrator/`](orchestrator/) — the runtime.** Seat registry and
  enrollment, tier-based routing, usage metering and a quota shadow price,
  cooldown breakers, failover with checkpoint reuse, isolated worktrees and
  dependency waves, and three adapters: `local` (plain subprocesses), `herdr`
  (the same agents in visible panes), and `mock` (scripted, for the tests).
- **[`agent-delegation/SKILL.md`](agent-delegation/SKILL.md) — the front door.**
  One file, for the agent deciding whether to reach for this: when it is worth
  the cost, how to write the jobs, and how to read what comes back.

[`orchestrator/README.md`](orchestrator/README.md) is the reference for what is
actually built and why each decision went the way it did.

**The relationship to run in your head is which way the call goes.** A skill
like superpowers or code-winnow does not get *processed* by this. It calls this,
when it has jobs that would run in parallel and more than one provider to run
them on, and gets back the work plus a record of what each seat did. Anything
that looks like this program deciding what the work is, or whether it is any
good, is a bug in the boundary rather than a feature.

## Why this is a wrapper and not a workflow

This repo used to run a role protocol: a planner decomposed, a test author wrote
tests from the requirements blind to the implementation, a reviewer ruled
against the plan. Two measurements retired it.

The first was an ablation on a fixture task, where a stock agent one-shot the
work. The second was harder and decisive. On a real assignment — implementing
four stages of a software rasterizer, graded by the course's own script against
26 reference images — the protocol and a caller using parallel subagents in one
warm context **both scored 31/31**. The protocol cost **4.2x the money, 4.6x the
wall clock and 4.4x the tokens** to get there.

A claim whose baseline improves with every model release is not worth defending,
and a caller that already has planning and review skills does not need worse
copies of them behind a subprocess boundary. So they were removed rather than
defended.

What does not improve with model releases is the seat that empties at 3pm while
another sits idle. That gets *worse*, because stronger models are metered
harder. Routing across providers, metering the draw on each, and moving a
half-finished job onto another seat is the part no model release makes
redundant, and it is what this is now.

Correctness is still checked, but as a **control rather than a claim**: if
routing a task across two providers costs correctness, that is a bug in the
routing. The negative control matters as much as the positive one — an ordinary
crash must consume an attempt and trigger no hop, because a router that reroutes
on any error converts real bugs into silent provider changes, and no happy-path
demo would ever show it.

## How a person uses it

**You talk to your own agent. That agent decides whether to delegate, and if it
does, it writes the jobs and runs `delegate`.**

Not: `delegate` launches an orchestrator agent that talks to you. That was
considered and rejected — it puts the orchestrator *inside* a provider, so the
3pm quota wall kills the very thing whose job is to route around the 3pm quota
wall. State lives on disk instead, which makes the human the recovery path: if
your provider dies, open another agent, point it at the same repo, and continue.

```text
you  →  your agent    "implement the four driver stages"
        your agent    (decides the decomposition itself, writes jobs.md)
        your agent    delegate run "driver stages" --plan jobs.md
        delegate      4 jobs across 2 providers, isolated worktrees, merged
        your agent    reads what each job touched and what the checks said
        your agent    reviews the result itself, then tells you
```

**The first question is whether there are two seats.** `delegate init` prints the
tier-to-provider table and then answers it outright. If everything resolves to
one provider, delegating buys subprocess indirection and nothing else — do the
work with your own subagents instead.

**Nothing reviews the work.** The merge-gate brief says that plainly rather than
letting "complete, checks pass" read as "something looked at this". What you get
is a row per job — which files it touched and which of them fell outside the
scope it was given — plus the output of the checks you configured and what each
seat cost. Files outside scope are recorded, never reverted.

**Exit code 1 does not mean it failed.** `run` and `resume` exit 0 only when the
task reached `done`; parked, waiting on quota, declined and crashed all exit 1.
Read the status, not the exit code.

## Install

Two halves, and the skill alone does nothing.

**1. The runtime.** Clone this repo. `orchestrator/delegate` is Python 3.9+,
standard library only — no install step and no dependencies. It runs **from the
repo you want changed**, not from this one, so give it an absolute path.

```bash
git clone https://github.com/wyc79/agent-delegation-skill.git
export ADG="$PWD/agent-delegation-skill/orchestrator/delegate"

cd /path/to/your/repo          # the repo to be changed
$ADG init                      # what's detected, and which seat serves which tier
$ADG run "add the endpoints" --plan jobs.md
```

`init` prints the project key, the state directory, the tier-to-provider table
and whether each seat's agent CLI is actually installed. Read that table first:
**if every tier resolves to one provider, do not delegate** — you would be
paying for subprocess indirection and nothing else. It writes nothing at all;
`registry.default.yaml` and `.adg.yaml` are the whole of the configuration, and
both are edited by hand. (`--repo` overrides the default of the current
directory.)

**2. The front door.** Copy `agent-delegation/` into your agent's skills
directory, so the agent you talk to knows when delegating is worth it, how to
write the jobs, and how to read what comes back. **This is not sufficient on its own** — the skill
describes a program, and without the clone above that program is not on disk.
Installing it without the CLI leaves an agent with instructions it cannot follow.

No model configuration is needed for either half. Agents never choose models;
they receive a job and read artifacts. Routing belongs to the runtime, and
`registry.default.yaml` is the file it reads (`--registry` points at another).

## The commands

Run them from the repository being changed. `delegate` identifies the project
from `git rev-parse --git-common-dir`, so the working directory matters.

| Command | Does |
|---|---|
| `init` | Detected seats, which one serves each tier, verify config. Writes nothing. |
| `run <request> --plan FILE` | Create a task from the jobs in `FILE` and run them. |
| `resume [--id] [--stage] [--when-open]` | Continue a parked task. `--when-open` sleeps out a quota window first. |
| `approve [--id] --note "…"` | Answer a waiting gate yes, then continue the run. |
| `reject [--id] --note "…"` | Answer it no. Records the decision and stops. |
| `status` | One line per task for this project, ending in its task directory. |
| `show [--id] [--brief]` | `--brief` prints the gate brief, already written for a human. |
| `channels [--clear NAME]` | Per-seat cooldowns and estimated quota draw. |

`run` also takes `--id`, `--mode attended|autonomous`,
`--adapter herdr|local|mock`, `--no-panes`, `--max-cost N`, `--dry-run` and
`--yes`. Three worth understanding before use: `--plan FILE` is not optional in
practice — a task with no jobs does nothing; `--dry-run` walks the state machine
with no agents and no spend, which is the cheapest way to see the shape of a
run; and `--yes` auto-approves **every** gate for the whole run, which exists
for unattended runs and should never be reached for because a gate is
inconvenient.

Two modes, and the difference is who answers the gates:

| Mode | Flag | Gates | Ends at |
|---|---|---|---|
| **Attended** (default) | — | Park for you | A patch file you apply yourself |
| **Autonomous** | `--mode autonomous --yes` | Auto-approved | An opened PR |

**Merge is never automatic in either mode**, and there is no commit path to your
branch in this codebase — checkpoint commits happen only inside throwaway
worktrees.

Worktrees live under `.adg-worktrees/<project-key>/` and are removed when the
task reaches `done` — **only** then. A parked or crashed task keeps its
worktree, because that is the salvage point a human resumes from and where
`_salvage` commits an interrupted agent's work before a failover hop. The
branch outlives the directory either way, so nothing is lost by the reaping.

## Where the state lives

Agents never message each other. Everything moves through files in a task
directory that lives **outside the repository**, so orchestration state never
touches your git history and every worktree shares one copy:

```text
~/.local/state/agent-delegation/projects/<project-key>/tasks/<task-id>/
```

`<project-key>` is derived from `git rev-parse --git-common-dir`, which resolves
identically from every worktree of a repo — so any agent, anywhere, finds the
same task state with no configuration. The runtime also injects
`$AGENT_DELEGATION_TASK_DIR` so the normal path is a single env read.

**This is what makes cross-provider handoff possible at all.** Nobody passes a
transcript. A cursor worker picks up a claude worker's work by reading
`plan.md`, and a replacement agent after a failover reads what its predecessor
committed. Without a deterministic shared path and a validated report shape there
is no handoff, and multi-provider delegation stops being a thing that can happen.

| File | Holds | Authority |
|---|---|---|
| `task.md` | The request, as the caller wrote it | **What was asked** — outranks everything |
| `plan.md` | One YAML block per job — supplied by the caller with `--plan` | **What was asked for** |
| `brief.md` | The last gate brief, written for a human | — |
| `reports/*.json` | One schema-validated handoff per agent per stage | Evidence |
| `verify/` | Build, test and lint output by run id | Evidence |
| `agent-logs/*.log` | Each agent's prompt and its output | Evidence |
| `task.json` | Status, spend, gate history, which model ran what | Runtime-owned; never edit it |

The repository keeps source, tests, and durable documentation. None of this
system's state — no `.task/`, not even ignored — is ever written into it.

## What's in the repo

```text
agent-delegation/
└── SKILL.md              THE FRONT DOOR. For the agent a human is talking to:
                          when to call `delegate`, which command comes next,
                          how to answer a parked gate. No methodology.

orchestrator/
├── delegate              the entry point
├── adg/                  the runtime: state machine, router, quota, cooldowns,
│                         adapters, verification, prompts  (see its README)
├── tests/                two suites, no tokens spent
└── workflows/default/    the contract DISPATCHED agents follow — what a
                          worktree is, what the scope boundary means, what to
                          write on the way out. Orchestrator-internal, not
                          installed, and repointable with `--workflow`.
    ├── PROTOCOL.md       entry point, read before any role card. No
    │                     frontmatter: it is read by absolute path rather than
    │                     installed, and two files claiming the same skill name
    │                     is a collision a loader resolves arbitrarily
    ├── workflow.yaml     the stage manifest
    ├── roles/            one card per role; an agent reads exactly one
    ├── references/       depth loaded only when its trigger fires: task-dir,
    │                     parallelism, handover, scratch-files
    └── schemas/          JSON Schema for reports and job blocks

registry.default.yaml     model capability scores, the one job profile,
                          channels (seats), and policy — the only file that
                          names models
```

The split between those two directories is the point, and it was one directory
until recently. The front-door skill's whole job is to name the runtime, down to
`--adapter herdr|local|mock`. The protocol's whole job is to name **no** model
and no runtime, so that it stands alone wherever the orchestrator unpacks it.
Merged, every implementer was handed a document telling it to call `delegate` —
the wrong document, and an invitation to recurse.

**Progressive disclosure is a hard constraint on the protocol, not a style**, and
the budget that matters is per agent rather than per repo. `PROTOCOL.md` is read
by every dispatched agent, so a line there costs once per job in the wave, and a
reference loads only when its stated trigger fires ("a previous agent held this
job and stopped part-way → read `references/handover.md`"). Moving those rows out
of the shared entry point lengthened the cards and made every individual job
cheaper.

## The graph, and how a job finds a seat

```text
implement → waves of jobs whose write scopes are disjoint, each in its own
            worktree on its own branch; checks after every attempt
integrate → merge each branch into the task branch, re-verifying at each step,
            then write the patch
```

Two stages, because the caller owns the rest. There is deliberately no `verify`
stage either: checks run *inside* `implement` rather than beside it.

A job names a **tier**; the registry says which model serves that band and which
seat prefers it:

| Tier | Model | Seat | For |
|---|---|---|---|
| `t1` | `fast-cheap` | cursor-seat | mechanical, high volume |
| `t2` | `balanced-coder` | cursor-seat | the workhorse — most jobs |
| `t3` | `opus-class-strong` | claude-seat | the hard one in the batch |

Swapping providers is an edit to `registry.default.yaml` and nowhere else.

**One model per band is load-bearing.** While two models shared t2, `tier: t2`
named both, the score took the cheaper every time, and "the default provider for
t2" had no answer. And selection is an exact band, never a floor: with a floor,
`tier: t1` resolves to t2 — the workhorse outranks the cheap model on every axis
the profile weighs — and the cheap seat never runs at all.

The seat preference is a preference, not a pin. A cooled or drawn-down seat
still yields to another, which is the entire point of the program. Underneath it
the shadow price keeps working: a subscription with headroom costs ~0, the same
seat at 90% drawn prices itself above a metered key, and `reserve_for` withholds
a declared share — unless that would leave a job with nowhere to go, in which
case it gets the seat anyway and the demotion is logged.

The shipped registry reserves 30% of the strong seat for the **integrator**,
which is the call that arrives last and cannot be deferred: a wave that draws
the seat down and then cannot pay for its own merge conflict strands finished
work on unmerged branches. A role named there that nothing dispatches reserves
headroom nobody can claim, so a test refuses one.

## Design choices worth knowing before you adopt it

- **A quota wall is a routing event, not an attempt.** `max_attempts_per_subtask`
  bounds how many times an *approach* may be retried; an empty seat is not the
  approach's fault. The hop happens below the attempt loop, reuses the same
  worktree so every checkpoint commit survives, and tells the replacement what it
  inherited.
- **Hopping and cooling are two decisions.** A quota wall hops *and* cools the
  seat. A timeout hops and never cools — nothing about a call that did not come
  back says the seat is out, and cooling on that evidence hides a working
  provider. An ordinary crash does neither.
- **Utilization is estimated, and says so.** No provider exposes a counter, so
  the runtime counts invocations against a declared `est_capacity` and prices the
  seat by how drawn it is. A subscription with headroom costs ~0; the same seat at
  90% prices itself above a metered key, so cost-sensitive roles drift to the
  emptier seat before anything hits a wall.
- **Limits fail closed.** A missing or unparseable limit parks the task rather
  than meaning "unlimited". The top model tier needs two switches — enrollment
  *and* the escalation ceiling — so reaching it takes two deliberate edits.
- **A stuck job is handed back, not guessed at.** An agent reporting `escalate`
  ends its job and its `signals` reach the caller whole — type, detail,
  evidence, what it already tried. Nothing here summarises them away, retries on
  a dearer model, or rewrites the plan. An honest `escalate` must never leave an
  agent worse off than failing in silence, or agents learn that.
- **Parallelism is pessimistic.** Disjoint declared write scopes in separate
  worktrees, with unmergeable files locked to one agent. A merge conflict in a
  binary scene has no good resolution.
- **Nothing here reverts a hunk.** Scope is *measured*, and files outside it are
  recorded and reported. A prompt that threatens an automatic revert would be
  found out by the first agent that tests it.
- **No agent process outlives the run.** Sessions are tracked and drained in a
  `finally`, and SIGTERM is turned into the interrupt that triggers it — an
  agent is a billed subprocess, and a leaked one keeps costing after the run has
  stopped.

## Where it sits beside other skills

It does not compete with them, and that is deliberate. The caller brings the
judgement:

- **[superpowers](https://github.com/obra/superpowers)** decides what the work
  is (`brainstorming`, `writing-plans`), whether to parallelise it at all
  (`dispatching-parallel-agents`), and what to do about a job that came back
  stuck (`subagent-driven-development`, which already says never to "force the
  same model to retry without changes"). `delegate` is what it can call when
  more than one provider is enrolled and the jobs are genuinely independent.
- **[code-winnow](https://github.com/wyc79/code-winnow)** decides what counts as
  chaff and which passes read a change. It runs over the patch a delegated run
  produces, or — the interesting direction — dispatches its own passes *through*
  `delegate` as jobs, so N readers over one diff land on N seats instead of
  queueing on one provider's quota.

  `delegate` used to run winnow's scanner itself at the merge gate. That was
  backwards: it made a quality pass a property of the dispatcher, chose one
  package on the caller's behalf, and put a judgement in the brief that the
  caller had not asked for.

The line is: anything about *how to do software work well* belongs to the
caller. What belongs here is what only this can do — placing work on seats and
keeping it moving when one empties.

## Status

Green suites, from a clone of this repo:

```bash
python3 orchestrator/tests/test_orchestrator.py  # 161 tests, no tokens spent
python3 orchestrator/tests/test_failover.py      # 99 more, same
```

They drive the real state machine over a real git repository with a scripted
adapter, so dispatch, isolated worktrees, waves, verify, failover and
integration are exercised without an agent CLI installed and without spending
anything.

**Exercised with real agents, once.** Four jobs across two providers on a real
assignment, graded by that course's own script: **31/31, first attempt, no human
correction**, one attempt per job, zero scope violations. The wave ran three
jobs concurrently and a second wave of one, and it crossed providers without
being told to — the plan marked one job `t3`, and only one seat serves that
band, so a strong model on one provider worked alongside two on another.

**Built and exercised:** dependency waves in isolated worktrees; scope measured
per job; the Integrator on merge conflicts; cost and elapsed time recorded per
delegation from the CLI's own JSON; autonomous mode ending at an opened PR;
quota-aware failover with per-channel cooldowns shared across repos; the
utilization shadow price; tier-to-provider routing; gates that park and are
answered out of band; and teardown that leaves no agent process behind whatever
ends the run. Multi-provider placement is exercised against the shipped registry
rather than a fixture one.

**Not yet exercised with real agents**, because the conditions have not arisen
in a live run: failover on an actual quota wall, and the Integrator on a real
merge conflict. Both are covered by deterministic tests; neither has been seen
in the wild.

**Two honest weaknesses.** Cross-provider *dollars* are weaker than
cross-provider tokens: not every CLI returns a cost figure, so where one reports
usage but not money the spend is this project's own price table applied to token
counts, carried as `usd_estimated` rather than passed off as billed. And there
is no cross-process lock on the breaker file — writes merge under an in-process
lock, so a breaker lost between two `delegate` processes is recoverable but the
file is not safe for simultaneous writers in the strict sense.

## License

MIT — see [LICENSE](LICENSE).
