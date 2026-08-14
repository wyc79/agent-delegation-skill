"""Reading a DeepSeek Harness session log, best-effort.

`dsh --profile headless` prints the final assistant message and exits. That is
prose, and prose is the one thing this program refuses to hand the quota
classifier. Everything structured a dsh run produces -- what it spent, why it
failed, who typed each turn -- is in the session log it persisted, so this
module goes and reads it afterwards.

**Nothing here is load-bearing.** Every entry point returns a partial answer or
an empty one and never raises: dsh is a 0.1 release candidate that promises
compatibility-breaking changes, so schema drift is expected rather than
exceptional, and the adapter it feeds must degrade to the exit-code floor
rather than take a run down with it.

What is verified against the installed package and what is not is recorded in
orchestrator/docs/dsh-adapter-notes.md. Pinned to @deepseek-ai/dsh@0.1.0-rc.6.

The zstd problem, stated once: dsh defaults its artifact to checksummed
Zstandard frames, and Python 3.13's stdlib cannot read those (`compression.zstd`
arrives in 3.14). A deployment that wants any of this must set
`compression: none` on the `session-persistence-jsonl` plugin in its headless
profile. A `.zstd` artifact here is reported as unreadable, never decoded and
never guessed at.
"""

import json
import logging
import os
import re

_LOG = logging.getLogger("adg.dsh")

# `dsh-home-paths`: an explicit configured path wins, then $DSH_HOME, then
# ~/.dsh. The shipped headless profile puts the session root at
# `dshHomePath('sessions')`, read out of `dsh --profile headless --dump-config`.
HOME_ENV = "DSH_HOME"
HOME_DIR_NAME = ".dsh"
SESSIONS_DIR = "sessions"


def home(env=None):
    env = os.environ if env is None else env
    got = (env.get(HOME_ENV) or "").strip()
    return got or os.path.join(os.path.expanduser("~"), HOME_DIR_NAME)


def sessions_root(env=None):
    return os.path.join(home(env), SESSIONS_DIR)


# `projectKey` from dsh-session-persistence-jsonl, transcribed rather than
# guessed: runs of `/ \ :` collapse to one `-`, `[A-Za-z0-9._-]` survive, `~`
# and everything else become `~XXXX` (uppercase hex, 4 wide), leading `-` are
# stripped, the result is truncated to 251 chars and wrapped in `--…--`.
#
# Transcribed and NOT verified on disk -- no session has ever been written in
# this environment -- so `find_log` treats it as a hint and falls back to
# searching the root by modification time.
_SAFE = re.compile(r"[A-Za-z0-9._-]")


def project_key(cwd):
    if not cwd:
        return None
    out, sep_run = [], False
    for ch in cwd:
        if ch in "/\\:":
            if not sep_run:
                out.append("-")
            sep_run = True
        elif ch != "~" and _SAFE.match(ch):
            out.append(ch)
            sep_run = False
        else:
            out.append("~%04X" % ord(ch))
            sep_run = False
    readable = "".join(out).lstrip("-") or "root"
    return "--%s--" % readable[:251]


def find_log(cwd, env=None, since=None):
    """The plaintext session log for a run that happened in `cwd`.

    -> (path, note). `path` is None when there is nothing readable, and `note`
    then says why in words a run log can print.

    `since` is an epoch: a log older than it belongs to an earlier run in the
    same directory, and attributing a previous session's spend to this job is
    worse than reporting none.
    """
    root = sessions_root(env)
    key = project_key(cwd)
    dirs = []
    for candidate in (os.path.join(root, key) if key else None, root):
        if candidate and os.path.isdir(candidate):
            dirs.append(candidate)
            break
    if not dirs:
        return None, "no dsh session store at %s" % root
    best, best_at, zstd_only = None, -1.0, False
    for parent, _, names in os.walk(dirs[0]):
        for name in names:
            if name == "session.jsonl.zstd":
                zstd_only = True
                continue
            if name != "session.jsonl":
                continue
            path = os.path.join(parent, name)
            try:
                at = os.path.getmtime(path)
            except OSError:
                continue
            if since is not None and at < since:
                continue
            if at > best_at:
                best, best_at = path, at
    if best:
        return best, None
    if zstd_only:
        return None, ("the dsh session log is Zstandard-compressed, which this "
                      "reader cannot open; set `compression: none` on the "
                      "session-persistence-jsonl plugin to enable enrichment")
    return None, "no dsh session log written for this run"


def read_events(path, limit=200000):
    """Every decodable record in the log, in file order.

    Undecodable lines are skipped rather than fatal. An append-only log written
    by a live process routinely ends in a torn line, and `packChunks` (on by
    default) stores runs of stream chunks as packed rows that are not
    `SessionEvent`s at all -- neither is a reason to abandon the events that did
    parse.
    """
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError as e:
        _LOG.debug("dsh session log unreadable (%s): %s", path, e)
    return out


