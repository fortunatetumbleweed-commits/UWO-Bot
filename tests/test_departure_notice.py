"""The auto-supply departure confirmation must be COMPLETED, not dismissed.

Live 2026-08-21 (Kochi → Malé): choosing the destination from a port raises
"Moving to Malé after Auto Supply. Continue?" and nothing happens until it is
answered. The perception layer identified it correctly and explicitly declined to
act ("Semantic dismissal 'tap_ok' — learning only, leaving action to caller"), and
the caller had no handler — so the run looped on it for 70s, re-consulting Qwen
every 8s and never tapping anything.
"""
from unittest import mock

import pytest

from actions.sail_actions import _looks_like_departure_notice, confirm_departure_notice


NOTICE = ("Notice Moving to Malé after Auto Supply. Continue? "
          "Fleet will immediately set sail if Auto Supply is not possible. "
          "Sailing can be dangerous with a lack of Food and Water. "
          "Do not show for a day Cancel OK")


class TestRecognition:
    def test_matches_the_live_notice(self):
        assert _looks_like_departure_notice(NOTICE, "Male")

    def test_matches_when_accent_differs_between_request_and_screen(self):
        """The catalogue name is 'Malé' but callers pass 'Male' — folding must bridge it."""
        assert _looks_like_departure_notice(NOTICE, "Malé")

    def test_rejects_a_notice_for_a_different_destination(self):
        assert not _looks_like_departure_notice(NOTICE, "Jakarta")

    def test_rejects_other_dialogs_titled_notice(self):
        """'Notice' titles many popups. Tapping OK on the wrong one closed the game once,
        so the Auto-Supply wording — not the title — is what identifies this dialog."""
        assert not _looks_like_departure_notice(
            "Notice Exit Game? Your progress will be saved. Cancel OK", "Male")

    def test_rejects_empty_text(self):
        assert not _looks_like_departure_notice("", "Male")


class TestConfirm:
    def _run(self, texts, tap_ok=True):
        frames = [object()] * len(texts)
        seq = list(texts)

        def fake_ocr(_frame, min_conf=0.3):
            return [(seq.pop(0), 0.9, 0, 0)] if seq else [("", 0.9, 0, 0)]

        taps = []
        with mock.patch("actions.sail_actions.capture_screen", side_effect=frames), \
             mock.patch("actions.sail_actions._ocr_frame", side_effect=fake_ocr), \
             mock.patch("actions.ui.tap_text",
                        side_effect=lambda f, *l, **kw: taps.append(l) or tap_ok), \
             mock.patch("actions.ui.settle"):
            return confirm_departure_notice("Male"), taps

    def test_taps_ok_then_reports_cleared(self):
        result, taps = self._run([NOTICE, "World Map Port Explore Route Trade"])
        assert result["seen"] and result["confirmed"]
        assert taps == [("ok",)]

    def test_no_notice_is_not_a_failure(self):
        result, taps = self._run(["World Map Port Explore Route Trade"])
        assert result["seen"] is False
        assert result["confirmed"] is False
        assert taps == []

    def test_missing_ok_button_is_reported_not_swallowed(self):
        result, _ = self._run([NOTICE], tap_ok=False)
        assert result["seen"] and not result["confirmed"]
        assert "OK button" in result["reason"]

    def test_notice_that_will_not_clear_gives_up_and_says_so(self):
        result, taps = self._run([NOTICE, NOTICE, NOTICE])
        assert result["seen"] and not result["confirmed"]
        assert "still up" in result["reason"]
        assert len(taps) == 3


class TestAlreadyAtDestination:
    """Selecting the port you are standing in must NOT trigger a departure.

    Live 2026-08-21: the mission's first gather node was `gather:Male` while the fleet
    was already docked at Malé. Selecting Malé closed the world map straight back to the
    same overworld, `_wait_until_at_sea` reported "still in port", and the failure-1 path
    fired a manual Supply Departure — sending the fleet to sea with NO DESTINATION, where
    it sat at speed 0 having bought nothing.
    """

    def _depart(self, where):
        from actions.sail_actions import depart_from_port_via_world_map
        with mock.patch("actions.sail_actions.where_am_i", return_value=where), \
             mock.patch("actions.sail_actions.open_world_map") as owm, \
             mock.patch("actions.sail_actions._navigate_world_map_to_destination",
                        return_value=False), \
             mock.patch("actions.sail_actions._depart_from_harbour") as harbour:
            result = depart_from_port_via_world_map("Male")
        return result, owm, harbour

    def test_already_docked_there_is_success_without_departing(self):
        result, owm, harbour = self._depart(
            {"location": "port_overworld", "port": "Malé"})
        assert result["ok"]
        assert result["departed_via"] == "no departure needed"
        harbour.assert_not_called()      # the dangerous bit: no Supply Departure
        owm.assert_not_called()          # and no need to even open the map

    def test_accent_mismatch_still_counts_as_already_there(self):
        """The mission carries 'Male'; the port HUD reads 'Malé'."""
        result, _, harbour = self._depart(
            {"location": "port_overworld", "port": "Male"})
        assert result["ok"]
        harbour.assert_not_called()

    def test_a_different_port_still_departs(self):
        """The guard must not swallow real departures."""
        result, owm, _ = self._depart(
            {"location": "port_overworld", "port": "Kochi"})
        assert result["departed_via"] != "no departure needed"
        owm.assert_called()


