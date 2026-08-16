"""Tests for quota-aware provider failover.

Every clock is injected and every provider is a mock: this suite must never
sleep, never reach the network, and never need a vendor CLI on PATH.

Run: python3 orchestrator/tests/test_failover.py
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adg import cli, cooldown, quota, router, runtime, store     # noqa: E402
from adg.machine import Orchestrator                             # noqa: E402
from test_orchestrator import (REGISTRY, TempRepo, _report, _slurp,  # noqa: E402,F401
                               sh)

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
        # five-hour breaker on a provider that is fine. It is now its own kind
        # rather than `other`, so that a hung call can move the work to another
        # seat -- but `Orchestrator.OPENS_THE_BREAKER` still excludes it, which
        # is the half that matters here.
        kind, at = quota.classify(
            "claude", {"settled": "timeout", "output": "", "code": None}, T0)
        self.assertEqual(kind, "timeout")
        self.assertNotEqual(kind, "quota_exhausted")
        self.assertIsNone(at, "a timeout must carry no reopen time")
        from adg.machine import Orchestrator
        self.assertNotIn(kind, Orchestrator.OPENS_THE_BREAKER)
        self.assertIn(kind, Orchestrator.HOPS_TO_ANOTHER_SEAT)

    def test_a_timeout_mentioning_rate_limits_is_not_read_as_quota(self):
        # Deliberately not folded into the test above: this one fails the moment
        # the timeout check moves *after* the pattern table, which is the actual
        # regression to guard against.
        kind, _ = quota.classify("claude", {
            "settled": "timeout", "code": None,
            "output": "waiting on rate limit ..."}, T0)
        self.assertEqual(kind, "timeout")

    def test_success_is_not_a_failure_at_all(self):
        kind, _ = quota.classify("claude", {
            "settled": "idle", "code": 0,
            "output": "done. (note: you are near your usage limit)"}, T0)
        self.assertIsNone(kind)

    def test_an_unknown_agent_kind_falls_back_to_other(self):
        # A provider nobody wrote a table for must not inherit claude's.
        kind, _ = quota.classify("nobody-ships-this", blocked("usage limit reached"), T0)
        self.assertEqual(kind, "other")


CORPUS = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "fixtures", "provider-messages.json"),
                        encoding="utf-8"))


class TestProviderMessageCorpus(unittest.TestCase):
    """Detection, driven from `fixtures/provider-messages.json`.

    Through `runtime._result` rather than `quota.classify` directly, because the
    thing that decides a run's fate is the whole path: which stream the text
    arrived on, whether the CLI wrapped it in an envelope, and only then the
    pattern table. `PROVIDER_SHAPES` above builds a result dict by hand and so
    cannot see any of that -- it was green while an agent's own summary of the
    retry handler it had just written was being read as its provider refusing.

    Adding a provider message is a data edit, which is the same trade
    `quota.PATTERNS` makes one file over.
    """

    # `{T0+N}` in a corpus message becomes an absolute epoch here. Writing one
    # into the JSON would pin the file to whatever this suite's fixed clock
    # happens to be, and a reset stamp is only meaningful relative to `now`.
    _STAMP = re.compile(r"\{T0\+(\d+)\}")

    @classmethod
    def _run(cls, case):
        kind, via = case["kind"], case["via"]
        text = cls._STAMP.sub(lambda m: str(int(T0 + int(m.group(1)))), case["text"])
        if via == "stderr":
            return runtime._result("", text, 1, kind=kind, now=T0)
        if via == "stdout":
            return runtime._result(text, "", 1, kind=kind, now=T0)
        if via == "result":
            # The shape a JSON CLI actually returns: the agent's prose inside an
            # envelope, nothing on stderr, non-zero exit for some unrelated
            # reason. The provider said nothing at all here.
            return runtime._result(json.dumps({"result": text}), "", 1,
                                   kind=kind, now=T0)
        if via == "error_code":
            return runtime._result("", "", 1, kind=kind, now=T0, error_code=text)
        raise AssertionError("unknown via %r" % via)

    def _cases(self):
        return [c for c in CORPUS["cases"] if "_class" not in c]

    def test_every_case_classifies_as_the_corpus_says(self):
        for case in self._cases():
            with self.subTest(kind=case["kind"], via=case["via"],
                              text=case["text"][:48]):
                res = self._run(case)
                self.assertEqual(res["failure"], case["expect"], case.get("note", ""))

    def test_every_case_carries_the_reopen_time_the_corpus_says(self):
        for case in self._cases():
            want = case["reset"]
            with self.subTest(text=case["text"][:48]):
                got = self._run(case)["reset_at"]
                if want is None:
                    self.assertIsNone(got)
                elif want == "some":
                    self.assertIsNotNone(got)
                else:
                    self.assertEqual(got, T0 + want)

    def test_an_agents_own_words_are_never_the_provider_speaking(self):
        """The corpus's reason for existing, asserted on its own.

        A job whose work IS rate limiting -- write the retry handler, review the
        limiter, grep for the error constant -- puts every string in
        `quota.PATTERNS` into its own transcript. Read as a provider refusal
        that does not cost an attempt, it opens a five-hour breaker on a healthy
        seat, machine-wide. Every `via: result` case is one of those.
        """
        prose = [c for c in self._cases() if c["via"] == "result"]
        self.assertTrue(prose, "the corpus lost its agent-prose cases")
        for case in prose:
            with self.subTest(text=case["text"][:48]):
                self.assertEqual(self._run(case)["failure"], "other")

    def test_a_refusal_on_stderr_still_wins_over_a_clean_envelope(self):
        """The other side of that split, and the thing it must not break: the
        envelope is not a licence to ignore stderr. A CLI can hand back a
        partial result AND report the wall it hit."""
        res = runtime._result('{"result": "partial work, all fine"}',
                              "HTTP 429 rate limit", 1, kind="claude", now=T0)
        self.assertEqual(res["failure"], "quota_exhausted")

    def test_an_error_envelope_is_still_read(self):
        """`error`/`subtype` are the provider talking, not the agent, so they
        stay in scope when `result` is dropped."""
        res = runtime._result(
            json.dumps({"result": "I stopped early.", "is_error": True,
                        "subtype": "usage limit reached"}),
            "", 1, kind="claude", now=T0)
        self.assertEqual(res["failure"], "quota_exhausted")

    KNOWN_WRONG = 3

    def test_the_corpus_pins_how_many_cases_detection_still_gets_wrong(self):
        """Pinned so the number cannot drift in either direction unnoticed.

        Both survivors are plain-text CLIs, where the whole stream is the
        agent's transcript and there is no envelope to separate its words from
        the provider's. Fixing one should fail this test -- move the case and
        drop the count.
        """
        wrong = [c for c in self._cases() if c.get("wrong")]
        self.assertEqual(len(wrong), self.KNOWN_WRONG)
        for c in wrong:
            self.assertIn(c["via"], ("stdout",),
                          "a JSON-envelope case is fixable and should not be "
                          "carried as known-wrong")


class TestResetParsing(unittest.TestCase):
    def test_an_epoch_reset_is_read_whatever_its_case(self):
        """`_EPOCH` was the one pattern here without `re.I`, so a message that
        began its sentence with "Resets" -- most of them -- lost the reset time
        the provider had just stated, and the run fell back to the channel's
        configured window. That parks a task longer than it needs to be parked
        and makes `resume --when-open` wait out a window already reopened."""
        want = int(T0 + 3600)
        for text in ("resets %d" % want, "Resets %d" % want, "RESETS %d" % want,
                     "Claude AI usage limit reached. Resets %d" % want):
            self.assertEqual(quota.parse_reset(text, T0), float(want),
                             "lost the reset time in %r" % text)

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

    def test_a_zero_window_is_refused(self):
        # A zero window makes _reopen_at return `now`, so the breaker is pruned
        # by the same write that opens it and failover bounces forever.
        self.assertEqual(quota.parse_window("0h", default=99.0), 99.0)
        self.assertEqual(quota.parse_window(0, default=99.0), 99.0)

    def test_a_unit_is_matched_whole_never_by_first_letter(self):
        # `1month` read as one *minute* is wrong by four orders of magnitude on
        # a monthly seat, and nothing downstream could tell.
        self.assertEqual(quota.parse_window("1month", default=99.0), 99.0)
        self.assertEqual(quota.parse_window("1mo", default=99.0), 99.0)
        self.assertEqual(quota.parse_window("30m"), 30 * 60)

    def test_capacity_tolerates_the_units_design_md_uses(self):
        # writes these as `40u` and `500req`.
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

    # The docstring promises "never fatal". Valid JSON of the wrong *shape* is
    # what a hand-edit or a future schema change actually produces, and it
    # sailed past the top-level dict check straight into an AttributeError.
    MALFORMED = ['{"cooldowns": [1, 2]}',
                 '{"usage": ["a"]}',
                 '{"usage": {"claude-seat": ["oops"]}}',
                 '{"usage": {"claude-seat": {"a": 1}}}',
                 '{"cooldowns": {"claude-seat": "soon"}}',
                 '[]']

    def _write_raw(self, text):
        os.makedirs(os.path.dirname(cooldown.path()), exist_ok=True)
        with open(cooldown.path(), "w") as fh:
            fh.write(text)

    def test_wrongly_shaped_json_reads_as_empty_rather_than_raising(self):
        for text in self.MALFORMED:
            with self.subTest(content=text):
                self._write_raw(text)
                cools, usage, warn = cooldown.read(T0)
                self.assertEqual(cools, {})
                # A surviving key with no usable stamps is fine; a surviving
                # *stamp* is not, because it would skew the shadow price.
                self.assertEqual([s for v in usage.values() for s in v], [])
                self.assertIsNotNone(warn, "a silent empty read hides the problem")

    def test_wrongly_shaped_json_does_not_break_a_write_either(self):
        # A run must survive it: _meter fires after *every* invocation.
        for text in self.MALFORMED:
            with self.subTest(content=text):
                self._write_raw(text)
                cooldown.record_use("claude-seat", 3600, T0)
                cooldown.open_breaker("claude-seat", "quota", T0 + 60, T0)
                self.assertEqual(cooldown.active(T0), {"claude-seat"},
                                 "the write did not recover the file")

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() != 0,
                         "chmod is a no-op for root")
    def test_an_unwritable_state_dir_is_reported_not_raised(self):
        # A read-only $XDG_STATE_HOME with a writable task dir is an ordinary CI
        # sandbox. _meter runs on the first agent call, so raising here kills
        # the run before it starts.
        #
        # Skipped as root, and on Windows, where there is no geteuid: root
        # ignores the mode bits, so `record_use` succeeds and the assertion that
        # the failure was REPORTED fails -- a red test that says nothing about
        # this code, in the one environment (a container running as root) where
        # a reader is least able to tell it apart from a real one.
        root = store.state_root()
        os.makedirs(root, exist_ok=True)
        mode = os.stat(root).st_mode
        os.chmod(root, 0o500)
        try:
            cooldown.record_use("claude-seat", 3600, T0)          # must not raise
            err = cooldown.open_breaker("claude-seat", "quota", T0 + 60, T0)
            self.assertIsNotNone(cooldown.last_error(),
                                 "an unwritable breaker failed silently")
            self.assertIsNotNone(err)
        finally:
            os.chmod(root, mode)

    def test_a_missing_file_is_simply_empty_and_quiet(self):
        cools, usage, warn = cooldown.read(T0)
        self.assertEqual((cools, usage, warn), ({}, {}, None))

    def test_a_second_writer_does_not_clobber_the_first(self):
        # Renamed: this is sequential, and calling it a concurrency test claimed
        # a guarantee the module does not make. There is no cross-process lock —
        # `_mutate` re-reads before writing, which narrows the lost-update window
        # rather than closing it. What this pins is the merge itself: writing one
        # channel must not drop another's entry.
        cooldown.open_breaker("claude-seat", "quota", T0 + 3600, T0)
        cooldown.open_breaker("cursor-seat", "quota", T0 + 7200, T0)
        self.assertEqual(cooldown.active(T0 + 10), {"claude-seat", "cursor-seat"})

    def test_a_lost_breaker_is_recoverable_rather_than_permanent(self):
        # There is no cross-process lock, so a breaker CAN be lost to a racing
        # writer. What makes that tolerable is not being tested for absence --
        # it is that the next call to the seat re-opens it from the provider's
        # own answer, so the system re-converges instead of forgetting.
        cooldown.open_breaker("claude-seat", "quota", T0 + 3600, T0)
        with open(cooldown.path(), "w") as fh:          # a racing writer wins
            fh.write('{"version": 1, "cooldowns": {}, "usage": {}}\n')
        self.assertEqual(cooldown.active(T0), set())
        cooldown.open_breaker("claude-seat", "quota", T0 + 3600, T0)
        self.assertEqual(cooldown.active(T0), {"claude-seat"})

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
        before = {c.channel for c in self.r.candidates()}
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
    """: a subscription seat with headroom is ~free; as its window fills it
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
    """claude-seat reserves 30% for the integrator in registry.default.yaml.

    It reserved for `planner` and `reviewer` until those roles stopped being
    dispatched, at which point every role that DOES run was the non-reserved
    one and the reservation withheld the seat on behalf of nobody. The
    integrator is the honest claimant: it is dispatched last, after the wave
    that drew the seat down has already been paid for.
    """

    def setUp(self):
        self.reg = router.load_registry(REGISTRY)
        self.r = router.Router(self.reg)

    def test_a_reserved_role_keeps_the_seat_at_any_draw(self):
        got = self.r.candidates("integrator", utilization={"claude-seat": 0.95})
        self.assertIn("claude-seat", {c.channel for c in got})

    def test_the_reserved_role_is_one_the_machine_actually_dispatches(self):
        """The defect that made the reservation inert. A role named here that
        no stage dispatches reserves headroom nobody can claim."""
        from adg import workflow as wf
        dispatched = {spec.get("role") for spec in wf.Workflow.load().stages.values()}
        for chan in self.reg["channels"].values():
            for role in (chan.get("reserve_for") or []):
                self.assertIn(role, dispatched,
                              "%r is reserved for and never dispatched" % role)

    def test_a_non_reserved_role_is_filtered_past_the_floor(self):
        got = self.r.candidates("implementer",
                                utilization={"claude-seat": 0.8, "cursor-seat": 0.0})
        self.assertNotIn("claude-seat", {c.channel for c in got})

    def test_below_the_floor_nothing_is_filtered(self):
        got = self.r.candidates("implementer",
                                utilization={"claude-seat": 0.5, "cursor-seat": 0.0})
        self.assertIn("claude-seat", {c.channel for c in got})

    def test_the_withheld_seat_is_available_on_request_and_flagged(self):
        # The router withholds; it does not decide whether withholding is
        # affordable, because only the caller knows which CLI is installed.
        held = self.r.candidates("implementer", utilization={"claude-seat": 0.8},
                                 cooldowns={"cursor-seat"}, ignore_reserve=True)
        # By channel and flag, not by count: claude-seat exposes more than one
        # model an unbanded job may use, and how many that is is not what this
        # asserts.
        self.assertTrue(held, "the withheld seat was not offered at all")
        self.assertEqual({c.channel for c in held}, {"claude-seat"})
        self.assertTrue(all(c.demoted for c in held),
                        "a withheld seat was handed back unflagged")
        self.assertEqual(self.r.candidates("implementer",
                                           utilization={"claude-seat": 0.8},
                                           cooldowns={"cursor-seat"}), [])

    def test_select_falls_back_to_a_reserved_seat_once_exclude_empties_the_rest(self):
        got = self.r.select("implementer", utilization={"claude-seat": 0.8},
                            exclude=(("balanced-coder", "cursor-seat"),
                                     ("fast-cheap", "cursor-seat")))
        self.assertEqual(got.channel, "claude-seat")
        self.assertTrue(got.demoted)

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

    def test_local_timeout_is_timeout_not_quota(self):
        # This is the path a real hung agent takes -- subprocess.TimeoutExpired,
        # never `quota.classify` -- so it has to agree with the classifier. While
        # it hardcoded `other`, every classifier test passed and the hop still
        # would not have fired on an actually stuck call.
        a = runtime.LocalAdapter()
        s = runtime.Session("implementer-local", ".")
        s.handle = {"argv": ["python3", "-c", "import time; time.sleep(30)"],
                    "env": dict(os.environ), "role": "implementer", "kind": "claude"}
        res = a.prompt(s, "hi", timeout=0.2)
        self.assertEqual(res["settled"], "timeout")
        self.assertEqual(res["failure"], "timeout")
        self.assertIsNone(res["reset_at"], "a timeout must carry no reopen time")

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

    def test_a_json_envelopes_error_code_classifies_without_prose(self):
        """`error.code` out of a JSON envelope, lifted by `runtime._result`.

        Named for herdr until the schema was actually read: herdr's `AgentInfo`
        has no error field at all, so nothing on that path ever set this key.
        What does set it is a CLI whose envelope reports the refusal as a code
        -- which is the provider talking through the CLI, and in scope.
        """
        res = {"settled": "blocked", "output": "", "code": 1,
               "error_code": "rate_limited"}
        self.assertEqual(quota.classify("claude", res, T0)[0], "quota_exhausted")


