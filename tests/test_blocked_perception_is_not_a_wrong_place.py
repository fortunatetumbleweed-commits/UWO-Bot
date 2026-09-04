"""An unreadable state is not evidence that the fleet is in the wrong place.

Live 2026-08-23, run 29 — an announcement popup covered the screen as the fleet ARRIVED at
Melanesian Village:

    [sail_to] tick=4  state='unknown'  detail='Sailing to Melanesian Village'
    [sail_to] Unexpected state 'unknown' — planning back to overworld

From a village, "back to overworld" means SAILING AWAY. The popup never blocked the game —
the fleet arrived fine — it blocked the bot's READING, and the bot navigated on the blindness,
giving up the position the mission had just spent a voyage on.

The popup is ORTHOGONAL to the state, never a state of its own: it can sit over a village, a
market or the sea, and collapsing all of those into 'unknown' destroys exactly the information
the recovery needed. Clear it, re-perceive, then decide.

2026-08-27 — THE SAME FAILURE, A THIRD TIME. Clearing the blocker first was only half the
fix: when nothing was blocking, `_handle_unknown` still planned navigation, and this file
PINNED that ("the recovery must survive for the case it exists for"). The fleet committed a
departure to Svear Village, the departure cinematic came up, and the planned recovery sailed
it back to Barcelona — cancelling the voyage and reporting success.

So the goal no longer navigates AT ALL. It takes ONE exit (`exit_current_screen` — Home, the
dialog X, or the in-game back arrow, refusing a system back on an overworld), re-perceives on
the next tick, and after three unreadable ticks reports FAILED for the layer above to act on.
Where an exit lands is the next perceive's business; where the FLEET should be is never this
goal's decision.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from brain.goals.sail_to import SailToGoal, SailPhase


class BlockedPerceptionClearsBeforeNavigating(unittest.TestCase):

    def _handle_unknown(self, *, cleared: bool):
        """Drive `_handle_unknown` with a blocker that does / does not clear."""
        goal = SailToGoal("Melanesian Village")
        goal.phase = SailPhase.SAILING
        state = type("S", (), {"state": "unknown", "port": None,
                               "detail": "Sailing to Melanesian Village", "flow": None})()
        planned = []
        with patch("brain.unexpected_dialog.clear_blockers",
                   return_value={"cleared": cleared}), \
             patch("capture.adb_capture.capture_screen", return_value=object()), \
             patch("brain.planner.get_planner") as planner:
            planner.return_value.plan_to.side_effect = \
                lambda *a, **k: planned.append("plan_to") or True
            result = goal._handle_unknown(state)
        return result, planned

    def test_a_blocker_is_cleared_instead_of_navigating(self):
        result, planned = self._handle_unknown(cleared=True)
        self.assertEqual(planned, [], "no navigation may be planned while a popup is up")
        self.assertEqual(result.action, "blocker_cleared")

    def test_it_takes_one_exit_and_does_not_navigate(self):
        """With nothing blocking, the goal must EXIT the screen, never plan a course."""
        exits = []
        goal = SailToGoal("Melanesian Village")
        goal.phase = SailPhase.SAILING
        state = type("S", (), {"state": "unknown", "port": None,
                               "detail": "Sailing to Melanesian Village", "flow": None})()
        planned = []
        with patch("brain.unexpected_dialog.clear_blockers", return_value={"cleared": False}), \
             patch("capture.adb_capture.capture_screen", return_value=object()), \
             patch("actions.screen_exit.exit_current_screen",
                   side_effect=lambda *a, **k: exits.append("exit")), \
             patch("brain.planner.get_planner") as planner:
            planner.return_value.plan_to.side_effect = \
                lambda *a, **k: planned.append("plan_to") or True
            goal._handle_unknown(state)
        self.assertEqual(planned, [], "planning a course is how the fleet got sailed home")
        self.assertEqual(exits, ["exit"], "one exit, then re-perceive")

    def test_a_failing_blocker_check_still_takes_the_exit(self):
        """A blocker check that raises must not stop the goal getting off the screen."""
        exits = []
        goal = SailToGoal("Melanesian Village")
        goal.phase = SailPhase.SAILING
        state = type("S", (), {"state": "unknown", "port": None, "detail": "", "flow": None})()
        with patch("brain.unexpected_dialog.clear_blockers",
                   side_effect=RuntimeError("no frame")), \
             patch("capture.adb_capture.capture_screen", return_value=object()), \
             patch("actions.screen_exit.exit_current_screen",
                   side_effect=lambda *a, **k: exits.append("exit")):
            goal._handle_unknown(state)
        self.assertEqual(exits, ["exit"])

    def test_a_transient_is_the_committed_action_still_landing(self):
        """Not unexpected at all — the departure cinematic IS the departure working."""
        exits = []
        goal = SailToGoal("Svear Village")
        goal.phase = SailPhase.SAILING
        state = type("S", (), {"state": "transient", "port": None,
                               "detail": "full-screen notice", "flow": None})()
        with patch("brain.unexpected_dialog.clear_blockers", return_value={"cleared": False}), \
             patch("capture.adb_capture.capture_screen", return_value=object()), \
             patch("actions.screen_exit.exit_current_screen",
                   side_effect=lambda *a, **k: exits.append("exit")):
            result = goal._handle_unknown(state)
        self.assertEqual(exits, [], "a cinematic must not be tapped away or recovered from")
        self.assertEqual(result.action, "settling")


if __name__ == "__main__":
    unittest.main()
