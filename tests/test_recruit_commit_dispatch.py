"""The plan's commit handler routes to the metric-gated verified recruit when
the crew HUD is readable (recruit/crew screen), and to the generic commit
otherwise.
"""
import unittest
from unittest.mock import patch, MagicMock

from brain.plan_actions import _handle_commit_via_positive_taps


class RecruitCommitDispatchTests(unittest.TestCase):
    def test_uses_verified_recruit_when_crew_readable(self):
        with patch("capture.adb_capture.capture_screen", return_value=MagicMock()), \
             patch("vision.hud_readers.read_crew", return_value=(0, 2275)), \
             patch("brain.verified_recruit.recruit_crew_verified",
                   return_value={"ok": True, "reason": "crew increased",
                                 "crew_before": (0, 2275), "crew_after": (953, 2275),
                                 "tapped": ["Recruit"]}) as mock_verify, \
             patch("brain.commit_actions.commit_via_positive_taps") as mock_generic:
            _handle_commit_via_positive_taps({"action": "commit_via_positive_taps"})
        mock_verify.assert_called_once()
        mock_generic.assert_not_called()

    def test_uses_generic_commit_when_no_crew_hud(self):
        with patch("capture.adb_capture.capture_screen", return_value=MagicMock()), \
             patch("vision.hud_readers.read_crew", return_value=None), \
             patch("brain.verified_recruit.recruit_crew_verified") as mock_verify, \
             patch("brain.commit_actions.commit_via_positive_taps") as mock_generic:
            _handle_commit_via_positive_taps(
                {"action": "commit_via_positive_taps", "goal_keywords": ["buy"]}
            )
        mock_verify.assert_not_called()
        mock_generic.assert_called_once()
        # goal_keywords is passed through to the generic commit
        self.assertEqual(mock_generic.call_args.kwargs.get("goal_keywords"), ["buy"])

    def test_verify_off_forces_generic_commit(self):
        with patch("capture.adb_capture.capture_screen", return_value=MagicMock()), \
             patch("vision.hud_readers.read_crew", return_value=(0, 2275)), \
             patch("brain.verified_recruit.recruit_crew_verified") as mock_verify, \
             patch("brain.commit_actions.commit_via_positive_taps") as mock_generic:
            _handle_commit_via_positive_taps(
                {"action": "commit_via_positive_taps", "verify": "off"}
            )
        mock_verify.assert_not_called()
        mock_generic.assert_called_once()


if __name__ == "__main__":
    unittest.main()
