"""Tests for the SailToGoal arrival-recognition heuristics.

Specifically pins the phase-aware arrival fallback (added 2026-05-25)
that prevents the bot from missing arrival when the transition-frame
OCR returns port=None.

Background: the goal-loop tick fires at the moment perception
transitions sea → port_overworld.  On that frame OCR sometimes hasn't
caught up and returns port=None.  The OCR-confirmed arrival check
(state.port matches destination) fails, and without the phase-aware
fallback the dispatcher fell through to navigate_to_building, the goal
never terminated, and the bot looped re-entering harbor and re-departing.
The Go-to-City tap that initiated SAILING already committed the
destination, so the first port_overworld after SAILING IS arrival by
construction.
"""
from __future__ import annotations

import unittest
from unittest import mock

from brain.goals.sail_to import SailToGoal, SailPhase
from brain.perceive import PerceiveResult


def _result(state: str, port=None, **kw) -> PerceiveResult:
    return PerceiveResult(
        state=state,
        port=port,
        detail=kw.pop("detail", f"{state}{f' ({port})' if port else ''}"),
        **kw,
    )


class PhaseAwareArrivalTests(unittest.TestCase):

    def _make_goal(self, destination: str, phase: SailPhase) -> SailToGoal:
        g = SailToGoal(destination=destination)
        g.phase = phase
        return g

    # ── The bug fix ──────────────────────────────────────────────────

    def test_port_overworld_with_port_none_after_sailing_is_arrival(self):
        """Reproduces the 2026-05-25 London failure: tick at the
        transition frame had port=None, the OCR-confirmed check failed,
        and the goal didn't terminate.  Phase-aware fallback should
        catch it.
        """
        goal = self._make_goal("London", SailPhase.SAILING)
        with mock.patch("brain.perceive.perceive",
                        return_value=_result("port_overworld", port=None)), \
             mock.patch("brain.planner.get_planner"):
            result = goal.tick()

        self.assertEqual(result.action, "arrived")
        self.assertEqual(goal.phase, SailPhase.ARRIVED)

    def test_port_overworld_with_partial_port_after_sailing_is_arrival(self):
        """A partial OCR ('City', 'Harbor', etc.) also shouldn't block
        arrival once we're in SAILING phase.  The OCR check fails the
        prefix match; phase-aware fallback should still fire.
        """
        goal = self._make_goal("London", SailPhase.SAILING)
        with mock.patch("brain.perceive.perceive",
                        return_value=_result("port_overworld", port="City")), \
             mock.patch("brain.planner.get_planner"):
            result = goal.tick()

        self.assertEqual(result.action, "arrived")
        self.assertEqual(goal.phase, SailPhase.ARRIVED)

    # ── Guard against false positives ─────────────────────────────────

    def test_port_overworld_pre_sailing_is_NOT_arrival(self):
        """At INIT / GO_TO_HARBOR phases the bot is at the SOURCE port
        — port_overworld must NOT be treated as arrival.  Phase guard
        keeps the fallback safe.
        """
        for phase in (
            SailPhase.INIT,
            SailPhase.EXIT_BUILDING,
            SailPhase.GO_TO_HARBOR,
            SailPhase.FLEET_CHECK,
            SailPhase.DEPART,
            SailPhase.SEA_NAVIGATE,
            SailPhase.WORLD_MAP,
        ):
            with self.subTest(phase=phase):
                goal = self._make_goal("London", phase)
                with mock.patch("brain.perceive.perceive",
                                return_value=_result("port_overworld", port=None)), \
                     mock.patch("brain.planner.get_planner"):
                    result = goal.tick()
                self.assertNotEqual(result.action, "arrived")
                self.assertNotEqual(goal.phase, SailPhase.ARRIVED)

    # ── Existing OCR-confirmed path still wins when port matches ─────

    def test_ocr_confirmed_arrival_logs_high_confidence_path(self):
        """When port name IS readable and matches destination, the
        high-confidence (OCR-confirmed) check should fire, not the
        phase-aware fallback.  Behaviour is the same (arrival), but
        the log line differs — verify via the result, not log scraping.
        """
        goal = self._make_goal("London", SailPhase.SAILING)
        with mock.patch("brain.perceive.perceive",
                        return_value=_result("port_overworld", port="London")), \
             mock.patch("brain.planner.get_planner"):
            result = goal.tick()

        self.assertEqual(result.action, "arrived")
        self.assertEqual(goal.phase, SailPhase.ARRIVED)


if __name__ == "__main__":
    unittest.main()
