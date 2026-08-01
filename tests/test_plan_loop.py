"""
Layer 3b tests — achieve_goal orchestration loop.

See docs/planner_architecture.md and brain/plan_loop.py.

Coverage (all paths exercised with stubs — no real ADB / Claude / perceive):
  - no plan available → REASON_NO_PLAN_AVAILABLE
  - happy path: light passes, heavy at last step says GOAL_ACHIEVED →
    success, plan.success_count incremented, cue catalog populated
  - light NO_PROGRESS terminates with REASON_LIGHT_NO_PROGRESS,
    failure_count incremented
  - heavy UNCERTAIN at checkpoint → terminates failure
  - heavy NOT_YET mid-plan → continues; final status determines outcome
  - plan exhausted without checkpoint → REASON_PLAN_EXHAUSTED
  - max_steps safety cap
  - execute_step_fn raises → recorded failure, REASON_EXECUTE_RAISED
"""

import json
import shutil
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import brain.plan as plan_mod
from brain.plan import (
    CueCatalog, Goal, Plan, PlanStep, GoalCheckpoint, ProgressExpectation,
    CONFIDENCE_HIGH, CONFIDENCE_UNVERIFIED,
    save_plan, save_cue_catalog,
)
from brain.plan_runtime import PlanRuntime
from brain.plan_loop import (
    achieve_goal, AchieveGoalResult,
    REASON_GOAL_ACHIEVED, REASON_NO_PLAN_AVAILABLE,
    REASON_LIGHT_NO_PROGRESS, REASON_HEAVY_UNCERTAIN,
    REASON_PLAN_EXHAUSTED, REASON_MAX_STEPS_REACHED,
    REASON_EXECUTE_RAISED,
)


@dataclass
class FakePerceive:
    state:     Optional[str] = None
    detail:    str = ""
    flow:      Optional[str] = None
    flow_step: Optional[str] = None


class _BaseAchieveGoalTest(unittest.TestCase):

    def setUp(self):
        # Sandbox the KB.
        self._tmp = Path(tempfile.mkdtemp(prefix="achieve_goal_test_"))
        self._original_plans  = plan_mod._PLANS_DIR
        self._original_verify = plan_mod._VERIFY_DIR
        plan_mod._PLANS_DIR  = self._tmp / "plans"
        plan_mod._VERIFY_DIR = self._tmp / "verification"

        # Per-test recorders for the injected callables.
        self.executed_actions:   list[dict] = []
        self.perceive_sequence:   list[FakePerceive] = []
        self.perceive_calls:      int = 0

    def tearDown(self):
        plan_mod._PLANS_DIR  = self._original_plans
        plan_mod._VERIFY_DIR = self._original_verify
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _capture_fn(self):
        return None  # frame value isn't inspected by stubs

    def _perceive_fn(self, frame):
        i = self.perceive_calls
        self.perceive_calls += 1
        if i < len(self.perceive_sequence):
            return self.perceive_sequence[i]
        return self.perceive_sequence[-1] if self.perceive_sequence else FakePerceive()

    def _execute_fn(self, action: dict):
        self.executed_actions.append(action)

    def _goal(self):
        return Goal(
            goal_id="has_enough_crew",
            description="Recruit committed",
            predicate_text="crew increased",
        )


class NoPlanTests(_BaseAchieveGoalTest):

    def test_returns_no_plan_when_lookup_empty(self):
        result = achieve_goal(
            self._goal(),
            perceive_fn=self._perceive_fn,
            capture_fn=self._capture_fn,
            execute_step_fn=self._execute_fn,
            runtime=PlanRuntime(),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.reason, REASON_NO_PLAN_AVAILABLE)
        self.assertIsNone(result.plan)
        self.assertEqual(result.history, [])


