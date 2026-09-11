"""The barter panel's yield is written with a thousands separator.

San Village, 2026-09-04:

    Barter panel: amity=Friendly(98849, 100000) good=Bambara Groundnut out=1 ...

while the panel beside it read "Trade Quantity 1,122(+522)" and the confirm dialog agreed:
1,122 groundnut for 219 Grapes and 219 Pig. `_TRADE_QTY_RE` matched `\\d+`, which stops at
the comma, so the yield came back as its leading digit.

Every yield at or above 1,000 was affected, and those are exactly the ones worth bartering
for — Hutu ran at 1,036/round. The neighbouring patterns in the same file (`_PAIR_RE`,
`_AMITY_CHANGE_RE`) already allowed commas, which is what made the odd one out easy to miss.
"""
from __future__ import annotations

import unittest

from actions.barter_reader import _TRADE_QTY_RE, _to_int


def _qty(text):
    m = _TRADE_QTY_RE.search(text)
    return _to_int(m.group(1)) if m else None


class ThousandsSeparators(unittest.TestCase):

    def test_the_san_village_reading(self):
        self.assertEqual(_qty("Trade Quantity 1,122(+522)"), 1122)

    def test_a_four_figure_yield_is_not_truncated_to_its_first_digit(self):
        self.assertEqual(_qty("Trade Quantity 1,036"), 1036)
        self.assertEqual(_qty("Trade Quantity 12,345"), 12345)

    def test_three_figure_yields_still_read(self):
        """The Apache walkthrough's own numbers — these always worked, and must keep working."""
        self.assertEqual(_qty("Trade Quantity 815(+215)"), 815)
        self.assertEqual(_qty("Trade Quantity 709(+109)"), 709)
        self.assertEqual(_qty("Trade Quantity 744(+144)"), 744)

    def test_the_bonus_in_parentheses_is_not_the_quantity(self):
        """'(+522)' is the amity-tier bonus already included in the 1,122."""
        self.assertEqual(_qty("Trade Quantity 1,122(+522)"), 1122)

    def test_no_quantity_reads_as_None(self):
        self.assertIsNone(_qty("Total Amity Change+15,376"))
        self.assertIsNone(_qty("Bambara Groundnut"))


if __name__ == "__main__":
    unittest.main()
