"""Tests for the MVP orchestrator.

The end-to-end test is the important one: it drives the real state machine
over a real git repository with a scripted adapter, so the pipeline is proven
without spending a token on a model.

Run: python3 orchestrator/tests/test_orchestrator.py
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adg import (brief, cli, cooldown, limits, prompts, router, runtime, schema,
                 store, verify, winnow, workflow as wf, yamlite)
from adg.machine import STAGES, Halt, Orchestrator

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REGISTRY = os.path.join(REPO_ROOT, "registry.default.yaml")


def sh(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class TempRepo:
    """A throwaway git repo with an isolated XDG state root."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="adg-test-")
        self.repo = os.path.join(self.dir, "proj")
        os.makedirs(self.repo)
        sh(["git", "init", "-q", "-b", "main"], self.repo)
        sh(["git", "config", "user.email", "t@example.com"], self.repo)
        sh(["git", "config", "user.name", "Test"], self.repo)
        with open(os.path.join(self.repo, "app.py"), "w") as fh:
            fh.write("def add(a, b):\n    return a + b\n")
        sh(["git", "add", "-A"], self.repo)
        sh(["git", "commit", "-qm", "init"], self.repo)
        self._prev = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = os.path.join(self.dir, "state")

    def close(self):
        if self._prev is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self._prev
        shutil.rmtree(self.dir, ignore_errors=True)


class TestYamlite(unittest.TestCase):
    def test_parses_the_real_registry(self):
        with open(REGISTRY) as fh:
            reg = yamlite.load(fh.read())
        self.assertEqual(reg["models"]["ultra-reasoner"]["enrolled_roles"], [])
        self.assertEqual(reg["policy"]["escalation_ceiling"]["max_tier"], "t2")
        self.assertIn("balanced-coder", reg["channels"]["cursor-seat"]["exposes"])

    def test_flow_collections_and_types(self):
        d = yamlite.load("a: {x: 1, y: [p, q]}\nb: true\nc: null\nd: 2.5\ne: 'x: y'\n")
        self.assertEqual(d["a"], {"x": 1, "y": ["p", "q"]})
        self.assertIs(d["b"], True)
        self.assertIsNone(d["c"])
        self.assertEqual(d["d"], 2.5)
        self.assertEqual(d["e"], "x: y")

    def test_list_of_maps_matches_plan_template(self):
        data = yamlite.load(
            "- id: st-1\n  goal: do a thing\n  file_scope: [\"src/**\"]\n"
            "  capability_hint: {coding: high}\n- id: st-2\n  goal: another\n")
        self.assertEqual([d["id"] for d in data], ["st-1", "st-2"])
        self.assertEqual(data[0]["file_scope"], ["src/**"])
        self.assertEqual(data[0]["capability_hint"], {"coding": "high"})

    def test_block_scalars_in_every_chomping_form(self):
        # A live planner emitted `test_notes: >-` and the whole plan failed to
        # parse. All block-scalar forms must work.
        for style in (">", ">-", ">+", "|", "|-", "|+"):
            d = yamlite.load("a: %s\n  one line\n  two line\nb: 2\n" % style)
            joiner = "\n" if style.startswith("|") else " "
            self.assertEqual(d["a"], "one line%stwo line" % joiner, style)
            self.assertEqual(d["b"], 2, style)

    def test_parses_a_realistic_planner_subtask_block(self):
        block = (
            '- id: st-1-subtract\n'
            '  goal: Add subtract(a, b) to calc.py\n'
            '  file_scope: ["calc.py", "test_calc.py"]\n'
            '  frozen_interfaces:\n'
            '    - "def subtract(a, b) -> a - b  # minuend first"\n'
            '  capability_hint: {reasoning: low, coding: low}\n'
            '  acceptance: [AC-1, AC-2]\n'
            '  test_notes: >-\n'
            '    Pure stdlib arithmetic. Verify with unittest discover;\n'
            '    pytest is not installed here.\n')
        data = yamlite.load(block)
        self.assertEqual(data[0]["id"], "st-1-subtract")
        self.assertEqual(data[0]["file_scope"], ["calc.py", "test_calc.py"])
        self.assertIn("pytest is not installed", data[0]["test_notes"])
        self.assertEqual(data[0]["acceptance"], ["AC-1", "AC-2"])

    def test_quoted_list_item_containing_a_colon(self):
        # A live planner wrote frozen_interfaces entries with prose containing
        # "calc.py; importable as ..." -- a naive colon split broke the quote.
        data = yamlite.load(
            '- id: st-1\n'
            '  frozen_interfaces:\n'
            '    - "def subtract(a, b) -> a - b  # module-level in calc.py; minuend first"\n'
            '  estimated_loc: 10\n')
        self.assertEqual(data[0]["id"], "st-1")
        self.assertIn("minuend first", data[0]["frozen_interfaces"][0])
        self.assertEqual(data[0]["estimated_loc"], 10)

    def test_colon_without_space_stays_in_the_scalar(self):
        d = yamlite.load("url: https://example.com/x\ntime: 10:30 sharp\n")
        self.assertEqual(d["url"], "https://example.com/x")
        self.assertEqual(d["time"], "10:30 sharp")

    def test_refuses_garbage_rather_than_guessing(self):
        with self.assertRaises(yamlite.YamlError):
            yamlite.load("just a bare line\n")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()

    def tearDown(self):
        self.t.close()

    def test_project_key_identical_from_every_worktree(self):
        wt = os.path.join(self.t.dir, "wt1")
        sh(["git", "worktree", "add", "-q", "-b", "side", wt], self.t.repo)
        self.assertEqual(store.project_key(self.t.repo), store.project_key(wt))

    def test_task_dir_is_outside_the_repository(self):
        task = store.Task.create(self.t.repo, "T-001", "# t\n", {"max_cost_usd": 1})
        self.assertFalse(task.path.startswith(os.path.realpath(self.t.repo)))
        out = subprocess.run(["git", "status", "--porcelain"], cwd=self.t.repo,
                             capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "", "task creation dirtied the working tree")

    def test_a_crash_midwrite_leaves_the_previous_state_readable(self):
        # The old version of this test simulated no crash and asserted a
        # property true of every directory. It would have passed with atomic
        # writes removed.
        task = store.Task.create(self.t.repo, "T-001", "# t\n", {"max_cost_usd": 1})
        before = task.update(status="planning")
        boom = RuntimeError("killed mid-write")

        def explode(state):
            state["status"] = "implementing"
            raise boom

        with self.assertRaises(RuntimeError):
            task.mutate(explode)
        reread = store.Task(task.path).state
        self.assertEqual(reread["status"], "planning", "a partial write was visible")
        # Not `len(json.dumps(reread)) > 0`, which is true of every dict ever
        # written: the property is that the whole previous state survived, not
        # that something did.
        self.assertEqual(reread, before, "the failed write cost part of the state")


class TestLimits(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.full = {"max_cost_usd": 10, "max_attempts_per_subtask": 3,
                     "max_review_loops": 2, "max_replans": 1, "max_parallel_agents": 2}

    def tearDown(self):
        self.t.close()

    def test_missing_limit_fails_closed(self):
        with self.assertRaises(limits.LimitsInvalid):
            limits.validate({"max_cost_usd": 5})

    def test_nonsense_limit_fails_closed(self):
        for bad in ({"max_cost_usd": "lots"}, {"max_cost_usd": 0}, {"max_cost_usd": True}):
            merged = dict(self.full, **bad)
            with self.assertRaises(limits.LimitsInvalid):
                limits.validate(merged)

    def test_task_may_lower_but_never_raise(self):
        merged, notes = limits.merge({"max_cost_usd": 10}, {"max_cost_usd": 3})
        self.assertEqual(merged["max_cost_usd"], 3)
        merged, notes = limits.merge({"max_cost_usd": 10}, {"max_cost_usd": 99})
        self.assertEqual(merged["max_cost_usd"], 10)
        self.assertTrue(notes, "raising a limit should be reported, not silent")

    def test_checks_fire_before_the_action(self):
        task = store.Task.create(self.t.repo, "T-001", "# t\n", self.full)
        b = limits.Budget(task)
        b.spend(9.5)
        with self.assertRaises(limits.LimitBreach):
            b.check_cost(1.0)
        for _ in range(3):
            b.used_attempt("st-1")
        with self.assertRaises(limits.LimitBreach) as cm:
            b.check_attempt("st-1")
        self.assertEqual(cm.exception.action, "escalate")


class TestChannelPreference(unittest.TestCase):
    """`prefers:` decides which seat serves a model that two seats both expose.

    That decision used to be alphabetical. Two subscription seats with headroom
    both price at ~0, so the scores tied and `candidates` fell through to
    `sort(..., c.channel)` — "claude-seat" beats "cursor-seat" — and a
    deployment with two providers ran every role on one of them. The rule is
    now written down rather than emergent, and it stays a preference: it may
    break a tie and may not buy a weaker model a job.
    """

    def _reg(self, **chan_overrides):
        reg = {
            "models": {
                "strong": {"tier": "t2", "reasoning": 5, "coding": 5,
                           "adherence": 4, "tool": 5, "speed": 2, "ctx": 200000,
                           "cost_out": 75, "enrolled_roles": ["implementer"]},
                "worker": {"tier": "t2", "reasoning": 4, "coding": 5,
                           "adherence": 4, "tool": 5, "speed": 4, "ctx": 200000,
                           "cost_out": 15, "enrolled_roles": ["implementer"]},
            },
            "profiles": {"implementer": {"require": {"coding": 4, "tool": 4},
                                         "weights": {"coding": 3, "adherence": 2,
                                                     "speed": 1},
                                         "cost_sensitivity": "high"}},
            "channels": {
                "aaa-seat": {"type": "subscription", "adapter": "mock",
                             "agent_kind": "claude", "exposes": ["worker"]},
                "zzz-seat": {"type": "subscription", "adapter": "mock",
                             "agent_kind": "cursor", "exposes": ["worker"]},
            },
            "policy": {"escalation_ceiling": {"max_tier": "t2"}},
        }
        for name, over in chan_overrides.items():
            reg["channels"][name.replace("_", "-")].update(over)
        return reg

    def test_without_a_preference_the_tie_falls_to_the_alphabet(self):
        """The behaviour that made this necessary, pinned so the fix has
        something to be a fix *of*."""
        top = router.Router(self._reg()).candidates("implementer")[0]
        self.assertEqual(top.channel, "aaa-seat")

    def test_a_preference_decides_which_seat_serves_the_model(self):
        reg = self._reg(zzz_seat={"prefers": ["worker"]})
        cands = router.Router(reg).candidates("implementer")
        self.assertEqual(cands[0].channel, "zzz-seat",
                         "the declared preference lost to the alphabet")
        # The other seat is still there, and still second: a preference orders
        # the fallback chain, it does not remove anything from it.
        self.assertEqual([c.channel for c in cands], ["zzz-seat", "aaa-seat"])

    def test_a_preference_cannot_buy_a_weaker_model_the_job(self):
        """The bound that makes this safe. `PREFERENCE_BONUS` is below the
        smallest gap two different capability scores can have, so a preferred
        seat wins ties and only ties — here `worker` scores 27 and `strong` 25
        on the implementer profile, and preferring `strong` must not flip it."""
        reg = self._reg()
        reg["channels"]["aaa-seat"]["exposes"] = ["strong"]
        reg["channels"]["aaa-seat"]["prefers"] = ["strong"]
        top = router.Router(reg).candidates("implementer")[0]
        self.assertEqual(top.model, "worker")
        self.assertEqual(top.channel, "zzz-seat")

    def test_a_preferred_seat_that_is_cooled_still_yields(self):
        """A preference, not a pin. Moving work off a walled seat is the whole
        point of the project, and a routing preference must not undo it."""
        reg = self._reg(zzz_seat={"prefers": ["worker"]})
        cands = router.Router(reg).candidates("implementer", cooldowns={"zzz-seat"})
        self.assertEqual([c.channel for c in cands], ["aaa-seat"])

    def test_a_preferred_seat_that_is_drawn_down_still_yields(self):
        """Utilisation is a real signal and outranks a stated preference: the
        bonus is a tie-break, so once the shadow price separates the two seats
        the cheaper one wins regardless of what the manifest would rather."""
        reg = self._reg(zzz_seat={"prefers": ["worker"]})
        cands = router.Router(reg).candidates(
            "implementer", utilization={"zzz-seat": 0.95, "aaa-seat": 0.0})
        self.assertEqual(cands[0].channel, "aaa-seat",
                         "a preference held a seat that had priced itself out")

    def test_preferring_a_model_the_seat_cannot_serve_is_refused(self):
        """A line that reads like a policy and routes nothing is the shape of
        mistake this registry refuses elsewhere. Caught at load, so `--registry`
        fails with the path rather than three stages into a run."""
        d = tempfile.mkdtemp(prefix="reg-")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "r.yaml")
        with open(p, "w") as fh:
            fh.write("""models:
  worker: {tier: t2, coding: 5, tool: 5, enrolled_roles: [implementer]}
profiles:
  implementer: {require: {}, weights: {coding: 3}}
channels:
  a-seat:
    type: subscription
    exposes: [worker]
    prefers: [nonesuch]
policy: {escalation_ceiling: {max_tier: t2}}
""")
        with self.assertRaises(router.RoutingError) as cm:
            router.load_registry(p)
        self.assertIn("nonesuch", str(cm.exception))
        self.assertIn("does not expose", str(cm.exception))

    def test_the_shipped_registry_splits_the_work_across_both_seats(self):
        """The deployment claim, checked against the file that makes it: the
        strong seat plans and reviews, the other implements. With both seats
        exposing `balanced-coder` and nothing to choose between them, this ran
        entirely on one provider — testing none of the routing this exists for."""
        r = router.Router(router.load_registry(REGISTRY))
        by_role = {role: r.candidates(role)[0]
                   for role in ("planner", "reviewer", "integrator",
                                "implementer", "test-author")}
        self.assertEqual(by_role["planner"].channel, "claude-seat")
        self.assertEqual(by_role["reviewer"].channel, "claude-seat")
        self.assertEqual(by_role["implementer"].channel, "cursor-seat")
        self.assertEqual(by_role["test-author"].channel, "cursor-seat")
        self.assertGreater(len({c.channel for c in by_role.values()}), 1,
                           "every role resolved to one seat, which is the "
                           "indirection SKILL.md tells the caller not to pay for")

    def test_the_strong_seat_keeps_planning_when_a_rival_seat_appears(self):
        """`claude-seat: prefers: [opus-class-strong]` decides nothing today —
        no other shipped seat exposes that model. It is not decoration: the
        moment a deployment enrols a second strong seat the tie returns, and
        without the line it would fall to the alphabet again, which is the
        failure the whole mechanism replaced. Pinned by adding that seat."""
        reg = router.load_registry(REGISTRY)
        reg["channels"]["aaa-strong-seat"] = {
            "type": "subscription", "adapter": "herdr", "agent_kind": "codex",
            "exposes": ["opus-class-strong"],
            "quota": {"window": "5h", "est_capacity": 40},
        }
        top = router.Router(reg).candidates("planner")[0]
        self.assertEqual(top.channel, "claude-seat",
                         "a newly enrolled seat took planning purely by sorting "
                         "earlier than the seat the registry prefers")

    def test_escalation_still_crosses_to_the_strong_seat(self):
        """And the preference must not trap an implementer on the worker seat:
        rung 2 raises a reasoning floor, which only the strong model clears, so
        the climb is itself a cross-provider hop."""
        r = router.Router(router.load_registry(REGISTRY))
        first = r.candidates("implementer")[0]
        self.assertEqual(first.channel, "cursor-seat")
        climbed = r.escalate("implementer", first)
        self.assertIsNotNone(climbed, "rung 2 had nowhere to go")
        self.assertEqual(climbed.channel, "claude-seat")


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.reg = router.load_registry(REGISTRY)
        self.r = router.Router(self.reg)

    def test_ultra_tier_unreachable_by_default(self):
        for role in ("planner", "implementer", "reviewer"):
            for c in self.r.candidates(role):
                self.assertNotEqual(c.model, "ultra-reasoner")

    def test_enrollment_alone_does_not_unlock_ultra(self):
        self.reg["models"]["ultra-reasoner"]["enrolled_roles"] = ["planner"]
        self.reg["channels"]["claude-seat"]["exposes"].append("ultra-reasoner")
        picked = [c.model for c in self.r.candidates("planner")]
        self.assertNotIn("ultra-reasoner", picked, "ceiling must still block it")

    def test_both_switches_unlock_ultra(self):
        self.reg["models"]["ultra-reasoner"]["enrolled_roles"] = ["planner"]
        self.reg["channels"]["claude-seat"]["exposes"].append("ultra-reasoner")
        picked = [c.model for c in self.r.candidates("planner", ceiling={"max_tier": "t3"})]
        self.assertIn("ultra-reasoner", picked)

    def test_escalate_returns_none_at_the_ceiling(self):
        top = self.r.select("planner")
        self.assertIsNone(self.r.escalate("planner", top),
                          "must stop climbing instead of exceeding the ceiling")

    def test_no_model_error_names_the_fix(self):
        with self.assertRaises(router.NoModelAvailable) as cm:
            self.r.select("planner", ceiling={"max_tier": "t1"})
        self.assertIn("registry.default.yaml", str(cm.exception))

    def test_bad_ceiling_refuses_rather_than_defaulting_open(self):
        with self.assertRaises(router.RoutingError):
            self.r.candidates("planner", ceiling={"max_tier": "unlimited"})

    def test_the_strong_model_is_the_implementers_rung_not_its_default(self):
        """The registry enrols opus for implementer so rung 2 has somewhere to
        climb, and its note promises that does not make it the default. The
        promise rests on the speed weight, which a later tuning pass could
        erase without noticing what else it decided."""
        self.assertIn("implementer",
                      self.reg["models"]["opus-class-strong"]["enrolled_roles"])
        first = self.r.select("implementer")
        self.assertEqual(first.model, "balanced-coder",
                         "the escalation target won a first attempt")
        # And it is reachable, or enrolling it bought nothing.
        self.assertEqual(self.r.escalate("implementer", first).model,
                         "opus-class-strong")

    def test_the_implementer_rung_survives_a_drawn_down_seat(self):
        # The cheap model has a second seat and the strong one does not, so a
        # filling window widens the gap rather than closing it.
        for u in (0.5, 0.9):
            self.assertEqual(
                self.r.select("implementer", utilization={"claude-seat": u}).model,
                "balanced-coder", "at %.0f%% draw" % (u * 100))

    def test_a_boost_raises_a_floor_the_profile_never_declared(self):
        # The implementer profile requires coding and tool, not reasoning, and
        # rung 2 of the ladder is exactly a reasoning+1 re-route.
        # Applying the boost only to declared requirements made that rung a
        # no-op for the one role that climbs it most.
        self.assertNotIn("reasoning", self.reg["profiles"]["implementer"]["require"])
        picked = [c.model for c in
                  self.r.candidates("implementer", boost={"reasoning": 5})]
        self.assertNotIn("balanced-coder", picked,
                         "reasoning 4 cleared a boosted floor of 5")

    def test_the_implementer_ladder_climbs_once_a_stronger_model_is_enrolled(self):
        # The failure this fixes: an operator enrolls the strong model to make
        # escalation work, and is still told nothing stronger is enrolled.
        self.reg["models"]["opus-class-strong"]["enrolled_roles"].append("implementer")
        r = router.Router(self.reg)
        cur = r.select("implementer")
        self.assertEqual(cur.model, "balanced-coder")
        stronger = r.escalate("implementer", cur)
        self.assertIsNotNone(stronger, "rung 2 refused a legal, enrolled rung")
        self.assertEqual(stronger.model, "opus-class-strong")

    def test_an_unscored_capability_cannot_clear_a_boosted_floor(self):
        self.reg["models"]["balanced-coder"].pop("reasoning")
        r = router.Router(self.reg)
        picked = [c.model for c in r.candidates("implementer", boost={"reasoning": 3})]
        self.assertNotIn("balanced-coder", picked,
                         "an unscored dimension is unverifiable, not a pass")

    def test_boosting_a_declared_requirement_still_takes_the_higher_floor(self):
        # The original behaviour, which must survive: require 4, boost 5 -> 5.
        picked = [c.model for c in self.r.candidates("reviewer", boost={"reasoning": 5})]
        self.assertEqual(picked, ["opus-class-strong"])


class TestVerifyAndScope(unittest.TestCase):
    def test_scope_violations_are_case_insensitive_where_the_fs_is(self):
        v = verify.scope_violations(["src/Foo.cs"], ["src/foo.cs"], case_insensitive=True)
        self.assertEqual(v, [], "Foo.cs and foo.cs are one file on win/mac")
        v = verify.scope_violations(["src/Foo.cs"], ["src/foo.cs"], case_insensitive=False)
        self.assertEqual(v, ["src/Foo.cs"])

    def test_out_of_scope_file_is_reported(self):
        v = verify.scope_violations(["src/a.py", "other/b.py"], ["src/**"])
        self.assertEqual(v, ["other/b.py"])


class TestSchema(unittest.TestCase):
    def test_valid_report_passes(self):
        self.assertTrue(schema.validate_report({
            "stage": "implement", "role": "implementer", "status": "complete",
            "summary": "did the thing", "evidence": {"tests": "3 passed"}}))

    def test_missing_evidence_is_rejected(self):
        with self.assertRaises(schema.Invalid):
            schema.validate_report({"stage": "implement", "role": "implementer",
                                    "status": "complete", "summary": "x"})

    def test_verdict_enum_is_closed(self):
        with self.assertRaises(schema.Invalid):
            schema.validate_verdict({"verdict": "LGTM", "ac_table": [], "findings": []})


class TestBriefLint(unittest.TestCase):
    def test_bare_jargon_is_caught(self):
        problems = brief.lint("We satisfied AC-2 and st-3 escalated to rung 2.")
        self.assertGreaterEqual(len(problems), 3)

    def test_expanded_ids_are_allowed(self):
        clean = ("The requirement that old saves still load (AC-2) is met by the "
                 "save-migration step (st-3).")
        self.assertEqual(brief.lint(clean), [])


# ---------------------------------------------------------------------------
# End to end: plan -> isolated implementation -> verify -> review -> integrate
# ---------------------------------------------------------------------------

PLAN_MD = """# Plan

## Approach
Add a subtract function beside the existing add.

## Subtasks
```yaml
- id: st-1-subtract
  goal: Add a subtract function to app.py
  file_scope: ["app.py"]
  acceptance: [AC-1]
```
"""


def _slurp(path):
    """Read a file and close it. Bare `open(...).read()` in an assertion leaks a
    handle, and a suite that prints ResourceWarnings teaches its readers to skim
    warnings -- which is where the next real one goes to hide."""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _spit(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _report(cwd_task, name, payload):
    with open(os.path.join(cwd_task, "reports", name), "w") as fh:
        json.dump(payload, fh)


def _tree_name(cwd):
    """The worktree's own directory name, for scripted mocks deciding which
    subtask they were dispatched as. Matching a subtask id against the FULL
    path is a trap: mkdtemp's prefix "adg-test-" ends in "st-", so whenever
    the random suffix opens with a digit, "st-1" (or st-2, st-3) appears in
    *every* path of the run and every mock takes the same branch. That was
    the long-standing ~1-in-19 wave flake: a suffix drawn from 37 characters
    starts with "1" once in 37 runs, and two tests each rolled that die."""
    return os.path.basename(cwd.rstrip("/"))


class TestClassifier(unittest.TestCase):
    """The keyword classifier sent a 12-line change down the full planning
    pipeline. Classification is a judgement, so a model makes it."""

    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)
        self.limits = dict(self.reg["policy"]["limits"],
                           escalation_ceiling=self.reg["policy"]["escalation_ceiling"])

    def tearDown(self):
        self.t.close()

    def _classify(self, request, verdict="SIMPLE -- small self-contained change",
                  hotspots=None):
        if hotspots:
            with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
                fh.write("hotspots:\n" + "".join('  - "%s"\n' % h for h in hotspots))
        task = store.Task.create(self.t.repo, "T-001", "# t\n\n%s\n" % request, self.limits)
        seen = {}

        class A(runtime.MockAdapter):
            def prompt(self, session, text, timeout):
                seen["role"] = session.handle["role"]
                seen["prompt"] = text
                return {"settled": "idle", "output": "VERDICT: %s" % verdict, "code": 0}

        orch = Orchestrator(task, self.reg, A(), lambda k, t: True, log=lambda *_: None)
        orch._stage_classify()
        return task.state["classification"], seen

    def test_adding_a_function_is_simple(self):
        # The exact request that took 40 minutes on the keyword classifier.
        c, _ = self._classify("Add a subtract function to the calculator API, "
                              "with a unit test covering it.")
        self.assertEqual(c["tier"], "simple")

    def test_the_word_api_does_not_decide_anything(self):
        c, _ = self._classify("Add an API helper", verdict="SIMPLE -- one function")
        self.assertEqual(c["tier"], "simple", "a keyword must not outvote the judge")

    def test_model_can_say_complex(self):
        c, _ = self._classify("Migrate the save format",
                              verdict="COMPLEX -- changes a shared format")
        self.assertEqual(c["tier"], "complex")

    def test_classifier_sees_repo_facts_not_just_the_request(self):
        _, seen = self._classify("Add a subtract function")
        self.assertIn("tracked files", seen["prompt"])
        self.assertIn("app.py", seen["prompt"])

    def test_hotspot_overrides_the_model(self):
        # Not a judgement call: declared hotspots always get a plan.
        c, seen = self._classify("Change combat_system.gd damage",
                                 verdict="SIMPLE -- looks small",
                                 hotspots=["combat_system.gd"])
        self.assertEqual(c["tier"], "complex")
        self.assertEqual(c["by"], "policy")
        self.assertNotIn("prompt", seen, "no model call needed for a policy override")

    def test_unparseable_verdict_fails_safe(self):
        c, _ = self._classify("Add a thing", verdict="I think maybe it depends")
        self.assertEqual(c["tier"], "complex")
        self.assertEqual(c["by"], "fallback")


class TestChannelAvailability(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)

    def tearDown(self):
        self.t.close()

    def test_uninstalled_cli_is_skipped_not_crashed_on(self):
        task = store.Task.create(self.t.repo, "T-001", "# t\n",
                                 dict(self.reg["policy"]["limits"]))

        class OnlyCursor(runtime.MockAdapter):
            def can_run(self, kind):
                return kind == "cursor"

        orch = Orchestrator(task, self.reg, OnlyCursor(), lambda k, t: True,
                            log=lambda *_: None)
        self.assertEqual(orch._pick("implementer").agent_kind, "cursor")

        class NothingInstalled(runtime.MockAdapter):
            def can_run(self, kind):
                return False

        orch2 = Orchestrator(task, self.reg, NothingInstalled(), lambda k, t: True,
                             log=lambda *_: None)
        with self.assertRaises(router.NoModelAvailable) as cm:
            orch2._pick("implementer")
        self.assertIn("needs", str(cm.exception), "error should name the missing CLI")


