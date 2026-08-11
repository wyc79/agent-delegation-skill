# Agent delegation

**`delegate` places each stage of a task on whichever enrolled agent seat can do
it — different providers, different models — meters the quota on each, and keeps
the run going when one of them hits a wall.**

It is a command line program, not a model and not a prompt. It spawns agent CLIs
(`claude`, `codex`, `cursor-agent`, `gemini` today) as subprocesses, hands each
one a role and a task directory on disk, and decides what runs next, on which
seat, and whether it is allowed to. The state graph and its guards are authored
code; LLMs choose among edges the graph already offers, through schema-validated
outputs.

Two pieces ship here, with very different sizes:

- **[`orchestrator/`](orchestrator/) — the runtime.** Seat registry and
  enrollment, capability router, escalation ceiling, usage metering and a quota
  shadow price, cooldown breakers, failover with checkpoint reuse, and three
  adapters: `local` (plain subprocesses), `herdr` (the same agents in visible
  panes), and `mock` (scripted, for the tests). This is the product.
- **[`agent-delegation/SKILL.md`](agent-delegation/SKILL.md) — the front door.**
  One file, for the agent a human is talking to: when to call `delegate`, which
  command comes next, how to answer a gate that parked. It teaches nothing about
  how to do the work.

[`orchestrator/README.md`](orchestrator/README.md) is the reference for what is
actually built and why each decision went the way it did.

## Why the claim here is continuity, not quality

This project used to carry a second claim: that its role protocol produces
better output than working without one. **That claim is retired.** An ablation
measured it and a stock agent one-shot the complex fixture task — 11 of 11
acceptance criteria, one call, thirteen minutes. A claim whose baseline improves
with every model release is not one worth defending, it is a crowded lane, and it
was never the reason this was built.

What does not improve with model releases is the seat that empties at 3pm while
another sits idle. That gets *worse*, because stronger models are metered harder.
Routing across providers, metering the draw on each, and moving a half-finished
subtask onto another seat is the part of this repo no model release makes
redundant.

Correctness is still checked, but as a **control rather than a claim**: if
routing a task across two providers costs correctness, that is a bug in the
routing. The negative control matters as much as the positive one — an ordinary
crash must consume an attempt and trigger no hop, because a router that reroutes
on any error converts real bugs into silent provider changes, and no happy-path
demo would ever show it.

## How a person uses it

**You talk to your own agent. That agent runs `delegate`.**

Not: `delegate` launches an orchestrator agent that talks to you. That was
considered and rejected — it puts the orchestrator *inside* a provider, so the
3pm quota wall kills the very thing whose job is to route around the 3pm quota
wall. State lives on disk instead, which makes the human the recovery path: if
your provider dies, open another agent, point it at the same repo, and continue.

```text
you  →  your agent    "add a subtract endpoint, and plan it before you write it"
        your agent    delegate run "add a subtract endpoint"
        delegate      planner on claude-seat → plan.md → parks at the plan gate
        your agent    reads the brief, puts the question to you in prose
you  →  your agent    "yes, but keep the old endpoint working"
        your agent    delegate approve --note "keep the old endpoint working"
        delegate      implementers in isolated worktrees, verify, review, patch
```

**Gates park; they never prompt.** Reaching `design`, `plan` or `merge` with
nobody at a terminal does not decline the gate — the task stops with status
`awaiting_approval` and a `pending_gate` holding the question and a
human-readable brief. A question nobody answered is not a no, and the CLI will
not record one as the other.

