"""The sweep must end because the LIST stopped, not because a screen failed to parse.

The old rule ended a sweep after two screens produced no new parse output, which cannot tell
"there is nothing more to read" from "I could not read this screen". Live at Cheyenne
2026-08-25 a parser bug blanked two screens mid-list, and that rule would have reported the
list finished with American Bison and Eagle Feather still below the fold.

These run against frames captured off the device, so the expected answers are known: the list
advances for three scrolls and is against its bottom stop from the fourth on.
"""

from __future__ import annotations

import glob
import os
import unittest

from PIL import Image

FRAMES = sorted(glob.glob("/tmp/village_scroll_report/cheyenne_village/frames/tick_*.png"))


@unittest.skipUnless(len(FRAMES) >= 6, "captured Cheyenne frames not present")
class TheScreenDecidesWhenTheSweepEnds(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from vision.omniparser import parse_fast_cached
        import tools.learn_village_barter as L
        cls.frames, cls.vps = [], []
        for f in FRAMES[:6]:
            img = Image.open(f).convert("RGB")
            cls.frames.append(img)
            cls.vps.append(L._list_viewport(list(parse_fast_cached(img))))

    def test_the_list_advances_while_there_is_more_below(self):
        from vision.list_position import content_shift
        for i in (1, 2, 3):
            dy, err, zero = content_shift(self.frames[i - 1], self.frames[i])
            self.assertNotEqual(dy, 0, f"frame {i} should have advanced")
            self.assertLess(err, 0.6 * zero, "the shift should explain the frame")

    def test_the_bottom_stop_is_seen_as_the_bottom(self):
        from vision.list_position import scrollbar_thumb, bar_position, content_shift
        at_top, at_end = bar_position(scrollbar_thumb(self.frames[4], self.vps[4]),
                                      self.vps[4])
        self.assertTrue(at_end, "the thumb is against the bottom stop")
        self.assertFalse(at_top)
        dy, _err, _zero = content_shift(self.frames[3], self.frames[4])
        self.assertEqual(dy, 0, "and the scroll moved nothing — that is the end of the list")

    def test_a_fresh_list_is_recognised_as_being_at_the_top(self):
        from vision.list_position import scrollbar_thumb, bar_position
        at_top, _at_end = bar_position(scrollbar_thumb(self.frames[0], self.vps[0]),
                                       self.vps[0])
        self.assertTrue(at_top, "frame 1 was captured right after a reset")

    def test_an_unreadable_screen_is_not_the_end_of_the_list(self):
        """Screens 2 and 4 parsed EMPTY before the parser fix. Neither is the list's end:
        the scrollbar shows content still below on 2, and the frames still advance."""
        from vision.list_position import scrollbar_thumb, bar_position
        _at_top, at_end = bar_position(scrollbar_thumb(self.frames[1], self.vps[1]),
                                       self.vps[1])
        self.assertFalse(at_end, "screen 2 has more below it — it cannot end the sweep")


if __name__ == "__main__":
    unittest.main()
