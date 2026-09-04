"""A departure cinematic is what SUCCESS looks like from inside the tap.

Live 2026-08-26, Seville → Barcelona. The departure worked: the auto-supply notice was
confirmed and the fleet put to sea. But the post-tap check in `_navigate_world_map_to_port`
matched the new state against a list that had never seen it:

    18:26:08  tap 'ok' — confirm departure to Barcelona      <- it worked
    18:26:24  [classify] -> transient (conf=1.00)            <- the departure cinematic
    18:26:26  Unexpected location 'transient' after tap — aborting
    18:26:26  [sail_to] could not commit the departure
    18:26:37  [classify] -> sea                              <- eleven seconds later

Holding that verdict, the goal re-opened the world map MID-VOYAGE, re-targeted Seville — the
port it had just left — sailed back, bought nothing, and started again.

Two lessons, and the second is the general one:

  * `transient` was introduced as a state string that same morning. Every allow-list in the
    codebase became a possible blind spot the moment it existed, and this is what that looks
    like when it lands on a critical path.
  * The check is a CONCLUSION about its own success (CLAUDE.md: never store a conclusion),
    drawn from an 8-second peek at a world mid-change. Widening the list fixes this instance.
    Deleting the verdict — tap, return, let the next perceive decide — is the real fix, and
    it is a larger change to the sailing path than this one.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from actions import sail_actions


class TheDepartureTapIsNotJudgedByAMidChangeScreen(unittest.TestCase):

    def _navigate(self, state_after):
        """Drive the Go-to-City loop with the post-tap read stubbed to `state_after`."""
        seen = {"aborted": False, "returned": None}

        def _wait(expected=(), max_wait=0.0):
            seen["expected"] = tuple(expected)
            return {"location": state_after}

        with patch.object(sail_actions, "_wait_for_state_change", side_effect=_wait), \
             patch.object(sail_actions, "confirm_departure_notice", return_value=None), \
             patch.object(sail_actions, "tap"), \
             patch.object(sail_actions, "_panel_showing", return_value=(False, None, None)):
            seen["returned"] = self._call_loop()
        return seen

    def _call_loop(self):
        """The loop under test lives inside `_navigate_world_map_to_port`; exercise the
        decision directly against the same tuple the code uses, so this test cannot pass by
        agreeing with a copy of the list."""
        import inspect
        src = inspect.getsource(sail_actions._navigate_world_map_to_port)
        line = next(l for l in src.splitlines() if "in_flux = (" in l)
        return eval(line.split("=", 1)[1].strip())          # the real tuple, not a restatement

    def test_a_cinematic_counts_as_the_departure_having_started(self):
        self.assertIn("transient", self._call_loop(),
                      "a departure cinematic is a full-screen notice, and reads as transient")

    def test_the_states_that_already_counted_still_do(self):
        for state in ("sea", "sea_cinematic", "loading", "port_overworld"):
            with self.subTest(state=state):
                self.assertIn(state, self._call_loop())

    def test_the_world_map_is_not_in_the_set(self):
        """Still on the map means the tap missed — that path re-reads the button and
        retries, and must not be swallowed as success."""
        self.assertNotIn("world_map", self._call_loop())


class EveryStateStringPerceiveCanReturnIsAccountedFor(unittest.TestCase):
    """The general hazard: a NEW state string silently fails every allow-list that predates
    it. `transient` was added on 2026-08-26 and cost a voyage the same day."""

    def test_transient_is_a_state_an_activity_serves(self):
        from brain.run_goal import default_activities
        self.assertIn("transient", default_activities(),
                      "if perception can return it, something must be able to act on it")


if __name__ == "__main__":
    unittest.main()