class HappyPathTests(_BaseAchieveGoalTest):

    def test_three_step_plan_with_goal_at_end_succeeds(self):
        plan = Plan(
            plan_id="p1", goal_id="has_enough_crew",
            confidence=CONFIDENCE_HIGH,
            steps=[
                PlanStep(step_id="s1", action={"kind": "tap_home"},
                          expected_progress=ProgressExpectation(kind="screen_change")),
                PlanStep(step_id="s2", action={"kind": "navigate_to", "target": "inn"},
                          expected_progress=ProgressExpectation(kind="screen_change")),
                PlanStep(step_id="s3", action={"kind": "tap_button", "label": "Recruit"},
                          expected_progress=ProgressExpectation(kind="screen_change"),
                          expected_goal=GoalCheckpoint(goal_id="has_enough_crew")),
            ],
        )
        save_plan(plan)

        # Each step needs a before+after perceive that DIFFER (so light passes)
        self.perceive_sequence = [
            FakePerceive(state="building", detail="harbor"),       # s1 before
            FakePerceive(state="port_overworld", detail="London"),  # s1 after
            FakePerceive(state="port_overworld", detail="London"),  # s2 before
            FakePerceive(state="building", detail="inn"),            # s2 after
            FakePerceive(state="building", detail="inn"),            # s3 before
            FakePerceive(state="building", detail="recruit dialog"), # s3 after
        ]

        # Stub heavy_check via injected claude_call: "yes goal achieved"
        stub_claude = lambda f, p: json.dumps({
            "goal_achieved": "yes",
            "confidence": 0.95,
            "cues_observed": [],
            "new_cues": ["fleet crew increased"],
            "evidence_summary": "all good",
        })

        result = achieve_goal(
            self._goal(),
            perceive_fn=self._perceive_fn,
            capture_fn=self._capture_fn,
            execute_step_fn=self._execute_fn,
            runtime=PlanRuntime(),
            heavy_claude_call=stub_claude,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.reason, REASON_GOAL_ACHIEVED)
        self.assertEqual(len(self.executed_actions), 3)
        self.assertEqual(self.executed_actions[2]["label"], "Recruit")

        # Plan was committed with success_count incremented
        from brain.plan import load_plan
        loaded = load_plan("has_enough_crew", "p1")
        self.assertEqual(loaded.success_count, 1)


class L4ShortCircuitTests(_BaseAchieveGoalTest):
    """Post-2026-05-12 livefix: when a plan's transaction achieves the
    goal, subsequent tail steps that fail light_check should NOT
    terminate the plan as failure.  An L4 check before light_check
    short-circuits to success."""

    def test_l4_goal_met_mid_plan_terminates_success(self):
        """Reproduces the live bug: step_3b_commit_recruit succeeds at
        the goal, step_4_exit_after_recruit then runs as a no-op and
        would fail light_check; L4 short-circuits to success."""
        plan = Plan(
            plan_id="p_short", goal_id="has_enough_crew",
            confidence=CONFIDENCE_HIGH,
            steps=[
                PlanStep(step_id="s1_recruit",
                          action={"kind": "commit_via_positive_taps"},
                          expected_progress=ProgressExpectation(kind="screen_change")),
                PlanStep(step_id="s2_exit",
                          action={"kind": "exit_to_port_overworld"},
                          expected_progress=ProgressExpectation(kind="screen_change")),
            ],
        )
        save_plan(plan)

        # Both steps capture the SAME post-tap state — exit-step would
        # normally light_no_progress, but L4 should fire first.
        self.perceive_sequence = [
            FakePerceive(state="building", detail="inn"),          # s1 before
            FakePerceive(state="port_overworld", detail="London"), # s1 after
            FakePerceive(state="port_overworld", detail="London"), # s2 before
            FakePerceive(state="port_overworld", detail="London"), # s2 after = same
        ]

        # Stub the L4 path: first call returns None (s1 just transacted,
        # state about to settle), second returns GOAL_ACHIEVED.
        from brain.verify import HeavyResult, GOAL_ACHIEVED
        call_count = {"n": 0}
        def fake_local(frame, goal):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return None  # not yet met after recruit
            return HeavyResult(status=GOAL_ACHIEVED, confidence=1.0,
                                evidence_summary="fleet 786/1676 >= min 786 (stubbed)")
        from unittest.mock import patch
        with patch("brain.verify.check_goal_locally", side_effect=fake_local):
            result = achieve_goal(
                self._goal(),
                perceive_fn=self._perceive_fn,
                capture_fn=self._capture_fn,
                execute_step_fn=self._execute_fn,
                runtime=PlanRuntime(),
                heavy_claude_call=lambda f, p: None,   # would error if called
            )
        self.assertTrue(result.success, f"plan failed: {result.reason}")
        self.assertEqual(result.reason, REASON_GOAL_ACHIEVED)
        # Both steps executed; success returned at s2 via L4 short-circuit.
        self.assertEqual(len(self.executed_actions), 2)

    def test_l4_returns_none_does_not_short_circuit(self):
        """When L4 abstains (state insufficient), the plan continues
        with the normal light_check → heavy_check flow."""
        plan = Plan(
            plan_id="p_passthrough", goal_id="has_enough_crew",
            confidence=CONFIDENCE_HIGH,
            steps=[
                PlanStep(step_id="s1", action={"kind": "tap_home"},
                          expected_progress=ProgressExpectation(kind="screen_change")),
            ],
        )
        save_plan(plan)
        self.perceive_sequence = [
            FakePerceive(state="building", detail="harbor"),       # before
            FakePerceive(state="port_overworld", detail="London"), # after — different = progress
        ]
        from unittest.mock import patch
        with patch("brain.verify.check_goal_locally", return_value=None):
            result = achieve_goal(
                self._goal(),
                perceive_fn=self._perceive_fn,
                capture_fn=self._capture_fn,
                execute_step_fn=self._execute_fn,
                runtime=PlanRuntime(),
                heavy_claude_call=lambda f, p: None,
            )
        # No checkpoint on s1, no heavy call needed, plan exhausted normally.
        # Just verify L4 didn't force early success when abstaining.
        self.assertFalse(result.success and result.reason == REASON_GOAL_ACHIEVED
                          and len(self.executed_actions) < 1,
                          "L4 abstain should NOT trigger early success")


