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
| **A** ‡ | the role protocol: planner → 4 implementers → reviewer | yes | 2 |
| **B** | the wrapper: 4 jobs the caller decomposed | yes | 2 |
| **C** ‡ | one warm Claude session dispatching its own subagents | no | 1 |
| **D** | 4 cold Claude agents, isolated worktrees, hand-rolled | **no** | 1 |
| **E1** | B, with a quota wall injected on a single-seat band | yes | 2 |
| **E2** | B, with a quota wall injected mid-job on a two-seat band | yes | 2 |
| **F** | B's jobs, run through delegate with Claude as the only seat | yes | **1** |
| **O** ✦ | D's jobs, run by a session following the skill instead of a script | no | 1 |
| **P** ✦ | O, with the briefs pre-written to files so batching is trivial | no | 1 |

✦ ran 2026-08-12, a day after the rest; see *What O settles*.

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

Columns **A** and **C** are quoted from the earlier round; their raw records are
not in this repository, so they cannot be re-derived from anything here. Every
other column can — the scripts and their output are beside this file.

| | A ‡ | B | C ‡ | D | E2 | F |
|---|---|---|---|---|---|---|
| Score | 31/31 | 31/31 | 31/31 | **31/31** | **31/31** | **31/31** |
| Wall clock | 31.5 min | 9.6 min | 6.8 min | **3.5 min** | 4.8 min | 9.6 min |
| Cost | $13.07 | $4.60 ⚠ | $3.10 | **$2.92** | $0.80 ⚠⚠ | $5.03 |
| Tokens in | 6,735,113 | 2,627,303 | 1,524,925 | 1,941,825 | 1,761,233 | 2,672,000 |
| Tokens out | 131,359 | 51,198 | 44,834 | 32,255 | 29,559 | 51,381 |
| Agent calls | 6 | 4 | 10 turns | 4 | 6 (2 walled) | 4 |
| Attempts/job | 1 | 1 | — (2 passes) | 1 | 1 | 1 |
| Scope violations | 0 | 0 | — | 0 | 0 | 0 |

‡ quoted from the earlier round; not reproducible from this repository.

⚠ **B's cost is an upper bound that cannot be corrected retrospectively.** It
was recorded while cached input was priced as fresh input, roughly ten times
over (see *Caveats*). $2.41 of it is billed (the Claude job); the $2.18 cursor portion was
computed with cache reads charged at 10× their real rate. Using E2's measured
per-job cursor costs — the same jobs, same prompts, same seat, correct pricing
— B's true total is **≈ $3.0**. The split was never stored, so this is a
recomputation, not a measurement.

**F was run twice.** The first attempt lost a job to a ~15-minute human
approval prompt from the agent CLI, so it had no usable wall clock; the figures
above are the clean rerun. The two runs cost $4.41 and $5.03 for identical work
— a ~14% spread on a four-job plan, which is the same run-to-run noise that
produced the 0.86x anomaly further down.

**A third of F's wall-clock gap is a config default.** 576s against D's 210s is
2.74x, worse than the 1.82x of summed agent time, because
`max_parallel_agents: 3` runs a four-job plan as three-then-one while the
hand-rolled control ran all four at once. `max(117, 224, 381) + 178 = 559s`
against 576s measured; all four concurrently would have been 381s. That ~195s is
a tuning choice, not overhead.

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

So the per-agent overhead is **~1.8-2.1x**. F's clean rerun agrees at the arm
level: 900s of summed agent time against D's 496s is 1.82x, and $5.03 against
$2.92 is 1.72x. The earlier arm-level 1.51x understated it because one of its
four jobs was a single-job rerun that happened to come in cheap. A single pair would have supported anything from 0.9x to 2.3x, which is
exactly what happened -- twice, in both directions, to me.

**And the protocol layer explains only about half of it.** The turn diffs in
this directory show 10 extra tool calls on `st-2-render` and 8 on
`st-4-raster`; four and five of those are protocol (reading it, the role card,
the report schema, writing the report). The remainder is `Bash` 9-vs-5 and
9-vs-6 plus a handful more source reads, and nothing here explains it. The
prompt is ~1.5x longer (3372 chars against 2099), which is a plausible cause and
is not evidence. Anywhere this write-up or the READMEs said "four turns" as if
it were the whole account, it was not.

