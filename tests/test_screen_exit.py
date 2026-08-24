"""Tests for actions/screen_exit.py:exit_current_screen().

The function picks one of five outcomes depending on what perception
sees on the frame.  Each test mocks the relevant detectors so the
priority order is verified in isolation.
"""
from __future__ import annotations

import unittest
from unittest import mock

from PIL import Image

from actions.screen_exit import (
    exit_current_screen,
    ExitResult,
    _HOME_SLOT_XY,
    _BACK_ARROW_SLOT_XY,
)


def _blank_frame() -> Image.Image:
    return Image.new("RGB", (2400, 1080), (0, 0, 0))


class ExitCurrentScreenTests(unittest.TestCase):

    def setUp(self):
        # Always stub out capture_screen so no ADB call ever fires.
        self._cap_patch = mock.patch(
            "capture.adb_capture.capture_screen",
            return_value=_blank_frame(),
        )
        self._cap_patch.start()
        self.addCleanup(self._cap_patch.stop)

        self._tap_patch = mock.patch("actions.adb_actions.tap")
        self.tap = self._tap_patch.start()
        self.addCleanup(self._tap_patch.stop)

        self._back_patch = mock.patch("actions.adb_actions.press_back")
        self.press_back = self._back_patch.start()
        self.addCleanup(self._back_patch.stop)

        # Default: parse_fast_cached returns no elements.
        self._omni_patch = mock.patch(
            "vision.omniparser.parse_fast_cached",
            return_value=[],
        )
        self._omni_patch.start()
        self.addCleanup(self._omni_patch.stop)

    # ── Priority 1: dialog close-X ────────────────────────────────

    def test_dialog_close_x_takes_priority(self):
        dlg = mock.MagicMock()
        dlg.close_button = (1800, 200, 1850, 250)   # cx=1825, cy=225
        dlg.kind.return_value = "informational"

        with mock.patch(
            "vision.region_detectors.dialog.detect_dialog",
            return_value=dlg,
        ):
            with mock.patch(
                "vision.chrome_detector.get_chrome_detector"
            ) as gcd:
                # Even if chrome reports has_home, dialog wins.
                gcd.return_value.detect.return_value = mock.MagicMock(
                    has_home=True, has_back_arrow=True, has_hamburger=False, positions={},
                )
                result = exit_current_screen(frame=_blank_frame())

        self.assertEqual(result.method, "dialog_close")
        self.assertTrue(result.ok)
        self.tap.assert_called_once_with(1825, 225)
        self.press_back.assert_not_called()

    # ── Priority 2: Home / main-menu X ────────────────────────────

    def test_home_button_when_no_dialog(self):
        with mock.patch(
            "vision.region_detectors.dialog.detect_dialog",
            return_value=None,
        ):
            with mock.patch(
                "vision.chrome_detector.get_chrome_detector"
            ) as gcd:
                gcd.return_value.detect.return_value = mock.MagicMock(
                    has_home=True, has_back_arrow=True, has_hamburger=False, positions={},
                )
                result = exit_current_screen(frame=_blank_frame())

        self.assertEqual(result.method, "home_or_menu_x")
        self.tap.assert_called_once_with(*_HOME_SLOT_XY)
        self.press_back.assert_not_called()

    # ── Priority 3: in-game back arrow ────────────────────────────

    def test_back_arrow_when_no_home(self):
        with mock.patch(
            "vision.region_detectors.dialog.detect_dialog",
            return_value=None,
        ):
            with mock.patch(
                "vision.chrome_detector.get_chrome_detector"
            ) as gcd:
                gcd.return_value.detect.return_value = mock.MagicMock(
                    has_home=False, has_back_arrow=True, has_hamburger=False, positions={},
                )
                result = exit_current_screen(frame=_blank_frame())

        self.assertEqual(result.method, "back_arrow")
        self.tap.assert_called_once_with(*_BACK_ARROW_SLOT_XY)
        self.press_back.assert_not_called()

    # ── Priority 4: system press_back fallback ────────────────────

    def test_press_back_fallback_when_nothing_else_detected(self):
        with mock.patch(
            "vision.region_detectors.dialog.detect_dialog",
            return_value=None,
        ):
            with mock.patch(
                "vision.chrome_detector.get_chrome_detector"
            ) as gcd:
                gcd.return_value.detect.return_value = mock.MagicMock(
                    has_home=False, has_back_arrow=False, has_hamburger=False, positions={},
                )
                with mock.patch("brain.perceive.perceive") as perc:
                    perc.return_value = mock.MagicMock(state="sea")
                    result = exit_current_screen(frame=_blank_frame())

        self.assertEqual(result.method, "press_back")
        self.assertTrue(result.ok)
        self.press_back.assert_called_once()

    # ── Refusals ─────────────────────────────────────────────────

    def test_refuses_press_back_on_overworld(self):
        with mock.patch(
            "vision.region_detectors.dialog.detect_dialog",
            return_value=None,
        ):
            with mock.patch(
                "vision.chrome_detector.get_chrome_detector"
            ) as gcd:
                gcd.return_value.detect.return_value = mock.MagicMock(
                    has_home=False, has_back_arrow=False, has_hamburger=False, positions={},
                )
                with mock.patch("brain.perceive.perceive") as perc:
                    perc.return_value = mock.MagicMock(state="port_overworld")
                    result = exit_current_screen(frame=_blank_frame())

        self.assertEqual(result.method, "refused")
        self.assertFalse(result.ok)
        self.press_back.assert_not_called()

    def test_allow_back_fallback_false_refuses(self):
        with mock.patch(
            "vision.region_detectors.dialog.detect_dialog",
            return_value=None,
        ):
            with mock.patch(
                "vision.chrome_detector.get_chrome_detector"
            ) as gcd:
                gcd.return_value.detect.return_value = mock.MagicMock(
                    has_home=False, has_back_arrow=False, has_hamburger=False, positions={},
                )
                result = exit_current_screen(
                    frame=_blank_frame(),
                    allow_back_fallback=False,
                )

        self.assertEqual(result.method, "refused")
        self.assertFalse(result.ok)
        self.press_back.assert_not_called()

    # ── Dry-run mode ─────────────────────────────────────────────

    def test_dry_run_does_not_tap_but_reports_method(self):
        """Dry-run mode skips the side-effect but still reports which
        method would have fired."""
        with mock.patch(
            "vision.region_detectors.dialog.detect_dialog",
            return_value=None,
        ):
            with mock.patch(
                "vision.chrome_detector.get_chrome_detector"
            ) as gcd:
                gcd.return_value.detect.return_value = mock.MagicMock(
                    has_home=True, has_back_arrow=False, has_hamburger=False, positions={},
                )
                result = exit_current_screen(
                    frame=_blank_frame(),
                    dry_run=True,
                )

        self.assertEqual(result.method, "home_or_menu_x")
        self.assertTrue(result.ok)
        self.tap.assert_not_called()         # the side-effect is gated
        self.press_back.assert_not_called()

    def test_overworld_with_home_still_taps_home(self):
        """The overworld refusal only gates press_back fallback, not
        on-screen taps.  Tapping Home on overworld is harmless (already
        home)."""
        with mock.patch(
            "vision.region_detectors.dialog.detect_dialog",
            return_value=None,
        ):
            with mock.patch(
                "vision.chrome_detector.get_chrome_detector"
            ) as gcd:
                gcd.return_value.detect.return_value = mock.MagicMock(
                    has_home=True, has_back_arrow=False, has_hamburger=False, positions={},
                )
                result = exit_current_screen(frame=_blank_frame())

        self.assertEqual(result.method, "home_or_menu_x")
        self.tap.assert_called_once_with(*_HOME_SLOT_XY)


