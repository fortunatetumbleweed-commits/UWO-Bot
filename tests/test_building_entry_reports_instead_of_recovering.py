"""Entering a building is a tap and a report — never a recovery.

`navigate_to_building` used to be the bot's most eager recoverer. From inside one call it
pressed Back on a sub-menu, Backed out of a wrong building, Backed off the world map, and
Home-escaped when Back stopped working. Every branch was locally reasonable, and one of them
cost 18.5 minutes: on 2026-08-22 the departure had already SUCCEEDED and the screen read
"sailing to Melanesian Village", so this function judged "not the harbour" and pressed Back —
cancelling the voyage. Four times.

The tap is this function's own effect. Where the bot IS belongs to the task.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import actions.sail_actions as sa

MARKET_LIST = [("Harbor", 2180, 300), ("Market", 2180, 400), ("Shipyard", 2180, 500),
               ("Bank", 2180, 600)]
# Bordeaux, 2026-08-24: the right panel was on the TASKS tab, and one quest objective
# contained the word "market".
TASKS_PANEL = [("Move to Market in Jakarta and deliver", 2180, 300),
               ("Reward: 30,000 ducats", 2180, 400)]


class WhichRowIsTheBuilding(unittest.TestCase):

    def test_the_plain_label_matches(self):
        self.assertEqual(sa._building_row(MARKET_LIST, "market"), (2180, 400))

    def test_a_quest_sentence_containing_the_name_does_not(self):
        """This exact match sent the fleet on a quest voyage to Jakarta."""
        self.assertIsNone(sa._building_row(TASKS_PANEL, "market"))

    def test_ocr_mangling_of_the_real_label_still_matches(self):
        self.assertIsNotNone(sa._building_row([("Markot", 2180, 400)], "market"))

    def test_a_short_decoration_is_tolerated(self):
        self.assertEqual(sa._building_row([("the Market", 2180, 400)], "market"), (2180, 400))


class RefusingToTap(unittest.TestCase):

    def test_it_will_not_tap_a_panel_that_is_not_the_building_list(self):
        """Knowing it is the wrong list and tapping anyway is the worst outcome."""
        with patch.object(sa, "_nameplate_for", return_value=None), \
             patch.object(sa, "select_buildings_tab",
                          return_value={"ok": False, "frame": None, "reason": "no tab icons"}), \
             patch.object(sa, "tap") as tap:
            res = sa.tap_building_entry("Market", frame=object())
        self.assertFalse(res["tapped"])
        self.assertEqual(res["reason"], "no tab icons")
        tap.assert_not_called()

    def test_a_tap_reports_where_it_tapped(self):
        with patch.object(sa, "_nameplate_for", return_value=None), \
             patch.object(sa, "select_buildings_tab",
                          return_value={"ok": True, "frame": object(), "reason": ""}), \
             patch("vision.ocr.read_building_menu", return_value=MARKET_LIST), \
             patch.object(sa, "tap") as tap:
            res = sa.tap_building_entry("Market", frame=object())
        self.assertEqual((res["tapped"], res["via"], res["position"]), (True, "list", (2180, 400)))
        tap.assert_called_once_with(2180, 400)


class TheTitleTest(unittest.TestCase):

    def test_the_name_in_the_title_is_enough(self):
        self.assertTrue(sa.inside_building("harbor", "Harbour"))

    def test_a_different_building_is_not(self):
        self.assertFalse(sa.inside_building("harbor", "Market"))

    def test_an_unreadable_title_is_accepted(self):
        """The alternative is walking back out of a building the bot is probably standing in."""
        self.assertTrue(sa.inside_building("market", "building (title unreadable)"))
        self.assertTrue(sa.inside_building("market", ""))

    def test_a_qwen_description_is_stripped_before_matching(self):
        """Qwen appends after ' — ' and hallucinates; the OCR title alone decides."""
        self.assertTrue(sa.inside_building("inn", "Inn — a bustling harbor tavern"))


class WhereTheBotIsBelongsToTheTask(unittest.TestCase):
    """Each of these was a Back-press in the old function."""

    def _run(self, location, detail=""):
        loc = {"location": location, "detail": detail}
        with patch.object(sa, "capture_screen", return_value=object()), \
             patch.object(sa, "harbor_panel_open", return_value=False), \
             patch("brain.perceive.perceive") as perceive, \
             patch.object(sa, "press_back") as back, \
             patch.object(sa, "tap_building_entry") as entry, \
             patch("brain.unexpected_dialog.clear_blockers", return_value={"cleared": False}):
            perceive.return_value.to_location_dict.return_value = loc
            ok = sa.navigate_to_building("harbor", timeout=5.0)
        return ok, back, entry

    def test_a_sub_menu_is_handed_back_not_backed_out_of(self):
        ok, back, _ = self._run("sub_menu", "sub_menu: Recruit Crew")
        self.assertFalse(ok)
        back.assert_not_called()

    def test_the_world_map_is_handed_back(self):
        ok, back, _ = self._run("world_map")
        self.assertFalse(ok)
        back.assert_not_called()

    def test_the_open_sea_is_handed_back(self):
        ok, back, entry = self._run("sea")
        self.assertFalse(ok)
        back.assert_not_called()
        entry.assert_not_called()

    def test_a_wrong_building_is_reported_not_escaped(self):
        ok, back, _ = self._run("building", "building: Bank")
        self.assertFalse(ok)
        back.assert_not_called()

    def test_a_fleet_under_way_is_left_alone(self):
        """The 18.5 minutes. The departure had succeeded; Back cancelled it."""
        ok, back, entry = self._run("building", "building: Sailing to Melanesian Village")
        self.assertFalse(ok)
        back.assert_not_called()
        entry.assert_not_called()

    def test_being_inside_the_target_is_success(self):
        ok, _back, _ = self._run("building", "building: Harbour")
        self.assertTrue(ok)


class TheWalkIsNotRetapped(unittest.TestCase):

    def test_a_second_tick_on_the_overworld_waits_rather_than_tapping_again(self):
        """A retap issued mid-walk lands inside the building once the scene loads — on an NPC
        in the inn, on a market shelf. There is no local signal separating "walking" from
        "the tap missed", so patience is the only correct answer."""
        loc = {"location": "port_overworld", "detail": "Lisboa"}
        taps = []
        with patch.object(sa, "capture_screen", return_value=object()), \
             patch.object(sa, "harbor_panel_open", return_value=False), \
             patch("brain.perceive.perceive") as perceive, \
             patch.object(sa, "time") as fake_time, \
             patch("brain.unexpected_dialog.clear_blockers", return_value={"cleared": False}), \
             patch.object(sa, "tap_building_entry",
                          side_effect=lambda n, f: taps.append(n) or
                          {"tapped": True, "via": "list", "position": (1, 2), "reason": ""}):
            perceive.return_value.to_location_dict.return_value = loc
            clock = iter([0.0, 0.0, 1.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 99.0])
            fake_time.time.side_effect = lambda: next(clock, 99.0)
            fake_time.sleep = lambda *_: None
            sa.navigate_to_building("market", timeout=10.0)
        self.assertEqual(len(taps), 1, f"tapped {len(taps)}× during one walk")


class LeavingAScreenIsNotRepositioningTheFleet(unittest.TestCase):
    """`exit_to_overworld` used to escalate into `recover_to_port_overworld` after two looks
    with no exit control. That function's sea branch SAILS THE FLEET to a home port — so
    failing to close a market panel could put to sea, and the two could re-enter each other
    with no shared budget."""

    def _run(self, *, on_overworld, tapped, cleared=False):
        with patch.object(sa, "capture_screen", return_value=object()), \
             patch.object(sa, "_is_on_overworld", side_effect=on_overworld), \
             patch.object(sa, "tap_exit_to_overworld",
                          return_value={"tapped": tapped, "control": "home", "position": (1, 2)}), \
             patch("brain.unexpected_dialog.clear_blockers", return_value={"cleared": cleared}), \
             patch("brain.recovery.recover_to_port_overworld") as recover, \
             patch.object(sa.time, "sleep"):
            ok = sa.exit_to_overworld(timeout=8.0)
        return ok, recover

    def test_home_gets_us_out(self):
        ok, _ = self._run(on_overworld=[False, True], tapped=True)
        self.assertTrue(ok)

    def test_no_exit_control_reports_rather_than_sailing(self):
        ok, recover = self._run(on_overworld=[False] * 8, tapped=False)
        self.assertFalse(ok)
        recover.assert_not_called()

    def test_a_blocker_covering_the_control_is_cleared_first(self):
        """Something on top of the control IS this function's business — it needs that control."""
        ok, recover = self._run(on_overworld=[False, False, True], tapped=False, cleared=True)
        self.assertTrue(ok)
        recover.assert_not_called()