def usage(events):
    """Token counts summed over the session, in this program's own shape.

    dsh reports tokens and never money -- there is no price field anywhere in
    `dsh-llm` -- so a dsh seat is always billed from the registry's rates and
    always marked estimated, exactly like cursor.
    """
    got = {"in": 0, "out": 0, "fresh_in": 0, "cache_read": 0, "cache_write": 0}
    seen = False
    for ev in events:
        if ev.get("type") != "assistant/message":
            continue
        u = (ev.get("data") or {}).get("usage")
        if not isinstance(u, dict):
            continue
        seen = True

        def n(key):
            v = u.get(key)
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
        fresh, cr, cw = n("inputTokens"), n("cacheReadTokens"), n("cacheWriteTokens")
        got["fresh_in"] += fresh
        got["cache_read"] += cr
        got["cache_write"] += cw
        got["in"] += fresh + cr + cw
        got["out"] += n("outputTokens")
    if not seen or not (got["in"] or got["out"]):
        return None
    return got


def failure(events):
    """The last structured provider failure, or None.

    `turn/end` carries a `TurnEndReason`, and its `error` variant holds an
    `LlmFailure`: `{message, code, status?, providerRetryAfterMs?, requestId?}`.
    That is a machine-routing code and a provider-stated delay, which is a
    better channel than any prose table -- and it is what makes a dsh seat
    classifiable at all, since stdout is the agent's own answer.

    The last one, not the first: a turn that failed and was retried into a turn
    that succeeded should not leave the run holding the earlier error.
    """
    got = None
    for ev in events:
        if ev.get("type") != "turn/end":
            continue
        reason = (ev.get("data") or {}).get("reason")
        if not isinstance(reason, dict) or reason.get("kind") != "error":
            continue
        err = reason.get("error")
        if isinstance(err, dict):
            got = err
    return got


def human_turn_kinds(events):
    """How many `user/message` events came from each source kind.

    `source.kind` is `user` for a direct human prompt and `plugin` for an
    `agent.inject()` context, which is the per-turn provenance
    orchestrator/docs/pane-mode-human-input.md asks a harness for. Counted here
    and deliberately not yet fed to `human_turns`: that number currently means
    "lines counted off a pane transcript", and giving it a second, differently
    derived meaning before anyone has seen a real dsh log would make one row of
    the brief mean two things.
    """
    counts = {}
    for ev in events:
        if ev.get("type") != "user/message":
            continue
        src = (ev.get("data") or {}).get("source")
        kind = src.get("kind") if isinstance(src, dict) else None
        counts[kind or "unknown"] = counts.get(kind or "unknown", 0) + 1
    return counts


def enrich(res, cwd, env=None, since=None):
    """Fold whatever the session log knows into an adapter result.

    Wrapped whole. Every failure here -- a missing store, a compressed
    artifact, a schema that moved under us -- leaves `res` exactly as the
    exit-code floor produced it, which is a complete and correct result on its
    own. Returns `res` for call-site convenience.
    """
    try:
        path, note = find_log(cwd, env=env, since=since)
        if note:
            res["dsh_note"] = note
        if not path:
            return res
        events = read_events(path)
        if not events:
            res["dsh_note"] = "the dsh session log held no readable events"
            return res
        res["dsh_log"] = path
        tok = usage(events)
        if tok:
            res["usage"] = tok
        kinds = human_turn_kinds(events)
        if kinds:
            res["dsh_turn_sources"] = kinds
        err = failure(events)
        if err:
            # The structured channel, and the only text this adapter lets the
            # classifier see. `code` and `status` are the provider's machine
            # words; `message` is its prose. None of it is the agent's.
            res["error_code"] = err.get("code")
            res["dsh_failure"] = err
    except Exception as e:                      # noqa: BLE001 -- never load-bearing
        _LOG.debug("dsh enrichment failed: %s", e)
    return res


def probe_text(res):
    """What the quota table is allowed to read for a dsh result.

    stderr and the structured failure. Never stdout: headless prints the final
    assistant message, so stdout is the agent's own prose -- the exact thing
    that opens a breaker on a healthy seat when an agent's job happens to
    involve rate limits.
    """
    parts = [res.get("stderr") or ""]
    err = res.get("dsh_failure")
    if isinstance(err, dict):
        parts.append(json.dumps({k: v for k, v in err.items()
                                 if k in ("message", "code", "status",
                                          "providerRetryAfterMs")}))
    return "\n".join(p for p in parts if p)


def stated_reset(res, now):
    """`providerRetryAfterMs` as an absolute epoch, or None.

    A provider-stated delay beats every regex in `quota.parse_reset`, so it is
    offered first and the prose table is the fallback rather than the other way
    round.
    """
    err = res.get("dsh_failure")
    if not isinstance(err, dict):
        return None
    ms = err.get("providerRetryAfterMs")
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms <= 0:
        return None
    return float(now) + float(ms) / 1000.0