class TestClosedGaps(unittest.TestCase):
    """Each of these covers a limit or role that was declared but inert."""

    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)
        self.pol = dict(self.reg["policy"]["limits"],
                        escalation_ceiling=self.reg["policy"]["escalation_ceiling"])

    def tearDown(self):
        self.t.close()

    # --- cost ------------------------------------------------------------
    def test_cli_cost_is_recorded_so_the_cap_can_bind(self):
        from adg import runtime as rt
        res = rt._result(json.dumps({"result": "done", "total_cost_usd": 0.42}), "", 0)
        self.assertEqual(res["cost_usd"], 0.42)
        self.assertEqual(res["output"], "done")

    def test_missing_cost_is_unknown_not_zero(self):
        from adg import runtime as rt
        self.assertIsNone(rt._result("plain text", "", 0)["cost_usd"])

    def test_tokens_and_elapsed_survive_both_cli_shapes(self):
        """Not every CLI reports money. Where one does not, `usd` is our own
        price table applied to these counts, so the counts have to be on the
        record too -- otherwise a run compares a measurement against an
        estimate with nothing saying which is which."""
        from adg import runtime as rt
        claude = rt._result(json.dumps(
            {"result": "ok", "total_cost_usd": 0.09, "duration_ms": 1789,
             "usage": {"input_tokens": 2, "output_tokens": 4,
                       "cache_read_input_tokens": 15953}}), "", 0)
        self.assertEqual(claude["cost_usd"], 0.09)
        self.assertEqual(claude["usage"], {"in": 15955, "out": 4})
        self.assertEqual(claude["elapsed_ms"], 1789)

        cursor = rt._result(json.dumps(
            {"result": "ok", "duration_ms": 14298,
             "usage": {"inputTokens": 12179, "outputTokens": 30,
                       "cacheReadTokens": 5248}}), "", 0)
        self.assertIsNone(cursor["cost_usd"], "cursor reports no cost today")
        self.assertEqual(cursor["usage"], {"in": 17427, "out": 30})
        self.assertEqual(cursor["elapsed_ms"], 14298)

    def test_the_delegation_record_keeps_tokens_and_time(self):
        task = store.Task.create(self.t.repo, "T-C5", "# t\n", self.pol)

        class Metered(runtime.MockAdapter):
            def prompt(self, session, text, timeout):
                r = runtime.MockAdapter.prompt(self, session, text, timeout)
                r["usage"], r["elapsed_ms"] = {"in": 100, "out": 20}, 4242
                if session.handle["role"] == "classifier":
                    r["output"] = "VERDICT: SIMPLE -- tiny"
                return r

        Orchestrator(task, self.reg, Metered({"implementer": lambda e, c: None}),
                     lambda k, t: True, log=lambda *_: None).run()
        history = task.state["delegation_history"]
        self.assertTrue(history, "nothing was delegated")
        self.assertEqual(history[0]["tokens"], {"in": 100, "out": 20})
        self.assertEqual(history[0]["elapsed_ms"], 4242)

    def test_spend_accumulates_and_then_parks_the_run(self):
        task = store.Task.create(self.t.repo, "T-C1", "# t\n", dict(self.pol, max_cost_usd=0.5))

        class Pricey(runtime.MockAdapter):
            def prompt(self, session, text, timeout):
                r = runtime.MockAdapter.prompt(self, session, text, timeout)
                r["cost_usd"] = 0.6
                if session.handle["role"] == "classifier":
                    r["output"] = "VERDICT: SIMPLE -- tiny"
                return r

        script = {"implementer": lambda env, cwd: None}
        status = Orchestrator(task, self.reg, Pricey(script), lambda k, t: True,
                              log=lambda *_: None).run()
        self.assertEqual(status, "needs_human", "an exhausted budget kept running")
        self.assertGreater(task.state["spent"]["usd"], 0.0, "cost was never recorded")

    # --- autonomous mode --------------------------------------------------
    def test_autonomous_mode_pushes_and_never_merges(self):
        src = os.path.join(self.t.dir, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", src], check=True)
        sh(["git", "remote", "add", "origin", src], self.t.repo)
        script, _ = TestEndToEnd._script(self)[0], None
        task = store.Task.create(self.t.repo, "T-C2", "# t\n\nAdd subtract\n",
                                 self.pol, mode="autonomous")
        orch = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                            lambda k, t: True, log=lambda *_: None)
        orch.run()
        branches = subprocess.run(["git", "branch", "-a"], cwd=src,
                                  capture_output=True, text=True).stdout
        self.assertIn("adg/T-C2/work", branches, "autonomous mode did not push")
        head = subprocess.run(["git", "log", "--oneline", "main"], cwd=self.t.repo,
                              capture_output=True, text=True).stdout
        self.assertEqual(len(head.strip().splitlines()), 1, "it merged to main")

    # --- test author ------------------------------------------------------
    def test_simple_tasks_do_not_pay_for_a_test_author(self):
        task = store.Task.create(self.t.repo, "T-C3", "# t\n\nAdd subtract\n", self.pol)

        class A(runtime.MockAdapter):
            def prompt(self, session, text, timeout):
                if session.handle["role"] == "classifier":
                    return {"settled": "idle", "output": "VERDICT: SIMPLE -- tiny", "code": 0}
                return runtime.MockAdapter.prompt(self, session, text, timeout)

        a = A({"implementer": lambda env, cwd: None})
        Orchestrator(task, self.reg, a, lambda k, t: True, log=lambda *_: None).run()
        self.assertNotIn("test-author", [h["role"] for h in task.state["delegation_history"]])

    def test_test_author_is_told_not_to_read_the_implementation(self):
        seen = {}

        class A(runtime.MockAdapter):
            def prompt(self, session, text, timeout):
                if session.handle["role"] == "test-author":
                    seen["prompt"] = text
                return runtime.MockAdapter.prompt(self, session, text, timeout)

        script, _ = self._e2e_script()
        task = store.Task.create(self.t.repo, "T-C4", "# t\n\nAdd subtract API\n", self.pol)
        Orchestrator(task, self.reg, A(script), lambda k, t: True, log=lambda *_: None).run()
        self.assertIn("do not read any implementation", seen.get("prompt", "").lower())

    def _e2e_script(self):
        return TestEndToEnd._script(self)

    # --- reporter ---------------------------------------------------------
    def test_polished_brief_replaces_the_template(self):
        task = store.Task.create(self.t.repo, "T-C5", "# t\n", self.pol)
        orch = Orchestrator(task, self.reg, runtime.MockAdapter(), lambda k, t: True,
                            log=lambda *_: None)
        orch._polish = lambda t: "# Plain\n\n" + ("A readable sentence. " * 8)
        text, problems = brief.write(task, "merge", "Land it?", polish=orch._polish)
        self.assertIn("A readable sentence", text)
        self.assertEqual(problems, [])

    def test_deviations_written_from_the_shipped_template_reach_the_brief(self):
        """The gate brief must say when the plan was departed from.

        The section was guarded on the whole file not starting with `#`, and
        `templates/deviations.md` -- the file agents are told to start from --
        opens with `# Deviations — Task <id>`. Every task that used the template
        therefore hid every deviation from every brief, which is the one line in
        it a human reads to decide whether to look closer.
        """
        task = store.Task.create(self.t.repo, "T-C7", "# t\n", self.pol)
        with open(os.path.join(wf.default_dir(), "templates", "deviations.md")) as fh:
            template = fh.read()
        self.assertTrue(template.lstrip().startswith("#"),
                        "the template no longer opens with a heading")
        task.write_text("deviations.md", template)
        self.assertNotIn("What didn't go to plan", brief.render(task, "merge", "Land it?"),
                         "an untouched template claimed the plan was departed from")

        task.write_text("deviations.md", template + "\ndev-st-1-1 | implementer:st-1 | "
                        "plan said add to calc.py\n        | did instead: added a module\n"
                        "        | why: calc.py is generated\n        | severity: major\n")
        self.assertIn("What didn't go to plan", brief.render(task, "merge", "Land it?"),
                      "a logged deviation never reached the human")

    def test_jargon_or_empty_rewrite_falls_back_to_the_template(self):
        task = store.Task.create(self.t.repo, "T-C6", "# t\n", self.pol)

        class A(runtime.MockAdapter):
            def prompt(self, session, text, timeout):
                return {"settled": "idle", "output": "AC-2 failed at rung 2", "code": 0}

        orch = Orchestrator(task, self.reg, A(), lambda k, t: True, log=lambda *_: None)
        original = "# Brief\n\nsomething the human can act on, at length. " * 3
        self.assertEqual(orch._polish(original), original)


class TestParallelism(unittest.TestCase):
    """max_parallel_agents governed nothing until subtasks could actually run
    concurrently."""

    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)
        self.pol = dict(self.reg["policy"]["limits"],
                        escalation_ceiling=self.reg["policy"]["escalation_ceiling"])

    def tearDown(self):
        self.t.close()

    def _orch(self, subtasks, cap=3):
        task = store.Task.create(self.t.repo, "T-P", "# t\n", dict(self.pol,
                                                                   max_parallel_agents=cap))
        task.update(subtasks=subtasks)
        return Orchestrator(task, self.reg, runtime.MockAdapter(), lambda k, t: True,
                            log=lambda *_: None), task

    def test_disjoint_scopes_run_together(self):
        orch, _ = self._orch([
            {"id": "st-1", "status": "pending", "planned_scope": ["src/a/**"]},
            {"id": "st-2", "status": "pending", "planned_scope": ["src/b/**"]},
        ])
        self.assertEqual([t["id"] for t in orch._wave(orch.task.state["subtasks"])],
                         ["st-1", "st-2"])

    def test_overlapping_scopes_serialize(self):
        orch, _ = self._orch([
            {"id": "st-1", "status": "pending", "planned_scope": ["src/combat/**"]},
            {"id": "st-2", "status": "pending", "planned_scope": ["src/combat/hooks.py"]},
        ])
        self.assertEqual([t["id"] for t in orch._wave(orch.task.state["subtasks"])], ["st-1"])

    def test_shared_hotspot_serializes_even_with_disjoint_scopes(self):
        orch, _ = self._orch([
            {"id": "st-1", "status": "pending", "planned_scope": ["src/a.gd"],
             "hotspots": ["scenes/main.tscn"]},
            {"id": "st-2", "status": "pending", "planned_scope": ["src/b.gd"],
             "hotspots": ["scenes/main.tscn"]},
        ])
        self.assertEqual(len(orch._wave(orch.task.state["subtasks"])), 1,
                         "an unmergeable scene must never have two writers")

    def test_cap_is_respected(self):
        subs = [{"id": "st-%d" % i, "status": "pending", "planned_scope": ["src/%d/**" % i]}
                for i in range(5)]
        orch, _ = self._orch(subs, cap=2)
        self.assertEqual(len(orch._wave(orch.task.state["subtasks"])), 2)

    def test_dependencies_are_respected(self):
        orch, _ = self._orch([
            {"id": "st-1", "status": "pending", "planned_scope": ["a/**"]},
            {"id": "st-2", "status": "pending", "planned_scope": ["b/**"],
             "depends_on": ["st-1"]},
        ])
        self.assertEqual([t["id"] for t in orch._wave(orch.task.state["subtasks"])], ["st-1"])

    def test_unscoped_subtasks_never_run_together(self):
        # A missing scope means "unknown", and unknown must not mean "safe".
        orch, _ = self._orch([
            {"id": "st-1", "status": "pending"},
            {"id": "st-2", "status": "pending"},
        ])
        self.assertEqual(len(orch._wave(orch.task.state["subtasks"])), 1)

    def test_a_plan_whose_dependencies_can_never_be_met_parks_and_says_why(self):
        """The planner writes `depends_on`, so a dependency on an id it never
        defined -- or a cycle -- is an ordinary bad plan, not a bug in here.

        Nothing is ready, so the wave is empty, and indexing it crashed the run:
        the user got `IndexError: list index out of range` in crash.log for a
        one-line typo in plan.md, with nothing naming the subtask or the
        dependency.
        """
        orch, task = self._orch([
            {"id": "st-1", "status": "pending", "planned_scope": ["a/**"],
             "depends_on": ["st-typo"]},
            {"id": "st-2", "status": "pending", "planned_scope": ["b/**"],
             "depends_on": ["st-1"]},
        ])
        self.assertEqual(orch._wave(task.state["subtasks"]), [])
        with self.assertRaises(Halt) as cm:
            orch._stage_implement()
        self.assertEqual(cm.exception.status, "needs_human")
        said = str(cm.exception)
        self.assertIn("st-1 waits on st-typo (no such subtask)", said)
        self.assertIn("st-2 waits on st-1", said)
        self.assertIn("plan.md", said, "it does not say where to fix it")


class TestParallelEndToEnd(unittest.TestCase):
    """Real git, real worktrees, real merges — mock agents."""

    PLAN = """# Plan

## Subtasks
```yaml
- id: st-1-alpha
  goal: add alpha
  file_scope: ["alpha.py"]
  acceptance: [AC-1]
- id: st-2-beta
  goal: add beta
  file_scope: ["beta.py"]
  acceptance: [AC-2]
```
"""

    def setUp(self):
        self.t = TempRepo()
        with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
            fh.write('fast:\n  - "python3 -c \'print(1)\'"\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "cfg"], self.t.repo)
        self.reg = router.load_registry(REGISTRY)

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.t.repo, capture_output=True)
        self.t.close()

    def test_two_subtasks_get_two_worktrees_and_both_land(self):
        import threading
        seen_cwds, lock = set(), threading.Lock()

        def planner(env, cwd):
            with open(os.path.join(env["AGENT_DELEGATION_TASK_DIR"], "plan.md"), "w") as fh:
                fh.write(self.PLAN)

        def implementer(env, cwd):
            with lock:
                seen_cwds.add(cwd)
            # Each writes only its own file, so the merges must be clean.
            name = "alpha" if "st-1" in _tree_name(cwd) else "beta"
            with open(os.path.join(cwd, "%s.py" % name), "w") as fh:
                fh.write("VALUE = %r\n" % name)

        def reviewer(env, cwd):
            with open(os.path.join(env["AGENT_DELEGATION_TASK_DIR"],
                                   "reports", "review-reviewer.json"), "w") as fh:
                json.dump({"verdict": "APPROVE",
                           "ac_table": [{"ac": "AC-1", "status": "met"},
                                        {"ac": "AC-2", "status": "met"}],
                           "findings": []}, fh)

        script = {"planner": planner, "implementer": implementer,
                  "test-author": lambda e, c: None, "reviewer": reviewer}
        # A parallel test pins its own concurrency rather than inheriting the
        # registry's: when the registry shipped max_parallel_agents: 1, every
        # one of these silently became a sequential run that still passed, and
        # a future tuning-down must not do that again.
        pol = dict(self.reg["policy"]["limits"],
                   escalation_ceiling=self.reg["policy"]["escalation_ceiling"],
                   max_parallel_agents=2)
        task = store.Task.create(self.t.repo, "T-PAR", "# t\n\nAdd alpha and beta\n", pol)
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=logs.append).run()

        self.assertEqual(status, "done", "\n".join(logs))
        self.assertEqual(len(seen_cwds), 2, "subtasks shared a worktree: %s" % seen_cwds)
        self.assertIn("in parallel", "\n".join(logs))
        # both branches merged into the integration branch
        merged = subprocess.run(["git", "log", "--oneline", "adg/T-PAR/work"],
                                cwd=self.t.repo, capture_output=True, text=True).stdout
        self.assertIn("st-1-alpha", merged)
        self.assertIn("st-2-beta", merged)
        # and the delivered patch carries both files
        patch = task.read_text("integrate.patch")
        self.assertIn("alpha.py", patch)
        self.assertIn("beta.py", patch)

    def test_a_failing_subtask_does_not_silently_land_its_sibling(self):
        def planner(env, cwd):
            with open(os.path.join(env["AGENT_DELEGATION_TASK_DIR"], "plan.md"), "w") as fh:
                fh.write(self.PLAN)

        def implementer(env, cwd):
            if "st-2" in _tree_name(cwd):
                raise RuntimeError("beta agent exploded")
            with open(os.path.join(cwd, "alpha.py"), "w") as fh:
                fh.write("VALUE = 1\n")

        script = {"planner": planner, "implementer": implementer,
                  "test-author": lambda e, c: None}
        # A parallel test pins its own concurrency rather than inheriting the
        # registry's: when the registry shipped max_parallel_agents: 1, this
        # silently became a sequential run that still passed, and a future
        # tuning-down must not do that again.
        pol = dict(self.reg["policy"]["limits"],
                   escalation_ceiling=self.reg["policy"]["escalation_ceiling"],
                   max_parallel_agents=2)
        task = store.Task.create(self.t.repo, "T-PF", "# t\n\nAdd alpha and beta\n", pol)
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=lambda *_: None).run()
        self.assertEqual(status, "needs_human")
        self.assertFalse(os.path.exists(task.file("integrate.patch")))


