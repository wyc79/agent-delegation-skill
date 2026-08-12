# Parallel jobs on a single seat, without `delegate`

Read this when `delegate init` said every tier resolves to one provider and you
still have several independent jobs to run.

**Independence is not the gate — size is.** Three one-line functions are three
`Write` calls in one message; a worktree apiece plus three cold agents is slower
than doing it yourself, however disjoint they are. This earns its setup when
each job is substantial enough that an agent would spend real turns on it. Below
that, write the code.

Measured on a four-job task, all arms scoring the same: on a single seat this
pattern beats dispatching the same jobs through `delegate`, on both money and
wall clock. What `delegate` adds there is a merge gate, a scope report and caps
— worth having if you will use them, and pure overhead if you will not.

## Why this is not the thing superpowers warns you off

`superpowers:subagent-driven-development` says, in its Red Flags:

> **Never:** Dispatch multiple implementation subagents in parallel (conflicts)

That is right, and it is right because SDD's subagents share **one working
tree** — two agents editing the same checkout collide, and the collision is
silent until something breaks. The warning is about the shared tree, not about
parallelism.

Give each agent its own git worktree and the premise is gone. There is no shared
checkout to conflict in. So this composes two superpowers skills that are
documented separately and warned against jointly: `using-git-worktrees` for the
isolation, `dispatching-parallel-agents` for the fan-out.

**The rule that replaces the warning:** two jobs may run at once only if their
write scopes do not overlap. Overlapping scopes go back to being sequential, and
SDD's advice applies again unchanged.

**The worktree does not make that rule redundant** — it is why the rule is
needed. Isolation does not remove a collision, it *defers* one:

| | prevents |
|---|---|
| a worktree per agent | two agents writing one file **at the same time** |
| disjoint write scopes | their two unreviewed edits being **reconciled at merge** by a tool that cannot read |

If two branches both changed `alpha.c`, git does a three-way merge. Where the
edits overlap textually you get a conflict — loud, and an agent can resolve it.
Where they do not, git merges them **clean**, having no opinion about meaning:
two independently invented helpers for one job, a call written against a
signature the other branch just changed, two coherent rewrites of one invariant
that are incoherent together. It builds. Often it passes. Nothing reports
anything, so nothing gets invoked to fix it — which is why "another agent can
resolve the conflict" does not cover this case. There is no conflict.

Deferred is also worse than immediate, in one specific way: by the time it
surfaces both sides are fully written, so you have paid for both and can drop
neither cheaply. That is what the two commands below are for — one before you
spend anything, one before you merge.

## The pattern

**1. A worktree per job, all cut from the same commit — and prove the scopes are
disjoint before you spend anything.**

```bash
base=$(git rev-parse HEAD)

# every job's write scope, one line each: "<job> <file> [file...]"
cat > /tmp/scopes <<'EOF'
st-1-alpha  alpha.c
st-2-beta   beta.c beta.h
st-3-gamma  gamma.c
EOF

awk '{for(i=2;i<=NF;i++) if($i in o){print "OVERLAP: "$i" in "o[$i]" and "$1; e=1} \
      else o[$i]=$1} END{exit e}' /tmp/scopes || exit 1

while read -r job _; do git worktree add "../wt/$job" -b "$job" "$base"; done < /tmp/scopes
```

If that prints `OVERLAP`, you do not have N parallel jobs — you have fewer. Merge
the colliding ones into a single job or run them in sequence.

**2. Dispatch the longest job first, and send the rest without stopping to read
anything.** Each gets its own goal, its write scope, and the frozen contracts —
nothing else. No plan file, no session history, no summary of what its siblings
are doing. All in one message if you can manage it, but the order matters more
than the batching, and that part is measured:

> **Three runs, three times one dispatch per message.** One had read this file
> four tool calls earlier. One read it with a warning about this exact mistake
> already in it. In the third the prompts were pre-written to files specifically
> so that batching would be trivial — and it still sent them one at a time. Take
> it as given that you will too.
>
> Which is survivable, because the number of messages is not what costs. Every
> one of those runs ended exactly when its longest job ended, and the longest job
> went out **third** every time:
>
> | | slowest job | sent | stagger cost |
> |---|---|---|---|
> | run 1 | clip, 151s | 3rd, 69s in | **69s** |
> | run 2 | clip, 313s | 3rd, 23s in | **23s** |
> | run 3 | clip, 197s | 3rd, 4s in | **4s** |
>
> The whole cost is **how late the critical-path job went out.** Send it first
> and there is nothing left to save — the others finish inside its shadow whether
> you batched them or not. You almost always know which one it is: it is the job
> you would have marked `t3`, the one with the subtlety in it.
>
> Pre-writing the prompts to files was tried, as run 3, and does not pay. The
> stagger did fall to 4s — but writing them cost 77 seconds during which no agent
> existed yet, where composing a prompt *between* dispatches overlaps with agents
> already running. It bought 19 seconds for 77.
>
> One thing does still matter independently of order: **never wait on a result
> before sending the next dispatch.** All three runs held that without trying,
> which is the only reason one-per-message stayed cheap. A harness where dispatch
> blocks until its agent returns, or a habit of reading the first result before
> composing the second, serializes the whole thing — and then you have paid the
> entire setup cost of isolation for none of the speed.

