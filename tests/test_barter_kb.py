"""Tests for the barter/nav KB schema (#11) — round-trip + persistence + amity gating.

Data is grounded in the barter walkthrough: Camas yields scale 709/744/813 with
amity; Sofrito needs 4 materials, some sourced at multiple ports.
"""
import unittest
from pathlib import Path
import tempfile

from memory import barter_kb as kb
from memory.barter_kb import (
    BarterRecipe, RecipeInput, Preconditions, Village, PortPreference, FleetState,
    amity_at_least, AMITY_GRADES,
)


def _sofrito() -> BarterRecipe:
    return BarterRecipe(
        good="Sofrito", season="2026-summer",
        inputs=[
            RecipeInput("Onion", 2, ["Buenos Aires", "Cairo", "Nantes"]),
            RecipeInput("Carrot", 1, ["Kerch", "Montpellier"]),
            RecipeInput("Garlic", 1, ["Alexandria", "Antalya", "Malaga"]),
            RecipeInput("Olive Oil", 3, ["Candia", "Hormuz", "Istanbul", "Ragusa"]),
        ],
        villages=["Coahuila y Tejas"],
        output_per_round={"Neutral": 709, "Favorable": 744, "Friendly": 813},
        preconditions=Preconditions(amity_min="Favorable"),
        notes="4-material recipe",
    )


class AmityGatingTests(unittest.TestCase):
    def test_grade_order(self):
        self.assertEqual(AMITY_GRADES, ("Neutral", "Favorable", "Trusting", "Friendly"))

    def test_at_least_meets_and_exceeds(self):
        self.assertTrue(amity_at_least("Friendly", "Favorable"))   # exceeds
        self.assertTrue(amity_at_least("Favorable", "Favorable"))  # meets
        self.assertFalse(amity_at_least("Neutral", "Favorable"))   # below

    def test_no_requirement_always_true(self):
        self.assertTrue(amity_at_least(None, None))
        self.assertTrue(amity_at_least("Neutral", None))

    def test_unknown_current_not_assumed_eligible(self):
        self.assertFalse(amity_at_least(None, "Favorable"))
        self.assertFalse(amity_at_least("Bogus", "Favorable"))


class RoundTripTests(unittest.TestCase):
    def test_recipe_round_trip_preserves_nested(self):
        r = _sofrito()
        r2 = BarterRecipe.from_dict(r.to_dict())
        self.assertEqual(r2.good, "Sofrito")
        self.assertEqual(len(r2.inputs), 4)
        self.assertIsInstance(r2.inputs[0], RecipeInput)
        self.assertEqual(r2.inputs[3].source_ports,
                         ["Candia", "Hormuz", "Istanbul", "Ragusa"])
        self.assertEqual(r2.output_per_round["Friendly"], 813)
        self.assertEqual(r2.preconditions.amity_min, "Favorable")
        self.assertIsInstance(r2.preconditions, Preconditions)

    def test_from_dict_ignores_unknown_keys(self):
        # Back-compat: a stored record with an extra field must not crash.
        d = _sofrito().to_dict()
        d["legacy_field"] = 123
        d["inputs"][0]["also_legacy"] = "x"
        r2 = BarterRecipe.from_dict(d)
        self.assertEqual(r2.good, "Sofrito")

    def test_village_round_trip(self):
        v = Village(name="Coahuila y Tejas", amity="Favorable", amity_points=744,
                    barter_rounds_total=6, barter_rounds_remaining=0,
                    eligible_goods=["Camas", "Wampum"], locked_goods=["Pulque"])
        v2 = Village.from_dict(v.to_dict())
        self.assertEqual(v2.amity_points, 744)
        self.assertEqual(v2.barter_rounds_remaining, 0)
        self.assertIn("Wampum", v2.eligible_goods)

    def test_preference_round_trip(self):
        p = PortPreference(port="Edinburgh", season="2026-summer",
                           preferences={"Textile": 50, "Food": 30, "Spices": 20})
        p2 = PortPreference.from_dict(p.to_dict())
        self.assertEqual(p2.preferences["Food"], 30)

    def test_fleet_state_round_trip(self):
        f = FleetState(trade_level=12, negotiation_expertise=5, guild="Merchants",
                       currencies={"ducat": 5000000, "blue_gem": 40, "red_gem": 0},
                       gift_tokens=3)
        f2 = FleetState.from_dict(f.to_dict())
        self.assertEqual(f2.currencies["ducat"], 5000000)
        self.assertEqual(f2.gift_tokens, 3)


class PersistenceTests(unittest.TestCase):
    """Redirect the module path constants to a tmp dir so real KB is untouched."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._orig = (kb._RECIPES_PATH, kb._VILLAGES_PATH,
                      kb._PREFERENCES_PATH, kb._FLEET_STATE_PATH)
        kb._RECIPES_PATH = base / "recipes.json"
        kb._VILLAGES_PATH = base / "villages.json"
        kb._PREFERENCES_PATH = base / "preferences.json"
        kb._FLEET_STATE_PATH = base / "fleet_state.json"

    def tearDown(self):
        (kb._RECIPES_PATH, kb._VILLAGES_PATH,
         kb._PREFERENCES_PATH, kb._FLEET_STATE_PATH) = self._orig
        self._tmp.cleanup()

    def test_recipe_save_load(self):
        kb.save_recipe(_sofrito())
        loaded = kb.load_recipe("Sofrito")
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded.inputs), 4)
        self.assertEqual([r.good for r in kb.all_recipes()], ["Sofrito"])

    def test_recipe_save_is_upsert_by_good(self):
        kb.save_recipe(_sofrito())
        kb.save_recipe(BarterRecipe(good="Sofrito", notes="updated"))
        self.assertEqual(len(kb.all_recipes()), 1)
        self.assertEqual(kb.load_recipe("Sofrito").notes, "updated")

    def test_village_save_load(self):
        kb.save_village(Village(name="Coahuila y Tejas", amity="Friendly"))
        self.assertEqual(kb.load_village("Coahuila y Tejas").amity, "Friendly")

    def test_preference_save_load(self):
        kb.save_preference(PortPreference(port="Edinburgh",
                                          preferences={"Food": 30}))
        self.assertEqual(kb.load_preference("Edinburgh").preferences["Food"], 30)

    def test_fleet_state_default_when_absent(self):
        self.assertIsInstance(kb.load_fleet_state(), FleetState)
        self.assertIsNone(kb.load_fleet_state().trade_level)

    def test_fleet_state_save_load(self):
        kb.save_fleet_state(FleetState(trade_level=12, gift_tokens=3))
        self.assertEqual(kb.load_fleet_state().trade_level, 12)

    def test_missing_recipe_returns_none(self):
        self.assertIsNone(kb.load_recipe("Nonexistent"))


if __name__ == "__main__":
    unittest.main()
