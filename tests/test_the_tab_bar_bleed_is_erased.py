"""The world map's tab bar is translucent, and the bleed is separable by BRIGHTNESS.

Live 2026-08-31 a Hutu Village run died at `sail_to_village`. The whole-frame parse returned
Explore, Weymouth and Route as a SINGLE element reading 'Explo eymoutRoute' — Plymouth's map
label sitting across the gap between two tabs. That token contains 'route', so it scored 0.95
by containment, took the slot, and carried Explore's element away with it. The row came back
['port', 'route', 'trade'] and `select_world_map_tab('explore')` refused to read another
tab's rail — correctly, on a reading that was wrong.

No scoring rule recovers this: once two tab names are ONE token, the assignment can spend it
on at most one of them. The merge has to be prevented.

It can be, because the game itself draws the two layers differently (user, 2026-09-01). Tab
labels are a flat UI grey — measured at exactly 149 on Explore, Route and Trade — while the
map behind the bar is DIMMED by the translucent overlay before it reaches the eye: Plymouth's
strokes measured 89, indistinguishable from the bar's own background at 92. That is a gap
about fifty wide, so the cutoff is not a tuned number.

Measured on the failing frames: 'Explo eymoutRoute' and 'CalTrade' become 'Explore', 'Route'
and 'Trade' — exact, with no fuzzy matching left to do.
"""
import unittest

import numpy as np
from PIL import Image

from actions.sail_actions import (_TAB_BAND_Y, _TAB_TEXT_LUMA,
                                  _tab_band_without_bleed)

# The three luminances that matter, taken off frame_0089 of
# trace_barter_cmd_2026-08-31T23-50-52.
TAB_LABEL = 149      # 'Explore', 'Route', 'Trade' — a flat UI grey
BAR = 92             # the translucent bar's own background
BLEED = 89           # 'Plymouth' showing through it


def _grey(v):
    """An RGB triple whose luminance is v (the game draws this text neutral)."""
    return (v, v, v)


def _frame_with(bar_regions):
    """A frame carrying a translucent tab bar and whatever is painted into it."""
    img = Image.new("RGB", (2400, 1080), _grey(BAR))
    px = img.load()
    for (x1, y1, x2, y2), value in bar_regions:
        for x in range(x1, x2):
            for y in range(y1, y2):
                px[x, y] = _grey(value)
    return img


def _lum(img):
    a = np.asarray(img.convert("RGB")).astype(float)
    return 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]


EXPLORE = (990, 42, 1100, 63)
PLYMOUTH = (1063, 18, 1213, 40)


class TheBleedIsErasedAndTheLabelIsNot(unittest.TestCase):
    def setUp(self):
        self.frame = _frame_with([(EXPLORE, TAB_LABEL), (PLYMOUTH, BLEED)])
        self.band = _lum(_tab_band_without_bleed(self.frame))

    def test_the_tab_label_survives(self):
        x1, y1, x2, y2 = EXPLORE
        self.assertGreater(self.band[y1:y2, x1:x2].max(), 0,
                           "the tab's own label must come through untouched")

    def test_the_map_text_bleeding_through_is_gone(self):
        x1, y1, x2, y2 = PLYMOUTH
        # Plymouth's box overlaps nothing else, so anything left here is bleed.
        self.assertEqual(self.band[y1:y2, x1:x2].max(), 0,
                         "Plymouth is what merged two tabs into one token")

    def test_the_bar_background_goes_too(self):
        # BAR (92) and BLEED (89) are within three of each other — that is WHY the bleed is
        # unreadable to us and was readable to OmniParser, which sees edges, not levels.
        self.assertEqual(self.band[25:38, 1140:1180].max(), 0)

    def test_the_cutoff_sits_in_the_gap_not_on_an_edge(self):
        self.assertLess(BLEED, _TAB_TEXT_LUMA, "the bleed must fall below the cutoff")
        self.assertLessEqual(_TAB_TEXT_LUMA, TAB_LABEL, "the label must not")
        self.assertGreater(TAB_LABEL - BLEED, 40,
                           "a narrow gap would make this a tuned number rather than a fact")


class TheSelectedTabIsNotDestroyed(unittest.TestCase):
    """The selected tab is INVERTED — dark glyphs on a bright pill — so it must not vanish."""

    def test_a_bright_pill_survives(self):
        pill = (700, 30, 900, 75)
        band = _lum(_tab_band_without_bleed(_frame_with([(pill, 230)])))
        x1, y1, x2, y2 = pill
        self.assertGreater(band[y1:y2, x1:x2].max(), 0,
                           "'Port' reads as dark-on-bright and needs its pill")


class TheBandIsTheBandAndTheCacheIsSafe(unittest.TestCase):
    def test_only_the_tab_band_is_returned(self):
        out = _tab_band_without_bleed(_frame_with([]))
        self.assertEqual(out.size, (2400, _TAB_BAND_Y),
                         "cropping from the origin keeps cx/cy in frame coordinates")

    def test_the_same_frame_is_masked_once(self):
        f = _frame_with([(EXPLORE, TAB_LABEL)])
        self.assertIs(_tab_band_without_bleed(f), _tab_band_without_bleed(f))

    def test_a_different_frame_is_not_served_the_first_one(self):
        # id() is reused the moment a frame is freed — the collision `parse_fast_cached`
        # documents. Holding both alive here is the point of the test.
        a, b = _frame_with([(EXPLORE, TAB_LABEL)]), _frame_with([(PLYMOUTH, BLEED)])
        ma, mb = _tab_band_without_bleed(a), _tab_band_without_bleed(b)
        self.assertIsNot(ma, mb)
        self.assertGreater(_lum(ma).max(), 0, "a has a real label")
        self.assertEqual(_lum(mb).max(), 0, "b has only bleed")


if __name__ == "__main__":
    unittest.main()