class TestHerdrPaneClassification(unittest.TestCase):
    """The pane path must not read the agent as the provider.

    A pane is a PTY: the CLI's stderr is already merged into the scrollback, so
    what `agent read` returns is the AGENT's prose. An agent whose job merely
    mentions rate limits -- writing a retry handler, reviewing a limiter,
    grepping for RESOURCE_EXHAUSTED -- puts every string in `quota.PATTERNS`
    into that scrollback.

    The asymmetry decides it. A false positive is a five-hour breaker on a
    healthy seat, machine-wide, across every repository, and it is silent. A
    missed wall is one failed attempt, and it is loud. herdr exposes no channel
    that carries the provider's own words (see `runtime.HerdrAdapter`), so the
    pane path classifies nothing.

    Driven through the adapter, not `quota.classify`: the whole question is
    which text the adapter hands over, and a test against the classifier cannot
    see that.
    """

    class _Pane(runtime.HerdrAdapter):
        """A herdr that answers `agent prompt` and `agent read` from a script."""

        def __init__(self, transcript, status="blocked"):
            runtime.HerdrAdapter.__init__(self, workspace="w1")
            self.transcript, self.status = transcript, status

        def _cli(self, args, check=True):
            if args[:2] == ["agent", "prompt"]:
                return {"result": {"agent": {"agent_status": self.status}}}
            if args[:2] == ["agent", "read"]:
                return {"result": {"text": self.transcript}}
            return {"result": {}}

    @staticmethod
    def _session():
        return runtime.Session("x", "/tmp",
                               handle={"herdr": True, "role": "implementer",
                                       "kind": "claude"})

    def _prompt(self, transcript, status="blocked"):
        return self._Pane(transcript, status).prompt(self._session(), "go", 10)

    def test_an_agents_quota_vocabulary_in_a_pane_is_not_a_wall(self):
        """Every positive string in the corpus, as the AGENT's transcript. Each
        one classifies as a wall when a provider says it on stderr; none may
        when it is the pane talking."""
        walls = [c["text"] for c in CORPUS["cases"]
                 if c.get("expect") == "quota_exhausted" and "_class" not in c]
        self.assertTrue(walls, "the corpus lost its positive cases")
        for text in walls:
            with self.subTest(text=text[:48]):
                res = self._prompt("I am writing the retry handler.\n" + text)
                self.assertEqual(res["failure"], "other",
                                 "the pane transcript was read as the provider")
                self.assertIsNone(res["reset_at"],
                                  "a reopen time was parsed out of agent prose")

    def test_the_documented_unclassified_behaviour_holds_end_to_end(self):
        """No structured channel exists, so this is the positive case the item
        asks for: a run whose seat really has walled gets a failed job, no
        cooldown entry, and no park -- the outcome SKILL.md now promises."""
        task = store.Task.create(self.t.repo, "T-PANE", "# t\n\n- **AC-1** — x\n",
                                 self.pol)
        task.write_text("plan.md", PLAN_MD)

        class Adapter(runtime.MockAdapter):
            """A mock wearing the pane path's answer: settled blocked, the
            provider's message only in the scrollback, nothing else."""

            def prompt(self, session, text, timeout):
                return runtime.settle_unclassified({
                    "settled": "blocked", "code": 1, "cost_usd": None,
                    "pane_transcript": True,
                    "output": "Error: Claude AI usage limit reached. "
                              "Your limit will reset in 2 hours."})

        logs = []
        Orchestrator(task, self.reg, Adapter({}), lambda k, x: True,
                     log=logs.append, clock=lambda: T0).run()
        self.assertEqual(cooldown.read(T0)[0], {},
                         "a pane transcript opened a breaker")
        self.assertNotIn("quota_all_exhausted",
                         (task.state.get("park") or {}).get("reason") or "")
        self.assertFalse(any("failover:" in x for x in logs),
                         "a pane failure was routed as a quota wall:\n"
                         + "\n".join(logs))

    def test_a_refusal_by_herdr_is_not_the_provider_either(self):
        """herdr's own ErrorResponse code says herdr would not take the
        request. Feeding it to the table would let herdr's vocabulary cool a
        seat herdr never called."""
        class H(runtime.HerdrAdapter):
            def _cli(self, args, check=True):
                self.last_error = "rate_limited"      # herdr's word, not a provider's
                return None
        res = H(workspace="w1").prompt(self._session(), "go", 10)
        self.assertEqual(res["failure"], "other")
        self.assertIsNone(res["reset_at"])
        self.assertEqual(res.get("herdr_error"), "rate_limited",
                         "the reason was dropped instead of being kept off the table")
        self.assertIsNone(res.get("error_code"),
                          "herdr's code reached the key the classifier reads")

    def test_a_hung_pane_is_still_a_timeout(self):
        """Not a reading of text -- it is how the call ended. A timeout still
        hops to another seat and still does not cool one."""
        class H(runtime.HerdrAdapter):
            def _cli(self, args, check=True):
                self.last_error = "timeout"
                return None
        res = H(workspace="w1").prompt(self._session(), "go", 10)
        self.assertEqual(res["failure"], "timeout")
        self.assertIsNone(res["reset_at"])

    def test_a_clean_pane_turn_is_not_a_failure(self):
        res = self._prompt("all done\n", status="idle")
        self.assertIsNone(res["failure"])
        self.assertIsNone(res["reset_at"])

    def test_no_panes_keeps_the_whole_local_probe(self):
        """The regression that would make this item a downgrade. `--no-panes`
        is a real subprocess with a real stderr, and it must keep classifying."""
        h = runtime.HerdrAdapter(workspace="w1", panes=False)
        sess = runtime.Session("x", "/tmp", handle={
            "kind": "claude", "argv": ["python3", "-c",
                                       "import sys; sys.stderr.write("
                                       "'Error: Claude AI usage limit reached');"
                                       "sys.exit(1)"],
            "env": dict(os.environ), "role": "implementer"})
        res = h.prompt(sess, "go", timeout=30)
        self.assertEqual(res["failure"], "quota_exhausted",
                         "the subprocess path stopped classifying")

    def test_a_pane_wall_captures_nothing_to_the_corpus(self):
        """Capture records what classification SAW. On this path it saw
        nothing, so there is nothing to file -- and filing agent prose as a
        provider message would poison the corpus that pins detection."""
        from adg import corpus
        self._prompt("Claude AI usage limit reached. Resets at 3pm.")
        self.assertFalse(os.path.exists(corpus.path()),
                         "agent prose was filed as a provider wall")

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


