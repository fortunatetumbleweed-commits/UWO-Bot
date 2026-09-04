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

import pytest
import unittest
from unittest import mock

from brain.goals.sail_to import SailToGoal, SailPhase, arrival_verdict
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

    @pytest.mark.simulation  # simulated goal-loop run: `tick()` executes the real action code against whatever frame the fixtures supply
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

    @pytest.mark.simulation  # simulated goal-loop run: `tick()` executes the real action code against whatever frame the fixtures supply
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
        """Before SAILING the fleet is still at the SOURCE port, so port_overworld must not
        read as arrival — otherwise the phase-aware fallback fires at the departure quay and
        every voyage "arrives" before it starts.

        Asserted against the DECISION, not by ticking the goal. Driving `tick()` meant that
        for any phase below SAILING it fell past this decision into the real departure action
        — `open_world_map()` OCRing a blank frame through ten retries — so the test took an
        hour, could only ever go red by RAISING from that action path, and asserted something
        the enum ordering already guaranteed. It was red for months on the adb guard while
        telling nobody anything about arrival.
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
                self.assertIsNone(
                    arrival_verdict(_result("port_overworld", port=None), phase,
                                    matches_destination=False,
                                    destination_is_likely_village=False),
                    f"{phase.name}: port_overworld read as arrival before sailing")

    def test_a_named_destination_is_arrival_at_any_phase(self):
        """Reading the destination's NAME is the high-confidence path and needs no phase."""
        for phase in (SailPhase.INIT, SailPhase.GO_TO_HARBOR, SailPhase.SAILING):
            with self.subTest(phase=phase):
                self.assertIsNotNone(
                    arrival_verdict(_result("port_overworld", port="London"), phase,
                                    matches_destination=True,
                                    destination_is_likely_village=False))

    def test_an_unnamed_village_is_arrival_only_after_sailing(self):
        """A village screen titles itself "Village", so the name never confirms it. Sailing
        toward a village and landing in one is the destination; the same frame before
        departure is not. Origin: 2026-05-23 Berber."""
        before = arrival_verdict(_result("village", port=None), SailPhase.GO_TO_HARBOR,
                                 matches_destination=False,
                                 destination_is_likely_village=True)
        after = arrival_verdict(_result("village", port=None), SailPhase.SAILING,
                                matches_destination=False,
                                destination_is_likely_village=True)
        self.assertIsNone(before)
        self.assertIsNotNone(after)

    def test_qwen_task_complete_needs_sailing_and_a_place_to_arrive(self):
        """The secondary signal is gated hard — high confidence, actually sailing, and a
        scene one can arrive in. A 'sea' frame claiming completion is ignored."""
        live = dict(matches_destination=False, destination_is_likely_village=False)
        self.assertIsNotNone(arrival_verdict(
            _result("sea", task_complete=True, confidence="high",
                    scene_type="port_overworld"), SailPhase.SAILING, **live))
        self.assertIsNone(arrival_verdict(
            _result("sea", task_complete=True, confidence="high", scene_type="sea"),
            SailPhase.SAILING, **live), "a sea frame is not somewhere one arrives")
        self.assertIsNone(arrival_verdict(
            _result("sea", task_complete=True, confidence="low",
                    scene_type="port_overworld"), SailPhase.SAILING, **live),
            "low confidence must not end a voyage")

    # ── Existing OCR-confirmed path still wins when port matches ─────

    @pytest.mark.simulation  # simulated goal-loop run: `tick()` executes the real action code against whatever frame the fixtures supply
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


class InsideABuildingTheBotStillKnowsWhereItIs(unittest.TestCase):
    """Inside a building the port name is not on screen, so `state.port` is None.

    A destination check that only reads the screen concludes "not there" and sails to the port
    it is standing in. Live 2026-08-21: `gather:Jakarta` ran while the fleet sat in Jakarta's
    Market — the one place it needed to be — and instead of buying it tried to exit, open the
    world map and sail to Jakarta, failed to get out of the building, and aborted after 600s.

    The settlement is carried across ticks and persisted, so a building falls back to that.
    """

    def _goal(self, destination="Jakarta"):
        return SailToGoal(destination=destination)

    def _resolve(self, goal, state, settlement):
        # Patch the MODULE, not sys.modules: `from brain import observation` reads the
        # attribute off the already-imported `brain` package, so a sys.modules patch is
        # bypassed whenever another test imported it first — which made these pass alone and
        # fail in a full run.
        from brain import observation as obs
        with mock.patch.object(obs, "current",
                               return_value=mock.Mock(last_known_settlement=settlement)), \
             mock.patch.object(obs, "_ensure_persisted_loaded",
                               return_value=settlement, create=True):
            return goal._settlement_now(state)

    def test_the_screen_wins_when_it_says_anything(self):
        g = self._goal()
        got = self._resolve(g, _result("port_overworld", port="Jakarta"), "Elsewhere")
        self.assertEqual(got, "Jakarta", "a visible port name outranks the remembered one")

    def test_a_building_falls_back_to_the_remembered_settlement(self):
        g = self._goal()
        got = self._resolve(g, _result("building", port=None), "Jakarta")
        self.assertEqual(got, "Jakarta")

    def test_a_sub_menu_falls_back_too(self):
        g = self._goal()
        self.assertEqual(self._resolve(g, _result("sub_menu", port=None), "Jakarta"), "Jakarta")

    def test_at_sea_does_not_fall_back(self):
        """At sea the fleet is NOT at a settlement; a remembered one would be a lie."""
        g = self._goal()
        self.assertIsNone(self._resolve(g, _result("sea", port=None), "Jakarta"))

    def test_standing_in_the_destination_is_arrival(self):
        """The whole point: the goal must not sail to where it already is."""
        g = self._goal()
        with mock.patch.object(g, "_settlement_now", return_value="Jakarta"):
            self.assertTrue(g._matches_destination(g._settlement_now(None)))
