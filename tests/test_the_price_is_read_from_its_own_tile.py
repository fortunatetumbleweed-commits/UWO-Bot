"""A price the whole-frame parse missed is re-read from the tile — or left unread.

Live 2026-09-06 at Lisboa, and it cost the mission its cargo. The Birch Tree tile reads

    99%   13,322 (10,330)

and the whole-frame parse proposed ONE token for that row: '990' — the 99% badge with its '%'
read as '0' — and no price at all. `select_sellable` skipped the good ("we hold 3668 but its
price is unreadable, and this pass sells on profit") and the mission reported DONE holding the
3,668 units it had sailed there to sell. The three neighbouring tiles, whose prices are four
digits, parsed perfectly from the same frame.

This is the failure CLAUDE.md already documents — "the `44` on Matchlock Gun's thumbnail was
NEVER PROPOSED AS TEXT ... the same detector on a 750x520 CROP read the `44` without trouble"
— and `fill_missing_quantities` is the same remedy for badges.
"""

from __future__ import annotations

import types
import unittest

from vision.market_reader import _PRICE_PAIR, fill_missing_prices


def _good(name, owned, profit=None):
    return types.SimpleNamespace(name=name, owned_qty=owned, profit_per_unit=profit,
                                 price=None, tap_x=570, tap_y=556)


class _Frame:
    """Just enough of a PIL image for the crop-and-upscale path."""
    height, width = 1080, 2400

    def crop(self, _box):
        return self

    def resize(self, _size):
        return self


class TheStrictParseIsTheSafety(unittest.TestCase):
    """A wrong number is worse than a missing one, and here it is worth 10x."""

    def test_a_clean_bar_is_accepted(self):
        m = _PRICE_PAIR.match("13,322 (10,330) F")
        self.assertEqual((m.group(1), m.group(2)), ("13,322", "10,330"))

    def test_a_bar_WITH_A_BROKEN_GROUP_is_rejected(self):
        """'13 1 322' is what x4 produced for 13,322. A pattern happy to start matching in
        the middle would read 322 — off by a factor of 41."""
        self.assertIsNone(_PRICE_PAIR.match("13 1 322 (10,330)"))

    def test_it_does_not_match_from_the_middle(self):
        self.assertIsNone(_PRICE_PAIR.match("junk 640 (445)"))


class TheTileFillsTheGap(unittest.TestCase):

    def _fill(self, goods, reads):
        seen = iter(reads)
        return fill_missing_prices(_Frame(), goods,
                                   read_text_fn=lambda _img: next(seen, ""))

    def test_the_price_and_profit_come_off_the_tile(self):
        g = _good("Birch Tree", 3668)
        self._fill([g], ["13,322 (10,330) F"])
        self.assertEqual((g.price, g.profit_per_unit), (13322, 10330))

    def test_a_noisy_scale_is_skipped_and_a_later_one_wins(self):
        """Noise is not monotonic in scale — the same bar read cleanly at x8, broken at x6."""
        g = _good("Birch Tree", 3668)
        self._fill([g], ["13 1 322 (10,330)", "13,322 (10,330)"])
        self.assertEqual(g.price, 13322)

    def test_a_bar_THAT_NEVER_READS_CLEANLY_stays_unread(self):
        """Unreadable is not zero, and a guessed price is worse than none."""
        g = _good("Birch Tree", 3668)
        self._fill([g], ["13 1 322 (10,330)"] * 4)
        self.assertIsNone(g.profit_per_unit)

    def test_a_good_the_page_ALREADY_priced_is_not_re_read(self):
        g = _good("Iron", 108, profit=1097)
        self._fill([g], ["999 (999)"])
        self.assertEqual(g.profit_per_unit, 1097)

    def test_a_good_NOT_ABOARD_is_not_priced(self):
        """No sale to price — and the Sell grid only shows what we hold."""
        g = _good("Ebony", 0)
        self._fill([g], ["640 (445)"])
        self.assertIsNone(g.profit_per_unit)


if __name__ == "__main__":
    unittest.main()