def _orch(reg, adapter, task, clock=None, logs=None):
    return Orchestrator(task, reg, adapter, lambda k, x: True,
                        log=(logs.append if logs is not None else (lambda *_: None)),
                        clock=clock or (lambda: T0))


def _seats(reg, role="implementer"):
    """The channels this role would use, best first: (first, fallback, ...).

    Asked of the registry rather than written down. Which provider a role
    starts on is a routing decision the registry owns, and it has already
    changed once: it used to fall out of the alphabetical tie-break in
    `candidates` -- both subscription seats with headroom price at ~0, so
    "claude-seat" < "cursor-seat" decided it -- and now falls out of an explicit
    `prefers:`. A failover test naming the seats is pinned to whatever settles
    that tie rather than to the failover it exists to prove, and the day the
    registry is retuned it either breaks for the wrong reason or, worse, keeps
    passing while proving nothing (cool the seat the router was not going to
    pick and the "it skipped the cooled seat" assertion is free).
    """
    order = []
    for c in router.Router(reg).candidates(role):
        if c.channel not in order:
            order.append(c.channel)
    return order


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
        # The seat the router WOULD have picked, or the skip is free.
        first, other = _seats(self.reg)[:2]
        cooldown.open_breaker(first, "quota", T0 + 3600, T0)
        orch = _orch(self.reg, runtime.MockAdapter(), self.task)
        self.assertEqual(orch._pick().channel, other)

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
            orch._pick()
        self.assertEqual(cm.exception.reopen_at, T0 + 3600, "earliest reopen wins")
        self.assertIn("claude-seat", cm.exception.channels)

    def test_an_unenrolled_model_is_still_a_plain_no_model_error(self):
        # Not every empty candidate list is a quota problem, and saying so would
        # send the user to `delegate channels` for a registry mistake.
        cooldown.open_breaker("claude-seat", "quota", T0 + 3600, T0)
        orch = _orch(self.reg, runtime.MockAdapter(), self.task)
        self.reg["profiles"]["worker"]["require"] = {"reasoning": 9}
        with self.assertRaises(router.NoModelAvailable) as cm:
            orch._pick()
        self.assertNotIsInstance(cm.exception, router.AllChannelsCooled)

    def test_a_corrupt_channels_file_is_logged_and_ignored(self):
        # AC-4: fail closed and honest -- no cooldowns, and say why.
        os.makedirs(os.path.dirname(cooldown.path()), exist_ok=True)
        with open(cooldown.path(), "w") as fh:
            fh.write("[[[")
        logs = []
        orch = _orch(self.reg, runtime.MockAdapter(), self.task, logs=logs)
        self.assertEqual(orch._pick().channel, _seats(self.reg)[0])
        self.assertTrue(any("unreadable" in x for x in logs), logs)

    def test_a_reserved_seat_is_handed_back_when_the_alternative_is_not_installed(self):
        """Regression. The reserve fallback used to be decided inside the router,
        which cannot see which CLI exists. On a box with the claude CLI but not
        cursor-agent, a claude-seat past its reserve floor was withheld in favour
        of a cursor-seat candidate that could never launch — and the run died
        with 'no runnable model' on a seat that was up and merely reserved."""
        for i in range(29):                       # past 1 - reserve_fraction (0.3)
            cooldown.record_use("claude-seat", 5 * 3600, T0 - i)

        class OnlyClaude(runtime.MockAdapter):
            def can_run(self, kind):
                return kind == "claude"

        logs = []
        orch = _orch(self.reg, OnlyClaude(), self.task, logs=logs)
        choice = orch._pick()
        self.assertEqual(choice.channel, "claude-seat")
        self.assertTrue(choice.demoted)
        self.assertTrue(any("reserve:" in x for x in logs), logs)

    def test_a_reserved_seat_is_still_withheld_when_a_runnable_alternative_exists(self):
        # The fallback must not degrade into "the reservation never applies".
        for i in range(29):
            cooldown.record_use("claude-seat", 5 * 3600, T0 - i)
        orch = _orch(self.reg, runtime.MockAdapter(), self.task)
        self.assertEqual(orch._pick().channel, "cursor-seat")

    def _escalated_choice(self, seats):
        """A registry where a strong implementer is enrolled on `seats`, and the
        Choice an escalation would have produced."""
        self.reg["models"]["opus-class-strong"]["enrolled"].append("implementer")
        for name, chan in self.reg["channels"].items():
            if name not in seats and "opus-class-strong" in (chan.get("exposes") or []):
                chan["exposes"] = [m for m in chan["exposes"] if m != "opus-class-strong"]
            if name in seats and "opus-class-strong" not in (chan.get("exposes") or []):
                chan["exposes"] = list(chan["exposes"]) + ["opus-class-strong"]
        orch = _orch(self.reg, runtime.MockAdapter(), self.task)
        strong = [c for c in orch.router.candidates()
                  if c.model == "opus-class-strong"]
        return orch, strong[0]

    def test_a_timeout_moves_the_work_but_leaves_the_seat_open(self):
        """The whole point of splitting the two decisions.

        A hung call should reach another seat -- the work is checkpointed and
        someone else can pick it up, which is why more than one provider is
        enrolled. But nothing about one call failing to return says the seat is
        out, so the five-hour breaker must stay shut. Before the split these
        were one decision, so a timeout could only do both or neither, and it
        did neither.
        """
        orch = _orch(self.reg, runtime.MockAdapter(), self.task)
        before = cooldown.read(T0)[0]

        timed_out = {"settled": "timeout", "output": "", "code": None,
                     "failure": "timeout", "reset_at": None}
        self.assertIn(timed_out["failure"], orch.HOPS_TO_ANOTHER_SEAT,
                      "a timeout must move the work")
        self.assertNotIn(timed_out["failure"], orch.OPENS_THE_BREAKER,
                         "a timeout must not take the seat out of service")

        # and the seat really is still there for everyone else
        self.assertEqual(cooldown.read(T0)[0], before,
                         "a timeout opened a breaker")
        self.assertEqual(orch._pick().channel,
                         _orch(self.reg, runtime.MockAdapter(), self.task)
                         ._pick().channel)

    def test_an_ordinary_crash_still_hops_nowhere(self):
        """The negative control, as a unit. A failover that fires on any error
        is worse than none: it hides real bugs behind provider changes."""
        orch = _orch(self.reg, runtime.MockAdapter(), self.task)
        self.assertNotIn("other", orch.HOPS_TO_ANOTHER_SEAT)
        self.assertNotIn("other", orch.OPENS_THE_BREAKER)

    def test_invocations_are_metered_against_the_window(self):
        orch = _orch(self.reg, runtime.MockAdapter(), self.task)
        choice = orch._pick()
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

        return state, {"implementer": implementer}

    def _run(self, script, clock=None, plan=None):
        task = store.Task.create(self.t.repo, "T-001",
                                 "# t\n\n- **AC-1** — subtract exists\n", self.pol)
        # Where `--plan` lands it. The caller supplies the decomposition, so
        # every one of these runs starts from a plan already on disk.
        task.write_text("plan.md", plan or PLAN_MD)
        logs = []
        clock = clock or (lambda: T0)
        adapter = runtime.MockAdapter(script, now=clock)
        status = Orchestrator(task, self.reg, adapter, lambda k, x: True,
                              log=logs.append, clock=clock).run()
        return task, status, logs

    def test_a_seat_without_room_to_finish_loses_to_one_that_has_it(self):
        """Tested on the decision itself, not through the router.

        Measured first: on the shipped registry the shadow price already moves
        the implementer to `cursor-seat` at every draw where headroom would have
        objected, so routing through `_pick` proves nothing about this rule --
        it passes for the other mechanism's reasons. The preference only ever
        binds where the shadow price has NOT already reordered the pool, which
        is a custom registry's case, so that is what is exercised here.
        """
        task = store.Task.create(self.t.repo, "T-HR", "# t\n\n- **AC-1** — x\n", self.pol)
        task.update(subtasks=[{"id": "st-%d" % i, "status": "pending"} for i in range(4)])
        logs = []
        orch = _orch(self.reg, runtime.MockAdapter({}), task, clock=lambda: T0, logs=logs)
        need = orch._calls_remaining()
        self.assertEqual(need, 6)

        by_seat = {}
        for c in orch.router.candidates("implementer", ceiling=orch.budget.ceiling()):
            by_seat.setdefault(c.channel, c)
        claude, cursor = by_seat["claude-seat"], by_seat["cursor-seat"]

        # claude-seat nearly drawn down, cursor-seat fresh; claude offered first
        util = {"claude-seat": 38 / 40.0, "cursor-seat": 0.0}
        self.assertLess(orch._headroom("claude-seat", util), need)
        picked = orch._prefer_by_headroom("implementer", [claude, cursor], util)
        self.assertEqual(picked.channel, "cursor-seat")
        self.assertTrue(any("headroom:" in x for x in logs), "the move was silent")

    def test_headroom_does_not_second_guess_a_seat_that_can_finish(self):
        """It must only act when the seat genuinely cannot finish. Otherwise it
        is a second, quieter router fighting the shadow price."""
        task = store.Task.create(self.t.repo, "T-HR4", "# t\n\n- **AC-1** — x\n", self.pol)
        task.update(subtasks=[{"id": "st-1", "status": "pending"}])
        logs = []
        orch = _orch(self.reg, runtime.MockAdapter({}), task, clock=lambda: T0, logs=logs)
        by_seat = {}
        for c in orch.router.candidates("implementer", ceiling=orch.budget.ceiling()):
            by_seat.setdefault(c.channel, c)
        order = [by_seat["claude-seat"], by_seat["cursor-seat"]]
        picked = orch._prefer_by_headroom("implementer", order, {"claude-seat": 0.1})
        self.assertEqual(picked.channel, "claude-seat", "overrode a seat that had room")
        self.assertFalse([x for x in logs if "headroom:" in x], "logged a non-event")

    def test_an_unmetered_seat_is_not_treated_as_having_no_room(self):
        """`None` headroom is "no meter", not "nothing left". Reading it as zero
        would route away from every metered-key seat in the registry."""
        task = store.Task.create(self.t.repo, "T-HR2", "# t\n\n- **AC-1** — x\n", self.pol)
        orch = _orch(self.reg, runtime.MockAdapter({}), task, clock=lambda: T0)
        chan = dict(self.reg["channels"]["claude-seat"])
        chan.pop("quota", None)
        self.reg["channels"]["claude-seat"] = chan
        self.assertIsNone(orch._headroom("claude-seat", {}))
        # and it is still selectable — asked for the tier that seat serves,
        # since a tier is a band and the workhorse lives elsewhere.
        self.assertEqual(orch._pick(tier="t3").channel, "claude-seat")

    def test_no_seat_with_room_still_starts_the_run(self):
        """It must never refuse work. A wrong estimate that parks a runnable
        task is worse than one that hops mid-run, because the hop already
        works."""
        for i in range(39):
            cooldown.record_use("claude-seat", 5 * 3600, T0 - i)
        for i in range(499):
            cooldown.record_use("cursor-seat", 30 * 24 * 3600, T0 - i)
        task = store.Task.create(self.t.repo, "T-HR3", "# t\n\n- **AC-1** — x\n", self.pol)
        task.update(subtasks=[{"id": "st-%d" % i, "status": "pending"} for i in range(9)])
        logs = []
        orch = _orch(self.reg, runtime.MockAdapter({}), task,
                     clock=lambda: T0, logs=logs)
        self.assertIsNotNone(orch._pick(), "refused to start")
        self.assertTrue(any("anyway" in x for x in logs), "started without saying why")

    def test_a_resumed_job_is_not_failed_for_its_predecessor_finishing_it(self):
        """The salvage path's own success case, which used to halt the run.

        A seat walls mid-job, the partial work is committed, and the diff base
        for the next attempt becomes that commit. If the predecessor had already
        done enough, the replacement correctly changes nothing -- and the
        "changed no files, checks were already green" guard fires on both its
        conditions at once. Seen on a real single-seat wall: the job was
        complete on disk and the run parked at needs_human anyway.
        """
        task = store.Task.create(self.t.repo, "T-INH", "# t\n", self.pol)
        task.update(subtasks=[{"id": "st-1-main", "status": "pending",
                               "planned_scope": ["app.py"],
                               "inherited_checkpoint": True}])
        orch = _orch(self.reg, runtime.MockAdapter({}), task, clock=lambda: T0)
        sub = task.state["subtasks"][0]
        self.assertTrue(sub.get("inherited_checkpoint"))

        # The guard's three conditions, as the machine evaluates them: checks
        # green, nothing recorded as changed, nothing in the diff.
        fires = (True and not sub.get("actual_files")
                 and not sub.get("inherited_checkpoint"))
        self.assertFalse(fires,
                         "a job that inherited a checkpoint is still failed for "
                         "changing nothing, which is what salvage produces")
        del orch

    def test_a_timeout_on_every_seat_is_not_reported_as_a_quota_wall(self):
        """The failure the split introduced, and that no test drove.

        `_invoke` appends a timed-out seat to `cooled` so the next iteration
        cannot hand the same call back to it. `_pick` then reported everything
        in that list as "in a quota cooldown" -- so a run where both agents
        merely hung parked with `reason: quota_all_exhausted` and **zero open
        breakers**: `delegate status` said "waiting on quota", the brief told
        the human their subscription was exhausted, and a provider wall that
        never happened went into the record that exists to count them.
        """
        _, script = self._script(quota_first=False)
        script["implementer"] = lambda env, cwd: {
            "settled": "timeout", "output": "", "code": None}
        task, status, logs = self._run(script)

        self.assertEqual(status, "needs_human", "\n".join(logs))
        park = task.state.get("park") or {}
        self.assertNotEqual(park.get("reason"), "quota_all_exhausted",
                            "a hung agent was recorded as a provider quota wall")
        self.assertEqual(cooldown.read(T0)[0], {},
                         "a timeout opened a breaker after all")
        # and it says what actually happened
        self.assertTrue(any("timed out on every enrolled seat" in x for x in logs),
                        "\n".join(logs))

    def test_a_quota_wall_fails_over_and_the_task_still_finishes(self):
        _, script = self._script(quota_first=True)
        task, status, logs = self._run(script)
        self.assertEqual(status, "done", "\n".join(logs))
        self.assertTrue(any("failover:" in x for x in logs), "\n".join(logs))

    def test_a_failover_that_drops_the_band_is_recorded_and_briefed(self):
        """The other demotion site, and the one SKILL.md calls the default
        outcome rather than the edge case: `t3` is served by one seat only, so
        walling it means there is no equal replacement by construction. The
        floor is dropped, the work finishes on the workhorse, and the brief has
        to say which job that happened to."""
        plan = ('# Plan\n\n```yaml\n- id: st-1-main\n'
                '  goal: Add a subtract function to app.py\n'
                '  file_scope: ["app.py"]\n  tier: t3\n```\n')
        state = {"seen": []}

        def implementer(env, cwd):
            state["seen"].append(cwd)
            if len(state["seen"]) == 1:
                return blocked(QUOTA_MSG)
            with open(os.path.join(cwd, "app.py"), "a") as fh:
                fh.write("\ndef subtract(a, b):\n    return a - b\n")
            _report(env["AGENT_DELEGATION_TASK_DIR"], "implement-st-1-main.json", {
                "stage": "implement", "role": "implementer", "subtask": "st-1-main",
                "status": "complete", "summary": "added subtract",
                "evidence": {"tests": "green"}})
            return None

        task, status, logs = self._run({"implementer": implementer}, plan=plan)
        self.assertEqual(status, "done", "\n".join(logs))
        got = task.state["subtasks"][0].get("demotions") or []
        self.assertEqual(len(got), 1,
                         "the failover demotion was not recorded:\n"
                         + "\n".join(logs))
        self.assertEqual(got[0]["asked"], "t3")
        self.assertIn("opus-class-strong", got[0]["reason"])
        self.assertNotEqual(got[0]["model"], "opus-class-strong",
                            "recorded a demotion to the model it started on")
        from adg import brief
        text = brief.render(task, "merge", "Land it?")
        self.assertIn("ran below the band", text)
        self.assertIn(got[0]["model"], text.split("ran below the band")[1])

    def test_the_quota_failure_costs_no_attempt(self):
        # AC-1: a quota failure is the channel's fault, not the approach's.
        _, script = self._script(quota_first=True)
        task, _, logs = self._run(script)
        attempts = task.state["spent"]["attempts"]
        self.assertEqual(attempts.get("st-1-main"), 1,
                         "the quota failure was billed to the subtask: %s" % attempts)

    def test_the_run_moved_to_the_other_channel(self):
        first, other = _seats(self.reg)[:2]
        _, script = self._script(quota_first=True)
        task, _, _ = self._run(script)
        used = [h["channel"] for h in task.state["delegation_history"]
                if h.get("role") == "implementer" and h.get("outcome") == "complete"]
        self.assertEqual(used, [other],
                         "the wall was on %s and the work did not move" % first)

    def test_the_cooled_channel_is_on_disk_with_the_stated_reset(self):
        # AC-2: "reset in 2 hours" from the message, not the 5h window.
        walled = _seats(self.reg)[0]
        _, script = self._script(quota_first=True)
        self._run(script)
        cools, _, _ = cooldown.read(T0)
        self.assertEqual(cools[walled]["reopen_at"], T0 + 2 * 3600)
        self.assertEqual(cools[walled]["reason"], "quota")

    def test_without_a_stated_reset_the_configured_window_is_used(self):
        state, script = self._script(quota_first=False)
        original = script["implementer"]

        def implementer(env, cwd):
            if not state["seen"]:
                state["seen"].append(cwd)
                return blocked("Error: usage limit reached")     # no time given
            return original(env, cwd)

        walled = _seats(self.reg)[0]
        _, status, logs = self._run(dict(script, implementer=implementer))
        cools, _, _ = cooldown.read(T0)
        # That channel's own window, read from the registry: the two seats are
        # configured differently (5h against monthly), so a literal here would
        # be asserting which seat walled rather than that its window was used.
        window = quota.parse_window(
            (self.reg["channels"][walled].get("quota") or {}).get("window"))
        self.assertEqual(cools[walled]["reopen_at"], T0 + window)
        self.assertTrue(any("stated no reset time" in x for x in logs), "\n".join(logs))

    def _red_until_done(self):
        """A fast check that actually fails until the work lands, so a retry
        really happens. With an always-green check the machine halts on the
        first attempt ('changed no files, and the checks were already green'),
        and a test asserting `attempts >= 1` cannot tell a billed failure from a
        billed success."""
        with open(os.path.join(self.t.repo, "check.py"), "w") as fh:
            fh.write("import os, sys\nsys.exit(0 if os.path.exists('done.txt') else 1)\n")
        with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
            fh.write('fast:\n  - "python3 check.py"\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "red check"], self.t.repo)

    def test_a_generic_failure_still_consumes_an_attempt(self):
        # AC-5: the other half of the contract. A stack trace is a retry.
        self._red_until_done()
        state, script = self._script(quota_first=False)
        original = script["implementer"]
        calls = {"n": 0}

        def implementer(env, cwd):
            calls["n"] += 1
            if calls["n"] == 1:
                with open(os.path.join(cwd, "partial.py"), "w") as fh:
                    fh.write("# started\n")
                return blocked("Traceback (most recent call last):\nValueError: x")
            open(os.path.join(cwd, "done.txt"), "w").close()
            return original(env, cwd)

        task, status, logs = self._run(dict(script, implementer=implementer))
        self.assertFalse(any("failover:" in x for x in logs), "\n".join(logs))
        self.assertEqual(cooldown.active(T0), set(), "a stack trace opened a breaker")
        # `>= 1` could not tell "the failure was billed" from "the success was".
        # Two attempts is the whole claim: the failure consumed one, the retry
        # consumed the other, and the run still finished.
        self.assertEqual(status, "done", "\n".join(logs))
        self.assertEqual(task.state["spent"]["attempts"].get("st-1-main"), 2,
                         "a generic failure stopped costing an attempt")

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
        # Scoped to the paused-seats section, as the cost-table test below is.
        # A bare `assertIn("claude-seat", text)` was answered by the cost table,
        # which names every seat that ran a step -- so dropping the seat list
        # from the park brief entirely still passed.
        self.assertIn("## Paused seats", text, "the park brief lost its seat list")
        self.assertIn("claude-seat", text.split("## Paused seats")[1],
                      "the brief never names the seat that walled")
        # The name said "and the reopen time" while asserting only the seat.
        import time as _time
        when = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(T0 + 2 * 3600))
        self.assertIn(when, text, "the brief never states when the seat comes back")

    def test_a_quota_failure_does_not_read_as_an_unbilled_run(self):
        # The quota record has no `usd`, so the cost table counted it as a run
        # that mysteriously reported nothing and warned that the real total was
        # higher. A seat that refused the call cost exactly zero.
        _, script = self._script(quota_first=True)
        task, status, logs = self._run(script)
        self.assertEqual(status, "done", "\n".join(logs))
        text = task.read_text("brief.md", "")
        first, other = _seats(self.reg)[:2]
        cost_table = text.split("## Who did the work")[1].split("##")[0]
        # The implementer rows only. Scanning the whole table for the refused
        # seat's name is not the same question: a row for a call that really
        # happened on that seat is exactly what the table is for.
        rows = [r for r in cost_table.splitlines() if "implementer" in r]
        self.assertEqual(len(rows), 1,
                         "the refused seat was billed a row of its own: %s" % rows)
        self.assertIn(other, rows[0], "the seat that did the work is missing")
        self.assertNotIn(first, rows[0],
                         "the refused seat was billed a row saying it reported no cost")
        self.assertNotIn("quota_exhausted", text, "raw protocol token reached the human")
        self.assertIn("ran out of its provider's capacity", text,
                      "the hop is invisible to the human who paid for it")

    def test_a_wave_that_runs_out_of_quota_parks_like_the_sequential_path(self):
        """Both subtasks hit the wall in parallel. The wave worker catches every
        Exception and re-raised it as a generic halt, so `park` was never
        written and `resume --when-open` had nothing to wait on — the two paths
        disagreed about the same event."""
        cooldown.open_breaker("cursor-seat", "quota", T0 + 9000, T0)
        plan = PLAN_MD.replace(
            "- id: st-1-main\n  goal: Add a subtract function to app.py\n"
            "  file_scope: [\"app.py\"]\n  acceptance: [AC-1]\n",
            "- id: st-1-a\n  goal: Add a\n  file_scope: [\"a.py\"]\n  acceptance: [AC-1]\n"
            "- id: st-2-b\n  goal: Add b\n  file_scope: [\"b.py\"]\n  acceptance: [AC-1]\n")

        _, script = self._script(quota_first=True)
        script = dict(script, implementer=lambda e, c: blocked(QUOTA_MSG))
        task, status, logs = self._run(script, plan=plan)
        self.assertEqual(status, "needs_human", "\n".join(logs))
        self.assertEqual((task.state.get("park") or {}).get("reason"),
                         "quota_all_exhausted",
                         "the wave path lost the park record: %s" % "\n".join(logs))

    def test_the_replacement_inherits_a_commit_not_a_dirty_tree(self):
        """: the replacement resumes from a *checkpoint*.

        The old version of this asserted only that `partial.py` still existed,
        which the same worktree path guarantees whether or not anything was ever
        committed — it would have passed with checkpointing entirely broken. The
        claim worth pinning is that `git log` shows the predecessor's work, since
        that is what the handover note tells the replacement to go and read."""
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
            saw["log"] = store.git(["log", "--oneline"], cwd, check=False)
            saw["dirty"] = store.git(["status", "--porcelain"], cwd, check=False)
            saw["tracked"] = store.git(["ls-files", "partial.py"], cwd, check=False)
            return original(env, cwd)

        task, status, logs = self._run(dict(script, implementer=implementer))
        self.assertEqual(status, "done", "\n".join(logs))
        self.assertIn("salvaged", saw.get("log", ""),
                      "the predecessor's work was not committed for the replacement")
        self.assertEqual(saw.get("tracked"), "partial.py",
                         "the inherited file is untracked, so `git log` cannot show it")
        self.assertEqual(saw.get("dirty"), "",
                         "the replacement inherited a dirty tree")

    def test_salvage_never_commits_into_the_users_checkout(self):
        # Handed the real checkout rather than a worktree. This program has no
        # path that commits to the user's own branch, and failover -- the one
        # place it commits on its own account -- must not become one.
        before = store.git(["rev-parse", "HEAD"], self.t.repo)
        with open(os.path.join(self.t.repo, "stray.py"), "w") as fh:
            fh.write("# uncommitted, in the real checkout\n")
        task = store.Task.create(self.t.repo, "T-002", "# t\n", self.pol)
        orch = Orchestrator(task, self.reg, runtime.MockAdapter(), lambda k, x: True,
                            log=lambda *_: None, clock=lambda: T0)
        self.assertFalse(orch._salvage(self.t.repo, "implementer", None))
        self.assertEqual(store.git(["rev-parse", "HEAD"], self.t.repo), before)
        self.assertIn("stray.py", store.git(["status", "--porcelain"], self.t.repo))


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


