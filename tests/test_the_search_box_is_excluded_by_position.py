"""The search box is excluded by POSITION — and only the box, not the port beside it.

The box holds what we typed, so it fuzzy-matches the destination BY CONSTRUCTION and outscores
the real label. Two copies of that rule existed and they disagreed: the reader's looked for
the search FIELD among the parsed elements, needing it to be wide (>=200px) or to say
"search" — so it found the box only when the box was EMPTY.

Live 2026-09-06 hunting Gijon. The box held 'gijo' and OmniParser returned just that word,
58px wide at (275,129)-(333,167), so the element lookup returned None:

    Matched 'Gijon': 'Gijon' conf=1.00 @ (1428,112)      <- the port, on the map
    Matched 'Gijon': 'gijo'  conf=1.00 @ (304,148)       <- the box, which won
    the 'Gijon' match sits in the search box — that is what we typed, not a result
    'Gijon' is not on screen — opening the list
    NOTHING CHANGED for 3 ticks ... choose port 'Gijon'

The backstop then discarded the WHOLE read, taking the real label with it — the port was on
the map at (1428,112) the entire time.
"""

from __future__ import annotations

import unittest
from unittest import mock

from actions.sail_actions import SEARCH_BOX_BAND, is_the_search_box

BOX = (420, 141)          # where the rail's box sits, and the observed fallback


class OnlyTheRailCanBeTheBox(unittest.TestCase):

    def _at(self, cx, cy, box=BOX):
        with mock.patch("actions.sail_actions._map_search_box", return_value=box):
            return is_the_search_box(object(), cx, cy)

    def test_our_own_typing_in_the_box_is_the_box(self):
        self.assertTrue(self._at(304, 148))

    def test_THE_PORT_BESIDE_IT_IS_NOT(self):
        """Gijon sat 29px from the box's y — well inside the band — so a y-only test excludes
        the very port being hunted. The x guard is load-bearing, not decoration."""
        self.assertTrue(abs(112 - BOX[1]) <= SEARCH_BOX_BAND, "same band, so x must decide")
        self.assertFalse(self._at(1428, 112))

    def test_a_row_below_the_box_is_not_the_box(self):
        """The list rows sit under it — Faro's was at y=200 against a box at y=144."""
        self.assertFalse(self._at(209, 200))

    def test_it_answers_even_when_the_field_cannot_be_picked_out(self):
        """`_map_search_box` falls back to the observed position, which is exactly the frame
        where this is needed — the one where the box holds our query."""
        with mock.patch("actions.sail_actions._map_search_box", side_effect=Exception("no")):
            self.assertFalse(is_the_search_box(object(), 304, 148))


# The reader itself is verified against the frame that stalled the run
# (trace_barter_cmd_2026-09-06T20-10-27 frame 76): `_find_port_on_world_map` returned the box
# at (304,148) before this change and the port at (1428,112) after. Its OCR is not injectable
# and the frames are not in the repo, so that check lives in the commit rather than here — a
# permanently skipped test asserts nothing.


if __name__ == "__main__":
    unittest.main()
