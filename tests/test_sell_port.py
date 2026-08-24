"""Tests for sell-port selection (#22)."""
import unittest

from brain.sell_port import SellOption, estimate_value, rank_sell_ports, best_sell_port


class SellPortTests(unittest.TestCase):
    def test_higher_preference_wins_at_equal_distance(self):
        opts = [SellOption("A", distance=100, preference_pct=10),
                SellOption("B", distance=100, preference_pct=50)]
        self.assertEqual(best_sell_port(opts).port, "B")

    def test_farther_wins_at_equal_preference(self):
        # Distance drives base value — the far port pays more.
        opts = [SellOption("Near", distance=100, preference_pct=30),
                SellOption("Far", distance=300, preference_pct=30)]
        self.assertEqual(best_sell_port(opts).port, "Far")

    def test_known_price_overrides_estimate(self):
        opts = [SellOption("Visible", distance=50, known_price=90000),
                SellOption("Estimated", distance=300, preference_pct=50)]
        best = best_sell_port(opts, base_per_distance=1.0)
        self.assertEqual(best.port, "Visible")
        self.assertTrue(best.known)
        self.assertEqual(best.value, 90000)

    def test_estimate_formula(self):
        # base 2 · distance 100 · (1 + 0.30) = 260
        self.assertAlmostEqual(
            estimate_value(SellOption("X", 100, 30), base_per_distance=2.0), 260.0)

    def test_ranking_is_sorted_desc(self):
        opts = [SellOption("A", 100, 0), SellOption("B", 200, 0), SellOption("C", 150, 0)]
        ranked = rank_sell_ports(opts)
        self.assertEqual([r.port for r in ranked], ["B", "C", "A"])

    def test_empty_options(self):
        self.assertIsNone(best_sell_port([]))

    def test_edinburgh_food_beats_local(self):
        # Real shape: local visible price vs far Food-preference port (Edinburgh +30%).
        local = SellOption("Veracruz", distance=20, known_price=68191)
        far = SellOption("Edinburgh", distance=400, preference_pct=30)
        # With a realistic distance→ducat scale the far Food port wins.
        best = best_sell_port([local, far], base_per_distance=250)
        self.assertEqual(best.port, "Edinburgh")


if __name__ == "__main__":
    unittest.main()