class TestSubtaskContinuity(unittest.TestCase):
    """What one subtask leaves behind, and what the next one is handed.

    Three defects of one shape: something a subtask produced -- its commits, or
    the findings written against it -- reached nobody, and the run reported
    success regardless. None of them is a race; each reproduces on the first
    run, every run.
    """

    INDEPENDENT = """# Plan

## Subtasks
```yaml
- id: st-1-alpha
  goal: add alpha
  file_scope: ["alpha.py"]
  acceptance: [AC-1]
- id: st-2-beta
  goal: add beta
  file_scope: ["beta.py"]
  acceptance: [AC-2]
```
"""

    # Three subtasks whose DECLARED scopes are disjoint, so they form one wave.
    # What they actually write is up to the scripted agent.
    CONFLICTING = """# Plan

## Subtasks
```yaml
- id: st-1-alpha
  goal: add alpha
  file_scope: ["alpha.py"]
  acceptance: [AC-1]
- id: st-2-beta
  goal: add beta
  file_scope: ["beta.py"]
  acceptance: [AC-2]
- id: st-3-gamma
  goal: add gamma
  file_scope: ["gamma.py"]
  acceptance: [AC-3]
```
"""

    # st-1 lands first and alone; the other two are a second wave that depends
    # on it. This is the shape `depends_on` exists for.
    CHAINED = """# Plan

## Subtasks
```yaml
- id: st-1-lib
  goal: add the shared library
  file_scope: ["lib.py"]
  acceptance: [AC-1]
- id: st-2-beta
  goal: build on the library
  file_scope: ["beta.py"]
  depends_on: ["st-1-lib"]
  acceptance: [AC-2]
- id: st-3-gamma
  goal: also build on the library
  file_scope: ["gamma.py"]
  depends_on: ["st-1-lib"]
  acceptance: [AC-3]
```
"""

    def setUp(self):
        self.t = TempRepo()
        with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
            fh.write('fast:\n  - "python3 -c \'print(1)\'"\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "cfg"], self.t.repo)
        self.reg = router.load_registry(REGISTRY)

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.t.repo, capture_output=True)
        self.t.close()

    def _pol(self, cap):
        return dict(self.reg["policy"]["limits"],
                    escalation_ceiling=self.reg["policy"]["escalation_ceiling"],
                    max_parallel_agents=cap)

    def _planner(self, plan):
        def planner(env, cwd):
            with open(os.path.join(env["AGENT_DELEGATION_TASK_DIR"], "plan.md"), "w") as fh:
                fh.write(plan)
        return planner

    @staticmethod
    def _approve(env, cwd):
        _report(env["AGENT_DELEGATION_TASK_DIR"], "review-reviewer.json", {
            "stage": "review", "role": "reviewer", "status": "complete",
            "summary": "ok", "evidence": {"tests": "ok"},
            "role_data": {"verdict": {"verdict": "APPROVE",
                                      "ac_table": [{"ac": "AC-1", "status": "met"}],
                                      "findings": []}}})

    def _log(self, repo_branch):
        return subprocess.run(["git", "log", "--oneline", repo_branch],
                              cwd=self.t.repo, capture_output=True, text=True).stdout

    def test_a_completed_sibling_lands_even_when_its_wave_fails(self):
        """`_integrate_wave` ran only after the whole wave came back clean, so
        one failing sibling stranded every green one: `_finish_subtask` had
        already marked them complete, a complete subtask never rejoins a wave,
        and nothing else merges a subtask branch. The run resumed, finished the
        survivor, reached `done`, and delivered a patch missing work that
        `actual_files` and the merge brief both listed as changed."""
        state = {"beta_exploded": False}

        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            if "st-1-alpha" in _tree_name(cwd):
                with open(os.path.join(cwd, "alpha.py"), "w") as fh:
                    fh.write("ALPHA = 1\n")
                _report(td, "implement-st-1-alpha.json", {
                    "stage": "implement", "role": "implementer",
                    "subtask": "st-1-alpha", "status": "complete",
                    "summary": "did alpha", "evidence": {"tests": "ok"}})
                return
            if not state["beta_exploded"]:
                state["beta_exploded"] = True
                raise RuntimeError("beta agent exploded")
            with open(os.path.join(cwd, "beta.py"), "w") as fh:
                fh.write("BETA = 1\n")
            _report(td, "implement-st-2-beta.json", {
                "stage": "implement", "role": "implementer",
                "subtask": "st-2-beta", "status": "complete",
                "summary": "did beta", "evidence": {"tests": "ok"}})

        script = {"planner": self._planner(self.INDEPENDENT),
                  "implementer": implementer, "test-author": lambda e, c: None,
                  "reviewer": self._approve}
        task = store.Task.create(self.t.repo, "T-SC1", "# t\n\nAdd two things\n",
                                 self._pol(2))
        logs = []
        first = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                             lambda k, t: True, log=logs.append).run()
        self.assertEqual(first, "needs_human", "\n".join(logs))

        by_id = {s["id"]: s for s in task.state["subtasks"]}
        self.assertEqual(by_id["st-1-alpha"]["status"], "complete")
        # The invariant, asserted where it is cheapest to see: a subtask marked
        # complete has its work on the integration branch. Without it the file
        # list in the brief and the patch are two different accounts.
        self.assertIn("st-1-alpha", self._log("adg/T-SC1/work"),
                      "the green sibling's branch was never merged:\n"
                      + "\n".join(logs))

        # Exactly what `delegate resume` does from needs_human (cli.cmd_resume).
        task.update(status="implement")
        second = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=logs.append).run()
        self.assertEqual(second, "done", "\n".join(logs))
        patch = task.read_text("integrate.patch")
        self.assertIn("beta.py", patch)
        self.assertIn("alpha.py", patch,
                      "the run reported success and delivered a patch without "
                      "the sibling's completed work")

    def test_a_later_wave_is_cut_from_what_the_earlier_one_landed(self):
        """Subtask worktrees were cut from the task's *base* commit, so a wave
        saw the repository as it was before the task started.
        `references/parallelism.md` promises they are cut from the integration
        branch and `roles/implementer.md` tells the agent `depends_on` "tells
        you what already exists" -- neither held, so a subtask that depended on
        one already finished began by importing a file that was not there."""
        seen = {}

        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            name = _tree_name(cwd)
            # A wave of one runs in the integration worktree ("<task>-work"),
            # not in a worktree named for the subtask -- and st-1-lib is the
            # only subtask this plan dispatches alone, the other two depending
            # on it. Reading the tree name without allowing for that makes the
            # first subtask answer to a sibling's name and write nothing.
            if "st-1-lib" in name or name.endswith("-work"):
                with open(os.path.join(cwd, "lib.py"), "w") as fh:
                    fh.write("VALUE = 42\n")
                _report(td, "implement-st-1-lib.json", {
                    "stage": "implement", "role": "implementer",
                    "subtask": "st-1-lib", "status": "complete",
                    "summary": "lib", "evidence": {"tests": "ok"}})
                return
            sub = "st-2-beta" if "st-2-beta" in name else "st-3-gamma"
            seen[sub] = os.path.isfile(os.path.join(cwd, "lib.py"))
            with open(os.path.join(cwd, sub.split("-")[-1] + ".py"), "w") as fh:
                fh.write("from lib import VALUE\n")
            _report(td, "implement-%s.json" % sub, {
                "stage": "implement", "role": "implementer", "subtask": sub,
                "status": "complete", "summary": "used lib",
                "evidence": {"tests": "ok"}})

        script = {"planner": self._planner(self.CHAINED),
                  "implementer": implementer, "test-author": lambda e, c: None,
                  "reviewer": self._approve}
        task = store.Task.create(self.t.repo, "T-SC2", "# t\n\nAdd three things\n",
                                 self._pol(3))
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=logs.append).run()
        self.assertEqual(status, "done", "\n".join(logs))
        self.assertEqual(seen, {"st-2-beta": True, "st-3-gamma": True},
                         "a dependant could not see its dependency's output")
        # Per subtask, not the union: cutting from the integration branch moves
        # what a diff is measured against, and measuring against the task base
        # would credit every dependant with the file st-1 wrote and then call
        # it a scope violation it never committed.
        by_id = {s["id"]: s for s in task.state["subtasks"]}
        self.assertEqual(by_id["st-2-beta"]["actual_files"], ["beta.py"])
        self.assertEqual(by_id["st-2-beta"]["scope_violations"], [])
        self.assertEqual(by_id["st-3-gamma"]["actual_files"], ["gamma.py"])

    def test_a_wave_implementer_can_see_the_authored_tests(self):
        """The same defect where it is most expensive: the Test Author writes
        failing tests into the integration worktree during `plan`, and a wave
        cut from the task base never contained them -- so the checks an
        implementer ran were green because the requirement was absent, which is
        exactly the evidence `_author_tests` exists to create."""
        seen = {}

        def test_author(env, cwd):
            with open(os.path.join(cwd, "test_requirements.py"), "w") as fh:
                fh.write("def test_alpha():\n    import alpha\n")

        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            sub = "st-1-alpha" if "st-1-alpha" in _tree_name(cwd) else "st-2-beta"
            seen[sub] = os.path.isfile(os.path.join(cwd, "test_requirements.py"))
            with open(os.path.join(cwd, sub.split("-")[-1] + ".py"), "w") as fh:
                fh.write("V = 1\n")
            _report(td, "implement-%s.json" % sub, {
                "stage": "implement", "role": "implementer", "subtask": sub,
                "status": "complete", "summary": "done",
                "evidence": {"tests": "ok"}})

        script = {"planner": self._planner(self.INDEPENDENT),
                  "implementer": implementer, "test-author": test_author,
                  "reviewer": self._approve}
        task = store.Task.create(self.t.repo, "T-SC3", "# t\n\nAdd two things\n",
                                 self._pol(2))
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=logs.append).run()
        self.assertEqual(status, "done", "\n".join(logs))
        self.assertEqual(seen, {"st-1-alpha": True, "st-2-beta": True},
                         "the authored tests were invisible to the implementers")

    def test_the_second_sequential_subtask_is_credited_only_with_its_own_files(self):
        """A subtask working in the integration worktree measured its diff
        against the *task* base, which still holds the Test Author's commit and
        every earlier subtask's — so the second one in a sequential run was
        credited with its predecessors' files and reported them as scope
        violations it never committed. Waves of one are not exotic: any
        dependency chain or overlapping scope produces them at any cap."""
        def test_author(env, cwd):
            with open(os.path.join(cwd, "test_requirements.py"), "w") as fh:
                fh.write("def test_it():\n    pass\n")

        def implementer(env, cwd):
            # Cap 1, so both run in the same integration worktree: which subtask
            # this is comes from the prompt, not the directory name.
            sub = "st-1-alpha" if not os.path.exists(os.path.join(cwd, "alpha.py")) \
                else "st-2-beta"
            with open(os.path.join(cwd, sub.split("-")[-1] + ".py"), "w") as fh:
                fh.write("V = 1\n")
            _report(env["AGENT_DELEGATION_TASK_DIR"], "implement-%s.json" % sub, {
                "stage": "implement", "role": "implementer", "subtask": sub,
                "status": "complete", "summary": "did %s" % sub,
                "evidence": {"tests": "ok"}})

        script = {"planner": self._planner(self.INDEPENDENT),
                  "implementer": implementer, "test-author": test_author,
                  "reviewer": self._approve}
        task = store.Task.create(self.t.repo, "T-SC5", "# t\n\nAdd two things\n",
                                 self._pol(1))
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=logs.append).run()
        self.assertEqual(status, "done", "\n".join(logs))
        by_id = {s["id"]: s for s in task.state["subtasks"]}
        self.assertEqual(by_id["st-1-alpha"]["actual_files"], ["alpha.py"])
        self.assertEqual(by_id["st-2-beta"]["actual_files"], ["beta.py"],
                         "it was credited with its predecessor's work")
        self.assertEqual(by_id["st-2-beta"]["scope_violations"], [],
                         "phantom scope violations from a cumulative diff base")

    def test_an_idle_agent_is_still_caught_after_a_sibling_has_landed(self):
        """The same cumulative base disarmed the "changed no files" guard: a
        predecessor's work always looked like this subtask's, so an agent that
        wrote nothing and reported `complete` was believed. The existing guard
        test uses a one-subtask plan, which is the only shape where it fired."""
        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            if not os.path.exists(os.path.join(cwd, "alpha.py")):
                with open(os.path.join(cwd, "alpha.py"), "w") as fh:
                    fh.write("V = 1\n")
                _report(td, "implement-st-1-alpha.json", {
                    "stage": "implement", "role": "implementer",
                    "subtask": "st-1-alpha", "status": "complete",
                    "summary": "did alpha", "evidence": {"tests": "ok"}})
                return
            # st-2-beta writes nothing at all, and says it finished.
            _report(td, "implement-st-2-beta.json", {
                "stage": "implement", "role": "implementer",
                "subtask": "st-2-beta", "status": "complete",
                "summary": "nothing to do", "evidence": {"tests": "ok"}})

        script = {"planner": self._planner(self.INDEPENDENT),
                  "implementer": implementer, "test-author": lambda e, c: None,
                  "reviewer": self._approve}
        task = store.Task.create(self.t.repo, "T-SC6", "# t\n\nAdd two things\n",
                                 self._pol(1))
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=logs.append).run()
        self.assertEqual(status, "needs_human",
                         "an agent that wrote nothing was delivered as done\n"
                         + "\n".join(logs))
        self.assertIn("changed no files", "\n".join(logs))
        self.assertFalse(os.path.exists(task.file("integrate.patch")))

    def test_a_rework_sees_what_its_siblings_landed(self):
        """A subtask's own checkout is a snapshot of the moment it was cut, so a
        rework after `REQUEST_CHANGES` ran against a tree missing everything its
        siblings merged in the meantime — the checks were evidence about an
        incomplete tree, and a finding citing a sibling's file pointed at a file
        the agent could not see. It stays in its own worktree, which is what
        preserves an interrupted subtask's salvage checkpoints; `_catch_up`
        brings that worktree up to the integration branch first."""
        seen = []
        rounds = {"n": 0}

        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            sub = "st-1-alpha" if "st-1-alpha" in _tree_name(cwd) else "st-2-beta"
            if rounds["n"] and sub == "st-1-alpha":
                # the rework round: can it see what st-2-beta landed?
                seen.append(os.path.isfile(os.path.join(cwd, "beta.py")))
            with open(os.path.join(cwd, sub.split("-")[-1] + ".py"), "w") as fh:
                fh.write("V = %d\n" % (rounds["n"] + 1))
            _report(td, "implement-%s.json" % sub, {
                "stage": "implement", "role": "implementer", "subtask": sub,
                "status": "complete",
                "summary": "round %s" % ("one" if not rounds["n"] else "two"),
                "evidence": {"tests": "ok"}})

        def reviewer(env, cwd):
            rounds["n"] += 1
            first = rounds["n"] == 1
            _report(env["AGENT_DELEGATION_TASK_DIR"], "review-reviewer.json", {
                "stage": "review", "role": "reviewer", "status": "complete",
                "summary": "ruled", "evidence": {"tests": "ok"},
                "role_data": {"verdict": {
                    "verdict": "REQUEST_CHANGES" if first else "APPROVE",
                    "ac_table": [{"ac": "AC-1",
                                  "status": "unmet" if first else "met"}],
                    "findings": ([{"id": "f-1", "severity": "blocking",
                                   "claim": "alpha must agree with beta.py",
                                   "cite": "AC-1",
                                   "suggested_owner": "st-1-alpha"}]
                                 if first else [])}}})

        script = {"planner": self._planner(self.INDEPENDENT),
                  "implementer": implementer, "test-author": lambda e, c: None,
                  "reviewer": reviewer}
        task = store.Task.create(self.t.repo, "T-SC7", "# t\n\nAdd two things\n",
                                 self._pol(2))
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=logs.append).run()
        self.assertEqual(status, "done", "\n".join(logs))
        self.assertEqual(seen, [True],
                         "the rework ran against a tree without the sibling's "
                         "landed work: %s\n%s" % (seen, "\n".join(logs)))
        # And it is still credited with its own file only, not the sibling's it
        # can now see: the diff base follows the tree on every dispatch.
        by_id = {s["id"]: s for s in task.state["subtasks"]}
        self.assertEqual(by_id["st-1-alpha"]["actual_files"], ["alpha.py"])
        self.assertEqual(by_id["st-1-alpha"]["scope_violations"], [])

    def test_a_replan_keeps_what_a_surviving_subtask_already_owns(self):
        """`_read_plan_subtasks` rebuilds every subtask from plan.md, so a
        replan that reissues an id dropped its `worktree` and `base_commit`
        while the checkout and branch stayed on disk. `create_worktree` then
        reused the checkout and ignored the base it was handed, and the subtask
        recorded TODAY's integration tip — a commit its own worktree had never
        seen — so its diff was credited with every file its siblings landed."""
        rounds = {"review": 0}

        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            sub = "st-1-alpha" if "st-1-alpha" in _tree_name(cwd) else "st-2-beta"
            with open(os.path.join(cwd, sub.split("-")[-1] + ".py"), "w") as fh:
                fh.write("V = %d\n" % (rounds["review"] + 1))
            _report(td, "implement-%s.json" % sub, {
                "stage": "implement", "role": "implementer", "subtask": sub,
                "status": "complete",
                "summary": "round %s" % ("one" if not rounds["review"] else "two"),
                "evidence": {"tests": "ok"}})

        def reviewer(env, cwd):
            rounds["review"] += 1
            first = rounds["review"] == 1
            _report(env["AGENT_DELEGATION_TASK_DIR"], "review-reviewer.json", {
                "stage": "review", "role": "reviewer", "status": "complete",
                "summary": "ruled", "evidence": {"tests": "ok"},
                "role_data": {"verdict": {
                    "verdict": "REPLAN" if first else "APPROVE",
                    "ac_table": [{"ac": "AC-1",
                                  "status": "unmet" if first else "met"}],
                    "findings": []}}})

        script = {"planner": self._planner(self.INDEPENDENT),
                  "implementer": implementer, "test-author": lambda e, c: None,
                  "reviewer": reviewer}
        task = store.Task.create(self.t.repo, "T-SC8", "# t\n\nAdd two things\n",
                                 self._pol(2))
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=logs.append).run()
        self.assertEqual(status, "done", "\n".join(logs))
        by_id = {s["id"]: s for s in task.state["subtasks"]}
        self.assertEqual(by_id["st-1-alpha"]["actual_files"], ["alpha.py"],
                         "the replan rebased its diff onto its sibling's work")
        self.assertEqual(by_id["st-2-beta"]["actual_files"], ["beta.py"])
        for s in by_id.values():
            self.assertEqual(s["scope_violations"], [], s["id"])

    def test_a_replan_reaps_the_worktree_of_a_subtask_it_drops(self):
        """`_reap_worktrees` names paths out of `state["subtasks"]`, so a plan
        that renames a subtask leaves a checkout nobody can name and the reaper
        walks past it forever — the failure the persistence it relies on exists
        to prevent."""
        rounds = {"review": 0}
        RENAMED = self.INDEPENDENT.replace("st-1-alpha", "st-9-delta")

        def planner(env, cwd):
            # Keyed on the review, not on a call counter: `brainstorm`
            # dispatches the planner too, so counting planner calls made the
            # very first plan the renamed one and st-1-alpha never existed.
            body = self.INDEPENDENT if rounds["review"] == 0 else RENAMED
            with open(os.path.join(env["AGENT_DELEGATION_TASK_DIR"], "plan.md"), "w") as fh:
                fh.write(body)

        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            name = _tree_name(cwd)
            sub = next((s for s in ("st-1-alpha", "st-9-delta", "st-2-beta")
                        if s in name), "st-2-beta")
            with open(os.path.join(cwd, sub.split("-")[-1] + ".py"), "w") as fh:
                fh.write("V = %d\n" % (rounds["review"] + 1))
            _report(td, "implement-%s.json" % sub, {
                "stage": "implement", "role": "implementer", "subtask": sub,
                "status": "complete",
                "summary": "round %s" % ("one" if not rounds["review"] else "two"),
                "evidence": {"tests": "ok"}})

        def reviewer(env, cwd):
            rounds["review"] += 1
            first = rounds["review"] == 1
            _report(env["AGENT_DELEGATION_TASK_DIR"], "review-reviewer.json", {
                "stage": "review", "role": "reviewer", "status": "complete",
                "summary": "ruled", "evidence": {"tests": "ok"},
                "role_data": {"verdict": {
                    "verdict": "REPLAN" if first else "APPROVE",
                    "ac_table": [{"ac": "AC-1",
                                  "status": "unmet" if first else "met"}],
                    "findings": []}}})

        script = {"planner": planner, "implementer": implementer,
                  "test-author": lambda e, c: None, "reviewer": reviewer}
        task = store.Task.create(self.t.repo, "T-SC9", "# t\n\nAdd two things\n",
                                 self._pol(2))
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=logs.append).run()
        self.assertEqual(status, "done", "\n".join(logs))
        trees = subprocess.run(["git", "worktree", "list"], cwd=self.t.repo,
                               capture_output=True, text=True).stdout
        self.assertNotIn("T-SC9-st-1-alpha", trees,
                         "the dropped subtask's worktree outlived the task:\n" + trees)

    def test_a_merge_failure_does_not_swallow_a_siblings_replan(self):
        """Merging the finished members before classifying the wave's outcomes
        let a `Halt` from `_integrate_wave` outrank a sibling's `Replan` — the
        ladder's rung 3, with its escalation bundle already written — and would
        do the same to a quota park's reopen time. That is the very
        "the sequential and parallel paths disagreed about the same event"
        failure the re-raises exist to prevent."""
        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            name = _tree_name(cwd)
            if "st-3-gamma" in name:
                _report(td, "implement-st-3-gamma.json", {
                    "stage": "implement", "role": "implementer",
                    "subtask": "st-3-gamma", "status": "escalate",
                    "summary": "the plan is wrong",
                    "signals": [{"type": "plan_conflict",
                                 "detail": "gamma cannot be built as planned",
                                 "evidence": "no interface to build against"}],
                    "evidence": {"not_verified": ["everything"]}})
                return
            sub = "st-1-alpha" if "st-1-alpha" in name else "st-2-beta"
            # Both write the same file with different content. Their DECLARED
            # scopes are disjoint, so the wave still forms all three — and the
            # second merge is an add/add conflict the scripted integrator
            # cannot resolve, which is how `_integrate_wave` is made to Halt.
            with open(os.path.join(cwd, "shared.py"), "w") as fh:
                fh.write("OWNER = %r\n" % sub)
            _report(td, "implement-%s.json" % sub, {
                "stage": "implement", "role": "implementer", "subtask": sub,
                "status": "complete", "summary": "did %s" % sub,
                "evidence": {"tests": "ok"}})

        script = {"planner": self._planner(self.CONFLICTING),
                  "implementer": implementer, "test-author": lambda e, c: None,
                  "integrator": lambda e, c: None, "reviewer": self._approve}
        task = store.Task.create(self.t.repo, "T-SCA", "# t\n\nAdd three things\n",
                                 self._pol(3))
        logs = []
        Orchestrator(task, self.reg, runtime.MockAdapter(script),
                     lambda k, t: True, log=logs.append).run()
        self.assertEqual(task.state["spent"]["replans"], 1,
                         "the sibling's rung 3 was swallowed by the merge "
                         "failure\n" + "\n".join(logs))
        self.assertIn("plan_conflict", task.read_text("escalation.md", ""),
                      "the escalation bundle names the wrong signal")

    def test_a_failed_merge_leaves_no_conflict_markers_behind(self):
        """`_reconcile` could raise out of an unfinished merge, and the run does
        not always end there — a sibling's `Replan` continues at the plan stage,
        where `_author_tests` does a blind `git add -A && git commit` and
        committed the conflict markers as the resolution. The run then reached
        `done` and shipped them in the patch."""
        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            name = _tree_name(cwd)
            if "st-3-gamma" in name:
                _report(td, "implement-st-3-gamma.json", {
                    "stage": "implement", "role": "implementer",
                    "subtask": "st-3-gamma", "status": "escalate",
                    "summary": "the plan is wrong",
                    "signals": [{"type": "plan_conflict",
                                 "detail": "gamma cannot be built as planned",
                                 "evidence": "no interface to build against"}],
                    "evidence": {"not_verified": ["everything"]}})
                return
            sub = "st-1-alpha" if "st-1-alpha" in name else "st-2-beta"
            with open(os.path.join(cwd, "shared.py"), "w") as fh:
                fh.write("OWNER = %r\n" % sub)
            _report(td, "implement-%s.json" % sub, {
                "stage": "implement", "role": "implementer", "subtask": sub,
                "status": "complete", "summary": "did %s" % sub,
                "evidence": {"tests": "ok"}})

        script = {"planner": self._planner(self.CONFLICTING),
                  "implementer": implementer, "test-author": lambda e, c: None,
                  "integrator": lambda e, c: None, "reviewer": self._approve}
        task = store.Task.create(self.t.repo, "T-SCD", "# t\n\nAdd three things\n",
                                 self._pol(3))
        logs = []
        Orchestrator(task, self.reg, runtime.MockAdapter(script),
                     lambda k, t: True, log=logs.append).run()
        wt = task.state["worktree"]
        mid = subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                             cwd=wt, capture_output=True, text=True)
        self.assertNotEqual(mid.returncode, 0,
                            "the worktree was left mid-merge: %s" % mid.stdout)
        # The whole history of the branch, not just its tip: a later round can
        # overwrite the file, so asking HEAD alone would miss a commit that
        # introduced markers and a subsequent one that happened to clear them.
        # `-S` finds any commit that changed how often the string occurs.
        introduced = subprocess.run(
            ["git", "log", "-S", "<<<<<<<", "--oneline", task.state["branch"]],
            cwd=self.t.repo, capture_output=True, text=True).stdout
        self.assertEqual(introduced.strip(), "",
                         "a commit carried conflict markers:\n%s" % introduced)
        self.assertNotIn("<<<<<<<", task.read_text("integrate.patch", ""),
                         "the delivered patch carries markers")

    def test_a_member_behind_a_broken_merge_is_not_left_complete(self):
        """`_integrate_wave` raises on the first member that breaks the branch,
        and every member behind it was already marked `complete` by
        `_finish_subtask` — but never merged, and a complete subtask never
        rejoins a wave. Its work was delivered as done and absent, the same
        silent loss as a partially failed wave, through the broken-merge door.
        """
        state = {"beta_breaks": True}

        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            name = _tree_name(cwd)
            sub = next(s for s in ("st-1-alpha", "st-2-beta", "st-3-gamma")
                       if s in name)
            leaf = sub.split("-")[-1]
            with open(os.path.join(cwd, leaf + ".py"), "w") as fh:
                fh.write("V = 1\n")
            if sub == "st-2-beta" and state["beta_breaks"]:
                # Poisons the shared config only once merged: alone in its own
                # worktree the checks still pass, which is the case the
                # post-merge verify exists to catch.
                with open(os.path.join(cwd, "boom.py"), "w") as fh:
                    fh.write("raise SystemExit(1)\n")
            _report(td, "implement-%s.json" % sub, {
                "stage": "implement", "role": "implementer", "subtask": sub,
                "status": "complete", "summary": "did %s" % sub,
                "evidence": {"tests": "ok"}})

        # A script, not an inline `python3 -c`: the check needs nested quotes,
        # and `yamlite` does not process backslash escapes inside a quoted
        # scalar, so the command reaches the shell with its backslashes intact
        # and fails to parse — which looks like every check failing rather than
        # like a broken fixture.
        _spit(os.path.join(self.t.repo, "combo_check.py"),
              "import os, sys\n"
              "sys.exit(1 if os.path.exists('boom.py') and "
              "os.path.exists('alpha.py') else 0)\n")
        _spit(os.path.join(self.t.repo, ".adg.yaml"),
              'fast:\n  - "python3 combo_check.py"\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "combination check"], self.t.repo)

        script = {"planner": self._planner(self.CONFLICTING),
                  "implementer": implementer, "test-author": lambda e, c: None,
                  "integrator": lambda e, c: None, "reviewer": self._approve}
        task = store.Task.create(self.t.repo, "T-SCE", "# t\n\nAdd three things\n",
                                 self._pol(3))
        logs = []
        first = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                             lambda k, t: True, log=logs.append).run()
        self.assertEqual(first, "needs_human", "\n".join(logs))
        by_id = {s["id"]: s for s in task.state["subtasks"]}
        for sid, s in by_id.items():
            if s.get("status") == "complete":
                self.assertTrue(s.get("merged"),
                                "%s is complete but its branch never merged" % sid)

    def test_a_request_for_changes_with_nothing_blocking_stops(self):
        """`severity: minor` is legal, so `blocking` can legally be empty — and
        then no owner is named, `_reopen_subtasks` falls through to reopening
        EVERYTHING, and `pending_findings` is empty. Every green subtask was
        rebuilt from scratch with no idea what was wrong, through a
        schema-legal verdict."""
        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            sub = "st-1-alpha" if "st-1-alpha" in _tree_name(cwd) else "st-2-beta"
            with open(os.path.join(cwd, sub.split("-")[-1] + ".py"), "w") as fh:
                fh.write("V = 1\n")
            _report(td, "implement-%s.json" % sub, {
                "stage": "implement", "role": "implementer", "subtask": sub,
                "status": "complete", "summary": "done", "evidence": {"tests": "ok"}})

        def reviewer(env, cwd):
            _report(env["AGENT_DELEGATION_TASK_DIR"], "review-reviewer.json", {
                "stage": "review", "role": "reviewer", "status": "complete",
                "summary": "ruled", "evidence": {"tests": "ok"},
                "role_data": {"verdict": {
                    "verdict": "REQUEST_CHANGES",
                    "ac_table": [{"ac": "AC-1", "status": "unmet"}],
                    "findings": [{"id": "f-1", "severity": "minor",
                                  "claim": "naming could be better",
                                  "cite": "taste"}]}}})

        script = {"planner": self._planner(self.INDEPENDENT),
                  "implementer": implementer, "test-author": lambda e, c: None,
                  "reviewer": reviewer}
        task = store.Task.create(self.t.repo, "T-SCB", "# t\n\nAdd two things\n",
                                 self._pol(2))
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=logs.append).run()
        self.assertEqual(status, "needs_human")
        self.assertIn("no blocking finding", "\n".join(logs))

    def test_a_request_for_changes_with_no_findings_at_all_says_so(self):
        # `%` binds tighter than `or`, so the fallback clause was unreachable
        # and the message trailed off after its colon.
        task = store.Task.create(self.t.repo, "T-SCF", "# t\n", self._pol(1))
        task.update(subtasks=[{"id": "st-1", "status": "complete",
                               "planned_scope": ["**"], "actual_files": []}])
        orch = Orchestrator(task, self.reg, runtime.MockAdapter(),
                            lambda k, t: True, log=lambda *_: None)
        with self.assertRaises(Halt) as cm:
            orch._apply_verdict({"verdict": "REQUEST_CHANGES", "ac_table": [],
                                 "findings": []})
        self.assertIn("listed no findings at all", str(cm.exception))

    def test_a_finding_naming_no_subtask_still_reaches_whoever_reworks(self):
        """`suggested_owner` is written by hand against ids the planner chose.
        One naming no subtask reopened `subtasks[0]` on the no-owners
        fallback — and `_findings_brief` then filtered it out of that subtask's
        brief, because it has an owner and this is not it. The agent was sent
        back to rebuild with the reason withheld."""
        seen, rounds = [], {"review": 0}

        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            sub = "st-1-alpha" if "st-1-alpha" in _tree_name(cwd) else "st-2-beta"
            if _tree_name(cwd).endswith("-work"):
                sub = "st-1-alpha"
            with open(os.path.join(cwd, sub.split("-")[-1] + ".py"), "w") as fh:
                fh.write("V = %d\n" % (rounds["review"] + 1))
            _report(td, "implement-%s.json" % sub, {
                "stage": "implement", "role": "implementer", "subtask": sub,
                "status": "complete",
                "summary": "round %s" % ("one" if not rounds["review"] else "two"),
                "evidence": {"tests": "ok"}})

        def reviewer(env, cwd):
            rounds["review"] += 1
            first = rounds["review"] == 1
            _report(env["AGENT_DELEGATION_TASK_DIR"], "review-reviewer.json", {
                "stage": "review", "role": "reviewer", "status": "complete",
                "summary": "ruled", "evidence": {"tests": "ok"},
                "role_data": {"verdict": {
                    "verdict": "REQUEST_CHANGES" if first else "APPROVE",
                    "ac_table": [{"ac": "AC-1",
                                  "status": "unmet" if first else "met"}],
                    "findings": ([{"id": "f-1", "severity": "blocking",
                                   "claim": "the guard is missing",
                                   "cite": "AC-1",
                                   "suggested_owner": "st-7-nonexistent"}]
                                 if first else [])}}})

        class Recorder(runtime.MockAdapter):
            def prompt(self, session, text, timeout):
                if session.handle["role"] == "implementer":
                    seen.append(text)
                return runtime.MockAdapter.prompt(self, session, text, timeout)

        script = {"planner": self._planner(self.INDEPENDENT),
                  "implementer": implementer, "test-author": lambda e, c: None,
                  "reviewer": reviewer}
        task = store.Task.create(self.t.repo, "T-SCC", "# t\n\nAdd two things\n",
                                 self._pol(2))
        logs = []
        status = Orchestrator(task, self.reg, Recorder(script), lambda k, t: True,
                              log=logs.append).run()
        self.assertEqual(status, "done", "\n".join(logs))
        rework = [t for t in seen if "the guard is missing" in t]
        self.assertTrue(rework,
                        "the finding reached nobody: it named a subtask that "
                        "does not exist, so it was filtered out of every brief")

    def test_every_reworking_subtask_gets_its_own_findings(self):
        """`_finish_subtask` cleared the whole `pending_findings` list, so the
        first subtask to go green disarmed the rework for every other one: a
        reviewer that rejected st-1 and st-2 saw st-1 fixed, and st-2 was then
        re-dispatched with nothing -- back to the same code with the same
        prompt, rebuilding what was rejected. Sequential, with a cap of one:
        no concurrency is involved, the second subtask is simply dispatched
        after the first has finished."""
        seen, rounds, holder = [], {"review": 0}, {}

        class Recorder(runtime.MockAdapter):
            """The prompt is how a sequential mock knows which subtask it is:
            at a cap of one every subtask runs in the same integration
            worktree, so there is no directory name to read."""

            last = ""

            def prompt(self, session, text, timeout):
                if session.handle["role"] == "implementer":
                    self.last = text
                    seen.append(text)
                return runtime.MockAdapter.prompt(self, session, text, timeout)

        def implementer(env, cwd):
            sub = ("st-1-alpha" if "Your subtask: st-1-alpha" in holder["a"].last
                   else "st-2-beta")
            with open(os.path.join(cwd, sub.split("-")[-1] + ".py"), "w") as fh:
                fh.write("V = %d\n" % rounds["review"])
            _report(env["AGENT_DELEGATION_TASK_DIR"], "implement-%s.json" % sub, {
                "stage": "implement", "role": "implementer", "subtask": sub,
                "status": "complete",
                # Distinct per round, and it has to be: `_collect_report`
                # decides freshness on (mtime, size), so a byte-identical
                # rewrite inside one filesystem tick reads as no report at all.
                "summary": "round %s" % ("one" if rounds["review"] == 0 else "two"),
                "evidence": {"tests": "ok"}})

        def reviewer(env, cwd):
            rounds["review"] += 1
            first = rounds["review"] == 1
            verdict = {
                "verdict": "REQUEST_CHANGES" if first else "APPROVE",
                "ac_table": [{"ac": "AC-1", "status": "unmet" if first else "met"}],
                "findings": ([{"id": "f-1", "severity": "blocking",
                               "claim": "alpha is wrong", "cite": "AC-1",
                               "suggested_owner": "st-1-alpha"},
                              {"id": "f-2", "severity": "blocking",
                               "claim": "beta is wrong", "cite": "AC-2",
                               "suggested_owner": "st-2-beta"}] if first else []),
            }
            _report(env["AGENT_DELEGATION_TASK_DIR"], "review-reviewer.json", {
                "stage": "review", "role": "reviewer", "status": "complete",
                "summary": "ruled", "evidence": {"tests": "ok"},
                "role_data": {"verdict": verdict}})

        script = {"planner": self._planner(self.INDEPENDENT),
                  "implementer": implementer, "test-author": lambda e, c: None,
                  "reviewer": reviewer}
        adapter = Recorder(script)
        holder["a"] = adapter
        task = store.Task.create(self.t.repo, "T-SC4", "# t\n\nAdd two things\n",
                                 self._pol(1))
        logs = []
        status = Orchestrator(task, self.reg, adapter, lambda k, t: True,
                              log=logs.append).run()
        self.assertEqual(status, "done", "\n".join(logs))

        mine = [t for t in seen if "Your subtask: st-2-beta" in t]
        self.assertEqual(len(mine), 2, "st-2-beta should have been dispatched "
                                       "once, then again for the rework")
        self.assertIn("beta is wrong", mine[-1],
                      "the second reworking subtask was dispatched with no "
                      "findings — a sibling's completion had cleared them")
        self.assertNotIn("alpha is wrong", mine[-1],
                         "it was handed another subtask's finding")
        self.assertEqual(task.state.get("pending_findings"), [],
                         "findings outlived the review that raised them")


