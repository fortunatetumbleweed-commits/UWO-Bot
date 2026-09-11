"""While a tile is in the cart the counter carries the pending units, and it must still read.

    4,572(+280)/4,952      held, plus staged, of capacity

`parse_pair` returns None for that, because the bracket sits between the number and the
slash — and that is EVERY reading taken during a purchase, which is exactly when the ledger
needs one. `_credit_the_cargo_rise` got `cargo_before=None`, made no claim, the ledger was
dropped as stale, and the only way left to learn the hold was to switch to the Sell tab and
read the whole grid back.

Live 2026-09-10 at Bordeaux and Trabzon: seven purchases, seven "the shelf could not be read
across that purchase", and eight trips to the Sell tab — once per buy (user: "the bot is
checking by tapping the Sell panel after every buy... it should record it as market buy").

THE HELD FIGURE IS THE ONE TO TAKE. Pending units are not bought yet, and once the purchase
goes through the bracket disappears and the same number includes them — so the difference
across the buy is the purchase, which is what the ledger wants.

Same family as the separator fix beside it: the counter has a form, and the parser has to
know it.
"""

from __future__ import annotations

import re
import unittest

from utils.digits import parse_pair

_STRIP_PENDING = r"\([^)]*\)"


class TheStagedFormReads(unittest.TestCase):

    def _read(self, text):
        return parse_pair(re.sub(_STRIP_PENDING, "", text))

    def test_the_reading_that_broke_it(self):
        self.assertEqual((4572, 4952), self._read("4,572(+280)/4,952"))

    def test_the_plain_forms_are_unchanged(self):
        self.assertEqual((1875, 4952), self._read("1,875/4,952"))
        self.assertEqual((613, 4952), self._read("613/4,952"))

    def test_the_separator_fix_still_holds(self):
        """'3.129/4,952' — a period the reader saw instead of a comma, fixed 2026-09-05."""
        self.assertEqual((3129, 4952), self._read("3.129/4,952"))

    def test_a_larger_pending_part_does_not_confuse_it(self):
        self.assertEqual((4572, 4952), self._read("4,572(+1,280)/4,952"))

    def test_it_takes_the_held_figure_not_the_pending_one(self):
        used, _cap = self._read("100(+900)/4,952")
        self.assertEqual(100, used, "staged units are not in the hold yet")


class TheReaderUsesIt(unittest.TestCase):

    def test_the_cargo_reader_strips_the_pending_part(self):
        import inspect
        from actions.buy_materials import _read_cargo_used_cap
        src = inspect.getsource(_read_cargo_used_cap)
        self.assertIn(r'\([^)]*\)', src,
                      "the counter is read during purchases, when the bracket is there")


if __name__ == "__main__":
    unittest.main()
