"""A GATED good is not an empty shelf, and no blue gem will ever restock it.

Some port goods are conditional — purchasable only when a condition holds (user, 2026-08-24):

  * time-limited — a magenta band naming the window, e.g. `09/21/2026/00:00:00 -`
  * guild monopoly — available only to members of the city's monopolising guild
  * nation control — available only if your nation controls the area, and these tiles are
    ACTIVE, so no visual test catches them; they fail at purchase

Both gated kinds and a sold-out shelf read as "cannot buy", but the REMEDY differs
completely: a blue-gem refresh restocks an empty shelf and is wasted on a gated one, forever.

The marker is a coloured ribbon in the tile's TOP-LEFT corner. Measured on the Bordeaux
Purchase grid: Hungary Water's corner is 62% magenta, every other tile 0%.
"""

from __future__ import annotations

import types
import unittest

from PIL import Image

from actions.buy_materials import tile_in_stock
from vision.market_reader import _tile_has_condition_ribbon


class _Cell:
    def __init__(self, x1, y1):
        self.x1, self.y1 = x1, y1


def _frame(corner_rgb):
    img = Image.new("RGB", (2400, 1080), (60, 60, 60))
    img.paste(Image.new("RGB", (40, 40), corner_rgb), (100, 100))
    return img


class TheCornerRibbonMarksAGatedGood(unittest.TestCase):

    def test_a_magenta_ribbon_is_detected(self):
        self.assertTrue(_tile_has_condition_ribbon(_frame((214, 30, 190)), _Cell(100, 100)))

    def test_a_plain_corner_is_not(self):
        self.assertFalse(_tile_has_condition_ribbon(_frame((70, 70, 70)), _Cell(100, 100)))

    def test_a_merely_colourful_corner_is_not(self):
        """Trend arrows and category colours must not read as a condition ribbon."""
        for rgb in ((40, 160, 60), (200, 40, 40), (200, 190, 40)):
            with self.subTest(rgb=rgb):
                self.assertFalse(_tile_has_condition_ribbon(_frame(rgb), _Cell(100, 100)))


class GatedGoodsAreSkippedNotRefreshed(unittest.TestCase):

    def _g(self, **kw):
        base = dict(name="X", available_qty=None, sold_out=False, conditional=False)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_a_gated_good_is_not_buyable(self):
        self.assertFalse(tile_in_stock(self._g(conditional=True, available_qty=500)))

    def test_a_gated_good_does_not_trigger_a_refresh(self):
        """THE point: the gem must not be spent on a good that can never restock."""
        from actions.buy_materials import buy_to_goal
        import inspect
        src = inspect.getsource(buy_to_goal)
        self.assertIn("conditional", src,
                      "the refresh trigger must exclude gated goods")

    def test_a_genuinely_empty_shelf_still_does(self):
        self.assertFalse(tile_in_stock(self._g(sold_out=True)))


if __name__ == "__main__":
    unittest.main()
