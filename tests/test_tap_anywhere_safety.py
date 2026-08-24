"""A "tap anywhere" dismissal must not perform a transaction.

Live 2026-08-21, Jakarta (session trace_barter_cmd_2026-08-21T21-47-28, frames 1-6):
the bot was left on the market's Purchase grid. Perception classified it as an unknown
obstruction, the consult returned dismissal='tap_anywhere', and the dismissal tapped dead
centre — which on that grid is the Lac Powder tile. It added 385 units (130,900 ducats) to
the cart. Back then raised "Moving to another menu will empty the cart. Continue?", and the
next centre tap landed on that dialog's BODY TEXT, hitting neither OK nor Cancel. The cycle
repeated three times.

"Tap anywhere to continue" is only true on a screen where anywhere really is nothing.
"""
from unittest import mock

import pytest

from brain.perceive import _element_under_point


class _El:
    def __init__(self, x1, y1, x2, y2, label, element_type="button"):
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.label, self.element_type = label, element_type


class _Frame:
    width, height = 2400, 1080


class TestElementUnderPoint:
    def _under(self, elements, x=1200, y=540):
        with mock.patch("vision.omniparser.parse_fast_cached", return_value=elements):
            return _element_under_point(_Frame(), x, y)

    def test_finds_a_control_under_the_centre(self):
        """The Lac Powder tile spans the centre of the Purchase grid."""
        assert self._under([_El(792, 430, 1224, 660, "Lac Powder")]) == "Lac Powder"

    def test_empty_centre_returns_none(self):
        """A splash screen — nothing under the centre, so tapping it is safe."""
        assert self._under([_El(0, 0, 200, 100, "back")]) is None

    def test_non_interactive_elements_do_not_count(self):
        assert self._under([_El(792, 430, 1224, 660, "Notice", element_type="text")]) is None

    def test_unlabelled_control_still_reports_its_type(self):
        assert self._under([_El(1100, 500, 1300, 600, "")]) == "button"

    def test_detection_failure_is_permissive(self):
        """This only ever REFUSES an action, so failing closed would block working
        dismissals. A detector error must not do that."""
        with mock.patch("vision.omniparser.parse_fast_cached",
                        side_effect=RuntimeError("no yolo")):
            assert _element_under_point(_Frame(), 1200, 540) is None


class TestDismissalRefusesToTransact:
    def _dismiss(self, elements):
        """Drive the _consult:tap_anywhere branch and report whether it tapped."""
        import brain.perceive as p
        taps = []
        with mock.patch("actions.adb_actions.tap", side_effect=lambda x, y: taps.append((x, y))), \
             mock.patch("vision.omniparser.parse_fast_cached", return_value=elements), \
             mock.patch("time.sleep"):
            p._dismiss_interruptor("_consult:tap_anywhere", _Frame())
        return taps

    def test_refuses_when_the_centre_is_a_control(self):
        taps = self._dismiss([_El(792, 430, 1224, 660, "Lac Powder")])
        assert taps == [], "tapping here buys goods; it is not a dismissal"

    def test_taps_when_the_centre_is_empty(self):
        taps = self._dismiss([_El(0, 0, 200, 100, "back")])
        assert taps == [(1200, 540)]
