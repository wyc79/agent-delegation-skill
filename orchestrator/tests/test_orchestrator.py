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
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adg import (brief, cli, limits, prompts, router, runtime, schema, store,
                 verify, winnow, yamlite)
from adg.machine import Halt, Orchestrator

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
        task.update(status="planning")
        boom = RuntimeError("killed mid-write")

        def explode(state):
            state["status"] = "implementing"
            raise boom

        with self.assertRaises(RuntimeError):
            task.mutate(explode)
        reread = store.Task(task.path).state
        self.assertEqual(reread["status"], "planning", "a partial write was visible")
        self.assertEqual(len(json.dumps(reread)) > 0, True)


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
        # rung 2 of the ladder (DESIGN §6.2) is exactly a reasoning+1 re-route.
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


class TestRungThree(unittest.TestCase):
    """DESIGN §6.2 rung 3. Rung 2 is "skipped entirely if nothing enrolled sits
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
                      "§12: a replan that cannot see finished work re-plans it")

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
        # The Windows/macOS rule, asserted directly rather than by platform.
        self.assertTrue(verify.scopes_overlap(["src/Foo.cs"], ["src/foo.cs"])
                        or sys.platform.startswith("linux"))
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
        # §6.2 rung 2. The shipped registry enrols opus-class-strong for
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
        # §6.2: plan_conflict enters at rung 3 directly -- a stronger
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
    """Milestone 1 (DESIGN.md §15.1)."""

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
        self.assertTrue(os.path.isdir(wt))
        self.assertNotIn("def subtract", _slurp(os.path.join(self.t.repo, "app.py")),
                         "change leaked into the user's checkout")
        self.assertIn("def subtract", _slurp(os.path.join(wt, "app.py")))

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
        self.assertNotIn("reviewer", [c[1] for c in adapter.calls if c[0] != "notify"])
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
