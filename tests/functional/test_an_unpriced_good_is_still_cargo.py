"""A good whose PRICE will not read is still a good, if we hold some of it.

LIVE 2026-08-30, Lisboa. The mission sailed there to sell 3,622 Birch Tree. The tile read
`Birch Tree / Wares / 3,622 / 100%` and its price line — `13,455 (10,463)` — was missing from
OmniParser's output for the WHOLE FRAME, though an ordinary crop reads it. The sell filter
used "has a price" to mean "is a good", so it dropped the cargo, and the mission finished
`{'sold': [], 'stopped_because': 'nothing to sell'}`. Roughly 48M ducats, reported as success.

The guard itself was right to exist: it stops another player's chat message ("Im glad") from
being tapped into the sell basket. It was the DISCRIMINATOR that was wrong. Chat text has a
name and nothing else; a good is on the Sell page because we hold it.
"""
import os
import unittest

from actions.sell_goods import select_sellable


class _Good:
    def __init__(self, name, owned_qty=None, sell_price=None, profit_per_unit=None):
        self.name, self.owned_qty = name, owned_qty
        self.sell_price, self.profit_per_unit = sell_price, profit_per_unit


BIRCH = dict(name="Birch Tree", owned_qty=3622)          # the price would not read
CHAT  = dict(name="Im glad")                             # another player, no price, no count


class TheOwnedCountSeparatesThem(unittest.TestCase):
    def test_the_cargo_is_sold_when_the_pass_clears_the_hold(self):
        out = select_sellable([_Good(**BIRCH), _Good(**CHAT)], goal="clear")
        self.assertEqual([g.name for g in out], ["Birch Tree"],
                         "we hold 3,622 of it; the clear does not need its price")

    def test_chat_text_is_never_sold(self):
        for goal in ("clear", "profit"):
            with self.subTest(goal=goal):
                out = select_sellable([_Good(**CHAT)], goal=goal)
                self.assertEqual(out, [], "no price AND no owned count — not a good")

    def test_a_profit_pass_will_not_guess_an_unreadable_price(self):
        out = select_sellable([_Good(**BIRCH)], goal="profit")
        self.assertEqual(out, [], "selling on profit needs the profit; it must not assume")

    def test_a_normal_priced_good_is_unaffected(self):
        g = _Good("Iron", owned_qty=108, sell_price=1392, profit_per_unit=930)
        self.assertEqual([x.name for x in select_sellable([g], goal="profit")], ["Iron"])


class AgainstTheRealFrame(unittest.TestCase):
    STAGE = "tests/stage_suite/frames/lisboa_birch_tree_no_price.png"

    def test_the_frame_that_left_the_cargo_aboard(self):
        if not os.path.exists(self.STAGE):
            self.skipTest("stage frame not available")
        from PIL import Image
        from vision.market_reader import read_market_page_omni
        goods = read_market_page_omni(Image.open(self.STAGE), tab="sell", claude_fallback=False)
        birch = next((g for g in goods if g.name == "Birch Tree"), None)
        self.assertIsNotNone(birch, "the reader does find the tile")
        self.assertEqual(birch.owned_qty, 3622, "and it reads the count, which is the point")
        self.assertIsNone(birch.sell_price, "the price is genuinely unreadable here")
        self.assertIn("Birch Tree", [g.name for g in select_sellable(goods, goal="clear")],
                      "so the clear must still sell it")


if __name__ == "__main__":
    unittest.main()
