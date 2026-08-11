"""Per-channel circuit breakers and the invocation meter.

State lives in `$XDG_STATE_HOME/agent-delegation/channels.json`, beside
`projects/` rather than inside one: a quota belongs to a *seat*, not to a
repository, so two projects driving the same subscription must see one breaker.

Every write is read-modify-merge under a lock and then an atomic replace. Merge
rather than overwrite is the point for the wave threads inside one process,
where the lock makes it exact.

Across *processes* the lock does nothing and there is deliberately no file lock:
the re-read before each write narrows the lost-update window, it does not close
it. That is a tolerated weakness rather than an overlooked one, because both
outcomes are self-correcting -- a lost `open_breaker` is re-opened by the next
call to that seat, from the provider's own answer, and a lost `clear` can simply
be run again. A stale lock file on a network home directory would be a worse
failure than either.

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
_LAST_ERROR = None

# Longest window any channel could declare, used to bound the usage log when a
# caller does not say. Stamps beyond it cannot affect any utilisation figure.
_MAX_WINDOW = 31 * 24 * 3600


def path():
    return os.path.join(state_root(), "channels.json")


def read(now):
    """-> (cooldowns, usage, warning). Expired breakers are already dropped, so
    a caller never has to remember to check the clock twice."""
    data, warning = _raw()
    dropped = 0
    try:
        raw_cools = data.get("cooldowns") or {}
        cools = {}
        for name, e in raw_cools.items():
            if not _entry_ok(e):
                dropped += 1              # unreadable entry: we are about to
                continue                  # treat a cooled seat as available
            if float(e["reopen_at"]) > now:
                cools[name] = e
        usage = {}
        for name, stamps in (data.get("usage") or {}).items():
            if not isinstance(stamps, list):
                dropped += 1
                continue
            usage[name] = [float(s) for s in stamps if _number(s)]
            dropped += len(stamps) - len(usage[name])
    except (AttributeError, TypeError, ValueError) as e:
        # Valid JSON of the wrong *shape* -- a hand-edit, or a future version of
        # this file. The top-level dict check does not catch it, and the promise
        # at the top of this module is that nothing here is ever fatal.
        return {}, {}, ("%s is the wrong shape (%s) -- continuing with no "
                        "cooldowns. Delete it to start clean." % (path(), e))
    if dropped and not warning:
        # Dropping a breaker fails *open* -- it makes a cooled seat look
        # available. Never do that quietly.
        warning = ("%s has %d unreadable entr%s, ignored -- a cooldown recorded "
                   "there is not being honoured. Delete it to start clean."
                   % (path(), dropped, "y" if dropped == 1 else "ies"))
    return cools, usage, warning


def active(now):
    return set(read(now)[0])


def open_breaker(channel, reason, reopen_at, now, detail=""):
    """Open (or extend) a breaker. Extending never shortens an existing one:
    two providers' messages disagreeing should not reopen a seat early."""
    entry = {"reason": reason, "opened_at": float(now),
             "reopen_at": float(reopen_at), "detail": (detail or "")[:200]}

    def apply(data):
        cur = data["cooldowns"].get(channel)
        if _entry_ok(cur) and float(cur["reopen_at"]) > entry["reopen_at"]:
            entry["reopen_at"] = float(cur["reopen_at"])
        data["cooldowns"][channel] = entry

    return _mutate(apply, now)


def clear(channel):
    """-> True when something was actually removed, so the CLI can say so.

    Goes through `_mutate` like every other write: a hand-rolled
    read-modify-write here skipped the version default, the prune, and the
    re-read that lets a concurrent writer's entry survive."""
    with _LOCK:
        cools, _, _ = read(float("-inf"))     # -inf: expiry is not the question
        if channel not in cools:
            return False                      # a no-op should not conjure a file
        _mutate(lambda data: data["cooldowns"].pop(channel, None), None)
        return True


def record_use(channel, window_seconds, now):
    """One invocation, one unit. Providers expose no meter, so this is an
    estimate by construction -- it exists to bias routing before a wall is hit,
    not to be right to the call."""
    cutoff = float(now) - min(float(window_seconds or _MAX_WINDOW), _MAX_WINDOW)

    def apply(data):
        stamps = data["usage"].get(channel)
        stamps = list(stamps) if isinstance(stamps, list) else []
        stamps.append(float(now))
        data["usage"][channel] = [float(s) for s in stamps
                                  if _number(s) and float(s) >= cutoff]

    return _mutate(apply, now)


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

def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _entry_ok(entry):
    if not isinstance(entry, dict):
        return False
    try:
        float(entry["reopen_at"])
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
    """Apply fn to the file's contents. Returns None on success, else a warning.

    Never raises. This file is advisory: a run whose breaker cannot be persisted
    must keep going -- the failover loop excludes the dead channel in memory for
    the rest of the invocation regardless -- but it must not pretend the write
    happened, so the caller gets a string to log."""
    global _LAST_ERROR
    with _LOCK:
        data, _ = _raw()                      # re-read: another writer may have won
        if not isinstance(data.get("cooldowns"), dict):
            data["cooldowns"] = {}            # recover a wrongly-shaped file
        if not isinstance(data.get("usage"), dict):
            data["usage"] = {}
        data.setdefault("version", VERSION)
        try:
            fn(data)
            if now is not None:               # prune on write, never on read
                data["cooldowns"] = {
                    n: e for n, e in data["cooldowns"].items()
                    if _entry_ok(e) and float(e["reopen_at"]) > float(now)}
            atomic_write(path(), json.dumps(data, indent=2, sort_keys=True) + "\n")
        except (AttributeError, TypeError, ValueError) as e:
            _LAST_ERROR = "%s could not be updated (%s)" % (path(), e)
        except OSError as e:
            # A read-only or full state dir. Ordinary in a CI sandbox, and
            # `_meter` runs after every single invocation -- raising here would
            # kill a run that has nothing wrong with it.
            _LAST_ERROR = ("%s is not writable (%s) -- quota cooldowns will not "
                           "persist across runs." % (path(), e))
        else:
            _LAST_ERROR = None
        return _LAST_ERROR


def last_error():
    """The most recent write failure, or None. Callers log it; nothing retries,
    because the next write attempt is the retry."""
    return _LAST_ERROR