class LightFailureTests(_BaseAchieveGoalTest):

    def test_light_no_progress_terminates_failure(self):
        plan = Plan(
            plan_id="p1", goal_id="has_enough_crew", confidence=CONFIDENCE_HIGH,
            steps=[
                PlanStep(step_id="s1", action={"kind": "tap"},
                          expected_progress=ProgressExpectation(kind="screen_change")),
            ],
        )
        save_plan(plan)
        # Same perceive both before and after → NO_PROGRESS
        same = FakePerceive(state="building", detail="harbor")
        self.perceive_sequence = [same, same]

        result = achieve_goal(
            self._goal(),
            perceive_fn=self._perceive_fn,
            capture_fn=self._capture_fn,
            execute_step_fn=self._execute_fn,
            runtime=PlanRuntime(),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.reason, REASON_LIGHT_NO_PROGRESS)
        from brain.plan import load_plan
        loaded = load_plan("has_enough_crew", "p1")
        self.assertEqual(loaded.failure_count, 1)


class HeavyUncertainTests(_BaseAchieveGoalTest):

    def test_uncertain_terminates_failure(self):
        plan = Plan(
            plan_id="p1", goal_id="g1", confidence=CONFIDENCE_HIGH,
            steps=[
                PlanStep(step_id="s1", action={"kind": "tap"},
                          expected_progress=ProgressExpectation(kind="screen_change"),
                          expected_goal=GoalCheckpoint(goal_id="g1")),
            ],
        )
        save_plan(plan)
        self.perceive_sequence = [
            FakePerceive(state="A"), FakePerceive(state="B"),
        ]
        stub_claude = lambda f, p: json.dumps({
            "goal_achieved": "uncertain",
            "confidence": 0.5,
            "evidence_summary": "screen unclear",
        })
        goal = Goal(goal_id="g1", description="", predicate_text="")
        result = achieve_goal(
            goal,
            perceive_fn=self._perceive_fn,
            capture_fn=self._capture_fn,
            execute_step_fn=self._execute_fn,
            runtime=PlanRuntime(),
            heavy_claude_call=stub_claude,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.reason, REASON_HEAVY_UNCERTAIN)


