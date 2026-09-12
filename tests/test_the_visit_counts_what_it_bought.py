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
and does not care what the tile behind it reads. IT IS A TABLE, and it is read as one:

    y120   Trade Goods    Purchase Cost   Tax 5%    Discount    Purchase Price
    y170   Raisin
    y236   Luxuries
    y247   410            <- the units, a cell of its own under the same header

Note which card carries what: CONFIRM has the quantity, RESULT has none — it reports money
(`Purchase Cost 254,364 / Tax 42,911 / Total Amount 248,677`). Confirm says how many, result
says it happened, and neither does both. CLAUDE.md's note that "the per-good quantity lives on
the tile, not in the dialog" is right about the result card and wrong about the confirm card.
"""
from __future__ import annotations

import types
import unittest

from brain.market_context import confirm_card_purchase
from brain.market_ledger import MarketLedger
from brain.market_state import MarketState


def _el(label, x1, y1, x2, y2):
    return types.SimpleNamespace(label=label, element_type="text",
                                 x1=x1, y1=y1, x2=x2, y2=y2)


# ── The Bordeaux confirm card, frame_0073, EXACTLY as OmniParser returned it ──────────
#
# NOT A TRANSCRIPTION. The first version of this test used
# `Trade Goods: Candle (Sundries) x495`, a string taken from the obstruction consult's
# `full_text` — which is Claude's RENDERING of the card, not the parse. The reader matched
# that fixture and never a live frame: at Bordeaux on 2026-09-12 every purchase fell through
# to "no confirm card credited it" and went to the Sell grid exactly as before, while these
# tests passed. A model's description of a screen is not a reading of it.
CONFIRM = [
    _el("Confirm Purchase", 1065, 53, 1337, 97),
    # the column headers
    _el("Trade Goods", 630, 120, 806, 160),
    _el("Purchase Cost", 1048, 120, 1242, 160),
    _el("Tax 5%", 1312, 120, 1416, 160),
    _el("Discount", 1562, 120, 1684, 160),
    _el("Purchase Price", 1760, 120, 1962, 160),
    # the first column: the good, its category, its units — three stacked cells
    _el("Raisin", 404, 170, 959, 226),
    _el("Luxuries", 518, 236, 634, 270),
    _el("410", 469, 247, 507, 285),
    # the money row
    _el("282,900", 1086, 208, 1197, 248),
    _el("11,070", 1332, 211, 1426, 251),
    _el("237,390", 1806, 212, 1914, 252),
    _el("Ok", 1204, 962, 1423, 1010),
    _el("Cancel", 985, 963, 1200, 1011),
]

# ── The Faro card, frame_0035, the SAME table with the cells grouped differently ──────
#
# Read minutes after the Bordeaux one. How OmniParser groups the cells is NOT stable, and a
# reader that demands a whole cell be a number fails here: the quantity, a stray bracket and
# the category came back welded into `489] Livestock`. The money on this card — 155,991 and
# 133,986 — is larger and HIGHER than the 489, so nothing about size or order can separate
# them; only the column can, which is what the header anchor is for.
CONFIRM_MERGED_CELLS = [
    _el("Confirm Purchase", 1065, 53, 1337, 97),
    _el("Trade Goods", 629, 117, 807, 157),
    _el("Purchase Cost", 1048, 120, 1242, 160),
    _el("Tax 6%", 1312, 120, 1416, 160),
    _el("Discount", 1562, 120, 1684, 160),
    _el("Purchase Price", 1760, 120, 1962, 160),
    _el("Pig", 517, 187, 573, 225),
    _el("489] Livestock", 467, 235, 648, 275),
    _el("155,991", 1086, 211, 1194, 251),
    _el("7,335", 1342, 212, 1418, 252),
    _el("-29,3400", 1553, 207, 1685, 247),
    _el("133,986", 1806, 212, 1914, 252),
]

# The result card, also from a live run: money only, never a quantity.
RESULT = [_el("Result", 1100, 200, 1300, 240),
          _el("Purchase Cost 254,364", 900, 300, 1500, 340),
          _el("Total Amount 248,677", 900, 400, 1500, 440),
          _el("Ok", 1204, 900, 1423, 950)]


class TheConfirmCardNamesTheGoodAndTheUnits(unittest.TestCase):

    def test_the_live_card(self):
        """Read from the TABLE: the good and its units are cells under the Trade Goods
        header, not a phrase in one label."""
        self.assertEqual(confirm_card_purchase(None, elements=CONFIRM, dialog=False),
                         ("Raisin", 410))

    def test_the_category_cell_is_not_mistaken_for_the_good(self):
        """`Luxuries` sits between the good and its units in the same column."""
        good, _qty = confirm_card_purchase(None, elements=CONFIRM, dialog=False)
        self.assertEqual(good, "Raisin")

    def test_the_money_columns_are_not_mistaken_for_the_units(self):
        """282,900 and 237,390 are larger and nearer the top than the 410 — and they are in
        OTHER columns, which is what the header anchor is for."""
        _good, qty = confirm_card_purchase(None, elements=CONFIRM, dialog=False)
        self.assertEqual(qty, 410)

    def test_THE_CARD_MAY_SIT_ANYWHERE(self):
        """Every position is relative to the `Trade Goods` header, so a shifted card reads
        the same — the cutout offset moves this card like everything else."""
        shifted = [_el(e.label, e.x1 + 120, e.y1 + 90, e.x2 + 120, e.y2 + 90) for e in CONFIRM]
        self.assertEqual(confirm_card_purchase(None, elements=shifted, dialog=False),
                         ("Raisin", 410))

    def test_THE_CELLS_MAY_BE_WELDED_TOGETHER(self):
        """`489] Livestock` — the quantity, a stray bracket and the category in one cell. The
        quantity is the first number anywhere in the column below the good, not a cell that
        happens to be purely numeric."""
        self.assertEqual(
            confirm_card_purchase(None, elements=CONFIRM_MERGED_CELLS, dialog=False),
            ("Pig", 489))

    def test_the_money_is_excluded_by_the_COLUMN_not_by_size(self):
        """On that card 155,991 and 133,986 are both larger and higher up than the 489."""
        _good, qty = confirm_card_purchase(None, elements=CONFIRM_MERGED_CELLS, dialog=False)
        self.assertEqual(qty, 489)

    def test_the_merged_form_still_works(self):
        """The parse sometimes groups the column into one label; that stays supported as a
        FALLBACK, not as the rule."""
        merged = [_el("Trade Goods: Candle (Sundries) x495", 630, 120, 1200, 160)]
        self.assertEqual(confirm_card_purchase(None, elements=merged, dialog=False),
                         ("Candle", 495))

    def test_THE_RESULT_CARD_CARRIES_NO_QUANTITY(self):
        """It reports money. Trusting it alone to credit a ledger cannot work, which is why
        the confirm card is read and carried rather than the result card parsed."""
        self.assertIsNone(confirm_card_purchase(None, elements=RESULT, dialog=False))

    def test_a_page_full_of_goods_is_not_a_confirm_card(self):
        self.assertIsNone(confirm_card_purchase(
            None, elements=[_el("Iron", 300, 400, 500, 440),
                            _el("Candle", 300, 500, 500, 540),
                            _el("Purchase", 300, 600, 500, 640)], dialog=False))


class ASaleIsNotAPurchase(unittest.TestCase):
    """The same card, the same table, the opposite direction — and the card cannot say which.

    Live 2026-09-12 at Faro during `trim_before_gather`: a SALES confirmation for 365 Raisin
    was read as a purchase of 365 Raisin. It did no damage only because the ledger happened to
    be None during a trim; with a live ledger the count moves by twice the amount, the wrong
    way. The GOAL knows the direction and the handler already holds it.
    """

    def test_the_reader_itself_cannot_tell_a_sale_from_a_buy(self):
        """Stated as a test because it is WHY the goal gate exists, not a defect in the
        reader: a sell card carries the same Trade Goods table."""
        self.assertEqual(confirm_card_purchase(None, elements=CONFIRM, dialog=False),
                         ("Raisin", 410))

    def test_only_a_Hold_goal_may_credit_a_purchase(self):
        import inspect

        from brain.activities import market

        src = inspect.getsource(market.MarketActivity._on_our_dialog)
        self.assertIn("isinstance(goal, Hold)", src,
                      "a sell confirm must not be carried as a purchase")


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