**`--note` is an instruction, not a log line.** On an approval it is written into
the prompts of the planner, of every implementer, and of the reviewer. The
reviewer is the one that matters most: without the note it sees a retained
endpoint nobody planned for, calls it scope creep, and rejects work that is doing
exactly what the human asked.

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
$ADG init                      # what's detected, who gets which role
$ADG run "fix the failing auth test"
```

`init` prints the project key, the state directory, which agent CLIs are usable,
which model gets which role, which companion skills were found, and what it will
do about verification. It writes nothing without `--write`. (`--repo` overrides
the default of the current directory, if you would rather not `cd`.)

**2. The front door.** Copy `agent-delegation/` into your agent's skills
directory, so the agent you talk to knows when to reach for delegation and what
to do with a parked gate. **This is not sufficient on its own** — the skill
describes a program, and without the clone above that program is not on disk.
Installing it without the CLI leaves an agent with instructions it cannot follow.

No model configuration is needed for either half. Agents never choose models;
they receive a role and read artifacts. Model routing belongs to the runtime, and
`registry.default.yaml` is the file it reads (`--registry` points at another).

## The commands

Run them from the repository being changed. `delegate` identifies the project
from `git rev-parse --git-common-dir`, so the working directory matters.

| Command | Does |
|---|---|
| `init [--write] [--force]` | Detected seats, role assignments, companion skills, verify config. |
| `run <request>` | Create a task and drive it. `<request>` is text, or a path to a file holding it. |
| `resume [--id] [--stage] [--when-open]` | Continue a parked task. `--when-open` sleeps out a quota window first. |
| `approve [--id] --note "…"` | Answer a waiting gate yes, then continue the run. |
| `reject [--id] --note "…"` | Answer it no. Records the decision and stops. |
| `status` | One line per task for this project, ending in its task directory. |
| `show [--id] [--brief]` | `--brief` prints the gate brief, already written for a human. |
| `channels [--clear NAME]` | Per-seat cooldowns and estimated quota draw. |

`run` also takes `--id`, `--mode attended|autonomous`,
`--adapter herdr|local|mock`, `--no-panes`, `--max-cost N`,
`--review auto|always|never`, `--dry-run` and `--yes`. Two worth understanding
before use: `--dry-run` walks the state machine with no agents and no spend,
which is the cheapest way to see the shape of a run; `--yes` auto-approves
**every** gate for the whole run, which exists for unattended runs and should
never be reached for because a gate is inconvenient.

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
transcript. A cursor implementer picks up a claude planner's work by reading
`plan.md`, and a replacement agent after a failover reads what its predecessor
committed. Without a deterministic shared path and a validated report shape there
is no handoff, and multi-provider delegation stops being a thing that can happen.

| File | Holds | Authority |
|---|---|---|
| `task.md` | The request and its numbered acceptance criteria | **What was asked** — outranks everything |
| `plan.md` | Approach plus one YAML block per subtask | **How it is being done** |
| `spec.md` | The approved design, when there was a design stage | Settled approach |
| `brief.md` | The last gate brief, written for a human | — |
| `deviations.md` | Append-only log of departures from the plan | Amends `plan.md` |
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
├── winnow-passes.md      a worked design for routing a FOREIGN pipeline's
│                         stages onto the existing router — deliberately unwired
└── workflows/default/    the bundled default workflow: the protocol DISPATCHED
                          agents follow. Orchestrator-internal, not installed.
    ├── PROTOCOL.md       entry point, read before any role card. No
    │                     frontmatter: it is read by absolute path rather than
    │                     installed, and two files claiming the same skill name
    │                     is a collision a loader resolves arbitrarily
    ├── workflow.yaml     the stage manifest (in progress — see Status)
    ├── roles/            one card per role; an agent reads exactly one
    ├── references/       depth loaded only when its trigger fires: task-dir,
    │                     escalation, deviations, parallelism, handover,
    │                     companions, scratch-files
    ├── schemas/          JSON Schema for reports, verdicts, subtask blocks
    └── templates/        starting points for task/plan/deviations

registry.default.yaml     model capability scores, role→capability profiles,
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
the budget that matters is per role rather than per repo. `PROTOCOL.md` is read
by all five roles, so a line there costs five times a line in a card, and a
reference loads only when its stated trigger fires ("tests failed 3 times → read
`references/escalation.md`"). A reviewer never loads the parallelism rules,
because it never writes code or shares a worktree. Moving those rows out of the
shared entry point lengthened the cards and made every individual role cheaper.

## The default workflow

The bundled workflow is the role protocol this project used to *be*. It is one
workflow among several now:

```text
intake → classify        simple work skips straight to implementation
       → brainstorm      complex attended work only — a spec you approve first
       → plan            a strong seat writes a durable plan, not chat
       → implement       one subtask each, isolated worktrees, checks after
                         every attempt
       → review          requirements → plan → diff → evidence, then a verdict
       → integrate       merge in dependency order, re-verify at each step
