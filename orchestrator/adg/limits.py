"""Hard limits, enforced before the action that would consume them (§4.2).

Fails closed by construction: a limit that is missing, unparseable, or out of
range raises rather than defaulting to "unlimited". Agents never read or write
these -- every counter is incremented by the orchestrator from observed events,
so a limit cannot be negotiated away in a prompt.
"""

REQUIRED = (
    "max_cost_usd",
    "max_attempts_per_subtask",
    "max_review_loops",
    "max_replans",
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
    raise it (§5.3b). Silently ignoring an attempted raise would hide intent,
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

    def check_review_loop(self):
        used = int(self.spent.get("review_loops", 0))
        cap = int(self.limits["max_review_loops"])
        if used >= cap:
            raise LimitBreach(
                "max_review_loops",
                "%d review loops already run" % used,
                "replan")

    def check_replan(self):
        used = int(self.spent.get("replans", 0))
        cap = int(self.limits["max_replans"])
        if used >= cap:
            raise LimitBreach(
                "max_replans",
                "already replanned %d time(s)" % used,
                "park")

    def check_parallel(self, running):
        cap = int(self.limits["max_parallel_agents"])
        if running >= cap:
            raise LimitBreach(
                "max_parallel_agents",
                "%d agents already running" % running,
                "queue")

    def requires_approval(self, stage):
        return stage in (self.limits.get("human_approval_required") or [])

    def ceiling(self):
        c = self.limits.get("escalation_ceiling")
        return {"max_tier": c} if isinstance(c, str) else (c or {})

    # --- counters (call AFTER the action) ---------------------------------
    def _bump(self, fn):
        state = self.task.state
        spent = state.setdefault("spent", {})
        fn(spent)
        self.task.write_json("task.json", state)
        self.spent = spent

    def spend(self, usd):
        self._bump(lambda s: s.__setitem__("usd", round(float(s.get("usd", 0.0)) + float(usd), 4)))

    def used_attempt(self, subtask):
        def f(s):
            a = s.setdefault("attempts", {})
            a[subtask] = int(a.get(subtask, 0)) + 1
        self._bump(f)

    def used_review_loop(self):
        self._bump(lambda s: s.__setitem__("review_loops", int(s.get("review_loops", 0)) + 1))

    def used_replan(self):
        self._bump(lambda s: s.__setitem__("replans", int(s.get("replans", 0)) + 1))

    def summary(self):
        return "$%.2f/$%.2f · review loops %d/%d · replans %d/%d" % (
            float(self.spent.get("usd", 0.0)), float(self.limits["max_cost_usd"]),
            int(self.spent.get("review_loops", 0)), int(self.limits["max_review_loops"]),
            int(self.spent.get("replans", 0)), int(self.limits["max_replans"]))
