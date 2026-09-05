"""Market restock/refresh button detector — the white pill in the Purchase-grid header.

THE TIMER IS NOT PART OF THE BUTTON (user, 2026-09-05: "right now the timer should not be
used at all... it should not interfere with the refresh").

This detector used to anchor on the OCR'd countdown and return None when the regex did not
match. Measured on two consecutive frames of ONE UNCHANGED SCREEN at Antalya:

    frame 294   '00.00.51'    matches   -> button found
    frame 295   '00.00:.42'   no match  -> None

One stray colon. `refresh_market` reported "no restock control", `buy_to_goal` read that as a
market that cannot be refreshed and broke out, and the leg stopped at 595 Mutton of 1,015 —
three barter rounds lost with the ↻ on screen at 3 blue gems, and not one line in the log to
say so.

The fixture below models the pill AS MEASURED on those frames: bright columns 1375-1422 and
1424-1578, i.e. one pill with the dark ↻ glyph splitting it near the left, and the gem cost
toward the right. The old fixture drew the pill at 1454-1580 with the ↻ *outside* it, which
only made sense while the timer was the anchor.
"""
import numpy as np
from PIL import Image

from vision.region_detectors.market_restock import find_restock_button

BAND_Y0, BAND_Y1 = 132, 188          # the pill's own vertical extent, inside the search band


def _frame(*, gem="blue", timer="00:00:36", pill=(1375, 1578), glyph=(1422, 1424)):
    """A dark frame carrying the restock pill as the game draws it."""
    arr = np.full((1080, 2400, 3), 25, dtype=np.uint8)
    arr[BAND_Y0:BAND_Y1, pill[0]:pill[1]] = (235, 235, 235)      # the white pill
    arr[BAND_Y0:BAND_Y1, glyph[0]:glyph[1]] = (40, 40, 40)       # the ↻ glyph splitting it
    if gem:                                                      # the cost icon, right of ↻
        arr[BAND_Y0 + 8:BAND_Y1 - 8, 1508:1534] = ((95, 120, 195) if gem == "blue"
                                                   else (200, 60, 60))
    ocr = lambda _f, min_conf=0.2: ([(timer, 0.9, 1308, 156)] if timer else [])
    return Image.fromarray(arr, "RGB"), ocr


def test_finds_the_blue_gem_pill():
    b = find_restock_button(*_frame(gem="blue"))
    assert b is not None and b.currency == "blue_gem"


def test_the_tap_target_is_the_refresh_glyph_not_the_gem():
    """A centre-tap on the gem does nothing (verified live 2026-08-17). The ↻ sits in the
    pill's first segment, left of the dark glyph."""
    b = find_restock_button(*_frame())
    assert 1375 <= b.cx <= 1422, b.cx


def test_red_gem_is_reported_not_silently_taken():
    """Real money. The caller refuses it; the detector must still name it."""
    b = find_restock_button(*_frame(gem="red"))
    assert b is not None and b.currency == "red_gem"


class TheTimerNeverGatesIt:
    """The regression this file exists for."""


def test_a_missing_timer_does_not_hide_the_button():
    assert find_restock_button(*_frame(timer=None)) is not None


def test_a_MANGLED_timer_does_not_hide_the_button():
    """'00.00:.42' — the exact OCR that cost three barter rounds."""
    b = find_restock_button(*_frame(timer="00.00:.42"))
    assert b is not None
    assert b.timer is None, "an unparseable countdown is reported as unknown, not invented"


def test_the_timer_still_rides_along_when_it_reads():
    """Kept for the log, and for a future caller that would rather wait out a nearly-expired
    countdown than spend a gem."""
    assert find_restock_button(*_frame(timer="00:00:36")).timer == "00:00:36"


def test_no_pill_means_no_control():
    arr = np.full((1080, 2400, 3), 25, dtype=np.uint8)
    ocr = lambda _f, min_conf=0.2: [("00:00:36", 0.9, 1308, 156)]
    assert find_restock_button(Image.fromarray(arr, "RGB"), ocr) is None


def test_a_pill_with_no_gem_cost_is_not_the_control():
    """Other screens carry wide white areas in this band — the village barter panel and the
    hold view both do. The gem is what makes this button that button."""
    assert find_restock_button(*_frame(gem=None)) is None
