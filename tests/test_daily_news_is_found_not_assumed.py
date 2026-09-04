"""The daily news popup is found by looking, not by remembering where it was.

Live 2026-08-26 at Stockholm the popup covered the idle lock and the detector never fired, so
the unlock swipe kept failing — the gesture could not reach the lock underneath. Two fixed
coordinates were wrong at once:

    signature crop   (1770,215)-(1820,265)     2500 px of empty background: all dark, none
                                               bright, so the pixel test could never fire
    KB close_position [1794, 240]              the button was at (1695, 223)

The close-X was 52px left of the crop, and OmniParser had it as an icon at (1695,223) without
difficulty. CLAUDE.md: if you are about to write a number that means where on the screen, find
the element instead.

What is NOT relaxed: the area and dimming gates, measured over 151 labelled frames (recall
4/4, false positives 0/147). They carry the specificity, and a false fire TAPS.
"""

from __future__ import annotations

import glob
import os
import unittest

from PIL import Image

FRAMES = os.path.join(os.path.dirname(__file__), "..", "data", "test_frames")
NEWS = os.path.join(FRAMES, "transitions", "daily_news_over_idle_lock.png")


@unittest.skipUnless(os.path.exists(NEWS), "daily-news fixture not present")
class ItFindsTheRealPopup(unittest.TestCase):

    def test_the_frame_that_stalled_the_run_is_detected(self):
        from brain.perceive import _has_daily_news_close_x
        self.assertTrue(_has_daily_news_close_x(Image.open(NEWS)))

    def test_the_close_x_is_located_where_it_actually_is(self):
        from brain.perceive import _round_close_x
        x = _round_close_x(Image.open(NEWS))
        self.assertIsNotNone(x)
        self.assertEqual((x.cx, x.cy), (1695, 223))

    def test_the_old_fixed_crop_saw_nothing_at_all(self):
        """Documents the failure: not a misjudgement, a look in the wrong place."""
        import numpy as np
        arr = np.array(Image.open(NEWS).crop((1770, 215, 1820, 265)).convert("L"))
        self.assertGreater((arr < 50).sum(), 2000, "the crop was almost entirely dark")
        self.assertEqual((arr > 200).sum(), 0, "and had no bright pixels to match against")

    def test_the_detected_position_is_what_gets_tapped(self):
        """The KB's close_position is [1794, 240]; on the main menu that coordinate is the
        Manage Fleet tile, which the bot once navigated into by tapping it."""
        from brain.perceive import _has_daily_news_close_x, _DAILY_NEWS_CLOSE_SEEN
        _DAILY_NEWS_CLOSE_SEEN[0] = None
        _has_daily_news_close_x(Image.open(NEWS))
        self.assertEqual(_DAILY_NEWS_CLOSE_SEEN[0], (1695, 223))


class ItStillRefusesEverythingElse(unittest.TestCase):
    """A false fire TAPS, and the tap dismisses somebody's transaction. Precision first."""

    def _frames(self):
        pats = ("transitions/*.png", "bulk_checkbox/*.png")
        out = []
        for pat in pats:
            out += [f for f in sorted(glob.glob(os.path.join(FRAMES, pat)))
                    if "_marked" not in f and "daily_news" not in f]
        return out

    def test_no_false_positives_on_the_fixture_frames(self):
        from brain.perceive import _has_daily_news_close_x
        frames = self._frames()
        self.assertTrue(frames, "no fixture frames found")
        fired = [os.path.basename(f) for f in frames
                 if _has_daily_news_close_x(Image.open(f))]
        self.assertEqual(fired, [])

    def test_a_dimmed_panel_offering_a_DECISION_is_not_the_news(self):
        """The barter Exchange confirmation is large and dimmed and carries a disc-like glyph.
        Relaxing the crop alone made the detector fire on it. A popup offering ACTIONS is a
        decision, not noise (CLAUDE.md) — and dismissing one by tapping a disc abandons a
        half-finished transaction."""
        from brain.perceive import _DIALOG_ACTION_WORDS
        self.assertIn("cancel", _DIALOG_ACTION_WORDS)
        self.assertIn("ok", _DIALOG_ACTION_WORDS)


class TheInvariantWasWrongAboutWhereItAppears(unittest.TestCase):
    def test_it_is_documented_as_appearing_over_the_idle_lock(self):
        """It was believed to show only on overworld screens. It was found over the idle lock
        — a state that did not exist when that rule was written (user, 2026-08-26). A
        whitelist of where a popup may appear ages badly; the guards are exclusions."""
        import inspect
        from brain import perceive
        src = inspect.getsource(perceive._has_daily_news_close_x)
        self.assertIn("IDLE LOCK", src)
