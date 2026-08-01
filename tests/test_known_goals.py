"""
Layer 4c tests — wired entry points in brain/known_goals.py.

Coverage:
  - HAS_ENOUGH_CREW Goal is well-formed and points at the seed catalog
  - The seed catalog file exists and parses as a CueCatalog
  - achieve_has_enough_crew is callable and returns an AchieveGoalResult;
    we don't exercise the live integration here (that happens in a
    real bot run), but verify the wiring composes correctly via
    monkeypatched callables
"""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from brain.plan import CueCatalog, Goal, load_cue_catalog


class HasEnoughCrewGoalTests(unittest.TestCase):

    def test_goal_definition_well_formed(self):
        from brain.known_goals import HAS_ENOUGH_CREW
        self.assertEqual(HAS_ENOUGH_CREW.goal_id, "has_enough_crew")
        self.assertTrue(HAS_ENOUGH_CREW.description)
        self.assertTrue(HAS_ENOUGH_CREW.predicate_text)
        self.assertTrue(HAS_ENOUGH_CREW.cue_catalog_path.endswith(
            "verification/has_enough_crew.json"
        ))


class SeedCatalogTests(unittest.TestCase):
    """The seed catalog ships with the repo."""

    def test_seed_catalog_file_exists_and_parses(self):
        path = Path("memory/knowledge/verification/has_enough_crew.json")
        self.assertTrue(path.exists(), "seed catalog file must be checked in")
        catalog = CueCatalog.from_dict(json.loads(path.read_text()))
        self.assertEqual(catalog.goal, "has_enough_crew")
        self.assertGreater(len(catalog.cues), 0)
        # Every cue has provenance.
        for cue in catalog.cues:
            self.assertIn(cue.provenance.source, (
                "hand_authored", "claude_observed", "human_taught",
                "distilled", "inherited",
            ))
        # Open questions field exists for curatorial use.
        self.assertIsInstance(catalog.open_questions, list)


class AchieveHasEnoughCrewWiringTests(unittest.TestCase):
    """
    Confirms the wiring composes — achieve_has_enough_crew calls
    achieve_goal with the right arguments.  Doesn't exercise the live
    integration; that happens in a real bot run.
    """

    def test_calls_achieve_goal_with_has_enough_crew_goal(self):
        captured = {}

        def fake_achieve(goal, **kwargs):
            captured["goal"]    = goal
            captured["kwargs"]  = kwargs
            from brain.plan_loop import AchieveGoalResult
            return AchieveGoalResult(
                success=True, reason="goal_achieved",
                duration_secs=0.0,
            )

        with patch("brain.known_goals.achieve_goal", fake_achieve):
            from brain.known_goals import achieve_has_enough_crew
            result = achieve_has_enough_crew(home_port="London", max_steps=10)
            self.assertTrue(result.success)
            self.assertEqual(captured["goal"].goal_id, "has_enough_crew")
            # Wiring includes the live callables and the replan function.
            self.assertIn("perceive_fn", captured["kwargs"])
            self.assertIn("capture_fn", captured["kwargs"])
            self.assertIn("execute_step_fn", captured["kwargs"])
            self.assertIn("replan_fn", captured["kwargs"])
            self.assertEqual(captured["kwargs"]["max_steps"], 10)


if __name__ == "__main__":
    unittest.main()
