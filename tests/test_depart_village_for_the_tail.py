"""The tail leaves the village on purpose.

A village has no world-map control, so the fleet must be at sea before the Route tab can be
reached (user, 2026-08-23). `open_world_map` refuses from a village now — it used to tap the
calibrated port globe at (2227,361) into a Check-Barter-Effect control and hang — so without
a deliberate departure the tail cannot start at all.

Leaving a PLACE is never done to satisfy a state test; it is done because the TASK decided
the position can be spent. This is that decision, in one place.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from brain import barter_command


def _depart(states):
    """Run the departure against a scripted sequence of perceived states."""
    seq = list(states)
    backs = []
    with patch("actions.sail_actions.where_am_i",
               side_effect=lambda *_a, **_k: {"location": seq.pop(0)}), \
         patch("actions.sail_actions.press_back", side_effect=lambda: backs.append(1)), \
         patch("capture.adb_capture.capture_screen", return_value=object()), \
         patch("time.sleep", lambda *_a, **_k: None):
        ok = barter_command._depart_village_to_sea()
    return ok, len(backs)


class DepartingForTheTail(unittest.TestCase):

    def test_it_backs_out_until_it_reaches_the_sea(self):
        ok, backs = _depart(["village", "village", "sea"])
        self.assertTrue(ok)
        self.assertEqual(backs, 2)

    def test_already_at_sea_presses_nothing(self):
        ok, backs = _depart(["sea"])
        self.assertTrue(ok)
        self.assertEqual(backs, 0, "no Back when the fleet is already clear")

    def test_the_world_map_counts_as_clear(self):
        ok, backs = _depart(["world_map"])
        self.assertTrue(ok)
        self.assertEqual(backs, 0)

    def test_it_gives_up_rather_than_pressing_back_forever(self):
        ok, backs = _depart(["village"] * 6)
        self.assertFalse(ok)
        self.assertLessEqual(backs, 4, "bounded — Back is not a strategy")

    def test_an_unreadable_state_stops_it(self):
        """Never press Back blind: that is how the fleet lost the village twice."""
        with patch("actions.sail_actions.where_am_i", side_effect=RuntimeError("no frame")), \
             patch("capture.adb_capture.capture_screen", return_value=object()), \
             patch("actions.sail_actions.press_back") as back:
            self.assertFalse(barter_command._depart_village_to_sea())
        back.assert_not_called()


if __name__ == "__main__":
    unittest.main()
