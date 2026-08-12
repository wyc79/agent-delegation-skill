# GPU driver shakedown, second round — 2026-08-11 (evening)

Same task, same starting tree and same grader as an earlier round that recorded
arms **A** (the retired role protocol) and **C** (one warm Claude session). Their
figures are quoted here; the raw records for those two are not in this
repository. This round adds **B**, **D**, **E** and **F**. Course code is not reproduced here; only what
each arm cost and scored.

**Task.** Implement four stages of a software rasterizer (`initialize_render`,
`render`, `clip_triangle`, `rasterize_triangle`), pre-split one stage per file
so the write scopes are disjoint. Graded by the course's own script: 26 scenes
diffed against reference images, 31 points, pass only at 31/31.

## The arms

| | What it is | delegate? | Providers |
|---|---|---|---|
| **A** | the role protocol: planner → 4 implementers → reviewer | yes | 2 |
| **B** | the wrapper: 4 jobs the caller decomposed | yes | 2 |
| **C** | one warm Claude session dispatching its own subagents | no | 1 |
| **D** | 4 cold Claude agents, isolated worktrees, hand-rolled | **no** | 1 |
| **E1** | B, with a quota wall injected on a single-seat band | yes | 2 |
| **E2** | B, with a quota wall injected mid-job on a two-seat band | yes | 2 |
| **F** | B's jobs, run through delegate with Claude as the only seat | yes | **1** |

