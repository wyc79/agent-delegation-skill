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

from adg import cooldown, quota, router, runtime, store          # noqa: E402
from adg.machine import Orchestrator                             # noqa: E402
from test_orchestrator import REGISTRY, TempRepo, _report, sh    # noqa: E402,F401

T0 = 1754800000.0        # a fixed epoch; every test clock starts here


QUOTA_MSG = "Error: Claude AI usage limit reached. Your limit will reset in 2 hours."


def blocked(text, code=1):
    """What an adapter hands back when the CLI failed."""
    return {"settled": "blocked", "output": text, "code": code}


# One row per shipped agent kind, mirroring quota.PATTERNS: adding a provider
# should be a data edit here too, not another copy of the same test body.
PROVIDER_SHAPES = [
    ("claude", "Claude AI usage limit reached. Your limit will reset at 3pm."),
    ("claude", "API Error: 429 {\"type\":\"rate_limit_error\"}"),
    ("codex", "stream error: 429 Too Many Requests (rate_limit_exceeded)"),
    ("codex", "ERROR: insufficient_quota — you have run out of usage limit"),
    ("gemini", "[API Error: Quota exceeded for quota metric 'Generate requests'. "
               "RESOURCE_EXHAUSTED]"),
    ("gemini", "GaxiosError: 429 Too Many Requests"),
    ("cursor", "You are out of requests for this billing period"),
]


class TestClassification(unittest.TestCase):
    """AC-5. A wrong `quota_exhausted` hides a working provider for hours, so
    the table must be narrow and everything unmatched must stay `other`."""

    def test_every_shipped_provider_shape_is_quota(self):
        for kind, message in PROVIDER_SHAPES:
            with self.subTest(kind=kind, message=message[:40]):
                got, _ = quota.classify(kind, blocked(message), T0)
                self.assertEqual(got, "quota_exhausted")

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
        # Deliberately not folded into the test above: this one fails the moment
        # the timeout check moves *after* the pattern table, which is the actual
        # regression to guard against.
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
        # A provider nobody wrote a table for must not inherit claude's.
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

    # Three separate rejections, not one: a stamp in the past, a stamp too far
    # ahead to believe, and no stamp at all fail at different guards, and each
    # falls back to the configured window for a different reason.

    def test_a_reset_in_the_past_is_refused(self):
        # Better to fall back to the window than to reopen a dead channel now.
        self.assertIsNone(quota.parse_reset("resets at 1999-01-01T00:00:00Z", T0))

    def test_an_absurd_reset_is_refused(self):
        # A month-long breaker from a misparse is worse than no breaker.
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


class TestRuntimeClassification(unittest.TestCase):
    """The runtime is where a failure shape becomes a fact the machine can act
    on. Classification must reach every adapter, not just the local one."""

    def test_local_result_classifies_from_stderr(self):
        res = runtime._result("", "Error: Claude AI usage limit reached", 1,
                              kind="claude", now=T0)
        self.assertEqual(res["failure"], "quota_exhausted")

    def test_local_result_leaves_a_clean_exit_alone(self):
        res = runtime._result("all good", "", 0, kind="claude", now=T0)
        self.assertIsNone(res["failure"])

    def test_a_json_result_still_sees_the_raw_stderr(self):
        # Not a duplicate of the plain-text case above: this one goes down the
        # JSON branch, where `output` is replaced by the parsed result field and
        # would drop the stderr the rate-limit message actually arrived on.
        res = runtime._result('{"result": "partial"}', "HTTP 429 rate limit", 1,
                              kind="claude", now=T0)
        self.assertEqual(res["failure"], "quota_exhausted")

    def test_local_timeout_is_other_not_quota(self):
        a = runtime.LocalAdapter()
        s = runtime.Session("implementer-local", ".")
        s.handle = {"argv": ["python3", "-c", "import time; time.sleep(30)"],
                    "env": dict(os.environ), "role": "implementer", "kind": "claude"}
        res = a.prompt(s, "hi", timeout=0.2)
        self.assertEqual(res["settled"], "timeout")
        self.assertEqual(res["failure"], "other")

    def test_mock_adapter_can_inject_a_quota_failure(self):
        def implementer(env, cwd):
            return blocked("Claude AI usage limit reached|resets at 5pm")

        a = runtime.MockAdapter({"implementer": implementer}, now=lambda: T0)
        s = a.start_agent("implementer", "claude", ".", {})
        res = a.prompt(s, "go", timeout=1)
        self.assertEqual(res["failure"], "quota_exhausted")

    def test_mock_adapter_default_is_still_a_plain_success(self):
        a = runtime.MockAdapter({"implementer": lambda e, c: None})
        s = a.start_agent("implementer", "claude", ".", {})
        self.assertIsNone(a.prompt(s, "go", timeout=1)["failure"])

    def test_herdr_error_codes_classify_without_prose(self):
        res = {"settled": "blocked", "output": "", "code": 1,
               "error_code": "rate_limited"}
        self.assertEqual(quota.classify("claude", res, T0)[0], "quota_exhausted")


