"""
Layer 1 tests — Plan, Goal, PlanStep, Cue dataclasses (data-types only,
no behaviour change yet).

See docs/planner_architecture.md and brain/plan.py.

Coverage:
  - Plan / PlanStep / Goal / Cue / CueCatalog round-trip through JSON
  - Default values are sensible (unverified/0 counters/empty steps)
  - record_success / record_failure update counters and demote/promote
    confidence as the architecture-doc lifecycle specifies
  - plan_from_flow_dict converts an existing flows.json entry to a Plan
  - plan_from_learned_recovery_dict converts a learned_recoveries entry
  - save/load round-trips through the file system
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import brain.plan as plan_mod
from brain.plan import (
    Cue, CueCatalog, CueProvenance, CueStats,
    Goal, Plan, PlanStep, ProgressExpectation, GoalCheckpoint,
    CONFIDENCE_HIGH, CONFIDENCE_LOW, CONFIDENCE_UNVERIFIED,
    PROVENANCE_HAND_AUTHORED, PROVENANCE_HUMAN_TAUGHT,
    FAILURE_DEMOTION_THRESHOLD,
    plan_from_flow_dict, plan_from_learned_recovery_dict,
    save_plan, load_plan, list_plans_for_goal,
    save_cue_catalog, load_cue_catalog,
)


# Real bad flow from flows.json — the May-1 12:39 incident, kept as the
# canonical migration test case per the user's direction.
BAD_FLOW = {
    "id": "learned_building__sea_cinematic__20260501T173946Z",
    "description": "Auto-learned via Claude guidance — recruit_crew tap.",
    "parent_state": "building",
    "atomic": True,
    "trigger_detection": "recruit crew",
    "step_detection_keywords": {"step_1": ["confirmation", "dialog", "result"]},
    "steps": [
        {
            "id": "step_1",
            "description": "Tap gold Recruit button.",
            "detection": "A confirmation dialog appears.",
            "recovery_action": "tap_button",
            "recovery_button_labels": ["recruit"],
        }
    ],
    "terminal_state": "sea_cinematic",
    "learned_at": "2026-05-01T17:39:46.493207+00:00",
    "learned_via": "claude_guidance",
    "parent_state_detail_contains": ["recruit"],
}


# Real learned_recovery entry shape (single-tap recipe).
SAMPLE_LEARNED_RECOVERY = {
    "id": "recruit_crew_confirmation_dialog",
    "category": "flow_step",
    "description": "Recruit-crew confirm dialog — tap OK to commit.",
    "detection_keywords": ["Notice", "Recruit", "Crew?", "Cancel", "OK"],
    "actions": [
        {"type": "tap", "label": "", "x": 1302, "y": 812,
         "x2": 0, "y2": 0, "seconds": 1.0,
         "x_min": -1, "y_min": -1, "x_max": -1, "y_max": -1}
    ],
    "learned_at": "2026-05-01T17:59:53.601346+00:00",
}


class PlanStepTests(unittest.TestCase):

    def test_default_step_is_not_a_checkpoint(self):
        s = PlanStep(step_id="s1", action={"kind": "tap", "x": 100, "y": 100})
        self.assertFalse(s.is_checkpoint)
        self.assertEqual(s.travel_count, 0)
        self.assertEqual(s.success_count, 0)

    def test_step_with_goal_checkpoint_marks_is_checkpoint(self):
        s = PlanStep(
            step_id="s1",
            action={"kind": "tap"},
            expected_goal=GoalCheckpoint(goal_id="has_enough_crew",
                                          rationale="post-recruit verify"),
        )
        self.assertTrue(s.is_checkpoint)

    def test_record_success_updates_counters_and_avg(self):
        s = PlanStep(step_id="s1", action={"kind": "tap"})
        s.record_success(2.0)
        s.record_success(4.0)
        self.assertEqual(s.travel_count, 2)
        self.assertEqual(s.success_count, 2)
        self.assertAlmostEqual(s.avg_duration_secs, 3.0)
        self.assertIsNotNone(s.last_traveled_at)

    def test_step_round_trips_through_dict(self):
        original = PlanStep(
            step_id="s1",
            action={"kind": "tap", "x": 100, "y": 200},
            expected_progress=ProgressExpectation(
                kind="state_change",
                hint="state should become port_overworld",
                expected_state="port_overworld",
            ),
            expected_goal=GoalCheckpoint(goal_id="has_enough_crew"),
            travel_count=3, success_count=2, failure_count=1,
            avg_duration_secs=1.5,
            notes="initial step of recruit recipe",
        )
        round_tripped = PlanStep.from_dict(original.to_dict())
        self.assertEqual(round_tripped.step_id, original.step_id)
        self.assertEqual(round_tripped.action, original.action)
        self.assertEqual(round_tripped.expected_progress.expected_state, "port_overworld")
        self.assertIsNotNone(round_tripped.expected_goal)
        self.assertEqual(round_tripped.expected_goal.goal_id, "has_enough_crew")
        self.assertEqual(round_tripped.travel_count, 3)
        self.assertEqual(round_tripped.success_count, 2)
        self.assertEqual(round_tripped.failure_count, 1)
        self.assertAlmostEqual(round_tripped.avg_duration_secs, 1.5)
        self.assertEqual(round_tripped.notes, "initial step of recruit recipe")


class PlanLifecycleTests(unittest.TestCase):

    def test_default_plan_is_unverified_with_zero_counters(self):
        p = Plan(plan_id="p1", goal_id="has_enough_crew")
        self.assertEqual(p.confidence, CONFIDENCE_UNVERIFIED)
        self.assertEqual(p.travel_count, 0)
        self.assertEqual(p.success_rate, 0.0)
        self.assertFalse(p.should_skip())

    def test_two_successes_promote_unverified_to_high(self):
        """Architecture doc Rule 2: one trial isn't proof, two is."""
        p = Plan(plan_id="p1", goal_id="g1", confidence=CONFIDENCE_UNVERIFIED)
        p.record_success(1.0)
        self.assertEqual(p.confidence, CONFIDENCE_UNVERIFIED)  # one is not enough
        p.record_success(3.0)
        self.assertEqual(p.confidence, CONFIDENCE_HIGH)
        self.assertAlmostEqual(p.avg_duration_secs, 2.0)

    def test_threshold_failures_with_zero_successes_demote_to_low(self):
        """Plan auto-demotion mirrors FSMFlow demotion (Phase 4 commit 1956c52)."""
        p = Plan(plan_id="p1", goal_id="g1", confidence=CONFIDENCE_HIGH)
        for _ in range(FAILURE_DEMOTION_THRESHOLD):
            p.record_failure()
        self.assertEqual(p.confidence, CONFIDENCE_LOW)
        self.assertTrue(p.should_skip())

    def test_below_threshold_doesnt_demote(self):
        p = Plan(plan_id="p1", goal_id="g1", confidence=CONFIDENCE_HIGH)
        p.record_failure()
        p.record_failure()
        self.assertEqual(p.confidence, CONFIDENCE_HIGH)
        self.assertFalse(p.should_skip())

    def test_plan_with_steps_round_trips(self):
        p = Plan(
            plan_id="p1",
            goal_id="has_enough_crew",
            steps=[
                PlanStep(step_id="s1", action={"kind": "tap_home"},
                         expected_progress=ProgressExpectation(
                             kind="state_change", expected_state="port_overworld")),
                PlanStep(step_id="s2", action={"kind": "navigate_to", "target": "inn"},
                         expected_progress=ProgressExpectation(
                             kind="detail_match",
                             expected_detail_substring="inn")),
                PlanStep(step_id="s3", action={"kind": "tap_button", "label": "Recruit"},
                         expected_progress=ProgressExpectation(
                             kind="screen_change",
                             hint="confirmation dialog appears"),
                         expected_goal=GoalCheckpoint(
                             goal_id="has_enough_crew",
                             rationale="post-confirm crew should increase")),
            ],
            provenance=PROVENANCE_HAND_AUTHORED,
            confidence=CONFIDENCE_HIGH,
            description="Recruit at inn, return to harbor",
        )
        round_tripped = Plan.from_dict(p.to_dict())
        self.assertEqual(round_tripped.plan_id, "p1")
        self.assertEqual(len(round_tripped.steps), 3)
        self.assertEqual(round_tripped.steps[0].action["kind"], "tap_home")
        self.assertTrue(round_tripped.steps[2].is_checkpoint)
        self.assertEqual(round_tripped.steps[2].expected_goal.goal_id, "has_enough_crew")