**F is D's twin, and the pair is the cleanest measurement here.** Same provider,
same model, same four jobs, same frozen contracts — the only difference is
whether `delegate` is in the loop. Arm B could not answer this, because it
changed two things at once (delegate's overhead up, a cheaper seat down).

**D is the control this round exists for.** It is delegate's *structure* —
one git worktree per job, four cold parallel agents, disjoint scopes, merge,
grade — with no delegate in it: `git worktree add`, four
`claude -p --output-format json` subprocesses, `git merge`. No state machine,
no router, no protocol file, no role card, no report schema. If **isolation**
is what costs, D lands near B. If **delegate's own layer** is what costs, D
lands near C.

## Result

| | A | B | C | D | E2 | F |
|---|---|---|---|---|---|---|
| Score | 31/31 | 31/31 | 31/31 | **31/31** | **31/31** | **31/31** |
| Wall clock | 31.5 min | 9.6 min | 6.8 min | **3.5 min** | 4.8 min | — ⚠⚠⚠ |
| Cost | $13.07 | $4.60 ⚠ | $3.10 | **$2.92** | $0.80 ⚠⚠ | $4.41 |
| Tokens in | 6,735,113 | 2,627,303 | 1,524,925 | 1,941,825 | 1,761,233 | 2,672,000 |
| Tokens out | 131,359 | 51,198 | 44,834 | 32,255 | 29,559 | 51,381 |
| Agent calls | 6 | 4 | 10 turns | 4 | 6 (2 walled) | 4 |
| Attempts/job | 1 | 1 | — (2 passes) | 1 | 1 | 1 |
| Scope violations | 0 | 0 | — | 0 | 0 | 0 |

⚠ **B's cost is an upper bound that cannot be corrected retrospectively.** It
was recorded while cached input was priced as fresh input (see *Two defects*
below). $2.41 of it is billed (the Claude job); the $2.18 cursor portion was
computed with cache reads charged at 10× their real rate. Using E2's measured
per-job cursor costs — the same jobs, same prompts, same seat, correct pricing
— B's true total is **≈ $3.0**. The split was never stored, so this is a
recomputation, not a measurement.

⚠⚠⚠ **F has no wall-clock figure.** One of its four jobs blocked ~15 minutes on
a human approval prompt from the agent CLI, so the run's wall clock is
meaningless. That job (`st-2-render`) was re-run alone afterwards and its clean
pair is used below; F's $4.41 is the sum of three original jobs plus the rerun.
The other three jobs' self-timed durations are unaffected — start times
reconstruct exactly from `elapsed_ms` plus completion, and all three finished
before the prompt.

⚠⚠ **E2's $0.80 excludes real work that was thrown away.** Two Claude agents
ran ~88s each before the injected wall; a killed CLI reports no usage, so that
Opus time is unbilled here. E2 is cheap because it did most of its work on the
cheaper seat, not because failover is free.

## What D settles

D is **cheaper and faster than every other arm, including the warm single
session it was supposed to lose to.** Four cold contexts beat one warm one on
wall clock (3.5 min vs 6.8) because they genuinely run in parallel, and beat it
on cost despite ~27% more input tokens, because Claude's cache pricing makes
cold-start context far less punitive than a raw token count suggests.

So isolation is not what delegate costs. **F vs D measures what does**, job by
job — same provider, same model (`claude-opus-5[1m]`; neither arm pins one),
every figure **billed**:

| job | delegate (F) | hand-rolled (D) | ×cost | ×time |
|---|---|---|---|---|
| st-1-buffers | $0.804 · 123s | $0.357 · 56s | 2.25 | 2.21 |
| st-2-render † | $1.086 · 183s | $0.668 · 107s | 1.63 | 1.71 |
| st-3-clip | $1.475 · 268s | $0.678 · 122s | 2.18 | 2.19 |
| st-4-raster | $1.043 · 168s | $1.213 · 210s | **0.86** | **0.80** |
| **total** | **$4.41 · 742s** | **$2.92 · 496s** | **1.51** | **1.50** |

† the clean rerun; the original blocked on an approval prompt.

**delegate costs about 1.5x a hand-rolled equivalent, on both axes.** Cost and
time track each other almost exactly (1.51 and 1.50), which is what you would
expect if the cost is really turns wearing a dollar sign.

That 1.5x is diluted by noise, and the per-job column shows how much: it ranges
0.86 to 2.25. **`st-4-raster` looked like a win for delegate — 0.86x — and it
did not reproduce.** Re-run back to back on the same tree, with every turn
attributed to the tool that made it, that job came back **1.77x against
delegate** (21 turns / $0.965 vs 13 / $0.545). Arm D's original `st-4-raster`
was simply its most expensive job and arm F's its cheapest.

Both controlled pairs agree with each other and not with the arm totals:

| job | delegate | hand-rolled | x cost |
|---|---|---|---|
| st-2-render | 20 turns / $0.856 | 10 turns / $0.407 | 2.10 |
| st-4-raster | 21 turns / $0.965 | 13 turns / $0.545 | 1.77 |

So the per-agent overhead is **~1.8-2.1x**, and the arm-level 1.51x understates
it. A single pair would have supported anything from 0.9x to 2.3x, which is
exactly what happened -- twice, in both directions, to me.

What the 1.5x buys is everything the script leaves out because it is a
throwaway: a merge gate that stops and hands a human graded evidence, per-job
scope measurement, `max_cost_usd` and `max_attempts_per_subtask` that bind, a
task on disk that survives a crash, and the grader running at the gate. Whether
that is worth 1.5x is a judgement — but it is now a judgement with a number
attached.

B lands with D and C (≈$3.0 vs $2.92 vs $3.10) because routing three of four
jobs to the cheaper seat pays back roughly what the overhead costs.

## What E settles — and it is the only thing no other arm can do

**E2: a provider went down mid-job, twice, and the run finished at 31/31.**

Both `t2` jobs started on claude-seat (this arm's registry gives claude the
`prefers` tie-break so there is something to fall from). The injected wall hit
each of them ~88s in. For each: the partial work was committed as a checkpoint,
the seat was cooled, the job was re-selected on cursor-seat, and the
replacement continued **in the same worktree** from its predecessor's commit.

```
salvaged st-3-clip's uncommitted work as a checkpoint
quota: claude-seat stated no reset time — assuming its 5h window
failover: implementer claude-seat -> cursor-seat (quota_exhausted, reopens 22:58 PDT)
```

Then `sh check-grade.sh` at the merge gate: **FINAL SCORE: 31, all 26 scenes
match the reference.** Routing a job across two providers mid-flight cost no
correctness — which is the control the project set for itself, tested here for
the first time on a real provider failure rather than in a fixture.

Every other arm stops dead in this scenario. A, B, C and D have no answer to a
seat going out; C and D would simply fail.

**E1: a wall on a single-seat band silently demotes rather than parking.**

`t3` resolves to `opus-class-strong`, exposed only by claude-seat. I expected
the run to park as `quota_all_exhausted`. It did not — it continued on
`balanced-coder` (reasoning 4, against opus's 5) on cursor-seat, and finished.

The mechanism: `_replacement` does not re-ask for the *tier*. It asks for the
walled model's **reasoning floor** on another seat, and when nothing clears it,
drops the floor and takes the best available, logging that the replacement is
weaker. So the caller's tier — described in the docs as "the whole of the
routing decision a caller gets to make" — is abandoned when its seat goes out.

Two defensible readings, and the project has not picked one in writing:

- **Finish the work.** Consistent with the reservation rule ("a reservation
  that parks work it could have done is not worth having").
- **Honour the band.** A caller marking a job `t3` is saying the cheap model
  will get it wrong. Finishing on the cheap model produces a plausible answer
  to a question that needed the strong one, and the merge brief does not say
  the band was abandoned — only the run log does.

The second matters here specifically: `clip.cpp` is the file whose one subtlety
(FI-7, `noperspective` interpolation at generated vertices) is exactly what a
weaker model gets wrong, and it is why arm C needed a second pass. E1 only
implemented `clip.cpp`, so it scores 0/31 by construction and gives no quality
signal either way.

## Two defects this round found, both by running rather than reading

Both were found while preparing or reading these runs, and both are fixed in
the repo with tests.

**1. `slow` checks never ran.** Only `"fast"` was ever passed to `verify.run`,
at all three call sites. `sh check-grade.sh` — the only thing that can observe
whether four stages work *together* — was parsed, printed in the merge brief as
*"not run … not evidence of anything"*, and executed zero times. Arm B's gate
asked a human to land a change whose grade nothing had measured; I graded it by
hand. E2 is the first run where the gate produced `FINAL SCORE: 31` itself.

**2. Cached input was billed as fresh input.** `_tokens` summed
`input + cache_read + cache_write` into one figure and `_bill` charged the lot
at the fresh rate. A cache read is 0.1× and a cache write 1.25×. Every agent
past its first turn reads mostly cache — on E2's cursor jobs, ~88% of input was
cache reads — so the estimate ran ~3.7× high, and that inflated figure reached
the brief, the budget cap, and arm B's recorded cost.

A third, smaller: `_EPOCH` was the one reset-time pattern without `re.I`, so a
provider message beginning "Resets …" lost its reset time and fell back to the
configured window.

Also fixed before arm B ran, and load-bearing for it: **`frozen_interfaces`
never reached the agent's prompt.** They were stored in the record, promised by
`SKILL.md` and by the role card ("if your prompt names a frozen interface,
treat it as a contract"), and composed into nothing. For a pipeline whose four
stages coordinate *only* through those contracts, that is the difference
between 31/31 and four agents disagreeing about coordinate space at merge time.

## Conclusion

**Do not reach for delegate to be cheaper or faster.** On one provider it costs
about 1.5x a script that does the same thing with none of the machinery.

**Reach for it when a seat can go out.** E2 is the only arm in this table that
survives a provider failing mid-job, and it survived two without losing a
point. That is a capability, not an optimisation, and nothing in A, C or D has
an answer to it.

The honest form of the pitch is the one the skill already makes to callers:
run `delegate init`, and if every tier resolves to one provider, do not
delegate. This round puts a number on what you would be giving up if you
ignored that, and it is not small.

**Caveats.** n=1 per arm. No arm hit a *real* quota wall — E's was injected. B's
cost is a recomputation. The task is unusually favourable to parallel
decomposition (four pre-split files, disjoint by construction). And B's
`jobs.md` is downstream of arm A's $2.93 planner: the frozen contracts I handed
the four agents, including the FI-7 subtlety, were written by that planner. No
arm here paid for producing its own decomposition.

## What is here

- `run-arm-d.py` — arm D in full: delegate's structure, with no delegate in it.
  The control every cost figure above is measured against.
- `diagnose-turns.py` — the turn-by-turn diff. Runs one job under two prompts
  with `--output-format stream-json` and attributes every turn to the tool that
  made it. This is what showed the overhead is four protocol turns per agent
  rather than the thing I had guessed twice.
- `turn-diagnosis-st-2-render.json`, `turn-diagnosis-st-4-raster.json` — its
  output for the two jobs, including the pair that refuted the 0.86x anomaly.
- `jobs.example.md` — the decomposition handed to `delegate`, with the frozen
  contracts written out. A worked `--plan` file.

The per-run task state, patches and briefs are not committed: they are large,
specific to one machine's paths, and nothing in the repository reads them.
