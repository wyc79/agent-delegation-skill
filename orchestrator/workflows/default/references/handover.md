# Continuing someone else's turn

Read this when your prompt says a previous agent held this role and stopped
part-way through.

**You are not a second attempt.** A session can end for reasons that have nothing
to do with the work — the provider's capacity ran out, the process died. When
that happens the approach is still on trial, not on its last chance. No attempt
budget was spent on you. Do not report the predecessor's stop as a failure of
the work, and do not treat the code you inherit as already suspect.

## Step 1 — Find out what it actually left

```bash
git log --oneline <base-commit>..HEAD   # its checkpoints, if it got that far
git status --porcelain                  # its uncommitted, possibly half-applied edits
```

Expect either, or both, or neither. An agent stopped mid-sentence gets no chance
to tidy up, so what you inherit is whatever it happened to be holding. **A clean
`git log` does not mean nothing was done** — check the working tree before you
conclude you are starting from zero.

Then read `reports/` for entries under your
subtask. It may have logged some before it stopped.

## Step 2 — Decide what each piece is worth

- **Committed work is yours to build on.** Do not restart it and do not revert
  it — reverting another agent's committed work is never an acceptable deviation
- **Uncommitted work is unproven.** Read it, keep what is right, and commit it as
  your own checkpoint once you have verified it. If it is incoherent, repairing
  it is a departure worth logging, not a silent cleanup.
- **A test that already passes is not a gap in your role card.** If your card
  tells you to watch a new test fail before writing code, and the predecessor
  already made it pass, that obligation is met. Say in your report which tests
  you inherited rather than rewriting them to manufacture a red run.

## Step 3 — Share the subtask's id namespace

Report ids are namespaced by **subtask**, not by
agent, and you are the second agent inside that namespace. Continue numbering
from the highest entry already there for your subtask; do not restart at 1.

## Step 4 — Report for the whole subtask, not your share of it

Your report replaces the predecessor's. It must describe the subtask's full
state, and say plainly in the summary which part you inherited and which part you
verified yourself. Whoever dispatched you reads the diff as one agent's work — your summary
is the only place that fact survives.
