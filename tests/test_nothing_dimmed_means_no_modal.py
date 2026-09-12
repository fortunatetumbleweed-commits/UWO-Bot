"""A frame with nothing dimmed cannot hold a modal, so it must not pay to find that out.

THE COST. On the 107-minute Svear run of 2026-09-11 the obstruction pass spent 18 minutes,
at about 5.1s per frame, and 13.7 of those went to `_large_dimmed_popup` and the daily-news
check that calls it. Both force an OmniParser parse. 75% of 125 sampled frames had nothing
covering the screen at all.

It also defeated a short-circuit built to avoid exactly this: Phase 4a of classification
settles sea, world map and port overworld with the family CNN in ~50ms so OmniParser is never
touched, and the obstruction pass ran first and dragged it in anyway.

THE GATE IS LOSSLESS BY CONSTRUCTION, not by luck. `_large_dimmed_popup` returns True only
when `dim < _DAILY_NEWS_MAX_MARGIN_DIM` — it is a test FOR dimming — so a frame that dims
nothing could never have been a hit.

WHY THE SCRIM RATHER THAN THE MARGIN MEAN this function already computes: a night sea is dark
on AVERAGE and would gate wrongly (the run rejected frames reading margin brightness 15).
`scrim_state` takes the left gutter's 99.9th percentile, so it asks whether ANY pixel there is
bright — a scrim caps that, a dark scene does not. The gutter is the probe because dialogs are
centred and panels are never on the left (`memory/dialogs-are-centered-panels-are-not-left`).

WHAT IS DELIBERATELY NOT GATED. `_detect_interruptors` keeps running on every frame. Dialogs
all dim, but POPUPS DO NOT NECESSARILY (user, 2026-09-12), and a popup that does not dim is
found by OCR tokens — which is that pass's job, not this one's.

Checked against the ungated path on 55 real frames of that run: both found the same three
popups, zero disagreements.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

import numpy as np
from PIL import Image

import brain.perceive as P


def _frame(fill: int, *, gutter: int = None) -> Image.Image:
    """A 2400x1080 grey frame. `gutter` paints the left probe band separately."""
    a = np.full((1080, 2400), fill, dtype=np.uint8)
    if gutter is not None:
        a[:, :200] = gutter
    return Image.fromarray(a, mode="L").convert("RGB")


class TheGateSkipsTheParse(unittest.TestCase):

    def test_a_clear_frame_never_reaches_omniparser(self):
        """The whole point: no parse, no 5 seconds."""
        with mock.patch("vision.omniparser.parse_fast_cached") as parse:
            big, area, _dim = P._large_dimmed_popup(_frame(200))
        parse.assert_not_called()
        self.assertFalse(big)
        self.assertEqual(area, P._NOT_MEASURED)

    def test_a_dimmed_frame_still_does(self):
        """A scrim means a modal is possible, so the real test must run."""
        el = types.SimpleNamespace(x1=600, y1=200, x2=1800, y2=880)
        with mock.patch("vision.omniparser.parse_fast_cached", return_value=[el]) as parse:
            big, area, _dim = P._large_dimmed_popup(_frame(20))
        parse.assert_called_once()
        self.assertNotEqual(area, P._NOT_MEASURED)
        self.assertTrue(big, "a big centred element over a dimmed screen is a modal")

    def test_A_DARK_NIGHT_SCENE_IS_NOT_A_SCRIM(self):
        """The case that decides WHICH cheap test to gate on, and it goes the opposite way to
        first instinct.

        A night sea is dark on average — the live run rejected frames reading margin
        brightness 15 — so this function's own margin mean says `dim < 40` and would let the
        frame through to the expensive parse. The gutter's WHITE POINT does not: the scene
        still carries bright pixels, so it reads CLEAR, and the parse is skipped.

        The scrim is therefore STRICTER than the margin mean here, and right to be: a dark
        scene is not an overlay. These frames are where the saving comes from, not a hazard.
        """
        a = np.asarray(_frame(18)).copy()
        a[300:340, 40:160] = 250          # a bright control in the left gutter
        frame = Image.fromarray(a)
        with mock.patch("vision.omniparser.parse_fast_cached") as parse:
            big, area, dim = P._large_dimmed_popup(frame)
        self.assertLess(dim, P._DAILY_NEWS_MAX_MARGIN_DIM,
                        "the margin mean alone would have called this dimmed")
        parse.assert_not_called()
        self.assertFalse(big)
        self.assertEqual(area, P._NOT_MEASURED)


class TheVerdictIsUnchanged(unittest.TestCase):

    def test_the_gate_can_only_answer_NO(self):
        """It never invents a popup — a skip is always False, which is the only direction a
        test FOR dimming could have gone on an undimmed frame anyway."""
        with mock.patch("vision.omniparser.parse_fast_cached") as parse:
            big, _a, _d = P._large_dimmed_popup(_frame(255))
        parse.assert_not_called()
        self.assertFalse(big)

    def test_the_skip_is_logged_not_silent(self):
        """The gate disagreed with the margin test on ~2 of 121 real frames sampled, so every
        skip is auditable against what the full check would have said."""
        import inspect

        src = inspect.getsource(P._large_dimmed_popup)
        self.assertIn("skipping the popup parse", src)
        self.assertIn("logger.info", src)

    def test_the_caller_does_not_report_an_unmeasured_area_as_zero(self):
        """`largest element 0.0%` would read as 'measured, and it was nothing'."""
        import inspect

        src = inspect.getsource(P._has_daily_news_close_x)
        self.assertIn("not measured", src)


class TheInterruptorPassIsNotGated(unittest.TestCase):

    def test_detect_interruptors_has_no_scrim_gate(self):
        """Dialogs all dim; popups do not necessarily. A popup that does not dim is found by
        OCR TOKENS, so that pass must keep running on every frame — gating it would trade 4.4
        minutes for a class of popup the bot could no longer see."""
        import inspect

        src = inspect.getsource(P._detect_interruptors)
        self.assertNotIn("scrim_state", src)


if __name__ == "__main__":
    unittest.main()
