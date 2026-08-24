"""`_is_on_overworld` must not blind-tap the screen centre on a real screen.

When it can see no chrome, no port name and no right panel, it concludes "full-screen
overlay" and taps the centre to dismiss it. That is reasonable for a splash — and dangerous
anywhere else, because the tap does whatever is under the centre point.

Live 2026-08-21, Jakarta (session trace_barter_cmd_2026-08-21T21-47-28, frames 1-6): the bot
was on the market's Purchase grid. Chrome detection returned all-False — the Home icon sat
at x 2159-2279 while CHROME_HOME_REGION started at 2280 — so the heuristic fired and tapped
(1200, 540), which on that grid is the **Lac Powder** tile. 385 units (130,900 ducats) went
into the cart. Back then raised "Moving to another menu will empty the cart. Continue?", the
next centre tap hit the dialog's body text, and the cycle repeated three times.

The fix is upstream (the chrome search regions now absorb the camera-cutout shift), so this
guards the behaviour that depended on it.
"""
from unittest import mock

import actions.sail_actions as sa


class _Frame:
    width, height = 2400, 1080


def _chrome(**kw):
    c = mock.MagicMock()
    c.has_home = kw.get("home", False)
    c.has_back_arrow = kw.get("back", False)
    c.has_hamburger = kw.get("hamburger", False)
    c.has_right_panel = kw.get("right_panel", False)
    c.positions = kw.get("positions", {})
    return c


def _run(chrome, port_name=None):
    taps = []
    with mock.patch("vision.chrome_detector.get_chrome_detector") as gcd, \
         mock.patch("vision.ocr.read_port_name", return_value=port_name), \
         mock.patch("vision.ocr.read_screen_title", return_value="purchase"), \
         mock.patch("actions.adb_actions.tap",
                    side_effect=lambda x, y: taps.append((x, y))), \
         mock.patch.object(sa, "_scene_model_says_overworld", return_value=None, create=True):
        gcd.return_value.detect.return_value = chrome
        result = sa._is_on_overworld(_Frame())
    return result, taps


class TestNoBlindTapWhenChromeIsVisible:
    def test_a_visible_home_button_means_inside_something_not_an_overlay(self):
        """The Purchase screen HAS a Home button; seeing it must end the check without
        touching the screen."""
        result, taps = _run(_chrome(home=True, positions={"home": (2219, 44)}))
        assert result is False
        assert taps == [], "no tap: this is a real screen, not a splash"

    def test_a_visible_back_arrow_also_ends_the_check(self):
        result, taps = _run(_chrome(back=True))
        assert result is False
        assert taps == []

    def test_a_port_name_confirms_the_overworld_without_tapping(self):
        result, taps = _run(_chrome(), port_name="Jakarta")
        assert result is True
        assert taps == []


class TestTheOverlayFallbackStillExists:
    def test_a_screen_with_no_signals_at_all_is_still_tapped(self):
        """The heuristic is not removed — a genuine chrome-less splash still gets a tap.
        This is the case it was written for."""
        result, taps = _run(_chrome())
        assert result is False
        assert taps == [(1200, 540)]