def _orch(reg, adapter, task, clock=None, logs=None):
    return Orchestrator(task, reg, adapter, lambda k, x: True,
                        log=(logs.append if logs is not None else (lambda *_: None)),
                        clock=clock or (lambda: T0))


class TestMachineSelection(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)
        self.pol = dict(self.reg["policy"]["limits"],
                        escalation_ceiling=self.reg["policy"]["escalation_ceiling"])
        self.task = store.Task.create(self.t.repo, "T-001", "# t\n\nDo it\n", self.pol)

    def tearDown(self):
        self.t.close()

    def test_pick_skips_a_cooled_channel(self):
        cooldown.open_breaker("claude-seat", "quota", T0 + 3600, T0)
        orch = _orch(self.reg, runtime.MockAdapter(), self.task)
        self.assertEqual(orch._pick("implementer").channel, "cursor-seat")

    def test_pick_uses_a_cooled_channel_again_once_it_reopens(self):
        # AC-2: expiry is on read, so no command has to remember to sweep.
        cooldown.open_breaker("claude-seat", "quota", T0 + 3600, T0)
        later = _orch(self.reg, runtime.MockAdapter(), self.task,
                      clock=lambda: T0 + 3601)
        self.assertEqual({c.channel for c in later.router.candidates(
            "implementer", cooldowns=later._channel_state()[1])},
            {"claude-seat", "cursor-seat"})

    def test_pick_raises_the_quota_error_when_every_channel_is_cooled(self):
        cooldown.open_breaker("claude-seat", "quota", T0 + 3600, T0)
        cooldown.open_breaker("cursor-seat", "quota", T0 + 7200, T0)
        orch = _orch(self.reg, runtime.MockAdapter(), self.task)
        with self.assertRaises(router.AllChannelsCooled) as cm:
            orch._pick("implementer")
        self.assertEqual(cm.exception.reopen_at, T0 + 3600, "earliest reopen wins")
        self.assertIn("claude-seat", cm.exception.channels)

    def test_an_unenrolled_role_is_still_a_plain_no_model_error(self):
        # Not every empty candidate list is a quota problem, and saying so would
        # send the user to `delegate channels` for a registry mistake.
        cooldown.open_breaker("claude-seat", "quota", T0 + 3600, T0)
        orch = _orch(self.reg, runtime.MockAdapter(), self.task)
        self.reg["profiles"]["nobody"] = {"require": {"reasoning": 9}, "weights": {}}
        with self.assertRaises(router.NoModelAvailable) as cm:
            orch._pick("nobody")
        self.assertNotIsInstance(cm.exception, router.AllChannelsCooled)

    def test_a_corrupt_channels_file_is_logged_and_ignored(self):
        # AC-4: fail closed and honest -- no cooldowns, and say why.
        os.makedirs(os.path.dirname(cooldown.path()), exist_ok=True)
        with open(cooldown.path(), "w") as fh:
            fh.write("[[[")
        logs = []
        orch = _orch(self.reg, runtime.MockAdapter(), self.task, logs=logs)
        self.assertEqual(orch._pick("implementer").channel, "claude-seat")
        self.assertTrue(any("unreadable" in x for x in logs), logs)

    def test_an_optional_role_going_dark_mid_call_degrades_instead_of_parking(self):
        """A seat that dies *during* a cosmetic call must degrade exactly like a
        seat that was already cooled before it. Parking a finished task because
        the brief could not be prettified is absurd, and the guard around
        `_pick` alone did not cover the call that follows it.

        No breaker is opened up front on purpose: `fast-cheap` is the only model
        enrolled for this role and only cursor-seat exposes it, so pre-cooling
        would fail at the pick -- the path that was already guarded -- and prove
        nothing about the call after it."""

        def reporter(env, cwd):
            return blocked(QUOTA_MSG)

        orch = _orch(self.reg, runtime.MockAdapter({"reporter": reporter},
                                                   now=lambda: T0), self.task)
        original = "# T-001 — Merge\n\nA plain brief that is long enough to keep." * 3
        self.assertEqual(orch._polish(original), original,
                         "the template fallback was skipped")

    def test_an_optional_role_going_dark_does_not_park_the_run(self):
        def classifier(env, cwd):
            return blocked(QUOTA_MSG)

        orch = _orch(self.reg, runtime.MockAdapter({"classifier": classifier},
                                                   now=lambda: T0), self.task)
        tier, why, by = orch._ask_classifier("Add a subtract function")
        self.assertEqual(tier, "complex", "the fail-safe classification was lost")
        self.assertEqual(by, "fallback")

    def test_invocations_are_metered_against_the_window(self):
        orch = _orch(self.reg, runtime.MockAdapter(), self.task)
        choice = orch._pick("implementer")
        orch._meter(choice)
        orch._meter(choice)
        _, usage, _ = cooldown.read(T0)
        self.assertEqual(len(usage[choice.channel]), 2)


