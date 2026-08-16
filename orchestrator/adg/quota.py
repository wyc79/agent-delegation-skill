"""Quota-exhaustion classification.

A provider says "you are out" in prose, and every provider says it differently.
This module turns that prose into two facts -- is this a quota wall, and when
does it reopen -- and nothing else. Pure functions over strings plus an explicit
`now`, so a classification replays identically from a log.

The bias is deliberate and one-sided. Calling a real quota failure `other` costs
one attempt. Calling a timeout `quota_exhausted` hides a working provider for
five hours, silently. So the table is narrow, timeouts are excluded ahead of it,
and anything unrecognised stays `other`.
"""

import calendar
import re
import time

# Longest cooldown we will ever believe from a provider message. Beyond this
# something was misparsed, and a month-long breaker is worse than no breaker.
MAX_COOLDOWN = 14 * 24 * 3600

# One table per agent_kind, so adding a provider is a data edit. Matched
# case-insensitively against `res["output"]`, which is whatever the adapter
# decided this table is allowed to read -- NOT the raw stream. `runtime._result`
# holds an agent's own words back: a CLI that returns a JSON envelope is
# classified on the rest of that envelope (`error`, `subtype`, `is_error`) plus
# stderr, with `result`/`output` removed, because those are definitionally the
# agent talking and not the provider. A plain-text CLI has no envelope to split
# on, so it is still classified on the whole stream; that residue is carried in
# `fixtures/provider-messages.json` as the cases marked `wrong`.
PATTERNS = {
    "claude": [
        r"usage limit reached",
        r"you(?:'ve| have) (?:reached|hit) your .{0,40}limit",
        r"\brate[ _-]?limit(?:ed|_error)?\b",
        r"\b429\b",
        r"\bquota\b.{0,40}\bexceeded\b",
    ],
    "codex": [
        r"\b429\b",
        r"\brate[ _-]?limit(?:_exceeded|ed)?\b",
        r"usage limit",
        r"\bquota\b.{0,40}\bexceeded\b",
        r"\binsufficient_quota\b",
    ],
    "gemini": [
        r"\b429\b",
        r"\bRESOURCE_EXHAUSTED\b",
        r"\bquota (?:exceeded|metric)\b",
        r"\brate[ _-]?limit(?:ed)?\b",
    ],
    "cursor": [
        r"\b429\b",
        r"\brate[ _-]?limit(?:ed)?\b",
        r"usage limit",
        r"\bout of (?:requests|credits)\b",
    ],
    # DeepSeek Harness. Narrow on purpose, and it matters more here than
    # elsewhere: `dsh --profile headless` prints the final assistant message on
    # stdout, so the adapter shows this table only stderr plus the session
    # log's structured `LlmFailure` -- see `runtime.LocalAdapter._prompt_dsh`.
    # The heavy lifting is meant to be done by `LlmFailure.code` through
    # ERROR_CODES; these are the fallback for a wall that never reaches a
    # session log, which is exactly the case a startup-time refusal produces.
    "dsh": [
        r"\b429\b",
        r"\brate[ _-]?limit(?:ed|_exceeded)?\b",
        r"usage limit",
        r"\bquota\b.{0,40}\bexceeded\b",
        r"\binsufficient_quota\b",
    ],
}

# A CLI that returns a JSON envelope can report the refusal as a code rather
# than as prose -- `runtime._result` lifts it from `error.code`. That is the
# provider talking through the CLI, so it is in scope here.
#
# This used to say "herdr reports failures as a JSON error code", and herdr
# does not: its `AgentInfo` carries no error field at all, only a liveness
# status. Nothing ever populated `error_code` on the pane path, and the pane
# path no longer classifies anything -- see `runtime.HerdrAdapter`.
ERROR_CODES = {"rate_limited", "quota_exceeded", "usage_limit_reached",
               "agent_rate_limited", "insufficient_quota"}

_COMPILED = {k: [re.compile(p, re.I) for p in v] for k, v in PATTERNS.items()}

_UNITS = {"second": 1, "sec": 1, "s": 1, "minute": 60, "min": 60, "m": 60,
          "hour": 3600, "hr": 3600, "h": 3600, "day": 86400, "d": 86400,
          "week": 604800, "w": 604800}

_WINDOWS = {"hourly": 3600, "daily": 86400, "weekly": 604800, "monthly": 2592000}

_RETRY_AFTER = re.compile(r"retry[-_ ]?after\D{0,8}(\d+)", re.I)
_RELATIVE = re.compile(
    r"(?:try again|retry|resets?|resume[sd]?|available again|wait)\D{0,24}?"
    r"(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\b", re.I)
_ISO = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?\s*(Z|UTC)?", re.I)
_CLOCK = re.compile(r"reset\w*\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
# `re.I` like every sibling above it. Without it "Resets 1786499561" -- a
# provider message that begins a sentence, which is most of them -- matched
# nothing, so a run that had been told exactly when the seat reopens fell back
# to the channel's configured window instead. The fallback is meant for a
# provider that stated nothing, and it parks the task longer than it needs to
# be parked; `resume --when-open` then waits out a window that had already
# reopened.
_EPOCH = re.compile(r"reset\w*\D{0,12}\b(1[5-9]\d{8}|2\d{9})\b", re.I)
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-z]+)\s*$", re.I)
_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)?)")


