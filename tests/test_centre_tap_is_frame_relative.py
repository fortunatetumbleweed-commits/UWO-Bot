"""Screen positions come from the FRAME, never from a remembered constant.

The phone can sit at either landscape rotation, and the game re-bakes its safe-area offset
per screen, so "the middle of the display" is not a fixed pixel. Hardcoding it is the same
mistake that has bitten repeatedly: the chrome search regions (Home icon 121px outside its
window), the Buildings tab (a computed point that landed on Players), and the cargo strip
(the tracked tile below an x cutoff).

User, 2026-08-22: "the bot should be more flexible at dealing with screen orientations, it
should not use fixed absolute positions, always use relative positions in bboxes or by
perceive."
"""
from unittest import mock

from actions import ui


class _Frame:
    def __init__(self, w, h):
        self.width, self.height = w, h


class TestScreenCentre:
    def test_centre_of_the_canonical_landscape_frame(self):
        assert ui.screen_centre(_Frame(2400, 1080)) == (1200, 540)

    def test_centre_follows_a_different_frame_size(self):
        """A portrait or differently-sized capture must not yield the landscape centre."""
        assert ui.screen_centre(_Frame(1080, 2400)) == (540, 1200)

    def test_centre_of_an_odd_sized_frame(self):
        assert ui.screen_centre(_Frame(1999, 999)) == (999, 499)

    def test_captures_a_frame_when_none_is_given(self):
        with mock.patch("capture.adb_capture.capture_screen",
                        return_value=_Frame(1080, 2400)) as cap:
            assert ui.screen_centre() == (540, 1200)
            cap.assert_called_once()


class TestTapCentre:
    def test_taps_the_frames_centre_not_a_constant(self):
        taps = []
        with mock.patch("actions.adb_actions.tap", side_effect=lambda x, y: taps.append((x, y))):
            ui.tap_centre(_Frame(1080, 2400), why="test")
        assert taps == [(540, 1200)], "the tap must follow the frame, not a hardcoded point"

    def test_returns_where_it_tapped(self):
        with mock.patch("actions.adb_actions.tap"):
            assert ui.tap_centre(_Frame(2400, 1080)) == (1200, 540)