class TestSecondColdRead(unittest.TestCase):
    """What a third cold read found under `TestSubtaskContinuity`.

    The same family -- a subtask's work, or the reason it was sent back, going
    missing while the run reports success -- but reached through the paths the
    continuity fixes themselves opened: a flag that is set and never cleared, a
    handler narrower than the exceptions that reach it, a union that can only
    grow. None is a race; each reproduces on the first run and every run.
    """

    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)
        self.pol = dict(self.reg["policy"]["limits"],
                        escalation_ceiling=self.reg["policy"]["escalation_ceiling"])
        self.task = store.Task.create(self.t.repo, "T-CR", "# t\n\ndo it\n", self.pol)
        self.logs = []

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.t.repo, capture_output=True)
        self.t.close()

    def _orch(self, adapter=None):
        return Orchestrator(self.task, self.reg,
                            adapter or runtime.MockAdapter({}),
                            lambda k, t: True, log=self.logs.append)

    def _branches(self):
        out = subprocess.run(["git", "branch", "--format=%(refname:short)"],
                             cwd=self.t.repo, capture_output=True, text=True).stdout
        return set(out.split())

    def _commit(self, cwd, name, body):
        with open(os.path.join(cwd, name), "w") as fh:
            fh.write(body)
        sh(["git", "add", "-A"], cwd)
        sh(["git", "commit", "-qm", "add %s" % name], cwd)

    # --- the flag that outlived what it described ---------------------------

    def test_reopening_a_subtask_clears_the_flag_that_says_it_landed(self):
        """`merged` is what `_unfinish` reads to decide whether a subtask that
        did not make it onto the integration branch may stay `complete`. It was
        set on a successful merge and cleared nowhere, so a subtask that landed
        in wave one and was then sent back by the reviewer carried the flag into
        its rework: when THAT merge was aborted, `_unfinish`'s
        `complete and not merged` guard was false, the subtask stayed complete,
        and the run fell through review to integrate and delivered a patch with
        none of the rework in it."""
        self.task.update(subtasks=[
            {"id": "st-1", "status": "complete", "merged": True, "actual_files": []},
            {"id": "st-2", "status": "complete", "merged": True, "actual_files": []}])
        orch = self._orch()
        orch._reopen_subtasks({"st-1"})

        by_id = {s["id"]: s for s in self.task.state["subtasks"]}
        self.assertEqual(by_id["st-1"]["status"], "pending")
        self.assertFalse(by_id["st-1"].get("merged"),
                         "a reopened subtask still claims its work is on the "
                         "integration branch, so `_unfinish` will refuse to "
                         "reopen it when the rework's own merge fails")
        # The one that was not reopened is untouched: its work really is landed.
        self.assertEqual(by_id["st-2"]["status"], "complete")
        self.assertTrue(by_id["st-2"]["merged"])

        # And the guard now bites: mark it complete again without a merge, and
        # `_unfinish` sends it back rather than letting it be delivered as done.
        self.task.update(subtasks=[dict(s, status="complete")
                                   for s in self.task.state["subtasks"]])
        orch._unfinish([{"id": "st-1"}])
        self.assertEqual({s["id"]: s["status"]
                          for s in self.task.state["subtasks"]}["st-1"], "pending")

    def test_the_no_owners_fallback_clears_the_flag_too(self):
        """`_reopen_subtasks` with nothing named reopens `subtasks[0]` through a
        separate branch, which is exactly the path a REQUEST_CHANGES whose
        findings name no subtask takes."""
        self.task.update(subtasks=[{"id": "st-1", "status": "complete",
                                    "merged": True, "actual_files": []}])
        self._orch()._reopen_subtasks({"st-nonexistent"})
        s = self.task.state["subtasks"][0]
        self.assertEqual(s["status"], "pending")
        self.assertFalse(s.get("merged"))

    # --- the handler narrower than what reaches it --------------------------

    def _conflicting_wave(self, orch):
        """An integration worktree and one subtask branch that cannot merge into
        it: both edit the same line from the same parent."""
        integration = orch._ensure_worktree()
        self._commit(integration, "shared.py", "V = 'integration'\n")
        branch = "adg/%s/st-1" % self.task.state["id"]
        tree = os.path.join(self.t.dir, "st1-tree")
        base = store.git(["rev-parse", "HEAD^"], integration).strip()
        sh(["git", "worktree", "add", "-b", branch, tree, base], self.t.repo)
        self._commit(tree, "shared.py", "V = 'subtask'\n")
        self.task.update(subtasks=[{"id": "st-1", "status": "complete",
                                    "worktree": tree, "actual_files": []}])
        return integration

    def test_a_merge_is_aborted_even_when_the_integrator_cannot_be_reached(self):
        """`_integrate_wave` cleaned up after `_reconcile` on `Halt` alone. But
        `_reconcile` dispatches an agent, so it also raises `AllChannelsCooled`
        (every integrator seat walled mid-conflict) and adapter `RuntimeError_`
        -- neither a `Halt`, and both unwound straight past the handler, leaving
        MERGE_HEAD set and conflict markers in the files. The run does not end
        there, and the next `_checkpoint` is a blind `git add -A && git commit`,
        so the markers were committed as the resolution and shipped."""
        for boom in (router.AllChannelsCooled("integrator", ["seat"],
                                               time.time() + 60, "all cooled"),
                     runtime.RuntimeError_("the integrator CLI vanished")):
            with self.subTest(raised=type(boom).__name__):
                self.tearDown()
                self.setUp()
                orch = self._orch()
                integration = self._conflicting_wave(orch)
                orch._reconcile = lambda *a, **k: (_ for _ in ()).throw(boom)

                with self.assertRaises(type(boom)):
                    orch._integrate_wave([self.task.state["subtasks"][0]])

                self.assertFalse(
                    os.path.exists(os.path.join(integration, ".git")) and
                    os.path.exists(store.git(["rev-parse", "--git-dir"],
                                       integration).strip() + "/MERGE_HEAD"),
                    "the integration worktree was left mid-merge, so the next "
                    "blind `git add -A && git commit` ships conflict markers")
                with open(os.path.join(integration, "shared.py")) as fh:
                    self.assertNotIn("<<<<<<<", fh.read(),
                                     "conflict markers were left in the tree")
                self.assertEqual(self.task.state["subtasks"][0]["status"], "pending",
                                 "a subtask that never reached the integration "
                                 "branch was left marked complete")

    def test_a_cooled_integrator_still_parks_on_quota(self):
        """`AllChannelsCooled` subclasses `NoModelAvailable` precisely so a
        mandatory stage propagates it to the park handler. `_reconcile` caught
        the parent, so the one exception carrying a reopen time was flattened
        into a bare `Halt("needs_human")`: `_quota_park` never ran, no `park`
        was written, and `resume --when-open` returned immediately on a run that
        was merely early."""
        orch = self._orch()
        cooled = router.AllChannelsCooled("integrator", ["claude-seat"],
                                           time.time() + 3600, "all cooled")

        def pick(role, **kw):
            raise cooled
        orch._pick = pick
        with self.assertRaises(router.AllChannelsCooled):
            orch._reconcile({"id": "st-1"}, self.t.repo, "CONFLICT")

        # ... and an ordinary "nothing enrolled" still halts, as it always did.
        def none_available(role, **kw):
            raise router.NoModelAvailable("no integrator enrolled")
        orch._pick = none_available
        with self.assertRaises(Halt):
            orch._reconcile({"id": "st-1"}, self.t.repo, "CONFLICT")

    # --- the base that moved under the branch -------------------------------

    def test_a_deleted_worktree_does_not_move_the_task_base(self):
        """`_ensure_worktree` re-ran whenever the checkout was missing -- a
        parked task whose worktree the user deleted, which both READMEs invite
        by calling them throwaway -- and rewrote `base_commit` to today's HEAD.
        `create_worktree` reattaches the branch that already exists and ignores
        the base it is handed, so the branch still forked where it always did:
        `_land` then wrote `git diff <today> <branch>`, a patch carrying a
        reverse-delta for every commit the user made while the task was parked.
        Applying it would have reverted their own work."""
        orch = self._orch()
        tree = orch._ensure_worktree()
        original = self.task.state["repo"]["base_commit"]

        # The user cleans up the checkout and gets on with their own work.
        subprocess.run(["git", "worktree", "remove", "--force", tree],
                       cwd=self.t.repo, capture_output=True)
        self._commit(self.t.repo, "unrelated.py", "MINE = 1\n")
        moved = store.git(["rev-parse", "HEAD"], self.t.repo).strip()
        self.assertNotEqual(original, moved)

        self._orch()._ensure_worktree()
        self.assertEqual(self.task.state["repo"]["base_commit"], original,
                         "the task base followed the user's own commits, so the "
                         "delivered patch would revert them")

    # --- the branch a discarded plan left behind ----------------------------

    def test_a_replan_does_not_inherit_an_unmerged_branch(self):
        """A REPLAN is raised by the subtask that failed, so the branch behind a
        reissued id holds the attempt written for the plan that was just thrown
        away. `_prepare` re-reads HEAD *above* those commits, so `actual_files`
        and the scope check never saw them -- while `_integrate_wave` merges the
        branch whole, landing work for an abandoned goal in the patch with
        nothing accounting for it."""
        orch = self._orch()
        branch = "adg/%s/st-1" % self.task.state["id"]
        tree = os.path.join(self.t.dir, "st1-tree")
        sh(["git", "worktree", "add", "-b", branch, tree], self.t.repo)
        self._commit(tree, "abandoned.py", "DISCARDED = 1\n")
        self.task.update(subtasks=[{"id": "st-1", "status": "pending",
                                    "worktree": tree, "base_commit": "deadbee",
                                    "actual_files": []}])

        fresh = orch._reseed([{"id": "st-1", "goal": "something else",
                               "planned_scope": ["other.py"]}])

        self.assertNotIn(branch, self._branches(),
                         "the reissued id kept the discarded plan's branch, so "
                         "its commits land in the patch unaccounted for")
        self.assertIn(branch + ".replaced-1", self._branches(),
                      "the abandoned attempt was destroyed rather than shelved")
        self.assertNotIn("worktree", fresh[0])
        self.assertNotIn("base_commit", fresh[0])
        self.assertFalse(os.path.isdir(tree))

    def test_a_replan_keeps_a_branch_whose_work_already_landed(self):
        """The other side of the same rule. Work that reached the integration
        branch is accounted for, so there is nothing to strand: that checkout is
        kept and `_catch_up` brings it current, which is what preserves an
        interrupted subtask's salvage checkpoints across a replan."""
        orch = self._orch()
        branch = "adg/%s/st-1" % self.task.state["id"]
        tree = os.path.join(self.t.dir, "st1-tree")
        sh(["git", "worktree", "add", "-b", branch, tree], self.t.repo)
        self.task.update(subtasks=[{"id": "st-1", "status": "complete",
                                    "worktree": tree, "base_commit": "cafe123",
                                    "merged": True, "actual_files": []}])

        fresh = orch._reseed([{"id": "st-1", "goal": "refined"}])
        self.assertIn(branch, self._branches())
        self.assertEqual(fresh[0]["worktree"], tree)
        self.assertEqual(fresh[0]["base_commit"], "cafe123")
        self.assertFalse(fresh[0].get("merged"),
                         "the reissued id inherited `merged`, which would tell "
                         "`_unfinish` that work it has not done yet is landed")

    # --- what a union could not take back -----------------------------------

    def test_a_file_a_rework_deleted_leaves_the_file_list(self):
        """`actual_files` accumulates because the diff base moves forward with
        the tree on every dispatch. But a union can only grow, so a file the
        rework DELETED came straight back -- the deletion is itself a change --
        and went on being reported as a scope violation, went on blocking
        `_skip_review`, and was still shown at the merge gate as a changed file
        the patch does not contain."""
        orch = self._orch()
        tree = orch._ensure_worktree()
        sub = {"id": "st-1", "status": "pending", "planned_scope": ["kept.py"],
               "actual_files": []}
        self.task.update(subtasks=[sub])

        self._commit(tree, "kept.py", "KEPT = 1\n")
        self._commit(tree, "scratch.py", "SCRATCH = 1\n")
        orch._finish_subtask(sub, tree)
        landed = self.task.state["subtasks"][0]
        self.assertEqual(landed["actual_files"], ["kept.py", "scratch.py"])
        self.assertEqual(landed["scope_violations"], ["scratch.py"])

        # The rework does what the finding asked: the stray file goes away. Its
        # own diff base has moved on to the commit above, so the deletion is the
        # only thing it shows.
        os.remove(os.path.join(tree, "scratch.py"))
        sh(["git", "add", "-A"], tree)
        sh(["git", "commit", "-qm", "drop the stray file"], tree)
        sub["base_commit"] = store.git(["rev-parse", "HEAD^"], tree).strip()
        orch._finish_subtask(sub, tree)

        landed = self.task.state["subtasks"][0]
        self.assertEqual(landed["actual_files"], ["kept.py"],
                         "a file with no net effect is still listed as changed")
        self.assertEqual(landed["scope_violations"], [],
                         "the rework fixed the scope violation and was still "
                         "charged with it")

    # --- the commit the orchestrator makes on its own account ---------------

    def test_a_merge_does_not_depend_on_the_users_git_identity(self):
        """`_checkpoint` always passed `-c user.email/-c user.name`, because the
        pipeline must not depend on the user having configured git. The merges
        did not, and a merge writes a commit too. With no identity anywhere, a
        fast-forward still succeeds while a real merge exits non-zero with
        "Committer identity unknown" -- which `_integrate_wave` reads as a
        conflict: it pays an integrator to resolve one that does not exist,
        finds no unmerged paths, verifies a branch nothing was merged into, and
        marks the subtask landed. Success, with none of its work in the patch."""
        orch = self._orch()
        integration = orch._ensure_worktree()
        # Diverging, so the merge needs a commit of its own rather than a
        # fast-forward -- the only case that needs an identity.
        self._commit(integration, "integration.py", "SIDE = 'a'\n")
        branch = "adg/%s/st-1" % self.task.state["id"]
        tree = os.path.join(self.t.dir, "st1-tree")
        base = store.git(["rev-parse", "HEAD^"], integration).strip()
        sh(["git", "worktree", "add", "-b", branch, tree, base], self.t.repo)
        self._commit(tree, "subtask.py", "SIDE = 'b'\n")
        self.task.update(subtasks=[{"id": "st-1", "status": "complete",
                                    "worktree": tree, "actual_files": []}])

        # Only now, so the fixture's own commits above are unaffected. Three
        # things together, because any one alone leaves an identity standing:
        # the repository's own config, the developer's ~/.gitconfig (which would
        # otherwise make this pass on a laptop and fail in the container it
        # describes), and git's willingness to invent `user@host` from the gecos
        # field, which `useConfigOnly` is what switches off. The local config is
        # shared by every linked worktree, so this covers both merge sites.
        sh(["git", "config", "--unset", "user.email"], self.t.repo)
        sh(["git", "config", "--unset", "user.name"], self.t.repo)
        sh(["git", "config", "user.useConfigOnly", "true"], self.t.repo)
        restore = {k: os.environ.get(k)
                   for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")}
        for key in restore:
            os.environ[key] = os.devnull
        try:
            def refuse(*a, **k):
                raise AssertionError("an integrator was dispatched to resolve a "
                                     "conflict that does not exist")
            orch._reconcile = refuse
            orch._integrate_wave([self.task.state["subtasks"][0]])

            self.assertTrue(os.path.isfile(os.path.join(integration, "subtask.py")),
                            "the run reported the merge and delivered none of it")
            self.assertTrue(self.task.state["subtasks"][0]["merged"])

            # `_catch_up` writes a merge commit on the same terms, and its
            # failure is quieter still: it logs that the worktree could not be
            # brought up to the branch, and the rework runs on the stale tree.
            sh(["git", "-c", "user.email=t@example.com", "-c", "user.name=Test",
                "commit", "-q", "--allow-empty", "-m", "diverge"], tree)
            orch._catch_up(self.task.state["subtasks"][0], tree)
            self.assertTrue(os.path.isfile(os.path.join(tree, "integration.py")),
                            "the reused worktree was never brought up to the "
                            "integration branch: %s" % "\n".join(self.logs))
        finally:
            for key, was in restore.items():
                if was is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = was

    # --- the context the escalation paid for and threw away ------------------

    def test_the_stronger_model_is_told_what_the_last_one_already_tried(self):
        """`_one_subtask` builds the escalation context -- the signals, the
        evidence, the `attempted` list -- and hands it to `_invoke` as
        `failure`. `_invoke` read it only when continuing a session or handing
        over on a quota hop, and `_climb` returns `session = None` by
        construction, because the whole point is a different model. So the one
        path that most needs the context was the one path that dropped it: the
        dearer model opened on the plain role prompt and re-tried what its
        predecessor had already reported as failed. references/escalation.md:
        the next model is smarter, not clairvoyant."""
        PLAN = """# Plan

## Subtasks
```yaml
- id: st-1-alpha
  goal: add alpha
  file_scope: ["alpha.py"]
  acceptance: [AC-1]
```
"""
        rounds = {"n": 0}

        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            rounds["n"] += 1
            with open(os.path.join(cwd, "alpha.py"), "w") as fh:
                fh.write("ALPHA = %d\n" % rounds["n"])
            if rounds["n"] == 1:
                _report(td, "implement-st-1-alpha.json", {
                    "stage": "implement", "role": "implementer",
                    "subtask": "st-1-alpha", "status": "escalate",
                    "summary": "the fixture will not build",
                    "evidence": {"tests": "red"},
                    "signals": [{
                        "type": "test_stuck",
                        "detail": "the fixture deadlocks on import",
                        "evidence": "E   RuntimeError: event loop is closed",
                        "attempted": ["raising the timeout",
                                      "running the fixture eagerly"]}]})
                return
            _report(td, "implement-st-1-alpha.json", {
                "stage": "implement", "role": "implementer",
                "subtask": "st-1-alpha", "status": "complete",
                "summary": "did alpha", "evidence": {"tests": "ok"}})

        def reviewer(env, cwd):
            _report(env["AGENT_DELEGATION_TASK_DIR"], "review-reviewer.json", {
                "stage": "review", "role": "reviewer", "status": "complete",
                "summary": "ok", "evidence": {"tests": "ok"},
                "role_data": {"verdict": {"verdict": "APPROVE",
                                          "ac_table": [{"ac": "AC-1",
                                                        "status": "met"}],
                                          "findings": []}}})

        def planner(env, cwd):
            with open(os.path.join(env["AGENT_DELEGATION_TASK_DIR"], "plan.md"), "w") as fh:
                fh.write(PLAN)

        with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
            fh.write('fast:\n  - "python3 -c \'print(1)\'"\ntest_author: never\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "cfg"], self.t.repo)

        adapter = runtime.MockAdapter({
            "planner": planner, "implementer": implementer,
            "test-author": lambda e, c: None, "reviewer": reviewer})
        task = store.Task.create(self.t.repo, "T-ESC", "# t\n\nAdd alpha\n", self.pol)
        logs = []
        status = Orchestrator(task, self.reg, adapter, lambda k, t: True,
                              log=logs.append).run()
        self.assertEqual(status, "done", "\n".join(logs))

        sent = [text for role, text in adapter.calls if role == "implementer"]
        self.assertEqual(len(sent), 2, "the subtask should have been dispatched "
                                       "once, then again on the stronger seat")
        self.assertIn("event loop is closed", sent[-1],
                      "the escalated-to model was given no evidence")
        self.assertIn("raising the timeout", sent[-1],
                      "`attempted` never reached the replacement, so it is free "
                      "to repeat what already failed")
        # And the first dispatch is untouched: there is nothing to carry yet.
        self.assertNotIn("event loop is closed", sent[0])

    def test_the_failure_note_is_folded_in_without_losing_the_brief(self):
        """The caller's `extra` is the findings brief. A rework that loses its
        findings is the failure the hand-off exists to stop, so the note is
        appended rather than substituted."""
        both = Orchestrator._with_failure("THE FINDINGS", "WHAT FAILED")
        self.assertIn("THE FINDINGS", both)
        self.assertIn("WHAT FAILED", both)
        self.assertEqual(Orchestrator._with_failure("only extra", None),
                         "only extra")
        self.assertIn("only failure", Orchestrator._with_failure(None, "only failure"))
        self.assertIsNone(Orchestrator._with_failure(None, None))

    # --- the write that failed and said nothing -----------------------------

    def test_a_breaker_that_cannot_be_written_is_reported(self):
        """`cooldown._mutate` returns the reason precisely so a caller can say
        it -- "it must not pretend the write happened" -- and both production
        callers dropped it, which made orchestrator/README.md's promise that an
        unwritable file "is reported" false. A read-only state dir then cost
        real money in silence: the wall was never recorded, every seat read as
        ready, and the next run paid for the exhausted seat's refusal."""
        orch = self._orch()
        real = cooldown.record_use
        cooldown.record_use = lambda *a, **k: "channels.json is not writable"
        try:
            choice = orch.router.candidates("implementer")[0]
            orch._meter(choice)
            orch._meter(choice)
        finally:
            cooldown.record_use = real

        said = [l for l in self.logs if "not writable" in l]
        self.assertEqual(len(said), 1,
                         "a breaker write that failed was either never reported "
                         "or repeated once per invocation: %s" % self.logs)

    def test_status_falls_back_to_the_park_when_the_breaker_is_unreadable(self):
        """`cmd_status` asks the breaker rather than the snapshot in task.json,
        because the snapshot goes stale. But "the file could not be READ" is not
        the same answer as "those windows have reopened", and only the second
        means the task is runnable. Falling through reported a task that is
        merely early as one that needs a human to think, and took away the
        `resume --when-open` the paused-seat brief points at."""
        import io
        import contextlib

        self.task.update(status="needs_human",
                         park={"reason": "quota_all_exhausted", "role": "implementer",
                               "channels": ["claude-seat"],
                               "reopen_at": time.time() + 3600})
        os.makedirs(os.path.dirname(cooldown.path()), exist_ok=True)
        with open(cooldown.path(), "w") as fh:
            fh.write("{not json at all")

        class Args:
            repo, registry = self.t.repo, REGISTRY

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_status(Args())
        out = buf.getvalue()
        self.assertIn("waiting on quota", out,
                      "an unreadable breaker made a parked task look broken")
        self.assertIn("reopens at", out)
        self.assertIn("claude-seat", out)


class TestDispatchWorkflow(unittest.TestCase):
    """delegate as a general wrapper: the caller brings the decomposition.

    The full protocol is one use. The other is execution only — a skill that
    has already designed the work wants N jobs placed on N seats, in isolated
    worktrees, with metering and failover underneath, and does not want a
    planner second-guessing it or a reviewer it will not read. `--plan` plus
    `workflows/dispatch` is that, and it needed no new stage: the machine
    already recovers subtasks from plan.md when the state has none.
    """

    PLAN = """# Supplied by the caller

```yaml
- id: st-1-alpha
  goal: add alpha
  file_scope: ["alpha.py"]
  acceptance: [AC-1]
- id: st-2-beta
  goal: add beta
  file_scope: ["beta.py"]
  acceptance: [AC-2]
```
"""

    def setUp(self):
        self.t = TempRepo()
        with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
            fh.write('fast:\n  - "python3 -c \'print(1)\'"\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "cfg"], self.t.repo)
        self.reg = router.load_registry(REGISTRY)
        self.pol = dict(self.reg["policy"]["limits"],
                        escalation_ceiling=self.reg["policy"]["escalation_ceiling"],
                        max_parallel_agents=2)
        self.dispatch = os.path.join(
            os.path.dirname(wf.default_dir()), "dispatch")
        self.addCleanup(setattr, wf, "_CURRENT", None)

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.t.repo,
                       capture_output=True)
        self.t.close()

    def test_the_bundled_dispatch_workflow_runs_only_execution(self):
        self.assertTrue(os.path.isdir(self.dispatch), self.dispatch)
        w = wf.use(self.dispatch)
        for off in ("intake", "classify", "brainstorm", "plan", "review"):
            self.assertFalse(w.enabled(off), "%s should be switched off" % off)
        for on in ("implement", "integrate"):
            self.assertTrue(w.enabled(on), "%s must stay on" % on)
        # `integrate` in particular: it is the stage that merges the subtask
        # branches and writes the patch, so a dispatcher without it would end
        # the run with the work stranded on branches nobody merges.
        self.assertEqual(w.next_enabled("implement", order=STAGES), "integrate")
        self.assertEqual(w.next_enabled("intake", order=STAGES), "implement")

    def test_a_supplied_plan_is_executed_with_no_planner_or_reviewer(self):
        wf.use(self.dispatch)
        dispatched = []

        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            sub = "st-1-alpha" if "st-1-alpha" in _tree_name(cwd) else "st-2-beta"
            dispatched.append(sub)
            with open(os.path.join(cwd, sub.split("-")[-1] + ".py"), "w") as fh:
                fh.write("V = 1\n")
            _report(td, "implement-%s.json" % sub, {
                "stage": "implement", "role": "implementer", "subtask": sub,
                "status": "complete", "summary": "did it",
                "evidence": {"tests": "ok"}})

        def refuse(role):
            def fn(env, cwd):
                raise AssertionError("%s ran under the dispatch workflow" % role)
            return fn

        task = store.Task.create(self.t.repo, "T-DIS", "# t\n\ntwo things\n",
                                 self.pol)
        task.write_text("plan.md", self.PLAN)      # what `--plan` does
        logs = []
        status = Orchestrator(
            task, self.reg,
            runtime.MockAdapter({"implementer": implementer,
                                 "planner": refuse("planner"),
                                 "reviewer": refuse("reviewer"),
                                 "classifier": refuse("classifier")}),
            lambda k, t: True, log=logs.append).run()

        self.assertEqual(status, "done", "\n".join(logs))
        self.assertEqual(sorted(dispatched), ["st-1-alpha", "st-2-beta"])
        self.assertTrue(any("2 subtasks in parallel" in l for l in logs),
                        "the supplied subtasks did not form a wave:\n"
                        + "\n".join(logs))
        # The patch still gets written: `integrate` is deliberately kept on.
        patch = task.read_text("integrate.patch", "")
        self.assertIn("alpha.py", patch)
        self.assertIn("beta.py", patch)
        # And the work is accounted for exactly as under the full protocol.
        by_id = {s["id"]: s for s in task.state["subtasks"]}
        self.assertEqual(by_id["st-1-alpha"]["actual_files"], ["alpha.py"])
        self.assertEqual(by_id["st-2-beta"]["scope_violations"], [])

    def test_the_cli_writes_a_supplied_plan_where_the_machine_looks(self):
        """`--plan` needs no new machinery: it lands the file at the path
        `_stage_implement` already reads when the state has no subtasks."""
        src = os.path.join(self.t.dir, "mine.md")
        with open(src, "w") as fh:
            fh.write(self.PLAN)

        class Args:
            repo, registry, request = self.t.repo, REGISTRY, "do it"
            plan, id, tier, review = src, "T-CLI", "auto", "auto"
            mode, adapter, no_panes = "attended", "mock", True
            max_cost, dry_run, yes, workflow = None, True, True, None
            func = None

        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            cli.cmd_run(Args())
        task = store.Task.open(self.t.repo, "T-CLI")
        self.assertIn("st-1-alpha", task.read_text("plan.md", ""),
                      "the supplied plan never reached the task directory")
        self.assertIn("plan supplied by the caller", buf.getvalue())

    def test_a_plan_that_is_not_there_is_refused_before_anything_runs(self):
        class Args:
            repo, registry, request = self.t.repo, REGISTRY, "do it"
            plan, id, tier, review = "/no/such/plan.md", "T-MISS", "auto", "auto"
            mode, adapter, no_panes = "attended", "mock", True
            max_cost, dry_run, yes, workflow = None, True, True, None
            func = None

        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            cli.cmd_run(Args())
        self.assertIn("no plan at", str(cm.exception))


class TestCallerDeclaredTier(unittest.TestCase):
    """`--tier`, and the criteria behind the judgement it replaces.

    Whether to delegate at all is the caller's decision and lives in SKILL.md.
    Whether delegated work needs a plan was the machine's alone: a caller that
    had already designed the decomposition still paid a billed call to be told
    what it knew, and could be told differently. `auto` still judges, so
    `delegate` run by hand is unchanged.
    """

    PLAN = """# Plan

## Subtasks
```yaml
- id: st-1-alpha
  goal: add alpha
  file_scope: ["alpha.py"]
  acceptance: [AC-1]
```
"""

    def setUp(self):
        self.t = TempRepo()
        with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
            fh.write('fast:\n  - "python3 -c \'print(1)\'"\ntest_author: never\n'
                     'hotspots:\n  - "combat_system"\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "cfg"], self.t.repo)
        self.reg = router.load_registry(REGISTRY)
        self.pol = dict(self.reg["policy"]["limits"],
                        escalation_ceiling=self.reg["policy"]["escalation_ceiling"])
        self.logs = []

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.t.repo, capture_output=True)
        self.t.close()

    def _run_to_classify(self, request, tier=None, task_id="T-T"):
        """Drive intake+classify only, with a classifier that must not be asked
        when the caller has declared the tier."""
        asked = []

        def classifier(env, cwd):
            asked.append(True)
            return {"output": "VERDICT: SIMPLE -- tiny"}

        task = store.Task.create(self.t.repo, task_id, "# t\n\n%s\n" % request, self.pol)
        if tier:
            task.update(tier=tier)
        orch = Orchestrator(task, self.reg,
                            runtime.MockAdapter({"classifier": classifier}),
                            lambda k, t: True, log=self.logs.append)
        orch._stage_classify()
        return task, asked

    def test_a_declared_complex_tier_skips_the_classifier_and_plans(self):
        task, asked = self._run_to_classify("add a subtract function", "complex")
        self.assertEqual(asked, [], "the classifier was billed for a judgement "
                                    "the caller had already made")
        self.assertEqual(task.state["classification"]["tier"], "complex")
        self.assertEqual(task.state["classification"]["by"], "caller")
        self.assertIn(task.state["status"], ("brainstorm", "plan"))
        # And crucially it is NOT seeded with one catch-all subtask, which is
        # what would have collapsed a parallel wave into a single agent.
        self.assertFalse(task.state.get("subtasks"))

    def test_a_declared_simple_tier_skips_the_classifier_and_implements(self):
        task, asked = self._run_to_classify("rewrite the whole engine", "simple")
        self.assertEqual(asked, [])
        self.assertEqual(task.state["classification"]["tier"], "simple")
        self.assertEqual(task.state["status"], "implement")
        self.assertEqual([s["id"] for s in task.state["subtasks"]], ["st-1-main"])

    def test_auto_still_asks(self):
        """The default is unchanged: `delegate run` by hand judges for itself."""
        task, asked = self._run_to_classify("add a subtract function")
        self.assertEqual(len(asked), 1)
        self.assertEqual(task.state["classification"]["tier"], "simple")

    def test_a_declared_tier_beats_a_hotspot_but_says_so(self):
        """A hotspot is the PROJECT's standing declaration and `--tier` is the
        operator's, for one run; the later and more specific instruction wins.
        Not silently, because the declaration exists for files that merge badly
        when the work is not planned."""
        task, asked = self._run_to_classify("touch combat_system today", "simple")
        self.assertEqual(asked, [])
        self.assertEqual(task.state["classification"]["tier"], "simple")
        self.assertTrue(any("overrides a declared hotspot" in l for l in self.logs),
                        "a hotspot was overridden with nothing in the log: %s"
                        % self.logs)
        # With no override the hotspot still decides, as it always did.
        task2, asked2 = self._run_to_classify("touch combat_system today",
                                              task_id="T-T2")
        self.assertEqual(asked2, [], "a hotspot is not a judgement call")
        self.assertEqual(task2.state["classification"]["tier"], "complex")

    def test_an_unrecognised_tier_is_ignored_rather_than_obeyed(self):
        """Only the two tiers the machine routes on are honoured. Anything else
        falls through to the classifier rather than reaching `_classified`,
        which would set a status no handler answers to."""
        task, asked = self._run_to_classify("add a subtract function", "medium")
        self.assertEqual(len(asked), 1)
        self.assertEqual(task.state["classification"]["tier"], "simple")

    def test_the_criteria_come_from_the_manifest(self):
        """The judgement moved out of `machine.py`, where it opened "Classify
        this software task" -- one workflow's opinion living in the state
        machine. The tiers and the VERDICT line stay code, because
        `_classified` routes on the tier names."""
        seen = {}

        def classifier(env, cwd):
            seen["prompt"] = None
            return {"output": "VERDICT: COMPLEX -- big"}

        adapter = runtime.MockAdapter({"classifier": classifier})
        task = store.Task.create(self.t.repo, "T-T3", "# t\n\ndo a thing\n", self.pol)
        orch = Orchestrator(task, self.reg, adapter, lambda k, t: True,
                            log=self.logs.append)
        orch._stage_classify()
        prompt = [text for role, text in adapter.calls if role == "classifier"][0]
        self.assertIn("no new architecture", prompt,
                      "the manifest's criteria never reached the classifier")
        self.assertIn("VERDICT: SIMPLE|COMPLEX", prompt,
                      "the frame the parser reads must survive")
        self.assertNotIn("Classify this software task", prompt,
                         "the workflow's own wording is back in the machine")

    def test_a_workflow_declaring_its_own_criteria_gets_them(self):
        """The one that proves the manifest is really the source. The bundled
        criteria and `DEFAULT_CLASSIFY_CRITERIA` are the same words -- they have
        to be, the default workflow is the one that used to be hardcoded -- so
        asserting on that text cannot tell a manifest that was read from a
        fallback that was not. This declares something the machine has never
        heard of and looks for it."""
        d = tempfile.mkdtemp(prefix="wf-tier-")
        self.addCleanup(shutil.rmtree, d, True)
        self.addCleanup(setattr, wf, "_CURRENT", None)
        shutil.copy(os.path.join(wf.default_dir(), "PROTOCOL.md"), d)
        with open(os.path.join(d, "workflow.yaml"), "w") as fh:
            fh.write("""name: houses
protocol: PROTOCOL.md
stages:
  intake: {role: intake}
  classify:
    role: classifier
    criteria: |
      SIMPLE: one room, one trade, no permit.
      COMPLEX: touches the load-bearing walls.
  plan: {role: planner}
  implement: {role: implementer}
""")
        wf.use(d)

        adapter = runtime.MockAdapter(
            {"classifier": lambda e, c: {"output": "VERDICT: COMPLEX -- walls"}})
        task = store.Task.create(self.t.repo, "T-T4", "# t\n\nknock it through\n",
                                 self.pol)
        Orchestrator(task, self.reg, adapter, lambda k, t: True,
                     log=self.logs.append)._stage_classify()

        prompt = [text for role, text in adapter.calls if role == "classifier"][0]
        self.assertIn("touches the load-bearing walls", prompt,
                      "the workflow in force declared criteria and the machine "
                      "asked its own question anyway")
        self.assertNotIn("no new architecture", prompt,
                         "the default criteria were used beside the manifest's")
        self.assertEqual(task.state["classification"]["tier"], "complex")

    def test_a_workflow_with_no_criteria_still_classifies(self):
        """Silence in a manifest leaves a stage as the machine has it -- the
        same rule `enabled()` follows -- so a workflow with no opinion about
        what counts as complex gets the default, not a prompt with a hole."""
        from adg.machine import DEFAULT_CLASSIFY_CRITERIA, CLASSIFY_PROMPT
        w = wf.current()
        self.assertEqual(w.criteria("plan"), "")
        filled = CLASSIFY_PROMPT % {"criteria": w.criteria("plan") or DEFAULT_CLASSIFY_CRITERIA,
                                    "request": "r", "facts": "f"}
        self.assertIn("no new architecture", filled)
        self.assertNotIn("%(criteria)s", filled)


class TestRungThree(unittest.TestCase):
    """Rung 3. Rung 2 is "skipped entirely if nothing enrolled sits
    above the current model", and the rung after it is the planner. The code
    went straight to needs_human, so a deployment whose implementer has nothing
    stronger enrolled -- which is the shipped registry -- had a ladder that
    stopped one rung early and threw the evidence at a human."""

    PLAN = """# Plan

## Subtasks
```yaml
- id: st-1-alpha
  goal: add alpha
  file_scope: ["alpha.py"]
  acceptance: [AC-1]
```
"""

    def setUp(self):
        self.t = TempRepo()
        with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
            fh.write('fast:\n  - "python3 -c \'raise SystemExit(1)\'"\n'
                     'test_author: never\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "cfg"], self.t.repo)
        self.reg = router.load_registry(REGISTRY)
        # Spend rung 2 up front. This class is about what happens when the
        # ladder has no rung left, so it builds that condition rather than
        # borrowing it from whatever the shipped registry happens to enrol --
        # which is exactly the coupling that made the earlier version of this
        # fixture break the moment implementer gained an escalation target.
        self.reg["models"]["opus-class-strong"]["enrolled_roles"] = [
            r for r in self.reg["models"]["opus-class-strong"]["enrolled_roles"]
            if r != "implementer"]
        self.pol = dict(self.reg["policy"]["limits"],
                        escalation_ceiling=self.reg["policy"]["escalation_ceiling"])
        self.plans = []

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.t.repo, capture_output=True)
        self.t.close()

    def _run(self):
        def planner(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            bundle = os.path.join(td, "escalation.md")
            if os.path.exists(bundle):
                with open(bundle) as fh:
                    self.plans.append(fh.read())
            else:
                self.plans.append("")
            with open(os.path.join(td, "plan.md"), "w") as fh:
                fh.write(self.PLAN)

        def implementer(env, cwd):
            # Writes a file, so this is a real attempt -- the checks are what
            # fail, which is the test_stuck signal rather than an empty diff.
            with open(os.path.join(cwd, "alpha.py"), "w") as fh:
                fh.write("A = 1\n")
            _report(env["AGENT_DELEGATION_TASK_DIR"], "implement-st-1-alpha.json", {
                "stage": "implement", "role": "implementer", "subtask": "st-1-alpha",
                "status": "complete", "summary": "I believe this is right",
                "evidence": {"tests": "ran"}})

        task = store.Task.create(self.t.repo, "T-R3", "# t\n\nAdd alpha\n", self.pol)
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(
            {"planner": planner, "implementer": implementer,
             "test-author": lambda e, c: None}),
            lambda k, t: True, log=logs.append).run()
        return status, task, logs

    def test_the_ladder_reaches_the_planner_before_it_reaches_a_human(self):
        status, task, logs = self._run()
        self.assertEqual(int(task.state["spent"]["replans"]), 1,
                         "rung 3 never fired\n%s" % "\n".join(logs))
        self.assertGreaterEqual(len(self.plans), 2, "the planner was never re-run")

    def test_the_planner_is_handed_the_bundle_not_just_a_status(self):
        self._run()
        bundle = self.plans[-1]
        self.assertIn("test_stuck", bundle)
        self.assertIn("st-1-alpha", bundle)
        self.assertIn("Failing checks", bundle)
        self.assertIn("I believe this is right", bundle,
                      "the implementer's own account was dropped")
        self.assertIn("disposition", bundle.lower(),
                      ": a replan that cannot see finished work re-plans it")

    def test_rung_four_is_reached_by_exhausting_rung_three_not_skipping_it(self):
        status, task, logs = self._run()
        self.assertEqual(status, "needs_human")
        self.assertEqual(int(task.state["spent"]["replans"]),
                         int(self.pol["max_replans"]),
                         "it should stop only once the replan budget is spent")
        self.assertTrue(any("LIMIT" in l or "max_replans" in l for l in logs),
                        "stopping looked like a crash, not a spent budget\n%s"
                        % "\n".join(logs))

    def test_the_bundle_accumulates_rather_than_overwriting(self):
        # max_replans can exceed 1, and the second replan has to see that the
        # first already tried something. Read the file, not what the planner
        # saw: the last rung-3 writes a bundle and then spends the budget, so
        # nobody is dispatched to read it -- it is still the record of why the
        # run stopped.
        _, task, _ = self._run()
        self.assertEqual(task.read_text("escalation.md", "").count("rung 3 after"), 2,
                         "the second bundle overwrote the first")


class TestWaveRaces(unittest.TestCase):
    """Parallel waves shared the state that encodes safety. Each of these is a
    concurrent restatement of an invariant the sequential path already keeps."""

    PLAN = """# Plan

## Subtasks
```yaml
- id: st-1-alpha
  goal: add alpha
  file_scope: ["alpha.py"]
  acceptance: [AC-1]
- id: st-2-beta
  goal: add beta
  file_scope: ["beta.py"]
  hotspots: ["shared/scene.tscn"]
  acceptance: [AC-2]
- id: st-3-gamma
  goal: add gamma
  file_scope: ["gamma.py"]
  hotspots: ["shared/scene.tscn"]
  acceptance: [AC-3]
```
"""

    def setUp(self):
        self.t = TempRepo()
        with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
            fh.write('fast:\n  - "python3 -c \'print(1)\'"\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "cfg"], self.t.repo)
        self.reg = router.load_registry(REGISTRY)
        # A parallel test pins its own concurrency rather than inheriting the
        # registry's: when the registry shipped max_parallel_agents: 1, every
        # race test silently became a sequential run that passed, and a future
        # tuning-down must not do that again.
        #
        # 3, not 2, and it matters: this class plans three independent subtasks,
        # so a cap of 2 runs them two-then-one and never opens the three-way
        # window the wave defect was measured in. The number here is the width
        # of the race this class exists to hunt.
        self.pol = dict(self.reg["policy"]["limits"],
                        escalation_ceiling=self.reg["policy"]["escalation_ceiling"],
                        max_parallel_agents=3)

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.t.repo, capture_output=True)
        self.t.close()

    def _run(self, implementer, task_id="T-W"):
        def planner(env, cwd):
            with open(os.path.join(env["AGENT_DELEGATION_TASK_DIR"], "plan.md"), "w") as fh:
                fh.write(self.PLAN)

        def reviewer(env, cwd):
            with open(os.path.join(env["AGENT_DELEGATION_TASK_DIR"],
                                   "reports", "review-reviewer.json"), "w") as fh:
                json.dump({"stage": "review", "role": "reviewer", "status": "complete",
                           "summary": "ok", "evidence": {"tests": "ok"},
                           "role_data": {"verdict": {"verdict": "APPROVE",
                                                     "ac_table": [{"ac": "AC-1",
                                                                   "status": "met"}],
                                                     "findings": []}}}, fh)

        task = store.Task.create(self.t.repo, task_id, "# t\n\nAdd three things\n",
                                 self.pol)
        script = {"planner": planner, "implementer": implementer,
                  "test-author": lambda e, c: None, "reviewer": reviewer}
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=logs.append).run()
        return status, task, logs

    def test_one_subtask_escalating_stops_the_run_even_as_a_sibling_succeeds(self):
        """The headline race: a sibling's `complete` report overwrote an
        escalation held on a shared attribute, so the escalated subtask was
        marked done off the sibling's green checks.

        The interleave is forced rather than hoped for: the escalating thread
        is held *between* its own _invoke returning and the caller reading the
        report, which is exactly the window the shared attribute exposed."""
        import threading
        sibling_reported = threading.Event()

        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            if "st-1" in _tree_name(cwd):
                with open(os.path.join(cwd, "alpha.py"), "w") as fh:
                    fh.write("A = 1\n")
                _report(td, "implement-st-1-alpha.json", {
                    "stage": "implement", "role": "implementer",
                    "subtask": "st-1-alpha", "status": "complete",
                    "summary": "did alpha", "evidence": {"tests": "ok"}})
            else:
                _report(td, "implement-st-2-beta.json", {
                    "stage": "implement", "role": "implementer",
                    "subtask": "st-2-beta", "status": "escalate",
                    "summary": "No code written — the plan is wrong.",
                    "evidence": {"not_verified": ["everything"]}})

        class Interleaved(Orchestrator):
            # Hooked on _collect_report, not _invoke. _invoke *contains* the
            # report read, so ordering around it let both threads read before
            # either signalled -- the window this test documents was never
            # opened, and it has been passing on the strength of a comment.
            def _collect_report(self, role, subtask, before=None):
                sub = subtask or {}
                if sub.get("id") == "st-2-beta":
                    sibling_reported.wait(10)   # alpha's report lands first
                out = Orchestrator._collect_report(self, role, subtask, before=before)
                if sub.get("id") == "st-1-alpha":
                    sibling_reported.set()      # mine is on disk and read
                return out

        def planner(env, cwd):
            with open(os.path.join(env["AGENT_DELEGATION_TASK_DIR"], "plan.md"), "w") as fh:
                fh.write(self.PLAN)

        task = store.Task.create(self.t.repo, "T-W1", "# t\n\nAdd things\n", self.pol)
        logs = []
        status = Interleaved(task, self.reg, runtime.MockAdapter(
            {"planner": planner, "implementer": implementer,
             "test-author": lambda e, c: None}),
            lambda k, t: True, log=logs.append).run()

        self.assertEqual(status, "needs_human",
                         "an escalating subtask was absorbed by its sibling\n"
                         + "\n".join(logs))
        self.assertIn("reported escalate", "\n".join(logs))
        self.assertFalse(os.path.exists(task.file("integrate.patch")))

    def test_a_sibling_report_named_for_the_role_is_never_read_as_mine(self):
        """The wave defect's symptom, reachable with no timing at all.

        PROTOCOL.md permits `<stage>-<role-or-subtask>.json`, so
        `implement-implementer.json` is a legal name for a subtask's report.
        _collect_report accepted a file on `role in name` alone, and sorted()
        made that file win for *every* sibling — so the escalating subtask read
        a sibling's `complete` and carried on. Nothing here races: it is one
        substring match that cannot tell two concurrent agents apart."""
        import threading
        alpha_landed = threading.Event()

        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            if "st-1" in _tree_name(cwd):
                with open(os.path.join(cwd, "alpha.py"), "w") as fh:
                    fh.write("A = 1\n")
                # Legal under PROTOCOL.md, and it sorts ahead of every sibling's.
                _report(td, "implement-implementer.json", {
                    "stage": "implement", "role": "implementer",
                    "subtask": "st-1-alpha", "status": "complete",
                    "summary": "did alpha", "evidence": {"tests": "ok"}})
                alpha_landed.set()
            elif "st-2" in _tree_name(cwd):
                alpha_landed.wait(10)   # alpha's file is on disk before I report
                # It MUST leave a file behind. An escalating agent normally has
                # checkpointed something, and without that the unrelated
                # "changed no files" guard parks the run for its own reason and
                # hides the misread this test is about.
                with open(os.path.join(cwd, "beta.py"), "w") as fh:
                    fh.write("B = 1\n")
                _report(td, "implement-st-2-beta.json", {
                    "stage": "implement", "role": "implementer",
                    "subtask": "st-2-beta", "status": "escalate",
                    "summary": "the plan contradicts the tree — nothing verified",
                    "evidence": {"not_verified": ["everything"]}})
            else:
                with open(os.path.join(cwd, "gamma.py"), "w") as fh:
                    fh.write("G = 1\n")
                _report(td, "implement-st-3-gamma.json", {
                    "stage": "implement", "role": "implementer",
                    "subtask": "st-3-gamma", "status": "complete",
                    "summary": "did gamma", "evidence": {"tests": "ok"}})

        status, task, logs = self._run(implementer, task_id="T-WNAME")
        self.assertEqual(status, "needs_human",
                         "st-2-beta escalated and the run absorbed it by reading "
                         "a sibling's report\n" + "\n".join(logs))
        self.assertFalse(os.path.exists(task.file("integrate.patch")),
                         "it produced a deliverable from a subtask that escalated")

    def test_counters_from_parallel_subtasks_are_not_lost(self):
        # Lost read-modify-writes on task.json make the attempt and spend caps
        # fail open, which is the opposite of the property they exist for.
        def implementer(env, cwd):
            sub = os.path.basename(cwd.rstrip("/")).split("-", 2)[-1]   # st-N-name
            name = sub.split("-")[-1]
            with open(os.path.join(cwd, "%s.py" % name), "w") as fh:
                fh.write("V = 1\n")
            _report(env["AGENT_DELEGATION_TASK_DIR"], "implement-%s.json" % sub, {
                "stage": "implement", "role": "implementer", "subtask": sub,
                "status": "complete", "summary": "ok", "evidence": {"tests": "ok"}})

        status, task, logs = self._run(implementer, task_id="T-W2")
        attempts = task.state["spent"]["attempts"]
        self.assertEqual(attempts.get("st-1-alpha"), 1, attempts)
        self.assertEqual(attempts.get("st-2-beta"), 1, attempts)
        roles = [h for h in task.state["delegation_history"] if h["role"] == "implementer"]
        self.assertGreaterEqual(len(roles), 2, "a delegation record was lost")

    def test_a_report_for_st_11_is_never_read_as_st_1s(self):
        """The id must fill the whole name after the stage token, not merely
        appear in it. Planners write the ids, and nothing stops a plan from
        naming siblings st-1 and st-11 -- with substring matching, st-1's
        collector claims `implement-st-11.json` the moment it lands first, and
        a sibling's escalate becomes st-1's own."""
        import threading
        eleven_reported = threading.Event()

        PLAN = """# Plan

## Subtasks
```yaml
- id: st-1
  goal: add a
  file_scope: ["a.py"]
  acceptance: [AC-1]
- id: st-11
  goal: add b
  file_scope: ["b.py"]
  acceptance: [AC-2]
```
"""

        def planner(env, cwd):
            with open(os.path.join(env["AGENT_DELEGATION_TASK_DIR"], "plan.md"), "w") as fh:
                fh.write(PLAN)

        def implementer(env, cwd):
            td = env["AGENT_DELEGATION_TASK_DIR"]
            if _tree_name(cwd).endswith("st-11"):
                with open(os.path.join(cwd, "b.py"), "w") as fh:
                    fh.write("B = 1\n")
                _report(td, "implement-st-11.json", {
                    "stage": "implement", "role": "implementer",
                    "subtask": "st-11", "status": "escalate",
                    "summary": "the plan contradicts the tree — nothing verified",
                    "evidence": {"not_verified": ["everything"]}})
                eleven_reported.set()
            else:
                eleven_reported.wait(10)   # st-11's report is on disk first
                with open(os.path.join(cwd, "a.py"), "w") as fh:
                    fh.write("A = 1\n")
                # Deliberately writes no report: green checks plus a changed
                # file is a legal way for an implementer to finish, and any
                # escalate attributed to st-1 can only have been read across.

        task = store.Task.create(self.t.repo, "T-W11", "# t\n\nAdd things\n", self.pol)
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(
            {"planner": planner, "implementer": implementer,
             "test-author": lambda e, c: None}),
            lambda k, t: True, log=logs.append).run()

        joined = "\n".join(logs)
        self.assertEqual(status, "needs_human", joined)
        self.assertIn("st-11: agent reported escalate", joined,
                      "st-11's own escalate was not seen")
        self.assertNotIn("st-1: agent reported escalate", joined,
                         "st-1 was credited with its sibling's report")

    def test_hotspots_from_a_real_plan_force_serialization(self):
        # The old test hand-wrote a `hotspots` key the plan parser dropped, so
        # it proved the scheduler's arithmetic and nothing about the pipeline.
        task = store.Task.create(self.t.repo, "T-W3", "# t\n", self.pol)
        orch = Orchestrator(task, self.reg, runtime.MockAdapter(), lambda k, t: True,
                            log=lambda *_: None)
        task.write_text("plan.md", self.PLAN)
        subs = orch._read_plan_subtasks()
        self.assertEqual(subs[1]["hotspots"], ["shared/scene.tscn"],
                         "the parser drops hotspots, so the guard cannot fire")
        task.update(subtasks=[dict(x, status="pending") for x in subs])
        wave = [t["id"] for t in orch._wave(task.state["subtasks"])]
        self.assertIn("st-1-alpha", wave)
        self.assertNotIn("st-3-gamma", wave,
                         "two subtasks sharing an unmergeable file ran together")

    def test_a_project_declared_hotspot_also_serializes(self):
        with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
            fh.write('fast: []\nhotspots:\n  - "alpha.py"\n')
        task = store.Task.create(self.t.repo, "T-W4", "# t\n", self.pol)
        orch = Orchestrator(task, self.reg, runtime.MockAdapter(), lambda k, t: True,
                            log=lambda *_: None)
        task.update(subtasks=[
            {"id": "st-1", "status": "pending", "planned_scope": ["alpha.py"]},
            {"id": "st-2", "status": "pending", "planned_scope": ["alpha.py", "b.py"]}])
        self.assertEqual(len(orch._wave(task.state["subtasks"])), 1)

    def test_scope_is_measured_against_the_base_not_the_checkpoint(self):
        # base_commit was unset on the wave path whenever the test author did
        # not run, so every diff was taken against a HEAD that already
        # contained the work: nothing ever looked changed.
        with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
            fh.write('fast:\n  - "python3 -c \'print(1)\'"\ntest_author: never\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "no test author"], self.t.repo)

        def implementer(env, cwd):
            sub = os.path.basename(cwd.rstrip("/")).split("-", 2)[-1]   # st-N-name
            name = sub.split("-")[-1]
            with open(os.path.join(cwd, "%s.py" % name), "w") as fh:
                fh.write("V = 1\n")
            _report(env["AGENT_DELEGATION_TASK_DIR"], "implement-%s.json" % sub, {
                "stage": "implement", "role": "implementer", "subtask": sub,
                "status": "complete", "summary": "ok", "evidence": {"tests": "ok"}})

        status, task, logs = self._run(implementer, task_id="T-W5")
        self.assertNotIn("test-author",
                         [h["role"] for h in task.state["delegation_history"]])
        # Per subtask, not the union: with the defect the *wave* members record
        # nothing while a later sequential subtask records everything, so a
        # union assertion passes while the guarantee is broken.
        by_id = {x["id"]: x.get("actual_files") or [] for x in task.state["subtasks"]}
        for sub_id, expected in (("st-1-alpha", "alpha.py"), ("st-2-beta", "beta.py")):
            self.assertIn(expected, by_id.get(sub_id, []),
                          "%s recorded %r — its diff was taken against its own "
                          "checkpoint\n%s" % (sub_id, by_id.get(sub_id), "\n".join(logs)))


class TestRuntimeSurfaces(unittest.TestCase):
    """herdr and Windows paths were designed but never executed."""

    def test_herdr_adapter_shells_out_and_falls_back(self):
        calls = []

        class H(runtime.HerdrAdapter):
            def _cli(self, args, check=True):
                calls.append(args[0])
                return None            # simulate herdr refusing

        h = H(workspace="w1")
        self.assertTrue(hasattr(h, "notify"))
        h.notify("merge", "land it?")
        self.assertIn("notification", calls)

    def test_text_reply_roles_bypass_panes(self):
        # A pane renders on the alternate screen, so reading it back gives TUI
        # chrome instead of the "VERDICT: ..." line the orchestrator parses.
        class H(runtime.HerdrAdapter):
            def _cli(self, args, check=True):
                raise AssertionError("classifier must not open a pane")

            def can_run(self, kind):
                return True        # routing is the subject; no vendor CLI needed
        h = H(workspace="w1")
        s = h.start_agent("classifier", "claude", "/tmp", {})
        self.assertFalse((s.handle or {}).get("herdr"))

    def test_working_roles_do_get_a_pane(self):
        class H(runtime.HerdrAdapter):
            def _cli(self, args, check=True):
                if args[:2] == ["pane", "split"]:
                    return {"result": {"pane": {"pane_id": "w1:p9"}}}
                return {"result": {"ok": True}}
        s = H(workspace="w1").start_agent("implementer", "claude", "/tmp",
                                          {"AGENT_DELEGATION_TASK_DIR": "/x/T-1"})
        self.assertTrue((s.handle or {}).get("herdr"))
        self.assertEqual(s.handle["pane"], "w1:p9")

    def test_the_agent_is_given_its_role_as_the_activation_signal(self):
        class T:
            path = "/x/T-1"
        env = prompts.env_for(T(), "reviewer")
        self.assertEqual(env["AGENT_DELEGATION_ROLE"], "reviewer")
        self.assertEqual(env["AGENT_DELEGATION_TASK_DIR"], "/x/T-1")

    def test_a_variadic_grant_flag_is_passed_once(self):
        a = runtime.LocalAdapter()
        argv = a._argv("claude", {"AGENT_DELEGATION_TASK_DIR": "/x/T-1",
                                  "AGENT_DELEGATION_SKILL_DIR": "/s/ad"})
        self.assertEqual(argv[-3:], ["--add-dir", "/x/T-1", "/s/ad"])

    def test_a_non_variadic_grant_flag_is_repeated_per_path(self):
        # `--add-dir A B` does not error on a CLI that declares `<path>`: B is
        # silently absorbed as a positional prompt argument, so the skill dir
        # becomes the instruction and the real prompt is the one that loses.
        class A(runtime.LocalAdapter):
            GRANTS = dict(runtime.LocalAdapter.GRANTS, cursor="--add-dir")
        argv = A()._argv("cursor", {"AGENT_DELEGATION_TASK_DIR": "/x/T-1",
                                    "AGENT_DELEGATION_SKILL_DIR": "/s/ad"})
        self.assertEqual(argv[-4:], ["--add-dir", "/x/T-1", "--add-dir", "/s/ad"])

    def test_cursor_retries_continue_its_own_session(self):
        # Verified against a real seat: cursor-agent --continue resumes the
        # previous conversation in this directory. Falling back to a fresh turn
        # made every retry re-read the protocol, the task and the repo.
        seen = {}

        class A(runtime.LocalAdapter):
            def prompt(self, session, text, timeout, argv=None):
                seen["argv"] = argv
                return {"settled": "ok"}
        sess = runtime.Session("x", "/tmp", handle={
            "kind": "cursor", "argv": list(runtime.LocalAdapter.LAUNCH["cursor"])})
        A().follow_up(sess, "fix it", timeout=10)
        self.assertEqual(seen["argv"][-1], "--continue")

    def test_a_kind_that_cannot_continue_falls_back_to_a_new_turn(self):
        seen = {}

        class A(runtime.LocalAdapter):
            def prompt(self, session, text, timeout, argv=None):
                seen["argv"] = argv
                return {"settled": "ok"}
        sess = runtime.Session("x", "/tmp", handle={
            "kind": "gemini", "argv": list(runtime.LocalAdapter.LAUNCH["gemini"])})
        A().follow_up(sess, "fix it", timeout=10)
        self.assertIsNone(seen["argv"], "a fresh turn must not carry a resume flag")

    def test_a_refused_prompt_is_reported_not_disguised_as_a_timeout(self):
        class H(runtime.HerdrAdapter):
            def _cli(self, args, check=True):
                self.last_error = "agent_prompt_stalled"
                return None
        h = H(workspace="w1")
        sess = runtime.Session("x", "/tmp", handle={"herdr": True, "role": "implementer"})
        r = h.prompt(sess, "hi", timeout=10)
        self.assertIn("agent_prompt_stalled", r["output"])

    def test_no_panes_restores_cost_accounting(self):
        # Panes are visible but report no cost, so the spend cap cannot bind.
        # The choice has to stay available, not be decided for the user.
        class H(runtime.HerdrAdapter):
            def _cli(self, args, check=True):
                raise AssertionError("panes are off; nothing should be split")

            def can_run(self, kind):
                return True        # routing is the subject; no vendor CLI needed
        h = H(workspace="w1", panes=False)
        s = h.start_agent("implementer", "claude", "/tmp",
                          {"AGENT_DELEGATION_TASK_DIR": "/x/T-1"})
        self.assertFalse((s.handle or {}).get("herdr"))
        self.assertIn("--output-format", " ".join(s.handle["argv"]),
                      "the subprocess path is what reports total_cost_usd")

    def test_a_missing_cli_is_refused_before_a_session_exists(self):
        # The PATH guard runs through can_run so tests can stub it. Stubbing the
        # seam must not be able to disarm the guard for a real run.
        class Nothing(runtime.LocalAdapter):
            def can_run(self, kind):
                return False

        with self.assertRaises(runtime.RuntimeError_) as cm:
            Nothing().start_agent("implementer", "claude", "/tmp", {})
        self.assertIn("not found on PATH", str(cm.exception))

        with self.assertRaises(runtime.RuntimeError_) as cm:
            runtime.LocalAdapter().start_agent("implementer", "nope", "/tmp", {})
        self.assertIn("unknown agent kind", str(cm.exception))

    def test_herdr_availability_needs_both_env_and_binary(self):
        prev = os.environ.get("HERDR_ENV")
        os.environ.pop("HERDR_ENV", None)
        try:
            self.assertFalse(runtime.HerdrAdapter.available())
        finally:
            if prev is not None:
                os.environ["HERDR_ENV"] = prev

    def test_get_falls_back_to_local_when_herdr_absent(self):
        prev = os.environ.get("HERDR_ENV")
        os.environ.pop("HERDR_ENV", None)
        try:
            self.assertIsInstance(runtime.get("herdr"), runtime.LocalAdapter)
        finally:
            if prev is not None:
                os.environ["HERDR_ENV"] = prev

    def test_case_insensitive_filesystems_treat_one_file_as_one_file(self):
        # `... or sys.platform.startswith("linux")` stood here, which is not an
        # assertion on Linux at all: `scopes_overlap` could return False
        # unconditionally and pass. Two functions decide the same case rule from
        # the same platform predicate, and what must hold on EVERY platform is
        # that they agree: `_wave` asks scopes_overlap whether two agents may
        # run at once, and scope_violations rules afterwards on whether a file
        # was theirs. Disagree, and one file was handed to two agents.
        one_file = not verify.scope_violations(["src/Foo.cs"], ["src/foo.cs"])
        self.assertEqual(verify.scopes_overlap(["src/Foo.cs"], ["src/foo.cs"]), one_file,
                         "the two case rules have drifted apart on this platform")
        self.assertEqual(
            verify.scope_violations(["src/Foo.cs"], ["src/foo.cs"], case_insensitive=True), [])
        self.assertEqual(
            verify.scope_violations(["src/Foo.cs"], ["src/foo.cs"], case_insensitive=False),
            ["src/Foo.cs"])

    def test_generated_output_is_never_an_authored_change(self):
        for p in ("__pycache__/x.pyc", "Library/artifacts/db", "Intermediate/Build/a.h",
                  ".godot/imported/x", "obj/Debug/a.o"):
            self.assertTrue(verify.is_ignored(p), p)
        self.assertFalse(verify.is_ignored("src/combat.gd"))


class TestLiveRunRegressions(unittest.TestCase):
    """Three bugs a live run found that no scripted test had reached."""

    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)
        self.pol = dict(self.reg["policy"]["limits"],
                        escalation_ceiling=self.reg["policy"]["escalation_ceiling"])
        # checks that are green before anything is implemented
        with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
            fh.write('fast:\n  - "python3 -c \'print(1)\'"\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "cfg"], self.t.repo)

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.t.repo, capture_output=True)
        self.t.close()

    def _simple(self, script):
        task = store.Task.create(self.t.repo, "T-R", "# t\n\nAdd a thing\n", self.pol)

        class A(runtime.MockAdapter):
            def prompt(self, session, text, timeout):
                if session.handle["role"] == "classifier":
                    return {"settled": "idle", "output": "VERDICT: SIMPLE -- tiny", "code": 0}
                return runtime.MockAdapter.prompt(self, session, text, timeout)

        logs = []
        status = Orchestrator(task, self.reg, A(script), lambda k, t: True,
                              log=logs.append).run()
        return status, task, logs

    def test_an_agent_that_writes_nothing_is_not_a_success(self):
        # The fast checks were green before the agent ran, so passing them
        # proves nothing about whether it did the work.
        def wrote_nothing(env, cwd):
            _report(env["AGENT_DELEGATION_TASK_DIR"], "implement-st-1-main.json", {
                "stage": "implement", "role": "implementer", "status": "complete",
                "summary": "all good", "evidence": {"tests": "green"}})
        status, task, logs = self._simple({"implementer": wrote_nothing})
        self.assertEqual(status, "needs_human", "\n".join(logs))
        self.assertIn("changed no files", "\n".join(logs))

    def test_an_agents_own_escalate_outranks_green_checks(self):
        # The live failure: the agent said "no code written", the orchestrator
        # saw green tests and marked the subtask complete.
        def honest(env, cwd):
            _report(env["AGENT_DELEGATION_TASK_DIR"], "implement-st-1-main.json", {
                "stage": "implement", "role": "implementer", "status": "escalate",
                "summary": "No code written — wrong repository in the worktree.",
                "evidence": {"tests": "green but irrelevant"}})
        status, task, logs = self._simple({"implementer": honest})
        self.assertEqual(status, "needs_human")
        self.assertIn("reported escalate", "\n".join(logs))

    def test_worktrees_of_different_projects_do_not_collide(self):
        # Two repos sharing a parent produced the same .adg-worktrees/T-001-work.
        other = os.path.join(self.t.dir, "other")
        os.makedirs(other)
        sh(["git", "init", "-q", "-b", "main"], other)
        sh(["git", "config", "user.email", "t@e.com"], other)
        sh(["git", "config", "user.name", "T"], other)
        open(os.path.join(other, "f.txt"), "w").close()
        sh(["git", "add", "-A"], other)
        sh(["git", "commit", "-qm", "i"], other)
        self.assertNotEqual(store.project_key(self.t.repo), store.project_key(other))

    def test_reusing_a_foreign_worktree_is_refused(self):
        other = os.path.join(self.t.dir, "other")
        os.makedirs(other)
        sh(["git", "init", "-q", "-b", "main"], other)
        sh(["git", "config", "user.email", "t@e.com"], other)
        sh(["git", "config", "user.name", "T"], other)
        open(os.path.join(other, "f.txt"), "w").close()
        sh(["git", "add", "-A"], other)
        sh(["git", "commit", "-qm", "i"], other)
        stolen = os.path.join(self.t.dir, "wt-of-other")
        sh(["git", "worktree", "add", "-q", "-b", "x", stolen], other)
        a = runtime.LocalAdapter()
        with self.assertRaises(runtime.RuntimeError_) as cm:
            a.create_worktree(self.t.repo, "adg/T/work", "HEAD", stolen)
        self.assertIn("different repository", str(cm.exception))


