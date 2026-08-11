# Continuing someone else's turn

Read this when your prompt says a previous agent held this job and stopped
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

Then read `reports/` for entries under your job. It may have written one before
it stopped.

## Step 2 — Decide what each piece is worth

- **Committed work is yours to build on.** Do not restart it and do not revert
  it — reverting another agent's committed work is never your call to make alone.
- **Uncommitted work is unproven.** Read it, keep what is right, and commit it as
  your own checkpoint once you have verified it. If it is incoherent, say in your
  report that you repaired it rather than cleaning it up silently.
- **A check that already passes is not a gap.** If the predecessor made it pass,
  that part of the job is done. Say in your report what you inherited rather
  than redoing it to have something to show.

## Step 3 — Report for the whole job, not your share of it

Your report replaces the predecessor's. It must describe the job's full state,
and say plainly in the summary which part you inherited and which part you
verified yourself. Whoever dispatched you reads the diff as one agent's work —
your summary is the only place that fact survives.
