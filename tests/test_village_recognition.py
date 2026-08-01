"""Tests for village recognition in the classifier and sail goal.

Covers:
  • text_correction.correct_village_name matches against the village
    catalogue and rejects unrelated text.
  • SailToGoal arrival check accepts state.state == 'village' when the
    village name matches the destination.
"""
import unittest
from unittest.mock import patch, MagicMock

from vision.text_correction import correct_village_name


class CorrectVillageNameTests(unittest.TestCase):

    def test_clean_match(self):
        match, ratio = correct_village_name("Berber Village")
        self.assertEqual(match, "Berber Village")
        self.assertGreaterEqual(ratio, 0.99)

    def test_truncated_ocr_still_matches(self):
        # OCR sometimes drops the trailing word — still high similarity.
        match, ratio = correct_village_name("Berber Villag")
        self.assertEqual(match, "Berber Village")
        self.assertGreater(ratio, 0.85)

    def test_lowercase_match(self):
        match, _ = correct_village_name("berber village")
        self.assertEqual(match, "Berber Village")

    def test_port_name_not_a_village(self):
        # Lisbon is a port — must NOT match against the village catalogue.
        match, ratio = correct_village_name("Lisbon")
        self.assertIsNone(match)

    def test_unrelated_text_rejected(self):
        match, _ = correct_village_name("Explore")
        self.assertIsNone(match)

    def test_empty_returns_none(self):
        match, _ = correct_village_name("")
        self.assertIsNone(match)


class SailToVillageArrivalTests(unittest.TestCase):
    """SailToGoal must mark ARRIVED when state.state == 'village' and
    the village's display name matches the destination."""

    def setUp(self):
        from brain.goals.sail_to import SailToGoal, SailPhase
        self.SailToGoal = SailToGoal
        self.SailPhase = SailPhase

    def _make_state(self, location, port):
        # Minimal duck-typed state object.
        s = MagicMock()
        s.state = location
        s.port = port
        s.flow = None
        s.flow_step = None
        s.in_flow = False
        s.detail = ""
        return s

    def test_village_arrival_marks_complete(self):
        goal = self.SailToGoal(destination="Berber", from_port="Tripoli")
        # Skip past INIT — pretend the goal had been working through phases.
        goal.phase = self.SailPhase.SAILING

        state = self._make_state("village", "Berber Village")

        # Patch the planner record + obstruction recovery to no-ops so
        # tick() can run without side-effects.
        with patch("brain.recovery.notify_perceive_result"), \
             patch("brain.planner.get_planner") as mock_planner, \
             patch("brain.perceive.perceive", return_value=state):
            mock_planner.return_value.record = MagicMock()
            result = goal.tick()

        self.assertEqual(goal.phase, self.SailPhase.ARRIVED)
        self.assertTrue(goal.is_complete)
        self.assertEqual(result.action, "arrived")

    def test_port_arrival_still_works(self):
        # Regression: don't break existing port arrival behaviour.
        goal = self.SailToGoal(destination="Lisbon", from_port="Tripoli")
        goal.phase = self.SailPhase.SAILING
        state = self._make_state("port_overworld", "Lisbon")

        with patch("brain.recovery.notify_perceive_result"), \
             patch("brain.planner.get_planner") as mock_planner, \
             patch("brain.perceive.perceive", return_value=state):
            mock_planner.return_value.record = MagicMock()
            result = goal.tick()

        self.assertEqual(goal.phase, self.SailPhase.ARRIVED)
        self.assertEqual(result.action, "arrived")

    def test_village_state_with_wrong_destination_does_not_arrive(self):
        """If the bot is in some unrelated village (not the destination),
        SailToGoal must not mark ARRIVED."""
        goal = self.SailToGoal(destination="Berber", from_port="Tripoli")
        goal.phase = self.SailPhase.SAILING
        state = self._make_state("village", "Yoruba Village")

        with patch("brain.recovery.notify_perceive_result"), \
             patch("brain.planner.get_planner") as mock_planner, \
             patch("brain.perceive.perceive", return_value=state):
            mock_planner.return_value.record = MagicMock()
            goal.tick()

        self.assertNotEqual(goal.phase, self.SailPhase.ARRIVED)


if __name__ == "__main__":
    unittest.main()