```

There is deliberately no `verify` stage: checks run *inside* `implement` and
`review` rather than beside them, so no LLM review is ever paid for to discover a
compile error.

Roles are matched to seats by capability, never by tier arithmetic.
`registry.default.yaml` declares what each model scores and what each role
requires — `planner: {reasoning: 5, ctx: 150000}`, `implementer: {coding: 4,
tool: 4}` — and the router picks from the seats that clear the floor. Swapping
providers is an edit to that file and nowhere else.

On the shipped two-seat registry that produces a genuine split as the quota
drains. At zero draw the two seats price identically and everything runs on the
claude seat; from its first recorded invocation the implementer and test-author
price out to the cursor seat, because a subscription's marginal cost rises with
how drawn it is and the emptier seat is then strictly cheaper. Past 70% drawn
(`1 − reserve_fraction`) the reservation withholds the claude seat from them
outright. The planner and reviewer stay on it either way: the only model
enrolled for those two roles is exposed by that seat alone, and they are the
roles in its `reserve_for` list. Nobody configured that split; it falls out of
the shadow price, the reservation and the capability floors.

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
  *and* the escalation ceiling — so reaching it takes two deliberate edits rather
  than one runaway ladder.
- **Escalation triggers on objective signals** — consecutive failed checks, or a
  signal the agent raised with an artifact citation and real output attached —
  not on self-reported confidence, which is a tiebreaker and cannot move anything
  on its own. An honest `escalate` is routed, never punished: if stopping at the
  threshold ever ends up worse than failing in silence, agents learn that.
- **Parallelism is pessimistic.** Disjoint declared write scopes in separate
  worktrees, with unmergeable files locked to one agent. A merge conflict in a
  binary scene has no good resolution.
- **Review is an evidence chain**, not a vibe check: every acceptance criterion
  gets a row and a verdict, and every blocking finding must cite a requirement, a
  plan line, or real output.

## Companion skills

Optional, detected once per run, each attached where it belongs. Missing ones are
reported as missing rather than silently skipped.

- **[code-winnow](https://github.com/wyc79/code-winnow-skill)** — only its
  deterministic scanner is used: stdlib, sub-second, no model call, run at the
  review stage alongside build/test/lint and on tasks that skip LLM review too.
  Its findings are advisory. Its six *judgment* passes are a separate, unwired
  thing — [`orchestrator/winnow-passes.md`](orchestrator/winnow-passes.md) is the
  design for routing them across seats, and it ends by saying nothing happens
  until an operator enrolls the roles.
- **[`andrej-karpathy-skills:karpathy-guidelines`](https://github.com/multica-ai/andrej-karpathy-skills)**
  — the mistakes that make generated code fail review (overcomplication,
  unrequested scope, unstated assumptions). Read by agents *before* they write.
- **[superpowers](https://github.com/obra/superpowers)** — `brainstorming`
  supplies the discipline for the design stage on complex attended tasks, writing
  `spec.md`, which is gated to you and then handed to the planner as a settled
  approach. Its `writing-plans` is deliberately *not* used — a plan here is a
  machine-enforced contract, not prose
  ([`companions.md`](orchestrator/workflows/default/references/companions.md)).

Authority stays with the acceptance criteria, the plan, and the deterministic
checks.

## Status

Green suites, from a clone of this repo:

```bash
python3 orchestrator/tests/test_orchestrator.py  # 192 tests, no tokens spent
python3 orchestrator/tests/test_failover.py      # 104 more, same
```

They drive the real state machine over a real git repository with a scripted
adapter, so the whole pipeline — plan, isolated implementation, verify, review,
integrate — is exercised without an agent CLI installed and without spending
anything.

**Built and exercised:** the full pipeline; parallel subtasks in separate
worktrees; the Integrator on merge conflicts; an independent Test Author on
complex tasks; cost and elapsed time recorded per delegation from the CLI's own
JSON; autonomous mode ending at an opened PR; quota-aware failover with
per-channel cooldowns shared across repos; the utilization shadow price;
signal-routed escalation from an agent's own report; and gates that park and are
answered out of band. Multi-provider placement is exercised against the shipped
registry rather than a fixture one: the suites run on `registry.default.yaml`,
and cooling the claude seat routes the implementer to the cursor seat.

**In progress on the branch this README sits on:** the stage manifest — the work
that makes the stage list *declared* instead of hardcoded in `machine.STAGES`
and `prompts.compose`. That pair is the only reason this has been a workflow
rather than a workflow host, since there was no way to point a stage at somebody
else's method. `workflow.yaml` and the code that reads it are landing now, and
**nothing on this branch should be read as a promise that hosting a foreign
workflow works yet** — no workflow this repo did not write has been run through
it. Note also what the manifest deliberately will *not* do: it cannot invent a
stage. `implement` runs worktrees, waves, an escalation ladder and a rework loop
while `classify` parses one line of text; listing them as interchangeable data
would be a lie that fails on the first non-default workflow. The graph stays
authored code, which is the property that makes a run replayable.

**Not done, and it is the headline acceptance test:** one run whose design stage
uses superpowers on provider A, whose implement stage runs a stock agent on
provider B, and whose review stage runs code-winnow's judgment passes split
across both, with a provider killed mid-run. **That run has not been performed**,
and its third leg is not even wired — `winnow-passes.md` is a design for routing
those passes, not an implementation. Treat "hosts superpowers and code-winnow end
to end" as a target, not a capability.

**Also absent:** *live* quota metering (the draw above is estimated from
invocation counts, not read from a provider), weekly-cap modelling,
telemetry-driven registry recalibration, container isolation, and a
before-dispatch check that can *refuse* work — the feasibility check warns when
no seat can finish, and never parks a runnable task.

**Two honest weaknesses.** Parallel waves are enabled (`max_parallel_agents: 3`)
on the strength of deterministic tests — the long-intermittent wave failure was
the test harness's own path matching rather than a race, and every attribution
defect found on the way is fixed and pinned — but no wave has yet run with real
agent CLIs, so treat the first as a shakedown. And cross-provider *dollars* are
weaker than cross-provider tokens: not every CLI returns a cost figure, so where
one reports usage but not money the spend is this project's own price table
applied to token counts, carried as `usd_estimated` rather than passed off as
measured. Under herdr's visible panes there is no cost at all — which is what
`--no-panes` trades back, and why `max_cost_usd` cannot bind while panes are on.
Lead with continuity; read the dollars as an estimate.

[`orchestrator/README.md`](orchestrator/README.md) has the configuration, the
full scope boundary, and the defect write-up.

`DESIGN.md` — the initial design, written before any code existed — was removed
on 2026-08-10. It described the workflow-quality thesis retired above, so keeping
it at the root would have made the repo's most prominent document its most
out-of-date one. Its reasoning either sits inline in the code it explains, or
moved to `orchestrator/README.md`; the section citations that pointed at it were
removed with it, because a pointer into a deleted file costs a reader a lookup
and returns nothing.

## License

MIT — see [LICENSE](LICENSE).
