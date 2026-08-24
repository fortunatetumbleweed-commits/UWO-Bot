"""daily_news fires only on an overworld — never on the main menu.

The context guard rejected buildings and sub-menus by looking for Home / back-arrow chrome.
The MAIN MENU has neither, so "no chrome" was read as "must be an overworld" and the guard
passed.

Live 2026-08-22: the ☰ opened the main menu, the daily_news pixel signature fired there,
Moondream confirmed YES, and the dismissal tapped its remembered close position (1794, 240)
— which on the main menu is the **Manage Fleet** tile. The bot navigated into Manage Fleet,
`read_fleet_status` then found itself in 'building' with no ☰ to open, and the whole run
died at "cargo capacity unreadable — pass cargo_capacity=/cargo_used=".

Inferring a state from the absence of evidence is the bug; the guard now tests positively
for the main menu's own tile vocabulary.
"""
from unittest import mock

import numpy as np
import pytest
from PIL import Image

import brain.perceive as p


def _frame():
    """A DIMMED frame — the size/dimming gate now requires a real popup shape, so a plain
    uniform frame would be rejected before any context guard is reached."""
    return Image.fromarray(np.full((1080, 2400, 3), 22, dtype=np.uint8))


def _ocr(words):
    return [(w, 0.9, 100, 100) for w in words]


def _check(words, *, home=False, back=False):
    """Run _has_daily_news_close_x with the given screen text.

    The pixel signature is forced ON and Moondream forced to say YES, so the ONLY thing that
    can return False is a context guard. That makes the result a direct read of "did a guard
    reject this screen?" rather than of the detector's other stages.
    """
    chrome = mock.MagicMock(has_home=home, has_back_arrow=back)
    # a 50x50 crop that satisfies the pixel signature (dark pixels AND bright pixels)
    crop = np.zeros((50, 50, 3), dtype=np.uint8)
    crop[:30, :] = 255
    # a popup-sized element, so the size/dimming gate passes and the CONTEXT GUARD is
    # what the test actually exercises
    big = mock.MagicMock(x1=100, y1=100, x2=1300, y2=800)
    with mock.patch("vision.chrome_detector.get_chrome_detector") as gcd, \
         mock.patch("actions.sail_actions._ocr_frame", return_value=_ocr(words)), \
         mock.patch("numpy.array", return_value=crop), \
         mock.patch("vision.omniparser.parse_fast_cached", return_value=[big]):
        gcd.return_value.detect.return_value = chrome
        return p._has_daily_news_close_x(_frame())


MAIN_MENU = ["Auction", "Friend", "Guild", "Rank", "Manage Fleet"]
OVERWORLD = ["Jakarta", "Harbor", "Market", "Shipyard"]


class TestMainMenuIsNotAnOverworld:
    def test_the_main_menu_is_rejected(self):
        """Two or more main-menu tile words mean the main menu is up."""
        assert _check(MAIN_MENU) is False

    def test_a_building_is_still_rejected_by_the_chrome_guard(self):
        assert _check(["Market", "Purchase"], home=True) is False

    def test_a_single_incidental_word_does_not_reject(self):
        """'rank' or 'mission' can appear elsewhere; one hit is not the main menu, so the
        guard must NOT fire — otherwise a real daily_news on a port overworld gets ignored."""
        assert _check(["Jakarta", "Harbor", "Rank"]) is True

    def test_a_real_overworld_still_detects_daily_news(self):
        """The guard must not break the case it exists to serve."""
        assert _check(OVERWORLD) is True
