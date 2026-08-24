"""Tests for the supply-reserve / voyage-length solver (deterministic, no game)."""
import math
import unittest

from brain.supply_planner import (
    Supply, PER_DAY_EACH,
    days_of_supply, supply_needed_each, route_reserve_each,
    route_supply_ok, free_sail_supply_ok, voyage_days_from_distance,
)


class SupplyPlannerTests(unittest.TestCase):
    def test_baseline_329_each_lasts_12_days(self):
        # Observed: 329 water + 329 food = 12 days.
        self.assertAlmostEqual(days_of_supply(Supply(329, 329)), 12.0, places=5)

    def test_days_limited_by_whichever_runs_out_first(self):
        # 300 water, 329 food → water limits it.
        self.assertLess(days_of_supply(Supply(300, 329)), days_of_supply(Supply(329, 329)))
        self.assertAlmostEqual(days_of_supply(Supply(100, 329)), 100 / PER_DAY_EACH, places=5)

    def test_supply_needed_each_rounds_up(self):
        self.assertEqual(supply_needed_each(6), math.ceil(6 * PER_DAY_EACH))   # 165
        self.assertEqual(supply_needed_each(0), 0)

    def test_route_reserve_uses_longest_leg_not_sum(self):
        # legs 1d, 6d, 4d → reserve sized to the 6d leg (+margin), NOT the 11d total.
        reserve = route_reserve_each([1, 6, 4], margin_days=0.5)
        self.assertEqual(reserve, supply_needed_each(6 + 0.5))
        self.assertLess(reserve, supply_needed_each(11))   # much less than the total voyage

    def test_route_ok_even_when_supply_below_total_voyage(self):
        # Post-jettison: 200 each; legs 1d/6d/4d (total 11d). Supply lasts ~7.3 days —
        # LESS than the 11-day total, but ENOUGH for the 6-day longest leg → route OK.
        supply = Supply(200, 200)
        self.assertLess(days_of_supply(supply), 11)                 # < total voyage
        self.assertTrue(route_supply_ok(supply, [1, 6, 4]))         # but route is fine
        # ...and it would NOT be enough to free-sail the whole 11 days:
        self.assertFalse(free_sail_supply_ok(supply, voyage_days=11))

    def test_route_not_ok_when_below_longest_leg(self):
        # Only ~3 days of supply, longest leg 6d → not enough even for the worst leg.
        supply = Supply(int(3 * PER_DAY_EACH), int(3 * PER_DAY_EACH))
        self.assertFalse(route_supply_ok(supply, [1, 6, 4]))

    def test_route_requires_both_water_and_food(self):
        # Plenty of food, but water short of the longest leg → not OK.
        need6 = route_reserve_each([6])
        self.assertFalse(route_supply_ok(Supply(need6 - 10, need6 + 100), [6]))
        self.assertTrue(route_supply_ok(Supply(need6, need6), [6]))

    def test_free_sail_needs_whole_voyage_plus_margin(self):
        self.assertTrue(free_sail_supply_ok(Supply(329, 329), voyage_days=8))    # 12d ≥ 8+1
        self.assertFalse(free_sail_supply_ok(Supply(329, 329), voyage_days=12))  # 12d < 12+1

    def test_empty_route_reserve_is_zero(self):
        self.assertEqual(route_reserve_each([]), 0)

    def test_voyage_days_from_distance(self):
        self.assertAlmostEqual(voyage_days_from_distance(1000, 200), 5.0)
        self.assertEqual(voyage_days_from_distance(1000, 0), math.inf)


if __name__ == "__main__":
    unittest.main()


# ── Live monitoring cadence (user 2026-08-20: 1 game day ≈ 1 real minute) ─────

from brain.supply_planner import (GAME_DAY_SECONDS, per_day_from_reading,
                                  supply_checkback_seconds)


def test_checkback_matches_the_users_reference_case():
    # "supply can last 5 days → check back after 6 minutes" (5 − 1 margin) × 90s.
    assert supply_checkback_seconds(5) == 6 * 60


def test_checkback_leaves_supply_in_hand_rather_than_waiting_it_out():
    # The re-read must land BEFORE the tank would be dry, never after.
    for days in (2, 5, 9, 12):
        assert supply_checkback_seconds(days) < days * GAME_DAY_SECONDS


def test_a_nearly_dry_fleet_is_floored_not_busy_polled():
    assert supply_checkback_seconds(1) == 60.0
    assert supply_checkback_seconds(0) == 60.0
    assert supply_checkback_seconds(-3) == 60.0


def test_per_day_calibrates_from_one_reading():
    assert abs(per_day_from_reading(329, 12) - 27.42) < 0.01
    assert per_day_from_reading(0, 12) is None
    assert per_day_from_reading(329, 0) is None
