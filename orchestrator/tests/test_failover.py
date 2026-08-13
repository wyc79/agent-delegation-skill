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

    def test_an_unwritable_state_dir_is_reported_not_raised(self):
        # A read-only $XDG_STATE_HOME with a writable task dir is an ordinary CI
        # sandbox. _meter runs on the first agent call, so raising here kills
        # the run before it starts.
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

    def test_herdr_error_codes_classify_without_prose(self):
        res = {"settled": "blocked", "output": "", "code": 1,
               "error_code": "rate_limited"}
        self.assertEqual(quota.classify("claude", res, T0)[0], "quota_exhausted")


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

    def test_a_cooling_seat_says_when_it_reopens(self):
        """The reopen time the classifier parsed out of the provider's own
        message. Without it the line says a seat is empty and not when to come
        back, which is the question the caller is actually asking."""
        reopen = self.now + 4200
        cooldown.open_breaker("claude-seat", "quota", reopen, self.now, "usage limit")
        out = self._status()
        self.assertIn("[cooling until %s]" % cli._stamp(reopen), out, out)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
