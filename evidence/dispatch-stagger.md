# Dispatch order on a single seat — the stagger measurement

Where step 2 of `agent-delegation/references/one-seat.md` comes from. The recipe
carries the rule; this file carries the derivation, so a reader following the
recipe is not made to sit through an argument it has already won.

The three runs are arms **N**, **O** and **P** of the four-job rasterizer task.
`RESULTS.md` has them in context — what each arm was, what it scored, and the
rest of what they settled; this is the dispatch-order part of it, as it read in
`one-seat.md` before the recipe was cut back to the rule.

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

Run 1 is N, run 2 is O, run 3 is P. The raw records for the last two are
`arm-o-results.json` and `arm-p-results.json` beside this file.
