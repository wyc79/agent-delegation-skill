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
- **[`agent-delegation/`](agent-delegation/) — the front door.** For the agent
  deciding whether to reach for this: when it is worth the cost, how to write
  the jobs, and how to read what comes back. Plus one reference loaded only on
  its trigger — `one-seat.md`, which is how to run parallel jobs *without* this
  when `init` says you have a single provider, because on one seat that is
  measurably the better choice.

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

Those ratios describe **the protocol**, not what is here now. Cutting it took
the run from $13.07 to about $3 — see Status, where the wrapper is measured
against two further controls and does not come out ahead of either on a single
provider.

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
| **Autonomous** | `--mode autonomous --yes` | Auto-approved | A branch pushed to `origin` |

**Merge is never automatic in either mode**, and there is no commit path to your
branch in this codebase — checkpoint commits happen only inside throwaway
worktrees.

**Neither mode opens a pull request.** Autonomous mode used to, with the merge
brief as the body. Proposing a branch is an outward-facing act on your account,
and this program cannot know whether work nothing reviewed is ready to be shown
to anyone — so it pushes, prints the `gh pr create` you would run, and stops.

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
├── SKILL.md              THE FRONT DOOR. For the agent a human is talking to:
│                         when to call `delegate`, which command comes next,
│                         how to answer a parked gate. No methodology.
└── references/
    └── one-seat.md       loaded only when `init` shows one provider: how to run
                          parallel jobs WITHOUT this, which on a single seat is
                          cheaper and faster. A skill that will not say so is
                          selling, not advising.

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

## Walkthrough: superpowers → delegate → superpowers

`superpowers:subagent-driven-development` carries a Red Flag that reads **"Never
dispatch multiple implementation subagents in parallel (conflicts)."** That rule
exists because its subagents share one working tree. `delegate` is what removes
the reason for it — disjoint declared write scopes, one git worktree per job,
merged afterward.

So the handoff is not "SDD calls `delegate` per task". It is: **SDD's per-task
implementer loop is replaced by one `delegate` run over a batch of independent
tasks, and SDD's review loop picks up after.** Everything before and after stays
superpowers.

**0. Brainstorm and plan.** `superpowers:brainstorming` → `superpowers:writing-plans`,
unchanged. You end with a plan whose `### Task N` blocks each carry **Files**
(Create/Modify/Test) and **Interfaces** (Consumes/Produces). Those two sections
are what make the next step mechanical.

**1. Check there are two seats.** This is the go/no-go, and it is one command:

```bash
cd /path/to/your/repo
delegate init
```

If it ends *"Every tier resolves to …"*, stop and use SDD normally — on one seat
you are buying subprocess indirection. If it ends *"2 seats serve these tiers"*,
continue.

**2. Translate the plan into `jobs.md`.** The only real work, and a field lift:

| superpowers plan | delegate job block |
|---|---|
| `### Task 3: Rate limiter` | `id: st-3-rate-limiter` |
| **Files:** Create / Modify / Test | `file_scope:` — all of them, tests included |
| **Interfaces: Consumes** | `reads:` and `depends_on:` |
| **Interfaces: Produces** | `frozen_interfaces:` |
| Model Selection: cheap / standard / most capable | `tier: t1` / `t2` / `t3` |
| Global Constraints | repeated into each `goal` or `acceptance` |

The tier column is the interesting one. SDD already tells you to choose a model
per task and to **"always specify the model explicitly"**. `tier` is that same
decision — except a tier resolves to a model *on whichever seat has headroom*,
which a plain subagent dispatch cannot do.

**3. Give it your checks.** `.adg.yaml` in the repo under work. Nothing here has
an opinion about what passing means:

```yaml
fast: ["pytest -q"]
slow: ["pytest -q --integration"]
```

**4. Run it.** `delegate run "rate limiting" --plan jobs.md`. Jobs land in
sibling worktrees under `../.adg-worktrees/<project-key>/`, each on
`adg/<task-id>/<job-id>`, merged into `adg/<task-id>/work`.

**5. The gate parks — exit 1 is not failure.** A calling agent has no tty, so
the gate cannot ask it. The question is recorded and the run exits `awaiting_approval`:

```bash
delegate show --brief                     # the question, written for a human
delegate approve --note "yes, but keep the v1 endpoint working"
```

The note is not decoration: it flows into every prompt downstream, so a
qualified yes reaches the agents that act on it.

**6. Read what came back.** The brief carries a row per job — the files it
touched and which of them fell outside the scope it was given — plus per-seat
cost and your check output. And, in as many words, that nothing reviewed any of
it.

**7. If a job escalated**, it hands back its `signals` whole and leaves its
worktree in place. That is exactly SDD's **BLOCKED** input: more context, a
stronger tier, split the task, or escalate to the human. `delegate` does none of
that itself — it would be a worse copy of the skill that called it.

**8. Review, and this is where superpowers returns.** Attended mode leaves
`integrate.patch` in the task directory:

```bash
delegate status                           # prints the task directory
git apply <task-dir>/integrate.patch
```

Then run `superpowers:requesting-code-review` over the branch, in your own warm
session, on work you can see. This is the step `delegate` refuses to fake.
Finish with `superpowers:finishing-a-development-branch`.

**Three things that bite:**

- **Commit the plan first.** Job agents work in worktrees cut from your HEAD, so
  an uncommitted plan file is invisible to them. Either commit it or put the
  requirements in `goal` — the goal *is* the whole brief; the agent gets no plan
  and no context from you.
