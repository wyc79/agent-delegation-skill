"""Deterministic capability router (DESIGN.md §5).

Pure scoring over a config file: no LLM, no network, no side effects, so a
routing decision is reproducible and explainable after the fact.

Two independent gates keep the heaviest tier out of reach (§5.3b): a model must
be *enrolled* for the role, and it must sit at or below the escalation ceiling.
Both must pass; flipping one alone does nothing.
"""

import os

from . import yamlite

TIERS = ["t1", "t2", "t3"]
_SENSITIVITY = {"low": 0.2, "medium": 0.6, "high": 1.2, "max": 2.0}
_LEVELS = {"low": 2, "medium": 3, "high": 4, "very_high": 5}


class RoutingError(RuntimeError):
    pass


class NoModelAvailable(RoutingError):
    pass


class AllChannelsCooled(NoModelAvailable):
    """Every channel that could serve this role is in a quota cooldown. A
    subclass of NoModelAvailable on purpose: optional stages that already
    degrade gracefully keep degrading, while mandatory ones propagate to the
    park handler, which knows when the seats come back."""

    def __init__(self, role, channels, reopen_at, message):
        super().__init__(message)
        self.role, self.channels, self.reopen_at = role, list(channels), reopen_at


def load_registry(path=None):
    if path is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(os.path.dirname(here), "registry.default.yaml")
    with open(path, encoding="utf-8") as fh:
        reg = yamlite.load(fh.read())
    for key in ("models", "profiles", "channels", "policy"):
        if key not in reg:
            raise RoutingError("registry missing '%s' (%s)" % (key, path))
    return reg


class Choice:
    def __init__(self, model, channel, spec, chan_spec, score, utilization=0.0,
                 demoted=False):
        self.model, self.channel = model, channel
        self.spec, self.chan_spec, self.score = spec, chan_spec, score
        # Draw on this channel's quota window at selection time, and whether the
        # channel's own reservation would normally have excluded this role.
        self.utilization, self.demoted = utilization, demoted

    @property
    def adapter(self):
        return self.chan_spec.get("adapter", "local")

    @property
    def agent_kind(self):
        return self.chan_spec.get("agent_kind", "claude")

    @property
    def tier(self):
        return self.spec.get("tier", "t1")

    def __repr__(self):
        return "<%s via %s (%s/%s) score=%.2f>" % (
            self.model, self.channel, self.adapter, self.agent_kind, self.score)