```text
You are implementing one file of <the thing>. Other agents are implementing
the others right now, in worktrees you cannot see. You cannot talk to them.

Your job: <id>
<goal — this is the whole brief>

Write ONLY these files: <scope>
You may read anything else, but do not modify it.

Frozen interfaces — the other agents are coding against these right now.
Match them exactly; changing one breaks work you cannot inspect:
  <exact signatures, invariants, coordinate conventions>

Verify with `<your check>` before you finish. Commit when it is green.
```

**3. Check each branch committed something and stayed in its scope, then merge
one at a time, checking after each**, so a break surfaces against the smallest
diff:

```bash
git checkout -b integration "$base"
while read -r job scope; do
  [ "$(git rev-list --count "$base..$job")" -gt 0 ] || { echo "EMPTY: $job"; break; }
  stray=$(git diff --name-only "$base" "$job" | grep -vxF "$(echo "$scope" | tr ' ' '\n')")
  [ -z "$stray" ] || echo "OUT OF SCOPE in $job: $stray"      # report, do not stop
  git merge --no-edit "$job" && <your check> || break
done < /tmp/scopes
```

**`rev-list` catches a failure with no symptoms.** An agent that reports success
without committing leaves its branch sitting at `base`, and merging that branch
**succeeds** — a merge with nothing to merge is a clean no-op. The untouched stub
still compiles, so your build check passes too. Green integration, missing
feature, nothing anywhere reporting a problem. On the measured runs every agent
did commit, on the strength of one line in its brief. The check costs a
`rev-list` and removes the case where you find out at the end. The branch is the
evidence that work happened — not the agent's report, and not the merge's exit
code.

**`diff --name-only` closes the scope rule.** Step 1 proved the scopes were
disjoint *as declared*; this is the only thing that tells you they were disjoint
*as written*, since nothing stopped an agent from editing a file it was merely
asked not to. Report and keep going rather than stopping: by now the work exists
and the question is whether to keep it, which is a judgement. Two branches that
both touched one file are also the one case where merge order changes the
result — merge the smaller claim first so the conflict surfaces while it is still
small.

Both checks read the same `/tmp/scopes` from step 1, so the declaration you
gated on is the declaration you audit against.

## The frozen contracts are the whole thing

Agents in separate worktrees cannot read each other's code. Every signature,
invariant or convention that two jobs share has to appear **verbatim in both
their prompts**. Get it wrong and the disagreement surfaces at merge — the one
place isolation cannot help you, because by then both sides are written.

On the measured task the contract that mattered was one sentence: *this vector
is in clip space, homogeneous, at every boundary; the perspective divide happens
exactly once, downstream.* Four agents, none of whom could see the others, all
built against it and the merge was mechanical. Leave it out and every one of
them makes a defensible local choice and they do not compose.

Write the contracts before you write the goals. If you cannot state them, the
jobs are not actually independent and you should not be running them in
parallel.

**Budget for writing them.** Measured on the same task twice: handed the
contracts, this pattern cost X; made to derive them mid-flight, it cost roughly
double, and the extra was not setup — it was one agent merging work that looked
right, failing the two cases that turned on a single interpolation rule, and
bisecting its way back to the cause. The contracts are not paperwork you do
before the real work. They *are* the part of the work that cannot be
parallelised, and skipping them moves the cost to the far end where it is dearer.

**Planning first is what produces them.** An agent that wrote a plan before
dispatching had the contracts in hand as a matter of course — its per-task
Interfaces blocks named the buffer layout, the ownership rules for borrowed
attribute data, and which coordinate space crossed each boundary. That is the
step to keep if you keep only one.

**What a plan will not do is guess your scope.** The same plan quietly narrowed
the job — two clipping planes instead of six — because nothing said how much was
wanted, and then implemented what it had planned, correctly, and passed every
check. If the size of the work is not written down somewhere, planning will
choose a size for you and nothing downstream will flag it.

## What you give up

- **No gate.** Nothing holds the work until you look at it.
- **No scope measurement.** An agent that edits a file outside its remit is
  invisible — nothing is watching, so nothing reports it.
- **No caps.** Nothing binds spend or attempts.
- **No checkpoint when a seat walls.** The CLI dies with your in-flight work
  uncommitted and that job restarts from nothing. `delegate` commits what it
  had, parks with the reopen time, and resumes from that commit.

That last one is the one people discover expensively. If you run your one seat
hard enough to hit walls, it is the reason to pay the overhead anyway.

## A worked implementation

`evidence/run-arm-d.py`, beside this skill's repository, is this pattern as ~150
lines of Python — worktree setup, concurrent dispatch, ordered merge, grade —
and it is what produced the measurements quoted above. It is a worked example,
not a supported tool: nothing in the test suite exercises it, its paths are the
ones it was run with, and it shells out to one specific agent CLI. Read it for
the shape; write your own for your setup.