One figure erring against itself, stated so nobody has to find it: these runs
predate the fix that stopped naming the `slow` commands in the prompt, and
`st-2-render`'s delegate arm spent a turn on `Read: check-grade.sh` as a result.
The published 1.77x and 2.10x are therefore slightly pessimistic.

What that overhead buys is everything the script leaves out because it is a
throwaway: a merge gate that stops and hands a human graded evidence, per-job
scope measurement, `max_cost_usd` and `max_attempts_per_subtask` that bind, a
task on disk that survives a crash, and the grader running at the gate. Whether
that is worth 1.5x is a judgement — but it is now a judgement with a number
attached.

B lands with D and C (≈$3.0 vs $2.92 vs $3.10) because routing three of four
jobs to the cheaper seat pays back roughly what the overhead costs.

## What O settles — what the *skill* costs on top of the plan (2026-08-12)

D's executor is a Python script that already knows the procedure. **O is D with
a live session in that slot**, reading `agent-delegation/SKILL.md` and following
it. Same `jobs.md`, same base commit, same single-seat registry, same grader.
The brief says the decomposition is already written and forbids re-planning, so
planning is held constant and the only difference is who drives.

| | D | O |
|---|---|---|
| plan | `jobs.md`, hand-authored | `jobs.md`, hand-authored |
| executor | ~150 lines of Python | a session following the skill |
| Score | 31/31 | **31/31** |
| Wall clock | 3.5 min | 8.3 min |
| Cost | $2.92 (agents only) | **$3.44** (everything) |
| Summed agent time | 495s | **509s** |

**Read the last row, not the wall clock.** Arm wall clock is hostage to whichever
single job happens to be slowest, and that job moved between runs — D waited on
`st-4-raster` at 210s, O on `st-3-clip` at 313s, the same two specs. Per job,
O against D: buffers 55s vs 56s, render 67s vs 107s, clip 313s vs 122s, raster
74s vs 210s. Noisy in both directions, summing to within 3%.

So **the skill costs nothing per agent.** What it costs is the dispatcher: 160s
of wall clock that the script spends zero on — 55s reading SKILL.md, `jobs.md`
and `delegate init`, 32s setting up worktrees, 73s merging, checking scope and
grading — plus $0.52 of its own context, 18% on top.

**Against N, the same skill without a supplied plan** (14.2 min, $5.76, also
31/31), the decomposition N wrote for itself cost ~5.9 min and ~$2.32. That is a
floor rather than an estimate: N's plan also quietly narrowed `clip_triangle` to
the near and far faces, and its clip agent ran 151s where O's did all six in
313s. N paid more and built less.

Two behaviours worth recording, because they are what the arm was really for:

