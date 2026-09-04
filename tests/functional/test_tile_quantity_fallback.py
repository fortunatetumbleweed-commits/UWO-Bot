"""A quantity the page read left silent is re-read from the good's OWN TILE.

Measured on real Barcelona sell pages, 2026-08-27, against truth read off the tiles and
user-confirmed (`Lemon Oil 1`, `Neroli 2`):

    read                     Lemon Oil (1)   Neroli (2)
    full frame               None (silent)   2  correct
    whole frame x2 LANCZOS   1    correct    8  WRONG
    whole frame x2 bicubic   31   WRONG      2  correct

Every whole-frame variant is wrong somewhere and the RESAMPLE FILTER CHANGES THE ANSWER, so
upscaling the whole frame to fill gaps could have written `31`. A wrong number is worse than
a missing one: nothing downstream can tell.

A tight crop of the one tile has the number alone in the image instead of one of forty in a
busy 2400x1080 frame.
"""

from __future__ import annotations

import types
import unittest

from vision.market_reader import fill_missing_quantities


def _good(name, owned, x=572, y=846):
    return types.SimpleNamespace(name=name, owned_qty=owned, tap_x=x, tap_y=y)


class _Frame:
    height = 1080
    def __init__(self, text=""):
        self._text = text
        self.crops = []
    def crop(self, box):
        self.crops.append(box)
        return self
    def resize(self, wh):
        self.scaled = wh
        return self
    width = 2400


class OnlyTheGapsArePaidFor(unittest.TestCase):

    def test_a_good_that_already_read_is_left_alone(self):
        f = _Frame("999")
        goods = [_good("Iron", 2099)]
        fill_missing_quantities(f, goods, read_text_fn=lambda _c: "999")
        self.assertEqual(goods[0].owned_qty, 2099)
        self.assertEqual(f.crops, [], "no crop, no OCR — it already had an answer")

    def test_a_silent_good_is_re_read_from_its_tile(self):
        f = _Frame()
        goods = [_good("Lemon Oil", None)]
        fill_missing_quantities(f, goods, read_text_fn=lambda _c: "Le Pe 1")
        self.assertEqual(goods[0].owned_qty, 1)

    def test_the_trailing_number_is_the_quantity(self):
        """The tight crop catches label fragments too — 'Irc Wz 2,099'."""
        goods = [_good("Iron", None)]
        fill_missing_quantities(_Frame(), goods, read_text_fn=lambda _c: "Irc Wz 2,099")
        self.assertEqual(goods[0].owned_qty, 2099)


class UnreadableStaysUnreadable(unittest.TestCase):
    """UNREADABLE IS NOT ZERO — the rule the whole evening turned on."""

    def test_a_tile_that_will_not_read_leaves_it_None(self):
        goods = [_good("Lemon Oil", None)]
        fill_missing_quantities(_Frame(), goods, read_text_fn=lambda _c: "Le Pe")
        self.assertIsNone(goods[0].owned_qty)

    def test_an_ocr_failure_leaves_it_None(self):
        def boom(_c):
            raise RuntimeError("ocr down")
        goods = [_good("Lemon Oil", None)]
        fill_missing_quantities(_Frame(), goods, read_text_fn=boom)
        self.assertIsNone(goods[0].owned_qty)

    def test_a_good_with_no_tile_position_is_skipped(self):
        goods = [types.SimpleNamespace(name="Ghost", owned_qty=None, tap_x=None, tap_y=None)]
        fill_missing_quantities(_Frame(), goods, read_text_fn=lambda _c: "5")
        self.assertIsNone(goods[0].owned_qty)


class AgainstTheRealFrames(unittest.TestCase):
    """The parse tests above use text I typed; these read the actual pixels."""

    STAGE = "tests/stage_suite/frames/sell_grid_iron_2099.png"

    def _page(self):
        import os
        if not os.path.exists(self.STAGE):
            self.skipTest("stage frame not available")
        from PIL import Image
        from vision.market_reader import read_market_page_omni
        im = Image.open(self.STAGE)
        return im, read_market_page_omni(im, tab="sell")

    def test_lemon_oil_is_recovered_and_the_rest_are_undisturbed(self):
        # THE PAGE READ FILLS ITS OWN GAPS. This test used to assert the opposite — that the
        # page read was silent on Lemon Oil and a caller had to run the fallback itself. That
        # contract is what broke the trim on 2026-08-29: `read_market_all_pages` ran the
        # fallback, `sell_down_to` did not, and Candle was skipped as unreadable eleven
        # seconds after the tile had given up 1182. If this assertion ever fails with None,
        # the fallback has been lifted back out of `read_market_page_omni` — put it back.
        im, goods = self._page()
        after = {g.name: g.owned_qty for g in goods}
        self.assertEqual(after["Lemon Oil"], 1, "recovered from its own tile by the page read")

        before = dict(after)
        fill_missing_quantities(im, goods)   # a second pass has nothing left to do
        after = {g.name: g.owned_qty for g in goods}
        for name, was in before.items():
            if was is not None:
                with self.subTest(good=name):
                    self.assertEqual(after[name], was, "values already read must not move")


if __name__ == "__main__":
    unittest.main()