class TestIntakeAndBrainstorm(unittest.TestCase):
    """Acceptance criteria for every task, and a design step for complex ones."""

    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)
        self.pol = dict(self.reg["policy"]["limits"],
                        escalation_ceiling=self.reg["policy"]["escalation_ceiling"])

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.t.repo, capture_output=True)
        self.t.close()

    def _adapter(self, verdict="SIMPLE -- tiny", criteria=True, spec=None, script=None):
        crit = ("## Acceptance criteria\n\n- **AC-1** — subtract works\n"
                "- **AC-2** — old callers still work\n") if criteria else "nothing useful"

        class A(runtime.MockAdapter):
            def prompt(inner, session, text, timeout):
                role = session.handle["role"]
                if role == "intake":
                    return {"settled": "idle", "output": crit, "code": 0}
                if role == "classifier":
                    return {"settled": "idle", "output": "VERDICT: %s" % verdict, "code": 0}
                if role == "planner" and spec is not None:
                    with open(os.path.join(session.handle["env"]["AGENT_DELEGATION_TASK_DIR"],
                                           "spec.md"), "w") as fh:
                        fh.write(spec)
                return runtime.MockAdapter.prompt(inner, session, text, timeout)

        return A(script or {})

    def _task(self, request="Add subtract", mode="attended"):
        return store.Task.create(self.t.repo, "T-I", "# t\n\n%s\n" % request,
                                 self.pol, mode=mode)

    def test_every_task_gets_acceptance_criteria(self):
        task = self._task()
        orch = Orchestrator(task, self.reg, self._adapter(), lambda k, t: True,
                            log=lambda *_: None)
        orch._stage_intake()
        self.assertIn("**AC-1**", task.read_text("task.md"))

    def test_criteria_already_written_by_a_human_are_left_alone(self):
        task = store.Task.create(self.t.repo, "T-I2",
                                 "# t\n\n- **AC-1** — mine\n", self.pol)
        called = []

        class A(runtime.MockAdapter):
            def prompt(inner, session, text, timeout):
                called.append(session.handle["role"])
                return runtime.MockAdapter.prompt(inner, session, text, timeout)

        Orchestrator(task, self.reg, A(), lambda k, t: True,
                     log=lambda *_: None)._stage_intake()
        self.assertNotIn("intake", called, "overwrote human-written criteria")

    def test_unusable_criteria_do_not_corrupt_the_task(self):
        task = self._task()
        before = task.read_text("task.md")
        Orchestrator(task, self.reg, self._adapter(criteria=False), lambda k, t: True,
                     log=lambda *_: None)._stage_intake()
        self.assertEqual(task.read_text("task.md"), before)

    def test_complex_attended_tasks_brainstorm_first(self):
        task = self._task("Migrate the save format")
        orch = Orchestrator(task, self.reg,
                            self._adapter(verdict="COMPLEX -- shared format"),
                            lambda k, t: True, log=lambda *_: None)
        orch._stage_classify()
        self.assertEqual(task.state["status"], "brainstorm")

    def test_autonomous_runs_skip_the_dialogue(self):
        # Nobody is there to answer, so a design conversation is theatre.
        task = self._task("Migrate the save format", mode="autonomous")
        orch = Orchestrator(task, self.reg,
                            self._adapter(verdict="COMPLEX -- shared format"),
                            lambda k, t: True, log=lambda *_: None)
        orch._stage_classify()
        self.assertEqual(task.state["status"], "plan")

    def test_simple_tasks_never_brainstorm(self):
        task = self._task()
        orch = Orchestrator(task, self.reg, self._adapter(), lambda k, t: True,
                            log=lambda *_: None)
        orch._stage_classify()
        self.assertEqual(task.state["status"], "implement")

    def test_the_design_is_gated_and_reaches_the_human(self):
        task = self._task("Migrate the save format")
        seen = {}
        orch = Orchestrator(
            task, self.reg,
            self._adapter(verdict="COMPLEX", spec="# Design\n\nUse a version field.\n"),
            lambda k, t: seen.setdefault(k, t) and True, log=lambda *_: None)
        orch._stage_brainstorm()
        self.assertIn("design", seen)
        self.assertIn("version field", seen["design"])
        self.assertEqual(task.state["status"], "plan")

    def test_a_design_that_never_appeared_does_not_block_planning(self):
        task = self._task("Migrate the save format")
        orch = Orchestrator(task, self.reg, self._adapter(verdict="COMPLEX"),
                            lambda k, t: True, log=lambda *_: None)
        orch._stage_brainstorm()
        self.assertEqual(task.state["status"], "plan")

    def test_the_planner_is_told_to_plan_against_the_design(self):
        task = self._task("Migrate the save format")
        task.write_text("spec.md", "# Design\n\nUse a version field.\n")
        prompts_seen = []

        class A(runtime.MockAdapter):
            def prompt(inner, session, text, timeout):
                if session.handle["role"] == "planner":
                    prompts_seen.append(text)
                return runtime.MockAdapter.prompt(inner, session, text, timeout)

        orch = Orchestrator(task, self.reg, A(), lambda k, t: True, log=lambda *_: None)
        try:
            orch._stage_plan()
        except Halt:
            pass                      # no subtasks written by the mock; fine
        self.assertTrue(prompts_seen)
        self.assertIn("approved design", prompts_seen[0])
        self.assertIn("rather than redesigning", prompts_seen[0])


