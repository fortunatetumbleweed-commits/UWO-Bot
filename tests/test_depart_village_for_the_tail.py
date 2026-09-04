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
    """Run the departure against a scripted sequence of perceived states.

    THE SEAM MOVED. The departure no longer perceives — it returns a `ClearOfTheVillage` work
    order and reads `state` on the next consult, so what a test scripts is what the DISPATCHER
    perceives, not what `where_am_i` returns. The last state repeats once the script runs out,
    the way a screen that nothing changes keeps reading the same.
    """
    import types

    seq = list(states)
    backs = []

    def _perceive():
        w = seq.pop(0) if len(seq) > 1 else seq[0]
        return types.SimpleNamespace(state=w, location=w, port=None, frame=None)

    with patch("actions.adb_actions.press_back", side_effect=lambda: backs.append(1)), \
         patch("brain.run_goal._refined_state", _perceive), \
         patch("time.sleep", lambda *_a, **_k: None):
        ok = barter_command._depart_village_to_sea()
    return ok, len(backs)


class DepartingForTheTail(unittest.TestCase):

    def test_it_backs_out_until_it_reaches_the_sea(self):
        """Two villages seen, two Backs pressed.

        The old loop pressed ONCE here. `finish_current_activity()` — the escape hatch for
        screens that end by finishing rather than by Back, meant for the idle lock — fired on
        an ordinary village and `continue`d past the press it was standing in for. The test
        recorded the bug as a failure for weeks. Under the dispatcher there is no such branch
        here at all: a screen that ends by finishing is a clearing activity's business.
        """
        ok, backs = _depart(["sub_menu:barter"] * 3 + ["village"] * 3 + ["sea"])
        self.assertTrue(ok)
        self.assertGreater(backs, 1, "each screen on the way out gets its own Back")

    def test_already_at_sea_presses_nothing(self):
        ok, backs = _depart(["sea"])
        self.assertTrue(ok)
        self.assertEqual(backs, 0, "no Back when the fleet is already clear")

    def test_the_world_map_counts_as_clear(self):
        ok, backs = _depart(["world_map"])
        self.assertTrue(ok)
        self.assertEqual(backs, 0)

    def test_it_stops_as_soon_as_it_is_clear(self):
        """One village, then the sea: it must not keep pressing into open water."""
        ok, backs = _depart(["village", "sea"])
        self.assertTrue(ok)
        self.assertLessEqual(backs, 1)

    def test_a_screen_that_does_not_change_is_pressed_ONCE(self):
        """The dispatcher does not repeat an action whose effect it has not observed.

        The old loop pressed Back up to four times at a screen that never moved, because it
        had no way to tell a slow transition from a missed tap. The dispatcher does: it holds
        the intent in flight until the state changes. One press, then it waits and looks.
        """
        ok, backs = _depart(["village"] * 40)
        self.assertFalse(ok)
        self.assertEqual(backs, 1, "an unchanging screen is not pressed again")

    def test_an_unreadable_state_stops_it(self):
        """Never press Back blind: that is how the fleet lost the village twice."""
        with patch("brain.run_goal._refined_state", side_effect=RuntimeError("no frame")), \
             patch("actions.adb_actions.press_back") as back, \
             patch("time.sleep", lambda *_a, **_k: None):
            self.assertFalse(barter_command._depart_village_to_sea())
        back.assert_not_called()

    def test_the_main_menu_is_reported_not_pressed(self):
        """Back means "Exit Game?" there. Live 2026-08-26 the fleet was at Stockholm — moved
        for supply, not in a village at all — and the old loop pressed Back twice, standing
        one positive tap from quitting the game."""
        ok, backs = _depart(["main_menu"])
        self.assertFalse(ok)
        self.assertEqual(backs, 0)


if __name__ == "__main__":
    unittest.main()
