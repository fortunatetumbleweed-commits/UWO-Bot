"""The state machine makes ONE move and reports — it owns no loop and no policy.

From docs/one_loop_task_drives_state.md: the loop belongs to the task, so the task is
consulted between every move. A primitive that iterates internally cannot know what the bot
is trying to achieve, which is how one pressed Back until the fleet left the village a
mission had just sailed to (live 2026-08-22).
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from brain import nav_step as ns


def _edge(action):
    return types.SimpleNamespace(action=action)


def _step(target, state, path, **kw):
    """Run step_toward with a canned path; return (result, actions_executed)."""
    done = []
    with patch.object(ns, "_find_path", return_value=path), \
         patch.object(ns, "_execute", side_effect=lambda a, s: done.append(a) or True):
        res = ns.step_toward(target, state=state, **kw)
    return res, done


class OneMoveOnly(unittest.TestCase):

    def test_it_executes_only_the_first_edge(self):
        res, done = _step("port_overworld", "sub_menu",
                          [_edge("press_back"), _edge("press_back"), _edge("exit_screen")])
        self.assertEqual(res.outcome, ns.MOVED)
        self.assertEqual(done, ["press_back"], "a step is ONE move; the loop owns iteration")

    def test_already_there_is_not_a_move(self):
        res, done = _step("port_overworld", "port_overworld", [])
        self.assertEqual(res.outcome, ns.ARRIVED)
        self.assertEqual(done, [])

    def test_no_route_is_reported_not_improvised(self):
        res, done = _step("market", "sea", None)
        self.assertEqual(res.outcome, ns.NO_ROUTE)
        self.assertEqual(done, [])

    def test_an_unreadable_state_blocks_rather_than_guessing(self):
        with patch.object(ns, "_current_state", return_value=None):
            res = ns.step_toward("market")
        self.assertEqual(res.outcome, ns.BLOCKED)


class PositionIsNotSpentSilently(unittest.TestCase):
    """Leaving a settlement costs the voyage that reached it — the caller must opt in."""

    def test_leaving_a_village_is_refused_by_default(self):
        res, done = _step("world_map", "village", [_edge("press_back")])
        self.assertEqual(res.outcome, ns.REFUSED)
        self.assertEqual(done, [], "no Back is pressed out of a village")

    def test_the_caller_may_authorise_it(self):
        """From a village, leaving IS the only route to the world map — the task's call."""
        res, done = _step("world_map", "village", [_edge("press_back")],
                          may_leave_a_place=True)
        self.assertEqual(res.outcome, ns.MOVED)
        self.assertEqual(done, ["press_back"])

    def test_a_port_overworld_is_also_a_place(self):
        res, _done = _step("world_map", "port_overworld", [_edge("press_back")])
        self.assertEqual(res.outcome, ns.REFUSED)

    def test_a_screen_is_not_a_place(self):
        """Leaving a market or a menu costs nothing and needs no permission."""
        res, done = _step("port_overworld", "sub_menu", [_edge("press_back")])
        self.assertEqual(res.outcome, ns.MOVED)
        self.assertEqual(done, ["press_back"])


class UnknownActionsAreReported(unittest.TestCase):

    def test_an_unmapped_fsm_action_does_not_improvise(self):
        with patch.object(ns, "_find_path", return_value=[_edge("do_a_barrel_roll")]):
            res = ns.step_toward("market", state="sub_menu")
        self.assertEqual(res.outcome, ns.BLOCKED)
        self.assertFalse(res.ok)


if __name__ == "__main__":
    unittest.main()