class TestResume(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)

    def tearDown(self):
        self.t.close()

    def test_explicit_stage_is_honoured_mid_flight(self):
        # A run killed during `plan` keeps status "plan". Gating the override on
        # "parked" dropped --stage exactly when it mattered and re-ran the very
        # stage the user was skipping past.
        from adg import cli
        task = store.Task.create(self.t.repo, "T-RS", "# t\n",
                                 dict(self.reg["policy"]["limits"]))
        task.update(status="plan")

        class Args:
            repo, registry, id, stage = self.t.repo, REGISTRY, "T-RS", "implement"
            adapter, dry_run, yes = "mock", True, True

        try:
            cli.cmd_resume(Args())
        except SystemExit:
            pass
        self.assertNotEqual(store.Task.open(self.t.repo, "T-RS").state["status"], "plan")

    def test_every_advertised_stage_has_a_handler(self):
        """STAGES is what `--stage` is validated against, so an entry with no
        `_stage_` method is a stage the CLI accepts and the run loop then parks
        on -- the exact outcome that validation was added to prevent. "verify"
        sat here for that reason: checks run inside implement and review, never
        as a stage of their own."""
        from adg.machine import STAGES, TERMINAL
        missing = [s for s in STAGES
                   if s not in TERMINAL and not hasattr(Orchestrator, "_stage_" + s)]
        self.assertEqual(missing, [])

    def test_a_misspelled_stage_is_refused_before_it_is_written(self):
        # --stage was written to status verbatim, and an unknown status has no
        # handler -- so a typo persisted first and parked the task with "no
        # handler for stage", which reads as a corrupted task rather than a
        # typo the user can retype.
        from adg import cli
        task = store.Task.create(self.t.repo, "T-RT", "# t\n",
                                 dict(self.reg["policy"]["limits"]))
        task.update(status="plan")

        class Args:
            repo, registry, id, stage = self.t.repo, REGISTRY, "T-RT", "implememt"
            adapter, dry_run, yes = "mock", True, True

        with self.assertRaises(SystemExit) as cm:
            cli.cmd_resume(Args())
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(store.Task.open(self.t.repo, "T-RT").state["status"], "plan",
                         "the bad stage was written before it was checked")


class TestStatusReadsTheBreaker(unittest.TestCase):
    """`park` records why a run stopped; the breaker file records whether the
    seat is still out. `_quota_guard` was taught the difference and `status`
    was not, so a task whose window had reopened — or whose seat had been
    cleared by hand — still reported "waiting on quota" beside a reopen time
    in the past, for a task `resume` would have run immediately."""

    def setUp(self):
        self.t = TempRepo()
        self.task = store.Task.create(self.t.repo, "T-Q", "# t\n",
                                      {"max_cost_usd": 1})
        self.task.update(status="needs_human",
                         park={"reason": "quota_all_exhausted", "role": "implementer",
                               "channels": ["claude-seat"],
                               "reopen_at": time.time() + 3600})

    def tearDown(self):
        self.t.close()

    def _status(self):
        import io
        import contextlib

        class Args:
            repo, registry = self.t.repo, REGISTRY

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_status(Args())
        return buf.getvalue()

    def test_an_open_breaker_still_reads_as_waiting(self):
        cooldown.open_breaker("claude-seat", "quota", time.time() + 3600, time.time())
        self.assertIn("waiting on quota", self._status())
        self.assertIn("reopens at", self._status())

    def test_a_cleared_seat_stops_reading_as_waiting(self):
        cooldown.open_breaker("claude-seat", "quota", time.time() + 3600, time.time())
        cooldown.clear("claude-seat")
        out = self._status()
        self.assertNotIn("waiting on quota", out,
                         "status reported a quota wall the breaker no longer has")
        self.assertIn("needs_human", out)

    def test_an_expired_window_stops_reading_as_waiting(self):
        # Nothing cleared it; the window simply came back. Same stale snapshot,
        # and the one a user hits by waiting rather than intervening.
        cooldown.open_breaker("claude-seat", "quota", time.time() - 1, time.time() - 2)
        self.assertNotIn("waiting on quota", self._status())


class TestCostBreakdown(unittest.TestCase):
    """The brief should say which model on which provider did what, and never
    fold an unreported cost into a total as zero."""

    STATE = {"id": "T-1", "spent": {"usd": 1.05}, "limits": {"max_cost_usd": 15},
             "delegation_history": [
                 {"role": "classifier", "model": "fast-cheap", "channel": "cursor-seat",
                  "adapter": "local", "usd": None},
                 {"role": "planner", "model": "opus-class-strong",
                  "channel": "claude-seat", "adapter": "local", "usd": 1.05},
                 {"role": "implementer", "model": "balanced-coder",
                  "channel": "claude-seat", "adapter": "herdr", "usd": None}]}

    def test_every_model_and_provider_is_named(self):
        out = "\n".join(brief._cost_section(self.STATE))
        for token in ("fast-cheap", "cursor-seat", "opus-class-strong",
                      "claude-seat", "balanced-coder"):
            self.assertIn(token, out, token)
        self.assertIn("classifier", out)
        self.assertIn("planner", out)

    def test_a_pane_run_says_it_cannot_be_measured(self):
        # Structural, not a hiccup: the user chose a mode that cannot bill.
        out = "\n".join(brief._cost_section(self.STATE))
        self.assertIn("cannot be measured in a pane", out)
        self.assertIn("cannot be billed", out)
        self.assertIn("--no-panes", out)
        self.assertNotIn("$0.00", out)

    def test_a_silent_subprocess_is_called_unexpected_instead(self):
        out = "\n".join(brief._cost_section(self.STATE))
        self.assertIn("which is unexpected", out)

    def test_pane_only_run_makes_no_unexpected_claim(self):
        state = dict(self.STATE, delegation_history=[
            {"role": "implementer", "model": "balanced-coder", "channel": "claude-seat",
             "adapter": "herdr", "usd": None}])
        out = "\n".join(brief._cost_section(state))
        self.assertIn("cannot be billed", out)
        self.assertNotIn("unexpected", out)

    def test_estimated_cost_is_labelled_as_estimated(self):
        state = dict(self.STATE, delegation_history=[
            {"role": "classifier", "model": "fast-cheap", "channel": "cursor-seat",
             "adapter": "local", "usd": 0.02, "usd_estimated": True}])
        out = "\n".join(brief._cost_section(state))
        self.assertIn("estimated from tokens", out)

    def test_a_fully_priced_run_makes_no_excuses(self):
        state = dict(self.STATE, delegation_history=[
            {"role": "planner", "model": "opus-class-strong", "channel": "claude-seat",
             "adapter": "local", "usd": 1.05}])
        out = "\n".join(brief._cost_section(state))
        self.assertNotIn("not reported", out)
        self.assertNotIn("the real total is higher", out)

    def test_the_breakdown_survives_the_jargon_lint(self):
        self.assertEqual(brief.lint("\n".join(brief._cost_section(self.STATE))), [])


class TestSkillContract(unittest.TestCase):
    """The systemic gap: schemas and role cards were written in separate passes
    and never checked against each other. A reviewer literally could not write a
    file that satisfied both, and nothing caught it because every fixture in
    this suite was hand-written to be valid."""

    # The dispatched-agent protocol, which moved under orchestrator/ when the
    # repo-root `agent-delegation/` became the front-door skill for the
    # user's own agent. Two audiences, two directories.
    SKILL = os.path.join(REPO_ROOT, "orchestrator", "workflows", "default")

    def _card(self, name):
        with open(os.path.join(self.SKILL, "roles", "%s.md" % name)) as fh:
            return fh.read()

    def test_a_reviewer_report_validates_as_a_report(self):
        report = {
            "stage": "review", "role": "reviewer", "status": "complete",
            "summary": "checked the criteria", "evidence": {"tests": "214 passed"},
            "role_data": {"verdict": {
                "verdict": "APPROVE",
                "ac_table": [{"ac": "AC-1", "status": "met", "evidence": "t.py:9"}],
                "findings": []}},
        }
        schema.validate_report(report)
        schema.validate_verdict(report["role_data"]["verdict"])

    def test_the_reviewer_can_report_blocked(self):
        # It had no legal way to say "I could not review this".
        schema.validate_report({
            "stage": "review", "role": "reviewer", "status": "blocked",
            "summary": "no diff was provided", "evidence": {"not_verified": ["everything"]}})

    def test_every_field_a_card_names_is_legal(self):
        # Fields a role card tells an agent to write must exist in the schema or
        # be explicitly routed to role_data.
        report = json.loads(_slurp(os.path.join(self.SKILL, "schemas",
                                            "report.schema.json")))
        allowed = set(report["properties"])
        for role, named in [("planner", ["subtask_ids", "estimated_total_loc"]),
                            ("test-author", ["ac_coverage"])]:
            card = self._card(role)
            for field in named:
                self.assertIn(field, card, "%s no longer names %s" % (role, field))
                self.assertNotIn(field, allowed, "schema gained %s; update the card" % field)
                near = card[max(0, card.find(field) - 220):card.find(field) + 40]
                self.assertIn("role_data", near,
                              "%s tells the agent to write %s without routing it to "
                              "role_data" % (role, field))

    def test_cards_do_not_point_at_the_verdict_schema_as_a_file(self):
        self.assertNotIn("per `schemas/verdict.schema.json`", self._card("reviewer"))

    def test_namespaced_deviation_ids_are_accepted_and_bare_ones_are_not(self):
        base = {"stage": "implement", "role": "implementer", "status": "complete",
                "summary": "s", "evidence": {"tests": "ok"}}
        schema.validate_report(dict(base, deviations=["dev-st-2-1"]))
        with self.assertRaises(schema.Invalid):
            schema.validate_report(dict(base, deviations=["dev-3"]))

    def test_empty_evidence_is_rejected(self):
        with self.assertRaises(schema.Invalid):
            schema.validate_report({"stage": "implement", "role": "implementer",
                                    "status": "complete", "summary": "s", "evidence": {}})

    def test_out_of_scope_severity_is_stated_once_and_agrees(self):
        ref = _slurp(os.path.join(self.SKILL, "references", "deviations.md"))
        impl = self._card("implementer")
        self.assertIn("One exception:", ref, "the carve-out belongs with the rule")
        self.assertNotIn("Small mechanical exceptions", impl,
                         "implementer.md restates the severity rule differently again")

    def test_task_md_authority_matches_what_the_planner_is_told(self):
        skill = _slurp(os.path.join(self.SKILL, "PROTOCOL.md"))
        self.assertNotIn("amended only by humans", skill)
        self.assertIn("planner may add criteria", skill)
        self.assertIn("missing entirely", self._card("planner"))

    def test_the_protocol_does_not_advertise_itself_as_an_installable_skill(self):
        """It is data the orchestrator reads, handed to an agent by absolute
        path, so it must not carry a skill identity.

        This replaces a test that asserted the frontmatter description triggered
        on `$AGENT_DELEGATION_ROLE` rather than on a path. That property is not
        gone -- it moved. With no frontmatter there is no loader trigger at all,
        which is the stronger form of the same guarantee, and what still
        enlists an agent is `env_for` withholding the role variable (see
        TestRoleMandate). What this guards now is the collision that replaced
        it: two files in one repo both declaring `name: agent-delegation`, with
        a loader free to pick either.
        """
        text = _slurp(os.path.join(self.SKILL, "PROTOCOL.md"))
        self.assertFalse(text.startswith("---"),
                         "the protocol still declares skill frontmatter")
        self.assertNotIn("\nname: agent-delegation", text)
        root_skill = _slurp(os.path.join(REPO_ROOT, "agent-delegation", "SKILL.md"))
        self.assertTrue(root_skill.startswith("---"),
                        "the front-door skill lost the frontmatter it needs")

    def test_the_skill_cites_nothing_outside_itself(self):
        """The protocol travels: an agent reads it from whatever machine the
        orchestrator put it on, so a citation of DESIGN.md or the repo root is a
        pointer that dangles wherever it is unpacked -- and it is invisible from
        inside this repo, where the file it names is always there."""
        for base, _dirs, names in os.walk(self.SKILL):
            for name in names:
                if not name.endswith((".md", ".json")):
                    continue
                path = os.path.join(base, name)
                body = _slurp(path)
                rel = os.path.relpath(path, self.SKILL)
                for outsider in ("DESIGN.md", "orchestrator/", "registry.default.yaml"):
                    self.assertNotIn(outsider, body,
                                     "%s cites %s, which is not shipped with the "
                                     "skill" % (rel, outsider))

    def test_the_integrator_names_its_report_by_subtask_not_by_role(self):
        """`_reconcile` passes a subtask, so `_collect_report` identifies the
        report by that id alone -- the wave-attribution rule. A card that says
        `integrate-integrator.json` describes a file the orchestrator can never
        credit, and a conflicting wave calls two integrators anyway."""
        card = self._card("integrator")
        self.assertIn("reports/integrate-<subtask-id>.json", card)
        self.assertIn("Not `integrate-integrator.json`", card)

    def test_a_card_named_integrator_report_is_actually_collected(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "reports"))
            task = store.Task(d)
            task.write_json("task.json", {"id": "T-1"})
            sub = {"id": "st-2-beta"}
            _report(d, "integrate-%s.json" % sub["id"], {
                "stage": "integrate", "role": "integrator", "status": "complete",
                "summary": "kept st-4's registration", "evidence": {"tests": "3 passed"}})
            orch = Orchestrator.__new__(Orchestrator)
            orch.task = task
            report, problems = orch._collect_report("integrator", sub)
            self.assertIsNone(problems)
            self.assertEqual(report["status"], "complete")
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestRoleMandate(unittest.TestCase):
    """`AGENT_DELEGATION_ROLE` is what enlists an agent into this protocol. Every
    role that carries it must have somewhere to go when the agent obeys."""

    # The dispatched-agent protocol, which moved under orchestrator/ when the
    # repo-root `agent-delegation/` became the front-door skill for the
    # user's own agent. Two audiences, two directories.
    SKILL = os.path.join(REPO_ROOT, "orchestrator", "workflows", "default")

    def _task(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        task = store.Task(d)
        task.write_json("task.json", {"id": "T-1", "limits": {}})
        return task

    def test_text_reply_roles_are_not_conscripted(self):
        task = self._task()
        for role in sorted(prompts.TEXT_REPLY_ROLES):
            env = prompts.env_for(task, role)
            self.assertNotIn("AGENT_DELEGATION_ROLE", env,
                             "%s has no card, no task id and no report to write; "
                             "telling it the protocol applies makes it answer with "
                             "a blocked report instead of the verdict" % role)
            # The location still travels: that is the whole point of the split.
            self.assertEqual(env["AGENT_DELEGATION_TASK_DIR"], task.path)

    def test_every_conscripted_role_has_a_card(self):
        task = self._task()
        for role in ("planner", "implementer", "test-author", "reviewer", "integrator"):
            env = prompts.env_for(task, role)
            self.assertEqual(env["AGENT_DELEGATION_ROLE"], role)
            self.assertTrue(os.path.isfile(
                os.path.join(self.SKILL, "roles", "%s.md" % role)),
                "%s is given the mandate with no card to read" % role)

    def test_runtime_does_not_keep_a_second_copy_of_the_list(self):
        self.assertIs(runtime.HerdrAdapter.TEXT_REPLY_ROLES, prompts.TEXT_REPLY_ROLES)

    def test_the_attempt_budget_is_not_quoted_to_roles_that_have_none(self):
        task = self._task()
        task.write_json("task.json", {"id": "T-1",
                                      "limits": {"max_attempts_per_subtask": 8}})
        self.assertNotIn("attempts on this subtask",
                         prompts.compose("reviewer", task))
        self.assertIn("attempts on this subtask",
                      prompts.compose("implementer", task,
                                      subtask={"id": "st-1", "file_scope": ["src/**"]}))

    def test_the_scope_line_promises_only_what_happens(self):
        task = self._task()
        text = prompts.compose("implementer", task,
                               subtask={"id": "st-1", "file_scope": ["src/**"]})
        self.assertNotIn("reverted", text,
                         "nothing in this program reverts an out-of-scope hunk; "
                         "it records the files and forces a review")
        self.assertIn("recorded and sent to a reviewer", text)


class TestReviewInputs(unittest.TestCase):
    """`roles/reviewer.md` step 1 calls them "the diff, and the verify output
    your prompt provides", and step 6 authorises a `blocked` report when they
    never arrive. The prompt named neither, so a card-compliant reviewer could
    halt the run over an input the orchestrator was holding all along."""

    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)

    def tearDown(self):
        self.t.close()

    def test_the_reviewer_is_given_the_diff_and_the_verify_output(self):
        _spit(os.path.join(self.t.repo, ".adg.yaml"), 'fast:\n  - "true"\n')
        task = store.Task.create(self.t.repo, "T-RV", "# t\n\n- **AC-1** — works\n",
                                 dict(self.reg["policy"]["limits"]))
        task.update(status="review", classification={"tier": "complex"},
                    subtasks=[{"id": "st-1-main", "status": "complete",
                               "actual_files": ["app.py"], "planned_scope": ["**"]}])
        orch = Orchestrator(task, self.reg, runtime.MockAdapter(),
                            lambda k, t: True, log=lambda *_: None)
        with self.assertRaises(Halt):
            # The mock writes no verdict, so review halts -- but the prompt was
            # composed and logged on the way, which is what this reads back.
            orch._stage_review()
        text = "".join(_slurp(task.file("agent-logs", n))
                       for n in os.listdir(task.file("agent-logs"))
                       if n.startswith("reviewer-"))
        self.assertIn("git diff %s" % task.state["repo"]["base_commit"], text,
                      "the reviewer was never told what to diff against")
        self.assertIn(os.path.join(task.path, "verify"), text,
                      "the reviewer was never pointed at the verify output")
        # Where the schemas it is told to match come from is pinned by
        # TestWorkflowManifest instead: under the bundled workflow the right
        # path and the wrong one are the same string, so asserting it here
        # would pass with the defect still in place.


class TestCapabilityHint(unittest.TestCase):
    """subtask.schema.json has always said a hint "raises the router's
    requirements for this subtask". Nothing read it, so a subtask marked
    very_high drew the same model as a one-line rename."""

    def setUp(self):
        self.reg = router.load_registry(REGISTRY)

    def test_a_hint_becomes_a_numeric_floor(self):
        self.assertEqual(router.as_boost({"reasoning": "high", "coding": "very_high"}),
                         {"reasoning": 4, "coding": 5})

    def test_a_typo_is_dropped_rather_than_guessed_at(self):
        self.assertEqual(router.as_boost({"reasoning": "extremely_high"}), {})
        self.assertEqual(router.as_boost(None), {})

    def test_the_hint_raises_the_floor(self):
        r = router.Router(self.reg)
        plain = r.candidates("implementer")[0]
        strong = r.candidates("implementer", boost=router.as_boost({"reasoning": "very_high"}))
        self.assertEqual(plain.model, "balanced-coder")
        self.assertTrue(strong, "reasoning 5 is enrolled for implementer")
        for c in strong:
            self.assertGreaterEqual(c.spec["reasoning"], 5)

    def test_an_unreachable_hint_degrades_instead_of_parking_the_task(self):
        t = TempRepo()
        self.addCleanup(t.close)
        task = store.Task.create(t.repo, "T-CH", "# t\n",
                                 dict(self.reg["policy"]["limits"]))
        orch = Orchestrator(task, self.reg, runtime.MockAdapter(),
                            lambda k, txt: True, log=lambda *_: None)
        # ctx 10**9 is above every enrolled model: a floor nothing can clear.
        choice = orch._pick_implementer({"id": "st-1", "capability_hint": {"ctx": 10 ** 9}})
        self.assertEqual(choice.model, "balanced-coder")


class TestRoundVersioning(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)

    def tearDown(self):
        self.t.close()

    def test_a_second_round_does_not_destroy_the_first(self):
        task = store.Task.create(self.t.repo, "T-A", "# t\n",
                                 dict(self.reg["policy"]["limits"]))
        _report(task.path, "review-reviewer.json", {"round": 1})
        orch = Orchestrator(task, self.reg, runtime.MockAdapter(), lambda k, t: True,
                            log=lambda *_: None)
        orch._archive_reports("review-")
        _report(task.path, "review-reviewer.json", {"round": 2})
        orch._archive_reports("review-")
        archived = sorted(os.listdir(task.file("reports", "archive")))
        self.assertEqual(archived, ["review-reviewer-r1.json", "review-reviewer-r2.json"])
        rounds = [json.loads(_slurp(task.file("reports", "archive", a)))["round"]
                  for a in archived]
        self.assertEqual(rounds, [1, 2], "an earlier round was overwritten")


