# Routing code-winnow's judgment passes

Orchestrator-side, like `registry.default.yaml`. Agents never read this file.

Two different things share the name "code-winnow", and only one of them is
wired today:

| | What it is | Status |
|---|---|---|
| `scripts/scan.py` | Deterministic scanner, stdlib, sub-second | **Wired** — runs as a check in `_stage_review`, see `adg/winnow.py` |
| Steps 1–3.5 | Six judgment passes plus a supervisor merge, each an LLM call | **Not wired** — this document is what it would take |

The scanner is a check. The passes are a review pipeline costing up to seven
model calls — `S` only when a feature was named, `C` and `D` only when the diff
warrants them, so the floor is four — which makes it a deliberate purchase, not
a default. Nothing here happens until an operator enrolls the roles.

## The passes

From code-winnow's `passes.json`, which is the machine-readable half of its
contract. Read it rather than this table if the two disagree — it is versioned
and tested upstream, and this is a copy.

| Pass | Reads | Asks | Runs | Band |
|---|---|---|---|---|
| `S` | The feature phrase | What was this change *for*? | Only when a feature was named | supervisor |
| `A` | Code, not comments | Does this line earn its place? | Always | mid |
| `B` | Every comment and docstring | Does this comment earn its space? | Always | mid |
| `C` | Docs, headers, doc-vs-code truth | Is this still true? | Conditional on the diff | mid |
| `D` | Runtime cost of added code | More work than it needs, at a frequency that matters? | Conditional on the diff | mid |
| `E` | Silent failure and fragility | How does this break, and why is the suite still green? | Whenever `A` does | supervisor |
| merge | Every pass's output | Which of these survive contact with each other? | Always | supervisor |

`A`–`E` are **N readers over one artifact** — the same bytes, by construction,
so their findings can be paired. They disagree on purpose. `E` holds a veto
over `A`'s deletions.

## Band → tier is not a name match

code-winnow's bands are **relative to what you enrolled**, not vendor tiers:

- `supervisor` = `relative_to: ceiling` — the strongest model you actually
  have. Its failure mode is *silent*: a weak read produces a report that looks
  complete and is not.
- `mid` = `relative_to: below-ceiling` — "any capable model", and equal to the
  ceiling when you only have one. Its failure mode is *visible*: a thinner
  report, findings simply absent.

The trap is reading `below-ceiling` as "one tier down". Under the default
registry the rung below `t2` is `t1` (`fast-cheap`, `reasoning: 2`), which is
not a capable model for a judgment pass — it is the classifier tier. **Map the
bands through capability requirements, not tier arithmetic**, which is what
`profiles` already does for every other role:

- `supervisor` → `require: {reasoning: 5}` → only `opus-class-strong` qualifies
- `mid` → `require: {reasoning: 4, coding: 4}` → both `t2` models qualify;
  `fast-cheap` is excluded

Which of the two a `mid` pass actually gets is decided by seat draw, not by the
band, and that is correct — the band asks for "any capable model" and both are.
Measured against the default registry:

| `claude-seat` draw | `winnow-reader` routes to |
|---|---|
| 0% | `opus-class-strong` — a subscription seat with headroom is ~free, and free capability is capability |
| 50%+ | `balanced-coder` on `cursor-seat` — the effective price of a filling window rises as `list x u²/(1-u)`, and `cost_sensitivity: high` acts on it |

`t3` stays out of reach either way: `escalation_ceiling.max_tier` is `t2` and
`ultra-reasoner` has `enrolled_roles: []`. Two switches, both deliberate. A
supervisor pass asks for the best you have *enrolled* — it must never be the
reason the top tier gets turned on.

## What an operator adds

Both edits are `registry.default.yaml`, both are once-per-deployment, and both
are fail-closed: an unenrolled pass id routes to nothing and raises
`RoutingError` rather than quietly inheriting the reviewer's models.

```yaml
profiles:
  winnow-supervisor:                  # passes S and E, and the merge
    require: {reasoning: 5, ctx: 150000}
    weights: {reasoning: 3, adherence: 2}
    cost_sensitivity: low             # E's veto is the last thing between a
                                      # deletion and a silent break
  winnow-reader:                      # passes A, B, C, D
    require: {reasoning: 4, coding: 4}
    weights: {reasoning: 2, coding: 2, adherence: 2}
    cost_sensitivity: high            # four of these run per round
```

Then enroll them on the models that should serve them — `winnow-supervisor` on
`opus-class-strong`, `winnow-reader` on `balanced-coder` and
`opus-class-strong` as its fallback.

Two profiles, not seven. The passes differ in what they read, which is carried
by the prompt, not in what they need from a model. Seven near-identical
profiles would be seven things to keep in sync for no routing difference.

**Decide whether supervisor passes may borrow the reservation.**
`claude-seat` keeps 30% of its window for `reserve_for: [planner, reviewer]`.
`winnow-supervisor` resolves to exactly one model on exactly one seat, so past
that threshold it is granted the seat anyway, flagged `demoted` — it eats
planning headroom because it has nowhere else to go. Measured, not inferred: at
90% draw the choice comes back `demoted: True`. Two honest options, and the
default is the first:

- Leave it. A review round late in a quota window competes with planning, which
  is visible in the log and self-corrects when the window rolls.
- Add `winnow-supervisor` to `claude-seat`'s `reserve_for` if you run the
  passes routinely and want `E`'s veto to be a first-class claimant. Its
  failure mode is the silent one, which is the argument for doing so.

