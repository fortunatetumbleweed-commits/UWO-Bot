"""Tests for the BarterMission live-wiring PLANNING core (#31 gap C)."""
import unittest

from memory.barter_kb import BarterRecipe, RecipeInput
from brain.barter_mission_live import (
    compute_material_needs, material_sources_from_recipe, plan_barter_task,
)


def _nutmeg():
    return BarterRecipe(
        good="Box of Nutmeg", season="7",
        inputs=[
            RecipeInput("Ebony", 136, []),                       # source unknown
            RecipeInput("Coral", 204, ["Male", "Atuona", "Guam"]),
            RecipeInput("Textiles", 180, []),                    # source unknown
        ],
        villages=["Melanesian Village"],
        output_per_round={"Neutral": 591})


class PlanningTests(unittest.TestCase):
    def test_material_needs(self):
        self.assertEqual(compute_material_needs(_nutmeg(), 6),
                         {"Ebony": 816, "Coral": 1224, "Textiles": 1080})

    def test_sources_from_recipe(self):
        s = material_sources_from_recipe(_nutmeg())
        self.assertEqual(s["Coral"], ["Male", "Atuona", "Guam"])
        self.assertEqual(s["Ebony"], [])

    def test_plan_flags_unsourced_materials(self):
        plan = plan_barter_task(
            _nutmeg(), "Melanesian Village", "Jakarta", rounds=6,
            port_coords={"Male": (0, 0), "Atuona": (10, 0), "Guam": (20, 0)},
            start=(0, 0))
        self.assertEqual(plan.total_output, 591 * 6)
        self.assertEqual(plan.needs["Coral"], 1224)
        # Ebony + Textiles have no known source → flagged
        self.assertEqual(plan.unsourced, ["Ebony", "Textiles"])
        # Coral assigned to the nearest source (Male @ start)
        self.assertIn("Male", plan.purchases)
        self.assertEqual(plan.purchases["Male"]["Coral"], 1224)

    def test_plan_all_sourced(self):
        r = _nutmeg()
        r.inputs[0].source_ports = ["Male"]        # Ebony sourced
        r.inputs[2].source_ports = ["Atuona"]      # Textiles sourced
        plan = plan_barter_task(
            r, "Melanesian Village", "Jakarta", rounds=6,
            port_coords={"Male": (0, 0), "Atuona": (10, 0), "Guam": (20, 0)},
            start=(0, 0))
        self.assertEqual(plan.unsourced, [])
        self.assertEqual(plan.purchases["Male"]["Ebony"], 816)


if __name__ == "__main__":
    unittest.main()
