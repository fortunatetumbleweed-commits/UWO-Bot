"""A market visit keeps its own per-good count, so a bought-out shelf costs no trip.

THE COST THIS REMOVES, measured on the Svear run of 2026-09-11: eleven visits to the Sell
grid, of which four were the legitimate first-look seed (one per port) and SEVEN were the
ledger being thrown away and re-read. About twelve minutes of a 107-minute run, and every one
of them followed buying out a shelf.

WHY THE TWO EXISTING CREDITS CANNOT COVER IT, and why this is the normal path rather than an
edge case:

  * `credit_the_shelf_drop` needs a reading of the tile AFTER the purchase. Buying the whole
    shelf greys the tile, `available_qty` comes back None, and `shelf_signature` records that
    as -1 — correctly, since unread is not zero. So the purchase that empties a shelf destroys
    the very measurement meant to price it, and gathering empties shelves by design.
  * `_credit_the_cargo_rise` reads the hold's total, which is an AGGREGATE and names no good.
    It refuses whenever more than one order material is stocked at the port — and it counts
    what the port STOCKS, not what was bought, so Barcelona carrying both Iron and Matchlock
    Gun blocks it even for an unambiguous single-good purchase (user, 2026-09-12).

The confirm card is independent of both. It says what the game is about to sell us, per good,
and does not care what the tile behind it reads.

    Confirm Purchase | Trade Goods: Candle (Sundries) x495 | Purchase Cost: 114,345 | ... | OK
    Result. Purchase Cost 254,364. Tax 42,911. Total Amount 248,677. Balance … OK

Note which card carries what: CONFIRM has the quantity, RESULT has none — it reports money.
Confirm says how many, result says it happened, and neither does both. CLAUDE.md's note that
"the per-good quantity lives on the tile, not in the dialog" is right about the result card
and wrong about the confirm card.
"""
from __future__ import annotations

import types
import unittest

from brain.market_context import confirm_card_purchase
from brain.market_ledger import MarketLedger
from brain.market_state import MarketState


def _el(label):
    return types.SimpleNamespace(label=label, element_type="text",
                                 x1=800, y1=400, x2=1600, y2=440)


# The cards exactly as they were read at Amsterdam and Tripoli on 2026-09-11.
CONFIRM = [_el("Confirm Purchase"),
           _el("Trade Goods: Candle (Sundries) x495"),
           _el("Purchase Cost: 114,345"), _el("Tax 6%: 5,445"), _el("Cancel"), _el("OK")]
RESULT = [_el("Result"), _el("Purchase Cost 254,364"), _el("Tax 42,911"),
          _el("Discount -48,598"), _el("Total Amount 248,677"), _el("OK")]


class TheConfirmCardNamesTheGoodAndTheUnits(unittest.TestCase):

    def test_the_live_card(self):
        self.assertEqual(confirm_card_purchase(None, elements=CONFIRM, dialog=False),
                         ("Candle", 495))

    def test_the_category_in_brackets_is_not_part_of_the_name(self):
        self.assertEqual(
            confirm_card_purchase(None, elements=[_el("Trade Goods: Iron (Metal) x517")],
                                  dialog=False), ("Iron", 517))

    def test_a_two_word_good(self):
        self.assertEqual(
            confirm_card_purchase(None, elements=[_el("Trade Goods: Matchlock Gun (Firearms) x1,205")],
                                  dialog=False), ("Matchlock Gun", 1205))

    def test_a_thousands_separator(self):
        self.assertEqual(
            confirm_card_purchase(None, elements=[_el("Trade Goods: Candle x1,047")],
                                  dialog=False), ("Candle", 1047))

    def test_THE_RESULT_CARD_CARRIES_NO_QUANTITY(self):
        """It reports money. Trusting it alone to credit a ledger cannot work, which is why
        the confirm card is read and carried rather than the result card parsed."""
        self.assertIsNone(confirm_card_purchase(None, elements=RESULT, dialog=False))

    def test_a_page_full_of_goods_is_not_a_confirm_card(self):
        self.assertIsNone(confirm_card_purchase(
            None, elements=[_el("Iron"), _el("Candle"), _el("Purchase")], dialog=False))


class TheCountSurvivesABoughtOutShelf(unittest.TestCase):
    """The ledger is already per-good — `fleet` and `pending` are dicts and its own docstring
    names the Barcelona case. What was missing is an input that works when the tile is gone."""

    def test_two_goods_in_one_visit_are_counted_separately(self):
        led = MarketLedger()
        led.seed({"iron": 517})
        led.bought("Iron", 517)
        led.bought("Matchlock Gun", 173)
        self.assertEqual(led.believed("Iron"), 1034)
        self.assertEqual(led.believed("Matchlock Gun"), 173)

    def test_a_card_credit_keeps_the_ledger_alive(self):
        """The flag the buy handler reads. It used to drop the ledger for EVERY good because
        ONE good's tile could not be read."""
        st = MarketState(ledger=MarketLedger())
        st.credited_by_card = True
        self.assertTrue(st.credited_by_card)
        self.assertIsNotNone(st.ledger)

    def test_the_in_flight_purchase_starts_empty(self):
        self.assertIsNone(MarketState(ledger=MarketLedger()).purchase_in_flight)


class TheBuyHandlerNoLongerThrowsTheCountAway(unittest.TestCase):

    def test_a_card_credit_counts_as_credited(self):
        """Read off the source: the awaiting-credit block must treat a card credit as a
        credit, or it falls through to dropping the ledger exactly as before."""
        import inspect

        from brain.activities import market_buy

        src = inspect.getsource(market_buy)
        self.assertIn("state.credited_by_card", src)
        drop = src.index("state.ledger = None")
        guard = src.index("if not credited and state.credited_by_card")
        self.assertLess(guard, drop, "the card credit must be considered BEFORE the drop")


if __name__ == "__main__":
    unittest.main()
