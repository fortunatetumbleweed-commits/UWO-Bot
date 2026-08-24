"""Facts are remembered with a timestamp, and screens are known by what they can answer.

Live 2026-08-22, run 25:

    21:29:38  Overworld confirmed via SceneModel (title='Kolkata')
    21:31:45  Overworld confirmed: port name 'Kolkata' visible
    21:31:57  tap (2312,62)  hamburger -> main menu, to read the cargo
    21:33:36  [classify] -> main_menu
    21:33:52  [mission] current port unreadable — cannot place the fleet on the map

The port name is painted on the port overworld and NOWHERE else — `perceive` only attempts
the OCR when the family classifier says `port_overworld`. Retrying on the main menu could
never succeed, and the answer had been in hand twice, two minutes earlier.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from brain.observation import screen_shows_settlement
from memory import observed_facts

COORDS = {"Kolkata": (7124, 2310), "Male": (6600, 2800)}


class ScreenKnowledge(unittest.TestCase):

    def test_the_overworld_shows_the_settlement(self):
        self.assertTrue(screen_shows_settlement("port_overworld"))

    def test_the_main_menu_does_not(self):
        self.assertFalse(screen_shows_settlement("main_menu"))

    def test_inside_a_building_it_does_not(self):
        """There the title is the sub-menu ("Sell"), not the port."""
        self.assertFalse(screen_shows_settlement("sub_menu"))
        self.assertFalse(screen_shows_settlement("building"))

    def test_an_unknown_state_is_not_assumed_to_show_it(self):
        self.assertFalse(screen_shows_settlement(None))


class TimestampedFacts(unittest.TestCase):

    def setUp(self):
        observed_facts._reset_for_tests()
        self._save = patch.object(observed_facts, "_save", lambda: None)
        self._save.start()
        self.addCleanup(self._save.stop)

    def test_a_fact_carries_its_age(self):
        observed_facts.remember("settlement", "Kolkata", now=1000.0)
        value, age = observed_facts.recall("settlement", now=1120.0)
        self.assertEqual(value, "Kolkata")
        self.assertAlmostEqual(age, 120.0)

    def test_a_stale_fact_is_withheld(self):
        observed_facts.remember("settlement", "Kolkata", now=1000.0)
        self.assertIsNone(observed_facts.recall("settlement", max_age_s=60, now=1120.0))

    def test_remembering_again_refreshes_the_stamp(self):
        observed_facts.remember("settlement", "Kolkata", now=1000.0)
        observed_facts.remember("settlement", "Kolkata", now=1100.0)
        _v, age = observed_facts.recall("settlement", now=1120.0)
        self.assertAlmostEqual(age, 20.0)

    def test_forgetting_removes_it(self):
        observed_facts.remember("settlement", "Kolkata", now=1000.0)
        observed_facts.forget("settlement")
        self.assertIsNone(observed_facts.recall("settlement", now=1000.0))

    def test_an_unknown_key_is_None_not_an_error(self):
        self.assertIsNone(observed_facts.recall("nothing-here"))


class CurrentPositionUsesMemory(unittest.TestCase):

    def setUp(self):
        observed_facts._reset_for_tests()
        self._save = patch.object(observed_facts, "_save", lambda: None)
        self._save.start()
        self.addCleanup(self._save.stop)

    def _position(self, where):
        from brain import barter_mission_live as bml
        with patch("actions.sail_actions.where_am_i", return_value=where), \
             patch("capture.adb_capture.capture_screen", return_value=object()):
            return bml.current_position(COORDS)

    def test_a_read_port_is_remembered(self):
        self._position({"location": "port_overworld", "port": "Kolkata"})
        value, _age = observed_facts.recall("settlement")
        self.assertEqual(value, "Kolkata")

    def test_the_main_menu_falls_back_to_what_was_seen(self):
        self._position({"location": "port_overworld", "port": "Kolkata"})
        self.assertEqual(self._position({"location": "main_menu", "port": None}),
                         COORDS["Kolkata"])

    def test_with_nothing_remembered_it_refuses_rather_than_guessing(self):
        """A fabricated origin reorders the whole voyage — None is the safe answer."""
        self.assertIsNone(self._position({"location": "main_menu", "port": None}))

    def test_departure_clears_the_remembered_settlement(self):
        self._position({"location": "port_overworld", "port": "Kolkata"})
        observed_facts.forget("settlement")          # what departure does
        self.assertIsNone(self._position({"location": "main_menu", "port": None}))


if __name__ == "__main__":
    unittest.main()


class OnlyRealPortsAreRemembered(unittest.TestCase):
    """`read_port_name` returns an unmatched read RAW, so chrome text arrives as a "port".

    Live 2026-08-22: "[read_port_name] raw OCR 'Lauless Waters' did not match any known port
    (best similarity 0.50); returning raw read" — that is the Lawless Waters SEA REGION.
    Remembering it would hand a fabricated origin to every later lookup, which is precisely
    what the None-not-(0,0) fallback exists to prevent.
    """

    def setUp(self):
        observed_facts._reset_for_tests()
        self._save = patch.object(observed_facts, "_save", lambda: None)
        self._save.start()
        self.addCleanup(self._save.stop)

    def _position(self, where):
        from brain import barter_mission_live as bml
        with patch("actions.sail_actions.where_am_i", return_value=where), \
             patch("capture.adb_capture.capture_screen", return_value=object()):
            return bml.current_position(COORDS)

    def test_an_uncatalogued_name_is_not_remembered(self):
        self._position({"location": "port_overworld", "port": "Lauless Waters"})
        self.assertIsNone(observed_facts.recall("settlement"))

    def test_an_uncatalogued_name_does_not_place_the_fleet(self):
        self.assertIsNone(self._position({"location": "port_overworld",
                                          "port": "Lauless Waters"}))

    def test_it_cannot_poison_a_later_lookup(self):
        """A good reading must survive a junk one that follows it."""
        self._position({"location": "port_overworld", "port": "Kolkata"})
        self._position({"location": "port_overworld", "port": "Lauless Waters"})
        self.assertEqual(self._position({"location": "main_menu", "port": None}),
                         COORDS["Kolkata"])
