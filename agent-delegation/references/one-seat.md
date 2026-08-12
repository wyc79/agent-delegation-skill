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
checkout to conflict in; the only place two jobs can disagree is at merge, and
disjoint write scopes make that mechanical. So this composes two superpowers
skills that are documented separately and warned against jointly:
`using-git-worktrees` for the isolation, `dispatching-parallel-agents` for the
fan-out.

**The rule that replaces the warning:** two jobs may run at once only if their
write scopes do not overlap. Overlapping scopes go back to being sequential, and
SDD's advice applies again unchanged.

That rule is the one thing here worth checking mechanically rather than
believing, so it appears twice below as a command — once before you dispatch and
once before you merge. Overlap is the failure this pattern cannot absorb: two
agents writing one file surface it at merge with both sides already written, and
the run that produced it looks fine the whole way through.

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

**2. Write every prompt out first. Then send them all in one message**, so they
run concurrently. Each gets its own goal, its write scope, and the frozen
contracts — nothing else. No plan file, no session history, no summary of what
its siblings are doing.

Do it in that order deliberately, because the obvious order fails:

> **Measured twice, and both times the agent did it the other way.** One had
> read this file four tool calls earlier and sent three dispatches in three
> separate messages. The second read this file *with this warning already in
> it*, and sent four in four. Right worktrees, right scopes, right contracts,
> one per message, both times.
>
> Both got away with it. In that harness a dispatch returns immediately and its
> agent runs in the background, so the jobs overlapped regardless: the stagger
> cost 68s of a 219s parallel phase in the first run and 23s of 336s in the
> second. Only the first was slow enough to matter — one agent **finished**
> before the third was sent, holding peak concurrency at two of three.
>
> Do not read that as permission. It survives on one property, which neither
> dispatcher set out to preserve: **neither waited on a result before sending
> the next.** Protect that and the rest is a rounding error. Break it — a
> harness where dispatch blocks until the agent returns, or a habit of reading
> the first result before composing the second — and the same shape serializes
> completely, having paid the entire setup cost of isolation for none of the
> speed.
>
> Drafting all N prompts before sending any is what makes that impossible to get
> wrong, which is why this is written as an order of operations rather than a
> rule to remember: composing a prompt and sending it is one motion, and N jobs
> is that motion N times. If you do end up sending them one at a time anyway,
> **send them back to back and read nothing in between.**

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
