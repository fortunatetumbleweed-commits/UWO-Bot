"""Tests for the barter strategy ranker (#29)."""
import unittest

from memory.barter_kb import BarterRecipe, RecipeInput, Preconditions, Village, FleetState
from brain.sell_port import SellOption
from brain.barter_strategy import rank_barter_plays, best_barter_play


def _camas():
    return BarterRecipe(
        good="Camas", season="7",
        inputs=[RecipeInput("Luxuries", 170), RecipeInput("Food", 170)],
        villages=["Apache Village"],
        output_per_round={"Neutral": 709, "Favorable": 744, "Friendly": 813},
        preconditions=Preconditions(amity_min="Neutral"))


def _wampum():
    return BarterRecipe(
        good="Wampum", season="7",
        inputs=[RecipeInput("Metal", 100)],
        villages=["Apache Village"],
        output_per_round={"Friendly": 500},
        preconditions=Preconditions(amity_min="Friendly"))


def _village(amity="Favorable", rounds=5, locked=None):
    return Village(name="Apache Village", amity=amity, barter_rounds_remaining=rounds,
                   locked_goods=locked or [])


_MATERIALS = {"Luxuries": 900, "Food": 900, "Metal": 500}
_SELL = {
    "Camas": [SellOption("Edinburgh", distance=400, preference_pct=30)],
    "Wampum": [SellOption("London", distance=350, preference_pct=10)],
}


class BarterStrategyTests(unittest.TestCase):
    def test_ranks_eligible_play(self):
        plays = rank_barter_plays(
            [_camas()], {"Apache Village": _village()}, FleetState(),
            _SELL, _MATERIALS, cargo_free=100000, base_per_distance=1.0)
        self.assertEqual(len(plays), 1)
        p = plays[0]
        self.assertEqual((p.good, p.village, p.sell_port), ("Camas", "Apache Village", "Edinburgh"))
        # 5 rounds × 744 (Favorable) output, revenue = output × (400·1.3)
        self.assertEqual(p.output_qty, 5 * 744)
        self.assertAlmostEqual(p.est_revenue, 5 * 744 * (400 * 1.3))

    def test_ineligible_recipe_excluded(self):
        # Wampum needs Friendly; village is only Favorable → excluded.
        plays = rank_barter_plays(
            [_wampum()], {"Apache Village": _village(amity="Favorable")}, FleetState(),
            _SELL, _MATERIALS, cargo_free=100000)
        self.assertEqual(plays, [])

    def test_material_cost_subtracted(self):
        plays = rank_barter_plays(
            [_camas()], {"Apache Village": _village()}, FleetState(),
            _SELL, _MATERIALS, cargo_free=100000, base_per_distance=1.0,
            material_prices={"Luxuries": 5, "Food": 5})
        p = plays[0]
        consumed = 5 * 170 * 2                      # both materials
        self.assertAlmostEqual(p.est_material_cost, consumed * 5)
        self.assertAlmostEqual(p.est_net_profit, p.est_revenue - p.est_material_cost)

    def test_best_play_picks_highest_net(self):
        # Two eligible goods; Camas revenue >> Wampum → Camas wins.
        recipes = [_camas(), _wampum()]
        village = _village(amity="Friendly")       # both eligible at Friendly
        best = best_barter_play(recipes, {"Apache Village": village}, FleetState(),
                                _SELL, _MATERIALS, cargo_free=100000)
        self.assertEqual(best.good, "Camas")

    def test_no_materials_excluded(self):
        plays = rank_barter_plays(
            [_camas()], {"Apache Village": _village()}, FleetState(),
            _SELL, {"Luxuries": 0, "Food": 0}, cargo_free=100000)
        self.assertEqual(plays, [])

    def test_overflow_noted(self):
        plays = rank_barter_plays(
            [_camas()], {"Apache Village": _village()}, FleetState(),
            _SELL, _MATERIALS, cargo_free=500)      # small hold → overflow
        self.assertTrue(any("overflow" in n for n in plays[0].notes))


if __name__ == "__main__":
    unittest.main()
