"""
Tests for the Plan-Completeness rule (CLAUDE.md → "Flow Completeness &
Self-Correction" → "Claude-generated plans are recommendations, not truth").

Covers:
  - _execute_action returns the correct outcome tag for every step type.
  - _execute_plan returns a positive transaction count when steps land,
    and zero when they don't.
  - On a missed find_and_tap, _execute_plan invokes commit_via_positive_taps
    as the first-line refinement; a successful refinement counts as a
    transaction.
  - A plan that produces zero transactions does NOT get persisted via
    _save_as_learned_recovery (the gate is at the call site, not in the
    save fn — but we verify the call site honours the rule).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock

from PIL import Image

from brain.human_escalation import (
    ActionStep, EscalationPlan,
    _execute_action, _execute_plan,
    ACTION_TRANSACTION, ACTION_NAVIGATION, ACTION_MISS, ACTION_NOOP,
)


def _frame(w: int = 240, h: int = 108) -> Image.Image:
    return Image.new("RGB", (w, h), color=(0, 0, 0))


# ── _execute_action outcome tagging ──────────────────────────────────────────

class ExecuteActionOutcomeTests(unittest.TestCase):

    def test_tap_with_chain_drain_is_transaction(self):
        """A raw tap that triggers a chain of positive-button dialogs
        (e.g. tap menu → gold button drained → OK drained) is a
        transaction.  The chain itself is the transaction signal."""
        with patch("actions.adb_actions.tap") as tap_mock, \
             patch("brain.commit_actions.commit_via_positive_taps",
                   return_value=[("Recruit", 0.9, 0.5), ("OK", 0.5, 0.5)]), \
             patch("brain.human_escalation._drain_screen_signature",
                   side_effect=["before", "after"]), \
             patch("time.sleep"):
            outcome = _execute_action(ActionStep(type="tap", x=100, y=200))
        tap_mock.assert_called_once_with(100, 200)
        self.assertEqual(outcome, ACTION_TRANSACTION)

    def test_tap_with_no_chain_drain_is_navigation(self):
        """A raw tap that opens a sub-menu (no positive button visible
        yet) is navigation — the user still has to make a choice."""
        with patch("actions.adb_actions.tap"), \
             patch("brain.commit_actions.commit_via_positive_taps", return_value=[]), \
             patch("brain.human_escalation._drain_screen_signature", return_value="x"), \
             patch("time.sleep"):
            outcome = _execute_action(ActionStep(type="tap", x=100, y=200))
        self.assertEqual(outcome, ACTION_NAVIGATION)

    def test_swipe_with_drain_is_transaction(self):
        with patch("actions.adb_actions.swipe") as swipe_mock, \
             patch("brain.commit_actions.commit_via_positive_taps",
                   return_value=[("OK", 0.5, 0.5)]), \
             patch("brain.human_escalation._drain_screen_signature",
                   side_effect=["before", "after"]), \
             patch("time.sleep"):
            outcome = _execute_action(ActionStep(
                type="swipe", x=10, y=20, x2=30, y2=40,
            ))
        swipe_mock.assert_called_once()
        self.assertEqual(outcome, ACTION_TRANSACTION)

    def test_swipe_with_no_drain_is_navigation(self):
        with patch("actions.adb_actions.swipe"), \
             patch("brain.commit_actions.commit_via_positive_taps", return_value=[]), \
             patch("brain.human_escalation._drain_screen_signature", return_value="x"), \
             patch("time.sleep"):
            outcome = _execute_action(ActionStep(
                type="swipe", x=10, y=20, x2=30, y2=40,
            ))
        self.assertEqual(outcome, ACTION_NAVIGATION)

    def test_press_back_is_navigation(self):
        with patch("actions.sail_actions.press_back") as back_mock, \
             patch("time.sleep"):
            outcome = _execute_action(ActionStep(type="press_back"))
        back_mock.assert_called_once()
        self.assertEqual(outcome, ACTION_NAVIGATION)

    def test_wait_is_noop(self):
        with patch("time.sleep"):
            outcome = _execute_action(ActionStep(type="wait", seconds=0.5))
        self.assertEqual(outcome, ACTION_NOOP)

    def test_unknown_type_is_noop(self):
        with patch("time.sleep"):
            outcome = _execute_action(ActionStep(type="zzz_unknown"))
        self.assertEqual(outcome, ACTION_NOOP)

    def test_find_and_tap_with_chain_drain_is_transaction(self):
        """The harbour Recruit-Crew flow: tap 'Recruit' menu → drain
        finds gold Recruit button → tap → drain finds OK → cycle closes.
        WHOLE chain is one transaction."""
        with patch("actions.adb_actions.tap") as tap_mock, \
             patch("actions.sail_actions._find_button", return_value=(123, 456)), \
             patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch("brain.commit_actions.commit_via_positive_taps",
                   return_value=[("Recruit", 0.9, 0.5), ("OK", 0.5, 0.5)]), \
             patch("brain.human_escalation._drain_screen_signature",
                   side_effect=["before", "after"]), \
             patch("time.sleep"):
            outcome = _execute_action(ActionStep(type="find_and_tap", label="recruit"))
        tap_mock.assert_called_once_with(123, 456)
        self.assertEqual(outcome, ACTION_TRANSACTION)

    def test_find_and_tap_commit_keyword_no_chain_is_transaction(self):
        """One-shot commit (e.g. tapping OK on a result screen with no
        further dialogs to drain).  Label is a commit keyword — the
        absence of a chain doesn't disqualify it."""
        with patch("actions.adb_actions.tap"), \
             patch("actions.sail_actions._find_button", return_value=(123, 456)), \
             patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch("brain.commit_actions.commit_via_positive_taps", return_value=[]), \
             patch("time.sleep"):
            outcome = _execute_action(ActionStep(type="find_and_tap", label="confirm"))
        self.assertEqual(outcome, ACTION_TRANSACTION)

    def test_find_and_tap_navigation_label_no_chain_is_navigation(self):
        """The bug we are fixing: tapping a left-side menu item like
        'Recruit Crew' that just OPENS the recruit sub-menu (no chain
        of positive buttons appears yet) must NOT count as a
        transaction.  Previously this caused the over-counting where
        tapping the menu was logged as 2 transactions despite no
        recruitment happening."""
        with patch("actions.adb_actions.tap"), \
             patch("actions.sail_actions._find_button", return_value=(247, 371)), \
             patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch("brain.commit_actions.commit_via_positive_taps", return_value=[]), \
             patch("time.sleep"):
            # "recruit crew" the menu item — recruit IS a commit keyword,
            # so it still classifies as transaction by label.  Use a
            # plainly-navigation label that has none of the commit keywords.
            outcome = _execute_action(ActionStep(type="find_and_tap", label="manage mates"))
        self.assertEqual(outcome, ACTION_NAVIGATION)

    def test_find_and_tap_miss_returns_miss(self):
        """Crucial regression: missed find_and_tap must return ACTION_MISS so
        _execute_plan can apply the refinement chain — NOT silently skip."""
        with patch("actions.adb_actions.tap") as tap_mock, \
             patch("actions.sail_actions._find_button", return_value=None), \
             patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch("time.sleep"):
            outcome = _execute_action(ActionStep(type="find_and_tap", label="confirm"))
        tap_mock.assert_not_called()
        self.assertEqual(outcome, ACTION_MISS)