class Router:
    def __init__(self, registry):
        self.reg = registry

    # --- gates ------------------------------------------------------------
    def _enrolled(self, spec, role):
        return role in (spec.get("enrolled_roles") or [])

    def _within_ceiling(self, spec, ceiling):
        max_tier = (ceiling or {}).get("max_tier", "t3")
        if max_tier not in TIERS:
            raise RoutingError("bad max_tier %r; refusing to route" % max_tier)
        return TIERS.index(spec.get("tier", "t1")) <= TIERS.index(max_tier)

    def _meets(self, spec, require, boost):
        for cap, need in (require or {}).items():
            have = spec.get(cap)
            if have is None:
                return False
            want = _LEVELS.get(need, need) if isinstance(need, str) else need
            if cap in boost:
                want = max(want, boost[cap])
            if have < want:
                return False
        return True

    # --- scoring ----------------------------------------------------------
    def _cost(self, spec, chan, utilization=0.0):
        """Marginal cost (§5.4). A subscription seat with headroom is ~free, so
        list price is the wrong signal for it -- but a prepaid seat is not free,
        it is *capped*, so its effective price rises with how much of the window
        is already gone:

            effective = list x u^2/(1-u)

        f(0)=0 (free capability is capability), f(0.5)=0.5, f(0.9)=8.1 -- past
        roughly 80% drawn the seat prices itself above a metered key, which is
        exactly the drift we want before a hard wall arrives."""
        list_cost = float(spec.get("cost_out", 1.0))
        if chan.get("type") != "subscription":
            return list_cost
        u = min(max(float(utilization or 0.0), 0.0), 0.99)
        return list_cost * (u * u) / (1.0 - u)

    def _reserved_out(self, chan, role, utilization):
        """Does this channel's headroom reservation exclude this role right now?
        (§5.4: 'keep >=30% of the strong window free for planner/reviewer'.)"""
        roles = chan.get("reserve_for") or []
        raw = chan.get("reserve_fraction")
        if not roles or raw is None or role in roles:
            return False
        try:
            frac = float(raw)
        except (TypeError, ValueError):
            raise RoutingError(
                "reserve_fraction is %r, expected a number between 0 and 1; "
                "fix it in the registry rather than routing on a guess" % (raw,))
        if not 0.0 <= frac < 1.0:
            raise RoutingError(
                "reserve_fraction is %r, must be >= 0 and < 1" % (raw,))
        return float(utilization or 0.0) > (1.0 - frac)

    def _score(self, spec, chan, profile, utilization=0.0):
        weights = profile.get("weights") or {}
        base = sum(float(w) * float(spec.get(cap, 0)) for cap, w in weights.items())
        lam = _SENSITIVITY.get(profile.get("cost_sensitivity", "medium"), 0.6)
        import math
        return base - lam * math.log1p(self._cost(spec, chan, utilization))

    # --- selection --------------------------------------------------------
    def candidates(self, role, ceiling=None, boost=None, cooldowns=(),
                   utilization=None):
        """All (model, channel) pairs legal for this role, best first.

        `cooldowns` is a set of channel names to filter exactly like
        `disabled: true`; `utilization` maps channel name -> 0.0-1.0 draw on its
        quota window. Both are computed by the caller from one `now`, so this
        method reads no clock and a routing decision replays from logged state.
        """
        profile = self.reg["profiles"].get(role)
        if profile is None:
            raise RoutingError("no capability profile for role %r" % role)
        ceiling = ceiling if ceiling is not None else self.reg["policy"].get("escalation_ceiling", {})
        boost = boost or {}
        cooldowns = set(cooldowns or ())
        util = utilization or {}
        out, reserved = [], []
        for cname, chan in (self.reg["channels"] or {}).items():
            if chan.get("disabled") or cname in cooldowns:
                continue
            u = float(util.get(cname, 0.0))
            held = self._reserved_out(chan, role, u)
            for mname in chan.get("exposes") or []:
                spec = self.reg["models"].get(mname)
                if spec is None:
                    continue
                if not self._enrolled(spec, role):
                    continue
                if not self._within_ceiling(spec, ceiling):
                    continue
                if not self._meets(spec, profile.get("require"), boost):
                    continue
                (reserved if held else out).append(
                    Choice(mname, cname, spec, chan,
                           self._score(spec, chan, profile, u), u, held))
        # A reservation that would leave the role with nothing is a reservation
        # that parks work it could have done. Hand the seat back, flagged, and
        # let the machine log the demotion.
        chosen = out or reserved
        chosen.sort(key=lambda c: (-c.score, c.model, c.channel))
        return chosen

    def select(self, role, ceiling=None, boost=None, exclude=(), cooldowns=(),
               utilization=None):
        """Best legal choice. The remaining sorted list is the fallback chain."""
        for c in self.candidates(role, ceiling, boost, cooldowns, utilization):
            if (c.model, c.channel) not in exclude and c.model not in exclude:
                return c
        raise NoModelAvailable(
            "no enrolled model for role %r within the ceiling (excluded: %s). "
            "Enroll a model in registry.default.yaml or raise the ceiling -- "
            "both are deliberate human edits." % (role, list(exclude)))

    def escalate(self, role, current, ceiling=None, cooldowns=(), utilization=None):
        """One rung up: require stronger reasoning, still clamped to the
        ceiling. Returns None when nothing legal is stronger, which is the
        signal to stop climbing and replan instead of burning budget."""
        cur_reasoning = int(current.spec.get("reasoning", 0)) if current else 0
        try:
            pick = self.select(role, ceiling, boost={"reasoning": cur_reasoning + 1},
                               exclude=((current.model, current.channel),) if current else (),
                               cooldowns=cooldowns, utilization=utilization)
        except NoModelAvailable:
            return None
        if current and pick.spec.get("reasoning", 0) <= cur_reasoning:
            return None
        return pick