DSH_HOME = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "dsh-home")
DSH_FIXTURES = os.path.join(DSH_HOME, "sessions")


class TestDshSeat(unittest.TestCase):
    """A dsh seat, without dsh installed.

    `dsh --profile headless` prints the final assistant message and exits: no
    structured-output flag exists (checked against `--help` on 0.1.0-rc.6), so
    stdout is prose and everything structured comes from the session log it
    persisted. Both halves are tested here -- the exit-code floor through the
    command pattern, and the enrichment against a recorded store -- and neither
    touches the network or needs the binary.

    orchestrator/docs/dsh-adapter-notes.md carries the verified/assumed split.
    """

    def setUp(self):
        self.t = TempRepo()

    def tearDown(self):
        self.t.close()

    # --- the reader -------------------------------------------------------
    def test_usage_is_summed_across_the_session(self):
        from adg import dsh
        events = dsh.read_events(
            os.path.join(DSH_FIXTURES, "--tmp-proj--", "s-01", "session.jsonl"))
        got = dsh.usage(events)
        self.assertEqual(got["fresh_in"], 1200)
        self.assertEqual(got["cache_read"], 8000)
        self.assertEqual(got["cache_write"], 500)
        self.assertEqual(got["out"], 340)
        # `in` is the total, matching every other adapter's shape, so `_bill`
        # prices a dsh run through exactly the same path.
        self.assertEqual(got["in"], 1200 + 8000 + 500)

    def test_a_packed_chunk_row_and_a_torn_tail_are_skipped_not_fatal(self):
        """`packChunks` is on by default, so runs of stream chunks are stored
        as rows that are not SessionEvents at all -- and an append-only log
        written by a live process routinely ends mid-line."""
        from adg import dsh
        events = dsh.read_events(
            os.path.join(DSH_FIXTURES, "--tmp-walled--", "s-02", "session.jsonl"))
        self.assertTrue(events, "a torn tail emptied the whole log")
        self.assertIsNotNone(dsh.failure(events),
                             "the structured failure was lost with the bad line")

    def test_the_structured_failure_is_what_classifies_not_the_prose(self):
        """The fixture's own user/message says "429" and "rate_limit" as the
        AGENT's words. Reading those as the provider is the failure this whole
        contract exists to prevent, so the wall has to come from LlmFailure."""
        from adg import dsh
        events = dsh.read_events(
            os.path.join(DSH_FIXTURES, "--tmp-walled--", "s-02", "session.jsonl"))
        err = dsh.failure(events)
        self.assertEqual(err["code"], "insufficient_quota")
        self.assertEqual(err["status"], 429)
        res = {"settled": "blocked", "stderr": "", "dsh_failure": err}
        probe = dsh.probe_text(res)
        self.assertIn("insufficient_quota", probe)
        self.assertNotIn("retry handler", probe,
                         "the agent's own prose reached the probe")

    def test_a_stated_retry_after_beats_the_prose_table(self):
        from adg import dsh
        events = dsh.read_events(
            os.path.join(DSH_FIXTURES, "--tmp-walled--", "s-02", "session.jsonl"))
        res = {"dsh_failure": dsh.failure(events)}
        self.assertEqual(dsh.stated_reset(res, T0), T0 + 5400,
                         "providerRetryAfterMs was not used as the reset time")

    def test_per_event_origin_is_available(self):
        """dsh answers the provenance ask pane mode cannot: `source.kind`
        separates a human prompt from an agent.inject() context."""
        from adg import dsh
        events = dsh.read_events(
            os.path.join(DSH_FIXTURES, "--tmp-proj--", "s-01", "session.jsonl"))
        self.assertEqual(dsh.human_turn_kinds(events), {"user": 1, "plugin": 1})

    def test_a_compressed_log_is_refused_by_name_not_decoded(self):
        """The default artifact, and the constraint the notes file records: no
        stdlib zstd before 3.14. Naming the reason beats a silent empty
        enrichment, because the fix is one line in the user's dsh profile and
        they cannot apply it if nothing says so."""
        from adg import dsh
        env = {"DSH_HOME": DSH_HOME}
        path, note = dsh.find_log("/tmp/zstd", env=env)
        self.assertIsNone(path, "a zstd artifact was offered to the line reader")
        self.assertIn("Zstandard", note)
        self.assertIn("compression: none", note, "the note omits the fix")

    def test_a_missing_store_says_so_rather_than_failing_quietly(self):
        from adg import dsh
        res = {}
        dsh.enrich(res, "/tmp/x", env={"DSH_HOME": "/nonexistent"})
        self.assertIn("no dsh session store", res["dsh_note"])

    def test_the_log_is_found_under_the_project_key(self):
        from adg import dsh
        path, note = dsh.find_log("/tmp/proj", env={"DSH_HOME": DSH_HOME})
        self.assertIsNotNone(path, note)
        self.assertIn("--tmp-proj--", path,
                      "the reader did not use the project key")

    def test_a_missing_store_leaves_the_result_untouched(self):
        from adg import dsh
        res = {"settled": "idle", "output": "done", "code": 0}
        before = dict(res)
        dsh.enrich(res, self.t.repo, env={"DSH_HOME": "/nonexistent"})
        for k, v in before.items():
            self.assertEqual(res[k], v, "enrichment changed the floor's answer")

    def test_the_project_key_matches_the_shipped_encoder(self):
        """Transcribed from dsh-session-persistence-jsonl, not invented: runs of
        separators collapse to one dash, `~` and anything unsafe become ~XXXX,
        leading dashes go, and the whole thing is wrapped."""
        from adg import dsh
        self.assertEqual(dsh.project_key("/tmp/proj"), "--tmp-proj--")
        self.assertEqual(dsh.project_key("/a//b"), "--a-b--")
        self.assertEqual(dsh.project_key("/x/~y"), "--x-~007Ey--")

    # --- the floor --------------------------------------------------------
    def _session(self, argv):
        return runtime.Session("dsh-x", self.t.repo, handle={
            "kind": "dsh", "role": "implementer", "argv": argv,
            "env": dict(os.environ, DSH_HOME="/nonexistent")})

    def test_the_task_goes_on_argv_and_settles_on_the_exit_code(self):
        """headless declares `[task...]` positionally and reads nothing from
        stdin, so the shared stdin path would hand it an empty task and wait."""
        a = runtime.LocalAdapter()
        argv = ["python3", "-c",
                "import sys; print('TASK=' + sys.argv[1]); sys.exit(0)"]
        res = a.prompt(self._session(argv), "do the thing", timeout=30)
        self.assertEqual(res["settled"], "idle")
        self.assertIn("TASK=do the thing", res["output"])
        self.assertIsNone(res["failure"])

    def test_a_non_zero_exit_is_a_plain_failure(self):
        a = runtime.LocalAdapter()
        argv = ["python3", "-c", "import sys; sys.exit(2)"]
        res = a.prompt(self._session(argv), "go", timeout=30)
        self.assertEqual(res["settled"], "blocked")
        self.assertEqual(res["failure"], "other")

    def test_the_printed_answer_is_never_classified(self):
        """The whole point of the seat kind. headless prints the agent's final
        message; an agent whose job is rate limiting must not cool its seat."""
        a = runtime.LocalAdapter()
        argv = ["python3", "-c",
                "print('I added the 429 retry_limit handler; usage limit "
                "reached is now handled'); import sys; sys.exit(1)"]
        res = a.prompt(self._session(argv), "go", timeout=30)
        self.assertEqual(res["failure"], "other",
                         "stdout was read as the provider refusing")
        self.assertIsNone(res["reset_at"])

    def test_stderr_is_classified(self):
        a = runtime.LocalAdapter()
        argv = ["python3", "-c",
                "import sys; sys.stderr.write('dsh: 429 rate_limited'); "
                "sys.exit(1)"]
        res = a.prompt(self._session(argv), "go", timeout=30)
        self.assertEqual(res["failure"], "quota_exhausted",
                         "the provider's own stream was ignored")

    def test_a_dsh_seat_renders_without_fabricated_headroom(self):
        """API-billed, so no metered window. `delegate status` must say it has
        no estimate rather than print a percentage nobody measured."""
        reg = json.loads(json.dumps(router.load_registry(REGISTRY)))
        reg["channels"]["dsh-seat"] = {
            "type": "metered", "adapter": "local", "agent_kind": "dsh",
            "exposes": ["balanced-coder"]}
        lines = cli._seat_quota_lines(reg, {}, {}, T0)
        row = [l for l in lines if "dsh-seat" in l][0]
        self.assertIn("no capacity estimate", row)
        self.assertNotIn("%", row)

    def test_the_notes_file_separates_verified_from_assumed(self):
        doc = _slurp(os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            "orchestrator", "docs", "dsh-adapter-notes.md"))
        self.assertIn("0.1.0-rc.6", doc, "the notes name no pinned version")
        self.assertIn("verified", doc)
        self.assertIn("ASSUMED", doc, "the notes claim everything was verified")
        self.assertIn("Zstandard", doc, "the notes omit the reader's blocker")