if __name__ == "__main__":
    unittest.main()


class HamburgerGuardTests(unittest.TestCase):
    """The Home slot is the ☰ hamburger on an overworld — tapping it opens
    Company Overview instead of exiting (it stranded the gather run 2026-08-17).

    These cases were untested, which is how every other test in this file came to
    rely on `MagicMock` auto-vivifying `has_hamburger` as truthy: the guard was
    firing everywhere and nothing said so.
    """

    def setUp(self):
        self._cap_patch = mock.patch("capture.adb_capture.capture_screen",
                                     return_value=_blank_frame())
        self._cap_patch.start()
        self.addCleanup(self._cap_patch.stop)
        self._tap_patch = mock.patch("actions.adb_actions.tap")
        self.tap = self._tap_patch.start()
        self.addCleanup(self._tap_patch.stop)

    def _run(self, **chrome):
        with mock.patch("vision.region_detectors.dialog.detect_dialog", return_value=None):
            with mock.patch("vision.chrome_detector.get_chrome_detector") as gcd:
                chrome.setdefault("positions", {})
                gcd.return_value.detect.return_value = mock.MagicMock(**chrome)
                return exit_current_screen(frame=_blank_frame())

    def test_home_with_hamburger_does_not_tap_home(self):
        result = self._run(has_home=True, has_back_arrow=False, has_hamburger=True)
        self.assertNotEqual(result.method, "home_or_menu_x")
        self.assertNotIn(mock.call(*_HOME_SLOT_XY), self.tap.call_args_list)

    def test_home_without_hamburger_taps_home(self):
        result = self._run(has_home=True, has_back_arrow=False, has_hamburger=False)
        self.assertEqual(result.method, "home_or_menu_x")
        self.tap.assert_called_once_with(*_HOME_SLOT_XY)

    def test_hamburger_screen_falls_through_to_back_arrow(self):
        result = self._run(has_home=True, has_back_arrow=True, has_hamburger=True)
        self.assertEqual(result.method, "back_arrow")


class DetectedPositionTests(unittest.TestCase):
    """Tap where the icon WAS DETECTED, not where it used to be.

    The game re-bakes its camera-cutout offset per screen, so the chrome row slides.
    Live 2026-08-21, Jakarta's Market: the Home icon was detected at (2219,44) while
    _HOME_SLOT_XY says (2300,45) — 81px away, on nothing.
    """

    def setUp(self):
        self._cap_patch = mock.patch("capture.adb_capture.capture_screen",
                                     return_value=_blank_frame())
        self._cap_patch.start()
        self.addCleanup(self._cap_patch.stop)
        self._tap_patch = mock.patch("actions.adb_actions.tap")
        self.tap = self._tap_patch.start()
        self.addCleanup(self._tap_patch.stop)

    def _run(self, positions):
        with mock.patch("vision.region_detectors.dialog.detect_dialog", return_value=None):
            with mock.patch("vision.chrome_detector.get_chrome_detector") as gcd:
                gcd.return_value.detect.return_value = mock.MagicMock(
                    has_home=True, has_back_arrow=False, has_hamburger=False,
                    positions=positions)
                return exit_current_screen(frame=_blank_frame())

    def test_taps_the_detected_home_position(self):
        result = self._run({"home": (2219, 44)})
        self.assertEqual(result.method, "home_or_menu_x")
        self.tap.assert_called_once_with(2219, 44)

    def test_falls_back_to_the_calibrated_slot_when_undetected(self):
        """Detection can still fail; the old slot remains the fallback, not the default."""
        result = self._run({})
        self.assertEqual(result.method, "home_or_menu_x")
        self.tap.assert_called_once_with(*_HOME_SLOT_XY)