class FlowConversionTests(unittest.TestCase):
    """Loading existing flows.json / learned_recoveries.json as Plans."""

    def test_bad_flow_converts_to_plan_intact(self):
        """The May-1 bad flow round-trips into a Plan with same shape."""
        plan = plan_from_flow_dict(BAD_FLOW)
        self.assertEqual(plan.plan_id, BAD_FLOW["id"])
        self.assertEqual(plan.goal_id, "sea_cinematic")  # the bad terminal claim
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].action["kind"], "tap_button")
        self.assertIn("recruit", plan.steps[0].action["labels"])
        self.assertEqual(plan.provenance, PROVENANCE_HAND_AUTHORED)

    def test_learned_recovery_converts_to_single_step_plan(self):
        plan = plan_from_learned_recovery_dict(SAMPLE_LEARNED_RECOVERY,
                                                goal_id="has_enough_crew")
        self.assertEqual(plan.plan_id, SAMPLE_LEARNED_RECOVERY["id"])
        self.assertEqual(plan.goal_id, "has_enough_crew")
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].action["x"], 1302)
        self.assertEqual(plan.steps[0].action["y"], 812)
        self.assertEqual(plan.provenance, PROVENANCE_HUMAN_TAUGHT)


class GoalAndCueTests(unittest.TestCase):

    def test_goal_round_trip(self):
        g = Goal(
            goal_id="has_enough_crew",
            description="Recruitment committed; fleet has crew sufficient to depart",
            predicate_text="fleet crew count strictly increased AND no warning",
            cue_catalog_path="memory/knowledge/verification/has_enough_crew.json",
        )
        d = g.to_dict()
        g2 = Goal.from_dict(d)
        self.assertEqual(g2.goal_id, g.goal_id)
        self.assertEqual(g2.predicate_text, g.predicate_text)

    def test_goal_cheap_predicate_is_runtime_only(self):
        """Predicate functions are NOT serialised — they're runtime-only."""
        g = Goal(
            goal_id="t",
            description="",
            predicate_text="",
            cheap_predicate=lambda r: True,
        )
        d = g.to_dict()
        self.assertNotIn("cheap_predicate", d)

    def test_cue_provenance_default_source(self):
        c = Cue(cue="test cue")
        self.assertEqual(c.provenance.source, PROVENANCE_HAND_AUTHORED)
        self.assertEqual(c.confidence, CONFIDENCE_UNVERIFIED)
        self.assertEqual(c.stats.observed, 0)

    def test_cue_round_trip_preserves_provenance(self):
        c = Cue(
            cue="no red dot on Recruit Crew menu",
            polarity="confirms",
            confidence="medium",
            stats=CueStats(observed=12, confirmed=12),
            provenance=CueProvenance(
                source="claude_observed",
                first_seen="2026-05-02T16:43Z",
                first_seen_in="claude_guided_loop step 3",
                noted_during="heavy_check_after_step_4",
                noted_by="claude_vision",
            ),
        )
        c2 = Cue.from_dict(c.to_dict())
        self.assertEqual(c2.cue, c.cue)
        self.assertEqual(c2.confidence, "medium")
        self.assertEqual(c2.stats.observed, 12)
        self.assertEqual(c2.provenance.source, "claude_observed")
        self.assertEqual(c2.provenance.noted_during, "heavy_check_after_step_4")

    def test_cue_catalog_round_trip(self):
        catalog = CueCatalog(
            goal="has_enough_crew",
            description="Recruitment committed",
            predicate_text="crew count increased",
            cues=[
                Cue(cue="fleet crew count strictly greater than before",
                    confidence="high",
                    stats=CueStats(observed=47, confirmed=46, refuted=1),
                    provenance=CueProvenance(source="hand_authored")),
                Cue(cue="no red dot on Recruit Crew menu",
                    confidence="medium",
                    provenance=CueProvenance(
                        source="claude_observed",
                        first_seen="2026-05-02T16:43Z",
                    )),
            ],
            open_questions=[
                "Does the red dot also indicate other crew problems (loyalty, injury)?",
            ],
        )
        c2 = CueCatalog.from_dict(catalog.to_dict())
        self.assertEqual(c2.goal, "has_enough_crew")
        self.assertEqual(len(c2.cues), 2)
        self.assertEqual(c2.cues[0].stats.observed, 47)
        self.assertEqual(len(c2.open_questions), 1)


