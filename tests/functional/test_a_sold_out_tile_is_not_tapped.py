"""The whole CARD greys when a shelf empties — read that, not the artwork.

Live 2026-09-07 at Ambon, the Box of Nutmeg run. Ebony's tile was greyed, carried a blue
`Sold Out` stamp, and its badge read 0 (frame 131 of trace_barter_cmd_2026-09-07T23-05-09).
Claude's own consult on that frame said "Ebony is sold out", three separate times. The buy
loop tapped it twice anyway:

    [Ambon] load Ebony — tap tile @ (1452, 265)
    [market] the cart is still empty after staging — that tap did not register; staging again
    [Ambon] load Ebony — tap tile @ (1452, 265)
    gather:Ambon: staged ['Ebony'] 2x and the cart stayed empty — the tile is not taking taps
    status failed

"The cart stayed empty" has more than one cause, and the code enumerated one: a dropped tap.
The other is a good that cannot be staged at all.

`_tile_looks_sold_out` measured the ARTWORK — the good's own picture — asking for
`sat <= 0.10 and brightness <= 35`. No absolute threshold can hold there:

    Ebony, SOLD OUT      sat 0.28   brightness 20.8      <- failed on saturation
    Rosewood, in stock   sat 0.45   brightness 53.6

`tile_in_stock` then reads `available_qty is None or qty > 0`, and the badge was unreadable,
so an unknown quantity deferred to a `sold_out` flag that was never set.

The CARD BODY is chrome with a fixed palette — cream alive, grey dead — and it separates
with ~90 points of clear air, while keeping GATED tiles (T'nalak, Pituri) on the live side,
which the code must not confuse with sold out.
"""
import os
import unittest


class TheFrameThatFailedTheMission(unittest.TestCase):
    STAGE = "tests/stage_suite/frames/ambon_ebony_sold_out.png"

    def _goods(self):
        if not os.path.exists(self.STAGE):
            self.skipTest("stage frame not available")
        from PIL import Image
        from vision.market_reader import read_market_page_omni
        return {str(g.name): g for g in (read_market_page_omni(Image.open(self.STAGE)) or [])}

    def test_ebony_reads_sold_out(self):
        self.assertTrue(self._goods()["Ebony"].sold_out)

    def test_and_is_therefore_not_buyable(self):
        from actions.buy_materials import tile_in_stock
        self.assertFalse(tile_in_stock(self._goods()["Ebony"]))

    def test_the_stocked_tiles_are_untouched(self):
        from actions.buy_materials import tile_in_stock
        goods = self._goods()
        for name in ("Feather Crafts", "Mace", "Durian", "Rosewood"):
            with self.subTest(good=name):
                self.assertFalse(goods[name].sold_out)
                self.assertTrue(tile_in_stock(goods[name]))

    def test_the_GATED_tile_is_still_gated_not_sold_out(self):
        """A conditional good may also look dim, and calling it sold out sends the buy loop
        off to spend a blue gem that cannot help."""
        g = self._goods()["T'nalak"]
        self.assertTrue(getattr(g, "conditional", False))
        self.assertFalse(g.sold_out)


if __name__ == "__main__":
    unittest.main()
