"""A refused trim is stepped over — and the mission must then CARRY ON.

`_finish_leg` already decided this: trimming is support, not a leg of the mission, so a
refused `sell_surplus` is reported and the mission stays RUNNING. `next_goal` then threw that
decision away by returning None regardless, so the runner said "carry on without it" and
immediately reported it had nothing to ask for.

Live 2026-09-06 at Tripoli, with every material aboard and the barter one sail away:

    sell_surplus refused (trim to Candle 709, Iron 822, Matchlock Gun 411)
      — trimming is support, so the mission carries on without it
    MissionRunner has nothing more to ask for after 37 step(s) — status running

`status running` with no work order is the signature: a FAILED mission is meant to stop, a
RUNNING one is meant to be asked again.
"""

from __future__ import annotations

import types
import unittest

from brain.mission_runner import DONE, FAILED, RUNNING, MissionRunner


class _Step:
    """A leg's runner: hands out `goals`, then reports `status`."""

    def __init__(self, goals, status, reason=""):
        self._goals, self.status, self.reason = list(goals), status, reason

    def next_goal(self, _result, _state):
        return self._goals.pop(0) if self._goals else None


def _leg(kind, ident):
    return types.SimpleNamespace(kind=kind, id=ident, done=False, deps=())


class ARefusedTrimDoesNotEndTheMission(unittest.TestCase):

    def _runner(self, refused_kind):
        m = MissionRunner.__new__(MissionRunner)
        m.status, m.reason, m.completed = RUNNING, "", []
        m.subtasks = [_leg(refused_kind, f"{refused_kind}:x"), _leg("barter", "barter:Svear")]
        m._leg, m._runner, m._steps = m.subtasks[0], _Step([], FAILED, "the tab did not open"), []
        m._started = ["after the refusal"]

        def _start(_state):
            if not m._started:
                return False
            m._leg = m.subtasks[1]
            m._runner = _Step([m._started.pop(0)], DONE)
            return True

        m._start_next_leg = _start
        return m

    def test_the_mission_goes_on_to_the_next_leg(self):
        m = self._runner("sell_surplus")
        goal = m.next_goal(None, types.SimpleNamespace(state="port_overworld"))
        self.assertEqual(goal, "after the refusal", "stopped with the barter one sail away")
        self.assertEqual(m.status, RUNNING)
        self.assertIn("sell_surplus:x", m.completed, "stepped over, not silently dropped")

    def test_a_REAL_leg_refusing_still_ends_it(self):
        """Gather, barter and sell ARE the mission — only support may be stepped over."""
        m = self._runner("gather")
        self.assertIsNone(m.next_goal(None, types.SimpleNamespace(state="port_overworld")))
        self.assertEqual(m.status, FAILED)


if __name__ == "__main__":
    unittest.main()
