"""Per-channel circuit breakers and the invocation meter (DESIGN.md §5.4, §5.5).

State lives in `$XDG_STATE_HOME/agent-delegation/channels.json`, beside
`projects/` rather than inside one: a quota belongs to a *seat*, not to a
repository, so two projects driving the same subscription must see one breaker.

Every write is read-modify-merge under a lock and then an atomic replace. Merge
rather than overwrite is the whole point -- wave threads and a second `delegate`
in another checkout write this file concurrently, and last-writer-wins would
drop a live breaker on the floor.

Failure here is never fatal and never invented: an unreadable file reads as "no
cooldowns" plus a warning for the caller to log. Fabricating a breaker would
hide a working provider; crashing would lose a resumable run.
"""

import json
import os
import threading

from . import quota
from .store import atomic_write, state_root

VERSION = 1
_LOCK = threading.RLock()

# Longest window any channel could declare, used to bound the usage log when a
# caller does not say. Stamps beyond it cannot affect any utilisation figure.
_MAX_WINDOW = 31 * 24 * 3600


def path():
    return os.path.join(state_root(), "channels.json")


def read(now):
    """-> (cooldowns, usage, warning). Expired breakers are already dropped, so
    a caller never has to remember to check the clock twice."""
    data, warning = _raw()
    cools = {name: e for name, e in (data.get("cooldowns") or {}).items()
             if _entry_ok(e) and float(e["reopen_at"]) > now}
    usage = {name: [float(s) for s in stamps]
             for name, stamps in (data.get("usage") or {}).items()
             if isinstance(stamps, list)}
    return cools, usage, warning


def active(now):
    return set(read(now)[0])


def open_breaker(channel, reason, reopen_at, now, detail=""):
    """Open (or extend) a breaker. Extending never shortens an existing one:
    two providers' messages disagreeing should not reopen a seat early."""
    entry = {"reason": reason, "opened_at": float(now),
             "reopen_at": float(reopen_at), "detail": (detail or "")[:200]}

    def apply(data):
        cur = (data.setdefault("cooldowns", {})).get(channel)
        if _entry_ok(cur) and float(cur["reopen_at"]) > entry["reopen_at"]:
            entry["reopen_at"] = float(cur["reopen_at"])
        data["cooldowns"][channel] = entry

    _mutate(apply, now)
    return entry


def clear(channel):
    """-> True when something was actually removed, so the CLI can say so."""
    seen = {}

    def apply(data):
        seen["hit"] = (data.get("cooldowns") or {}).pop(channel, None) is not None

    _mutate(apply, None)
    return bool(seen.get("hit"))


def record_use(channel, window_seconds, now):
    """One invocation, one unit. Providers expose no meter, so this is an
    estimate by construction -- it exists to bias routing before a wall is hit,
    not to be right to the call."""
    cutoff = float(now) - min(float(window_seconds or _MAX_WINDOW), _MAX_WINDOW)

    def apply(data):
        stamps = (data.setdefault("usage", {})).setdefault(channel, [])
        stamps.append(float(now))
        data["usage"][channel] = [float(s) for s in stamps if float(s) >= cutoff]

    _mutate(apply, now)


def utilization(stamps, quota_spec, now):
    """Draw on this channel's window, 0.0-1.0+. Unknown capacity is 0.0: no
    estimate means no shadow price, because inventing one moves routing on a
    number nobody supplied."""
    cap = quota.parse_capacity((quota_spec or {}).get("est_capacity"))
    if not cap:
        return 0.0
    window = quota.parse_window((quota_spec or {}).get("window"))
    start = float(now) - window
    return len([s for s in (stamps or []) if float(s) >= start]) / cap


def earliest_reopen(cooldowns, channels=None):
    """The soonest a member of `channels` comes back, for the park brief."""
    times = [float(e["reopen_at"]) for name, e in (cooldowns or {}).items()
             if _entry_ok(e) and (channels is None or name in channels)]
    return min(times) if times else None


# --- internals -------------------------------------------------------------

def _entry_ok(entry):
    try:
        float((entry or {})["reopen_at"])
        return True
    except (TypeError, ValueError, KeyError):
        return False


def _raw():
    """-> (data, warning). Never raises: this file is advisory, and a run that
    cannot read it must still run."""
    try:
        with open(path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        return {}, ("%s is unreadable (%s) -- continuing with no cooldowns. "
                    "Delete it to start clean." % (path(), e))
    if not isinstance(data, dict):
        return {}, ("%s does not hold an object -- continuing with no cooldowns. "
                    "Delete it to start clean." % path())
    return data, None


def _mutate(fn, now):
    with _LOCK:
        data, _ = _raw()                      # re-read: another writer may have won
        data.setdefault("version", VERSION)
        fn(data)
        if now is not None:                   # prune on write, never on read
            data["cooldowns"] = {
                n: e for n, e in (data.get("cooldowns") or {}).items()
                if _entry_ok(e) and float(e["reopen_at"]) > float(now)}
        atomic_write(path(), json.dumps(data, indent=2, sort_keys=True) + "\n")
