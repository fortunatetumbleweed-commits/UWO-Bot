"""The game filters on ITS spelling; we type ours, and accents make them differ.

Live 2026-09-06 hunting Gijon. The port is `Gijón`; our KB and the map OCR both flatten it to
`Gijon`, so the prefix `Gijo` matches nothing — character four is `ó`. The list came back with
ZERO rows, and an empty list looks exactly like no list:

    typing 'Gijo' (prefix of 'Gijon') — attempt 1/4, 0/2 landed
    'Gijon' is not on screen — opening the list          (x3)
    NOTHING CHANGED for 3 ticks ... choose port 'Gijon'

The frame shows the box reading `gijo` with nothing beneath it and the map visible through the
rail. Every "is the list open?" check answers no — they all look for the search FIELD, and the
parse returned only the 58px word we typed. So the screen cannot answer this; our own recorded
intent can: we typed, and nothing came back.

A shorter prefix is a SUPERSET — it cannot exclude the destination, only filter less — and an
accent can sit anywhere (`Málaga` fails at two, `Ávila` at one), so the remedy walks the
prefix down rather than guessing a safe length.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from brain.activities.world_map import (_MAX_SHORTENINGS, _PREFIX_LEN, ChooseDestination,
                                        WorldMapActivity)
from brain.dispatcher import WORKING

GOAL = ChooseDestination(where="Gijon", kind="port")


class AnEmptyFilterRetypesShorter(unittest.TestCase):

    def _activity(self, typed):
        act = WorldMapActivity(find_fn=lambda _w: None)      # never found on screen
        act._tick_frame = object()
        act._typed = typed
        act.typed_prefixes = []
        act._type_prefix = lambda pfx: act.typed_prefixes.append(pfx)
        act._open_list = lambda _g: act.typed_prefixes.append("OPENED THE LIST")
        return act

    def test_it_shortens_instead_of_re_opening_the_list(self):
        act = self._activity(typed=1)
        res = act._on_map(GOAL)
        self.assertEqual(res.status, WORKING)
        self.assertEqual(act.typed_prefixes, ["Gij"], "re-opening toggles the rail shut")

    def test_it_walks_the_prefix_down(self):
        act = self._activity(typed=1)
        for _ in range(_MAX_SHORTENINGS):
            act._on_map(GOAL)
            act._typed = 1                                    # each retype lands
        self.assertEqual(act.typed_prefixes, ["Gij", "Gi", "G"])

    def test_a_one_character_prefix_cannot_exclude_anything(self):
        """The floor is 1, never 0 — and 'G' matches 'Gijón' whatever its accents."""
        act = self._activity(typed=1)
        for _ in range(_MAX_SHORTENINGS):
            act._on_map(GOAL)
            act._typed = 1
        self.assertTrue(all(p for p in act.typed_prefixes))
        self.assertEqual(min(len(p) for p in act.typed_prefixes), 1)

    def test_once_the_shortenings_are_spent_it_opens_the_list_again(self):
        act = self._activity(typed=1)
        for _ in range(_MAX_SHORTENINGS + 1):
            act._on_map(GOAL)
            act._typed = 1
        self.assertEqual(act.typed_prefixes[-1], "OPENED THE LIST")

    def test_HAVING_NOT_TYPED_it_opens_the_list_as_before(self):
        """Nothing typed means nothing to blame on the query — the old path stands."""
        act = self._activity(typed=0)
        act._on_map(GOAL)
        self.assertEqual(act.typed_prefixes, ["OPENED THE LIST"])

    def test_the_first_prefix_is_still_the_full_length(self):
        self.assertEqual(_PREFIX_LEN, 4, "shortening starts from the normal prefix")


if __name__ == "__main__":
    unittest.main()