PLAN_MD = """# Plan

## Subtasks
```yaml
- id: st-1-main
  goal: Add a subtract function to app.py
  file_scope: ["app.py"]
  acceptance: [AC-1]
```
"""



class TestFailoverEndToEnd(unittest.TestCase):
    """AC-1, AC-3. The whole point: a seat empties and the run continues."""

    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)
        self.pol = dict(self.reg["policy"]["limits"],
                        escalation_ceiling=self.reg["policy"]["escalation_ceiling"])
        with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
            fh.write('fast:\n  - "python3 -c \'print(1)\'"\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "cfg"], self.t.repo)

    def tearDown(self):
        self.t.close()

    def _script(self, quota_first):
        """An implementer that hits a quota wall on its first channel only."""
        state = {"seen": []}

        def planner(env, cwd):
            with open(os.path.join(env["AGENT_DELEGATION_TASK_DIR"], "plan.md"), "w") as fh:
                fh.write(PLAN_MD)

        def implementer(env, cwd):
            state["seen"].append(cwd)
            if quota_first and len(state["seen"]) == 1:
                return blocked(QUOTA_MSG)
            with open(os.path.join(cwd, "app.py"), "a") as fh:
                fh.write("\ndef subtract(a, b):\n    return a - b\n")
            _report(env["AGENT_DELEGATION_TASK_DIR"], "implement-st-1-main.json", {
                "stage": "implement", "role": "implementer", "subtask": "st-1-main",
                "status": "complete", "summary": "added subtract",
                "evidence": {"tests": "green"}})
            return None

        return state, {"planner": planner, "implementer": implementer,
                       "test-author": lambda e, c: None,
                       "classifier": lambda e, c: {"output": "VERDICT: SIMPLE -- small"},
                       "reviewer": lambda e, c: None}

    def _run(self, script, clock=None):
        task = store.Task.create(self.t.repo, "T-001",
                                 "# t\n\n- **AC-1** — subtract exists\n", self.pol)
        logs = []
        clock = clock or (lambda: T0)
        adapter = runtime.MockAdapter(script, now=clock)
        status = Orchestrator(task, self.reg, adapter, lambda k, x: True,
                              log=logs.append, clock=clock).run()
        return task, status, logs

    def test_a_quota_wall_fails_over_and_the_task_still_finishes(self):
        _, script = self._script(quota_first=True)
        task, status, logs = self._run(script)
        self.assertEqual(status, "done", "\n".join(logs))
        self.assertTrue(any("failover:" in x for x in logs), "\n".join(logs))

    def test_the_quota_failure_costs_no_attempt(self):
        # AC-1: a quota failure is the channel's fault, not the approach's.
        _, script = self._script(quota_first=True)
        task, _, logs = self._run(script)
        attempts = task.state["spent"]["attempts"]
        self.assertEqual(attempts.get("st-1-main"), 1,
                         "the quota failure was billed to the subtask: %s" % attempts)

    def test_the_run_moved_to_the_other_channel(self):
        _, script = self._script(quota_first=True)
        task, _, _ = self._run(script)
        used = [h["channel"] for h in task.state["delegation_history"]
                if h.get("role") == "implementer" and h.get("outcome") == "complete"]
        self.assertEqual(used, ["cursor-seat"])

    def test_the_cooled_channel_is_on_disk_with_the_stated_reset(self):
        # AC-2: "reset in 2 hours" from the message, not the 5h window.
        _, script = self._script(quota_first=True)
        self._run(script)
        cools, _, _ = cooldown.read(T0)
        self.assertEqual(cools["claude-seat"]["reopen_at"], T0 + 2 * 3600)
        self.assertEqual(cools["claude-seat"]["reason"], "quota")

    def test_without_a_stated_reset_the_configured_window_is_used(self):
        state, script = self._script(quota_first=False)
        original = script["implementer"]

        def implementer(env, cwd):
            if not state["seen"]:
                state["seen"].append(cwd)
                return blocked("Error: usage limit reached")     # no time given
            return original(env, cwd)

        _, status, logs = self._run(dict(script, implementer=implementer))
        cools, _, _ = cooldown.read(T0)
        self.assertEqual(cools["claude-seat"]["reopen_at"], T0 + 5 * 3600)
        self.assertTrue(any("stated no reset time" in x for x in logs), "\n".join(logs))

    def test_a_generic_failure_still_consumes_an_attempt(self):
        # AC-5: the other half of the contract. A stack trace is a retry.
        state, script = self._script(quota_first=False)
        original = script["implementer"]
        calls = {"n": 0}

        def implementer(env, cwd):
            calls["n"] += 1
            if calls["n"] == 1:
                return blocked("Traceback (most recent call last):\nValueError: x")
            return original(env, cwd)

        task, status, logs = self._run(dict(script, implementer=implementer))
        self.assertFalse(any("failover:" in x for x in logs), "\n".join(logs))
        self.assertGreaterEqual(task.state["spent"]["attempts"].get("st-1-main", 0), 1)
        self.assertEqual(cooldown.active(T0), set(), "a stack trace opened a breaker")

    def test_every_channel_cooled_parks_with_the_quota_reason(self):
        # AC-3.
        cooldown.open_breaker("cursor-seat", "quota", T0 + 9000, T0)
        _, script = self._script(quota_first=True)
        task, status, logs = self._run(script)
        self.assertEqual(status, "needs_human")
        park = task.state.get("park") or {}
        self.assertEqual(park.get("reason"), "quota_all_exhausted")
        self.assertEqual(park.get("reopen_at"), T0 + 2 * 3600, "earliest reopen")
        self.assertIn("claude-seat", park.get("channels", []))

    def test_a_quota_park_consumes_no_attempt_budget(self):
        # AC-3: parking must not also spend the thing that would let a retry
        # work once the window reopens.
        cooldown.open_breaker("cursor-seat", "quota", T0 + 9000, T0)
        _, script = self._script(quota_first=True)
        task, _, _ = self._run(script)
        self.assertEqual(task.state["spent"]["attempts"].get("st-1-main", 0), 0)

    def test_the_park_brief_names_the_seats_and_the_reopen_time(self):
        cooldown.open_breaker("cursor-seat", "quota", T0 + 9000, T0)
        _, script = self._script(quota_first=True)
        task, _, _ = self._run(script)
        text = task.read_text("brief.md", "")
        self.assertIn("usage limit", text.lower())
        self.assertIn("claude-seat", text)

    def test_work_already_committed_survives_the_hop(self):
        # §5.5: the replacement resumes from the checkpoint, it does not restart.
        state, script = self._script(quota_first=False)
        original = script["implementer"]
        calls = {"n": 0}
        saw = {}

        def implementer(env, cwd):
            calls["n"] += 1
            if calls["n"] == 1:
                with open(os.path.join(cwd, "partial.py"), "w") as fh:
                    fh.write("# half done\n")
                return blocked(QUOTA_MSG)
            saw["partial"] = os.path.exists(os.path.join(cwd, "partial.py"))
            return original(env, cwd)

        task, status, logs = self._run(dict(script, implementer=implementer))
        self.assertEqual(status, "done", "\n".join(logs))
        self.assertTrue(saw.get("partial"),
                        "the replacement got a clean worktree, losing the work")


class TestChannelsCommand(unittest.TestCase):
    """AC-4. These drive the real entry point, which reads the real clock, so
    they place their fixtures relative to now rather than to the fixed T0 the
    machine tests inject."""

    def setUp(self):
        import time as _time
        self.t = TempRepo()
        self.now = _time.time()
        self.reg_arg = ["--registry", REGISTRY]

    def tearDown(self):
        self.t.close()

    def _run(self, argv):
        import contextlib
        import io

        from adg import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(self.reg_arg + argv)
        return buf.getvalue()

    def test_it_lists_an_open_breaker(self):
        cooldown.open_breaker("claude-seat", "quota", self.now + 3600, self.now,
                              "usage limit")
        out = self._run(["channels"])
        self.assertIn("claude-seat", out)
        self.assertIn("cooling", out)

    def test_it_says_so_when_nothing_is_cooling(self):
        out = self._run(["channels"])
        self.assertIn("claude-seat", out, "enrolled channels are listed either way")
        self.assertNotIn("cooling", out)

    def test_clear_removes_one(self):
        cooldown.open_breaker("claude-seat", "quota", self.now + 3600, self.now)
        out = self._run(["channels", "--clear", "claude-seat"])
        self.assertIn("cleared", out)
        self.assertEqual(cooldown.active(self.now), set())

    def test_clearing_something_that_is_not_cooling_says_so(self):
        out = self._run(["channels", "--clear", "cursor-seat"])
        self.assertIn("not cooling", out)

    def test_a_corrupt_file_is_reported_not_crashed_on(self):
        os.makedirs(os.path.dirname(cooldown.path()), exist_ok=True)
        with open(cooldown.path(), "w") as fh:
            fh.write("garbage")
        out = self._run(["channels"])
        self.assertIn("unreadable", out)

    def test_it_shows_utilization(self):
        for i in range(10):
            cooldown.record_use("claude-seat", 5 * 3600, self.now - i)
        out = self._run(["channels"])
        self.assertIn("25%", out, out)   # 10 of est_capacity 40


class TestResumeAtTheWindow(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)
        self.pol = dict(self.reg["policy"]["limits"],
                        escalation_ceiling=self.reg["policy"]["escalation_ceiling"])
        self.task = store.Task.create(self.t.repo, "T-001", "# t\n", self.pol)
        self.task.update(status="needs_human",
                         park={"reason": "quota_all_exhausted",
                               "reopen_at": T0 + 3600,
                               "channels": ["claude-seat"], "role": "implementer"})

    def tearDown(self):
        self.t.close()

    def test_resuming_early_refuses_and_says_when(self):
        from adg import cli
        with self.assertRaises(SystemExit) as cm:
            cli._quota_guard(self.task, when_open=False, clock=lambda: T0)
        self.assertIn("quota", str(cm.exception).lower())
        self.assertIn("--when-open", str(cm.exception))

    def test_resuming_after_the_window_just_works(self):
        from adg import cli
        cli._quota_guard(self.task, when_open=False, clock=lambda: T0 + 3601)
        self.assertIsNone(self.task.state.get("park"), "the park was not cleared")

    def test_when_open_waits_out_the_window_without_ever_sleeping(self):
        # The clock only moves because the fake sleep moves it, which is what
        # proves the wait is driven by the remaining time and not by luck.
        from adg import cli
        fake = {"t": T0}
        slept = []

        def sleep(n):
            slept.append(n)
            fake["t"] += n

        prev, cli._SLEEP = cli._SLEEP, sleep
        try:
            cli._quota_guard(self.task, when_open=True, clock=lambda: fake["t"],
                             log=lambda *_: None)
        finally:
            cli._SLEEP = prev
        self.assertTrue(slept, "--when-open did not wait at all")
        self.assertAlmostEqual(sum(slept), 3600, delta=1)
        self.assertGreaterEqual(fake["t"], T0 + 3600)
        self.assertIsNone(self.task.state.get("park"))

    def test_a_task_parked_for_any_other_reason_is_untouched(self):
        # The guard owns exactly one park reason. Clearing someone else's would
        # let a budget-parked task resume as though its cap had been raised.
        from adg import cli
        self.task.update(park={"reason": "budget_exhausted", "reopen_at": T0 + 3600})
        cli._quota_guard(self.task, when_open=False, clock=lambda: T0)  # no exit
        self.assertEqual((self.task.state.get("park") or {}).get("reason"),
                         "budget_exhausted", "the guard cleared a park it does not own")


if __name__ == "__main__":
    unittest.main(verbosity=2)
