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

    def test_navigation_still_happens_when_nothing_is_blocking(self):
        """The recovery must survive for the case it exists for."""
        _result, planned = self._handle_unknown(cleared=False)
        self.assertEqual(planned, ["plan_to"])

    def test_a_failing_blocker_check_does_not_stop_the_recovery(self):
        goal = SailToGoal("Melanesian Village")
        goal.phase = SailPhase.SAILING
        state = type("S", (), {"state": "unknown", "port": None, "detail": "", "flow": None})()
        planned = []
        with patch("brain.unexpected_dialog.clear_blockers",
                   side_effect=RuntimeError("no frame")), \
             patch("capture.adb_capture.capture_screen", return_value=object()), \
             patch("brain.planner.get_planner") as planner:
            planner.return_value.plan_to.side_effect = \
                lambda *a, **k: planned.append("plan_to") or True
            goal._handle_unknown(state)
        self.assertEqual(planned, ["plan_to"])


if __name__ == "__main__":
    unittest.main()
