"""Tests for quota-aware provider failover (DESIGN.md §5.4, §5.5).

Every clock is injected and every provider is a mock: this suite must never
sleep, never reach the network, and never need a vendor CLI on PATH.

Run: python3 orchestrator/tests/test_failover.py
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adg import cooldown, quota, router, store                   # noqa: E402
from test_orchestrator import REGISTRY, TempRepo, _report, sh    # noqa: E402,F401

T0 = 1754800000.0        # a fixed epoch; every test clock starts here


def blocked(text, code=1):
    """What an adapter hands back when the CLI failed."""
    return {"settled": "blocked", "output": text, "code": code}


class TestClassification(unittest.TestCase):
    """AC-5. A wrong `quota_exhausted` hides a working provider for hours, so
    the table must be narrow and everything unmatched must stay `other`."""

    def test_claude_usage_limit_is_quota(self):
        kind, _ = quota.classify("claude", blocked(
            "Claude AI usage limit reached. Your limit will reset at 3pm."), T0)
        self.assertEqual(kind, "quota_exhausted")

    def test_codex_rate_limit_is_quota(self):
        kind, _ = quota.classify("codex", blocked(
            "stream error: 429 Too Many Requests (rate_limit_exceeded)"), T0)
        self.assertEqual(kind, "quota_exhausted")

    def test_gemini_quota_is_quota(self):
        kind, _ = quota.classify("gemini", blocked(
            "[API Error: Quota exceeded for quota metric 'Generate requests'. "
            "RESOURCE_EXHAUSTED]"), T0)
        self.assertEqual(kind, "quota_exhausted")

    def test_a_stack_trace_is_other(self):
        kind, at = quota.classify("claude", blocked(
            'Traceback (most recent call last):\n  File "x.py", line 1\n'
            "ValueError: limit is not a number"), T0)
        self.assertEqual(kind, "other")
        self.assertIsNone(at)

    def test_a_timeout_is_never_quota(self):
        # The rule with the sharpest edge: a generic timeout must not open a
        # five-hour breaker on a provider that is fine.
        kind, _ = quota.classify(
            "claude", {"settled": "timeout", "output": "", "code": None}, T0)
        self.assertEqual(kind, "other")

    def test_a_timeout_mentioning_rate_limits_is_still_other(self):
        kind, _ = quota.classify("claude", {
            "settled": "timeout", "code": None,
            "output": "waiting on rate limit ..."}, T0)
        self.assertEqual(kind, "other")

    def test_success_is_not_a_failure_at_all(self):
        kind, _ = quota.classify("claude", {
            "settled": "idle", "code": 0,
            "output": "done. (note: you are near your usage limit)"}, T0)
        self.assertIsNone(kind)

    def test_an_unknown_agent_kind_falls_back_to_other(self):
        kind, _ = quota.classify("nobody-ships-this", blocked("usage limit reached"), T0)
        self.assertEqual(kind, "other")


class TestResetParsing(unittest.TestCase):
    def test_relative_minutes(self):
        at = quota.parse_reset("rate limited; try again in 45 minutes", T0)
        self.assertEqual(at, T0 + 45 * 60)

    def test_retry_after_seconds(self):
        self.assertEqual(quota.parse_reset("HTTP 429\nretry-after: 90", T0), T0 + 90)

    def test_iso_timestamp(self):
        at = quota.parse_reset("limit resets at 2025-08-10T12:00:00Z", T0)
        self.assertIsNotNone(at)

    def test_a_reset_in_the_past_is_refused(self):
        # Better to fall back to the window than to reopen a dead channel now.
        self.assertIsNone(quota.parse_reset("resets at 1999-01-01T00:00:00Z", T0))

    def test_an_absurd_reset_is_refused(self):
        self.assertIsNone(quota.parse_reset("try again in 900 days", T0))

    def test_no_time_at_all(self):
        self.assertIsNone(quota.parse_reset("usage limit reached", T0))


class TestWindowAndCapacityParsing(unittest.TestCase):
    def test_windows(self):
        self.assertEqual(quota.parse_window("5h"), 5 * 3600)
        self.assertEqual(quota.parse_window("24h"), 24 * 3600)
        self.assertEqual(quota.parse_window("weekly"), 7 * 24 * 3600)
        self.assertEqual(quota.parse_window("monthly"), 30 * 24 * 3600)

    def test_an_unparseable_window_uses_the_default_rather_than_zero(self):
        self.assertEqual(quota.parse_window("whenever", default=99.0), 99.0)

    def test_capacity_tolerates_the_units_design_md_uses(self):
        # DESIGN.md §5.4 writes these as `40u` and `500req`.
        self.assertEqual(quota.parse_capacity(40), 40.0)
        self.assertEqual(quota.parse_capacity("40u"), 40.0)
        self.assertEqual(quota.parse_capacity("500req"), 500.0)
        self.assertIsNone(quota.parse_capacity("lots"))


class TestCooldownStore(unittest.TestCase):
    """AC-2, AC-4. The file is shared across projects on purpose: a quota
    belongs to a seat, not to a repository."""

    def setUp(self):
        self.t = TempRepo()

    def tearDown(self):
        self.t.close()

    def test_it_lives_beside_projects_not_inside_one(self):
        p = cooldown.path()
        self.assertTrue(p.startswith(store.state_root()))
        self.assertNotIn("projects", p)
        self.assertNotIn(self.t.repo, p)

    def test_open_then_read_back(self):
        cooldown.open_breaker("claude-seat", "quota", T0 + 3600, T0, "usage limit")
        cools, _, warn = cooldown.read(T0 + 10)
        self.assertIsNone(warn)
        self.assertEqual(cools["claude-seat"]["reopen_at"], T0 + 3600)
        self.assertEqual(cools["claude-seat"]["reason"], "quota")
        self.assertEqual(cooldown.active(T0 + 10), {"claude-seat"})

    def test_an_expired_breaker_is_invisible_and_then_pruned(self):
        cooldown.open_breaker("claude-seat", "quota", T0 + 60, T0)
        self.assertEqual(cooldown.active(T0 + 61), set())
        cooldown.open_breaker("cursor-seat", "quota", T0 + 999, T0 + 61)
        with open(cooldown.path()) as fh:
            raw = json.load(fh)
        self.assertNotIn("claude-seat", raw["cooldowns"], "expired entry was not pruned")

    def test_clear_removes_one_and_reports_whether_it_did(self):
        cooldown.open_breaker("claude-seat", "quota", T0 + 3600, T0)
        self.assertTrue(cooldown.clear("claude-seat"))
        self.assertEqual(cooldown.active(T0 + 10), set())
        self.assertFalse(cooldown.clear("claude-seat"))

    def test_a_corrupt_file_is_empty_plus_a_warning_never_a_crash(self):
        os.makedirs(os.path.dirname(cooldown.path()), exist_ok=True)
        with open(cooldown.path(), "w") as fh:
            fh.write("{not json at all")
        cools, usage, warn = cooldown.read(T0)
        self.assertEqual(cools, {})
        self.assertEqual(usage, {})
        self.assertIsNotNone(warn)
        self.assertIn(cooldown.path(), warn, "the warning must name the file to delete")

    def test_a_missing_file_is_simply_empty_and_quiet(self):
        cools, usage, warn = cooldown.read(T0)
        self.assertEqual((cools, usage, warn), ({}, {}, None))

    def test_a_concurrent_writer_is_merged_not_clobbered(self):
        # Two projects sharing a seat write the same file. A last-writer-wins
        # implementation loses one of these silently.
        cooldown.open_breaker("claude-seat", "quota", T0 + 3600, T0)
        cooldown.open_breaker("cursor-seat", "quota", T0 + 7200, T0)
        self.assertEqual(cooldown.active(T0 + 10), {"claude-seat", "cursor-seat"})

    def test_extending_a_breaker_never_shortens_it(self):
        cooldown.open_breaker("claude-seat", "quota", T0 + 7200, T0)
        cooldown.open_breaker("claude-seat", "quota", T0 + 60, T0)
        cools, _, _ = cooldown.read(T0)
        self.assertEqual(cools["claude-seat"]["reopen_at"], T0 + 7200)

    def test_usage_is_metered_and_pruned_to_the_window(self):
        for i in range(4):
            cooldown.record_use("claude-seat", 3600, T0 + i)
        _, usage, _ = cooldown.read(T0 + 5)
        self.assertEqual(len(usage["claude-seat"]), 4)
        cooldown.record_use("claude-seat", 3600, T0 + 4000)
        _, usage, _ = cooldown.read(T0 + 4000)
        self.assertEqual(len(usage["claude-seat"]), 1, "stale stamps outlived the window")

    def test_utilization_is_calls_over_capacity_inside_the_window(self):
        spec = {"window": "5h", "est_capacity": "40u"}
        stamps = [T0 + i for i in range(10)]
        self.assertAlmostEqual(cooldown.utilization(stamps, spec, T0 + 60), 0.25)
        self.assertEqual(cooldown.utilization(stamps, spec, T0 + 6 * 3600), 0.0)

    def test_utilization_is_zero_when_capacity_is_unknown(self):
        # No estimate means no shadow price. Guessing one would move routing on
        # a number nobody supplied.
        self.assertEqual(cooldown.utilization([T0], {"window": "5h"}, T0), 0.0)
        self.assertEqual(cooldown.utilization([T0], None, T0), 0.0)

    def test_earliest_reopen_picks_the_soonest(self):
        cooldown.open_breaker("claude-seat", "quota", T0 + 7200, T0)
        cooldown.open_breaker("cursor-seat", "quota", T0 + 3600, T0)
        cools, _, _ = cooldown.read(T0)
        self.assertEqual(cooldown.earliest_reopen(cools), T0 + 3600)


class TestRouterCooldowns(unittest.TestCase):
    def setUp(self):
        self.reg = router.load_registry(REGISTRY)
        self.r = router.Router(self.reg)

    def test_a_cooled_channel_is_filtered_exactly_like_disabled(self):
        before = {c.channel for c in self.r.candidates("implementer")}
        self.assertIn("claude-seat", before)
        after = {c.channel for c in self.r.candidates(
            "implementer", cooldowns={"claude-seat"})}
        self.assertNotIn("claude-seat", after)
        self.assertIn("cursor-seat", after)

    def test_cooling_every_channel_leaves_nothing(self):
        self.assertEqual(self.r.candidates(
            "implementer", cooldowns={"claude-seat", "cursor-seat"}), [])

    def test_select_still_names_the_fix_when_nothing_is_left(self):
        with self.assertRaises(router.NoModelAvailable):
            self.r.select("implementer", cooldowns={"claude-seat", "cursor-seat"})


class TestShadowPrice(unittest.TestCase):
    """§5.4: a subscription seat with headroom is ~free; as its window fills it
    prices itself above a metered key."""

    def setUp(self):
        self.reg = router.load_registry(REGISTRY)
        self.r = router.Router(self.reg)
        self.sub = {"type": "subscription"}
        self.metered = {"type": "metered"}
        self.spec = {"cost_out": 10.0}

    def test_an_empty_seat_is_free(self):
        self.assertEqual(self.r._cost(self.spec, self.sub, 0.0), 0.0)

    def test_cost_rises_with_utilization(self):
        half = self.r._cost(self.spec, self.sub, 0.5)
        full = self.r._cost(self.spec, self.sub, 0.9)
        self.assertGreater(half, 0.0)
        self.assertGreater(full, half)

    def test_a_nearly_full_seat_costs_more_than_metered(self):
        self.assertGreater(self.r._cost(self.spec, self.sub, 0.9),
                           self.r._cost(self.spec, self.metered, 0.0))

    def test_utilization_never_divides_by_zero(self):
        self.assertGreater(self.r._cost(self.spec, self.sub, 1.0), 0.0)
        self.assertGreater(self.r._cost(self.spec, self.sub, 5.0), 0.0)

    def test_metered_ignores_utilization_entirely(self):
        self.assertEqual(self.r._cost(self.spec, self.metered, 0.9),
                         self.r._cost(self.spec, self.metered, 0.0))

    def test_a_drawn_seat_loses_a_cost_sensitive_role_to_the_emptier_one(self):
        # implementer is cost_sensitivity: high, and both seats expose
        # balanced-coder, so the only thing separating them is the window.
        drained = self.r.candidates("implementer",
                                    utilization={"claude-seat": 0.6, "cursor-seat": 0.0})
        self.assertEqual(drained[0].channel, "cursor-seat")
        fresh = self.r.candidates("implementer",
                                  utilization={"claude-seat": 0.0, "cursor-seat": 0.6})
        self.assertEqual(fresh[0].channel, "claude-seat")


class TestReserve(unittest.TestCase):
    """claude-seat reserves 30% for planner/reviewer in registry.default.yaml."""

    def setUp(self):
        self.reg = router.load_registry(REGISTRY)
        self.r = router.Router(self.reg)

    def test_a_reserved_role_keeps_the_seat_at_any_draw(self):
        got = self.r.candidates("reviewer", utilization={"claude-seat": 0.95})
        self.assertIn("claude-seat", {c.channel for c in got})

    def test_a_non_reserved_role_is_filtered_past_the_floor(self):
        got = self.r.candidates("implementer",
                                utilization={"claude-seat": 0.8, "cursor-seat": 0.0})
        self.assertNotIn("claude-seat", {c.channel for c in got})

    def test_below_the_floor_nothing_is_filtered(self):
        got = self.r.candidates("implementer",
                                utilization={"claude-seat": 0.5, "cursor-seat": 0.0})
        self.assertIn("claude-seat", {c.channel for c in got})

    def test_the_seat_comes_back_demoted_when_it_is_the_only_one(self):
        # A guarantee that parks work it could have done is not worth having.
        got = self.r.candidates("implementer",
                                utilization={"claude-seat": 0.8},
                                cooldowns={"cursor-seat"})
        self.assertEqual([c.channel for c in got], ["claude-seat"])
        self.assertTrue(got[0].demoted)

    def test_a_nonsense_reserve_fraction_refuses_rather_than_ignoring_it(self):
        reg = router.load_registry(REGISTRY)
        reg["channels"]["claude-seat"]["reserve_fraction"] = "most of it"
        with self.assertRaises(router.RoutingError) as cm:
            router.Router(reg).candidates("implementer",
                                          utilization={"claude-seat": 0.9})
        self.assertIn("reserve_fraction", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
