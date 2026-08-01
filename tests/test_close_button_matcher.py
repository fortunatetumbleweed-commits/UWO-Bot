"""Tests for vision.close_button_matcher.find_close_button.

Positive: runs against the captured daily-news frame and asserts the X
is found near the expected coordinate with high confidence.

Negative: runs against a frame that has no daily-news popup and asserts
the matcher returns None (no false positive).
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from PIL import Image

from vision.close_button_matcher import (
    find_close_button,
    CloseButtonMatch,
)


_REPO = Path(__file__).resolve().parent.parent

# Test asset: the captured daily-news popup frame, X centre ~ (1790, 220).
# Lives at /tmp/daily_news_stuck.png from the 2026-05-25 sail-to-London
# run.  Tests skip gracefully when the file isn't present (e.g. fresh
# clone) — the matcher itself is still exercised by the unit cases.
_DAILY_NEWS_FRAME = Path("/tmp/daily_news_stuck.png")


def _skip_if_no_frame() -> bool:
    return not _DAILY_NEWS_FRAME.exists()


class CloseButtonMatcherTests(unittest.TestCase):

    @unittest.skipIf(_skip_if_no_frame(), f"missing {_DAILY_NEWS_FRAME}")
    def test_daily_news_x_found_in_captured_frame(self):
        img = Image.open(_DAILY_NEWS_FRAME)
        result = find_close_button(img, "daily_news")
        self.assertIsNotNone(result, "expected to find the daily-news X")
        # Expected X centre ~ (1790, 220).  Allow ±25 px slop for the
        # template's centre vs the icon's visual centre.
        self.assertAlmostEqual(result.cx, 1790, delta=25)
        self.assertAlmostEqual(result.cy, 220, delta=25)
        self.assertGreater(result.confidence, 0.75)

    def test_unknown_button_returns_none(self):
        img = Image.new("RGB", (2400, 1080), (0, 100, 200))
        result = find_close_button(img, "no_such_button")
        self.assertIsNone(result)

    def test_no_match_on_blank_frame(self):
        # Solid colour frame — no X anywhere → matcher should return None
        # rather than fabricate a low-confidence match.
        img = Image.new("RGB", (2400, 1080), (60, 130, 200))
        result = find_close_button(img, "daily_news")
        self.assertIsNone(result)

    @unittest.skipIf(_skip_if_no_frame(), f"missing {_DAILY_NEWS_FRAME}")
    def test_threshold_rejects_when_impossible_to_match(self):
        # Threshold > 1.0 is impossible for TM_CCOEFF_NORMED — sanity
        # check that the threshold parameter is actually consulted, not
        # ignored.
        img = Image.open(_DAILY_NEWS_FRAME)
        result = find_close_button(img, "daily_news", threshold=1.01)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