class TestWinnow(unittest.TestCase):
    """code-winnow is referenced, never vendored: a stale copy that reports
    nothing looks exactly like a clean scan."""

    def test_absent_scanner_degrades_loudly_not_silently(self):
        import tempfile
        empty = tempfile.mkdtemp()          # a machine with no skills installed
        prev = os.environ.get("HOME")
        os.environ["HOME"] = empty
        try:
            self.assertIsNone(winnow.find(empty, configured="/nope/scan.py"))
        finally:
            if prev is not None:
                os.environ["HOME"] = prev
            shutil.rmtree(empty, ignore_errors=True)

    def test_explicit_path_and_env_override_win(self):
        import tempfile
        d = tempfile.mkdtemp()
        fake = os.path.join(d, "scan.py")
        open(fake, "w").close()
        self.assertEqual(winnow.find("/nonexistent", configured=fake), fake)
        os.environ[winnow.ENV_OVERRIDE] = fake
        try:
            self.assertEqual(winnow.find("/nonexistent"), fake)
        finally:
            del os.environ[winnow.ENV_OVERRIDE]
        shutil.rmtree(d, ignore_errors=True)

    def test_a_plugin_install_is_found_rather_than_reported_missing(self):
        """code-winnow can be delivered as a plugin, which nests the same
        `skills/<name>/` tail under a marketplace and a plugin directory.
        `companions.py` already walked those roots; this module was written
        against the flat layout and answered "not installed"."""
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        nested = os.path.join(home, ".claude", "plugins", "cache", "some-market",
                              "winnow-plugin", "skills", "code-winnow", "scripts")
        os.makedirs(nested)
        open(os.path.join(nested, "scan.py"), "w").close()
        prev = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            found = winnow.find(tempfile.mkdtemp())
        finally:
            if prev is not None:
                os.environ["HOME"] = prev
        self.assertEqual(found, os.path.join(nested, "scan.py"))

    def test_files_in_scope_but_none_opened_is_not_a_clean_scan(self):
        """scan.py answers this with files>0, scanned_files=0 and complete:false.
        Summarised as a run that found nothing, the whole fact lived in a caveat
        under a zero -- and a zero is what a clean branch looks like."""
        out = winnow.summarize({"files": 4, "scanned_files": 0, "findings": [],
                                "complete": False, "errors": [{"path": "a.min.js"}]},
                               "v-1")
        self.assertFalse(out["ran"])
        self.assertIn("nothing was scanned", out["why"])
        self.assertIn("did not run", winnow.as_text(out))

    def test_a_configured_path_that_is_wrong_says_so(self):
        # Otherwise a typo in winnow_scan is indistinguishable from a machine
        # that never installed code-winnow.
        why = winnow.misconfigured("/nope/scan.py")
        self.assertIn("winnow_scan", why)
        self.assertIn("/nope/scan.py", why)

        os.environ[winnow.ENV_OVERRIDE] = "/also/nope.py"
        try:
            self.assertIn(winnow.ENV_OVERRIDE, winnow.misconfigured())
        finally:
            del os.environ[winnow.ENV_OVERRIDE]

        self.assertIsNone(winnow.misconfigured(), "unset is not misconfigured")

    def test_summary_keeps_only_significant_findings(self):
        s = winnow.summarize({"findings": [
            {"severity": "P1", "path": "a.py", "line": 3, "message": "except/pass"},
            {"severity": "P3", "path": "b.py", "line": 9, "message": "em dash"},
            {"severity": "P2", "path": "a.py", "line": 1, "message": "no assertion"},
        ]})
        self.assertEqual(s["total"], 3)
        self.assertEqual([n["severity"] for n in s["notable"]], ["P1", "P2"])

    def test_foreign_schema_changes_degrade_instead_of_crashing(self):
        # Another project's JSON: a moved key must shrink the summary, not
        # break the pipeline.
        s = winnow.summarize({"findings": [{"sev": "P1"}, "not-a-dict", {}]})
        self.assertTrue(s["ran"])
        self.assertEqual(s["notable"], [])

    def test_text_never_implies_a_scan_that_did_not_happen(self):
        t = winnow.as_text({"ran": False, "why": "code-winnow not installed"})
        self.assertIn("did not run", t)
        self.assertIn("not installed", t)

    def test_findings_are_labelled_advisory(self):
        t = winnow.as_text(winnow.summarize({"findings": [
            {"severity": "P1", "path": "a.py", "line": 3, "message": "except/pass"}]}))
        self.assertIn("advisory", t)
        self.assertIn("not requirement failures", t)

    def test_an_empty_scope_is_not_a_clean_scan(self):
        # scan.py answers "no diff found" with exit 0, findings [] and files 0 --
        # identical to a reviewed change that came back clean. Reporting that as
        # "nothing significant" is the fabricated clean this module refuses.
        s = winnow.summarize({"files": 0, "findings": [], "complete": True,
                              "warnings": ["No diff found in scope 'branch'."]})
        self.assertFalse(s["ran"])
        self.assertIn("nothing was scanned", winnow.as_text(s))

    def test_a_scope_emptied_by_skips_says_which(self):
        s = winnow.summarize({"files": 0, "findings": [],
                              "errors": [{"path": "v.js", "error": "looks minified"}]})
        self.assertFalse(s["ran"])
        self.assertIn("skipped", s["why"])

    def test_a_real_clean_scan_still_reports_clean(self):
        s = winnow.summarize({"files": 4, "findings": [], "complete": True})
        self.assertTrue(s["ran"])
        self.assertIn("nothing significant", winnow.as_text(s))

    def test_partial_coverage_is_stated_not_swallowed(self):
        # complete:false means a file in scope was binary, minified or
        # unreadable. The findings that landed are still true; the coverage
        # behind them is not, and silence there reads as absence.
        t = winnow.as_text(winnow.summarize({
            "files": 2, "complete": False,
            "findings": [{"severity": "P1", "path": "a.py", "line": 1,
                          "message": "secret"}]}))
        self.assertIn("INCOMPLETE", t)
        self.assertIn("a.py", t)

    def test_a_missing_file_count_is_not_assumed_clean(self):
        # If the key is renamed upstream we cannot tell a clean tree from an
        # unexamined one. Say that, rather than pick the flattering reading.
        s = winnow.summarize({"findings": []})
        self.assertFalse(s["ran"])

    def test_a_moved_key_still_summarises_when_findings_exist(self):
        # The other half of that: findings prove files were opened, so a schema
        # drift must shrink the summary, never convert it into "did not run".
        s = winnow.summarize({"findings": [{"sev": "P1"}, "not-a-dict", {}]})
        self.assertTrue(s["ran"])

    def test_exit_two_carries_findings_and_must_not_be_discarded(self):
        # scan.py's contract is 0 = complete, 2 = incomplete. Exit 2 still
        # prints a valid report: one unreadable file must not throw away a P1
        # found in a different one.
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        fake = os.path.join(d, "scan.py")
        with open(fake, "w") as fh:
            fh.write("import json, sys\n"
                     "print(json.dumps({'files': 2, 'complete': False,\n"
                     "  'errors': [{'path': 'v.js', 'error': 'looks minified'}],\n"
                     "  'findings': [{'severity': 'P1', 'path': 'a.py',\n"
                     "                'line': 7, 'message': 'committed secret'}]}))\n"
                     "sys.exit(2)\n")
        written = {}

        class _Task:
            def write_text(self, path, text):
                written[path] = text

        s = winnow.run(_Task(), fake, d, "HEAD", "run-1")
        self.assertTrue(s["ran"], "a partial scan was reported as no scan")
        self.assertEqual(len(s["notable"]), 1)
        self.assertFalse(s["complete"])
        self.assertTrue(written, "the raw report was not persisted")

    def test_an_unexpected_exit_code_is_still_refused(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        fake = os.path.join(d, "scan.py")
        with open(fake, "w") as fh:
            fh.write("import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n")

        class _Task:
            def write_text(self, path, text):
                raise AssertionError("nothing should be persisted for a failed run")

        s = winnow.run(_Task(), fake, d, "HEAD", "run-1")
        self.assertFalse(s["ran"])
        self.assertIn("boom", s["why"])


class TestAgentRaisedSignals(unittest.TestCase):
    """An agent that follows references/escalation.md -- stops at the threshold
    and attaches a signal -- has to reach the ladder, not the human.

    `status: escalate` was read as rung 4 whatever it carried, so an agent that
    obeyed the protocol was routed strictly worse than one that failed in
    silence: the silent one got rungs 2 and 3, the honest one got a parked run.
    `report.signals` had no reader anywhere in the orchestrator, which is what
    made the two defects one defect -- there was nothing to route on."""

    PLAN = """# Plan

## Subtasks
```yaml
- id: st-1-alpha
  goal: add alpha
  file_scope: ["alpha.py"]
  acceptance: [AC-1]
```
"""

    def setUp(self):
        self.t = TempRepo()
        with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
            fh.write('fast:\n  - "python3 -c \'print(1)\'"\ntest_author: never\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "cfg"], self.t.repo)
        self.reg = router.load_registry(REGISTRY)
        self.pol = dict(self.reg["policy"]["limits"],
                        escalation_ceiling=self.reg["policy"]["escalation_ceiling"])
        self.planner_calls = 0
        self.planner_bundle = ""

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.t.repo, capture_output=True)
        self.t.close()

    def _run(self, signals, escalate_forever=False):
        """One implementer that escalates with `signals`, then does the job.

        Escalating once and succeeding afterwards is what separates the rungs:
        a run that recovers proves the ladder moved, where a run that parks
        cannot tell rung 4 from a rung that was never walked."""
        calls = {"impl": 0}

        def planner(env, cwd):
            self.planner_calls += 1
            td = env["AGENT_DELEGATION_TASK_DIR"]
            if os.path.exists(os.path.join(td, "escalation.md")):
                with open(os.path.join(td, "escalation.md")) as fh:
                    self.planner_bundle = fh.read()
            with open(os.path.join(td, "plan.md"), "w") as fh:
                fh.write(self.PLAN)

        def implementer(env, cwd):
            calls["impl"] += 1
            with open(os.path.join(cwd, "alpha.py"), "w") as fh:
                fh.write("A = %d\n" % calls["impl"])
            first = calls["impl"] == 1 or escalate_forever
            _report(env["AGENT_DELEGATION_TASK_DIR"], "implement-st-1-alpha.json", {
                "stage": "implement", "role": "implementer", "subtask": "st-1-alpha",
                "status": "escalate" if first else "complete",
                "summary": "stopping at the threshold, signal attached",
                "signals": signals, "evidence": {"tests": "captured"}})

        def reviewer(env, cwd):
            with open(os.path.join(env["AGENT_DELEGATION_TASK_DIR"],
                                   "reports", "review-reviewer.json"), "w") as fh:
                json.dump({"verdict": "APPROVE",
                           "ac_table": [{"ac": "AC-1", "status": "met",
                                         "evidence": "checks pass"}],
                           "findings": []}, fh)

        task = store.Task.create(self.t.repo, "T-SIG", "# t\n\nAdd alpha\n", self.pol)
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(
            {"planner": planner, "implementer": implementer, "reviewer": reviewer}),
            lambda k, t: True, log=logs.append).run()
        return status, task, logs

    def test_a_test_stuck_signal_climbs_to_a_stronger_model(self):
        # rung 2. The shipped registry enrols opus-class-strong for
        # implementer precisely so this rung has somewhere to go.
        status, task, logs = self._run([{
            "type": "test_stuck", "detail": "test_alpha fails on the third fix",
            "evidence": "$ pytest -q\nE   assert 1 == 2",
            "attempted": ["widened the guard", "reordered the calls"]}])
        self.assertEqual(status, "done", "\n".join(logs))
        self.assertEqual(int(task.state["spent"]["replans"]), 0,
                         "rung 2 was available and it skipped to rung 3")
        self.assertTrue(any("escalating to" in l for l in logs),
                        "no rung was climbed\n%s" % "\n".join(logs))

    def test_a_plan_conflict_reaches_the_planner_not_the_human(self):
        # : plan_conflict enters at rung 3 directly -- a stronger
        # implementer cannot fix a plan that is wrong about reality.
        status, task, logs = self._run([{
            "type": "plan_conflict",
            "detail": "plan.md:L4 scopes alpha.py, but alpha.py is generated",
            "evidence": "$ git check-ignore -q alpha.py && echo ignored\nignored"}])
        self.assertEqual(int(task.state["spent"]["replans"]), 1,
                         "rung 3 never fired\n%s" % "\n".join(logs))
        self.assertGreaterEqual(self.planner_calls, 2, "the planner was never re-run")
        self.assertEqual(status, "done", "\n".join(logs))

    def test_the_planner_is_handed_the_signal_the_agent_wrote(self):
        # The whole point of `attempted`: the next reader skips what is already
        # ruled out. Dropping it makes the field decoration.
        self._run([{
            "type": "plan_conflict", "detail": "plan.md:L4 contradicts the tree",
            "evidence": "$ git check-ignore -q alpha.py\n(exit 0)",
            "attempted": ["renaming the module", "widening the scope glob"]}])
        bundle = self.planner_bundle
        self.assertIn("plan_conflict", bundle)
        self.assertIn("plan.md:L4 contradicts the tree", bundle)
        self.assertIn("renaming the module", bundle,
                      "the agent's ruled-out attempts never reached the planner")

    def test_a_missing_dependency_still_stops_for_a_human(self):
        # Rung 4 is right here and nothing below it helps: no model and no plan
        # can install a package.
        status, _, logs = self._run([{
            "type": "missing_dependency", "detail": "needs libfoo>=2",
            "evidence": "ModuleNotFoundError: No module named 'libfoo'"}],
            escalate_forever=True)
        self.assertEqual(status, "needs_human", "\n".join(logs))
        self.assertTrue(any("libfoo" in l or "missing_dependency" in l for l in logs),
                        "it parked without saying which signal parked it")

    def test_low_confidence_alone_routes_nowhere(self):
        # D4. Self-reported confidence is a tiebreaker; on its own it carries no
        # routing information, so this is an escalate with nothing to act on.
        status, task, logs = self._run([{
            "type": "low_confidence", "detail": "unsure about the approach",
            "evidence": "(none)"}], escalate_forever=True)
        self.assertEqual(status, "needs_human", "\n".join(logs))
        self.assertEqual(int(task.state["spent"]["replans"]), 0,
                         "a confidence report alone spent a replan")
        # Parking is the right answer, but it has to be the *reasoned* one.
        # Asserting only the status cannot tell "nothing to route on" from the
        # old behaviour, where every escalate parked whatever it carried.
        self.assertTrue(any("low_confidence" in l for l in logs),
                        "it parked without saying what it could not route\n%s"
                        % "\n".join(logs))


class TestReportFreshness(unittest.TestCase):
    """A report already on disk when the turn began is not this turn's report.

    The freshness test was `mtime >= time.time() - 1`, a wall-clock window wide
    enough to swallow the whole run under the mock adapter. Unique filenames
    stopped siblings colliding, but a *retry of the same subtask* writes to the
    same path, so an agent that produced nothing on attempt 2 could still hand
    back attempt 1's success. Comparing the file against what was there before
    the turn needs no clock and no tolerance."""

    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)
        self.task = store.Task.create(self.t.repo, "T-FRESH", "# t\n",
                                      dict(self.reg["policy"]["limits"]))
        self.orch = Orchestrator(self.task, self.reg, runtime.MockAdapter({}),
                                 lambda k, t: True, log=lambda *_: None)
        self.sub = {"id": "st-1-alpha"}

    def tearDown(self):
        self.t.close()

    def _write(self, summary):
        _report(self.task.path, "implement-st-1-alpha.json", {
            "stage": "implement", "role": "implementer", "subtask": "st-1-alpha",
            "status": "complete", "summary": summary, "evidence": {"tests": "ok"}})

    def test_a_report_unchanged_since_the_turn_began_is_not_read_as_fresh(self):
        self._write("attempt 1 succeeded")
        before = self.orch._report_state()          # the turn starts here
        report, problems = self.orch._collect_report("implementer", self.sub,
                                                     before=before)
        self.assertIsNone(report, "attempt 1's report was read as attempt 2's")
        self.assertTrue(problems)

    def test_a_report_rewritten_during_the_turn_is_read(self):
        self._write("attempt 1 succeeded")
        before = self.orch._report_state()
        self._write("attempt 2 did something else")
        report, problems = self.orch._collect_report("implementer", self.sub,
                                                     before=before)
        self.assertIsNone(problems)
        self.assertEqual(report["summary"], "attempt 2 did something else")

    def test_a_report_written_for_the_first_time_is_read(self):
        before = self.orch._report_state()          # nothing on disk yet
        self._write("first attempt")
        report, problems = self.orch._collect_report("implementer", self.sub,
                                                     before=before)
        self.assertIsNone(problems)
        self.assertEqual(report["summary"], "first attempt")


class TestResourceHygiene(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)

    def tearDown(self):
        self.t.close()

    def test_reading_the_repo_for_facts_leaks_no_file_handles(self):
        # `open(...).read()` per tracked file, never closed. Harmless on a small
        # repo and not harmless at the 400-file cap it is written to tolerate,
        # which is exactly where it would first matter.
        import warnings
        task = store.Task.create(self.t.repo, "T-FD", "# t\n",
                                 dict(self.reg["policy"]["limits"]))
        orch = Orchestrator(task, self.reg, runtime.MockAdapter({}),
                            lambda k, t: True, log=lambda *_: None)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            orch._repo_facts()
        leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
        self.assertEqual([str(w.message) for w in leaks], [])


