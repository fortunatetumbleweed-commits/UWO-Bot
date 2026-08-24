"""Tests for the route-vs-free-sail supply branch (pure decision layer)."""
import unittest

from brain.supply_planner import Supply
from brain.supply_guard import (
    SailMode, SupplyVerdict, decide_supply,
)


class SupplyGuardTests(unittest.TestCase):
    # --- ROUTE mode: only the longest leg matters, not the total voyage ---
    def test_route_proceeds_when_covers_longest_leg(self):
        # 200 each ~ 7.3 days: below the 11d total but clears the 6d longest leg.
        d = decide_supply(Supply(200, 200), SailMode.ROUTE, leg_days=[1, 6, 4])
        self.assertIs(d.verdict, SupplyVerdict.PROCEED)

    def test_route_resupply_when_below_longest_leg(self):
        d = decide_supply(Supply(80, 80), SailMode.ROUTE, leg_days=[1, 6, 4])
        self.assertIs(d.verdict, SupplyVerdict.RESUPPLY_FIRST)

    def test_route_requires_leg_days(self):
        d = decide_supply(Supply(329, 329), SailMode.ROUTE)
        self.assertIs(d.verdict, SupplyVerdict.UNKNOWN)

    # --- FREE_SAIL mode: whole voyage must be covered ---
    def test_free_sail_proceeds_when_covers_voyage(self):
        d = decide_supply(Supply(329, 329), SailMode.FREE_SAIL, voyage_days=8)
        self.assertIs(d.verdict, SupplyVerdict.PROCEED)

    def test_free_sail_resupply_when_short(self):
        d = decide_supply(Supply(329, 329), SailMode.FREE_SAIL, voyage_days=12)
        self.assertIs(d.verdict, SupplyVerdict.RESUPPLY_FIRST)

    def test_free_sail_requires_voyage_days(self):
        d = decide_supply(Supply(329, 329), SailMode.FREE_SAIL)
        self.assertIs(d.verdict, SupplyVerdict.UNKNOWN)

    # --- The key contrast: same supply, route OK but free-sail not ---
    def test_same_supply_route_ok_free_sail_not(self):
        supply = Supply(200, 200)
        route = decide_supply(supply, SailMode.ROUTE, leg_days=[1, 6, 4])
        free = decide_supply(supply, SailMode.FREE_SAIL, voyage_days=11)
        self.assertIs(route.verdict, SupplyVerdict.PROCEED)
        self.assertIs(free.verdict, SupplyVerdict.RESUPPLY_FIRST)

    # --- Unreadable supply escalates, never silently proceeds ---
    def test_unknown_when_supply_none(self):
        d = decide_supply(None, SailMode.ROUTE, leg_days=[6])
        self.assertIs(d.verdict, SupplyVerdict.UNKNOWN)

    def test_unknown_when_one_resource_unreadable(self):
        d = decide_supply(Supply(329, None), SailMode.FREE_SAIL, voyage_days=5)
        self.assertIs(d.verdict, SupplyVerdict.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
