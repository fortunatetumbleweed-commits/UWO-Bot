"""daily_news is a LARGE DIMMED POPUP — not a 50x50 ornament.

The detector used to decide on a pixel probe at a hardcoded (1794, 240): does that patch
contain dark pixels and bright pixels? That cannot tell a dark round X from dark text on a
light tile. Live 2026-08-22 the patch contained the word "Fleet" on the main menu (186 dark
/ 1440 bright), the probe passed, Moondream "confirmed", and the dismissal tapped its
remembered close coordinate — which on the main menu is the Fleet tile. The bot navigated
into Manage Fleet and the run died at "cargo capacity unreadable".

Measured over 151 distinct labelled frames (uwo_v2 label server):

                          largest element     margin dimming
    daily_news (4)          7.5 - 21.3 %       18.2 - 27.5
    false positives         <= 5.0 %           >= 59.9      (6 main_menu, 1 gameplay)

    pixel signature alone   : recall 4/4, false positives 7/147
    + area and dimming      : recall 4/4, false positives 0/147

FALSE POSITIVES ARE THE EXPENSIVE FAILURE (user 2026-08-22): daily_news appears once a day,
so a miss just leaves it on screen, while a false fire TAPS a remembered coordinate on a
screen the bot has misread. Hence the gates are ANDed and every uncertain path fails closed.
"""
from unittest import mock

import numpy as np
import pytest
from PIL import Image

import brain.perceive as p


def _frame(margin_brightness: int, popup_pct: float):
    """A frame with the given outer-margin brightness and one popup-sized element."""
    a = np.full((1080, 2400, 3), margin_brightness, dtype=np.uint8)
    return Image.fromarray(a), popup_pct


def _run(margin_brightness, popup_pct):
    img, pct = _frame(margin_brightness, popup_pct)
    area = pct / 100.0 * 2400 * 1080
    side = int(area ** 0.5)
    el = mock.MagicMock(x1=100, y1=100, x2=100 + side, y2=100 + side)
    with mock.patch("vision.omniparser.parse_fast_cached", return_value=[el]):
        return p._large_dimmed_popup(img)


class TestLargeDimmedPopup:
    def test_a_real_daily_news_shape_passes(self):
        """dialog_system measured 7.5-21.3% area at 18-28 margin brightness."""
        ok, area, dim = _run(margin_brightness=22, popup_pct=10.0)
        assert ok, f"area={area:.1f}% dim={dim:.0f} should qualify"

    def test_the_smallest_measured_true_popup_passes(self):
        assert _run(margin_brightness=27, popup_pct=7.5)[0]

    def test_the_main_menu_shape_is_rejected(self):
        """The live false positive: small elements, undimmed screen."""
        assert not _run(margin_brightness=110, popup_pct=3.2)[0]

    def test_a_bright_screen_is_rejected_even_with_a_big_element(self):
        """Building interiors reach 66% area but are not dimmed."""
        assert not _run(margin_brightness=80, popup_pct=40.0)[0]

    def test_a_dim_screen_with_only_small_elements_is_rejected(self):
        """Some dialogs dim the background but are small — e.g. dialog_shop."""
        assert not _run(margin_brightness=20, popup_pct=4.5)[0]


class TestFailsClosed:
    def test_a_detector_error_does_not_fire(self):
        """No evidence must never mean 'go ahead' — this gate authorises a tap."""
        img, _ = _frame(20, 10.0)
        with mock.patch("vision.omniparser.parse_fast_cached",
                        side_effect=RuntimeError("no yolo")):
            ok, area, dim = p._large_dimmed_popup(img)
        assert ok is False and dim == 255.0

    def test_no_elements_means_no_popup(self):
        img, _ = _frame(20, 0)
        with mock.patch("vision.omniparser.parse_fast_cached", return_value=[]):
            assert not p._large_dimmed_popup(img)[0]


class TestMoondreamIsGone:
    def test_the_detector_no_longer_calls_moondream(self):
        """Measured on the labelled set with the exact production prompt: recall 2/4,
        false positives 3/5 — noise in both directions, at 2-5s per call, with an
        'unavailable -> True' default that made a MISSING model more likely to fire."""
        import inspect
        src = inspect.getsource(p._has_daily_news_close_x)
        # Check for CALLS, not the word — the comments deliberately record why it was
        # dropped, so that history is not lost to a future reader.
        for call in ("ask_cached", "get_vision(", "vision.ask"):
            assert call not in src, \
                f"{call} is back in the detector; it was measured as noise (recall 2/4, " \
                "false positives 3/5) — do not reinstate it un-measured"
