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
            name = "alpha" if "st-1" in cwd else "beta"
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
        pol = dict(self.reg["policy"]["limits"],
                   escalation_ceiling=self.reg["policy"]["escalation_ceiling"])
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
            if "st-2" in cwd:
                raise RuntimeError("beta agent exploded")
            with open(os.path.join(cwd, "alpha.py"), "w") as fh:
                fh.write("VALUE = 1\n")

        script = {"planner": planner, "implementer": implementer,
                  "test-author": lambda e, c: None}
        pol = dict(self.reg["policy"]["limits"],
                   escalation_ceiling=self.reg["policy"]["escalation_ceiling"])
        task = store.Task.create(self.t.repo, "T-PF", "# t\n\nAdd alpha and beta\n", pol)
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=lambda *_: None).run()
        self.assertEqual(status, "needs_human")
        self.assertFalse(os.path.exists(task.file("integrate.patch")))


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
        self.pol = dict(self.reg["policy"]["limits"],
                        escalation_ceiling=self.reg["policy"]["escalation_ceiling"])

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
            if "st-1" in cwd:
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
            def _invoke(self, role, choice, cwd, **kw):
                out = Orchestrator._invoke(self, role, choice, cwd, **kw)
                sub = kw.get("subtask") or {}
                if sub.get("id") == "st-1-alpha":
                    sibling_reported.set()          # my report is now the latest
                elif sub.get("id") == "st-2-beta":
                    sibling_reported.wait(10)       # let it land before I read mine
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
        h = H(workspace="w1", panes=False)
        s = h.start_agent("implementer", "claude", "/tmp",
                          {"AGENT_DELEGATION_TASK_DIR": "/x/T-1"})
        self.assertFalse((s.handle or {}).get("herdr"))
        self.assertIn("--output-format", " ".join(s.handle["argv"]),
                      "the subprocess path is what reports total_cost_usd")

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

    SKILL = os.path.join(REPO_ROOT, "agent-delegation")

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
        report = json.load(open(os.path.join(self.SKILL, "schemas", "report.schema.json")))
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
        ref = open(os.path.join(self.SKILL, "references", "deviations.md")).read()
        impl = self._card("implementer")
        self.assertIn("One exception:", ref, "the carve-out belongs with the rule")
        self.assertNotIn("Small mechanical exceptions", impl,
                         "implementer.md restates the severity rule differently again")

    def test_task_md_authority_matches_what_the_planner_is_told(self):
        skill = open(os.path.join(self.SKILL, "SKILL.md")).read()
        self.assertNotIn("amended only by humans", skill)
        self.assertIn("planner may add criteria", skill)
        self.assertIn("missing entirely", self._card("planner"))


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
        rounds = [json.load(open(task.file("reports", "archive", a)))["round"]
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
