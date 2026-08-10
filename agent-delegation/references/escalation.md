# Escalation

Read when you are stuck, repeating yourself, or discovering the task is not what
the plan assumed.

Escalation moves the problem to a stronger model, back to the planner, or to a
human — **with your evidence attached**. It is a routing decision, not a
confession. The failure mode this system cares about is not "agent gave up too
early"; it is an agent burning ten iterations and a large budget to produce
something that does not work.

## Escalate now if any of these is true

These are thresholds, not suggestions. Do not negotiate with them.

| Signal | Fires when |
|---|---|
| `test_stuck` | The same test has failed **3** consecutive fix attempts |
| `edit_churn` | You have rewritten the same file **4+** times without converging |
| `scope_overrun` | The work is far bigger than planned — beyond ~5 files outside `file_scope`, or roughly double `estimated_loc`. This is about **magnitude**, not permission: a single out-of-scope file is a deviation question, not this signal |
| `plan_conflict` | The plan asserts something the code contradicts, and you cannot patch it locally |
| `ambiguous_requirement` | A requirement admits two reasonable readings that produce different code, and picking wrong is expensive |
| `blocked_command` | A command you need is refused by the sandbox or the runtime |
| `missing_dependency` | The work needs a new dependency, tool, or engine version |

Three attempts is the threshold because attempts 4 through 10 by the *same*
model are usually variations on attempts 1 through 3. If you catch yourself
thinking "one more idea," that is exactly the moment to escalate — write the idea
into the signal so the next agent can try it with more capability.

## Writing a signal that is actually useful

Put signals in the `signals` array of your report. A useful signal lets the next
agent skip everything you already ruled out:

```json
{
  "type": "plan_conflict",
  "detail": "plan.md:L48 says extend BaseSystem, but BaseSystem is final in engine 4.3 and cannot be subclassed.",
  "evidence": "godot --headless --check-only --script src/effects/poison.gd → ERROR: Cannot extend final class 'BaseSystem' (line 3)",
  "attempted": [
    "Subclass directly — compile error above",
    "Composition wrapper — works, but plan.md:L52 assumes callers get a BaseSystem"
  ],
  "suggestion": "Either the wrapper plus an adapter at the call site, or the plan changes what st-4 expects."
}
```

Rules for the fields:

- **`detail` cites the artifact.** "The plan is wrong" is unusable;
  "`plan.md:L48` says X, reality is Y" is actionable.
- **`evidence` is real output.** Paste the actual error. Never paraphrase a
  failure you did not read, and never invent output that looks plausible.
- **`attempted` prevents repetition.** Say what you tried *and why it failed* —
  the next model is smarter, not clairvoyant.
- **`suggestion` is optional and non-binding.** Offer it; do not act on it
  unilaterally after raising the signal.

## Confidence is a tiebreaker, not a trigger

You may report low confidence, and it will be taken seriously **alongside**
objective signals. It will not escalate anything on its own, because self-assessed
confidence is poorly calibrated in both directions. Conversely: feeling confident
does **not** cancel a threshold above. Three failures is three failures.

## What happens next (so you can stop cleanly)

The orchestrator walks a ladder: retry with your context → a different model at
the same tier → a stronger model within the deployment's ceiling → back to the
planner → a human. You do not choose the rung and you do not know the ceiling.

Your job is to **stop in a resumable state**:

1. Leave the worktree consistent — either your work committed as a checkpoint, or
   reverted to the last green state. Never leave a half-applied edit.
2. Log any deviations you did make before stopping.
3. Write your report with `status: "escalate"` (or `"blocked"` if nothing further
   is possible) and the signal attached.
4. Say what *is* done. Completed, verified work is preserved across escalation —
   but only if the next agent can tell what it was.

## Do not escalate for

- A test you have not yet read the failure output of.
- A requirement you could resolve by reading one more file.
- A decision already recorded in `decisions.md` that you happen to disagree with.
- Wanting a second opinion on code style.
