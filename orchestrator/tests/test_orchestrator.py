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
                 store, verify, workflow as wf, yamlite)
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
        self.assertIs(reg["models"]["ultra-reasoner"]["enrolled"], False)
        self.assertEqual(reg["policy"]["escalation_ceiling"]["max_tier"], "t3")
        self.assertIn("balanced-coder", reg["channels"]["cursor-seat"]["exposes"])

    def test_flow_collections_and_types(self):
        d = yamlite.load("a: {x: 1, y: [p, q]}\nb: true\nc: null\nd: 2.5\ne: 'x: y'\n")
        self.assertEqual(d["a"], {"x": 1, "y": ["p", "q"]})
        self.assertIs(d["b"], True)
        self.assertIsNone(d["c"])
        self.assertEqual(d["d"], 2.5)
        self.assertEqual(d["e"], "x: y")

    def test_block_scalars_in_every_chomping_form(self):
        # A live planner emitted `test_notes: >-` and the whole plan failed to
        # parse. All block-scalar forms must work.
        for style in (">", ">-", ">+", "|", "|-", "|+"):
            d = yamlite.load("a: %s\n  one line\n  two line\nb: 2\n" % style)
            joiner = "\n" if style.startswith("|") else " "
            self.assertEqual(d["a"], "one line%stwo line" % joiner, style)
            self.assertEqual(d["b"], 2, style)

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
        task = _task(self.t.repo, "T-001", "# t\n", {"max_cost_usd": 1})
        self.assertFalse(task.path.startswith(os.path.realpath(self.t.repo)))
        out = subprocess.run(["git", "status", "--porcelain"], cwd=self.t.repo,
                             capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "", "task creation dirtied the working tree")

    def test_a_crash_midwrite_leaves_the_previous_state_readable(self):
        # The old version of this test simulated no crash and asserted a
        # property true of every directory. It would have passed with atomic
        # writes removed.
        task = _task(self.t.repo, "T-001", "# t\n", {"max_cost_usd": 1})
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
                     "max_parallel_agents": 2}

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
        task = _task(self.t.repo, "T-001", "# t\n", self.full)
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
                           "cost_out": 75, "enrolled": True},
                "worker": {"tier": "t2", "reasoning": 4, "coding": 5,
                           "adherence": 4, "tool": 5, "speed": 4, "ctx": 200000,
                           "cost_out": 15, "enrolled": True},
            },
            "profiles": {"worker": {"require": {"coding": 4, "tool": 4},
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
        top = router.Router(self._reg()).candidates()[0]
        self.assertEqual(top.channel, "aaa-seat")

    def test_a_preference_decides_which_seat_serves_the_model(self):
        reg = self._reg(zzz_seat={"prefers": ["worker"]})
        cands = router.Router(reg).candidates()
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
        top = router.Router(reg).candidates()[0]
        self.assertEqual(top.model, "worker")
        self.assertEqual(top.channel, "zzz-seat")

    def test_a_preferred_seat_that_is_cooled_still_yields(self):
        """A preference, not a pin. Moving work off a walled seat is the whole
        point of the project, and a routing preference must not undo it."""
        reg = self._reg(zzz_seat={"prefers": ["worker"]})
        cands = router.Router(reg).candidates(cooldowns={"zzz-seat"})
        self.assertEqual([c.channel for c in cands], ["aaa-seat"])

    def test_a_preferred_seat_that_is_drawn_down_still_yields(self):
        """Utilisation is a real signal and outranks a stated preference: the
        bonus is a tie-break, so once the shadow price separates the two seats
        the cheaper one wins regardless of what the manifest would rather."""
        reg = self._reg(zzz_seat={"prefers": ["worker"]})
        cands = router.Router(reg).candidates(
            utilization={"zzz-seat": 0.95, "aaa-seat": 0.0})
        self.assertEqual(cands[0].channel, "aaa-seat",
                         "a preference held a seat that had priced itself out")

    def test_the_shipped_registry_gives_every_tier_a_provider(self):
        """The table a calling skill picks from. It could not exist while two
        models shared t2: `tier: t2` named both, the score took the cheaper
        every time, and "the default provider for t2" had no answer."""
        r = router.Router(router.load_registry(REGISTRY))
        got = {t: r.candidates(tier=t)[0] for t in ("t1", "t2", "t3")}
        self.assertEqual(got["t1"].model, "fast-cheap")
        self.assertEqual(got["t2"].model, "balanced-coder")
        self.assertEqual(got["t3"].model, "opus-class-strong")
        self.assertEqual(got["t1"].channel, "cursor-seat")
        self.assertEqual(got["t2"].channel, "cursor-seat")
        self.assertEqual(got["t3"].channel, "claude-seat")
        self.assertGreater(len({c.channel for c in got.values()}), 1,
                           "every tier resolved to one seat, which is the "
                           "indirection SKILL.md tells the caller not to pay for")

    def test_a_tier_is_a_band_and_never_a_floor(self):
        """`tier: t1` must reach the cheap seat. With a floor it never would:
        the profile ranks the workhorse above the cheap model on every axis it
        weighs, so anything at-or-above t1 resolves to t2 and t1 is unreachable
        — which would quietly make the cheapest band cost the most."""
        r = router.Router(router.load_registry(REGISTRY))
        self.assertEqual([c.model for c in r.candidates(tier="t1")], ["fast-cheap"])
        self.assertNotIn("balanced-coder",
                         [c.model for c in r.candidates(tier="t1")])

    def test_the_strong_seat_keeps_its_tier_when_a_rival_appears(self):
        """`claude-seat: prefers: [opus-class-strong]` decides nothing today —
        no other shipped seat exposes that model. It is not decoration: enrol a
        second strong seat and the tie returns, and without the line it falls to
        the alphabet again, which is the failure the mechanism replaced."""
        reg = router.load_registry(REGISTRY)
        reg["channels"]["aaa-strong-seat"] = {
            "type": "subscription", "adapter": "herdr", "agent_kind": "codex",
            "exposes": ["opus-class-strong"],
            "quota": {"window": "5h", "est_capacity": 40}}
        self.assertEqual(router.Router(reg).candidates(tier="t3")[0].channel,
                         "claude-seat",
                         "a newly enrolled seat took the tier purely by sorting "
                         "earlier than the seat the registry prefers")

    def test_preferring_a_model_the_seat_cannot_serve_is_refused(self):
        """A line that reads like a policy and routes nothing is the shape of
        mistake this registry refuses elsewhere. Caught at load, so `--registry`
        fails with the path rather than three stages into a run."""
        d = tempfile.mkdtemp(prefix="reg-")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "r.yaml")
        with open(p, "w") as fh:
            fh.write("""models:
  worker: {tier: t2, coding: 5, tool: 5, enrolled: true}
profiles:
  worker: {require: {}, weights: {coding: 3}}
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

class TestRouter(unittest.TestCase):
    def setUp(self):
        self.reg = router.load_registry(REGISTRY)
        self.r = router.Router(self.reg)

    def test_ultra_tier_unreachable_by_default(self):
        for role in ("implementer", "integrator"):
            for c in self.r.candidates(role):
                self.assertNotEqual(c.model, "ultra-reasoner")

    def test_enrollment_alone_does_not_unlock_ultra(self):
        self.reg["models"]["ultra-reasoner"]["enrolled"] = True
        self.reg["channels"]["claude-seat"]["exposes"].append("ultra-reasoner")
        picked = [c.model for c in self.r.candidates("implementer",
                                                     ceiling={"max_tier": "t2"})]
        self.assertNotIn("ultra-reasoner", picked, "ceiling must still block it")

    def test_both_switches_unlock_ultra(self):
        """Two independent switches, on purpose: enrolling it is not enough, and
        raising the ceiling is not enough. Ultra is scored and present and off."""
        self.reg["channels"]["claude-seat"]["exposes"].append("ultra-reasoner")
        self.assertNotIn("ultra-reasoner",
                         [c.model for c in self.r.candidates(ceiling={"max_tier": "t3"})],
                         "the ceiling alone reached a model nobody enrolled")
        self.reg["models"]["ultra-reasoner"]["enrolled"] = True
        self.assertNotIn("ultra-reasoner",
                         [c.model for c in self.r.candidates(ceiling={"max_tier": "t2"})],
                         "enrolment alone reached past the ceiling")
        self.assertIn("ultra-reasoner",
                      [c.model for c in self.r.candidates(ceiling={"max_tier": "t3"})])

    def test_no_model_error_names_the_fix(self):
        with self.assertRaises(router.NoModelAvailable) as cm:
            self.r.select(ceiling={"max_tier": "t1"}, tier="t3")
        self.assertIn("registry.default.yaml", str(cm.exception))
        self.assertIn("t3", str(cm.exception), "the message does not say which "
                      "tier could not be served")

    def test_bad_ceiling_refuses_rather_than_defaulting_open(self):
        with self.assertRaises(router.RoutingError):
            self.r.candidates("implementer", ceiling={"max_tier": "unlimited"})

    def test_the_workhorse_survives_a_drawn_down_seat(self):
        # The cheap model has a second seat and the strong one does not, so a
        # filling window widens the gap rather than closing it.
        for u in (0.5, 0.9):
            self.assertEqual(
                self.r.select("implementer", utilization={"claude-seat": u}).model,
                "balanced-coder", "at %.0f%% draw" % (u * 100))

    def test_an_unscored_capability_cannot_clear_a_boosted_floor(self):
        self.reg["models"]["balanced-coder"].pop("reasoning")
        r = router.Router(self.reg)
        picked = [c.model for c in r.candidates("implementer", boost={"reasoning": 3})]
        self.assertNotIn("balanced-coder", picked,
                         "an unscored dimension is unverifiable, not a pass")

    def test_boosting_a_declared_requirement_still_takes_the_higher_floor(self):
        # The original behaviour, which must survive: require 4, boost 5 -> 5.
        picked = [c.model for c in self.r.candidates("implementer", boost={"reasoning": 5})]
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

class TestBriefLint(unittest.TestCase):
    def test_bare_jargon_is_caught(self):
        problems = brief.lint("We satisfied AC-2 and st-3 ended NEEDS_HUMAN.")
        self.assertGreaterEqual(len(problems), 3)

    def test_expanded_ids_are_allowed(self):
        clean = ("The requirement that old saves still load (AC-2) is met by the "
                 "save-migration step (st-3).")
        self.assertEqual(brief.lint(clean), [])

    def test_an_id_column_is_not_jargon(self):
        """The rule is about prose leaning on an id the reader was never told
        the meaning of. An identifier column has nothing to expand it into, and
        linting it would mean the scope table could not name its own jobs."""
        self.assertEqual(brief.lint("| Job | Files |\n|---|---|\n| st-3 | `a.py` |"), [])

    def test_the_exemption_does_not_cover_prose_inside_a_row(self):
        problems = brief.lint("| Job | Note |\n|---|---|\n| st-3 | blocked on st-4 |")
        self.assertTrue(problems, "a bare id in a sentence escaped through a table")


class TestBriefCarriesTheScopeMeasurement(unittest.TestCase):
    """Scope is the whole of what `delegate` measures, and it has to reach the
    human. It was computed per job into task.json, logged to the run's stdout,
    and then dropped: the brief flattened every changed file into one
    alphabetical list with no job column and no way to see that a file was
    written outside the boundary its job declared — while the paragraph printed
    beside it pointed at "the scope column above" as the evidence to weigh.
    """

    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)

    def tearDown(self):
        self.t.close()

    def _brief(self, subtasks):
        task = _task(self.t.repo, "T-SC", "# t\n\ndo it\n",
                     self.reg["policy"]["limits"])
        task.update(subtasks=subtasks)
        return brief.render(task, "merge", "Land it?")

    def test_a_violation_is_named_against_the_job_that_wrote_it(self):
        out = self._brief([
            {"id": "st-1-a", "actual_files": ["a.py", "build.gradle"],
             "scope_violations": ["build.gradle"]},
            {"id": "st-2-b", "actual_files": ["b.py"], "scope_violations": []},
        ])
        self.assertIn("Outside its scope", out, "no scope column")
        row = [l for l in out.splitlines() if l.startswith("| st-1-a")][0]
        self.assertIn("build.gradle", row.split("|")[3],
                      "the violation is not attributed to the job that wrote it")
        clean = [l for l in out.splitlines() if l.startswith("| st-2-b")][0]
        self.assertIn("—", clean.split("|")[3],
                      "a job that stayed in scope is not shown as clean")
        self.assertIn("Nothing was reverted", out,
                      "the brief implies the violation was undone")

    def test_a_clean_run_says_so_without_a_warning(self):
        out = self._brief([{"id": "st-1-a", "actual_files": ["a.py"],
                            "scope_violations": []}])
        self.assertIn("| st-1-a", out)
        self.assertNotIn("were written outside", out,
                         "a clean run is reported as if something escaped")


# ---------------------------------------------------------------------------
# End to end: supplied jobs -> isolated implementation -> verify -> integrate
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

def _task(repo, task_id, request, limits, mode="attended", plan=None):
    """Create a task with its jobs already in place.

    Every end-to-end test used to get `plan.md` from a scripted planner. There
    is no planner: the caller supplies the decomposition, so a test that wants a
    run to do anything has to supply one too. `plan=None` means the default
    single-job plan; pass a string for a different shape, or `False` to create a
    task with no jobs at all, which is its own thing worth testing.
    """
    task = store.Task.create(repo, task_id, request, limits, mode=mode)
    if plan is not False:
        task.write_text("plan.md", plan if plan is not None else PLAN_MD)
    return task



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


class TestChannelAvailability(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)

    def tearDown(self):
        self.t.close()

    def test_uninstalled_cli_is_skipped_not_crashed_on(self):
        task = _task(self.t.repo, "T-001", "# t\n",
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
        task = _task(self.t.repo, "T-C5", "# t\n", self.pol)

        class Metered(runtime.MockAdapter):
            def prompt(self, session, text, timeout):
                r = runtime.MockAdapter.prompt(self, session, text, timeout)
                r["usage"], r["elapsed_ms"] = {"in": 100, "out": 20}, 4242
                return r

        Orchestrator(task, self.reg, Metered({"implementer": lambda e, c: None}),
                     lambda k, t: True, log=lambda *_: None).run()
        history = task.state["delegation_history"]
        self.assertTrue(history, "nothing was delegated")
        self.assertEqual(history[0]["tokens"], {"in": 100, "out": 20})
        self.assertEqual(history[0]["elapsed_ms"], 4242)

    def test_spend_accumulates_and_then_parks_the_run(self):
        task = _task(self.t.repo, "T-C1", "# t\n", dict(self.pol, max_cost_usd=0.5))

        class Pricey(runtime.MockAdapter):
            def prompt(self, session, text, timeout):
                r = runtime.MockAdapter.prompt(self, session, text, timeout)
                r["cost_usd"] = 0.6
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
        task = _task(self.t.repo, "T-C2", "# t\n\nAdd subtract\n",
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

    # --- what a run is allowed to pay for ---------------------------------
    def test_a_run_dispatches_nothing_but_the_work(self):
        """The wrapper property, asserted where it can regress: an agent call
        is money, and every role this once had -- planner, test-author,
        classifier, reviewer, reporter -- was a call the caller did not ask
        for. A clean run buys implementer turns and nothing else."""
        script, _ = TestEndToEnd._script(self)[0], None
        task = _task(self.t.repo, "T-C3", "# t\n\nAdd subtract\n", self.pol)
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=lambda *_: None).run()
        self.assertEqual(status, "done")
        roles = {h["role"] for h in task.state["delegation_history"]}
        self.assertEqual(roles, {"implementer"},
                         "a run bought turns nobody asked for: %s" % sorted(roles))

    def _e2e_script(self):
        return TestEndToEnd._script(self)

    # --- reporter ---------------------------------------------------------
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
        task = _task(self.t.repo, "T-P", "# t\n", dict(self.pol,
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
        """The caller writes `depends_on`, so a dependency on an id it never
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

        def implementer(env, cwd):
            with lock:
                seen_cwds.add(cwd)
            # Each writes only its own file, so the merges must be clean.
            name = "alpha" if "st-1" in _tree_name(cwd) else "beta"
            with open(os.path.join(cwd, "%s.py" % name), "w") as fh:
                fh.write("VALUE = %r\n" % name)

        script = {"implementer": implementer}
        # A parallel test pins its own concurrency rather than inheriting the
        # registry's: when the registry shipped max_parallel_agents: 1, every
        # one of these silently became a sequential run that still passed, and
        # a future tuning-down must not do that again.
        pol = dict(self.reg["policy"]["limits"],
                   escalation_ceiling=self.reg["policy"]["escalation_ceiling"],
                   max_parallel_agents=2)
        task = _task(self.t.repo, "T-PAR", "# t\n\nAdd alpha and beta\n", pol, plan=self.PLAN)
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
        def implementer(env, cwd):
            if "st-2" in _tree_name(cwd):
                raise RuntimeError("beta agent exploded")
            with open(os.path.join(cwd, "alpha.py"), "w") as fh:
                fh.write("VALUE = 1\n")

        script = {"implementer": implementer}
        # A parallel test pins its own concurrency rather than inheriting the
        # registry's: when the registry shipped max_parallel_agents: 1, this
        # silently became a sequential run that still passed, and a future
        # tuning-down must not do that again.
        pol = dict(self.reg["policy"]["limits"],
                   escalation_ceiling=self.reg["policy"]["escalation_ceiling"],
                   max_parallel_agents=2)
        task = _task(self.t.repo, "T-PF", "# t\n\nAdd alpha and beta\n", pol, plan=self.PLAN)
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

        script = {"implementer": implementer}
        task = _task(self.t.repo, "T-SC1", "# t\n\nAdd two things\n",
                                 self._pol(2), plan=self.INDEPENDENT)
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

        script = {"implementer": implementer}
        task = _task(self.t.repo, "T-SC2", "# t\n\nAdd three things\n",
                                 self._pol(3), plan=self.CHAINED)
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

    def test_the_second_sequential_subtask_is_credited_only_with_its_own_files(self):
        """A subtask working in the integration worktree measured its diff
        against the *task* base, which still holds every earlier subtask's
        commit — so the second one in a sequential run was credited with its
        predecessors' files and reported them as scope violations it never
        committed. Waves of one are not exotic: any dependency chain or
        overlapping scope produces them at any cap."""
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

        script = {"implementer": implementer}
        task = _task(self.t.repo, "T-SC5", "# t\n\nAdd two things\n",
                                 self._pol(1), plan=self.INDEPENDENT)
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

        script = {"implementer": implementer}
        task = _task(self.t.repo, "T-SC6", "# t\n\nAdd two things\n",
                                 self._pol(1), plan=self.INDEPENDENT)
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=logs.append).run()
        self.assertEqual(status, "needs_human",
                         "an agent that wrote nothing was delivered as done\n"
                         + "\n".join(logs))
        self.assertIn("changed no files", "\n".join(logs))
        self.assertFalse(os.path.exists(task.file("integrate.patch")))

    def test_a_reused_worktree_is_brought_up_to_the_integration_branch(self):
        """`_catch_up`, tested directly now that nothing drives a
        rework. The mechanism still matters: a job re-dispatched into a worktree
        it already owns — a resume, or a second wave — would otherwise run
        against a snapshot from before its siblings landed, so its checks would
        be evidence about a tree that no longer exists.
        """
        task = _task(self.t.repo, "T-CU", "# t\n\ndo\n", self._pol(2),
                     plan=self.INDEPENDENT)
        orch = Orchestrator(task, self.reg, runtime.MockAdapter({}),
                            lambda k, t: True, log=lambda *a: None)
        integration = orch._ensure_worktree()

        # A sibling lands beta.py on the integration branch.
        with open(os.path.join(integration, "beta.py"), "w") as fh:
            fh.write("BETA = 1\n")
        sh(["git", "add", "-A"], integration)
        sh(["git", "commit", "-qm", "sibling landed"], integration)

        # st-1's own checkout was cut before that and cannot see it.
        branch = "adg/%s/st-1-alpha" % task.state["id"]
        tree = os.path.join(self.t.dir, "st1")
        sh(["git", "worktree", "add", "-b", branch, tree,
            store.git(["rev-parse", "HEAD^"], integration).strip()], self.t.repo)
        self.assertFalse(os.path.isfile(os.path.join(tree, "beta.py")))

        orch._catch_up({"id": "st-1-alpha"}, tree)
        self.assertTrue(os.path.isfile(os.path.join(tree, "beta.py")),
                        "the reused worktree never saw what its sibling landed")

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

        script = {"implementer": implementer,
                  "integrator": lambda e, c: None}
        task = _task(self.t.repo, "T-SCD", "# t\n\nAdd three things\n",
                                 self._pol(3), plan=self.CONFLICTING)
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

        script = {"implementer": implementer,
                  "integrator": lambda e, c: None}
        task = _task(self.t.repo, "T-SCE", "# t\n\nAdd three things\n",
                                 self._pol(3), plan=self.CONFLICTING)
        logs = []
        first = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                             lambda k, t: True, log=logs.append).run()
        self.assertEqual(first, "needs_human", "\n".join(logs))
        by_id = {s["id"]: s for s in task.state["subtasks"]}
        for sid, s in by_id.items():
            if s.get("status") == "complete":
                self.assertTrue(s.get("merged"),
                                "%s is complete but its branch never merged" % sid)

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
        self.task = _task(self.t.repo, "T-CR", "# t\n\ndo it\n", self.pol)
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

        def pick(role=None, **kw):
            raise cooled
        orch._pick = pick
        with self.assertRaises(router.AllChannelsCooled):
            orch._reconcile({"id": "st-1"}, self.t.repo, "CONFLICT")

        # ... and an ordinary "nothing enrolled" still halts, as it always did.
        def none_available(role=None, **kw):
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
            choice = orch.router.candidates()[0]
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
    """delegate as a wrapper: the caller brings the decomposition.

    A skill that has already designed the work wants N jobs placed on N seats,
    in isolated worktrees, with metering and failover underneath — and nothing
    second-guessing the decomposition or reviewing a result it will not read.
    `--plan` is that, and it needed no new stage: the machine already recovers
    jobs from plan.md when the state has none.
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
        self.dispatch = wf.default_dir()
        self.addCleanup(setattr, wf, "_CURRENT", None)

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.t.repo,
                       capture_output=True)
        self.t.close()

    def test_the_bundled_dispatch_workflow_runs_only_execution(self):
        self.assertTrue(os.path.isdir(self.dispatch), self.dispatch)
        w = wf.use(self.dispatch)
        self.assertEqual(w.order(), ["implement", "integrate"],
                         "the manifest names a stage the machine does not have, "
                         "which reads like policy and routes nothing")
        for on in ("implement", "integrate"):
            self.assertTrue(w.enabled(on), "%s must stay on" % on)
        # `integrate` in particular: it is the stage that merges the subtask
        # branches and writes the patch, so a dispatcher without it would end
        # the run with the work stranded on branches nobody merges.
        self.assertEqual(w.next_enabled("implement", order=STAGES), "integrate")

    def test_a_supplied_plan_is_executed_and_nothing_else_is_dispatched(self):
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
                raise AssertionError("%s was dispatched; the caller asked for "
                                     "jobs, not a workflow" % role)
            return fn

        task = _task(self.t.repo, "T-DIS", "# t\n\ntwo things\n",
                                 self.pol)
        task.write_text("plan.md", self.PLAN)      # what `--plan` does
        logs = []
        status = Orchestrator(
            task, self.reg,
            runtime.MockAdapter({"implementer": implementer,
                                 "planner": refuse("planner"),
                                 "test-author": refuse("test-author"),
                                 "reviewer": refuse("reviewer"),
                                 "classifier": refuse("classifier"),
                                 "reporter": refuse("reporter")}),
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
        # And the work is accounted for per job.
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

    def _run(self, implementer, task_id="T-W", plan=None):
        task = _task(self.t.repo, task_id, "# t\n\nAdd three things\n",
                                 self.pol, plan=plan or self.PLAN)
        script = {"implementer": implementer}
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

        task = _task(self.t.repo, "T-W1", "# t\n\nAdd things\n", self.pol, plan=self.PLAN)
        logs = []
        status = Interleaved(task, self.reg,
                             runtime.MockAdapter({"implementer": implementer}),
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

        task = _task(self.t.repo, "T-W11", "# t\n\nAdd things\n", self.pol, plan=PLAN)
        logs = []
        status = Orchestrator(task, self.reg,
                              runtime.MockAdapter({"implementer": implementer}),
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
        task = _task(self.t.repo, "T-W3", "# t\n", self.pol)
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
        task = _task(self.t.repo, "T-W4", "# t\n", self.pol)
        orch = Orchestrator(task, self.reg, runtime.MockAdapter(), lambda k, t: True,
                            log=lambda *_: None)
        task.update(subtasks=[
            {"id": "st-1", "status": "pending", "planned_scope": ["alpha.py"]},
            {"id": "st-2", "status": "pending", "planned_scope": ["alpha.py", "b.py"]}])
        self.assertEqual(len(orch._wave(task.state["subtasks"])), 1)

    def test_scope_is_measured_against_the_base_not_the_checkpoint(self):
        # base_commit was unset on the wave path, so every diff was taken
        # against a HEAD that already contained the work: nothing ever looked
        # changed. `setUp` already committed the checks this needs.
        def implementer(env, cwd):
            sub = os.path.basename(cwd.rstrip("/")).split("-", 2)[-1]   # st-N-name
            name = sub.split("-")[-1]
            with open(os.path.join(cwd, "%s.py" % name), "w") as fh:
                fh.write("V = 1\n")
            _report(env["AGENT_DELEGATION_TASK_DIR"], "implement-%s.json" % sub, {
                "stage": "implement", "role": "implementer", "subtask": sub,
                "status": "complete", "summary": "ok", "evidence": {"tests": "ok"}})

        status, task, logs = self._run(implementer, task_id="T-W5")
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

    def test_panes_off_bypasses_herdr_entirely(self):
        """`--no-panes` is what trades the visible agents back for cost
        accounting, so it has to reach the adapter rather than only the label:
        a pane session reports no cost and `max_cost_usd` cannot bind while one
        is open."""
        class H(runtime.HerdrAdapter):
            def _cli(self, args, check=True):
                raise AssertionError("panes are off; herdr must not be called")

            def can_run(self, kind):
                return True        # routing is the subject; no vendor CLI needed
        s = H(workspace="w1", panes=False).start_agent(
            "implementer", "claude", "/tmp", {})
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
        task = _task(self.t.repo, "T-R", "# t\n\nAdd a thing\n", self.pol)

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
            _report(env["AGENT_DELEGATION_TASK_DIR"], "implement-st-1-subtract.json", {
                "stage": "implement", "role": "implementer",
                "subtask": "st-1-subtract", "status": "escalate",
                "summary": "No code written — wrong repository in the worktree.",
                "evidence": {"tests": "green but irrelevant"},
                "signals": [{"type": "blocked_command",
                             "detail": "the worktree is not the repo I was told",
                             "attempted": ["re-reading the task dir"]}]})
        status, task, logs = self._simple({"implementer": honest})
        self.assertEqual(status, "needs_human")
        joined = "\n".join(logs)
        self.assertIn("stopped and asked for help", joined)
        # and the caller is handed what the agent knew, not a summary of it:
        # nothing here decides what to do about a stuck job any more.
        self.assertIn("blocked_command", joined)
        self.assertIn("already tried: re-reading the task dir", joined)

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
        task = _task(self.t.repo, "T-RS", "# t\n",
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
        task = _task(self.t.repo, "T-RT", "# t\n",
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


# One provider exposing every band -- the deployment `init` has to tell a
# caller not to delegate on.
ONE_SEAT = """version: 1
models:
  fast-cheap: {tier: t1, reasoning: 2, coding: 3, adherence: 3, tool: 3, speed: 5, ctx: 1000000, cost_out: 1.5, enrolled: true}
  balanced-coder: {tier: t2, reasoning: 4, coding: 5, adherence: 4, tool: 5, speed: 4, ctx: 200000, cost_out: 15, enrolled: true}
  opus-class-strong: {tier: t3, reasoning: 5, coding: 5, adherence: 4, tool: 5, speed: 2, ctx: 200000, cost_out: 75, enrolled: true}
