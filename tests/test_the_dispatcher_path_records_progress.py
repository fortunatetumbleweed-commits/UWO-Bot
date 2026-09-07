"""A finished mission must not still look in-flight to the next launch.

`mission_progress` survives the process, and a mission recorded as in-flight is what the next
launch RESUMES — skipping planning entirely. The dispatcher path advanced the phase to
"bartering" when the sail to the village began and then never touched it again: no
`sailing_route`, no `finish`.

Live 2026-09-06. The fleet was standing in Lisboa's market with 3,668 Birch Tree aboard, its
barter six rounds finished an hour earlier and the whole mission reported done, and the
relaunch said:

    [barter_command] already bartering for Svear Village (3705s ago) — skipping the check
                     and the plan, arriving and bartering with what is aboard
    [barter_command] not at a village (state is 'sub_menu:sell') — sailing to Svear Village

and set sail across the map to barter again. The record only goes stale after six hours, so
this cannot be left to expire — that window is exactly when a relaunch happens.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from brain.mission_runner import DONE, RUNNING, MissionRunner


def _leg(kind, ident, done=False):
    return types.SimpleNamespace(kind=kind, id=ident, done=done, deps=())


class _Step:
    status = DONE
    reason = ""


class TheMissionRecordsHowFarItGot(unittest.TestCase):

    def _runner(self, legs, leg, kind_done):
        m = MissionRunner.__new__(MissionRunner)
        m.status, m.reason, m.completed = RUNNING, "", []
        m.subtasks, m._leg, m._runner, m._steps = legs, leg, _Step(), []
        return m

    def test_the_barter_finishing_moves_the_phase_past_it(self):
        leg = _leg("barter", "barter:Svear")
        m = self._runner([leg], leg, "barter")
        with mock.patch("brain.mission_progress.advance") as adv:
            m._finish_leg()
        adv.assert_called_once_with("sailing_route")

    def test_another_leg_finishing_does_NOT_move_it(self):
        """Only the barter is the phase boundary."""
        leg = _leg("gather", "gather:Tripoli")
        m = self._runner([leg], leg, "gather")
        with mock.patch("brain.mission_progress.advance") as adv:
            m._finish_leg()
        adv.assert_not_called()

    def test_every_leg_done_clears_the_record(self):
        m = self._runner([_leg("sell", "sell", done=True)], None, None)
        m._departed_for_the_village = lambda _s: None
        with mock.patch("brain.mission_progress.finish") as fin:
            self.assertFalse(m._start_next_leg(types.SimpleNamespace(state="port_overworld")))
        fin.assert_called_once()
        self.assertEqual(m.status, DONE)

    def test_bookkeeping_that_fails_does_not_fail_the_mission(self):
        leg = _leg("barter", "barter:Svear")
        m = self._runner([leg], leg, "barter")
        with mock.patch("brain.mission_progress.advance", side_effect=RuntimeError("disk")):
            m._finish_leg()                       # must not raise
        self.assertTrue(leg.done)


if __name__ == "__main__":
    unittest.main()
