# Human input in pane mode

`delegate` was written assuming it is the only writer of a session. Every
adapter it started with holds that: `local` runs an agent CLI as a subprocess
with the prompt on stdin, `mock` replays a script, and neither has a keyboard on
it. `herdr` with panes does not. A pane is a real terminal, visible on purpose,
and the thing it is visible *for* is a human watching a job and reacting to it.

So the assumption fails exactly where the feature succeeds. This note says what
ships today, what it cannot see, and what herdr would have to expose to replace
it with something that is not a heuristic.

## Unblock and steer are different events

Two things a human can type, and they are not the same fact about the result.

**Unblock.** The session is parked waiting for input — herdr's blocked state,
which `HerdrAdapter.prompt` already reads back and `machine` already turns into
`Halt("needs_human")`. An agent stuck on a permission prompt, a question, a
choice it will not make alone. Typing here is the intended use of the mode:
nothing was in flight, the job resumes on the goal it was dispatched with, and
the claim `delegate` makes about the result is undamaged. Expected, worth
counting, not worth flagging.

**Steer.** The session is working and somebody redirects it — "no, use the other
API", "skip the tests", "actually do it this way". The work continues, and it
continues towards something the dispatched goal does not describe. That is not a
failure of the job; the human may well be right, and the result may well be
better. It is a **downgrade of what `delegate` can claim**: the merge brief
otherwise says a job was dispatched with this goal and this scope and these
checks, and a steered job answers a goal nobody wrote down.

The brief reports the second and cannot yet tell it from the first, so it counts
both and says so in the row. That is the honest version of what the transcript
supports.

## What ships (the transcript diff)

`machine.foreign_input(transcript, sent)`.

`delegate` knows every prompt it has sent a session — `Session.sent`, appended
by `machine._sent` before each `prompt`/`follow_up`. On every read, the pane's
scrollback is scanned for input lines, and any input line that is not one of
ours came from somebody else. The count and any clock stamps land per subtask as
`human_turns: {count, at}` in `task.json`, and `brief._by_job` renders a column
plus a note. Nothing is rejected, retried, or reverted — same contract as
`file_scope`.

Only pane reads are scanned. `HerdrAdapter.prompt` marks its result
`pane_transcript: True`; no other adapter does, and no other adapter should.
A subprocess run's `output` is the agent's own prose, where a line beginning `>`
is a markdown blockquote and not somebody talking — counting there would
manufacture human turns out of ordinary writing, which is the same mistake
`runtime._result` documents at length for quota classification.

### Known limits

- **It depends on the pane transcript distinguishing input from output.** The
  scan keys on a terminal prompt marker (`>`, `›`, `❯`) at the start of a line.
  A CLI that renders user input without one is invisible to it, and a CLI whose
  *output* uses one is noise. Both are per-`agent_kind` facts nothing here
  models.
- **It undercounts, on purpose.** A line is credited to `delegate` if it matches
  a line of a sent prompt or appears anywhere inside one, because a terminal may
  echo a multi-line prompt whole, wrapped, or a line at a time and the
  scrollback does not say which. A human who pastes back a fragment of the brief
  is therefore not counted. The bias is the one `quota.classify` takes: missing
  a steered job under-reports it, while inventing one puts a human-interference
  warning on a run nobody touched, and a caveat that fires on clean runs is a
  caveat nobody reads.
- **The read is a rolling window.** `agent read --source recent-unwrapped
  --lines 400` returns the same scrollback every turn, so the same line would be
  counted once per read. `Session.counted` dedupes within a session; a failover
  starts a fresh session on a fresh pane, so the window cannot outlive it.
- **Long jobs can lose the beginning.** 400 lines is the window. A human turn
  that has scrolled out before the next read is gone, and nothing knows it was
  there.
- **It cannot separate unblock from steer.** The transcript carries the lines,
  not the session state each line arrived in. Whether the agent was blocked at
  that moment is knowable to herdr and not to the scrollback, so the blocked
  whitelist described above is **design only** — it is not implemented, and
  faking it (say, by guessing from surrounding text) would put a confident
  distinction on a coin flip.

## The provenance ask

What the heuristic wants is one field, and it is a field the runtime already has
and does not hand out: **the source of each turn's input**.

Concretely, from `herdr agent read` (and from any successor exposing a pane —
`runpane`, `Pane`), per turn in the transcript:

```
{ "role": "user",
  "source": "agent_prompt" | "pane_keyboard",   # who typed it
  "at": <epoch seconds>,                        # when
  "agent_state": "working" | "blocked" }        # what the session was doing
```

- `source` replaces the whole diff. No marker regex, no subtraction against
  `Session.sent`, no undercount bias — `pane_keyboard` is the count, exactly.
  `Session.sent` and `Session.counted` both go away with it.
- `agent_state` is what splits unblock from steer, and it is the field that
  changes what the brief *says* rather than how accurately it counts. With it,
  `human_turns` becomes two numbers: turns that answered a blocked session
  (reported plainly, no flag) and turns that arrived mid-work (flagged, as
  today). Nothing else in the pipeline changes.
- `at` makes the timestamps real. Today `at` is populated only when the terminal
  happened to stamp the line, and is empty otherwise, because inventing a time
  would take away the one thing the row is for — going and looking at when it
  happened.

`delegate` would consume it in `machine._count_human_turns`, which is already
the single place this is decided: it would read the per-turn records instead of
calling `foreign_input`, and write the same `human_turns` shape with the
`blocked` half separated out. The brief, the state file and the contract
("measured, never enforced") stay as they are. That is the point of doing the
cheap version first — the seam is in the right place, and only what feeds it
changes.

## What is deliberately not here

**No provenance infrastructure.** No per-turn ledger, no signing, no attempt to
attribute a diff hunk to a typist. The question this answers is "should the
reader of the brief trust that this result maps to the goal it was given", and a
count plus a caveat answers it. Anything more would be building a record herdr
is better placed to keep.

**No enforcement.** Nothing locks a pane, refuses a steered job, or reverts its
work. A human steering an agent is usually a human who knows something the goal
did not say. The brief reports it and the reader decides — the same trade
`file_scope` makes.
