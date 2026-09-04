"""A material that has reached its target is no longer buyable.

LIVE 2026-08-30 at Faro, gathering for Bambara Groundnut (Pig 1260, Raisin 1432). Faro stocks
Pig and does NOT stock Raisin. The buyable filter asked only "is it on the grid" and "is the
tile in stock" — never "is this one already done" — so:

  * Raisin was absent from the grid and dropped out,
  * Pig passed its goal at 1,344/1,260 but stayed in,
  * and the loop exits only when EVERY good is met.

It bought Pig again and again — 1,568, then 1,792 — a blue gem per restock, filling the hold
Raisin needed, chasing a material this port would never have. The log said
`not met — short: Raisin 0/1432` and the next line was `load Pig`.

`_goal_met` was always per-good. The BUY FILTER was not.
"""
import unittest

from actions.buy_materials import buyable_now, enough_already


class _Good:
    """A grid tile. `tile_in_stock` reads available_qty / sold_out."""
    def __init__(self, name, available_qty=224, sold_out=False, conditional=False):
        self.name, self.available_qty = name, available_qty
        self.sold_out, self.conditional = sold_out, conditional


class _Ledger:
    """Per-good beliefs, the way MarketLedger holds them."""
    def __init__(self, believed=None, unknown=()):
        self._b, self._u = believed or {}, set(unknown)

    def believed(self, m):        return self._b.get(m, 0)
    def amount_unknown(self, m):  return m in self._u


GOAL  = {"Pig": 1260, "Raisin": 1432}
FARO  = {"pig": _Good("Pig")}          # Faro's grid: Pig only, Raisin nowhere on it


class TheFaroCase(unittest.TestCase):
    def test_a_met_good_drops_out_even_when_it_is_all_that_is_left(self):
        led = _Ledger({"Pig": 1344, "Raisin": 0})       # exactly the live numbers
        self.assertEqual(buyable_now(GOAL, FARO, led, track=True), [],
                         "Pig is done and Raisin is not sold here — there is nothing to buy")

    def test_it_still_buys_while_the_good_is_short(self):
        led = _Ledger({"Pig": 1120, "Raisin": 0})
        self.assertEqual(buyable_now(GOAL, FARO, led, track=True), ["Pig"])

    def test_a_good_absent_from_the_grid_is_never_buyable(self):
        led = _Ledger({"Pig": 0, "Raisin": 0})
        self.assertNotIn("Raisin", buyable_now(GOAL, FARO, led, track=True))

    def test_an_empty_shelf_is_not_buyable_it_is_refreshable(self):
        led = _Ledger({"Pig": 0})
        empty = {"pig": _Good("Pig", available_qty=0, sold_out=True)}
        self.assertEqual(buyable_now(GOAL, empty, led, track=True), [])


class UnknownIsNeverEnough(unittest.TestCase):
    def test_an_unreadable_amount_does_not_count_as_met(self):
        led = _Ledger({"Pig": 9999}, unknown=["Pig"])
        self.assertFalse(enough_already(led, True, "Pig", 1260),
                         '"I could not read it" must not become "I have plenty"')
        self.assertEqual(buyable_now(GOAL, FARO, led, track=True), ["Pig"])

    def test_the_untracked_path_is_unchanged(self):
        led = _Ledger({"Pig": 9999})
        self.assertFalse(enough_already(led, False, "Pig", 1260),
                         "no per-good reading on that path, so it cannot claim met")


if __name__ == "__main__":
    unittest.main()
