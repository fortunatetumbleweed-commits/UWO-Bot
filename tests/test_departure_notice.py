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






