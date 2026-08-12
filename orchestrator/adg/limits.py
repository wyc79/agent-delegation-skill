"""Hard limits, enforced before the action that would consume them.

Fails closed by construction: a limit that is missing, unparseable, or out of
range raises rather than defaulting to "unlimited". Agents never read or write
these -- every counter is incremented by the orchestrator from observed events,
so a limit cannot be negotiated away in a prompt.
"""

# Every limit here is checked before the action that would consume it, and
# every one of them is consumed by something. `max_review_loops` and
# `max_replans` were required here long after the loops they bounded were
# removed: a run refused to start without two numbers nothing counted, and the
# budget line reported "review loops 0/2" for machinery that no longer existed.
# A limit that cannot bind is not a limit, and requiring one is worse than not
# having it -- it reads as a bound on a deployment that has none.
REQUIRED = (
    "max_cost_usd",
    "max_attempts_per_subtask",
    "max_parallel_agents",
)


class LimitBreach(Exception):
    """A limit would be exceeded. Carries what to do about it."""

    def __init__(self, limit, message, action):
        super().__init__(message)
        self.limit, self.action = limit, action


class LimitsInvalid(Exception):
    """Limits could not be trusted, so nothing may run (fail closed)."""


def validate(limits):
    if not isinstance(limits, dict):
        raise LimitsInvalid("limits block missing or not a mapping")
    for key in REQUIRED:
        if key not in limits:
            raise LimitsInvalid("limit %r missing -- refusing to run unbounded" % key)
        val = limits[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise LimitsInvalid("limit %r is %r, expected a number" % (key, val))
        if val <= 0:
            raise LimitsInvalid("limit %r is %r, must be > 0" % (key, val))
    approvals = limits.get("human_approval_required", [])
    if not isinstance(approvals, list):
        raise LimitsInvalid("human_approval_required must be a list")
    return True


def merge(defaults, overrides):
    """Compose downward only: a task may lower a deployment default, never
    raise it (b). Silently ignoring an attempted raise would hide intent,
    so it is clamped and reported by the caller reading the returned notes."""
    out = dict(defaults or {})
    notes = []
    for key, val in (overrides or {}).items():
        if key not in out:
            out[key] = val
            continue
        if isinstance(val, (int, float)) and not isinstance(val, bool) \
                and isinstance(out[key], (int, float)):
            if val > out[key]:
                notes.append("%s: %s requested, clamped to deployment max %s"
                             % (key, val, out[key]))
                continue
        out[key] = val
    return out, notes


class Budget:
    """Reads limits + spent from task.json and answers 'may I do this?'."""

    def __init__(self, task):
        self.task = task
        state = task.state
        self.limits = state.get("limits")
        validate(self.limits)
        self.spent = state.get("spent") or {}

    # --- checks (call BEFORE the action) ----------------------------------
    def check_cost(self, projected_usd=0.0):
        spent = float(self.spent.get("usd", 0.0))
        total = spent + float(projected_usd)
        cap = float(self.limits["max_cost_usd"])
        # Already at the cap is a breach even with no estimate for the next
        # call: without this, a zero projection lets an exhausted budget run
        # forever.
        if spent >= cap or total > cap:
            raise LimitBreach(
                "max_cost_usd",
                "would spend $%.2f against a $%.2f cap" % (total, cap),
                "park")

    def check_attempt(self, subtask):
        used = int((self.spent.get("attempts") or {}).get(subtask, 0))
        cap = int(self.limits["max_attempts_per_subtask"])
        if used >= cap:
            raise LimitBreach(
                "max_attempts_per_subtask",
                "subtask %s has used all %d attempts" % (subtask, cap),
                "escalate")

    # No `check_parallel` here. `max_parallel_agents` is not a
    # check-before-the-action limit like the others: nothing queues, so there is
    # nothing to raise about. `machine._wave` reads the same
    # `limits["max_parallel_agents"]` and simply stops forming the wave at the
    # cap, which is the enforcement. A second, never-called checker beside it
    # read as the enforcement and was not.

    def requires_approval(self, stage):
        return stage in (self.limits.get("human_approval_required") or [])

    def ceiling(self):
        c = self.limits.get("escalation_ceiling")
        return {"max_tier": c} if isinstance(c, str) else (c or {})

    # --- counters (call AFTER the action) ---------------------------------
    def _bump(self, fn):
        # Under the task lock: a lost increment here is a limit that fails open.
        state = self.task.mutate(lambda s: fn(s.setdefault("spent", {})))
        self.spent = state.get("spent", {})

    def spend(self, usd):
        self._bump(lambda s: s.__setitem__("usd", round(float(s.get("usd", 0.0)) + float(usd), 4)))

    def used_attempt(self, subtask):
        def f(s):
            a = s.setdefault("attempts", {})
            a[subtask] = int(a.get(subtask, 0)) + 1
        self._bump(f)

    def summary(self):
        return "$%.2f/$%.2f" % (float(self.spent.get("usd", 0.0)),
                                float(self.limits["max_cost_usd"]))
