"""Trimming is SUPPORT. A refused trim must not end the mission.

CLAUDE.md already says so — "the task is gather, barter, sell; trim, supply and capacity are
SUPPORT — opportunistic when the place affords them, never mandatory legs" — and the runner
failed the mission for it anyway.

Live 2026-09-05 at Madeira: the Sell tab did not open (the game drops roughly one tap in
twenty), so the trim REFUSED rather than reporting a hold it had never seen — which is the
correct behaviour and exactly what `None is not []` exists to protect. The runner then had
nothing left to ask for and failed. The fleet sat one port from Hutu with both materials
aboard, having bartered nothing, over an over-stock of 108 Pig.
"""

from __future__ import annotations

import types
import unittest

from brain.mission import SubTask
from brain.mission_runner import FAILED, MissionRunner

COORDS = {"Near": (0.0, 0.0)}


def _runner_that(status, reason=""):
    return types.SimpleNamespace(status=status, reason=reason)


def _mission(*legs):
    return MissionRunner(list(legs), COORDS, good="Bambara Groundnut", village="Hutu Village")


def _refuse(mission, leg, reason):
    """Drive _finish_leg the way the loop does, with a runner that refused."""
    mission._leg, mission._runner = leg, _runner_that("blocked", reason)
    mission._finish_leg()


class ARefusedTrimIsSteppedOver(unittest.TestCase):

    REASON = "could not reach the Sell grid — refusing to report the hold as trimmed"

    def test_the_mission_carries_on(self):
        leg = SubTask("sell_surplus", "sell_surplus", "Madeira")
        m = _mission(leg)
        _refuse(m, leg, self.REASON)
        self.assertNotEqual(m.status, FAILED,
                            "a hold that stays full is the barter's problem, not the mission's")

    def test_the_leg_does_not_come_back_round(self):
        """Marked done, or the runner offers the same refusing leg for ever."""
        leg = SubTask("trim_before_gather", "sell_surplus", "Lisboa")
        m = _mission(leg)
        _refuse(m, leg, self.REASON)
        self.assertTrue(leg.done)
        self.assertIn("trim_before_gather", m.completed)

    def test_both_trim_legs_are_covered(self):
        """The graph carries two, under one kind: trim_before_gather and sell_surplus."""
        for leg_id in ("trim_before_gather", "sell_surplus"):
            leg = SubTask(leg_id, "sell_surplus", "Madeira")
            m = _mission(leg)
            _refuse(m, leg, self.REASON)
            self.assertNotEqual(m.status, FAILED, leg_id)


class EveryOtherLegStillFailsTheMission(unittest.TestCase):
    """Gather, barter and sell ARE the mission. Stepping over those would report a success
    that never happened — the thing this codebase keeps having to relearn."""

    def test_a_refused_gather_fails(self):
        leg = SubTask("gather:Near", "gather", "Near")
        m = _mission(leg)
        _refuse(m, leg, "the market would not open")
        self.assertEqual(m.status, FAILED)
        self.assertIn("gather:Near", m.reason)

    def test_a_refused_barter_fails(self):
        leg = SubTask("barter", "barter", "Hutu Village")
        m = _mission(leg)
        _refuse(m, leg, "the village refused")
        self.assertEqual(m.status, FAILED)

    def test_a_refused_sell_fails(self):
        leg = SubTask("sell", "sell", "London")
        m = _mission(leg)
        _refuse(m, leg, "no market here")
        self.assertEqual(m.status, FAILED)

    def test_a_refused_supply_check_still_fails(self):
        """Supply is not trimming: a village cannot resupply, so the reserve must be real."""
        leg = SubTask("supply_verify", "supply_verify", "Madeira")
        m = _mission(leg)
        _refuse(m, leg, "could not read the hold")
        self.assertEqual(m.status, FAILED)


class ASucceedingTrimIsUnchanged(unittest.TestCase):

    def test_done_is_still_done(self):
        from brain.mission_runner import DONE

        leg = SubTask("sell_surplus", "sell_surplus", "Madeira")
        m = _mission(leg)
        m._leg, m._runner = leg, _runner_that(DONE)
        m._finish_leg()
        self.assertTrue(leg.done)
        self.assertNotEqual(m.status, FAILED)
        self.assertIn("sell_surplus", m.completed)


if __name__ == "__main__":
    unittest.main()