Readers need no such decision — they have a second seat to fall to, and the
cost curve moves them there on its own.

## What is missing on this side

Enrollment alone does not make this run. Four gaps, in the order they bite:

**0. The passes write into the working repository, and the scanner does not.**
This is the one that changes an invariant rather than adding machinery.
code-winnow's Step 0 creates `.code-winnow/` in the repo under review and
git-excludes it through `.git/info/exclude`; Steps 2 through 6 write rounds,
reports, fix plans and pre-fix backups there. `scan.py --json` writes nothing —
verified, which is why the wired path costs this nothing today.

An excluded path satisfies `references/scratch-files.md` rule 2, since
`git check-ignore` honours `info/exclude`. It does **not** satisfy the stronger
promise both READMEs make, that nothing about a run is ever written into the
repository. Three honest ways out, and the choice belongs to whoever wires this:
run the passes in the throwaway worktree and let the directory die with it;
teach code-winnow a root outside the repo; or narrow the README claim to
*orchestration state* and say plainly that an enrolled review package writes an
excluded directory. Deciding it silently is the one option that is not
available — the invariant is load-bearing enough that it is tested.

**1. ~~`AGENT_DELEGATION_TASK_DIR` activates this skill.~~ Fixed.** It used to
be both the location of the artifacts and the trigger in `SKILL.md`'s
description, so launching a code-winnow prompt through `runtime.py` conscripted
the agent into agent-delegation — two protocols pointing at different files,
fired by the environment, so sending the foreign prompt verbatim did not avoid
it. Activation now rides on `AGENT_DELEGATION_ROLE`, which a foreign pass has
no reason to be given. `TASK_DIR` is a location again and can be handed over as
a plain scratch path. **A dispatcher must not set `AGENT_DELEGATION_ROLE` for a
pass that is not playing one of this protocol's roles.**

**2. There is no fan-out/merge stage.** The state machine is stage-sequential
with subtask parallelism inside `implement`. "N readers over one artifact, then
an arbitrated merge" is a new stage kind. Note that `merge` is
`delegable: false` upstream — it is judgment over other agents' output, graded
against `references/comment-evidence.md`, so it belongs to whoever is driving,
not to a seventh dispatched agent.

**3. Prompts must be located, never copied.** `passes.json` stores a `find`
string with a `count`; `scripts/passes.py` refuses to emit when it does not
match exactly, and `tests/test_passes.py` fails when one goes stale. Copying
prompt bodies into orchestrator config drifts undetectably and nothing reports
it. Read them out of the installed skill at dispatch time, the way
`winnow.find()` already locates `scan.py`.

## Two things that do not change

- **The scanner stays separate.** It is cheap enough to run on every task
  including ones that skip LLM review. Folding it into this pipeline would make
  the cheap check contingent on the expensive one.
- **Findings stay advisory.** Same rule as the scanner: a style judgement that
  could block a merge reintroduces the taste loop the reviewer rules exist to
  prevent. Authority is the acceptance criteria, the plan, and the
  deterministic checks. code-winnow's own Step 4b fix-plan approval is its
  gate, independent of anything here.

---

## Notes for generalizing — deliberately not done yet

This document is a **bespoke adapter for one package**. That is the current
recommendation, not an oversight: there is no second package, and designing the
interface against a sample size of one is how it comes out wrong. Two profiles
hand-written today is cheaper than a protocol. Revisit at package #2 or #3.

What the general version would replace, roughly in the order it should be built:

1. **Relative tiers as first-class vocabulary.** `ceiling` / `ceiling-1` /
   `floor`, resolved against the registry at dispatch. Any package naming an
   absolute tier is wrong on someone's registry. code-winnow already publishes
   its bands relatively; adg's vocabulary is the absolute one, so this is a
   consumer-side resolver. Small, and correct regardless of whether the rest
   ever happens.
2. **The env-var split.** Gap 1 above. Independent of packaging — it is an adg
   defect on its own terms, and it collides with *any* hosted skill. Also
   small, also worth doing alone.
3. **Fan-out/merge as a stage kind.** Gap 2. The largest piece and the only one
   needing new machinery rather than new config. Once it exists, every
   map-reduce package is covered.
4. **Manifest discovery.** Generalize `winnow.find()` from one hardcoded path
   to a `provides:` manifest. Note that code-winnow's `passes.json` **is
   already this shape** — `provides: "judgment-passes"`, relative bands,
   locators with counts, a declared merge step. The package half of this is
   done upstream; only consumption is missing.

The division of authority to hold onto, whatever gets built:

| Package may declare | Operator alone may grant |
|---|---|
| Pass ids, trigger conditions | `enrolled_roles` |
| Relative capability intent | `escalation_ceiling` |
| Input artifacts, output contract | `limits`, approval gates |
| Prompt locators | Anything that raises a bound |

A task may lower any limit and never raise one; a package is just one more
downstream and inherits that rule. If a package could inject enrollment, any
third-party skill could declare "my pass needs `t3`" and silently unlock the
tier that ships off by default.

**One escalation to be explicit about.** Today `winnow.py` runs a *script with
fixed arguments* — stdlib, no network, auditable in one read. Dispatching
prompt bodies from a package directory to a model with tool access is a
different trust posture, not an incremental one. It is already true of skills
generally, but automatic discovery makes it a supply-chain surface. Keep
enrollment explicit per package; do not let discovery imply consent.