profiles:
  worker:
    require: {coding: 3, tool: 3}
    weights: {coding: 3, adherence: 2, speed: 1}
    cost_sensitivity: high
channels:
  claude-seat:
    type: subscription
    adapter: local
    agent_kind: claude
    exposes: [opus-class-strong, balanced-coder, fast-cheap]
    quota: {window: 5h, est_capacity: 40}
policy:
  escalation_ceiling: {max_tier: t3}
  limits:
    max_cost_usd: 15
    max_attempts_per_subtask: 8
    max_parallel_agents: 3
    human_approval_required: [merge]
"""


class TestInit(unittest.TestCase):
    """`init` is the front door, and the skill sends callers here to make one
    decision: is there more than one seat? Nothing tested it, and it drifted
    into answering a different question wrongly — it printed a per-ROLE table
    naming planner, test-author and reviewer, which no stage dispatches, and
    every row resolved to the same seat because roles stopped choosing models.
    A caller reading it would conclude one provider and stop delegating on a
    deployment with two.
    """

    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)

    def tearDown(self):
        self.t.close()

    def _init(self, registry=REGISTRY):
        import io
        import contextlib
        args = argparse.Namespace(repo=self.t.repo, registry=registry)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_init(args)
        return buf.getvalue()

    def test_it_prints_the_tier_table_the_docs_promise(self):
        out = self._init()
        for tier in router.TIERS:
            self.assertIn(tier, out, "no row for %s" % tier)
        for model in ("fast-cheap", "balanced-coder", "opus-class-strong"):
            self.assertIn(model, out, "%s serves a band and is not named" % model)
        for gone in ("planner", "test-author", "reviewer"):
            self.assertNotIn(gone, out,
                             "%r is offered a seat and is never dispatched" % gone)

    def test_it_says_outright_whether_delegating_is_worth_it(self):
        """The decision, not the raw table. A caller that has to derive it from
        three rows is a caller that gets it wrong."""
        self.assertIn("2 seats serve these tiers", self._init())

        one = os.path.join(self.t.dir, "one-seat.yaml")
        with open(one, "w", encoding="utf-8") as fh:
            fh.write(ONE_SEAT)
        out = self._init(one)
        self.assertIn("Every tier resolves to claude-seat", out)
        self.assertIn("nowhere to fail over", out)

    def test_the_unenrolled_list_reads_the_key_the_registry_writes(self):
        """It read `enrolled_roles`, which the registry stopped having when
        roles stopped choosing models — so every model, including the three
        that serve the three bands printed directly above, was reported as
        deliberately not enrolled."""
        out = self._init()
        self.assertIn("ultra-reasoner", out.split("not enrolled:")[1],
                      "the one model that really is off is not listed")
        for on in ("fast-cheap", "balanced-coder", "opus-class-strong"):
            self.assertNotIn(on, out.split("not enrolled:")[1],
                             "%s is enrolled and reported as not" % on)

    def test_it_writes_nothing(self):
        """There is no state for it to save. It used to write a `config.json`
        holding the detected companion skills and chaff scanner beside a
        registry path, and nothing ever read the file back."""
        self._init()
        self.assertFalse(os.path.exists(
            os.path.join(store.project_dir(self.t.repo), "config.json")))


class TestStatusReadsTheBreaker(unittest.TestCase):
    """`park` records why a run stopped; the breaker file records whether the
    seat is still out. `_quota_guard` was taught the difference and `status`
    was not, so a task whose window had reopened — or whose seat had been
    cleared by hand — still reported "waiting on quota" beside a reopen time
    in the past, for a task `resume` would have run immediately."""

    def setUp(self):
        self.t = TempRepo()
        self.task = _task(self.t.repo, "T-Q", "# t\n",
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


class TestRoundVersioning(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.reg = router.load_registry(REGISTRY)

    def tearDown(self):
        self.t.close()

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
        self.task = _task(self.t.repo, "T-FRESH", "# t\n",
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

    def _run(self, script, gate=lambda k, t: True, request="Add a subtract API function",
             plan=None):
        pol = dict(self.reg["policy"]["limits"])
        pol["escalation_ceiling"] = self.reg["policy"]["escalation_ceiling"]
        task = _task(self.t.repo, "T-001",
                     "# Task T-001\n\n## Request\n\n%s\n" % request, pol, plan=plan)
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
        self.assertEqual(steps, [("implement", "implementer")],
                         "delegate dispatched something other than the work: %s"
                         % steps)
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
        task = _task(self.t.repo, "T-KEEP",
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
        task = _task(self.t.repo, "T-003", "# t\n\nAdd subtract\n", pol)
        Orchestrator(task, self.reg, A(script), lambda k, t: True, log=lambda *_: None).run()
        self.assertTrue(captured, "no follow-up sent")
        self.assertIn("did not pass", captured[0])
        self.assertIn("import app", captured[0], "should quote the failing command")

    def _run_simple(self, script, review=None, break_scope=False):
        """A task the classifier calls simple, so the review policy applies."""
        pol = dict(self.reg["policy"]["limits"],
                   escalation_ceiling=self.reg["policy"]["escalation_ceiling"])
        task = _task(self.t.repo, "T-00S", "# t\n\nAdd subtract\n", pol)
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

    def test_brief_says_plainly_that_no_review_ran(self):
        script, _ = self._script()
        _, task, _, _ = self._run_simple(script)
        text = task.read_text("brief.md")
        self.assertIn("Nothing reviewed this", text,
                      "the brief lets a human read 'complete, checks pass' as "
                      "'something looked at this'")
        self.assertEqual(brief.lint(text), [], "the notice must stay jargon-free")

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
        task = _task(self.t.repo, "T-042", "# t\n\nAdd a subtract API function\n",
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
        task = _task(self.t.repo, "T-043", "# t\n\nAdd a subtract API function\n",
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
        task = _task(self.t.repo, task_id,
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
        task = _task(self.t.repo, "T-060",
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
        task = _task(self.t.repo, "T-061", "# t\n\nwhatever\n",
                                 self.reg["policy"]["limits"])
        text = prompts.compose("implementer", task,
                               subtask={"id": "st-1", "goal": "g", "planned_scope": []})
        self.assertIn("not restricted", text)
        self.assertNotIn("a hard boundary", text)

    def test_unparseable_plan_parks_rather_than_guessing(self):
        script, _ = self._script()
        status, task, _, _ = self._run(script, plan="no yaml here")
        self.assertEqual(status, "needs_human")

    def test_cost_cap_stops_the_run(self):
        script, _ = self._script()
        task = _task(
            self.t.repo, "T-002", "# t\n\nAdd a subtract API function\n",
            dict(self.reg["policy"]["limits"], max_cost_usd=5))
        b = limits.Budget(task)
        b.spend(5.0)
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=lambda *_: None).run()
        self.assertEqual(status, "needs_human")


class TestWorkflowManifest(unittest.TestCase):
    """The workflow is declared, not hardcoded.

    What these pin is the boundary: a manifest may enable, disable and repoint a
    stage, and may NOT invent one. The state machine stays code -- `implement`
    runs worktrees, waves and a bounded retry loop -- so a manifest that could
    add stages would be promising something the machine cannot deliver.

    What a manifest may no longer do is inject METHOD. `discipline:` let it hand
    a stage's working style to an installed companion skill, with a fallback
    when that skill was absent; nothing in the machine ever read it, and telling
    a dispatched agent how to work is the caller's business rather than this
    program's.
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
  implement:
    role: implementer
    card: roles/worker.md
  integrate: {role: integrator, card: roles/integrator.md%s}
