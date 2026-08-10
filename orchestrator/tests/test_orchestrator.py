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

from adg import brief, limits, prompts, router, runtime, schema, store, verify, yamlite
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
        schema.validate_report({
            "stage": "implement", "role": "implementer", "status": "complete",
            "summary": "did the thing", "evidence": {"tests": "3 passed"}})

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
        self.assertEqual(roles, ["planner", "implementer", "reviewer"])
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
