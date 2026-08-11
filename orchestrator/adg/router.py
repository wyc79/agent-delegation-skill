"""Deterministic capability router.

Pure scoring over a config file: no LLM, no network, no side effects, so a
routing decision is reproducible and explainable after the fact.

Two independent gates keep the heaviest tier out of reach (b): a model must
be *enrolled* for the role, and it must sit at or below the escalation ceiling.
Both must pass; flipping one alone does nothing.
"""

import os

from . import yamlite

TIERS = ["t1", "t2", "t3"]
_SENSITIVITY = {"low": 0.2, "medium": 0.6, "high": 1.2, "max": 2.0}
_LEVELS = {"low": 2, "medium": 3, "high": 4, "very_high": 5}

# What a channel's `prefers:` is worth when two seats expose the same model.
#
# Deliberately smaller than 1.0, which is the smallest gap two DIFFERENT
# capability scores can have: capabilities are integers 1-5 and the shipped
# weights are integers, so a base score that differs at all differs by at least
# one. A preference can therefore only ever decide between seats the router
# already rates equally -- it can never buy a weaker model a job.
#
# It exists because that decision was previously alphabetical. Two subscription
# seats with headroom both price at ~0, so `balanced-coder` on claude-seat and
# on cursor-seat scored identically and the tie fell to `sort(... c.channel)`:
# "claude-seat" < "cursor-seat", so every implementer went to Claude and a
# deployment with two providers ran as if it had one. An arbitrary rule that
# looks like a policy is worse than no rule, so the policy is now written down.
PREFERENCE_BONUS = 0.5


class RoutingError(RuntimeError):
    pass


def as_boost(hint):
    """A plan's `capability_hint` as a `boost` mapping, or {}.

    The planner writes difficulty in words (`{reasoning: high}`); `_meets` reads
    floors as numbers. Unknown capabilities and unknown words are dropped rather
    than guessed at -- a hint is the planner's read, and a typo in it must not be
    able to raise a floor nothing can clear.
    """
    out = {}
    for cap, level in (hint or {}).items():
        if isinstance(level, bool):
            continue
        if isinstance(level, str):
            level = _LEVELS.get(level.lower())
        if isinstance(level, (int, float)):
            out[cap] = int(level)
    return out


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
    # A `prefers:` naming something the channel does not expose is a typo that
    # would route exactly as if it were absent -- the shape of mistake this
    # registry already refuses elsewhere, because a line that reads like a
    # policy and is enforced by nothing is worse than no line. Checked here
    # rather than in `candidates`, so `--registry` fails on load with the path
    # in the message instead of three stages into a run.
    for cname, chan in (reg["channels"] or {}).items():
        if not isinstance(chan, dict):
            continue
        exposes = chan.get("exposes") or []
        unknown = [m for m in (chan.get("prefers") or []) if m not in exposes]
        if unknown:
            raise RoutingError(
                "channel %r prefers %s, which it does not expose (%s) — a "
                "preference for a model the seat cannot serve routes nothing "
                "(%s)" % (cname, ", ".join(map(repr, unknown)),
                          ", ".join(exposes) or "nothing", path))
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
        """Does this model clear the role's floors, as raised by `boost`?

        Both key sets, not just `require`. A boost is how the ladder asks for
        something the profile did not think to demand, so iterating `require`
        alone dropped every boost on an undeclared capability -- silently, and
        exactly where it mattered: `escalate` boosts `reasoning`, and neither
        the implementer nor the test-author profile requires it. Rung 2 was
        therefore never legal for the two roles that actually climb it,
        `escalate` returned None, and the caller told an operator that nothing
        stronger was enrolled when they had just enrolled it.

        Sorted so a refusal is decided by the same capability every run; the
        answer is order-independent, but which floor it trips on is not.
        """
        require = require or {}
        boost = boost or {}
        for cap in sorted(set(require) | set(boost)):
            have = spec.get(cap)
            # Unscored is unverifiable, and a raised floor is not the place to
            # give a model the benefit of the doubt.
            if have is None:
                return False
            need = require.get(cap)
            want = 0 if need is None else (
                _LEVELS.get(need, need) if isinstance(need, str) else need)
            if cap in boost:
                want = max(want, boost[cap])
            if have < want:
                return False
        return True

    # --- scoring ----------------------------------------------------------
    def _cost(self, spec, chan, utilization=0.0):
        """Marginal cost. A subscription seat with headroom is ~free, so
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
        (: 'keep >=30% of the strong window free for planner/reviewer'.)"""
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

    def _score(self, spec, chan, profile, utilization=0.0, model=None):
        weights = profile.get("weights") or {}
        base = sum(float(w) * float(spec.get(cap, 0)) for cap, w in weights.items())
        lam = _SENSITIVITY.get(profile.get("cost_sensitivity", "medium"), 0.6)
        import math
        score = base - lam * math.log1p(self._cost(spec, chan, utilization))
        if model is not None and model in (chan.get("prefers") or []):
            score += PREFERENCE_BONUS
        return score

    # --- selection --------------------------------------------------------
    def candidates(self, role, ceiling=None, boost=None, cooldowns=(),
                   utilization=None, ignore_reserve=False):
        """All (model, channel) pairs legal for this role, best first.

        `cooldowns` is a set of channel names to filter exactly like
        `disabled: true`; `utilization` maps channel name -> 0.0-1.0 draw on its
        quota window. Both are computed by the caller from one `now`, so this
        method reads no clock and a routing decision replays from logged state.

        `ignore_reserve` returns the seats a channel's headroom reservation
        would normally withhold, flagged `demoted`. The caller asks for it when
        its own filters -- which model is actually installed -- leave nothing
        runnable, because only the caller knows that.
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
            legal = []
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
                legal.append((mname, spec))
            if not legal:
                continue
            # Only now: a malformed reserve_fraction on a channel this role can
            # never use is not this role's problem, and raising for it would
            # crash-park a run over a config error somewhere else entirely.
            held = self._reserved_out(chan, role, u)
            for mname, spec in legal:
                (reserved if held else out).append(
                    Choice(mname, cname, spec, chan,
                           self._score(spec, chan, profile, u, mname), u, held))
        # A reservation that would leave the role with nothing is a reservation
        # that parks work it could have done -- but "nothing" is not decidable
        # here: the caller still filters by which CLI is installed. So return the
        # withheld seats only when asked, and let the caller decide.
        chosen = reserved if ignore_reserve else out
        chosen.sort(key=lambda c: (-c.score, c.model, c.channel))
        return chosen

    def select(self, role, ceiling=None, boost=None, exclude=(), cooldowns=(),
               utilization=None):
        """Best legal choice. The remaining sorted list is the fallback chain.

        Reserved seats are considered only once `exclude` has emptied the
        unreserved ones -- the same "withhold unless it would leave nothing"
        rule `_pick` applies, evaluated after this method's own filter."""
        for reserved_pass in (False, True):
            for c in self.candidates(role, ceiling, boost, cooldowns, utilization,
                                     ignore_reserve=reserved_pass):
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