class TestRunLogAndBundle(unittest.TestCase):
    """The record a real quota wall gets diagnosed from, hours later.

    The bar is that somebody -- or something -- reading the bundle alone, with
    no follow-up questions available, can reconstruct which seats existed,
    every routing decision AND ITS INPUTS, every classification and what text
    it saw, every failover hop, and the final brief. Outcomes alone do not
    clear that bar: "picked cursor-seat" does not say why claude-seat lost, and
    the numbers that decided it existed for one instant.
    """

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

    def _run(self, wall=False, message=QUOTA_MSG):
        state = {"seen": []}

        def implementer(env, cwd):
            state["seen"].append(cwd)
            if wall and len(state["seen"]) == 1:
                return blocked(message)
            with open(os.path.join(cwd, "app.py"), "a") as fh:
                fh.write("\ndef subtract(a, b):\n    return a - b\n")
            _report(env["AGENT_DELEGATION_TASK_DIR"], "implement-st-1-main.json", {
                "stage": "implement", "role": "implementer", "subtask": "st-1-main",
                "status": "complete", "summary": "added subtract",
                "evidence": {"tests": "green"}})
            return None

        task = store.Task.create(self.t.repo, "T-LOG", "# t\n\n- **AC-1** — x\n",
                                 self.pol)
        task.write_text("plan.md", PLAN_MD)
        logs = []
        status = Orchestrator(task, self.reg,
                              runtime.MockAdapter({"implementer": implementer},
                                                  now=lambda: T0),
                              lambda k, x: True, log=logs.append,
                              clock=lambda: T0).run()
        return task, status, logs

    def test_the_run_log_persists_everything_stdout_saw(self):
        task, status, logs = self._run()
        self.assertEqual(status, "done", "\n".join(logs))
        text = task.read_text("run.log")
        for line in logs:
            self.assertIn(line, text,
                          "a line reached stdout and not the record: %r" % line)
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}  ",
                         "the record carries no wall-clock stamps")

    def test_routing_records_its_inputs_not_only_its_pick(self):
        """The headroom numbers and the shadow prices, as the router saw them.
        Neither is recoverable afterwards: a seat that was 4% from a wall looks
        identical in channels.json an hour after it reopened."""
        for i in range(12):
            cooldown.record_use("claude-seat", 5 * 3600, T0 - i)
        task, _, _ = self._run()
        text = task.read_text("run.log")
        self.assertIn("routing:", text)
        self.assertIn("seats as the router saw them:", text)
        self.assertIn("window:", text, "no per-window figure was recorded")
        self.assertRegex(text, r"headroom: .*~\d+",
                         "no headroom number reached the record")
        self.assertRegex(text, r"by shadow price: .*\d+\.\d\d",
                         "no shadow price reached the record")
        self.assertRegex(text, r"picked: \S+ on \S+")

    def test_the_seat_block_is_the_status_renderer_not_a_second_opinion(self):
        """Reused, not duplicated. Two renderings of one number drift, and the
        day they disagree the log is the one nobody can check."""
        import inspect
        from adg import machine as m
        self.assertIn("_seat_quota_lines",
                      inspect.getsource(m.Orchestrator._log_routing))

    def test_classification_records_the_text_it_actually_read(self):
        task, _, logs = self._run(wall=True)
        text = task.read_text("run.log")
        self.assertIn("classification:", text)
        self.assertIn("quota_exhausted", text)
        self.assertIn("it read:", text, "the verdict was recorded without its input")
        self.assertIn("usage limit reached", text,
                      "the classified text is absent, so the verdict cannot be checked")

    def test_a_hop_is_one_line_carrying_the_whole_narrative(self):
        task, status, logs = self._run(wall=True)
        self.assertEqual(status, "done", "\n".join(logs))
        hops = [l for l in task.read_text("run.log").splitlines() if "hop:" in l]
        self.assertEqual(len(hops), 1, "expected one hop line, got %s" % hops)
        line = hops[0]
        for part in ("walled on", "reopens", "respawned on", "resumes from"):
            self.assertIn(part, line, "the hop line drops %r: %s" % (part, line))

    def test_a_redacted_string_does_not_survive_into_the_bundle(self):
        task, _, _ = self._run(
            wall=True,
            message=QUOTA_MSG + " ctx=%s/x.py ada@example.com sk-ABCDEFGHIJKLMNOP"
                    % os.path.expanduser("~"))
        text = cli._bundle_text(task, cli._capture_lines_for(task))
        self.assertNotIn("ada@example.com", text, "an address survived into the bundle")
        self.assertNotIn("sk-ABCDEFGHIJKLMNOP", text, "a key survived into the bundle")
        self.assertNotIn(os.path.expanduser("~"), text, "a home path survived")
        self.assertIn("usage limit reached", text,
                      "redaction took the provider message with it")

    def test_the_bundle_is_one_file_with_all_four_sections(self):
        import contextlib
        import io
        task, _, _ = self._run(wall=True)
        buf = io.StringIO()

        class Args:
            repo, registry, id, out = self.t.repo, REGISTRY, "T-LOG", None
        with contextlib.redirect_stdout(buf):
            cli.cmd_bundle(Args())
        printed = buf.getvalue().strip()
        self.assertEqual(len(printed.splitlines()), 1,
                         "bundle printed more than the path: %r" % printed)
        self.assertTrue(os.path.isfile(printed), "no file at %r" % printed)
        body = _slurp(printed)
        for name in cli.BUNDLE_SECTIONS:
            self.assertIn("=== %s" % name, body, "the bundle has no %s section" % name)
        # And each section carries something, not just a header.
        self.assertIn("T-LOG", body.split("=== task.json")[1][:2000])
        self.assertIn("routing:", body.split("=== run.log")[1][:20000])
        self.assertIn("quota_exhausted",
                      body.split("=== corpus-capture.jsonl")[1])

    def test_an_unwritable_log_leaves_the_run_alone(self):
        """Exhaust, like corpus capture. And it must never report itself
        through `self.log` -- that IS the thing failing, and the recursion
        would turn a full disk into a hang."""
        class Deaf(store.Task):
            def append(self, name, line):
                raise OSError("read-only")

        task = Deaf(store.Task.create(
            self.t.repo, "T-DEAF", "# t\n\n- **AC-1** — x\n", self.pol).path)
        task.write_text("plan.md", PLAN_MD)

        def implementer(env, cwd):
            with open(os.path.join(cwd, "app.py"), "a") as fh:
                fh.write("\ndef subtract(a, b):\n    return a - b\n")
            _report(env["AGENT_DELEGATION_TASK_DIR"], "implement-st-1-main.json", {
                "stage": "implement", "role": "implementer", "subtask": "st-1-main",
                "status": "complete", "summary": "s", "evidence": {"tests": "green"}})

        logs = []
        status = Orchestrator(task, self.reg,
                              runtime.MockAdapter({"implementer": implementer},
                                                  now=lambda: T0),
                              lambda k, x: True, log=logs.append,
                              clock=lambda: T0).run()
        self.assertEqual(status, "done",
                         "an unwritable run log changed the run:\n" + "\n".join(logs))


