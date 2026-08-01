"""
Per-step blocker re-check in escalation (`_execute_plan`) +
`_check_blocker_resolved` helper.

Origin: 2026-05-12 17:19 stuck-state — escalation kept executing its
3-step plan for ~4.5 minutes after the harbour depart panel had already
cleared.  The escalation loop didn't re-check whether the original
blocker was still on screen.  Fix: thread `blocker_text` into
`_execute_plan` and consult `_check_blocker_resolved` before each step.

Coverage:
  * _check_blocker_resolved: three-way return contract
    (True / False / None) and the depart-panel anchor guard.
  * _execute_plan: pre-plan early exit when blocker is already gone.
  * _execute_plan: mid-plan early exit when a step's action removes the
    blocker.
  * _execute_plan: backward compat — without blocker_text, behaves
    exactly as before (no early exit, returns (count, False)).
"""

import unittest
from unittest.mock import MagicMock, patch


def _make_token(text, cx=100, cy=100, conf=0.9):
    """_ocr_frame returns 4-tuples (text, conf, cx, cy)."""
    return (text, conf, cx, cy)


def _make_frame(w=2400, h=1080):
    f = MagicMock()
    f.width = w
    f.height = h
    f.crop.return_value = f
    f.copy.return_value = f
    return f


class CheckBlockerResolvedTests(unittest.TestCase):
    """The three-way return contract: True / False / None."""

    def test_blocker_visible_returns_false(self):
        """Blocker phrase visible on screen → False (still blocked)."""
        from actions.sail_actions import _check_blocker_resolved
        with patch("actions.sail_actions._ocr_frame",
                    return_value=[_make_token("Not Enough Crew"),
                                   _make_token("Supply Departure")]):
            self.assertFalse(_check_blocker_resolved("Not Enough Crew",
                                                       frame=_make_frame()))

    def test_blocker_absent_with_depart_anchor_returns_true(self):
        """Blocker gone AND depart panel visible → True (resolved)."""
        from actions.sail_actions import _check_blocker_resolved
        with patch("actions.sail_actions._ocr_frame",
                    return_value=[_make_token("Depart Now"),
                                   _make_token("Supply Departure"),
                                   _make_token("Crew Size 1500/1676")]):
            self.assertTrue(_check_blocker_resolved("Not Enough Crew",
                                                     frame=_make_frame()))

    def test_blocker_absent_without_anchor_returns_none(self):
        """Blocker gone AND we're not on depart panel → None
        (no evidence either way — could be deep in a sub-menu)."""
        from actions.sail_actions import _check_blocker_resolved
        with patch("actions.sail_actions._ocr_frame",
                    return_value=[_make_token("Recruit"),
                                   _make_token("1,836 Recruit")]):
            self.assertIsNone(_check_blocker_resolved("Not Enough Crew",
                                                       frame=_make_frame()))

    def test_empty_blocker_returns_none(self):
        from actions.sail_actions import _check_blocker_resolved
        self.assertIsNone(_check_blocker_resolved("", frame=_make_frame()))
        self.assertIsNone(_check_blocker_resolved(None, frame=_make_frame()))

    def test_short_word_filter(self):
        """Only words of length >=4 are required to be visible.  'Not
        Enough Crew' → requires 'enough' AND 'crew' to be visible.
        Missing 'not' alone is not enough to fail."""
        from actions.sail_actions import _check_blocker_resolved
        # 'enough' present but 'crew' absent — blocker NOT visible
        with patch("actions.sail_actions._ocr_frame",
                    return_value=[_make_token("There is enough food"),
                                   _make_token("Depart Now")]):
            # blocker_visible should be False (crew missing), depart-anchor
            # IS visible → returns True (resolved)
            self.assertTrue(_check_blocker_resolved("Not Enough Crew",
                                                     frame=_make_frame()))


