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


def _run(margin_brightness, popup_pct, *, centre=True):
    """A synthetic frame with one big element.

    CENTRED BY DEFAULT, because a modal is. The element used to be pinned at (100,100) —
    incidental to what these tests check (the area and dimming thresholds), but it put the
    fake popup at dy 17-20% from centre, which is where a VILLAGE panel sits, not a popup.
    Two real daily-news frames measure dy 6.7% and 8.3% (2026-08-30).
    """
    img, pct = _frame(margin_brightness, popup_pct)
    area = pct / 100.0 * 2400 * 1080
    side = int(area ** 0.5)
    x1, y1 = ((2400 - side) // 2, (1080 - side) // 2) if centre else (100, 100)
    el = mock.MagicMock(x1=x1, y1=y1, x2=x1 + side, y2=y1 + side)
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


def test_a_village_panel_is_not_a_modal_because_a_modal_is_CENTRED():
    """LIVE 2026-08-30: the detector fired THREE TIMES on a legitimate village screen and
    tapped a different round icon each time — the '?' on the title (which opened learning
    mode), a top-right icon, and the Ducat icon (which opened a tooltip that then ate the
    Back press meant to leave the village, stalling the run one leg from home).

    Size and dimming both passed, and they were the whole test. A village at night has a
    BIGGER panel than any real daily-news popup (12.0% against the calibrated "true 7.5+")
    and a darker margin than the ceiling. Two weak large-scale signals failing together are
    not independent evidence.

    A modal is CENTRED; the village's panel sits off to one side (user, 2026-08-30):

        real daily_news   box (515,267)-(1717,669)    dx  3.5%  dy  6.7%
        village panel     box (1451,121)-(2230,511)   dx 26.7%  dy 20.7%

    Vertical is the reliable axis — horizontal may shift with orientation and the camera
    cutout, so it is bounded loosely rather than tested.
    """
    import pathlib as _p

    import pytest
    from PIL import Image

    from brain.perceive import _has_daily_news_close_x

    real = _p.Path("data/test_frames/transitions/daily_news_over_idle_lock.png")
    village = _p.Path("data/sessions/trace_barter_cmd_2026-08-30T11-55-40/frame_0140.png")
    if not (real.exists() and village.exists()):
        pytest.skip("reference frames not present")

    assert _has_daily_news_close_x(Image.open(real).convert("RGB")) is True
    assert _has_daily_news_close_x(Image.open(village).convert("RGB")) is False


def test_the_corner_rule_would_not_have_worked():
    """Recorded so it is not re-proposed. "The close-X sits near a corner of the big box"
    admits the Ducat icon too — the relative geometry is nearly identical, and only WHICH
    corner separates them (user, 2026-08-30):

        real close-X   (-22, -44) from its popup's top-RIGHT
        Ducat icon     (+51, -72) from the village panel's top-LEFT

    Centring is what actually separates the two cases, by a factor of three.
    """
    real_dy_pct, village_dy_pct = 6.7, 20.7
    from brain.perceive import _DAILY_NEWS_MAX_DY_PCT

    assert real_dy_pct <= _DAILY_NEWS_MAX_DY_PCT < village_dy_pct


def test_an_off_centre_panel_is_not_a_modal():
    """LIVE 2026-08-30: a village screen fired this detector three times, tapping the '?' on
    the title, a top-right icon and the Ducat icon. Its panel is BIGGER than any real popup
    (12.0% against "true 7.5+") and its margin darker than the ceiling, so both original
    conjuncts passed. A modal is centred; that panel is not (user)."""
    assert not _run(margin_brightness=22, popup_pct=12.0, centre=False)[0]
    assert _run(margin_brightness=22, popup_pct=12.0, centre=True)[0]