class TestEndToEnd(unittest.TestCase):
    """Milestone 1."""

    def setUp(self):
        self.t = TempRepo()
        with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
            fh.write('fast:\n  - "python3 -c \'import app; assert app.add(1,2)==3\'"\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "config"], self.t.repo)
        self.reg = router.load_registry(REGISTRY)

    def tearDown(self):
        self.t.close()

    def _script(self, fail_first=False):
        state = {"impl_calls": 0}

        def planner(env, cwd):
            with open(os.path.join(env["AGENT_DELEGATION_TASK_DIR"], "plan.md"), "w") as fh:
                fh.write(PLAN_MD)
            _report(env["AGENT_DELEGATION_TASK_DIR"], "plan-planner.json", {
                "stage": "plan", "role": "planner", "status": "complete",
                "summary": "one subtask", "evidence": {"commands_run": []}})

        def implementer(env, cwd):
            state["impl_calls"] += 1
            broken = fail_first and state["impl_calls"] == 1
            body = "def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return %s\n" % (
                "a - b" if not broken else "a - b\n\nraise SystemExit(1)")
            with open(os.path.join(cwd, "app.py"), "w") as fh:
                fh.write(body)
            _report(env["AGENT_DELEGATION_TASK_DIR"], "implement-st-1-subtract.json", {
                "stage": "implement", "role": "implementer", "subtask": "st-1-subtract",
                "status": "complete", "summary": "added subtract",
                "evidence": {"tests": "ok"}})

        def reviewer(env, cwd):
            with open(os.path.join(env["AGENT_DELEGATION_TASK_DIR"],
                                   "reports", "review-reviewer.json"), "w") as fh:
                json.dump({"verdict": "APPROVE",
                           "ac_table": [{"ac": "AC-1", "status": "met",
                                         "evidence": "fast checks pass"}],
                           "findings": []}, fh)

        return {"planner": planner, "implementer": implementer, "reviewer": reviewer}, state

    def _run(self, script, gate=lambda k, t: True, request="Add a subtract API function"):
        pol = dict(self.reg["policy"]["limits"])
        pol["escalation_ceiling"] = self.reg["policy"]["escalation_ceiling"]
        task = store.Task.create(self.t.repo, "T-001",
                                 "# Task T-001\n\n## Request\n\n%s\n" % request, pol)
        adapter = runtime.MockAdapter(script)
        logs = []
        orch = Orchestrator(task, self.reg, adapter, gate, log=logs.append)
        return orch.run(), task, adapter, logs

    def test_full_pipeline_reaches_done(self):
        script, _ = self._script()
        status, task, adapter, logs = self._run(script)
        self.assertEqual(status, "done", "\n".join(logs))

        state = task.state
        # every role actually ran, in order
        steps = [(h["stage"], h["role"]) for h in state["delegation_history"]]
        self.assertEqual(steps, [("brainstorm", "planner"), ("plan", "planner"),
                                 ("plan", "test-author"), ("implement", "implementer"),
                                 ("review", "reviewer")])
        # the plan produced a real subtask, completed, with files from git
        self.assertEqual(state["subtasks"][0]["status"], "complete")
        self.assertEqual(state["subtasks"][0]["actual_files"], ["app.py"])
        self.assertEqual(state["subtasks"][0]["scope_violations"], [])
        # verify output was persisted as evidence
        runs = os.listdir(task.file("verify"))
        self.assertTrue(runs, "no verify output recorded")
        # a human-readable brief exists and is jargon-free
        self.assertEqual(brief.lint(task.read_text("brief.md")), [])
        # the user's repository was never touched
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=self.t.repo,
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(dirty, "", "orchestrator dirtied the user's checkout")

    def test_work_happened_in_a_worktree_not_the_checkout(self):
        script, _ = self._script()
        status, task, _, logs = self._run(script)
        self.assertEqual(status, "done", "\n".join(logs))
        wt = task.state["worktree"]
        self.assertTrue(wt, "no worktree was ever recorded")
        self.assertFalse(wt.startswith(os.path.realpath(self.t.repo) + os.sep),
                         "the worktree was inside the user's checkout")
        self.assertNotIn("def subtract", _slurp(os.path.join(self.t.repo, "app.py")),
                         "change leaked into the user's checkout")
        # The work itself is on the branch, which outlives the worktree -- the
        # directory is reaped on `done`, and asserting it still exists was
        # asserting the leak this program now cleans up.
        show = subprocess.run(["git", "show", "%s:app.py" % task.state["branch"]],
                              cwd=self.t.repo, capture_output=True, text=True)
        self.assertIn("def subtract", show.stdout, show.stderr)

    def test_a_finished_task_does_not_leave_its_worktrees_behind(self):
        """`.adg-worktrees/` grew one directory per subtask per task forever
        while two READMEs called them throwaway."""
        script, _ = self._script()
        status, task, _, logs = self._run(script)
        self.assertEqual(status, "done", "\n".join(logs))
        left = [p for p in [task.state.get("worktree")]
                + [s.get("worktree") for s in task.state.get("subtasks") or []]
                if p and os.path.isdir(p)]
        self.assertEqual(left, [], "worktrees survived a finished task")

    def test_a_parked_task_keeps_its_worktree(self):
        """The salvage point. A parked or crashed task's worktree holds the
        checkpointed work a human resumes from — reaping on any terminal state
        would delete exactly the cases where the files still matter."""
        script, _ = self._script()
        script["reviewer"] = lambda env, cwd: None
        task = store.Task.create(self.t.repo, "T-KEEP",
                                 "# t\n\nAdd a subtract API function\n",
                                 self.reg["policy"]["limits"])
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: k != "merge",     # decline the merge
                              log=lambda *_: None).run()
        self.assertEqual(status, "needs_human")
        self.assertTrue(os.path.isdir(task.state["worktree"]),
                        "the salvage point was deleted from under a parked task")

    def test_attended_mode_produces_a_patch_and_never_commits(self):
        script, _ = self._script()
        status, task, _, _ = self._run(script)
        self.assertEqual(status, "done")
        self.assertTrue(os.path.exists(task.file("integrate.patch")))
        log = subprocess.run(["git", "log", "--oneline", "main"], cwd=self.t.repo,
                             capture_output=True, text=True).stdout
        self.assertEqual(len(log.strip().splitlines()), 2, "orchestrator committed to main")

    def test_failing_checks_force_rework_before_review(self):
        script, state = self._script(fail_first=True)
        status, task, _, logs = self._run(script)
        self.assertEqual(status, "done", "\n".join(logs))
        self.assertEqual(state["impl_calls"], 2, "should have retried after a red check")
        roles = [h["role"] for h in task.state["delegation_history"]]
        self.assertEqual(roles.count("implementer"), 2)
        self.assertLess(roles.index("implementer"), roles.index("reviewer"))

    def test_retry_continues_the_same_agent_instead_of_restarting(self):
        # A fresh agent per attempt re-reads the protocol, the task and the
        # repo every time -- most of the wall clock on a short task.
        script, state = self._script(fail_first=True)
        status, task, adapter, logs = self._run(script)
        self.assertEqual(status, "done", "\n".join(logs))
        follow_ups = [c for c in adapter.calls if c[0] == "follow_up"]
        self.assertEqual(len(follow_ups), 1, "second attempt should be a follow-up")
        self.assertEqual(follow_ups[0][1], "implementer")
        self.assertIn("continued", "\n".join(logs))

    def test_retry_prompt_carries_the_real_failure(self):
        script, _ = self._script(fail_first=True)
        captured = []

        class A(runtime.MockAdapter):
            def follow_up(self, session, text, timeout):
                captured.append(text)
                return runtime.MockAdapter.follow_up(self, session, text, timeout)

        pol = dict(self.reg["policy"]["limits"],
                   escalation_ceiling=self.reg["policy"]["escalation_ceiling"])
        task = store.Task.create(self.t.repo, "T-003", "# t\n\nAdd subtract\n", pol)
        Orchestrator(task, self.reg, A(script), lambda k, t: True, log=lambda *_: None).run()
        self.assertTrue(captured, "no follow-up sent")
        self.assertIn("did not pass", captured[0])
        self.assertIn("import app", captured[0], "should quote the failing command")

    def test_reviewer_is_always_a_fresh_session(self):
        # Independence is the whole point of review: it must not inherit the
        # implementer's context.
        script, _ = self._script()
        status, task, adapter, _ = self._run(script)
        self.assertEqual(status, "done")
        roles = [c[1] for c in adapter.calls if c[0] == "follow_up"]
        self.assertNotIn("reviewer", roles)

    def _run_simple(self, script, review=None, break_scope=False):
        """A task the classifier calls simple, so the review policy applies."""
        pol = dict(self.reg["policy"]["limits"],
                   escalation_ceiling=self.reg["policy"]["escalation_ceiling"])
        task = store.Task.create(self.t.repo, "T-00S", "# t\n\nAdd subtract\n", pol)
        if review:
            task.update(review=review)

        class A(runtime.MockAdapter):
            def prompt(self, session, text, timeout):
                if session.handle["role"] == "classifier":
                    return {"settled": "idle", "output": "VERDICT: SIMPLE -- tiny change",
                            "code": 0}
                return runtime.MockAdapter.prompt(self, session, text, timeout)

        adapter = A(script)
        logs = []
        status = Orchestrator(task, self.reg, adapter, lambda k, t: True,
                              log=logs.append).run()
        return status, task, adapter, logs

    def test_simple_task_skips_llm_review_by_default(self):
        script, _ = self._script()
        status, task, adapter, logs = self._run_simple(script)
        self.assertEqual(status, "done", "\n".join(logs))
        self.assertFalse(task.state["review_outcome"]["reviewed"])
        # `c[0]`, the role. `c[1]` is the whole prompt body for a prompt() call
        # (MockAdapter.calls holds (role, text)), so the old form asked whether
        # any element of a list of multi-KB prompts was exactly "reviewer" --
        # which nothing can be. Dispatching a reviewer on every simple task
        # passed it.
        self.assertNotIn("reviewer", [c[0] for c in adapter.calls])
        self.assertIn("checks green", task.state["review_outcome"]["why"])

    def test_brief_says_plainly_that_no_review_ran(self):
        script, _ = self._script()
        _, task, _, _ = self._run_simple(script)
        text = task.read_text("brief.md")
        self.assertIn("No independent review was run", text)
        self.assertIn("--review always", text)
        self.assertEqual(brief.lint(text), [], "the notice must stay jargon-free")

    def test_review_always_forces_it_on_a_simple_task(self):
        script, _ = self._script()
        status, task, adapter, logs = self._run_simple(script, review="always")
        self.assertEqual(status, "done", "\n".join(logs))
        self.assertTrue(task.state["review_outcome"]["reviewed"])

    def test_complex_task_still_always_reviewed(self):
        script, _ = self._script()
        status, task, _, logs = self._run(script)  # planner path = complex
        self.assertEqual(status, "done", "\n".join(logs))
        self.assertTrue(task.state["review_outcome"]["reviewed"])

    def test_reviewer_findings_reach_the_implementer(self):
        # Reopening a subtask without the findings sends the agent back to the
        # same code with the same prompt -- it rebuilds what was rejected.
        script, _ = self._script()
        rounds = {"n": 0}
        seen = []

        def reviewer(env, cwd):
            rounds["n"] += 1
            first = rounds["n"] == 1
            with open(os.path.join(env["AGENT_DELEGATION_TASK_DIR"],
                                   "reports", "review-reviewer.json"), "w") as fh:
                json.dump({
                    "verdict": "REQUEST_CHANGES" if first else "APPROVE",
                    "ac_table": [{"ac": "AC-1", "status": "unmet" if first else "met"}],
                    "findings": ([{"id": "f-1", "severity": "blocking",
                                   "claim": "subtract must reject non-numeric input",
                                   "cite": "task.md#AC-1", "file": "app.py"}]
                                 if first else []),
                }, fh)
        script["reviewer"] = reviewer

        class A(runtime.MockAdapter):
            def prompt(self, session, text, timeout):
                if session.handle["role"] == "implementer":
                    seen.append(text)
                return runtime.MockAdapter.prompt(self, session, text, timeout)

        pol = dict(self.reg["policy"]["limits"],
                   escalation_ceiling=self.reg["policy"]["escalation_ceiling"])
        task = store.Task.create(self.t.repo, "T-004", "# t\n\nAdd subtract API\n", pol)
        status = Orchestrator(task, self.reg, A(script), lambda k, t: True,
                              log=lambda *_: None).run()
        self.assertEqual(status, "done")
        self.assertGreaterEqual(len(seen), 2, "implementer should have run twice")
        self.assertIn("reject non-numeric input", seen[-1], "findings not delivered")
        self.assertIn("task.md#AC-1", seen[-1], "citation not delivered")

    def test_findings_are_cleared_once_the_subtask_is_green(self):
        script, _ = self._script()
        _, task, _, _ = self._run(script)
        self.assertEqual(task.state.get("pending_findings"), [])

    def test_declined_gate_parks_instead_of_proceeding(self):
        script, _ = self._script()
        status, task, _, _ = self._run(script, gate=lambda k, t: k != "merge")
        self.assertEqual(status, "needs_human")
        self.assertFalse(os.path.exists(task.file("integrate.patch")))

    def test_a_declined_gate_is_recorded_not_only_the_approvals(self):
        """A decline is the one moment the machine was about to be wrong. It
        used to raise Halt without writing anything, so `gates` held approvals
        only and nothing counting interventions could see a rejection."""
        script, _ = self._script()
        _, task, _, _ = self._run(script, gate=lambda k, t: k != "merge")
        states = [(g["kind"], g["state"]) for g in task.state["gates"]]
        self.assertIn(("merge", "declined"), states,
                      "the rejection left no trace: %s" % states)

    def test_a_gate_with_nobody_to_ask_parks_instead_of_declining(self):
        """`no tty` is not `no`.

        The old `_confirm` caught EOFError and returned False, which wrote a
        rejection no human made into the one record that exists to count what
        humans decided. It also makes the CLI unusable as a bridge: the caller
        is now the user's own agent shelling out, and an agent has no tty
        either, so every gate of every run would auto-decline.
        """
        from adg.machine import AwaitingApproval
        script, _ = self._script()

        def no_human(kind, text):
            raise AwaitingApproval(kind, text, resume_status=None)

        status, task, _, _ = self._run(script, gate=no_human)
        self.assertEqual(status, "awaiting_approval",
                         "a question with nobody to answer it is not a failure")
        self.assertNotIn("declined", [g["state"] for g in task.state["gates"]],
                         "recorded a rejection nobody made")
        pend = task.state.get("pending_gate") or {}
        self.assertTrue(pend.get("brief"), "parked without keeping the question")
        self.assertTrue(pend.get("resume_status"),
                        "parked without recording where to continue -- the "
                        "caller knows the pipeline, the gate does not")

    def test_an_answered_gate_is_not_asked_again_on_resume(self):
        """`resume_status` has to land past the question, not on it.

        The design and plan gates sit at the END of their stage, so resuming
        the stage that asked would re-run the planner and re-author the tests
        to put a question that has already been answered.
        """
        from adg.machine import AwaitingApproval
        script, _ = self._script()
        asked = []

        def park(kind, text):
            asked.append(kind)
            raise AwaitingApproval(kind, text, resume_status=None)

        status, task, _, _ = self._run(script, gate=park)
        self.assertEqual(status, "awaiting_approval")
        first, pend = asked[0], task.state["pending_gate"]

        # the state `delegate approve` leaves behind
        task.update(pending_gate=dict(pend, decision="approved", note="ship it"),
                    status=pend["resume_status"])
        Orchestrator(task, self.reg, runtime.MockAdapter(script), park,
                     log=lambda *_: None).run()

        self.assertEqual(asked.count(first), 1,
                         "re-asked the %s gate after it was answered" % first)
        # It parks again, at the NEXT gate -- which is the run making progress,
        # not the answer being ignored. A run with more than one gate cannot end
        # with an empty `pending_gate` while a human is still being asked things.
        self.assertNotEqual((task.state.get("pending_gate") or {}).get("kind"), first,
                            "still parked on the gate that was already answered")

    def test_a_qualified_approval_reaches_the_agents_that_act_on_it(self):
        """"Yes, but keep the old endpoint" is not a yes to what was proposed.

        The note was recorded in `gates[]` and read by nobody, which makes it a
        record of an instruction that never happened. It has to reach the
        implementer, who builds the different thing, and the reviewer, who would
        otherwise flag the retained endpoint as scope creep and reject work that
        is doing exactly what the human asked.
        """
        script, _ = self._script()
        task = store.Task.create(self.t.repo, "T-042", "# t\n\nAdd a subtract API function\n",
                                 self.reg["policy"]["limits"])
        task.update(gate_note={"kind": "plan", "note": "keep the old endpoint working"})
        orch = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                            lambda k, t: True, log=lambda *_: None)

        note = orch._human_note()
        self.assertIn("keep the old endpoint working", note)
        self.assertIn("plan", note, "the agent is not told which gate this came from")

        # folded into a prompt that already had content, and one that had none
        self.assertIn("existing text", orch._with_note("existing text"))
        self.assertIn("keep the old endpoint working", orch._with_note("existing text"))
        self.assertEqual(orch._with_note(None), note)

        # and it survives all the way into what an agent is actually handed
        for role in ("implementer", "reviewer"):
            text = prompts.compose(role, task, extra=orch._with_note(None))
            self.assertIn("keep the old endpoint working", text,
                          "the %s never sees the qualification" % role)

    def test_no_qualification_adds_nothing_to_a_prompt(self):
        """An unqualified yes must not append an empty paragraph to every
        downstream prompt — `_with_note` has to be a no-op, not a formatter."""
        script, _ = self._script()
        task = store.Task.create(self.t.repo, "T-043", "# t\n\nAdd a subtract API function\n",
                                 self.reg["policy"]["limits"])
        orch = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                            lambda k, t: True, log=lambda *_: None)
        self.assertEqual(orch._human_note(), "")
        self.assertEqual(orch._with_note("just this"), "just this")
        self.assertIsNone(orch._with_note(None))
        task.update(gate_note={"kind": "plan", "note": "   "})
        self.assertEqual(orch._human_note(), "", "whitespace is not a qualification")

    def test_a_later_unqualified_approval_clears_an_earlier_qualification(self):
        """`_human_note` promises the note is "superseded by the next approval".
        It was only written when non-empty, so approving the design with
        "keep the old endpoint" and then the plan with nothing left the design's
        qualification flowing into every prompt for the rest of the run."""
        task = self._parked("T-053")
        pend = task.state["pending_gate"]
        cli.cmd_approve(self._args(task, "approve", note="keep the old endpoint",
                                   no_continue=True))
        self.assertEqual(task.state["gate_note"]["note"], "keep the old endpoint")

        task.update(status="awaiting_approval",
                    pending_gate=dict(pend, kind="merge", decision=None))
        cli.cmd_approve(self._args(task, "approve", no_continue=True))
        self.assertEqual(task.state["gate_note"]["note"], "",
                         "the earlier qualification outlived the approval that "
                         "was supposed to supersede it")

    # --- the CLI half of the gate flow -------------------------------------
    #
    # These three drive `cmd_approve` / `cmd_reject` themselves. Every earlier
    # gate test simulated the CLI by hand-writing `pending_gate`, so all three
    # bugs below lived in the one code path nothing executed.

    def _parked(self, task_id):
        """A task parked at its first gate, via the real park path."""
        from adg.machine import AwaitingApproval
        script, _ = self._script()
        task = store.Task.create(self.t.repo, task_id,
                                 "# t\n\nAdd a subtract API function\n",
                                 self.reg["policy"]["limits"])

        def park(kind, text):
            raise AwaitingApproval(kind, text, resume_status=None)

        Orchestrator(task, self.reg, runtime.MockAdapter(script), park,
                     log=lambda *_: None).run()
        self.assertEqual(task.state["status"], "awaiting_approval")
        return task

    def _args(self, task, decision, **kw):
        fields = dict(repo=self.t.repo, registry=REGISTRY, id=task.state["id"],
                      note="", adapter="mock", no_panes=False, dry_run=True,
                      no_continue=False, decision=decision)
        fields.update(kw)
        return argparse.Namespace(**fields)

    def test_a_rejected_gate_can_be_answered_again_after_the_fix(self):
        """A decline is not the end of the task, only of that run.

        The merge gate kept its pending decision unconditionally, so a reject
        was permanent: the human's agent fixed the problem, called `approve`,
        and got "already answered 'declined'" with no way back -- and a later
        `--yes` run found the stale decline and threw the rework away at a gate
        nobody was asked about.
        """
        task = self._parked("T-050")
        kind = task.state["pending_gate"]["kind"]
        cli.cmd_approve(self._args(task, "reject"))
        self.assertEqual([g["state"] for g in task.state["gates"]], ["declined"])
        self.assertIsNone(task.state.get("pending_gate"),
                          "a decline left a decision behind that nothing consumes")
        self.assertEqual(task.state["status"], "needs_human")

        # the gate is answerable again once the run is put back in front of it
        task.update(status="awaiting_approval",
                    pending_gate={"kind": kind, "brief": "b", "resume_status": "implement"})
        # --no-continue: what is under test is that the gate ACCEPTS a second
        # answer, not what the run does afterwards.
        cli.cmd_approve(self._args(task, "approve", no_continue=True))
        self.assertEqual([g["state"] for g in task.state["gates"]],
                         ["declined", "approved"],
                         "the rework could not be approved after a decline")

    def test_no_continue_leaves_a_task_that_can_still_be_resumed(self):
        """It cleared `pending_gate` and left the status at `awaiting_approval`,
        which nothing handles -- so `resume` halted with "no handler for stage"
        and `approve` refused because no gate was pending. `resume_status` was
        gone, and for a design gate the fallback guess is wrong: `needs_human`
        resumes at `implement` and skips planning entirely."""
        task = self._parked("T-051")
        resume_at = task.state["pending_gate"]["resume_status"]
        cli.cmd_approve(self._args(task, "approve", no_continue=True))
        self.assertEqual(task.state["status"], resume_at,
                         "the task was left on a status no stage handles")
        self.assertNotEqual(task.state["status"], "awaiting_approval")

    def test_resume_on_a_waiting_gate_says_to_answer_it(self):
        """`awaiting_approval` is not a stage. Falling through gave
        "no handler for stage 'awaiting_approval'", which reads as a crash for
        an ordinary state: a question is waiting on a human."""
        task = self._parked("T-052")
        args = argparse.Namespace(repo=self.t.repo, registry=REGISTRY,
                                  id=task.state["id"], stage=None, adapter="mock",
                                  no_panes=False, dry_run=True, yes=False,
                                  when_open=False)
        with self.assertRaises(SystemExit) as cm:
            cli.cmd_resume(args)
        self.assertIn("approve", str(cm.exception))
        self.assertEqual(task.state["status"], "awaiting_approval",
                         "refusing to resume must not also mutate the task")

    def test_an_implementer_is_told_the_scope_it_is_measured_against(self):
        """The boundary in the prompt and the boundary in the check were two
        different keys.

        The planner writes `file_scope` in plan.md; `_read_plan_subtasks`
        renames it to `planned_scope` on the way into task.json. `compose` read
        `file_scope`, which a stored subtask never has — so every implementer
        prompt printed the scope header with nothing under it, while
        `verify.scope_violations` recorded violations of a boundary the agent
        was never shown.
        """
        script, _ = self._script()
        task = store.Task.create(self.t.repo, "T-060",
                                 "# t\n\nAdd a subtract API function\n",
                                 self.reg["policy"]["limits"])
        Orchestrator(task, self.reg, runtime.MockAdapter(script),
                     lambda k, t: True, log=lambda *_: None).run()
        sub = task.state["subtasks"][0]
        self.assertTrue(sub.get("planned_scope"),
                        "the fixture plan declares no scope, so this proves nothing")

        text = prompts.compose("implementer", task, subtask=sub)
        # Asserted against the SCOPE SECTION, not against the whole prompt. The
        # fixture's goal line is "Add a subtract function to app.py" and its
        # scope is ["app.py"], so a bare `assertIn("app.py", text)` passes on
        # the goal and proves nothing -- it passed against the unfixed code.
        # That is the same substring-that-cannot-tell-two-things-apart mistake
        # this repo has now made in four places.
        after = text.split("Write scope", 1)
        self.assertEqual(len(after), 2, "no scope section in the prompt at all")
        section = after[1].split("Verification commands")[0].split("Budget for")[0]
        for glob in sub["planned_scope"]:
            self.assertIn("\n  %s" % glob, section,
                          "the implementer was never shown %r, which "
                          "scope_violations measures it against.\n%s" % (glob, section))

    def test_an_unrestricted_subtask_says_so_rather_than_showing_a_bare_header(self):
        """An empty scope means `**` to `scope_violations`. Printing "a hard
        boundary:" with nothing under it reads as unspecified when it means
        unrestricted."""
        task = store.Task.create(self.t.repo, "T-061", "# t\n\nwhatever\n",
                                 self.reg["policy"]["limits"])
        text = prompts.compose("implementer", task,
                               subtask={"id": "st-1", "goal": "g", "planned_scope": []})
        self.assertIn("not restricted", text)
        self.assertNotIn("a hard boundary", text)

    def test_unparseable_plan_parks_rather_than_guessing(self):
        script, _ = self._script()
        script["planner"] = lambda env, cwd: _spit(
            os.path.join(env["AGENT_DELEGATION_TASK_DIR"], "plan.md"), "no yaml here")
        status, task, _, _ = self._run(script)
        self.assertEqual(status, "needs_human")

    def test_cost_cap_stops_the_run(self):
        script, _ = self._script()
        task = store.Task.create(
            self.t.repo, "T-002", "# t\n\nAdd a subtract API function\n",
            dict(self.reg["policy"]["limits"], max_cost_usd=5))
        b = limits.Budget(task)
        b.spend(5.0)
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=lambda *_: None).run()
        self.assertEqual(status, "needs_human")


class TestWorkflowManifest(unittest.TestCase):
    """The workflow is declared, not hardcoded.

    What these pin is the boundary: a manifest may enable, disable, repoint and
    re-discipline a stage, and may NOT invent one. The state machine stays code
    -- `implement` runs worktrees, waves and an escalation ladder while
    `classify` is one parsed line -- so a manifest that could add stages would
    be promising something the machine cannot deliver.
    """

    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)
        self.dir = tempfile.mkdtemp(prefix="wf-")
        src = wf.default_dir()
        shutil.copy(os.path.join(src, "PROTOCOL.md"), self.dir)
        shutil.copytree(os.path.join(src, "roles"), os.path.join(self.dir, "roles"))

    def tearDown(self):
        wf._CURRENT = None          # the module-level workflow is process state
        shutil.rmtree(self.dir, ignore_errors=True)
        self.t.close()

    def _manifest(self, body):
        with open(os.path.join(self.dir, "workflow.yaml"), "w") as fh:
            fh.write(body)
        return wf.use(self.dir)

    BASE = """name: t
protocol: PROTOCOL.md
stages:
  intake: {role: intake}
  classify: {role: classifier}
  brainstorm:
    role: planner
    card: roles/planner.md
    discipline:
      skill: superpowers:brainstorming
      text: Borrowed discipline.
      fallback: Do it yourself.
  plan: {role: planner, card: roles/planner.md}
  implement: {role: implementer, card: roles/implementer.md}
  review: {role: reviewer, card: roles/reviewer.md%s}
  integrate: {role: integrator, card: roles/integrator.md}
"""

    def test_the_bundled_default_is_a_valid_manifest(self):
        w = wf.Workflow.load()
        self.assertEqual(w.order(),
                         ["intake", "classify", "brainstorm", "plan",
                          "implement", "review", "integrate"],
                         "the manifest and machine.STAGES disagree on the pipeline")
        for role in ("planner", "implementer", "reviewer", "integrator", "test-author"):
            self.assertTrue(os.path.exists(w.card(role)),
                            "%s's card is declared but not on disk" % role)
        self.assertTrue(os.path.exists(w.protocol()))

    def test_a_stage_borrows_its_method_from_an_installed_skill(self):
        """The compatibility primitive. This used to be an `if superpowers` with
        both texts inline in `_stage_brainstorm`, so hosting any other design
        discipline meant patching the machine."""
        w = self._manifest(self.BASE % "")
        self.assertEqual(w.discipline("brainstorm", {"superpowers": True}),
                         "Borrowed discipline.")
        self.assertEqual(w.discipline("brainstorm", {}), "Do it yourself.")
        self.assertEqual(w.discipline("brainstorm", {"superpowers": False}),
                         "Do it yourself.")
        self.assertEqual(w.wants_skill("brainstorm"), "superpowers:brainstorming")

    def test_a_disabled_stage_is_skipped_and_the_run_still_finishes(self):
        self._manifest(self.BASE % ", enabled: false")
        with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
            fh.write('fast:\n  - "python3 -c \'import app; assert app.add(1,2)==3\'"\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "cfg"], self.t.repo)
        script, _ = TestEndToEnd._script(self)
        task = store.Task.create(self.t.repo, "T-WF", "# t\n\nAdd a subtract API function\n",
                                 self.reg["policy"]["limits"])
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=logs.append).run()
        self.assertEqual(status, "done", "\n".join(logs))
        self.assertTrue(any("review: disabled by workflow" in x for x in logs),
                        "the skip was silent:\n%s" % "\n".join(logs))
        self.assertFalse([h for h in task.state["delegation_history"]
                          if h.get("role") == "reviewer"],
                         "a disabled stage still dispatched its agent")

    def test_a_workflow_with_no_card_for_a_role_omits_the_line(self):
        """A stage whose method is entirely a foreign skill has no card of its
        own. Pointing the agent at a path that does not exist sends it hunting
        for a file instead of working."""
        w = self._manifest("""name: cardless
stages:
  implement: {role: implementer}
""")
        self.assertIsNone(w.card("implementer"))
        self.assertIsNone(w.protocol())
        task = store.Task.create(self.t.repo, "T-WF2", "# t\n\nx\n",
                                 self.reg["policy"]["limits"])
        text = prompts.compose("implementer", task, subtask={"id": "s", "goal": "g"})
        self.assertNotIn("role card", text)
        self.assertNotIn("PROTOCOL.md", text)
        self.assertIn("Task directory", text, "the dynamic facts must survive")

    def test_a_directory_without_a_manifest_is_refused_by_name(self):
        empty = tempfile.mkdtemp(prefix="nowf-")
        try:
            with self.assertRaises(wf.WorkflowError) as cm:
                wf.Workflow.load(empty)
            self.assertIn(empty, str(cm.exception), "the error does not say where it looked")
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_a_manifest_with_no_stages_is_refused(self):
        with self.assertRaises(wf.WorkflowError):
            self._manifest("name: empty\n")

    def test_two_stages_may_not_give_one_role_different_cards(self):
        """`brainstorm` and `plan` both dispatch the planner, and the lookup
        answered with the first match — so a second declaration was silently
        dead and repointing `plan.card` alone changed nothing.

        Refused when the manifest LOADS, not when the role is first dispatched.
        `card()` is reached from `prompts.compose`, stages into a run and after
        real spend, and a WorkflowError there arrives at the generic handler as
        CRASHED — for a typo in a file that was read before the run started.
        `cli.main` resolves `--workflow` ahead of every subcommand precisely so
        a bad manifest fails with its path in the message instead.
        """
        with self.assertRaises(wf.WorkflowError) as cm:
            self._manifest("""name: clash
stages:
  brainstorm: {role: planner, card: roles/planner.md}
  plan: {role: planner, card: roles/reviewer.md}
""")
        self.assertIn("planner", str(cm.exception))
        self.assertIn("brainstorm", str(cm.exception), "the error does not say which stages")

    def test_a_stage_the_manifest_never_mentions_is_not_off(self):
        """`enabled()` answered False for an undeclared stage while
        `machine.run` only ever asked about declared ones — two answers to one
        question. The machine owns the graph: a manifest switches a stage off
        by saying so, and silence leaves it as the machine has it. Had they
        stayed apart, the first manifest written without a `review:` block
        would have been told review was off and got a review anyway."""
        w = self._manifest("""name: partial
stages:
  intake: {role: intake}
  implement: {role: implementer, card: roles/implementer.md}
""")
        self.assertTrue(w.enabled("review"), "an undeclared stage read as off")
        self.assertTrue(w.enabled("implement"))

    def test_a_stage_outside_the_order_has_no_next(self):
        """`next_enabled` fell back to the whole list for an id it could not
        place, which returns the FIRST stage — a run sent back to `intake` and
        around again. Unreachable from `machine.run`, which only asks about a
        stage it is standing in; it stays a trap for the next caller."""
        w = self._manifest("""name: partial
stages:
  intake: {role: intake}
  implement: {role: implementer, card: roles/implementer.md}
""")
        self.assertEqual(w.next_enabled("intake"), "implement")
        self.assertEqual(w.next_enabled("review"), "done",
                         "an unplaceable stage sent the run backwards")

    def test_skipping_a_disabled_stage_can_still_reach_an_undeclared_one(self):
        """`enabled()` treats an undeclared stage as on, but `next_enabled`
        walked the manifest's declared stages alone and so could never route to
        one — the same two-answers-to-one-question shape. A manifest that
        disables `review` and never mentions `integrate` skipped straight to
        `done`, ending the run without the stage that writes the patch."""
        from adg.machine import STAGES
        w = self._manifest("""name: partial
stages:
  plan: {role: planner, card: roles/planner.md}
  implement: {role: implementer, card: roles/implementer.md}
  review: {role: reviewer, card: roles/reviewer.md, enabled: false}
""")
        self.assertEqual(w.next_enabled("review"), "done",
                         "the manifest's own order ends at review")
        self.assertEqual(w.next_enabled("review", order=STAGES), "integrate",
                         "the run would end before the stage that lands the work")

    def test_a_run_skipping_a_disabled_review_still_reaches_integrate(self):
        """The same property from the machine's side, which is where it is load
        bearing: `machine.run` has to walk STAGES when it skips, or a manifest
        that switches review off and never mentions integrate ends the run
        without the stage that writes the patch."""
        self._manifest("""name: partial
protocol: PROTOCOL.md
stages:
  intake: {role: intake}
  classify: {role: classifier}
  plan: {role: planner, card: roles/planner.md}
  implement: {role: implementer, card: roles/implementer.md}
  review: {role: reviewer, card: roles/reviewer.md, enabled: false}
""")
        _spit(os.path.join(self.t.repo, ".adg.yaml"),
              'fast:\n  - "python3 -c \'import app; assert app.add(1,2)==3\'"\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "cfg"], self.t.repo)
        script, _ = TestEndToEnd._script(self)
        task = store.Task.create(self.t.repo, "T-WFI",
                                 "# t\n\nAdd a subtract API function\n",
                                 self.reg["policy"]["limits"])
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=logs.append).run()
        self.assertEqual(status, "done", "\n".join(logs))
        self.assertTrue(os.path.exists(task.file("integrate.patch")),
                        "the run finished without landing anything:\n"
                        + "\n".join(logs))

    def _with_bad_env_workflow(self, body, manifest=None):
        old = os.environ.get(wf.ENV_VAR)
        where = os.path.join(self.dir, "broken")
        os.makedirs(where, exist_ok=True)
        if manifest is not None:
            with open(os.path.join(where, "workflow.yaml"), "w") as fh:
                fh.write(manifest)
        os.environ[wf.ENV_VAR] = where
        wf._CURRENT = None                  # or the cache answers for it
        try:
            return body()
        finally:
            if old is None:
                os.environ.pop(wf.ENV_VAR, None)
            else:
                os.environ[wf.ENV_VAR] = old

    def test_a_bad_workflow_in_the_environment_stops_a_run_before_it_starts(self):
        """`cli.main` guarded the early load on `--workflow` alone, so the
        bundled default and $AGENT_DELEGATION_WORKFLOW were left to
        `wf.current()` — called from `Orchestrator.__init__`, outside every
        handler. A bad env var let `delegate run` create the task directory and
        then die with a raw traceback: the crash-instead-of-a-typo outcome the
        early load exists to prevent."""
        def body():
            with self.assertRaises(SystemExit) as cm:
                cli.main(["--repo", self.t.repo, "run", "do a thing"])
            self.assertIn("refusing to run", str(cm.exception))
            self.assertIn("broken", str(cm.exception), "the message omits the path")
        self._with_bad_env_workflow(body)

    def test_an_unparseable_manifest_is_caught_like_a_missing_one(self):
        """`yamlite.YamlError` is a sibling ValueError, not a WorkflowError, so
        catching only the latter left the commonest typo of the two — a
        manifest that exists and does not parse — crashing with a traceback."""
        def body():
            with self.assertRaises(SystemExit) as cm:
                cli.main(["--repo", self.t.repo, "run", "do a thing"])
            self.assertIn("refusing to run", str(cm.exception))
        self._with_bad_env_workflow(body, manifest="stages:\n  intake: {role: x\n")

    def test_the_reporting_commands_survive_a_broken_workflow(self):
        """`status`, `show` and `channels --clear` are how a user finds out what
        went wrong and unsticks a seat. Refusing to run them over an unrelated
        $AGENT_DELEGATION_WORKFLOW would take away the tools for the recovery,
        and none of them dispatches an agent."""
        import contextlib
        import io

        def body():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.main(["--repo", self.t.repo, "status"])   # must not raise
            return buf.getvalue()

        out = self._with_bad_env_workflow(body)
        self.assertIn("warning:", out, "the failure was not reported at all")
        self.assertIn("no tasks", out, "status did not do its job")

    def test_a_stage_with_no_body_is_refused_rather_than_read_two_ways(self):
        """`review:` with nothing under it parses as None, and the accessors
        disagreed about it: `enabled` called it off, `discipline` called it
        empty. Same trade `yamlite` makes — a config that silently parses to
        the wrong shape is worse than one that will not load."""
        with self.assertRaises(wf.WorkflowError) as cm:
            self._manifest("name: empty\nstages:\n  intake: {role: intake}\n  review:\n")
        self.assertIn("review", str(cm.exception))

    def test_the_schemas_do_not_move_with_the_workflow(self):
        """The report envelope is how the RUNTIME reads a result, so it is the
        runtime's contract: a hosted workflow must not have to ship a copy.

        It was also frozen at import time, so a workflow with no `schemas/`
        validated fine in-process and would have raised FileNotFoundError into
        the generic handler under the real CLI.
        """
        bundled = schema.schemas_dir()
        self._manifest("name: cardless\nstages:\n  implement: {role: implementer}\n")
        self.assertEqual(schema.schemas_dir(), bundled,
                         "--workflow moved the runtime's own contract")
        self.assertNotIn(self.dir, bundled)
        # and validation still works under that workflow
        schema.validate_report({"stage": "implement", "role": "implementer",
                                "status": "complete", "summary": "did it",
                                "evidence": {"tests": "ok"}})

    def test_the_reviewer_is_sent_to_the_runtimes_schemas_not_the_workflows(self):
        """The same contract, from the side that names it in a prompt. The
        review prompt built both schema paths from `prompts.skill_path()`, so
        under any workflow but the bundled one it sent the reviewer to files
        that need not exist — and put a second, different absolute path for
        report.schema.json in the same prompt `compose` had already named
        correctly. Under the bundled workflow the two are one string, which is
        why this has to be asserted from a foreign workflow directory.
        """
        self._manifest(self.BASE % "")
        self.assertFalse(os.path.isdir(os.path.join(self.dir, "schemas")),
                         "the fixture ships schemas, so it proves nothing")
        _spit(os.path.join(self.t.repo, ".adg.yaml"), 'fast:\n  - "true"\n')
        task = store.Task.create(self.t.repo, "T-WS", "# t\n\n- **AC-1** — works\n",
                                 dict(self.reg["policy"]["limits"]))
        task.update(status="review", classification={"tier": "complex"},
                    subtasks=[{"id": "st-1-main", "status": "complete",
                               "actual_files": ["app.py"], "planned_scope": ["**"]}])
        orch = Orchestrator(task, self.reg, runtime.MockAdapter(),
                            lambda k, t: True, log=lambda *_: None)
        with self.assertRaises(Halt):
            orch._stage_review()          # no verdict from the mock
        text = "".join(_slurp(task.file("agent-logs", n))
                       for n in os.listdir(task.file("agent-logs"))
                       if n.startswith("reviewer-"))
        self.assertIn(os.path.join(schema.schemas_dir(), "report.schema.json"), text)
        self.assertNotIn(os.path.join(self.dir, "schemas"), text,
                         "the reviewer was sent to a schemas directory this "
                         "workflow does not have")

    def test_a_design_report_is_a_legal_report(self):
        """`_stage_brainstorm` dispatches a planner and `compose` tells it to
        write a report, but `brainstorm` was not in the stage enum — so a design
        stage that succeeded was recorded as blocked."""
        schema.validate_report({"stage": "brainstorm", "role": "planner",
                                "status": "complete", "summary": "designed it",
                                "evidence": {"not_verified": ["nothing is built yet"]}})

    def test_a_finding_against_st_11_does_not_reopen_st_1(self):
        """`"st-1" in "st-11"` is true, so a finding against one subtask sent a
        different, already-green one back to be rebuilt."""
        owns = Orchestrator._owned_by
        self.assertFalse(owns("st-11", "st-1"))
        self.assertTrue(owns("st-1", "st-1"))
        # both forms the schema documents ("e.g. impl:st-2")
        self.assertTrue(owns("impl:st-2", "st-2"))
        self.assertFalse(owns("impl:st-2", "st-2x"))
        self.assertFalse(owns("", "st-1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
