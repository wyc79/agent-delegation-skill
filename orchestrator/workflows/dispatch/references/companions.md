# Companion skills

Read when `task.json` has a `companions` block, or when you are about to
hand-roll something one of these already does well.

## You do not go hunting

The orchestrator detects what is installed and declares it:

```json
"companions": {"karpathy-guidelines": true, "superpowers": true}
```

With no orchestrator, check **once** and record what you found in your report.
Never claim a companion ran when it did not, and never quietly substitute a
lookalike — a discipline that silently did not happen reads in a report exactly
like one that did.

**Nothing here is required.** If a companion is absent, do the work yourself and
say so. No step in this protocol depends on one.

## What to call, and when

| Companion | Who | When |
|---|---|---|
| `andrej-karpathy-skills:karpathy-guidelines` | Implementer, Integrator | **Before writing code.** 67 lines on the mistakes that make agent-written code fail review: overcomplication, unrequested scope, unstated assumptions. |
| `superpowers:brainstorming` | Planner, at the **design** stage | Producing `spec.md` before any plan exists — explore the code, then reason about purpose, constraints and success criteria before proposing anything. Ignore its instructions about where to save files and about committing; the design goes to `$TASK_DIR/spec.md` and nowhere else. |
| `superpowers:systematic-debugging` | Implementer | The **second** failed attempt on the same check — before the third triggers escalation. |
| `superpowers:test-driven-development` | Test Author, Implementer | Writing the failing test, if you want the fuller discipline. |

Several of these earn their place by being the failure modes this protocol
already worries about, stated by someone else in more detail than a role card
can afford.

**With an orchestrator, brainstorming is not yours to invoke** — it runs the
design stage itself and puts the discipline in your prompt. The row is here for
the hand-driven case, and so the boundary below reads correctly.

**karpathy-guidelines is the one to actually read if you are editing code.** Its
"surgical changes" section is your `file_scope` rule arriving from the other
direction, and its "simplicity first" is what the reviewer will measure your diff
against. Reading it costs less than one rejected review round.

**systematic-debugging at attempt two is a real hook, not decoration.** By
attempt three you are escalating anyway. It does **not** buy you a fourth
attempt; the ceiling is unchanged.

## Limits that do not move

A companion changes how you work, never what counts as done:

- It does not extend the attempt ceiling.
- It does not relax the requirement that a new test **fails first**, for the
  stated reason, with the output captured.
- It does not substitute for your report, your deviations, or your evidence.
- Its opinion is not authority. Authority is `task.md`, the plan, and the
  deterministic checks — in that order.

## What is deliberately not delegated

Named so the boundary reads as a decision rather than an oversight:

| Not delegated | Why |
|---|---|
| `superpowers:writing-plans` | `plan.md`'s YAML blocks carry `file_scope` and `depends_on`, which the orchestrator mechanically enforces. Prose cannot be enforced. It also writes plans inside the repo, which this protocol forbids. **Not a rejection of its design half** — `superpowers:brainstorming` *is* used, one stage earlier, and its prose output is exactly what `spec.md` wants. The split is that a design is prose and a plan is a contract. |
| `superpowers:requesting-code-review` | It dispatches a reviewer with a crafted prompt. Ours emits a schema-validated verdict that selects a state transition and outlives the session that asked for it. |
| `superpowers:using-git-worktrees` | Worktrees and hotspot locks are orchestrator authority. An agent that makes its own worktree has left the system. |
| `code-winnow` | The orchestrator runs its scanner as a deterministic check and hands you the findings. You do not invoke it. |
| `superpowers:verification-before-completion` | Convergent with the honest-evidence rule, not additive. Follow the rule. |
