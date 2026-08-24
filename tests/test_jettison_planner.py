"""Tests for the dump/jettison planner (#21) — safety-critical."""
import unittest

from brain.jettison_planner import (
    CargoItem, DumpAction, plan_jettison, reserves_from_route,
)


def _plan(*a, **k):
    return plan_jettison(*a, **k)


class JettisonTests(unittest.TestCase):
    def test_dumps_cheapest_good_first(self):
        cargo = [CargoItem("Camas", 800, unit_value=86),
                 CargoItem("Avocado", 200, unit_value=3)]
        plan, short = _plan(50, cargo, reserves={})
        self.assertEqual(short, 0)
        self.assertEqual(plan, [DumpAction("Avocado", 50, None)])   # cheap good only

    def test_spills_to_next_cheapest_when_needed(self):
        cargo = [CargoItem("Camas", 800, 86),
                 CargoItem("Avocado", 30, 3), CargoItem("Cotton", 100, 10)]
        plan, short = _plan(50, cargo, reserves={})
        self.assertEqual(short, 0)
        # all 30 Avocado + 20 of the next-cheapest (Cotton)
        self.assertEqual(plan, [DumpAction("Avocado", 30, None),
                                DumpAction("Cotton", 20, None)])

    def test_supply_never_dumped_below_reserve(self):
        # Longest leg needs 165 of each; only the excess above 165 may go.
        cargo = [CargoItem("Water", 226, 1, resource="water"),
                 CargoItem("Food", 226, 1, resource="food")]
        plan, short = _plan(300, cargo, reserves={"water": 165, "food": 165})
        # max dumpable excess = (226-165)*2 = 122 → 300 can't be cleared.
        dumped = {d.name: d.qty for d in plan}
        self.assertLessEqual(dumped.get("Water", 0), 61)
        self.assertLessEqual(dumped.get("Food", 0), 61)
        self.assertEqual(short, 300 - 122)                 # shortfall reported, reserve intact

    def test_goods_before_supply(self):
        cargo = [CargoItem("Water", 300, 1, resource="water"),
                 CargoItem("Trinket", 100, 2)]
        plan, short = _plan(50, cargo, reserves={"water": 100})
        self.assertEqual(short, 0)
        self.assertEqual(plan, [DumpAction("Trinket", 50, None)])   # good used, supply untouched

    def test_excess_supply_used_after_goods_exhausted(self):
        cargo = [CargoItem("Trinket", 20, 2),
                 CargoItem("Food", 300, 1, resource="food")]
        plan, short = _plan(50, cargo, reserves={"food": 200})
        self.assertEqual(short, 0)
        # 20 Trinket + 30 excess Food (300-200=100 excess available)
        self.assertEqual(plan, [DumpAction("Trinket", 20, None),
                                DumpAction("Food", 30, "food")])

    def test_reserves_from_route_longest_leg(self):
        r = reserves_from_route([1, 6, 4])
        self.assertEqual(r["water"], r["food"])
        self.assertGreater(r["water"], 0)

    def test_zero_overflow_dumps_nothing(self):
        cargo = [CargoItem("Trinket", 100, 2)]
        self.assertEqual(_plan(0, cargo, reserves={}), ([], 0))


if __name__ == "__main__":
    unittest.main()