"""

    def test_the_bundled_default_is_a_valid_manifest(self):
        w = wf.Workflow.load()
        self.assertEqual(w.order(), ["implement", "integrate"],
                         "the manifest and machine.STAGES disagree on the pipeline")
        for role in ("implementer", "integrator"):
            self.assertTrue(os.path.exists(w.card(role)),
                            "%s's card is declared but not on disk" % role)
        self.assertTrue(os.path.exists(w.protocol()))

    def test_a_manifest_cannot_inject_method_into_a_prompt(self):
        """The boundary, from the other side. A manifest moves WHERE an agent's
        instructions live; it has no key that puts working style into a prompt,
        and a key that looked like one but reached nothing is worse than none.
        """
        w = self._manifest("""name: opinionated
protocol: PROTOCOL.md
stages:
  implement:
    role: implementer
    card: roles/worker.md
    discipline: {skill: superpowers:brainstorming, text: Work my way.}
""")
        self.assertFalse(hasattr(w, "discipline"),
                         "the manifest can still declare method for a stage")
        task = _task(self.t.repo, "T-WF3", "# t\n\nx\n",
                     self.reg["policy"]["limits"])
        text = prompts.compose("implementer", task, subtask={"id": "s", "goal": "g"})
        self.assertNotIn("Work my way", text,
                         "a manifest key reached the agent's prompt")

    def test_a_disabled_stage_is_skipped_and_the_run_still_finishes(self):
        self._manifest(self.BASE % ", enabled: false")
        with open(os.path.join(self.t.repo, ".adg.yaml"), "w") as fh:
            fh.write('fast:\n  - "python3 -c \'import app; assert app.add(1,2)==3\'"\n')
        sh(["git", "add", "-A"], self.t.repo)
        sh(["git", "commit", "-qm", "cfg"], self.t.repo)
        script, _ = TestEndToEnd._script(self)
        task = _task(self.t.repo, "T-WF", "# t\n\nAdd a subtract API function\n",
                                 self.reg["policy"]["limits"])
        logs = []
        status = Orchestrator(task, self.reg, runtime.MockAdapter(script),
                              lambda k, t: True, log=logs.append).run()
        self.assertEqual(status, "done", "\n".join(logs))
        self.assertFalse([h for h in task.state["delegation_history"]
                          if h.get("role") == "integrator"],
                         "a disabled stage still dispatched its agent")
        # And the observable consequence, which is the point of switching it
        # off: no merge happened, so there is no patch to apply.
        self.assertFalse(os.path.exists(task.file("integrate.patch")),
                         "integrate was disabled and still wrote its output")

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
        task = _task(self.t.repo, "T-WF2", "# t\n\nx\n",
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
        task = _task(self.t.repo, "T-WFI",
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

    def test_the_report_schema_admits_exactly_the_roles_that_are_dispatched(self):
        """A stage that succeeds must be able to say so. The enums drifted the
        other way once -- `brainstorm` dispatched an agent and was not in the
        stage enum, so a stage that worked was recorded as blocked -- and they
        can drift back by keeping names for stages that no longer run."""
        for stage, role in (("implement", "implementer"),
                            ("integrate", "integrator")):
            schema.validate_report({"stage": stage, "role": role,
                                    "status": "complete", "summary": "did it",
                                    "evidence": {"tests": "1 passed"}})
        for stage, role in (("plan", "planner"), ("review", "reviewer"),
                            ("test", "test-author")):
            with self.assertRaises(schema.Invalid,
                                   msg="%s/%s is still a legal report" % (stage, role)):
                schema.validate_report({"stage": stage, "role": role,
                                        "status": "complete", "summary": "did it",
                                        "evidence": {"tests": "1 passed"}})



if __name__ == "__main__":
    unittest.main(verbosity=2)
