"""The scrollbar must be able to say "at the top" when the list IS at the top.

Measured on Cheyenne's trade list: the track is not symmetric around the viewport — at the
very top the thumb sits 23px inside it, at the very bottom only 9px. A fixed tolerance
between those two answers "am I at the top?" with a permanent NO, and a rewind loop then
swipes to its limit against a list that arrived after two swipes.
"""

from __future__ import annotations

import unittest

from vision.list_position import bar_position

VP = (383, 966)
TOP = (394, 610)      # right after a fresh open — genuinely the top
MID = (517, 733)
END = (753, 970)      # thumb flush against the bottom stop


class WhereIsTheList(unittest.TestCase):

    def test_the_top_is_recognised_as_the_top(self):
        at_top, at_end = bar_position(TOP, VP)
        self.assertTrue(at_top, "a list at the top was not recognised — the rewind cannot end")
        self.assertFalse(at_end)

    def test_the_end_is_recognised_as_the_end(self):
        at_top, at_end = bar_position(END, (382, 967))
        self.assertTrue(at_end)
        self.assertFalse(at_top)

    def test_the_middle_is_neither(self):
        self.assertEqual(bar_position(MID, VP), (False, False))

    def test_no_scrollbar_claims_nothing(self):
        self.assertEqual(bar_position(None, VP), (False, False))