- **Running from inside a superpowers worktree is fine.** The project key comes
  from `git rev-parse --git-common-dir`, which is identical from every worktree
  of a repo, so the task state is the same from anywhere in it.
- **A parked or crashed task keeps its worktree on purpose.** That is the
  salvage point. Only `done` reaps them; the branch survives either way.

`--dry-run` walks the whole state machine, spends nothing, and shows the wave
shape before you pay for it.

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
python3 orchestrator/tests/test_orchestrator.py  # 171 tests, no tokens spent
python3 orchestrator/tests/test_failover.py      # 99 more, same
```

They drive the real state machine over a real git repository with a scripted
adapter, so dispatch, isolated worktrees, waves, verify, failover and
integration are exercised without an agent CLI installed and without spending
anything.

**Measured against three controls, on a real assignment.** Four stages of a
software rasterizer, graded by the course's own script over 26 reference images,
pass only at 31/31. Every arm scored 31/31; what differs is what it cost.

| | **wrapper**, 2 seats | **wrapper**, 1 seat | one warm session | 4 cold agents, no delegate |
|---|---|---|---|---|
| Cost | ≈$3.0 | $4.41 | $3.10 | **$2.92** |
| Wall clock | 9.6 min | — | 6.8 min | **3.5 min** |
| Tokens in | 2.63M | 2.67M | 1.52M | 1.94M |

Every one of these scored 31/31. The retired role protocol is deliberately not
a column: it is not something anyone can choose now, and measuring this against
a version of itself that no longer ships would flatter it for free. Its numbers
are in the section above, as the reason it went.

The last column is the one to read first. It is **this program's own structure —
a worktree per job, cold parallel agents, disjoint scopes, merge — as a 150-line
script with no `delegate` in it**, on one provider. It is cheaper and faster than
everything else in the table, including the warm single session. So isolation is
not what this costs.

The two middle columns isolate what does: same provider, same model, same four
jobs, only `delegate` added — **1.51x overall**. Run back to back under
controlled conditions, with every turn attributed to the tool that made it, the
same two jobs come in at **1.77x and 2.10x**; the arm-level 1.51x is diluted by
run-to-run noise wide enough to have produced one pair where `delegate` looked
*cheaper*, which did not reproduce.

Where the overhead goes is stable across both jobs: **four turns of protocol
layer, every time** — reading `PROTOCOL.md`, the role card and the report
schema, then writing the report. That is not waste to be trimmed. Three of those
four are what makes a job resumable on another provider, and the fourth is the
only way a stuck job can tell you why it stopped.

What that overhead buys is everything the script leaves out because it is a
throwaway: a merge gate that hands a human graded evidence before anything
lands, per-job scope measurement, `max_cost_usd` and `max_attempts_per_subtask`
that bind, a resumable task on disk, and the grader running at the gate.

**Which is why the precondition in the skill is not a formality.** On a single
provider with no quota pressure you are paying roughly double for a gate, a scope report
and a resumable record. That may be worth it; it is not worth it by accident.
Run `init`; if every tier resolves to one provider, decide deliberately.

**And on one provider `tier` is decoration.** `delegate` never pins a model — the
CLI runs whatever it is configured with — so a band only routes to a different
model when the *seats* expose different models. The same fifteen-line job cost
**$0.018** on a `t1` cursor seat and **$0.80** on a Claude-only deployment where
`t1` resolved to the same Opus as everything else.

**What no control can do.** A fifth arm injected a provider usage-limit failure
mid-job, twice. Each time the partial work was committed as a checkpoint, the
seat was cooled, the job was re-selected on the other provider and the
replacement continued **in the same worktree** from its predecessor's commit —
then the merge gate ran the grader itself: `FINAL SCORE: 31`. Routing across
providers mid-flight cost no correctness. Every other arm in that table simply
stops when a seat goes out.

Full write-up, per-agent figures and the scripts:
[`gpudriver-shakedown-result-wrapper/RESULTS.md`](https://github.com/wyc79/agent-delegation-skill).

**Built and exercised:** dependency waves in isolated worktrees; scope measured
per job; cost and elapsed time recorded per delegation from the CLI's own JSON;
autonomous mode ending at a pushed branch; quota-aware failover with per-channel
cooldowns shared across repos, **including salvage-and-continue across a real
mid-job provider failure**; the utilization shadow price; tier-to-provider
routing; slow checks at the merge gate; gates that park and are answered out of
band; and teardown that leaves no agent process behind whatever ends the run.

**Not yet exercised with real agents:** the Integrator on a real merge conflict,
and a wall that arrived unprompted rather than injected. Both are covered by
deterministic tests; neither has been seen in the wild.

**A known rough edge.** When a job's band is served by exactly one seat and that
seat walls, the replacement does not re-ask for the band — it asks for the
walled model's reasoning floor, and when nothing clears it, drops the floor and
continues on a weaker model, logging that it did. The work finishes; the tier
the caller asked for does not survive, and only the run log says so. Whether
that should park instead is not yet decided.

**Two honest weaknesses.** Cross-provider *dollars* are weaker than
cross-provider tokens: not every CLI returns a cost figure, so where one reports
usage but not money the spend is this project's own price table applied to token
counts, carried as `usd_estimated` rather than passed off as billed. And there
is no cross-process lock on the breaker file — writes merge under an in-process
lock, so a breaker lost between two `delegate` processes is recoverable but the
file is not safe for simultaneous writers in the strict sense.

## License

MIT — see [LICENSE](LICENSE).
