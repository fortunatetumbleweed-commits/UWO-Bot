"""
Layer 3a tests — PlanRuntime (lookup + commit_success + commit_failure).

See docs/planner_architecture.md and brain/plan_runtime.py.

Coverage:
  - lookup combines new Plan KB + legacy flows.json
  - lookup excludes demoted plans (should_skip())
  - lookup orders by success_rate desc, then avg_duration asc
  - commit_success updates Plan, persists, applies cue observations
  - commit_failure updates Plan, persists; supplies refutations to catalog
  - cue catalog is created on first commit if absent
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import brain.plan as plan_mod
from brain.plan import (
    Cue, CueCatalog, CueProvenance, CueStats, Goal, Plan, PlanStep,
    CONFIDENCE_HIGH, CONFIDENCE_LOW, CONFIDENCE_UNVERIFIED,
    PROVENANCE_HAND_AUTHORED, PROVENANCE_HUMAN_TAUGHT,
    save_plan, list_plans_for_goal,
)
from brain.plan_runtime import PlanRuntime, get_plan_runtime
from brain.verify import HeavyResult, CueObservation, GOAL_ACHIEVED, NOT_YET


class PlanRuntimeLookupTests(unittest.TestCase):
    """Lookup combines KBs and applies filtering + ordering."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="plan_runtime_test_"))
        self._original_plans  = plan_mod._PLANS_DIR
        self._original_verify = plan_mod._VERIFY_DIR
        plan_mod._PLANS_DIR  = self._tmp / "plans"
        plan_mod._VERIFY_DIR = self._tmp / "verification"

    def tearDown(self):
        plan_mod._PLANS_DIR  = self._original_plans
        plan_mod._VERIFY_DIR = self._original_verify
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_returns_empty_list_when_no_plans_present(self):
        runtime = PlanRuntime()
        self.assertEqual(runtime.lookup("no_such_goal"), [])

    def test_returns_plans_from_new_kb(self):
        save_plan(Plan(plan_id="p1", goal_id="has_enough_crew",
                        confidence=CONFIDENCE_HIGH))
        save_plan(Plan(plan_id="p2", goal_id="has_enough_crew",
                        confidence=CONFIDENCE_HIGH))
        runtime = PlanRuntime()
        plans = runtime.lookup("has_enough_crew")
        self.assertEqual(len(plans), 2)
        self.assertEqual({p.plan_id for p in plans}, {"p1", "p2"})

    def test_excludes_demoted_plans(self):
        good = Plan(plan_id="good", goal_id="g1", confidence=CONFIDENCE_HIGH)
        bad  = Plan(plan_id="bad",  goal_id="g1", confidence=CONFIDENCE_LOW,
                     failure_count=5)
        save_plan(good)
        save_plan(bad)
        runtime = PlanRuntime()
        plans = runtime.lookup("g1")
        self.assertEqual([p.plan_id for p in plans], ["good"])

    def test_orders_by_success_rate_then_duration(self):
        # Plan A: 2/2 success, fast
        a = Plan(plan_id="A", goal_id="g1",
                  confidence=CONFIDENCE_HIGH,
                  travel_count=2, success_count=2,
                  avg_duration_secs=5.0)
        # Plan B: 1/2 success, faster
        b = Plan(plan_id="B", goal_id="g1",
                  confidence=CONFIDENCE_HIGH,
                  travel_count=2, success_count=1,
                  avg_duration_secs=2.0)
        # Plan C: 2/2 success, slow
        c = Plan(plan_id="C", goal_id="g1",
                  confidence=CONFIDENCE_HIGH,
                  travel_count=2, success_count=2,
                  avg_duration_secs=20.0)
        save_plan(a); save_plan(b); save_plan(c)
        runtime = PlanRuntime()
        plans = runtime.lookup("g1")
        # Expected order: A (1.0 success, 5s) → C (1.0, 20s) → B (0.5)
        self.assertEqual([p.plan_id for p in plans], ["A", "C", "B"])

    def test_best_plan_returns_top_or_none(self):
        runtime = PlanRuntime()
        self.assertIsNone(runtime.best_plan("nothing"))
        save_plan(Plan(plan_id="only", goal_id="g1",
                        confidence=CONFIDENCE_HIGH))
        self.assertEqual(runtime.best_plan("g1").plan_id, "only")


