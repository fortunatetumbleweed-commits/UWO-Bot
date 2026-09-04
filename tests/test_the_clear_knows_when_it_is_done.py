"""A clear is done when the hold holds nothing but materials — counted, not glimpsed.

Live 2026-08-26 at Barcelona, the surplus clear read `omni grid 3x3: 9 goods` four times,
sold 36 goods, then found nothing sellable IN VIEW, logged `no goods grid detected` and left
— with the rest of the hold still aboard below the fold. The run then gathered into a hold it
believed was empty, and the buy beside it reported `owned=UNREADABLE ... 0/470` while `Iron
1,451` sat printed on the next page of the very same grid.

The user's test (2026-08-26): "the right panel has more tiles than the number of materials
plus food and water is a sign there are more surplus too."

That is an OBSERVATION — count the tiles — and it needs no scrolling to DETECT the problem,
only to fix it. "The visible page had nothing sellable" is a conclusion, and it is the one
that ended the clear early.
"""

from __future__ import annotations

import types
import unittest

from actions.sell_goods import barter_materials_exclude, surplus_remains


def _goods(*names):
    return [types.SimpleNamespace(name=n) for n in names]


KEEP = ["Water", "Food", "Iron", "Candle", "Matchlock Gun"]


class CountingTheTilesAnswersIt(unittest.TestCase):

    def test_only_kept_goods_means_done(self):
        self.assertFalse(surplus_remains(_goods("Iron", "Candle", "Water", "Food"), KEEP))

    def test_one_extra_tile_means_not_done(self):
        self.assertTrue(surplus_remains(_goods("Iron", "Water", "Sherry"), KEEP))

    def test_the_live_case(self):
        """What was actually still aboard when the clear declared itself finished."""
        left = _goods("Iron", "Water", "Food", "Graphite", "Fur", "Gobelin", "Satin")
        self.assertTrue(surplus_remains(left, KEEP))

    def test_an_empty_page_is_not_an_empty_hold(self):
        """The bug in one line: nothing VISIBLE is not nothing ABOARD. This function is asked
        about the whole hold, so an empty list here means the hold really is only kept goods —
        and it is the CALLER's job to have scrolled before asking."""
        self.assertFalse(surplus_remains([], KEEP))

    def test_case_and_spacing_do_not_matter(self):
        self.assertFalse(surplus_remains(_goods("  iron ", "WATER"), KEEP))


class TheKeepListIsTheRecipePlusSupplies(unittest.TestCase):

    def test_it_protects_water_and_food(self):
        keep = barter_materials_exclude("Birch Tree")
        self.assertIn("Water", keep)
        self.assertIn("Food", keep)

    def test_it_protects_the_recipe_materials(self):
        keep = [k.lower() for k in barter_materials_exclude("Birch Tree")]
        for material in ("iron", "candle", "matchlock gun"):
            with self.subTest(material=material):
                self.assertIn(material, keep)


if __name__ == "__main__":
    unittest.main()


class TheSellLoopScrollsBeforeBelievingItIsDone(unittest.TestCase):
    """The break that ended the clear early.

    `if not sellable: break` treated an empty VISIBLE page as an empty hold. Live 2026-08-26
    that stopped after four pages with the rest of the hold aboard, and the gather then ran
    into a hold it believed was clear.
    """

    def _sell(self, pages, keep=("Water", "Food", "Iron")):
        """`pages` is what the grid reads BEFORE each read; scrolling advances to the next."""
        from unittest.mock import patch
        from actions.sell_goods import sell_goods

        state = {"page": 0, "scrolls": 0}

        def read(_frame):
            return list(pages[min(state["page"], len(pages) - 1)])

        def scroll():
            state["scrolls"] += 1
            state["page"] += 1

        commit = types.SimpleNamespace(cx=2112, cy=997)
        # The Sell page is a PRECONDITION of this flow, not part of what these tests
        # exercise: they inject the page reader. `ensure_sell_tab` does the real
        # capture-and-verify, so say plainly that we are already on it.
        with patch("actions.buy_materials.ensure_sell_tab", return_value=True), \
             patch("actions.buy_materials.react_after_commit"), \
             patch("actions.sell_goods.time.sleep"), \
             patch("memory.observed_facts.forget"):
            res = sell_goods("Barcelona", goal="clear", keep=list(keep),
                             capture_fn=lambda: object(), tap_fn=lambda *a, **k: None,
                             omni_fn=lambda _f: [], read_profits_fn=read,
                             commit_fn=lambda *a: commit, scroll_fn=scroll, settle=0)
        return res, state

    def _good(self, name):
        return types.SimpleNamespace(name=name, tap_x=1, tap_y=1, profit_per_unit=10,
                                     is_loss=False, owned_qty=5)

    def test_it_scrolls_when_the_visible_page_has_nothing_sellable(self):
        pages = [[self._good("Iron")],                       # page 1: only a kept good
                 [self._good("Sherry"), self._good("Fur")]]  # page 2: more to sell
        res, state = self._sell(pages)
        self.assertGreaterEqual(state["scrolls"], 1, "an empty page must be scrolled past")
        self.assertIn("Sherry", res["sold"])

    def test_it_stops_when_a_scrolled_page_is_also_empty(self):
        """Only a page that yields nothing sellable AFTER scrolling is the end."""
        pages = [[self._good("Iron")], [self._good("Water")]]
        res, state = self._sell(pages)
        self.assertEqual(res["sold"], [])
        self.assertLessEqual(state["scrolls"], 2)

    def test_a_page_with_things_to_sell_is_not_scrolled_past(self):
        pages = [[self._good("Sherry")], [self._good("Iron")]]
        res, state = self._sell(pages)
        self.assertIn("Sherry", res["sold"])

    def test_the_scroll_is_bounded(self):
        """A hold deeper than the cap is a bigger problem than one visit can fix."""
        from actions.sell_goods import _MAX_SELL_SCROLLS
        pages = [[self._good("Iron")]] * 40
        _res, state = self._sell(pages)
        self.assertLessEqual(state["scrolls"], _MAX_SELL_SCROLLS)