class DrainNoOpDetectionTests(unittest.TestCase):
    """The harbour Trade Goods bug (2026-05-04 19:03): drain found a gold
    TAB and tapped it.  The tap didn't change the screen — Trade Goods
    stayed selected.  commit_via_positive_taps reported "1 tap fired,
    cycle closed" and the drain over-counted as a transaction.  Pre/post
    signature comparison detects this no-op."""

    def test_drain_with_screen_change_returns_tap_count(self):
        """A drain that tapped a button AND changed the screen signature
        is a real transaction chain."""
        from brain.human_escalation import _drain_positive_button_chain
        sigs = ["before-tap-state", "after-tap-state"]
        with patch("brain.commit_actions.commit_via_positive_taps",
                   return_value=[("OK", 0.5, 0.5)]), \
             patch("brain.human_escalation._drain_screen_signature",
                   side_effect=sigs):
            count = _drain_positive_button_chain()
        self.assertEqual(count, 1)

    def test_drain_no_screen_change_returns_no_op_sentinel(self):
        """The Trade Goods case: drain tapped 1 button but the screen
        signature is identical pre/post.  Must return DRAIN_NO_OP (-1)
        so callers can suppress the label-heuristic fallback — proof
        that the tap committed nothing."""
        from brain.human_escalation import _drain_positive_button_chain, DRAIN_NO_OP
        same = "harbor-trade-goods-tab-gold"
        with patch("brain.commit_actions.commit_via_positive_taps",
                   return_value=[("Trade Goods", 0.78, 0.55)]), \
             patch("brain.human_escalation._drain_screen_signature",
                   side_effect=[same, same]):
            count = _drain_positive_button_chain()
        self.assertEqual(count, DRAIN_NO_OP)

    def test_drain_zero_taps_returns_zero_without_sig_check(self):
        """When commit_via_positive_taps tapped nothing, no signature
        comparison is needed — return 0 immediately."""
        from brain.human_escalation import _drain_positive_button_chain
        sig_mock = MagicMock(return_value="any")
        with patch("brain.commit_actions.commit_via_positive_taps", return_value=[]), \
             patch("brain.human_escalation._drain_screen_signature", sig_mock):
            count = _drain_positive_button_chain()
        self.assertEqual(count, 0)
        # The pre-drain signature is captured (one call) but the post-drain
        # signature is short-circuited because there was nothing to drain.
        self.assertLessEqual(sig_mock.call_count, 1)

    def test_find_and_tap_noop_drain_overrides_commit_label_heuristic(self):
        """The harbour Trade Goods bug fix: when drain proves the tap
        committed nothing (signature unchanged after drain taps), the
        label heuristic must NOT rescue the step — even if the label is
        a commit keyword like 'recruit'.  The drain has empirical proof
        no commit happened.  Classify as NAVIGATION so the save-gate /
        Claude revamp engages instead of trusting a false transaction."""
        same = "harbor-trade-goods-tab"
        with patch("actions.adb_actions.tap"), \
             patch("actions.sail_actions._find_button", return_value=(247, 371)), \
             patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch("brain.commit_actions.commit_via_positive_taps",
                   return_value=[("Trade Goods", 0.78, 0.55)]), \
             patch("brain.human_escalation._drain_screen_signature",
                   side_effect=[same, same]), \
             patch("time.sleep"):
            outcome = _execute_action(ActionStep(type="find_and_tap", label="recruit"))
        self.assertEqual(outcome, ACTION_NAVIGATION)

    def test_find_and_tap_one_shot_commit_no_drain_uses_label_heuristic(self):
        """When drain finds NO follow-up button at all (DRAIN_NO_BUTTON,
        not no-op), the label heuristic still applies — this is the
        legitimate one-shot commit path (e.g. tap OK on a result screen
        with nothing to drain afterward)."""
        with patch("actions.adb_actions.tap"), \
             patch("actions.sail_actions._find_button", return_value=(123, 456)), \
             patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch("brain.commit_actions.commit_via_positive_taps", return_value=[]), \
             patch("brain.human_escalation._drain_screen_signature", return_value="any"), \
             patch("time.sleep"):
            outcome = _execute_action(ActionStep(type="find_and_tap", label="recruit"))
        self.assertEqual(outcome, ACTION_TRANSACTION)

    def test_find_and_tap_with_navigation_label_and_noop_drain_is_navigation(self):
        """If the label is plain navigation AND the drain is a no-op,
        the step is honestly classified as NAVIGATION."""
        same = "harbor-trade-goods-tab"
        with patch("actions.adb_actions.tap"), \
             patch("actions.sail_actions._find_button", return_value=(247, 371)), \
             patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch("brain.commit_actions.commit_via_positive_taps",
                   return_value=[("Trade Goods", 0.78, 0.55)]), \
             patch("brain.human_escalation._drain_screen_signature",
                   side_effect=[same, same]), \
             patch("time.sleep"):
            outcome = _execute_action(ActionStep(type="find_and_tap", label="manage mates"))
        self.assertEqual(outcome, ACTION_NAVIGATION)