class PlanRuntimeCommitTests(unittest.TestCase):

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="plan_runtime_commit_"))
        self._original_plans  = plan_mod._PLANS_DIR
        self._original_verify = plan_mod._VERIFY_DIR
        plan_mod._PLANS_DIR  = self._tmp / "plans"
        plan_mod._VERIFY_DIR = self._tmp / "verification"

    def tearDown(self):
        plan_mod._PLANS_DIR  = self._original_plans
        plan_mod._VERIFY_DIR = self._original_verify
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _goal(self):
        return Goal(
            goal_id="has_enough_crew",
            description="Recruitment committed",
            predicate_text="crew count increased",
        )

    def test_commit_success_persists_plan_with_updated_counters(self):
        runtime = PlanRuntime()
        plan = Plan(plan_id="p1", goal_id="has_enough_crew",
                     confidence=CONFIDENCE_UNVERIFIED)
        runtime.commit_success(plan, duration_secs=10.0)
        # Plan was persisted with success_count=1
        from brain.plan import load_plan
        loaded = load_plan("has_enough_crew", "p1")
        self.assertEqual(loaded.success_count, 1)
        self.assertEqual(loaded.travel_count, 1)
        self.assertAlmostEqual(loaded.avg_duration_secs, 10.0)

    def test_commit_success_promotes_unverified_after_two(self):
        runtime = PlanRuntime()
        plan = Plan(plan_id="p1", goal_id="g1",
                     confidence=CONFIDENCE_UNVERIFIED)
        runtime.commit_success(plan, 1.0)
        self.assertEqual(plan.confidence, CONFIDENCE_UNVERIFIED)
        runtime.commit_success(plan, 2.0)
        self.assertEqual(plan.confidence, CONFIDENCE_HIGH)

    def test_commit_failure_demotes_at_threshold(self):
        runtime = PlanRuntime()
        plan = Plan(plan_id="p1", goal_id="g1", confidence=CONFIDENCE_HIGH)
        for _ in range(3):
            runtime.commit_failure(plan, reason="test")
        self.assertEqual(plan.confidence, CONFIDENCE_LOW)
        # And the demoted plan is excluded from lookup
        plans = runtime.lookup("g1")
        self.assertEqual(plans, [])

    def test_commit_success_with_heavy_result_creates_cue_catalog(self):
        """First commit_success with HeavyResult creates the catalog on demand."""
        runtime = PlanRuntime()
        plan = Plan(plan_id="p1", goal_id="has_enough_crew",
                     confidence=CONFIDENCE_HIGH)
        heavy = HeavyResult(
            status=GOAL_ACHIEVED,
            confidence=0.95,
            cues_observed=[],
            new_cues=["fleet crew count increased",
                      "no red dot on Recruit Crew menu"],
            evidence_summary="goal reached",
        )
        runtime.commit_success(
            plan, duration_secs=5.0,
            goal=self._goal(),
            heavy_result=heavy,
            context="test_run_1",
        )
        # Catalog file should exist with two cues
        from brain.plan import load_cue_catalog
        cat = load_cue_catalog("has_enough_crew")
        self.assertIsNotNone(cat)
        self.assertEqual(len(cat.cues), 2)
        # Each new cue carries provenance pointing at this run
        for c in cat.cues:
            self.assertEqual(c.provenance.source, "claude_observed")
            self.assertEqual(c.provenance.first_seen_in, "test_run_1")

    def test_commit_failure_with_heavy_result_records_refutations(self):
        """Refutations are also valuable cue signal — apply them on failure."""
        runtime = PlanRuntime()
        plan = Plan(plan_id="p1", goal_id="has_enough_crew",
                     confidence=CONFIDENCE_HIGH)
        # Pre-populate the catalog so refutation has something to refute
        from brain.plan import save_cue_catalog
        save_cue_catalog(CueCatalog(
            goal="has_enough_crew",
            cues=[Cue(cue="fleet crew count increased", confidence="medium",
                       stats=CueStats(observed=4, confirmed=4))],
        ))

        heavy = HeavyResult(
            status=NOT_YET,
            confidence=0.9,
            cues_observed=[CueObservation(
                cue="fleet crew count increased",
                present=True, confirms=False,   # observed false claim
            )],
            evidence_summary="crew unchanged; recruitment did not commit",
        )
        runtime.commit_failure(
            plan,
            goal=self._goal(),
            heavy_result=heavy,
            reason="test failure",
            context="test_run_failed",
        )
        from brain.plan import load_cue_catalog
        cat = load_cue_catalog("has_enough_crew")
        cue = cat.cues[0]
        self.assertEqual(cue.stats.observed, 5)
        self.assertEqual(cue.stats.refuted, 1)


class PlanRuntimeSingletonTests(unittest.TestCase):

    def test_singleton_returns_same_instance(self):
        a = get_plan_runtime()
        b = get_plan_runtime()
        self.assertIs(a, b)


if __name__ == "__main__":
    unittest.main()