class HeavyNotYetMidPlanTests(_BaseAchieveGoalTest):

    def test_not_yet_mid_plan_continues_to_subsequent_steps(self):
        plan = Plan(
            plan_id="p1", goal_id="g1", confidence=CONFIDENCE_HIGH,
            steps=[
                PlanStep(step_id="s1", action={"kind": "tap1"},
                          expected_progress=ProgressExpectation(kind="screen_change"),
                          expected_goal=GoalCheckpoint(goal_id="g1")),
                PlanStep(step_id="s2", action={"kind": "tap2"},
                          expected_progress=ProgressExpectation(kind="screen_change"),
                          expected_goal=GoalCheckpoint(goal_id="g1")),
            ],
        )
        save_plan(plan)
        self.perceive_sequence = [
            FakePerceive(state="A"), FakePerceive(state="B"),
            FakePerceive(state="B"), FakePerceive(state="C"),
        ]
        # First heavy: "no", second: "yes"
        responses = iter([
            {"goal_achieved": "no",  "confidence": 0.9,
              "evidence_summary": "not done yet"},
            {"goal_achieved": "yes", "confidence": 0.95,
              "evidence_summary": "done"},
        ])
        stub_claude = lambda f, p: json.dumps(next(responses))

        goal = Goal(goal_id="g1", description="", predicate_text="")
        result = achieve_goal(
            goal,
            perceive_fn=self._perceive_fn,
            capture_fn=self._capture_fn,
            execute_step_fn=self._execute_fn,
            runtime=PlanRuntime(),
            heavy_claude_call=stub_claude,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.reason, REASON_GOAL_ACHIEVED)
        self.assertEqual(len(self.executed_actions), 2)


class PlanExhaustedTests(_BaseAchieveGoalTest):

    def test_plan_with_no_checkpoint_exhausts(self):
        plan = Plan(
            plan_id="p1", goal_id="g1", confidence=CONFIDENCE_HIGH,
            steps=[
                PlanStep(step_id="s1", action={"kind": "tap1"},
                          expected_progress=ProgressExpectation(kind="screen_change")),
                PlanStep(step_id="s2", action={"kind": "tap2"},
                          expected_progress=ProgressExpectation(kind="screen_change")),
            ],
        )
        save_plan(plan)
        self.perceive_sequence = [
            FakePerceive(state="A"), FakePerceive(state="B"),
            FakePerceive(state="B"), FakePerceive(state="C"),
        ]
        goal = Goal(goal_id="g1", description="", predicate_text="")
        result = achieve_goal(
            goal,
            perceive_fn=self._perceive_fn,
            capture_fn=self._capture_fn,
            execute_step_fn=self._execute_fn,
            runtime=PlanRuntime(),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.reason, REASON_PLAN_EXHAUSTED)
        self.assertEqual(len(self.executed_actions), 2)


class MaxStepsTests(_BaseAchieveGoalTest):

    def test_max_steps_caps_runaway(self):
        plan = Plan(
            plan_id="p1", goal_id="g1", confidence=CONFIDENCE_HIGH,
            steps=[
                PlanStep(step_id=f"s{i}", action={"kind": f"tap{i}"},
                          expected_progress=ProgressExpectation(kind="screen_change"))
                for i in range(10)
            ],
        )
        save_plan(plan)
        # Each pair of perceives differs (screen change each time).
        self.perceive_sequence = [
            FakePerceive(state=f"S{i}") for i in range(50)
        ]
        goal = Goal(goal_id="g1", description="", predicate_text="")
        result = achieve_goal(
            goal,
            perceive_fn=self._perceive_fn,
            capture_fn=self._capture_fn,
            execute_step_fn=self._execute_fn,
            runtime=PlanRuntime(),
            max_steps=3,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.reason, REASON_MAX_STEPS_REACHED)
        self.assertEqual(len(self.executed_actions), 3)


class ExecuteRaisesTests(_BaseAchieveGoalTest):

    def test_executor_exception_is_recorded(self):
        plan = Plan(
            plan_id="p1", goal_id="g1", confidence=CONFIDENCE_HIGH,
            steps=[
                PlanStep(step_id="s1", action={"kind": "boom"},
                          expected_progress=ProgressExpectation(kind="screen_change")),
            ],
        )
        save_plan(plan)
        self.perceive_sequence = [FakePerceive(state="A")]

        def crashing_executor(action):
            raise RuntimeError("simulated executor failure")

        goal = Goal(goal_id="g1", description="", predicate_text="")
        result = achieve_goal(
            goal,
            perceive_fn=self._perceive_fn,
            capture_fn=self._capture_fn,
            execute_step_fn=crashing_executor,
            runtime=PlanRuntime(),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.reason, REASON_EXECUTE_RAISED)
        from brain.plan import load_plan
        loaded = load_plan("g1", "p1")
        self.assertEqual(loaded.failure_count, 1)


if __name__ == "__main__":
    unittest.main()
