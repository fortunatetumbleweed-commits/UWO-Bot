"""Tests for _supply_sufficient — the free-sail HUD-days supply decision.

The sea HUD reports supply and ETA directly in game-days ('N Days of Sailing
Left', 'ETA n d'), so the free-sail check is a simple days comparison with a
safety buffer. Unreadable HUD → None (caller must not assume sufficiency).
"""
import unittest

from actions.task_runner import _supply_sufficient, _RESUPPLY_BUFFER_DAYS


class SupplySufficientTests(unittest.TestCase):
    def test_sufficient_with_buffer(self):
        # 11 days supply, ETA 5 → 11 >= 5 + 2 → True
        self.assertTrue(_supply_sufficient(11, 5))

    def test_exactly_at_buffer_is_sufficient(self):
        self.assertTrue(_supply_sufficient(7, 5))          # 7 == 5 + 2

    def test_insufficient_within_buffer(self):
        self.assertFalse(_supply_sufficient(6, 5))         # 6 < 5 + 2

    def test_insufficient_below_eta(self):
        self.assertFalse(_supply_sufficient(3, 5))

    def test_none_when_supply_unreadable(self):
        self.assertIsNone(_supply_sufficient(None, 5))

    def test_none_when_eta_unreadable(self):
        self.assertIsNone(_supply_sufficient(11, None))

    def test_custom_buffer(self):
        self.assertTrue(_supply_sufficient(6, 5, buffer_days=1))
        self.assertFalse(_supply_sufficient(6, 5, buffer_days=2))

    def test_default_buffer_is_two(self):
        self.assertEqual(_RESUPPLY_BUFFER_DAYS, 2)


if __name__ == "__main__":
    unittest.main()


# ── Dynamic mid-voyage cadence (user 2026-08-20) ─────────────────────────────
# "After the ship is set to sail, check the supply days, and recheck about 1 day
# before it runs out." The watch schedules itself off the HUD instead of polling
# on a fixed clock.

from unittest import mock

from actions.task_runner import (_SUPPLY_CHECK_FALLBACK_S, _ensure_supply,
                                 _next_supply_check_s)
from brain.supply_planner import GAME_DAY_SECONDS


class SupplyCadenceTests(unittest.TestCase):
    def test_it_comes_back_about_one_game_day_before_empty(self):
        for days in (12, 9, 5, 3):
            wait = _next_supply_check_s(days)
            self.assertAlmostEqual(wait, (days - 1) * GAME_DAY_SECONDS, places=6)
            self.assertLess(wait, days * GAME_DAY_SECONDS,
                            "must land BEFORE the tank is dry, never after")

    def test_the_users_reference_case(self):
        # 5 days aboard → look again in ~6 real minutes.
        self.assertEqual(_next_supply_check_s(5), 6 * 60)

    def test_a_nearly_dry_tank_is_floored_not_busy_polled(self):
        self.assertEqual(_next_supply_check_s(1), 60.0)
        self.assertEqual(_next_supply_check_s(0), 60.0)

    def test_unreadable_supply_is_not_treated_as_plenty(self):
        wait = _next_supply_check_s(None)
        self.assertEqual(wait, _SUPPLY_CHECK_FALLBACK_S)
        self.assertLess(wait, _next_supply_check_s(5))     # blind → look again sooner


class EnsureSupplyReportsWhatItReadTests(unittest.TestCase):
    """The cadence can only be dynamic if the check hands back what it saw."""

    def _run(self, location, hud):
        with mock.patch("actions.sail_actions.where_am_i",
                        return_value={"location": location, "port": "Jakarta"}), \
             mock.patch("actions.sail_actions.read_sea_hud", return_value=hud):
            return _ensure_supply("Melanesian Village")

    def test_returns_the_days_it_read_at_sea(self):
        self.assertEqual(self._run("sea", {"supply_days": 9, "eta_days": 3}), 9)

    def test_returns_none_in_port_so_the_caller_uses_the_fallback(self):
        # In port the harbour's Supply Departure handles restocking — nothing to time off.
        self.assertIsNone(self._run("port_overworld", {}))
        self.assertEqual(_next_supply_check_s(self._run("port_overworld", {})),
                         _SUPPLY_CHECK_FALLBACK_S)

    def test_an_unreadable_hud_does_not_fabricate_days(self):
        self.assertIsNone(self._run("sea", {"supply_days": None, "eta_days": None}))