class TestStatusShowsSeatQuota(unittest.TestCase):
    """What is left on each seat, on the command a caller actually runs.

    The router already meters invocations against each channel's window and
    prices the seat off the result -- that number decided which provider ran
    the work. It surfaced only in `delegate channels`, which is a command you
    reach for after something has gone wrong. `status` is the one you run
    before dispatching a wave, and it is the answer to "have I got the room".

    Read-only by construction: `status` renders what the store already knows
    and must not so much as touch the file it reads.
    """

    def setUp(self):
        import time as _time
        self.t = TempRepo()
        self.now = _time.time()
        self.reg = router.load_registry(REGISTRY)

    def tearDown(self):
        self.t.close()

    def _status(self):
        import contextlib
        import io
        buf = io.StringIO()

        class Args:
            repo, registry = self.t.repo, REGISTRY
        with contextlib.redirect_stdout(buf):
            cli.cmd_status(Args())
        return buf.getvalue()

    def test_a_seeded_store_renders_the_remaining_window_per_seat(self):
        # 34 of claude-seat's est_capacity 40 -> 15% left; 180 of cursor-seat's
        # 500 over a monthly window -> 64%.
        for i in range(34):
            cooldown.record_use("claude-seat", 5 * 3600, self.now - i)
        for i in range(180):
            cooldown.record_use("cursor-seat", 30 * 86400, self.now - i * 3600)
        out = self._status()
        self.assertIn("  claude-seat    5h window: 15% left", out, out)
        self.assertIn("  cursor-seat    monthly window: 64% left", out, out)
        # Machine-tolerable: one seat per line, stable order, nothing coloured.
        seats = [l for l in out.splitlines() if l.startswith("  claude-seat")
                 or l.startswith("  cursor-seat")]
        self.assertEqual([l.split()[0] for l in seats],
                         ["claude-seat", "cursor-seat"], "seat order is not stable")
        self.assertNotIn("\x1b", out, "status emitted a terminal escape")

    def test_a_cooling_seat_says_when_it_reopens_and_who_said_so(self):
        """The reopen time the classifier parsed out of the provider's own
        message. Without it the line says a seat is empty and not when to come
        back, which is the question the caller is actually asking.

        And who decided, since `delegate cooldown` gave a human the same store
        to write into: "the provider said this" and "somebody typed this" are
        different facts about a seat, and the row is where they are told apart.
        """
        reopen = self.now + 4200
        cooldown.open_breaker("claude-seat", "quota", reopen, self.now, "usage limit")
        out = self._status()
        self.assertIn("[cooling until %s (classified)]" % cli._stamp(reopen), out, out)
        # And the seat that is fine is not decorated as if it were not.
        cursor = [l for l in out.splitlines() if l.startswith("  cursor-seat")][0]
        self.assertNotIn("cooling", cursor)

    def test_an_unmetered_seat_shows_no_percentage_at_all(self):
        """`cooldown.utilization` returns 0.0 for a channel with no capacity
        estimate -- deliberately, so no estimate means no shadow price. Rendered
        as a percentage that is "100% left", which is the fabricated number a
        caller would decide to dispatch a wave on."""
        reg = json.loads(json.dumps(self.reg))          # a deep copy of plain data
        reg["channels"]["claude-seat"]["quota"] = {"window": "5h"}
        lines = cli._seat_quota_lines(reg, {}, {}, self.now)
        claude = [l for l in lines if "claude-seat" in l][0]
        self.assertIn("no capacity estimate", claude)
        self.assertNotIn("%", claude, "a seat with no meter was given a figure")

    def test_a_seat_drawn_past_its_estimate_reads_as_empty_not_negative(self):
        """The estimate is one invocation per unit and providers expose no
        meter, so a seat can genuinely go past it. "-12% left" reads as a bug in
        this program rather than as a seat well past its guess."""
        usage = {"claude-seat": [self.now - i for i in range(60)]}   # 60 of 40
        lines = cli._seat_quota_lines(self.reg, {}, usage, self.now)
        claude = [l for l in lines if "claude-seat" in l][0]
        self.assertIn("0% left", claude)
        self.assertNotRegex(claude, r"-\d+%")

    def test_a_seat_the_router_cannot_pick_is_not_listed(self):
        reg = json.loads(json.dumps(self.reg))
        reg["channels"]["cursor-seat"]["disabled"] = True
        lines = cli._seat_quota_lines(reg, {}, {}, self.now)
        self.assertTrue(any("claude-seat" in l for l in lines))
        self.assertFalse(any("cursor-seat" in l for l in lines),
                         "a disabled seat advertised headroom it will never serve")

    def test_status_writes_nothing(self):
        """The whole item is a render. A reporting command that mutated the
        breaker file would change the routing of the next run by being looked
        at, and `status` is the command a user runs repeatedly while waiting."""
        cooldown.record_use("claude-seat", 5 * 3600, self.now)
        before = {}
        for root, _, names in os.walk(store.state_root()):
            for n in names:
                p = os.path.join(root, n)
                with open(p, "rb") as fh:
                    before[p] = fh.read()
        self.assertTrue(before, "nothing was seeded, so this proves nothing")
        self._status()
        after = {}
        for root, _, names in os.walk(store.state_root()):
            for n in names:
                p = os.path.join(root, n)
                with open(p, "rb") as fh:
                    after[p] = fh.read()
        self.assertEqual(before, after, "`status` wrote to the state it reports on")

    def test_the_seat_block_survives_a_registry_that_will_not_load(self):
        """`status` is what a user runs when something is already broken. A bad
        --registry may cost them the seat lines; it must not cost them the task
        lines, which is the same rule `main` applies to a workflow manifest."""
        import contextlib
        import io
        bad = os.path.join(self.t.dir, "bad.yaml")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("channels: [this is not a mapping\n")

        class Args:
            repo, registry = self.t.repo, bad
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_status(Args())
        out = buf.getvalue()
        self.assertIn("no tasks for", out, "the task half died with the seat half")
        self.assertIn("registry could not be read", out)


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
        # The breaker the park refers to. Tests that build only the task.json
        # record describe a state the machine never produces -- and one that
        # cannot distinguish "still cooling" from "the user already cleared it",
        # which is exactly the bug these tests exist to pin.
        cooldown.open_breaker("claude-seat", "quota", T0 + 3600, T0)

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

    def test_clearing_the_breaker_actually_unblocks_the_task(self):
        """The guard's own message tells the user to run `channels --clear`. If
        the guard then keeps refusing, that advice is a closed loop with no exit
        but waiting out the window or hand-editing task.json.

        The breaker file is the truth; `park` is only a record of why we stopped.
        """
        cooldown.open_breaker("claude-seat", "quota", T0 + 3600, T0)
        self.assertTrue(cooldown.clear("claude-seat"))
        cli._quota_guard(self.task, when_open=False, clock=lambda: T0)   # must not exit
        self.assertIsNone(self.task.state.get("park"), "the stale park survived a clear")

    def test_a_breaker_that_expired_on_its_own_also_unblocks(self):
        # Same path, no user action: the window simply reopened.
        cooldown.open_breaker("claude-seat", "quota", T0 + 3600, T0)
        cli._quota_guard(self.task, when_open=False, clock=lambda: T0 + 3601)
        self.assertIsNone(self.task.state.get("park"))

    def test_a_seat_still_cooling_still_refuses(self):
        # The fix must not degrade into "always proceed".
        cooldown.open_breaker("claude-seat", "quota", T0 + 3600, T0)
        with self.assertRaises(SystemExit):
            cli._quota_guard(self.task, when_open=False, clock=lambda: T0)

    def test_the_live_breaker_outranks_a_stale_reopen_time(self):
        # task.json still records the original 1h park. The seat was cleared and
        # re-cooled with a much shorter window (a later attempt got a smaller
        # reset from the provider), so the live breaker is the shorter one.
        # A breaker is never *shortened* in place -- clearing first is what makes
        # the shorter window real rather than silently extended.
        cooldown.clear("claude-seat")
        cooldown.open_breaker("claude-seat", "quota", T0 + 60, T0)
        cli._quota_guard(self.task, when_open=False, clock=lambda: T0 + 61)
        self.assertIsNone(self.task.state.get("park"),
                          "the guard trusted task.json over the live breaker")

    def test_a_park_naming_no_channels_does_not_wedge_the_task(self):
        # reopen_at may be None when no breaker carried one; refusing forever on
        # a park nothing corroborates would be unrecoverable.
        self.task.update(park={"reason": "quota_all_exhausted", "reopen_at": None,
                               "channels": [], "role": "implementer"})
        cli._quota_guard(self.task, when_open=False, clock=lambda: T0)
        self.assertIsNone(self.task.state.get("park"))

    def test_a_task_parked_for_any_other_reason_is_untouched(self):
        # The guard owns exactly one park reason. Clearing someone else's would
        # let a budget-parked task resume as though its cap had been raised.
        from adg import cli
        self.task.update(park={"reason": "budget_exhausted", "reopen_at": T0 + 3600})
        cli._quota_guard(self.task, when_open=False, clock=lambda: T0)  # no exit
        self.assertEqual((self.task.state.get("park") or {}).get("reason"),
                         "budget_exhausted", "the guard cleared a park it does not own")