- **The empty-branch gate in `references/one-seat.md` was adopted verbatim** —
  `[ "$(git rev-list --count "$base..$j")" -gt 0 ] || { echo "EMPTY: $j"; break; }`
  appeared in O's merge loop exactly as written, and O reached for `rev-list
  --count` three more times to poll which jobs had landed. It also diffed
  `--name-only` per branch before merging, which is the scope report done by
  hand.
- **The one-message dispatch rule was not followed**, for the second run in a
  row. O sent four dispatches in four messages, 11–13s apart. It cost 23s of a
  336s parallel phase here — peak concurrency still reached 4 of 4 — because the
  harness returns from a dispatch immediately. The rule survives on the
  dispatcher not *waiting* between sends, not on it batching them.

The pair of them is the useful signal: the step that shipped as a runnable
command was copied; the step that shipped as an instruction was not.

### P: the batching rule was wrong about its own cost

If instructions do not propagate and commands do, the obvious repair for the
dispatch step is to give it an artifact. **P is O with one variable changed** —
identical tree (`994b39c6`), identical `jobs.md`, identical brief — where
`one-seat.md` step 2 asked for each brief to be written to `../briefs/<id>.md`
first, so that every dispatch is three uniform lines pointing at a file and
batching them is trivial.

It worked on its own terms and still lost. Dispatch prompts fell from
2,558–3,683 chars to 442–454. The stagger fell from 36s to 6s. **And the
dispatches still went out one per message** — the third run in a row, now
including one designed to make batching effortless.

| | N | O | P |
|---|---|---|---|
| dispatches / messages | 3 / 3 | 4 / 4 | 4 / 4 |
| stagger | 68s | 36s | **6s** |
| dispatcher overhead | — | 160s | **197s** |
| Score | 31/31 | 31/31 | 31/31 |
| Wall clock | 14.2 min | 8.3 min | 6.6 min |
| Cost | $5.76 | $3.44 | $3.27 |

Wall clock is again not the story — P's clip agent ran 197s where O's ran 313s,
which is most of the gap. Summed agent time: P 461s, O 509s, D 495s.

**Writing the briefs cost 77s during which no agent was running.** Composing a
prompt *between* dispatches, as O did, overlaps with agents already working.
Moving it earlier converts overlapped time into serial time — 19s of stagger
bought for 77s of dead air. The step was reverted.

What the three runs do settle is what the batching rule was ever worth. Every
run ended exactly when its longest job ended, and that job was dispatched third
in all three:

| | slowest job | sent | stagger cost |
|---|---|---|---|
| N | clip, 151s | 3rd, 69s in | 69s |
| O | clip, 313s | 3rd, 23s in | 23s |
| P | clip, 197s | 3rd, 4s in | 4s |

The cost was never the message count — it is **how late the critical-path job
goes out**, and it is zero if that job goes first. `one-seat.md` step 2 now asks
for the longest job first and treats batching as a preference. Three attempts to
make an agent batch its dispatches failed; the ordering rule needs no compliance
at all, because getting it right costs nothing and the run is bounded by that one
job either way.

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

**The score measures output, not work — and every cost comparison here rests on
it.** "All arms scored 31/31" is doing heavy lifting above: it is what licenses
reading the cost column as the price of the same outcome. It does not mean the
arms did the same amount of work.

The grader diffs 26 rendered images against references. Anything that does not
change a pixel is invisible to it **by construction, not by weakness** — and at
least one requirement in `jobs.md` is exactly that. `st-3-clip` asks for all six
clip-space faces; the x and y faces are redundant against any rasterizer that
clamps its bounding box to the image, which every arm's `raster.cpp` does. Only
near and far are load-bearing, because `w <= 0` breaks the perspective divide
and produces coordinates no clamp can rescue.

So an arm that clipped against two faces renders identically to one that clipped
against six, and scores identically. One did: arm N, which wrote its own plan
and scoped the job to near and far explicitly — reasonably, on the merits. Its
clip agent ran 151s where O's six-face agent ran 313s.

The consequence for this document: **between-arm cost comparisons carry an
unmeasured variable** — how much work each arm did beyond what the images
require. It is bounded (all arms shipped four working stages against the same
contracts) and it is not zero. Where an arm wrote its own decomposition rather
than being handed `jobs.md`, treat its cost as a lower bound on the cost of the
full job.

**Other caveats.** Arms B and E2 were recorded before a pricing correction:
cached input was being charged at the fresh-input rate, roughly ten times over,
which is why B's cost above is a recomputation rather than a measurement and is
marked as one. Figures for D and F are unaffected -- their costs are billed by the
provider, not derived. n=1 per arm. No arm hit a *real* quota wall — E's was
injected. The task is unusually favourable to parallel
decomposition (four pre-split files, disjoint by construction). And B's
`jobs.md` is downstream of arm A's $2.93 planner: the frozen contracts I handed
the four agents, including the FI-7 subtlety, were written by that planner. No
arm here paid for producing its own decomposition, except N, which paid ~$2.32
and ~5.9 min for it.

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
- `anatomy.py` — reads a `--output-format stream-json` transcript and says who
  did what: the dispatcher's own calls, each dispatch's send/return/duration,
  peak concurrency, what the stagger cost, and who committed. It exists because
  a subagent's messages are emitted into the *parent's* stream and are
  distinguishable only by `parent_tool_use_id`; filtering on `type ==
  "assistant"` alone reads a subagent's writes and commits as the dispatcher's
  own, which inverts the conclusion. Two claims in `one-seat.md` were published
  from that mistake and had to be withdrawn.
- `arm-o-results.json`, `arm-p-results.json` — O's record (per-job times against
  D's, the dispatcher overhead breakdown, the dispatch pattern) and P's, which
  includes the rejected hypothesis and why it lost. A negative result is kept
  here because the step it tested is one somebody will propose again.

The per-run task state, patches and briefs are not committed: they are large,
specific to one machine's paths, and nothing in the repository reads them.
