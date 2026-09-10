"""The owned-count badge must survive a comma and a short tile box.

Both defences failed at once on the Lisboa Sell page, and the ledger read a hold of 1,841
Almond as `have: 0` against a want of 870 — then bought to 2.1x its target.

Geometry MEASURED off frame_0205.png of `data/sessions/trace_barter_cmd_2026-09-10T16-49-19`,
where OmniParser returned the badge exactly right:

    button 'Almond'  x[359,787] y[195,369]      <- CLIPPED: the card runs to y 425
    text   '1,841'   x[414,486] y[298,330]      <- read correctly, and thrown away

Two independent rejections of a correct reading:

1. `re.fullmatch(r"\\d{1,4}", "1,841")` fails on the comma. The price branch three lines
   above has always applied `translate(_SEP)` for this (`the-separator-is-whatever-ocr-saw`);
   the owned-qty branch never did, and digits-only also capped the badge at 9,999.

2. The tile's y positions are scaled to the `_TILE_H` reference by dividing by the DETECTED
   box height. A clipped box is smaller, so the same offset scales HIGHER: 119px down a true
   230 pitch is 119 and lands in the (80,150) band; down the clipped 174 it is 157 and misses.

Neither alone explains the run: `1,867` at San carries a comma too and read correctly, because
its tile was full height and the targeted-crop fallback rescued it. A SINGLE good on the page
is what removes that safety net — the short-box compensation upstream takes the MEDIAN cell
height, and with one cell the clipped box is its own median.
"""

from __future__ import annotations

import types
import unittest

from vision.market_reader import _TILE_H, _parse_tile_from_button


def _el(label, x1, x2, y1, y2, kind="text"):
    return types.SimpleNamespace(label=label, element_type=kind,
                                 x1=x1, x2=x2, y1=y1, y2=y2,
                                 cx=(x1 + x2) // 2, cy=(y1 + y2) // 2)


def _almond_tile(*, tile_y2, badge):
    """The Lisboa card. `tile_y2` 369 is the clipped box, 425 the true extent."""
    button = _el("Almond", 359, 787, 195, tile_y2, kind="button")
    texts = [_el("Luxuries", 494, 615, 252, 289),
             _el(badge, 414, 486, 298, 330),
             _el("111(-1,053)", 449, 783, 375, 425)]
    return button, texts


class TheBadgeIsReadThroughBothFaults(unittest.TestCase):

    def test_A_COMMA_IN_A_CLIPPED_TILE(self):
        """The live case: both faults together."""
        button, texts = _almond_tile(tile_y2=369, badge="1,841")
        good = _parse_tile_from_button(button, texts, "sell", row_h=174)
        self.assertEqual(good.owned_qty, 1841)

    def test_the_comma_alone(self):
        button, texts = _almond_tile(tile_y2=425, badge="1,841")
        good = _parse_tile_from_button(button, texts, "sell", row_h=230)
        self.assertEqual(good.owned_qty, 1841)

    def test_the_clipped_tile_alone(self):
        """789 needs no separator, and read as 7 through the crop fallback live."""
        button, texts = _almond_tile(tile_y2=369, badge="789")
        good = _parse_tile_from_button(button, texts, "sell", row_h=174)
        self.assertEqual(good.owned_qty, 789)

    def test_A_FULL_STOP_IS_A_COMMA(self):
        """OCR returns the separator per glyph — '1.841' is the same number."""
        button, texts = _almond_tile(tile_y2=425, badge="1.841")
        good = _parse_tile_from_button(button, texts, "sell", row_h=230)
        self.assertEqual(good.owned_qty, 1841)

    def test_a_full_height_tile_is_unchanged(self):
        """The San case, which always worked — the fix must not disturb it."""
        button, texts = _almond_tile(tile_y2=430, badge="1,867")
        good = _parse_tile_from_button(button, texts, "sell", row_h=234)
        self.assertEqual(good.owned_qty, 1867)

    def test_a_tile_TALLER_than_the_reference_keeps_its_own_height(self):
        """Only SHORT boxes are the clipping; a taller layout is real and must be trusted."""
        tall = _TILE_H + 120
        button = _el("Almond", 359, 787, 195, 195 + tall, kind="button")
        # The badge sits at the same FRACTION of the tile, so it must still be found.
        cy = 195 + int(tall * 119 / 230)
        texts = [_el("1,841", 414, 486, cy - 16, cy + 16)]
        good = _parse_tile_from_button(button, texts, "sell", row_h=tall)
        self.assertEqual(good.owned_qty, 1841)

    def test_the_purchase_tab_is_untouched(self):
        """`owned_qty` is a SELL-tab reading; the purchase grid shows the shop's stock."""
        button, texts = _almond_tile(tile_y2=369, badge="1,841")
        good = _parse_tile_from_button(button, texts, "purchase", row_h=174)
        self.assertIsNone(good.owned_qty)


if __name__ == "__main__":
    unittest.main()
