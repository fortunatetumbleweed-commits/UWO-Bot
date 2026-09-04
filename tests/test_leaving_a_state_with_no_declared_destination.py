"""A state whose one exit leads somewhere undeclared is not a dead end.

`idle_lock` and `loading` both declare `"to": null` — the action is known (swipe up; wait) and
the destination is not — so BFS can build no path through them and `_find_path` returns
nothing. Reporting NO_ROUTE there is wrong twice over: it is not true, because there IS a way
out and the registry names it; and it strands the caller on a screen that one gesture clears.

Live 2026-08-26 at Svear Village the bot sat on the idle lock with 445 Iron, 146 Matchlock Gun
and 438 Candle aboard and the barter one tap away, naming the screen correctly and doing
nothing, until the mission aborted.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from brain import nav_step
from brain.nav_step import ARRIVED, BLOCKED, MOVED, NO_ROUTE, REFUSED, step_toward


class LeavingByActingThenLooking(unittest.TestCase):

    def _step(self, state, target="port_overworld", **kw):
        with patch.object(nav_step, "_execute", return_value=True) as ex:
            res = step_toward(target, state=state, **kw)
        return res, ex

    def test_the_idle_lock_is_left_by_its_declared_action(self):
        res, ex = self._step("idle_lock")
        self.assertEqual(res.outcome, MOVED)
        self.assertEqual(res.action, "swipe_up")
        ex.assert_called_once_with("swipe_up", "idle_lock")

    def test_loading_works_the_same_way(self):
        """Same shape, same reason — this is not a special case for the lock."""
        res, _ex = self._step("loading")
        self.assertEqual(res.outcome, MOVED)
        self.assertEqual(res.action, "wait")

    def test_the_destination_is_left_to_the_next_perceive(self):
        res, _ex = self._step("idle_lock")
        self.assertIn("discovered", res.reason)

    def test_a_failed_action_does_not_claim_a_move(self):
        with patch.object(nav_step, "_execute", return_value=False):
            res = step_toward("port_overworld", state="idle_lock")
        self.assertNotEqual(res.outcome, MOVED)

    def test_a_state_with_a_real_route_is_unaffected(self):
        """This must not hijack states that BFS can genuinely route."""
        res, ex = self._step("building")
        self.assertEqual(res.outcome, MOVED)
        self.assertNotEqual(res.action, "swipe_up")

    def test_arriving_still_short_circuits(self):
        res = step_toward("idle_lock", state="idle_lock")
        self.assertEqual(res.outcome, ARRIVED)


class ItStillWillNotGiveUpAPlace(unittest.TestCase):
    """The settlement guard outranks this. A village is a PLACE the task may have sailed for,
    and whether that position can be spent is the task's call, not the state machine's."""

    def test_a_place_is_still_refused_without_permission(self):
        with patch.object(nav_step, "_execute") as ex:
            res = step_toward("world_map", state="port_overworld")
        self.assertEqual(res.outcome, REFUSED)
        ex.assert_not_called()

    def test_a_place_is_never_left_by_the_undeclared_exit_path_either(self):
        """The new branch is gated on the same guard: a PLACE is not left without permission,
        whatever its exits declare."""
        with patch.object(nav_step, "_exits_of") as exits, \
             patch.object(nav_step, "_execute") as ex:
            exits.return_value = [type("T", (), {"action": "swipe_up", "to": None})()]
            step_toward("world_map", state="port_overworld")
        ex.assert_not_called()

    def test_village_is_a_place_the_fsm_does_not_know(self):
        """Recorded, not fixed: 'village' is in _PLACES but has no FSM node, so step_toward
        from a village reports NO_ROUTE and the settlement guard below never runs. Separate
        gap from this change; noted so it is not rediscovered as a symptom."""
        from brain.fsm_registry import get_fsm_registry
        self.assertIn("village", nav_step._PLACES)
        self.assertNotIn("village", get_fsm_registry().states)
        self.assertEqual(step_toward("world_map", state="village").outcome, NO_ROUTE)