class TestBoundElsewhere:
    """Movement decides; the destination readout only vetoes a NAMED mismatch.

    The game will display a destination as set when the tap that set it did nothing —
    that is the speed-0 bug, and the cure is to re-open the world map and set it again
    (user 2026-08-21). So a matching name is not evidence a departure worked, and a blank
    one is not evidence it failed. The single thing the readout is good for: a hand-fired
    Supply Departure carries NO destination, and `is_moving` can still read true off a
    ticking day-at-sea — if the HUD names somewhere we did not ask for, re-select.
    """

    def _veto(self, hud):
        from actions.sail_actions import _bound_elsewhere
        return _bound_elsewhere(hud, "Male")

    def test_same_port_is_no_veto(self):
        assert not self._veto({"destination": "Male"})

    def test_accent_difference_is_no_veto(self):
        assert not self._veto({"destination": "Malé"})

    def test_a_different_named_port_vetoes(self):
        assert self._veto({"destination": "Jakarta"})

    def test_blank_destination_does_not_veto_a_moving_fleet(self):
        """A blank readout must never override demonstrated movement."""
        assert not self._veto({"destination": ""})

    def test_unreadable_destination_does_not_veto(self):
        assert not self._veto({"destination": None})

    def test_missing_hud_does_not_veto(self):
        assert not self._veto({})


class TestMotionIsDecisive:
    """A departure is confirmed by MOVEMENT, never by the destination label."""

    def _depart(self, moving, hud):
        from actions.sail_actions import depart_from_port_via_world_map
        with mock.patch("actions.sail_actions.where_am_i",
                        return_value={"location": "sea"}), \
             mock.patch("actions.sail_actions._confirm_making_way",
                        return_value=(moving, hud)) as cmw:
            return depart_from_port_via_world_map("Male", max_retries=0), cmw

    def test_moving_with_the_right_destination_succeeds(self):
        result, _ = self._depart(True, {"destination": "Malé"})
        assert result["ok"]

    def test_moving_with_a_blank_destination_still_succeeds(self):
        """The old logic re-selected here, disrupting a voyage that was working."""
        result, _ = self._depart(True, {"destination": None})
        assert result["ok"]

    def test_not_moving_fails_even_when_the_destination_looks_set(self):
        """The speed-0 bug in one line: the label says Malé, nothing is happening."""
        result, _ = self._depart(False, {"destination": "Malé"})
        assert not result["ok"]

    def test_moving_toward_a_different_port_is_not_success(self):
        result, _ = self._depart(True, {"destination": "Jakarta"})
        assert not result["ok"]


class TestAlreadyThereFromInsideABuilding:
    """Being INSIDE a building at the destination still counts as being there.

    Live 2026-08-21: `gather:Jakarta` ran while the fleet sat on Jakarta's Market Purchase
    screen — the exact place the task needed to be to buy Ebony. The port name is not
    rendered inside a building, so "am I already there?" answered no, and the mission tried
    to exit, open the world map and sail to the port it was standing in. It could not get
    out of the building, escalated to the teaching loop, and aborted after 600s.

    The bot is not actually ignorant here: `last_known_settlement` is carried across ticks
    and persisted to disk precisely so a restart knows where it is.
    """

    def _depart(self, location, settlement, persisted=None):
        from actions.sail_actions import depart_from_port_via_world_map
        obs = mock.MagicMock()
        obs.last_known_settlement = settlement
        with mock.patch("actions.sail_actions.where_am_i",
                        return_value={"location": location, "port": None}), \
             mock.patch("brain.observation.current", return_value=obs), \
             mock.patch("brain.observation._ensure_persisted_loaded",
                        return_value=persisted), \
             mock.patch("actions.sail_actions.open_world_map") as owm, \
             mock.patch("actions.sail_actions._navigate_world_map_to_destination",
                        return_value=False), \
             mock.patch("actions.sail_actions._depart_from_harbour") as harbour:
            return depart_from_port_via_world_map("Jakarta"), owm, harbour

    def test_inside_a_building_at_the_destination_needs_no_sailing(self):
        result, owm, harbour = self._depart("building", "Jakarta")
        assert result["ok"]
        assert result["departed_via"] == "no departure needed"
        owm.assert_not_called()
        harbour.assert_not_called()

    def test_inside_a_sub_menu_at_the_destination_needs_no_sailing(self):
        """The exact live state: the Market's Purchase sub_menu."""
        result, _, _ = self._depart("sub_menu", "Jakarta")
        assert result["ok"]

    def test_inside_a_building_somewhere_else_still_sails(self):
        result, owm, _ = self._depart("building", "Male")
        assert result["departed_via"] != "no departure needed"
        owm.assert_called()

    def test_unknown_settlement_does_not_claim_to_be_there(self):
        result, owm, _ = self._depart("building", None, persisted=None)
        assert result["departed_via"] != "no departure needed"
        owm.assert_called()

    def test_falls_back_to_the_settlement_persisted_on_disk(self):
        """The restart case: a fresh process has no in-memory observation yet, which is
        exactly how the live failure arose — the mission was launched while the bot was
        already standing in the Market."""
        result, owm, _ = self._depart("sub_menu", None, persisted="Jakarta")
        assert result["ok"]
        assert result["departed_via"] == "no departure needed"
        owm.assert_not_called()
