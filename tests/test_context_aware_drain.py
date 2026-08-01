"""
Tests for context-aware drain (post-2026-05-04 livefix).

The harbour Trade-Goods bug: while resolving 'not enough crew', the
drain found the gold "Trade Goods" tab on the harbour right panel and
tapped it — because POSITIVE_LABELS includes 'trade' as a transaction
verb, and "Trade Goods" matches.  But "Trade Goods" is irrelevant to
recruiting crew.

The fix: when the active blocker is "not enough crew", the drain
should only tap buttons whose labels match goal-relevant keywords
(recruit / hire / crew / mate) or universal commit keywords (ok /
confirm / yes / continue).  "Trade Goods" matches neither and gets
skipped.
"""

import unittest
from unittest.mock import patch, MagicMock

from PIL import Image


def _frame() -> Image.Image:
    return Image.new("RGB", (240, 108), color=(0, 0, 0))


# ── Goal-keyword extraction ───────────────────────────────────────────────────

class ExtractGoalKeywordsTests(unittest.TestCase):

    def test_not_enough_crew_yields_recruit_keywords(self):
        from brain.human_escalation import _extract_goal_keywords
        kws = _extract_goal_keywords("Not Enough Crew")
        self.assertIn("recruit", kws)
        self.assertIn("crew", kws)

    def test_not_enough_supply_yields_supply_keywords(self):
        from brain.human_escalation import _extract_goal_keywords
        kws = _extract_goal_keywords("Not Enough Supply")
        self.assertIn("supply", kws)

    def test_unknown_blocker_yields_empty(self):
        """Unknown blocker → no context, drain falls back to legacy
        any-positive-button matcher.  Backward compatible."""
        from brain.human_escalation import _extract_goal_keywords
        self.assertEqual(_extract_goal_keywords("Some random text"), [])

    def test_none_blocker_yields_empty(self):
        from brain.human_escalation import _extract_goal_keywords
        self.assertEqual(_extract_goal_keywords(None), [])

    def test_lowercase_match_works(self):
        from brain.human_escalation import _extract_goal_keywords
        kws = _extract_goal_keywords("you have insufficient mate count")
        self.assertIn("recruit", kws)


# ── Context-aware find_positive_button ───────────────────────────────────────

class _FakeElement:
    def __init__(self, label, cx, cy, element_type="button"):
        self.label = label
        self.cx = cx
        self.cy = cy
        self.element_type = element_type


class FindPositiveButtonForContextTests(unittest.TestCase):

    def test_skips_irrelevant_positive_label(self):
        """Trade Goods matches POSITIVE_LABELS via 'trade' but is
        irrelevant to a recruit-crew goal — must be skipped."""
        from brain.commit_actions import find_positive_button_for_context
        elements = [
            _FakeElement("Trade Goods", 1886, 588),
        ]
        btn = find_positive_button_for_context(
            elements, frame_w=2400, frame_h=1080,
            goal_keywords=["recruit", "crew"],
        )
        self.assertIsNone(btn, "Trade Goods must be skipped under recruit context")

    def test_picks_recruit_button_under_recruit_context(self):
        from brain.commit_actions import find_positive_button_for_context
        elements = [
            _FakeElement("Trade Goods", 1886, 588),
            _FakeElement("Recruit", 2092, 898),
        ]
        btn = find_positive_button_for_context(
            elements, frame_w=2400, frame_h=1080,
            goal_keywords=["recruit", "crew"],
        )
        self.assertIsNotNone(btn)
        self.assertEqual(btn.label, "Recruit")

    def test_universal_commit_label_always_allowed(self):
        """Even when context-filtering is active, 'OK' / 'Confirm'
        should still match — they're universal dialog progressors."""
        from brain.commit_actions import find_positive_button_for_context
        elements = [
            _FakeElement("OK", 1500, 700),
        ]
        btn = find_positive_button_for_context(
            elements, frame_w=2400, frame_h=1080,
            goal_keywords=["recruit", "crew"],  # OK isn't in this list
        )
        self.assertIsNotNone(btn)
        self.assertEqual(btn.label, "OK")

    def test_no_goal_keywords_falls_back_to_any_positive(self):
        """Backward compatibility: callers that don't supply
        goal_keywords get the legacy behaviour."""
        from brain.commit_actions import find_positive_button_for_context
        elements = [_FakeElement("Trade Goods", 1886, 588)]
        btn = find_positive_button_for_context(
            elements, frame_w=2400, frame_h=1080, goal_keywords=None,
        )
        self.assertIsNotNone(btn)
        self.assertEqual(btn.label, "Trade Goods")

    def test_negative_labels_still_rejected_under_context(self):
        from brain.commit_actions import find_positive_button_for_context
        elements = [
            _FakeElement("Cancel", 1500, 700),
            _FakeElement("Recruit", 2092, 898),
        ]
        btn = find_positive_button_for_context(
            elements, frame_w=2400, frame_h=1080,
            goal_keywords=["recruit", "crew"],
        )
        self.assertEqual(btn.label, "Recruit")


# ── End-to-end drain with context ────────────────────────────────────────────

class DrainWithContextTests(unittest.TestCase):

    def test_drain_skips_trade_goods_under_crew_context(self):
        """Live-bug reproduction: drain finds only 'Trade Goods' on
        screen.  With recruit/crew goal_keywords, drain returns 0
        because there's no goal-relevant positive button to tap."""
        from brain.human_escalation import _drain_positive_button_chain
        # Patch commit_via_positive_taps to inspect what goal_keywords arrive.
        commit_mock = MagicMock(return_value=[])
        with patch("brain.commit_actions.commit_via_positive_taps", commit_mock), \
             patch("brain.human_escalation._drain_screen_signature", return_value="x"):
            count = _drain_positive_button_chain(goal_keywords=["recruit", "crew"])
        commit_mock.assert_called_once()
        # Verify goal_keywords were forwarded.
        kwargs = commit_mock.call_args.kwargs
        self.assertEqual(kwargs.get("goal_keywords"), ["recruit", "crew"])
        self.assertEqual(count, 0)

    def test_drain_passes_no_keywords_when_unfiltered(self):
        """Backward compat: drain without goal_keywords forwards None."""
        from brain.human_escalation import _drain_positive_button_chain
        commit_mock = MagicMock(return_value=[])
        with patch("brain.commit_actions.commit_via_positive_taps", commit_mock), \
             patch("brain.human_escalation._drain_screen_signature", return_value="x"):
            _drain_positive_button_chain()
        kwargs = commit_mock.call_args.kwargs
        self.assertIsNone(kwargs.get("goal_keywords"))

    def test_execute_plan_threads_goal_keywords_into_drain(self):
        from brain.human_escalation import (
            _execute_plan, EscalationPlan, ActionStep,
        )
        plan = EscalationPlan(
            scenario_id="ctx_test", category="flow_step", description="",
            actions=[ActionStep(type="tap", x=10, y=20)],
        )
        commit_mock = MagicMock(return_value=[])
        with patch("actions.adb_actions.tap"), \
             patch("brain.commit_actions.commit_via_positive_taps", commit_mock), \
             patch("brain.human_escalation._drain_screen_signature", return_value="x"), \
             patch("time.sleep"):
            _execute_plan(plan, goal_keywords=["recruit", "crew"])
        # commit_via_positive_taps must have been invoked with the keywords.
        kwargs = commit_mock.call_args.kwargs
        self.assertEqual(kwargs.get("goal_keywords"), ["recruit", "crew"])


if __name__ == "__main__":
    unittest.main()