class ExecutePlanBlockerEarlyExitTests(unittest.TestCase):
    """The `early_exit` half of the new (transactions, early_exit) tuple."""

    def _build_plan(self, action_types):
        from brain.human_escalation import EscalationPlan, ActionStep
        actions = []
        for t in action_types:
            actions.append(ActionStep(type=t, label="Test", x=100, y=100))
        return EscalationPlan(
            scenario_id="test_plan", category="flow_step",
            description="test", actions=actions,
        )

    def test_pre_plan_blocker_gone_returns_zero_and_early_exit(self):
        """If the blocker is already resolved BEFORE any step runs, the
        plan exits with transactions=0 AND early_exit=True.  Caller will
        treat this as success."""
        from brain.human_escalation import _execute_plan
        plan = self._build_plan(["tap", "tap", "press_back"])
        with patch("brain.human_escalation._execute_action") as mock_act, \
             patch("actions.sail_actions._check_blocker_resolved",
                    return_value=True):
            count, early_exit = _execute_plan(plan, blocker_text="Not Enough Crew")
        self.assertEqual(count, 0)
        self.assertTrue(early_exit)
        # No step should have executed
        mock_act.assert_not_called()

    def test_mid_plan_blocker_removal_exits_early(self):
        """Plan has 3 steps; the first step's action removes the blocker.
        Plan should exit after step 1 with early_exit=True, not run
        steps 2 and 3."""
        from brain.human_escalation import (
            _execute_plan, ACTION_TRANSACTION,
        )
        plan = self._build_plan(["tap", "tap", "tap"])
        # First _check_blocker_resolved call (pre-plan) → None (no opinion).
        # After step 1 → True (resolved).  Subsequent calls would also be
        # True but should never fire if early-exit works.
        check_returns = iter([None, True])

        def fake_check(*args, **kwargs):
            try:
                return next(check_returns)
            except StopIteration:
                self.fail("blocker check called more than expected")

        with patch("brain.human_escalation._execute_action",
                    return_value=ACTION_TRANSACTION) as mock_act, \
             patch("actions.sail_actions._check_blocker_resolved",
                    side_effect=fake_check):
            count, early_exit = _execute_plan(plan, blocker_text="Not Enough Crew")

        self.assertTrue(early_exit)
        self.assertEqual(count, 1)               # one transaction recorded
        self.assertEqual(mock_act.call_count, 1)  # only step 1 ran

    def test_no_blocker_text_no_early_exit(self):
        """When blocker_text is None (e.g. learned-recovery caller has
        no specific blocker), the per-step re-check is disabled and the
        plan runs to completion with early_exit=False."""
        from brain.human_escalation import (
            _execute_plan, ACTION_TRANSACTION,
        )
        plan = self._build_plan(["tap", "tap"])
        with patch("brain.human_escalation._execute_action",
                    return_value=ACTION_TRANSACTION) as mock_act, \
             patch("actions.sail_actions._check_blocker_resolved",
                    return_value=True) as mock_check:
            count, early_exit = _execute_plan(plan)  # no blocker_text

        self.assertFalse(early_exit)
        self.assertEqual(count, 2)               # both steps ran
        self.assertEqual(mock_act.call_count, 2)
        # The check is only called when blocker_text is supplied
        mock_check.assert_not_called()

    def test_blocker_text_but_never_resolves_runs_full_plan(self):
        """When the blocker stays on screen throughout, plan runs all
        steps and returns early_exit=False."""
        from brain.human_escalation import (
            _execute_plan, ACTION_TRANSACTION,
        )
        plan = self._build_plan(["tap", "tap"])
        with patch("brain.human_escalation._execute_action",
                    return_value=ACTION_TRANSACTION) as mock_act, \
             patch("actions.sail_actions._check_blocker_resolved",
                    return_value=False):    # always still blocked
            count, early_exit = _execute_plan(plan, blocker_text="Not Enough Crew")

        self.assertFalse(early_exit)
        self.assertEqual(count, 2)
        self.assertEqual(mock_act.call_count, 2)


class BuildOmniparserTableTests(unittest.TestCase):
    """The OmniParser → text-table conversion used in Claude prompts.
    A regression here causes Claude to receive empty input and fall back
    to pixel-hunting on a thumbnail (the 2026-05-12 17:19 failure mode)."""

    def test_table_includes_idx_type_coords_label(self):
        from actions.sail_actions import _build_omniparser_table
        el1 = MagicMock(label="Depart Now", element_type="button", cx=2155, cy=836)
        el2 = MagicMock(label="Supply Departure", element_type="button", cx=2154, cy=903)

        with patch("vision.omniparser.get_omniparser") as MockOmni, \
             patch("vision.omniparser.parse_fast_cached", return_value=[el1, el2]):
            MockOmni.return_value.yolo_available.return_value = True
            table, els = _build_omniparser_table(_make_frame())

        self.assertIn("Depart Now", table)
        self.assertIn("Supply Departure", table)
        self.assertIn("2155", table)
        self.assertIn("2154", table)
        self.assertIn("button", table)
        self.assertIn("idx", table)
        self.assertEqual(len(els), 2)

    def test_omniparser_unavailable_returns_empty(self):
        from actions.sail_actions import _build_omniparser_table
        with patch("vision.omniparser.get_omniparser") as MockOmni:
            MockOmni.return_value.yolo_available.return_value = False
            table, els = _build_omniparser_table(_make_frame())
        self.assertEqual(table, "")
        self.assertEqual(els, [])


if __name__ == "__main__":
    unittest.main()
