"""Skill registry — many-to-many map + cost-aware location selection."""
import unittest

from brain.skill_registry import default_registry, SkillRegistry, Skill, SkillLocation


class ManyToManyTests(unittest.TestCase):
    def setUp(self):
        self.r = default_registry()

    def test_function_to_buildings_best_first(self):
        # crew is recruitable at multiple buildings, Harbor most efficient
        self.assertEqual(self.r.buildings_for("recruit_crew"),
                         ["Harbor", "Inn", "Village"])

    def test_building_to_functions(self):
        # a Harbor offers several functions (affordance view)
        fns = set(self.r.functions_at("Harbor"))
        self.assertIn("recruit_crew", fns)
        self.assertIn("repair", fns)
        self.assertIn("resupply", fns)

    def test_repair_at_both_shipyard_and_harbor(self):
        self.assertEqual(set(self.r.buildings_for("repair")), {"Shipyard", "Harbor"})


class LocationSelectionTests(unittest.TestCase):
    def setUp(self):
        self.r = default_registry()

    def test_prefer_current_building(self):
        # THE crew-shortage fix: already at the Harbor, which recruits crew →
        # recruit HERE, don't leave for the Inn.
        self.assertEqual(
            self.r.choose_location("recruit_crew", current_building="Harbor"),
            "Harbor",
        )

    def test_prefer_current_even_if_not_most_efficient(self):
        # at the Inn (0.7) needing crew → stay at the Inn (already here) rather
        # than travel to the Harbor (1.0). Zero-travel beats efficiency.
        self.assertEqual(
            self.r.choose_location("recruit_crew", current_building="Inn"),
            "Inn",
        )

    def test_not_at_any_location_picks_most_efficient(self):
        self.assertEqual(
            self.r.choose_location("recruit_crew", current_building="Market"),
            "Harbor",
        )
        self.assertEqual(
            self.r.choose_location("recruit_crew", current_building=None),
            "Harbor",
        )

    def test_unknown_function_returns_none(self):
        self.assertIsNone(self.r.choose_location("teleport"))


if __name__ == "__main__":
    unittest.main()
