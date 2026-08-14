# dsh (DeepSeek Harness) — what was verified before the adapter was written

`dsh` promises COMPATIBILITY-BREAKING CHANGES, so everything here is pinned to
one version and every claim says how it was established. Nothing in this file
is inferred from the announcement or from how other harnesses behave.

**Pinned version: `@deepseek-ai/dsh@0.1.0-rc.6`** — installed locally into a
scratch directory (`npm install @deepseek-ai/dsh`, 531 packages) and run as
`./node_modules/.bin/dsh --version`. Note this is a release candidate, not the
`v0.1` the brief names; the adapter is written against rc.6 and says so.

Method: `dsh --help`, `dsh --profile headless --help`, `dsh --profile headless
--dump-config`, and the TypeScript declaration files shipped inside the
published packages (`node_modules/@deepseek-ai/dsh-*/lib/types/*.d.ts`). Those
declarations are the package's own contract, not documentation about it.

## Verified vs assumed

| # | Point | Status | How |
|---|---|---|---|
| a | Headless has **no** quiet/JSON/structured-output flag | **verified** | `dsh --profile headless --help` lists exactly `-h, --help`. Structure must come from the session store. |
| b1 | `$DSH_HOME` overrides; default `~/.dsh` | **verified** | `dsh-home-paths`: `DSH_HOME_DIR_NAME = ".dsh"`, `resolveDshHome` precedence "explicit configured path, `$DSH_HOME`, then `~/.dsh`". |
| b2 | Session root is `$DSH_HOME/sessions` | **verified** | `dsh --profile headless --dump-config` → `session-persistence-jsonl` with `config.root: dshHomePath('sessions')`. |
| b3 | Layout `<root>/<projectKey(cwd)>/<sessionId>/session.jsonl[.zstd]` | **verified** | `dsh-session-persistence-jsonl/lib/index.js`: `projectDir`, `sessionDir`, `logPath`. `projectKey` lowercases nothing, replaces `/ \ :` runs with `-`, `~XXXX`-escapes the rest, and wraps in `--…--`. |
| b4 | **Default compression is checksummed Zstandard** (`session.jsonl.zstd`) | **verified** | `Config.compression` — "Physical encoding; defaults to checksummed Zstandard frames". The shipped headless profile sets no override. |
| b5 | `packChunks` defaults **true**: runs of `assistant/chunk` are stored as packed `text-chunks`/`reasoning-chunks`/`tool-call-chunks` rows | **verified** | `Config.packChunks` docstring. A line reader therefore meets rows that are not `SessionEvent`s. |
| b6 | Usage/cost events exist — **tokens only, no money** | **verified** | `SessionEventMap['assistant/message']` carries `usage?: TokenUsage`; `TokenUsage = {inputTokens, outputTokens, cacheReadTokens?, cacheWriteTokens?, reasoningTokens?}`. No price field anywhere in `dsh-llm`. |
| b7 | Structured error objects exist, with a retry-after | **verified** | `turn/end.data.reason` is a `TurnEndReason` union; its `error` variant is `{kind:'error', error: LlmFailure}` and `LlmFailure = {message, code, status?, providerRetryAfterMs?, requestId?}`. |
| b8 | Per-event origin exists (user vs agent vs tool) | **verified** | `user/message` is an `llm.UserMessage` whose `source: MessageSource` has `kind: 'user' \| 'plugin' \| 'model' \| 'tool'`. The event doc says a direct human prompt, an `agent.inject()` context and a goal continuation "all project their `content` verbatim; `source` tells them apart". |
| c | Exit code is non-zero on an invalid profile and on a startup failure | **verified** | `dsh --profile nosuchprofile "hi"` → exit 1; `dsh --profile headless "say hi"` with an unbootable plugin tree → exit 1, message on stderr, stdout empty. |
| — | A completed headless run writes a session log | **ASSUMED** | Never observed. See below. |
| — | The on-disk `projectKey` spelling for a real cwd | **ASSUMED** | Derived from the shipped `projectKey` source, never seen on disk. |
| — | That a real provider wall surfaces as `turn/end` `error` with a quota `code` | **ASSUMED** | The type says `LlmFailure.code` is a "stable provider-neutral machine-routing code"; its vocabulary is not enumerated anywhere in the package, and no run has produced one. |

### Why the run half is unverified

There is no DeepSeek API key in this environment. `dsh --profile headless "say
hi"` bootstrapped `$DSH_HOME/profiles/headless` and installed its plugin tree,
then failed before reaching a model:

    Error: dsh: plugin tree failed to load: failed to apply loader entry
    include (cordis:include): loader entries failed to apply

(The tree includes a native addon, `node-addon-landlock-run`, which is the
likely cause under this sandbox.) So no session directory was ever written, and
every claim above about a *completed* run is marked assumed. The event schema
itself is not assumed — it is read from shipped declarations.

## The constraint that shapes the adapter: zstd

The default artifact is `session.jsonl.zstd`. **Python 3.13's standard library
cannot read Zstandard** — `compression.zstd` lands in 3.14 — and this project is
stdlib-only, including tests. So:

* the enrichment parser reads plaintext `session.jsonl` only;
* a `.zstd` artifact is detected and skipped with a reason, not decoded, and not
  crashed on;
* a deployment that wants enrichment must set `compression: none` on the
  `session-persistence-jsonl` plugin in its headless profile.

That is recorded here rather than worked around, because the alternative — a
vendored decompressor or a new dependency — buys a best-effort enrichment at the
cost of a house rule.

## What the adapter does

**The floor** (always): `dsh --profile headless "<prompt>"` in the subtask
worktree, settle on exit code, stdout captured as `output`.

Classification reads **stderr only**. Never the printed answer: headless prints
the final assistant message, which is the agent's own prose, and reading it as
the provider is the failure `runtime._result` and `HerdrAdapter` both document.
Same asymmetry as the herdr item — a false positive is a multi-hour breaker on a
healthy seat, a missed wall is one failed attempt.

**The enrichment** (best-effort, only what is verified above): after settle, the
run's session log is located and parsed for

* `assistant/message.usage` → token counts, which feed the spend rows through
  the same `_bill` path every other adapter uses (tokens priced from the
  registry, marked estimated — dsh reports no money);
* `turn/end.reason.error` → an `LlmFailure`, handed to classification as the
  structured probe under the same adapter-decides-what-the-table-reads contract.
  `providerRetryAfterMs` is a *stated* reset time, which is strictly better than
  parsing prose;
* `user/message.source.kind` → per-event origin.

Every parse is wrapped so that schema drift, a `.zstd` artifact, a missing
store, or a torn tail degrades to the exit-code floor. Never to a crash.

### dsh may answer the provenance ask natively

`orchestrator/docs/pane-mode-human-input.md` asks a harness for one field:
per-turn input source, so `delegate` can stop guessing which turns a human
typed. dsh has it already — `user/message.source.kind` distinguishes `user` (a
direct human prompt) from `plugin` (an `agent.inject()` context). A dsh seat can
therefore report `human_turns` exactly rather than by transcript diff, and it is
the first seat kind that could.

Not wired up yet: the field is verified in the schema and unverified on disk,
and `human_turns` currently means "counted from a pane transcript". Wiring a
second, differently-derived meaning into one number before anyone has seen a
real dsh session log would make the brief's row mean two things at once.