class FilesystemRoundTripTests(unittest.TestCase):
    """save_plan / load_plan / save_cue_catalog / load_cue_catalog work end-to-end."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="plan_test_"))
        self._original_plans = plan_mod._PLANS_DIR
        self._original_verify = plan_mod._VERIFY_DIR
        plan_mod._PLANS_DIR  = self._tmp / "plans"
        plan_mod._VERIFY_DIR = self._tmp / "verification"

    def tearDown(self):
        plan_mod._PLANS_DIR  = self._original_plans
        plan_mod._VERIFY_DIR = self._original_verify
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_save_and_load_plan(self):
        p = Plan(
            plan_id="test_plan_1",
            goal_id="has_enough_crew",
            steps=[PlanStep(step_id="s1", action={"kind": "tap_home"})],
            confidence=CONFIDENCE_HIGH,
        )
        save_plan(p)
        loaded = load_plan("has_enough_crew", "test_plan_1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.plan_id, p.plan_id)
        self.assertEqual(len(loaded.steps), 1)

    def test_load_plan_returns_none_for_missing(self):
        self.assertIsNone(load_plan("no_such_goal", "no_such_plan"))

    def test_list_plans_for_goal(self):
        for i in range(3):
            save_plan(Plan(plan_id=f"plan_{i}", goal_id="has_enough_crew"))
        plans = list_plans_for_goal("has_enough_crew")
        self.assertEqual(len(plans), 3)

    def test_save_and_load_cue_catalog(self):
        catalog = CueCatalog(
            goal="has_enough_crew",
            description="Recruitment committed",
            predicate_text="crew count increased",
            cues=[Cue(cue="test", confidence="high")],
        )
        save_cue_catalog(catalog)
        loaded = load_cue_catalog("has_enough_crew")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.goal, "has_enough_crew")
        self.assertEqual(len(loaded.cues), 1)


if __name__ == "__main__":
    unittest.main()