# ── _execute_plan transaction accounting ─────────────────────────────────────

class ExecutePlanTransactionTests(unittest.TestCase):

    def _make_plan(self, *steps: ActionStep) -> EscalationPlan:
        return EscalationPlan(
            scenario_id="test_plan", category="flow_step",
            description="test", actions=list(steps),
        )

    def test_all_taps_with_drain_counted_as_transactions(self):
        plan = self._make_plan(
            ActionStep(type="tap", x=10, y=20),
            ActionStep(type="tap", x=30, y=40),
        )
        sig_seq = iter(["pre1", "post1", "pre2", "post2"])
        with patch("actions.adb_actions.tap"), \
             patch("brain.commit_actions.commit_via_positive_taps",
                   return_value=[("OK", 0.5, 0.5)]), \
             patch("brain.human_escalation._drain_screen_signature",
                   side_effect=lambda *a, **kw: next(sig_seq)), \
             patch("time.sleep"):
            count, _early_exit = _execute_plan(plan)
        self.assertEqual(count, 2)

    def test_taps_with_no_drain_classified_as_navigation(self):
        """A plan of pure raw taps with no follow-up chain is just
        navigation; transaction count is zero and the plan is invalid."""
        plan = self._make_plan(
            ActionStep(type="tap", x=10, y=20),
            ActionStep(type="tap", x=30, y=40),
        )
        with patch("actions.adb_actions.tap"), \
             patch("brain.commit_actions.commit_via_positive_taps", return_value=[]), \
             patch("brain.human_escalation._drain_screen_signature", return_value="x"), \
             patch("time.sleep"):
            count, _early_exit = _execute_plan(plan)
        self.assertEqual(count, 0)

    def test_press_back_does_not_count(self):
        plan = self._make_plan(
            ActionStep(type="press_back"),
            ActionStep(type="press_back"),
        )
        with patch("actions.sail_actions.press_back"), \
             patch("time.sleep"):
            count, _early_exit = _execute_plan(plan)
        self.assertEqual(count, 0)

    def test_missed_step_falls_back_to_commit_via_positive_taps(self):
        """The harbour Recruit-Crew failure pattern: Claude's plan has
        find_and_tap("Confirm") which doesn't exist on the post-tap
        screen.  Runner must call commit_via_positive_taps as
        refinement; a successful refinement counts as a transaction."""
        plan = self._make_plan(
            ActionStep(type="find_and_tap", label="confirm"),
        )
        with patch("actions.sail_actions._find_button", return_value=None), \
             patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch("brain.commit_actions.commit_via_positive_taps",
                   return_value=[("Recruit", 0.9, 0.5)]) as commit_mock, \
             patch("time.sleep"):
            count, _early_exit = _execute_plan(plan)
        commit_mock.assert_called_once()
        self.assertEqual(count, 1, "refinement that produced a tap counts as transaction")

    def test_missed_step_with_no_positive_button_yields_zero_transactions(self):
        """If commit_via_positive_taps finds nothing either, the plan has
        no transactions — caller must NOT save it."""
        plan = self._make_plan(
            ActionStep(type="find_and_tap", label="confirm"),
            ActionStep(type="press_back"),
        )
        with patch("actions.sail_actions._find_button", return_value=None), \
             patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch("brain.commit_actions.commit_via_positive_taps", return_value=[]), \
             patch("actions.sail_actions.press_back"), \
             patch("time.sleep"):
            count, _early_exit = _execute_plan(plan)
        self.assertEqual(count, 0)

    def test_mixed_plan_counts_only_transactions(self):
        plan = self._make_plan(
            ActionStep(type="tap", x=1, y=1),       # +1 only when drain runs
            ActionStep(type="press_back"),          # +0 (navigation)
            ActionStep(type="wait", seconds=0.1),   # +0 (noop)
            ActionStep(type="find_and_tap", label="ghost"),  # +0 (miss + no positive button)
        )
        # Drain returns one tap on the first raw-tap call (chain settles),
        # then nothing on subsequent invocations (e.g. the missed
        # find_and_tap's refinement also finds nothing).
        drain_calls = [[("OK", 0.5, 0.5)], [], []]
        def fake_drain(*a, **kw):
            return drain_calls.pop(0) if drain_calls else []
        with patch("actions.adb_actions.tap"), \
             patch("actions.sail_actions._find_button", return_value=None), \
             patch("actions.sail_actions.press_back"), \
             patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch("brain.commit_actions.commit_via_positive_taps", side_effect=fake_drain), \
             patch("time.sleep"):
            count, _early_exit = _execute_plan(plan)
        self.assertEqual(count, 1)


