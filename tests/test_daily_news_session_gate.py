"""Tests for the daily-news Moondream session-level suppression.

After the first Moondream NO verdict, subsequent calls within the TTL
should short-circuit before reaching Moondream.
"""
import time
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

import brain.perceive as perceive_mod


def _frame_with_close_x_signature():
    """Build a real frame whose (1770-1820, 215-265) crop contains the
    dark + bright pixel cluster Stage 1 expects.  Uses an alternating
    checker pattern so >100 dark and >50 bright pixels are present in
    the 50×50 crop (2500 total pixels)."""
    img = Image.new("RGB", (2400, 1080), (128, 128, 128))
    for y in range(215, 265):
        for x in range(1770, 1820):
            if (x + y) % 2 == 0:
                img.putpixel((x, y), (10, 10, 10))     # dark
            else:
                img.putpixel((x, y), (250, 250, 250))  # bright
    return img


class DailyNewsSessionGateTests(unittest.TestCase):

    def setUp(self):
        # Reset the session gate before each test
        perceive_mod._daily_news_no_suppress_until = 0.0

    def _patch_overworld_chrome(self):
        mock_chrome = MagicMock()
        mock_chrome.has_home = False
        mock_chrome.has_back_arrow = False
        detector = MagicMock()
        detector.detect.return_value = mock_chrome
        return patch("vision.chrome_detector.get_chrome_detector",
                     return_value=detector)

    def test_first_call_invokes_moondream_then_suppresses(self):
        frame = _frame_with_close_x_signature()
        moondream_calls = []

        def fake_ask_cached(_frame, _key, fn):
            moondream_calls.append(_key)
            return fn()

        with self._patch_overworld_chrome(), \
             patch("vision.moondream_cache.ask_cached",
                   side_effect=fake_ask_cached), \
             patch.object(perceive_mod,
                          "_has_daily_news_close_x_moondream_inference",
                          return_value=False):
            # First call: Moondream invoked, returns NO, sets suppress
            result1 = perceive_mod._has_daily_news_close_x(frame)
            self.assertFalse(result1)
            self.assertEqual(len(moondream_calls), 1)
            self.assertGreater(
                perceive_mod._daily_news_no_suppress_until, time.time(),
            )

            # Second call: suppressed, no Moondream
            result2 = perceive_mod._has_daily_news_close_x(frame)
            self.assertFalse(result2)
            self.assertEqual(len(moondream_calls), 1)

            # Third call: still suppressed
            result3 = perceive_mod._has_daily_news_close_x(frame)
            self.assertFalse(result3)
            self.assertEqual(len(moondream_calls), 1)

    def test_suppression_expires_after_ttl(self):
        frame = _frame_with_close_x_signature()
        moondream_calls = []

        def fake_ask_cached(_frame, _key, fn):
            moondream_calls.append(_key)
            return fn()

        with self._patch_overworld_chrome(), \
             patch("vision.moondream_cache.ask_cached",
                   side_effect=fake_ask_cached), \
             patch.object(perceive_mod,
                          "_has_daily_news_close_x_moondream_inference",
                          return_value=False):
            # Trigger initial suppression
            perceive_mod._has_daily_news_close_x(frame)
            self.assertEqual(len(moondream_calls), 1)

            # Force TTL expiry
            perceive_mod._daily_news_no_suppress_until = time.time() - 1.0

            # Next call: suppression expired, Moondream called again
            perceive_mod._has_daily_news_close_x(frame)
            self.assertEqual(len(moondream_calls), 2)

    def test_chrome_context_guard_still_short_circuits_first(self):
        """If we're inside a building (back-arrow visible), the Stage-2
        context guard rejects before reaching Moondream regardless of
        session suppression state."""
        frame = _frame_with_close_x_signature()

        mock_chrome = MagicMock()
        mock_chrome.has_home = True
        mock_chrome.has_back_arrow = True
        detector = MagicMock()
        detector.detect.return_value = mock_chrome

        with patch("vision.chrome_detector.get_chrome_detector",
                   return_value=detector), \
             patch("vision.moondream_cache.ask_cached") as mock_md:
            result = perceive_mod._has_daily_news_close_x(frame)
            self.assertFalse(result)
            mock_md.assert_not_called()

    def test_pixel_signature_miss_skips_everything(self):
        """A frame without the pixel pattern returns False without
        touching chrome or Moondream."""
        plain = Image.new("RGB", (2400, 1080), (128, 128, 128))
        with patch("vision.chrome_detector.get_chrome_detector") as mock_cd, \
             patch("vision.moondream_cache.ask_cached") as mock_md:
            result = perceive_mod._has_daily_news_close_x(plain)
            self.assertFalse(result)
            mock_cd.assert_not_called()
            mock_md.assert_not_called()


if __name__ == "__main__":
    unittest.main()
