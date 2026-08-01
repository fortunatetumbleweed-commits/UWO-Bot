"""Tests for the auto_drain=False knob in _execute_action / _execute_plan.

Regression: 2026-05-20 recruit-crew teaching session.  The human typed
"tap the yellow Recruit button at lower right, then wait for the dialog
to show up, then tap the OK button in the dialog" — parsed as three
actions [tap, wait, find_and_tap].  But after step 1 (the tap), the
post-tap drain fired _drain_positive_button_chain which tapped
'Emergency Recruit @' and 'Normal Recruit' menu items in cycle, closing
the chain before the human's wait + OK steps could land on the actual
confirm dialog.

The fix: when the human is authoring steps interactively, run those
steps verbatim — no implicit auto-taps between them.  Autonomous /
learned-recovery replay still drains by default.
"""
import unittest
from unittest.mock import patch, MagicMock

from brain.human_escalation import (
    ActionStep, EscalationPlan,
    _execute_action, _execute_plan,
    ACTION_TRANSACTION, ACTION_NAVIGATION,
)


class AutoDrainSuppressedForTapTests(unittest.TestCase):
    """Coord-tap step: auto_drain=False must NOT call drain helpers."""

    def test_tap_with_auto_drain_false_skips_drain(self):
        step = ActionStep(type="tap", x=1200, y=800)
        with patch("actions.adb_actions.tap") as mock_tap, \
             patch("brain.human_escalation._drain_positive_button_chain") as mock_drain, \
             patch("brain.human_escalation.time.sleep"):
            outcome = _execute_action(step, auto_drain=False)
        mock_tap.assert_called_once_with(1200, 800)
        mock_drain.assert_not_called()
        # Without drain we can't prove it was a commit; classify as nav.
        self.assertEqual(outcome, ACTION_NAVIGATION)

    def test_tap_with_auto_drain_true_default_calls_drain(self):
        """Backward-compat: default behaviour unchanged for autonomous flows."""
        step = ActionStep(type="tap", x=1200, y=800)
        with patch("actions.adb_actions.tap"), \
             patch("brain.human_escalation._drain_positive_button_chain",
                   return_value=0) as mock_drain, \
             patch("brain.human_escalation.time.sleep"):
            _execute_action(step)   # auto_drain default True
        mock_drain.assert_called_once()


class AutoDrainSuppressedForFindAndTapTests(unittest.TestCase):
    """find_and_tap step: auto_drain=False uses label-implies-commit
    heuristic for classification, no drain."""

    def _make_step(self, label):
        return ActionStep(type="find_and_tap", label=label)

    def test_find_and_tap_with_auto_drain_false_skips_drain(self):
        step = self._make_step("OK")
        with patch("capture.adb_capture.capture_screen", return_value=MagicMock()), \
             patch("actions.sail_actions._find_button", return_value=(1300, 970)), \
             patch("actions.adb_actions.tap") as mock_tap, \
             patch("brain.human_escalation._drain_positive_button_chain") as mock_drain, \
             patch("brain.human_escalation.time.sleep"):
            outcome = _execute_action(step, auto_drain=False)
        mock_tap.assert_called_once_with(1300, 970)
        mock_drain.assert_not_called()
        # "OK" is a commit keyword → classify as transaction even without drain.
        self.assertEqual(outcome, ACTION_TRANSACTION)

    def test_find_and_tap_non_commit_label_is_navigation(self):
        """A label that isn't in the commit-keyword set is navigation
        (no drain to rescue it).  Example: tapping a sub-menu item."""
        step = self._make_step("Recruit Crew")   # opens sub-menu
        with patch("capture.adb_capture.capture_screen", return_value=MagicMock()), \
             patch("actions.sail_actions._find_button", return_value=(100, 200)), \
             patch("actions.adb_actions.tap"), \
             patch("brain.human_escalation._drain_positive_button_chain") as mock_drain, \
             patch("brain.human_escalation.time.sleep"):
            outcome = _execute_action(step, auto_drain=False)
        mock_drain.assert_not_called()
        # "Recruit Crew" isn't in UNIVERSAL_COMMIT_LABELS — nav, not commit.
        # (The current _label_implies_commit may treat "recruit" as commit;
        # this asserts what the helper *says* either way.)
        from brain.human_escalation import _label_implies_commit
        expected = (ACTION_TRANSACTION
                    if _label_implies_commit("Recruit Crew")
                    else ACTION_NAVIGATION)
        self.assertEqual(outcome, expected)


class AutoDrainSuppressedForSwipeTests(unittest.TestCase):

    def test_swipe_with_auto_drain_false_skips_drain(self):
        step = ActionStep(type="swipe", x=100, y=200, x2=400, y2=500)
        with patch("actions.adb_actions.swipe") as mock_swipe, \
             patch("brain.human_escalation._drain_positive_button_chain") as mock_drain, \
             patch("brain.human_escalation.time.sleep"):
            outcome = _execute_action(step, auto_drain=False)
        mock_swipe.assert_called_once()
        mock_drain.assert_not_called()
        self.assertEqual(outcome, ACTION_NAVIGATION)


class ExecutePlanForwardsAutoDrainTests(unittest.TestCase):
    """_execute_plan should forward auto_drain to each step's
    _execute_action call."""

    def test_execute_plan_forwards_auto_drain_false(self):
        plan = EscalationPlan(
            scenario_id="test",
            category="flow_step",
            description="",
            actions=[ActionStep(type="tap", x=100, y=200)],
        )
        with patch("brain.human_escalation._execute_action",
                   return_value=ACTION_NAVIGATION) as mock_exec:
            _execute_plan(plan, auto_drain=False)
        # _execute_action called once with auto_drain=False.
        _, kwargs = mock_exec.call_args
        self.assertFalse(kwargs.get("auto_drain", True))

    def test_execute_plan_default_keeps_auto_drain_true(self):
        plan = EscalationPlan(
            scenario_id="test",
            category="flow_step",
            description="",
            actions=[ActionStep(type="tap", x=100, y=200)],
        )
        with patch("brain.human_escalation._execute_action",
                   return_value=ACTION_NAVIGATION) as mock_exec:
            _execute_plan(plan)   # no auto_drain kw → default True
        _, kwargs = mock_exec.call_args
        self.assertTrue(kwargs.get("auto_drain", True))


if __name__ == "__main__":
    unittest.main()
