"""Tests for the MVP orchestrator.

The end-to-end test is the important one: it drives the real state machine
over a real git repository with a scripted adapter, so the pipeline is proven
without spending a token on a model.

Run: python3 orchestrator/tests/test_orchestrator.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adg import brief, limits, prompts, router, runtime, schema, store, verify, winnow, yamlite
from adg.machine import Orchestrator

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

    def test_task_json_survives_a_crash_midwrite(self):
        task = store.Task.create(self.t.repo, "T-001", "# t\n", {"max_cost_usd": 1})
        task.update(status="planning")
        self.assertEqual(task.state["status"], "planning")
        self.assertEqual(len(os.listdir(task.path)), len(set(os.listdir(task.path))))


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


def _report(cwd_task, name, payload):
    with open(os.path.join(cwd_task, "reports", name), "w") as fh:
        json.dump(payload, fh)


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
        roles = [h["role"] for h in state["delegation_history"]]
        self.assertEqual(roles, ["planner", "test-author", "implementer", "reviewer"])
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
        self.assertNotIn("def subtract", open(os.path.join(self.t.repo, "app.py")).read(),
                         "change leaked into the user's checkout")
        self.assertIn("def subtract", open(os.path.join(wt, "app.py")).read())

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

    def test_unparseable_plan_parks_rather_than_guessing(self):
        script, _ = self._script()
        script["planner"] = lambda env, cwd: open(
            os.path.join(env["AGENT_DELEGATION_TASK_DIR"], "plan.md"), "w").write("no yaml here")
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
