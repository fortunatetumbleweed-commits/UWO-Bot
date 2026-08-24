"""Departing by picking the destination FROM PORT, and the two ways it misbehaves.

User 2026-08-21: "picking destination on the sea is too time consuming and runs the risk
of running low on supply. The destination can be selected in a port... the player first
runs to the harbor, automatically supply and sets sail. But there are certain cases that
the player does not automatically leave the port, in this case you need to manually tap
Supply Depart, or sometimes the game has a bug, the fleet leaves the port but does not set
sail, the speed is 0, in this case the bot needs to set the destination again."""
import types
import unittest
from unittest import mock

import actions.sail_actions as sa


class PortDepartureTests(unittest.TestCase):
    def _run(self, locations, motion, *, select_ok=True, manual_ok=True, **kw):
        """locations: what where_am_i returns, in order. motion: results of the
        under-way check, in order."""
        locs, moves = iter(locations), iter(motion)
        selects, manual = [], []

        with mock.patch.object(sa, "where_am_i",
                               side_effect=lambda *a, **k: {"location": next(locs, "sea")}), \
             mock.patch.object(sa, "open_world_map", return_value=True), \
             mock.patch.object(sa, "_navigate_world_map_to_destination",
                               side_effect=lambda d, **k: (selects.append(d), select_ok)[1]), \
             mock.patch.object(sa, "_depart_from_harbour",
                               side_effect=lambda: (manual.append(1), manual_ok)[1]), \
             mock.patch.object(sa, "_confirm_making_way",
                               side_effect=lambda *a, **k: (next(moves, False), {})), \
             mock.patch.object(sa, "_wait_until_at_sea", return_value=kw.pop("at_sea", True)), \
             mock.patch("time.sleep"):
            res = sa.depart_from_port_via_world_map("Male", **kw)
        return res, selects, manual

    def test_the_happy_path_is_one_selection(self):
        res, selects, manual = self._run(["port_overworld"], [True])
        self.assertTrue(res["ok"])
        self.assertEqual(res["departed_via"], "auto")
        self.assertEqual(selects, ["Male"])       # one operation, no harbour trip
        self.assertEqual(manual, [])

    def test_staying_ashore_falls_back_to_supply_departure(self):
        # FAILURE 1: destination accepted, but the player never left.
        res, _s, manual = self._run(["port_overworld"], [True], at_sea=False)
        self.assertTrue(res["ok"])
        self.assertEqual(res["departed_via"], "supply_departure")
        self.assertEqual(len(manual), 1)

    def test_speed_zero_reselects_the_destination(self):
        # FAILURE 2: at sea, but the fleet never moves — re-select to kick it off.
        res, selects, _m = self._run(["port_overworld", "port_overworld"], [False, True])
        self.assertTrue(res["ok"])
        self.assertEqual(selects, ["Male", "Male"])   # selected a second time
        self.assertEqual(res["retries"], 1)

    def test_it_gives_up_with_a_reason_rather_than_looping(self):
        res, selects, _m = self._run(["port_overworld"] * 4, [False, False, False],
                                     max_retries=2)
        self.assertFalse(res["ok"])
        self.assertIn("never got under way", res["reason"])
        self.assertEqual(len(selects), 3)             # bounded: 1 + 2 retries

    def test_already_at_sea_skips_straight_to_the_motion_check(self):
        res, selects, _m = self._run(["sea"], [True])
        self.assertTrue(res["ok"])
        self.assertEqual(selects, [])                 # no pointless map trip
        self.assertEqual(res["departed_via"], "already at sea")

    def test_a_failed_selection_is_reported_not_retried_blindly(self):
        res, _s, _m = self._run(["port_overworld"], [True], select_ok=False)
        self.assertFalse(res["ok"])
        self.assertIn("could not select", res["reason"])


class MotionConfirmationTests(unittest.TestCase):
    """A selected destination is NOT evidence of movement — that is the speed-0 bug."""

    def _confirm(self, before, after):
        """_confirm_making_way returns (moving, hud_after) — the HUD comes back so the
        caller can check the destination without a second OCR pass. Only the flag is
        under test here."""
        from actions.route_execution import is_moving
        with mock.patch.object(sa, "read_sea_hud", side_effect=[before, after]), \
             mock.patch("time.sleep"):
            moving, _hud = sa._confirm_making_way(1.0, is_moving)
            return moving

    def test_a_falling_eta_counts_as_under_way(self):
        self.assertTrue(self._confirm({"eta_days": 6, "speed": 5},
                                      {"eta_days": 5, "speed": 5}))

    def test_a_static_eta_with_zero_speed_is_not(self):
        self.assertFalse(self._confirm({"eta_days": 6, "speed": 0},
                                       {"eta_days": 6, "speed": 0}))

    def test_a_rising_day_at_sea_counts(self):
        self.assertTrue(self._confirm({"day_at_sea": 3}, {"day_at_sea": 4}))


if __name__ == "__main__":
    unittest.main()