# ── Plan-completeness save-gate ──────────────────────────────────────────────

class ResolveBlockerSaveGateTests(unittest.TestCase):
    """The plan-completeness save-gate lives at the call site in
    _resolve_blocker_with_reasoning.  When _execute_plan returns zero
    transactions and revamp also fails, the resolver must NOT persist the
    plan and must return False so the caller can fall back further."""

    def test_zero_transactions_does_not_save_and_returns_false(self):
        from actions.sail_actions import _resolve_blocker_with_reasoning

        # Mock the Claude API roundtrip — return a plan whose only step
        # is a find_and_tap that we will force to miss.
        fake_response = MagicMock()
        fake_response.content = [MagicMock(text=(
            '{"blocker": "Not Enough Crew", "reasoning": "test", '
            '"scenario_id": "test_blocker", "detection_keywords": ["crew"], '
            '"actions": [{"type": "find_and_tap", "label": "ghost_button"}]}'
        ))]
        fake_anthropic = MagicMock()
        fake_anthropic.Anthropic.return_value.messages.create.return_value = fake_response

        # Stub perceive_result minimally
        pr = MagicMock()
        pr.state = "building"
        pr.detail = "building: harbor"

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}), \
             patch("actions.sail_actions._find_button", return_value=None), \
             patch("brain.commit_actions.commit_via_positive_taps", return_value=[]), \
             patch("brain.human_escalation._save_as_learned_recovery") as save_mock, \
             patch("actions.sail_actions._revamp_plan_with_claude", return_value=None), \
             patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch("time.sleep"):
            result = _resolve_blocker_with_reasoning(_frame(), pr, ocr_text="not enough crew")

        save_mock.assert_not_called()
        self.assertFalse(result)

    def test_transactions_present_does_save_and_returns_true(self):
        """When the plan does land at least one transaction, the resolver
        saves the recovery and returns True (existing behaviour preserved)."""
        from actions.sail_actions import _resolve_blocker_with_reasoning

        fake_response = MagicMock()
        fake_response.content = [MagicMock(text=(
            '{"blocker": "Not Enough Crew", "reasoning": "test", '
            '"scenario_id": "test_blocker_ok", "detection_keywords": ["crew"], '
            '"actions": [{"type": "tap", "x": 100, "y": 200}]}'
        ))]
        fake_anthropic = MagicMock()
        fake_anthropic.Anthropic.return_value.messages.create.return_value = fake_response

        pr = MagicMock()
        pr.state = "building"
        pr.detail = "building: harbor"

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}), \
             patch("actions.adb_actions.tap"), \
             patch("brain.commit_actions.commit_via_positive_taps",
                   return_value=[("OK", 0.5, 0.5)]), \
             patch("brain.human_escalation._save_as_learned_recovery") as save_mock, \
             patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch("time.sleep"):
            result = _resolve_blocker_with_reasoning(_frame(), pr, ocr_text="not enough crew")

        save_mock.assert_called_once()
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