def failed(res):
    """Did this invocation fail? `settled` is the adapter's own verdict and the
    only one every adapter agrees on."""
    return (res or {}).get("settled") != "idle"


def classify(agent_kind, res, now):
    """-> (kind, reset_at). kind is 'quota_exhausted', 'other', or None when
    the invocation did not fail at all."""
    if not failed(res):
        return None, None
    # Ahead of the table, never after it: a timeout tells us nothing about the
    # provider's quota, and its output may quote a rate-limit message it was
    # merely waiting on.
    #
    # It is its own kind rather than `other`, because hopping and cooling are
    # two different decisions and a timeout wants one of them, not both. A hung
    # call should move to another seat -- the work is checkpointed and someone
    # else can pick it up, which is the whole point of enrolling more than one
    # provider. But it must NOT open a five-hour breaker: nothing here says the
    # seat is out, only that this call did not come back, and cooling on that
    # evidence hides a working provider for the rest of the afternoon.
    if (res or {}).get("settled") == "timeout":
        return "timeout", None
    text = (res or {}).get("output") or ""
    code = (res or {}).get("error_code") or ""
    hit = str(code).lower() in ERROR_CODES
    if not hit:
        for rx in _COMPILED.get(agent_kind, ()):
            if rx.search(text):
                hit = True
                break
    if not hit:
        return "other", None
    return "quota_exhausted", parse_reset(text, now)


def parse_reset(text, now):
    """When the provider says it reopens, in epoch seconds. None when it does
    not say, or says something we refuse to believe -- the caller then falls
    back to the configured window, which is the honest answer."""
    for parse in (_from_retry_after, _from_relative, _from_epoch,
                  _from_iso, _from_clock):
        at = parse(text, now)
        if at is None:
            continue
        if now < at <= now + MAX_COOLDOWN:
            return float(at)
    return None


def _from_retry_after(text, now):
    m = _RETRY_AFTER.search(text or "")
    return now + int(m.group(1)) if m else None


def _from_relative(text, now):
    m = _RELATIVE.search(text or "")
    if not m:
        return None
    unit = m.group(2).lower().rstrip("s")
    secs = _UNITS.get(unit) or _UNITS.get(unit[:3]) or 0
    return now + int(m.group(1)) * secs


def _from_epoch(text, now):
    m = _EPOCH.search(text or "")
    return float(m.group(1)) if m else None


def _from_iso(text, now):
    m = _ISO.search(text or "")
    if not m:
        return None
    parts = [int(m.group(i) or 0) for i in range(1, 7)]
    # Naive stamps are read as UTC: providers quote UTC, and guessing a local
    # zone would move the reopen time by hours in the wrong direction.
    try:
        return calendar.timegm(tuple(parts) + (0, 0, 0))
    except ValueError:
        return None


def _from_clock(text, now):
    """"resets at 3pm" -- the next occurrence of that wall-clock hour, local."""
    m = _CLOCK.search(text or "")
    if not m:
        return None
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == "pm":
        hour += 12
    lt = time.localtime(now)
    at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour,
                      int(m.group(2) or 0), 0, 0, 0, -1))
    return at + 86400 if at <= now else at


def parse_duration(text):
    """`5h`, `90m`, `7d` -> seconds, or None when it is not a duration at all.

    The strict sibling of `parse_window`, sharing its units table so the two
    cannot drift. `parse_window` reads a registry field and falls back to a
    default, which is right there: a typo in a config file must not stop a run.
    This one reads what a person just typed at the CLI, where a silent default
    would cool a seat for a length nobody asked for.
    """
    m = _DURATION.match(str(text or "").strip().lower())
    if not m:
        return None
    # Whole-word units only. A first-letter fallback silently read `1month`
    # as one *minute*, which is a 60-second window on a monthly seat --
    # wrong by four orders of magnitude and completely invisible.
    secs = _UNITS.get(m.group(2).rstrip("s"))
    if not secs:
        return None
    got = float(m.group(1)) * secs
    return got if got > 0 else None


def parse_window(text, default=5 * 3600.0):
    """`5h`, `24h`, `weekly`, `monthly` -> seconds. An unparseable window
    returns the default rather than 0, which would mean 'always exhausted' --
    and a zero window makes a breaker expire in the same write that opens it."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text) if text > 0 else float(default)
    key = str(text or "").strip().lower()
    if key in _WINDOWS:
        return float(_WINDOWS[key])
    return parse_duration(key) or float(default)


def parse_capacity(text):
    """`40`, `40u`, `500req` -> float. writes all three."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text) if text > 0 else None
    m = _NUMBER.match(str(text or ""))
    if not m:
        return None
    val = float(m.group(1))
    return val if val > 0 else None
