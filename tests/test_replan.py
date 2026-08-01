"""
Layer 4b tests — replan call + integration into achieve_goal.

See docs/planner_architecture.md and brain/replan.py / brain/plan_loop.py.

Coverage:
  - request_replan parses well-formed Claude response into ReplanResponse
  - response with code-fenced JSON parses correctly
  - missing / non-JSON / no-API responses all degrade to DECISION_ESCALATE
  - apply_replan_to_plan: insert_step, replace, continue / escalate (no-op)
  - achieve_goal calls replan_fn on NO_PROGRESS and proceeds when
    replan returns insert_step
  - achieve_goal calls replan_fn on UNCERTAIN at checkpoint
  - achieve_goal terminates when replan_fn returns ESCALATE
  - max_replans cap stops runaway replanning
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
    Goal, Plan, PlanStep, GoalCheckpoint, ProgressExpectation,
    CONFIDENCE_HIGH, save_plan,
)
from brain.plan_loop import (
    achieve_goal,
    REASON_GOAL_ACHIEVED, REASON_LIGHT_NO_PROGRESS,
    REASON_HEAVY_UNCERTAIN, REASON_PLAN_EXHAUSTED,
)
from brain.plan_runtime import PlanRuntime
from brain.replan import (
    ReplanResponse,
    DECISION_CONTINUE, DECISION_INSERT_STEP, DECISION_REPLACE,
    DECISION_ESCALATE,
    REPLAN_NO_PROGRESS, REPLAN_HEAVY_UNCERTAIN,
    request_replan, apply_replan_to_plan,
)


@dataclass
class FakePerceive:
    state:     Optional[str] = None
    detail:    str = ""
    flow:      Optional[str] = None
    flow_step: Optional[str] = None


# ── request_replan parsing ───────────────────────────────────────────────────


class RequestReplanParseTests(unittest.TestCase):

    def _goal(self):
        return Goal(goal_id="g1", description="", predicate_text="")

    def test_well_formed_response_parses_to_insert_step(self):
        stub = lambda f, p: json.dumps({
            "progress_made":   "partial",
            "new_cues":        ["cue X"],
            "options_visible": ["tap OK", "tap Cancel"],
            "decision":        "insert_step",
            "inserted_steps":  [
                {"action": {"kind": "tap_button", "labels": ["OK"]},
                 "expected_progress_hint": "dialog dismisses"},
            ],
            "reasoning":       "the recipe forgot to confirm the dialog",
        })
        result = request_replan(
            goal=self._goal(),
            plan=None, current_step_idx=0,
            history=[], current_perceive=FakePerceive(),
            replan_reason=REPLAN_NO_PROGRESS,
            claude_call=stub,
        )
        self.assertEqual(result.decision, DECISION_INSERT_STEP)
        self.assertEqual(result.progress_made, "partial")
        self.assertEqual(result.new_cues, ["cue X"])
        self.assertEqual(len(result.inserted_steps), 1)
        self.assertEqual(result.inserted_steps[0].action["kind"], "tap_button")
        self.assertIn("forgot to confirm", result.reasoning)

    def test_replace_response_parses_updated_plan(self):
        stub = lambda f, p: json.dumps({
            "decision": "replace",
            "updated_plan": [
                {"action": {"kind": "press_back"},
                 "expected_progress_hint": "back to harbor"},
                {"action": {"kind": "tap_button", "labels": ["Depart"]},
                 "expected_progress_hint": "departure begins",
                 "expected_goal_id": "g1"},
            ],
            "reasoning": "wrong path; restart with depart",
        })
        result = request_replan(
            goal=self._goal(),
            plan=None, current_step_idx=0,
            history=[], current_perceive=FakePerceive(),
            replan_reason=REPLAN_NO_PROGRESS,
            claude_call=stub,
        )
        self.assertEqual(result.decision, DECISION_REPLACE)
        self.assertEqual(len(result.updated_plan), 2)
        # Second step has expected_goal_id → it's a checkpoint
        self.assertIsNotNone(result.updated_plan[1].expected_goal)
        self.assertEqual(result.updated_plan[1].expected_goal.goal_id, "g1")

    def test_code_fenced_json_parses(self):
        stub = lambda f, p: '```json\n{"decision": "continue", "reasoning": "ok"}\n```'
        result = request_replan(
            goal=self._goal(),
            plan=None, current_step_idx=0,
            history=[], current_perceive=FakePerceive(),
            replan_reason=REPLAN_NO_PROGRESS,
            claude_call=stub,
        )
        self.assertEqual(result.decision, DECISION_CONTINUE)

    def test_no_response_escalates(self):
        stub = lambda f, p: None
        result = request_replan(
            goal=self._goal(),
            plan=None, current_step_idx=0,
            history=[], current_perceive=FakePerceive(),
            replan_reason=REPLAN_NO_PROGRESS,
            claude_call=stub,
        )
        self.assertEqual(result.decision, DECISION_ESCALATE)

    def test_non_json_escalates(self):
        stub = lambda f, p: "I think you should tap OK"
        result = request_replan(
            goal=self._goal(),
            plan=None, current_step_idx=0,
            history=[], current_perceive=FakePerceive(),
            replan_reason=REPLAN_NO_PROGRESS,
            claude_call=stub,
        )
        self.assertEqual(result.decision, DECISION_ESCALATE)

    def test_unknown_decision_normalises_to_escalate(self):
        stub = lambda f, p: json.dumps({
            "decision": "telepathy",
            "reasoning": "?",
        })
        result = request_replan(
            goal=self._goal(),
            plan=None, current_step_idx=0,
            history=[], current_perceive=FakePerceive(),
            replan_reason=REPLAN_NO_PROGRESS,
            claude_call=stub,
        )
        self.assertEqual(result.decision, DECISION_ESCALATE)


# ── apply_replan_to_plan ─────────────────────────────────────────────────────


class ApplyReplanTests(unittest.TestCase):

    def _make_plan(self):
        return Plan(plan_id="p1", goal_id="g1", steps=[
            PlanStep(step_id="s1", action={"kind": "tap1"}),
            PlanStep(step_id="s2", action={"kind": "tap2"}),
            PlanStep(step_id="s3", action={"kind": "tap3"}),
        ])

    def test_insert_step_prepends_at_index(self):
        plan = self._make_plan()
        new_step = PlanStep(step_id="inserted", action={"kind": "tap_OK"})
        apply_replan_to_plan(plan, current_step_idx=1, response=ReplanResponse(
            decision=DECISION_INSERT_STEP,
            inserted_steps=[new_step],
            reasoning="missing OK tap",
        ))
        # Order: s1, inserted, s2, s3
        ids = [s.step_id for s in plan.steps]
        self.assertEqual(ids, ["s1", "inserted", "s2", "s3"])

    def test_replace_drops_remaining_and_appends(self):
        plan = self._make_plan()
        replacement = [
            PlanStep(step_id="r1", action={"kind": "press_back"}),
            PlanStep(step_id="r2", action={"kind": "tap_other"}),
        ]
        apply_replan_to_plan(plan, current_step_idx=1, response=ReplanResponse(
            decision=DECISION_REPLACE,
            updated_plan=replacement,
            reasoning="wrong path",
        ))
        ids = [s.step_id for s in plan.steps]
        self.assertEqual(ids, ["s1", "r1", "r2"])

    def test_continue_does_not_mutate_plan(self):
        plan = self._make_plan()
        before = [s.step_id for s in plan.steps]
        apply_replan_to_plan(plan, current_step_idx=1, response=ReplanResponse(
            decision=DECISION_CONTINUE, reasoning="all good",
        ))
        self.assertEqual([s.step_id for s in plan.steps], before)

    def test_escalate_does_not_mutate_plan(self):
        plan = self._make_plan()
        before = [s.step_id for s in plan.steps]
        apply_replan_to_plan(plan, current_step_idx=1, response=ReplanResponse(
            decision=DECISION_ESCALATE, reasoning="give up",
        ))
        self.assertEqual([s.step_id for s in plan.steps], before)

    def test_replace_preserves_protected_commit_step(self):
        """commit_via_positive_taps steps embody the user-articulated
        rule 'positive button tapping is the top priority'.  When
        replan tries to REPLACE a range that contains one, the
        protected step is preserved at the front of the replacement
        sequence so its positive-button cycle still runs."""
        plan = Plan(plan_id="p1", goal_id="g1", steps=[
            PlanStep(step_id="s1", action={"kind": "tap1"}),
            PlanStep(step_id="s2_commit",
                     action={"kind": "commit_via_positive_taps",
                             "max_taps": 6}),
            PlanStep(step_id="s3", action={"kind": "exit_to_port_overworld"}),
        ])
        replacement = [
            PlanStep(step_id="r1", action={"kind": "tap_button",
                                           "labels": ["Max"]}),
            PlanStep(step_id="r2", action={"kind": "tap_button",
                                           "labels": ["Recruit"]}),
        ]
        apply_replan_to_plan(plan, current_step_idx=1, response=ReplanResponse(
            decision=DECISION_REPLACE,
            updated_plan=replacement,
            reasoning="claude wanted hardcoded coords",
        ))
        ids = [s.step_id for s in plan.steps]
        # Protected commit step preserved BEFORE Claude's replacements
        self.assertEqual(ids, ["s1", "s2_commit", "r1", "r2"])

    def test_replace_with_no_protected_steps_unchanged(self):
        """When the replaced range has no commit_via_positive_taps,
        the original replace behaviour applies."""
        plan = self._make_plan()
        replacement = [PlanStep(step_id="r1", action={"kind": "tapX"})]
        apply_replan_to_plan(plan, current_step_idx=1, response=ReplanResponse(
            decision=DECISION_REPLACE,
            updated_plan=replacement,
            reasoning="test",
        ))
        self.assertEqual([s.step_id for s in plan.steps], ["s1", "r1"])

    def test_replace_preserves_multiple_protected_steps_in_order(self):
        """If the dropped range contains multiple commit steps (rare
        but possible — e.g. a transaction with two phases), all are
        preserved in their original order."""
        plan = Plan(plan_id="p1", goal_id="g1", steps=[
            PlanStep(step_id="s1", action={"kind": "tap1"}),
            PlanStep(step_id="commit_a",
                     action={"kind": "commit_via_positive_taps"}),
            PlanStep(step_id="s2", action={"kind": "navigate_to",
                                           "target": "harbor"}),
            PlanStep(step_id="commit_b",
                     action={"kind": "commit_via_positive_taps"}),
        ])
        apply_replan_to_plan(plan, current_step_idx=1, response=ReplanResponse(
            decision=DECISION_REPLACE,
            updated_plan=[PlanStep(step_id="r1", action={"kind": "noop"})],
            reasoning="full replace",
        ))
        ids = [s.step_id for s in plan.steps]
        self.assertEqual(ids, ["s1", "commit_a", "commit_b", "r1"])


# ── achieve_goal + replan integration ────────────────────────────────────────


class _ReplanIntegrationBase(unittest.TestCase):

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="replan_int_"))
        self._original_plans  = plan_mod._PLANS_DIR
        self._original_verify = plan_mod._VERIFY_DIR
        plan_mod._PLANS_DIR  = self._tmp / "plans"
        plan_mod._VERIFY_DIR = self._tmp / "verification"
        self.executed: list[dict] = []
        self.perceive_seq: list[FakePerceive] = []
        self.perceive_calls: int = 0

    def tearDown(self):
        plan_mod._PLANS_DIR  = self._original_plans
        plan_mod._VERIFY_DIR = self._original_verify
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _capture_fn(self): return None
    def _perceive_fn(self, frame):
        i = self.perceive_calls; self.perceive_calls += 1
        return self.perceive_seq[i] if i < len(self.perceive_seq) else (
            self.perceive_seq[-1] if self.perceive_seq else FakePerceive())
    def _execute_fn(self, action): self.executed.append(action)


class ReplanOnNoProgressTests(_ReplanIntegrationBase):

    def test_no_progress_invokes_replan_then_succeeds(self):
        """
        Plan: 1 step with checkpoint.  Light says NO_PROGRESS on first
        attempt.  Replan inserts a corrective step.  After the inserted
        step runs and the original step is retried, heavy says yes.
        """
        plan = Plan(plan_id="p1", goal_id="g1", confidence=CONFIDENCE_HIGH,
                     steps=[PlanStep(
                         step_id="s1", action={"kind": "tap_recruit"},
                         expected_progress=ProgressExpectation(kind="screen_change"),
                         expected_goal=GoalCheckpoint(goal_id="g1"),
                     )])
        save_plan(plan)
        # Sequence:
        #   step s1 attempt 1: before=A, after=A (NO_PROGRESS)
        #   replan inserts "corrective" step
        #   inserted attempt: before=A, after=B (PROGRESS, no checkpoint)
        #   step s1 attempt 2: before=B, after=C (PROGRESS, checkpoint heavy)
        same = FakePerceive(state="A")
        self.perceive_seq = [
            same, same,                                # s1 attempt 1
            same, FakePerceive(state="B"),             # inserted step
            FakePerceive(state="B"), FakePerceive(state="C"),  # s1 attempt 2
        ]
        # Heavy on s1 attempt 2: GOAL_ACHIEVED
        stub_claude_heavy = lambda f, p: json.dumps({
            "goal_achieved": "yes", "confidence": 1.0,
            "evidence_summary": "done",
        })

        def fake_replan(*, goal, plan, current_step_idx, history,
                          current_perceive, replan_reason, frame=None,
                          catalog=None):
            return ReplanResponse(
                decision=DECISION_INSERT_STEP,
                inserted_steps=[PlanStep(
                    step_id="inserted",
                    action={"kind": "press_back"},
                    expected_progress=ProgressExpectation(kind="screen_change"),
                )],
                reasoning="missing back-press",
            )

        goal = Goal(goal_id="g1", description="", predicate_text="")
        result = achieve_goal(
            goal,
            perceive_fn=self._perceive_fn,
            capture_fn=self._capture_fn,
            execute_step_fn=self._execute_fn,
            runtime=PlanRuntime(),
            heavy_claude_call=stub_claude_heavy,
            replan_fn=fake_replan,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.reason, REASON_GOAL_ACHIEVED)
        # Three actions executed: original tap_recruit, inserted press_back,
        # then tap_recruit retried.
        self.assertEqual(len(self.executed), 3)
        self.assertEqual(self.executed[0]["kind"], "tap_recruit")
        self.assertEqual(self.executed[1]["kind"], "press_back")
        self.assertEqual(self.executed[2]["kind"], "tap_recruit")


class ReplanEscalateTerminatesTests(_ReplanIntegrationBase):

    def test_escalate_terminates_failure(self):
        plan = Plan(plan_id="p1", goal_id="g1", confidence=CONFIDENCE_HIGH,
                     steps=[PlanStep(
                         step_id="s1", action={"kind": "tap"},
                         expected_progress=ProgressExpectation(kind="screen_change"),
                     )])
        save_plan(plan)
        same = FakePerceive(state="A")
        self.perceive_seq = [same, same]

        def fake_replan(**kw):
            return ReplanResponse(decision=DECISION_ESCALATE,
                                    reasoning="don't know")

        goal = Goal(goal_id="g1", description="", predicate_text="")
        result = achieve_goal(
            goal,
            perceive_fn=self._perceive_fn,
            capture_fn=self._capture_fn,
            execute_step_fn=self._execute_fn,
            runtime=PlanRuntime(),
            replan_fn=fake_replan,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.reason, REASON_LIGHT_NO_PROGRESS)


class MaxReplansBudgetTests(_ReplanIntegrationBase):

    def test_max_replans_budget_stops_runaway_replanning(self):
        plan = Plan(plan_id="p1", goal_id="g1", confidence=CONFIDENCE_HIGH,
                     steps=[PlanStep(
                         step_id="s1", action={"kind": "tap_a"},
                         expected_progress=ProgressExpectation(kind="screen_change"),
                     )])
        save_plan(plan)
        # All same perceive — every step is NO_PROGRESS.
        same = FakePerceive(state="X")
        self.perceive_seq = [same] * 50

        replan_call_count = [0]
        def fake_replan(**kw):
            replan_call_count[0] += 1
            return ReplanResponse(
                decision=DECISION_INSERT_STEP,
                inserted_steps=[PlanStep(
                    step_id=f"insert_{replan_call_count[0]}",
                    action={"kind": "tap_b"},
                    expected_progress=ProgressExpectation(kind="screen_change"),
                )],
                reasoning=f"try inserting {replan_call_count[0]}",
            )

        goal = Goal(goal_id="g1", description="", predicate_text="")
        result = achieve_goal(
            goal,
            perceive_fn=self._perceive_fn,
            capture_fn=self._capture_fn,
            execute_step_fn=self._execute_fn,
            runtime=PlanRuntime(),
            replan_fn=fake_replan,
            max_replans=2,
        )
        self.assertFalse(result.success)
        # At most 2 replans should have been performed.
        self.assertLessEqual(replan_call_count[0], 2)


class ReplanOnHeavyUncertainTests(_ReplanIntegrationBase):

    def test_uncertain_invokes_replan_then_continues(self):
        """
        Plan: 2 steps, both checkpoints.
        Step 1 heavy: UNCERTAIN → replan returns CONTINUE.
        Step 2 heavy: GOAL_ACHIEVED.
        """
        plan = Plan(plan_id="p1", goal_id="g1", confidence=CONFIDENCE_HIGH,
                     steps=[
                         PlanStep(step_id="s1", action={"kind": "tap1"},
                                    expected_progress=ProgressExpectation(kind="screen_change"),
                                    expected_goal=GoalCheckpoint(goal_id="g1")),
                         PlanStep(step_id="s2", action={"kind": "tap2"},
                                    expected_progress=ProgressExpectation(kind="screen_change"),
                                    expected_goal=GoalCheckpoint(goal_id="g1")),
                     ])
        save_plan(plan)
        self.perceive_seq = [
            FakePerceive(state="A"), FakePerceive(state="B"),
            FakePerceive(state="B"), FakePerceive(state="C"),
        ]
        responses = iter([
            {"goal_achieved": "uncertain", "confidence": 0.4,
              "evidence_summary": "unsure"},
            {"goal_achieved": "yes",       "confidence": 0.95,
              "evidence_summary": "done"},
        ])
        stub_heavy = lambda f, p: json.dumps(next(responses))
        fake_replan = lambda **kw: ReplanResponse(
            decision=DECISION_CONTINUE, reasoning="just keep going",
        )
        goal = Goal(goal_id="g1", description="", predicate_text="")
        result = achieve_goal(
            goal,
            perceive_fn=self._perceive_fn,
            capture_fn=self._capture_fn,
            execute_step_fn=self._execute_fn,
            runtime=PlanRuntime(),
            heavy_claude_call=stub_heavy,
            replan_fn=fake_replan,
        )
        self.assertTrue(result.success)
        self.assertEqual(len(self.executed), 2)


if __name__ == "__main__":
    unittest.main()