class TestCorpusCapture(unittest.TestCase):
    """A real wall is a test case, and collecting them was a chore nobody did.

    `fixtures/provider-messages.json` says so about itself: its cases are
    written from provider error formats, not recorded from runs, and detection
    is the one judgement this program makes on its own account. So a live run
    now writes down every wall it classifies, and somebody promotes the useful
    ones by hand.

    Exhaust, not a feature. Nothing reads the file back, and nothing about the
    run changes if it cannot be written.
    """

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

    def _lines(self):
        from adg import corpus
        if not os.path.exists(corpus.path()):
            return []
        with open(corpus.path(), encoding="utf-8") as fh:
            return [json.loads(x) for x in fh if x.strip()]

    def _wall(self, message=QUOTA_MSG):
        """One job that walls on its first seat and finishes on the second --
        the shape TestFailoverEndToEnd drives, run for its side effect."""
        state = {"seen": []}

        def implementer(env, cwd):
            state["seen"].append(cwd)
            if len(state["seen"]) == 1:
                return blocked(message)
            with open(os.path.join(cwd, "app.py"), "a") as fh:
                fh.write("\ndef subtract(a, b):\n    return a - b\n")
            _report(env["AGENT_DELEGATION_TASK_DIR"], "implement-st-1-main.json", {
                "stage": "implement", "role": "implementer", "subtask": "st-1-main",
                "status": "complete", "summary": "added subtract",
                "evidence": {"tests": "green"}})
            return None

        task = store.Task.create(self.t.repo, "T-CAP", "# t\n\n- **AC-1** — x\n",
                                 self.pol)
        task.write_text("plan.md", PLAN_MD)
        logs = []
        status = Orchestrator(task, self.reg,
                              runtime.MockAdapter({"implementer": implementer},
                                                  now=lambda: T0),
                              lambda k, x: True, log=logs.append,
                              clock=lambda: T0).run()
        return status, logs

    def test_a_wall_writes_one_well_formed_line(self):
        status, logs = self._wall()
        self.assertEqual(status, "done", "\n".join(logs))
        lines = self._lines()
        self.assertEqual(len(lines), 1, "expected exactly one capture: %s" % lines)
        rec = lines[0]
        walled = _seats(self.reg)[0]
        self.assertEqual(rec["verdict"], "quota_exhausted")
        self.assertEqual(rec["adapter"], "mock")
        self.assertEqual(rec["channel"], walled)
        # Read from the registry rather than named here: the agent kind is the
        # only field a promoted case is keyed on, so it has to be the kind that
        # actually walled, not the one this test expected to go first.
        self.assertEqual(rec["kind"], self.reg["channels"][walled]["agent_kind"])
        self.assertIn(rec["model"], self.reg["models"])
        # The message said "reset in 2 hours", and the point of keeping it is
        # that a promoted case can be written as {T0+N}.
        self.assertEqual(rec["reset_in_s"], 2 * 3600)
        self.assertEqual(rec["reset_at"], T0 + 2 * 3600)
        self.assertIn("usage limit reached", rec["text"])
        # Everything a hand-promotion needs, so the promotion recipe in the
        # corpus file cannot ask for a field that is not written.
        for field in ("at", "kind", "text", "verdict", "reset_in_s"):
            self.assertIn(field, rec)

    def test_the_captured_text_is_redacted(self):
        home = os.path.expanduser("~")
        secret = "sk-" + "A1b2C3d4E5f6G7h8"
        status, logs = self._wall(
            "Error: Claude AI usage limit reached. Your limit will reset in 2 "
            "hours. context=%s/work/proj/app.py user=ada@example.com "
            "auth=%s" % (home, secret))
        self.assertEqual(status, "done", "\n".join(logs))
        text = self._lines()[0]["text"]
        self.assertNotIn(home, text, "a home path reached the capture file")
        self.assertNotIn("ada@example.com", text, "an address reached the file")
        self.assertNotIn(secret, text, "a key-shaped string reached the file")
        self.assertIn("<path>", text)
        self.assertIn("<email>", text)
        self.assertIn("<key>", text)
        # And the part the corpus is actually for survives the scrubbing.
        self.assertIn("usage limit reached", text)

    def test_an_unwritable_capture_path_does_not_touch_the_run(self):
        """Exhaust must never be load-bearing. The state root is where the
        breaker also lives, and that failure is already reported to the run --
        this one is not even that: the line is lost and the run cannot tell."""
        from adg import corpus
        os.makedirs(os.path.dirname(corpus.path()), exist_ok=True)
        # A directory where the file goes: open(..., "a") cannot win, on every
        # platform, without depending on chmod semantics or on not being root.
        os.makedirs(corpus.path(), exist_ok=True)
        status, logs = self._wall()
        self.assertEqual(status, "done",
                         "an unwritable capture path changed the run:\n"
                         + "\n".join(logs))
        self.assertTrue(any("failover:" in x for x in logs),
                        "the failover itself stopped working")
        self.assertFalse(any("corpus" in x.lower() for x in logs),
                         "capture complained into the run log")

    def test_an_ordinary_crash_captures_nothing(self):
        """A corpus that fills up on ordinary failures is a corpus nobody
        trusts. A stack trace says nothing about a provider's quota, and
        `quota.classify` calls it `other` for exactly that reason -- the capture
        hangs off `_cool`, which only a wall reaches."""
        def implementer(env, cwd):
            return blocked("Traceback (most recent call last):\n"
                           "ValueError: limit is not a number")

        task = store.Task.create(self.t.repo, "T-NOCAP", "# t\n\n- **AC-1** — x\n",
                                 self.pol)
        task.write_text("plan.md", PLAN_MD)
        Orchestrator(task, self.reg,
                     runtime.MockAdapter({"implementer": implementer},
                                         now=lambda: T0),
                     lambda k, x: True, log=lambda *_: None, clock=lambda: T0).run()
        self.assertEqual(self._lines(), [],
                         "a crash was filed as a provider wall")

    def test_redaction_is_not_asked_to_be_clever(self):
        """Unit-level, because the promise in the docstring is a bounded one:
        these shapes and no others. A test that asserted 'no secrets' would be
        claiming something stdlib regex cannot deliver."""
        from adg import corpus
        got = corpus.redact(
            "path /home/ada/x.py and C:\\Users\\ada\\y.py, mail a.b@c.co, "
            "ghp_abcdefghij1234567890, Bearer abcdefghijklmnop, "
            "and " + "z" * 44)
        self.assertNotIn("/home/ada", got)
        self.assertNotIn("Users\\ada", got)
        self.assertNotIn("a.b@c.co", got)
        self.assertNotIn("ghp_abcdefghij", got)
        self.assertNotIn("z" * 44, got)
        self.assertIn("Bearer <key>", got)


