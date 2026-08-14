"""commit_via_positive_taps must prefer the yellow COMMIT button (colour+layout)
over a text-verb match.

Regression for the 2026-08-13 recruit loop: OmniParser labels the yellow "Recruit"
commit with its COST ("205,848") and detects "Normal Recruit" (the type selector)
as a button, so the text-verb search tapped the selector forever. detect_commit_buttons
recovers the real commit; this primitive should tap it — and never a red-gem one.
"""
import types
import unittest
from unittest.mock import patch, MagicMock

from brain.commit_actions import commit_via_positive_taps


def _frame(w=2400, h=1080):
    f = MagicMock(); f.width = w; f.height = h
    return f


def _commit(verb, cost, currency, cx, cy):
    return types.SimpleNamespace(verb=verb, cost=cost, currency=currency, cx=cx, cy=cy)


class YellowCommitPreferenceTests(unittest.TestCase):
    def test_taps_yellow_recruit_not_normal_recruit(self):
        taps = []
        # detect_commit_buttons returns the real yellow "Recruit" @ (1959,940);
        # the text-verb path (if reached) would have returned "Normal Recruit" @ (1961,868).
        with patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("vision.region_detectors.commit_button.detect_commit_buttons",
                   return_value=[_commit("Recruit", "205,848", "ducat", 1959, 940)]), \
             patch("brain.commit_actions.time.sleep"):
            commit_via_positive_taps(
                max_taps=3,
                capture_fn=lambda: _frame(),
                tap_fn=lambda x, y: taps.append((x, y)),
                goal_keywords=["recruit"],
            )
        # First (and only, cycle-closes on repeat) tap is the yellow commit.
        self.assertEqual(taps[0], (1959, 940))

    def test_prefers_goal_matching_verb_among_multiple_commits(self):
        taps = []
        commits = [_commit("Sell", "10", "ducat", 500, 900),
                   _commit("Recruit", "205,848", "ducat", 1959, 940)]
        with patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("vision.region_detectors.commit_button.detect_commit_buttons",
                   return_value=commits), \
             patch("brain.commit_actions.time.sleep"):
            commit_via_positive_taps(
                max_taps=2, capture_fn=lambda: _frame(),
                tap_fn=lambda x, y: taps.append((x, y)),
                goal_keywords=["recruit"],
            )
        self.assertEqual(taps[0], (1959, 940))   # matched the goal verb, not the first commit

    def test_red_gem_commit_is_not_autotapped(self):
        taps = []
        # The only yellow commit spends red gems (real money). It must be dropped;
        # with the text-verb fallback also empty, nothing is tapped.
        with patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("vision.region_detectors.commit_button.detect_commit_buttons",
                   return_value=[_commit("Buy", "999", "red_gem", 1959, 940)]), \
             patch("brain.commit_actions.find_positive_button", return_value=None), \
             patch("brain.commit_actions.find_positive_button_for_context", return_value=None), \
             patch("brain.commit_actions.time.sleep"):
            commit_via_positive_taps(
                max_taps=2, capture_fn=lambda: _frame(),
                tap_fn=lambda x, y: taps.append((x, y)),
                goal_keywords=["buy"],
            )
        self.assertEqual(taps, [])

    def test_falls_back_to_text_verb_when_no_yellow_commit(self):
        taps = []
        btn = types.SimpleNamespace(label="OK", cx=1200, cy=800)
        with patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("vision.region_detectors.commit_button.detect_commit_buttons",
                   return_value=[]), \
             patch("brain.commit_actions.find_positive_button", return_value=btn), \
             patch("brain.commit_actions.time.sleep"):
            commit_via_positive_taps(
                max_taps=2, capture_fn=lambda: _frame(),
                tap_fn=lambda x, y: taps.append((x, y)),
            )
        self.assertEqual(taps[0], (1200, 800))   # dialog OK still works via text-verb path


if __name__ == "__main__":
    unittest.main()