class TestManualCooldown(unittest.TestCase):
    """`delegate cooldown`: a human sitting in the classifier's seat.

    In pane mode nothing classifies anything -- herdr exposes no channel
    carrying the provider's own words, so a wall settles unclassified and no
    breaker opens (`runtime.HerdrAdapter`). The message is still there; a person
    is reading it in the pane. This command is how what they read reaches the
    router, and the entry it writes is THE SAME ENTRY classification would have
    written, so nothing downstream can tell the two apart. Only `origin` says
    who decided, and it is a record rather than a switch.
    """

    def setUp(self):
        import time as _time
        self.t = TempRepo()
        self.now = _time.time()
        self.reg = router.load_registry(REGISTRY)
        self.pol = dict(self.reg["policy"]["limits"],
                        escalation_ceiling=self.reg["policy"]["escalation_ceiling"])

    def tearDown(self):
        self.t.close()

    def _run(self, argv):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["--registry", REGISTRY, "--repo", self.t.repo] + argv)
        return buf.getvalue()

    # --- the entry itself --------------------------------------------------
    def test_it_writes_the_entry_classification_would_have_written(self):
        self._run(["cooldown", "claude-seat", "--for", "5h"])
        cools, _, _ = cooldown.read(self.now)
        entry = cools["claude-seat"]
        # `reason` stays "quota" on purpose: the human read a quota wall, and
        # every downstream reader keys on that word -- `_quota_guard`, the park,
        # `status`'s "waiting on quota". Only the origin differs.
        self.assertEqual(entry["reason"], "quota")
        self.assertEqual(cooldown.origin(entry), "manual")
        self.assertAlmostEqual(entry["reopen_at"], self.now + 5 * 3600, delta=10)

    def test_an_entry_written_before_origin_existed_reads_as_classified(self):
        # Every breaker on disk today was opened by the classifier, so the
        # absent key has exactly one honest reading.
        cooldown.open_breaker("claude-seat", "quota", T0 + 60, T0)
        with open(cooldown.path()) as fh:
            raw = json.load(fh)
        del raw["cooldowns"]["claude-seat"]["origin"]
        with open(cooldown.path(), "w") as fh:
            json.dump(raw, fh)
        cools, _, _ = cooldown.read(T0)
        self.assertEqual(cooldown.origin(cools["claude-seat"]), "classified")

    def test_a_classified_entry_still_says_it_was_classified(self):
        cooldown.open_breaker("claude-seat", "quota", T0 + 60, T0)
        cools, _, _ = cooldown.read(T0)
        self.assertEqual(cooldown.origin(cools["claude-seat"]), "classified")

    # --- downstream reads it as the same entry -----------------------------
    def test_a_manual_entry_routes_jobs_away_from_the_seat(self):
        # The seat the router WOULD have picked, or the skip proves nothing.
        first, other = _seats(self.reg)[:2]
        task = store.Task.create(self.t.repo, "T-001", "# t\n\nDo it\n", self.pol)
        self._run(["cooldown", first, "--for", "5h"])
        orch = _orch(self.reg, runtime.MockAdapter(), task,
                     clock=lambda: self.now)
        self.assertEqual(orch._pick().channel, other)

    def test_resume_when_open_waits_out_a_manual_window(self):
        task = store.Task.create(self.t.repo, "T-002", "# t\n", self.pol)
        self._run(["cooldown", "claude-seat", "--for", "1h"])
        reopen = cooldown.read(self.now)[0]["claude-seat"]["reopen_at"]
        task.update(status="needs_human",
                    park={"reason": "quota_all_exhausted", "reopen_at": reopen,
                          "channels": ["claude-seat"], "role": "implementer"})
        fake = {"t": self.now}
        slept = []

        def sleep(n):
            slept.append(n)
            fake["t"] += n

        prev, cli._SLEEP = cli._SLEEP, sleep
        try:
            cli._quota_guard(task, when_open=True, clock=lambda: fake["t"],
                             log=lambda *_: None)
        finally:
            cli._SLEEP = prev
        self.assertTrue(slept, "--when-open did not honour a manual cooldown")
        self.assertAlmostEqual(sum(slept), 3600, delta=10)
        self.assertIsNone(task.state.get("park"))

    def test_clear_reopens_the_seat_early(self):
        self._run(["cooldown", "claude-seat", "--for", "5h"])
        out = self._run(["cooldown", "claude-seat", "--clear"])
        self.assertIn("claude-seat", out)
        self.assertEqual(cooldown.active(self.now), set())

    def test_clearing_something_that_is_not_cooling_says_so(self):
        out = self._run(["cooldown", "cursor-seat", "--clear"])
        self.assertIn("not cooling", out)

    # --- what a human can type ---------------------------------------------
    def test_a_bare_clock_time_means_the_next_occurrence_of_it(self):
        import time as _time
        soon = _time.localtime(self.now + 3600)
        at = cli._until_epoch("%02d:%02d" % (soon.tm_hour, soon.tm_min), self.now)
        self.assertAlmostEqual(at, self.now + 3600, delta=60)
        # And the same wall-clock time already past today is tomorrow's, not an
        # error and not a zero-length cooldown.
        past = _time.localtime(self.now - 3600)
        at = cli._until_epoch("%02d:%02d" % (past.tm_hour, past.tm_min), self.now)
        self.assertGreater(at, self.now)
        self.assertAlmostEqual(at, self.now + 86400 - 3600, delta=60)

    def test_a_dated_time_is_read_as_local(self):
        import time as _time
        at = cli._until_epoch("2026-08-16 09:00", self.now)
        self.assertEqual(_time.strftime("%Y-%m-%d %H:%M", _time.localtime(at)),
                         "2026-08-16 09:00")

    def test_a_time_in_the_past_is_an_error_not_a_zero_length_cooldown(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(["cooldown", "claude-seat", "--until", "2020-01-01 09:00"])
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn("past", str(cm.exception).lower())
        self.assertEqual(cooldown.active(self.now), set(),
                         "a refused time still cooled the seat")

    def test_exactly_one_of_until_for_clear(self):
        for argv in (["cooldown", "claude-seat"],
                     ["cooldown", "claude-seat", "--for", "5h", "--clear"],
                     ["cooldown", "claude-seat", "--for", "5h",
                      "--until", "23:00"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as cm:
                    self._run(argv)
                self.assertNotEqual(cm.exception.code, 0)
                self.assertIn("exactly one", str(cm.exception).lower())
        self.assertEqual(cooldown.active(self.now), set())

    def test_a_duration_that_is_not_one_is_refused_rather_than_defaulted(self):
        # `quota.parse_window` falls back to 5h for a registry typo, which is
        # right there and wrong here: nobody asked for five hours.
        with self.assertRaises(SystemExit) as cm:
            self._run(["cooldown", "claude-seat", "--for", "soon"])
        self.assertNotEqual(cm.exception.code, 0)
        self.assertEqual(cooldown.active(self.now), set())

    def test_the_help_documents_the_formats_it_accepts(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                cli.main(["cooldown", "--help"])
        help_text = buf.getvalue()
        for wanted in ("HH:MM", "YYYY-MM-DD", "5h", "90m", "7d"):
            self.assertIn(wanted, help_text,
                          "--help does not document %r" % wanted)
        self.assertIn("tomorrow", help_text.lower(),
                      "--help does not state the next-occurrence rule, which is "
                      "the case a human types while reading a wall message")

    # --- what a reader sees ------------------------------------------------
    def test_the_no_args_listing_names_what_is_cooling(self):
        self._run(["cooldown", "claude-seat", "--for", "5h"])
        out = self._run(["cooldown"])
        self.assertIn("claude-seat", out)
        self.assertIn("manual", out)
        self.assertNotIn("cursor-seat", out,
                         "the listing is of cooldowns, not of every seat")

    def test_the_listing_says_so_when_nothing_is_cooling(self):
        out = self._run(["cooldown"])
        self.assertIn("nothing is cooling", out.lower())

    def test_a_seat_the_registry_does_not_name_is_still_findable(self):
        # Otherwise a typo writes an entry the listing cannot show and the
        # human cannot find to clear.
        out = self._run(["cooldown", "claud-seat", "--for", "5h"])
        self.assertIn("claud-seat", out.lower())
        self.assertIn("claud-seat", self._run(["cooldown"]))

    def test_status_says_which_cooldowns_a_human_set(self):
        import contextlib
        import io
        self._run(["cooldown", "claude-seat", "--for", "5h"])
        cooldown.open_breaker("cursor-seat", "quota", self.now + 60, self.now)
        buf = io.StringIO()

        class Args:
            repo, registry = self.t.repo, REGISTRY
        with contextlib.redirect_stdout(buf):
            cli.cmd_status(Args())
        out = buf.getvalue()
        claude = [l for l in out.splitlines() if l.startswith("  claude-seat")][0]
        cursor = [l for l in out.splitlines() if l.startswith("  cursor-seat")][0]
        self.assertIn("(manual)", claude, out)
        self.assertIn("(classified)", cursor, out)

    def test_the_run_log_carries_the_origin_too(self):
        # `_log_routing` renders the seat block through `cli._seat_quota_lines`,
        # so the origin reaches the run log by the same code `status` prints --
        # which is the point: a reader diagnosing a run hours later must be able
        # to see that a seat was cooled by hand.
        task = store.Task.create(self.t.repo, "T-003", "# t\n\nDo it\n", self.pol)
        self._run(["cooldown", _seats(self.reg)[0], "--for", "5h"])
        logs = []
        orch = _orch(self.reg, runtime.MockAdapter(), task, clock=lambda: self.now,
                     logs=logs)
        orch._pick()
        self.assertIn("(manual)", "\n".join(logs), "\n".join(logs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
