"""When the owned count cannot be read, the SHELF still says what was bought.

Live 2026-09-04 at Faro (session trace_barter_cmd_2026-09-04T17-35-01). The Sell tab would
not confirm — "could not confirm the Sell tab — refusing to report owned" — so every round
of buy_to_goal logged the same blind line:

    [buy_to_goal] round 1/60: owned=UNREADABLE (+?) → 0/2520
    [buy_to_goal] not met — amount unknown for Pig — the sell grid must settle it
    [buy_to_goal] count unreadable but 'Pig' has gone empty — taking the refresh path

while the grid beside it said plainly what had happened:

    'Pig' owned was unreadable on the page; its tile reads 684
    [Faro] load Pig — tap tile @ (1007, 555)
    'Pig' owned was unreadable on the page; its tile reads 0

684 → 0 is 684 bought, and it does not depend on the panel that failed.

WHAT IT COST. The ledger recorded "amount unknown", so `buyable_now` could never mark Pig
met; Pig sells out in one purchase, so the sold-out exception fired every round and bypassed
the never-buy-blind stop; and the only remaining bound was the SUMMED runaway guard, 2,520
for a 1,260 Pig goal. Four shelves went aboard — 2,736 — and the 1,476 surplus filled the
hold to 4,847/4,952. Raisin then had 105 slots to land in, came home at 869 of 1,260, and
capped the barter at four rounds. Four blue-gem refreshes were spent on a Raisin shelf that
was never empty.
"""
from __future__ import annotations

import types
import unittest

from actions.buy_materials import _qty_of, _shelf_drop


def _grid(**qty):
    """A grid reading keyed the way the market reader keys it — lowercased names."""
    return {k.lower(): types.SimpleNamespace(available_qty=v) for k, v in qty.items()}


class TheShelfDropIsTheAmountBought(unittest.TestCase):

    def test_the_faro_round(self):
        """The exact numbers from the log."""
        self.assertEqual(_shelf_drop(_grid(Pig=684), _grid(Pig=0), "Pig"), 684)

    def test_two_shelves_reach_the_1260_goal(self):
        """684 + 684 = 1,368, which meets 1,260. The run bought FOUR."""
        bought = sum(_shelf_drop(_grid(Pig=684), _grid(Pig=0), "Pig") for _ in range(2))
        self.assertGreaterEqual(bought, 1260)

    def test_a_partial_buy_counts_what_left_the_shelf(self):
        self.assertEqual(_shelf_drop(_grid(Pig=684), _grid(Pig=200), "Pig"), 484)

    def test_a_shelf_that_did_not_move_is_zero(self):
        self.assertEqual(_shelf_drop(_grid(Pig=684), _grid(Pig=684), "Pig"), 0)


class ItNeverINVENTSAnAmount(unittest.TestCase):
    """0 keeps the ledger's "amount unknown" meaning. Guessing here would be worse than the
    blindness it replaces — a wrong number stops the loop believing a goal is met."""

    def test_an_unread_shelf_before_is_not_a_drop(self):
        self.assertEqual(_shelf_drop(_grid(Pig=None), _grid(Pig=0), "Pig"), 0)

    def test_an_unread_shelf_after_is_not_a_drop(self):
        self.assertEqual(_shelf_drop(_grid(Pig=684), _grid(Pig=None), "Pig"), 0)

    def test_a_good_absent_from_either_grid_is_not_a_drop(self):
        self.assertEqual(_shelf_drop(_grid(Pig=684), _grid(), "Pig"), 0)
        self.assertEqual(_shelf_drop(_grid(), _grid(Pig=0), "Pig"), 0)

    def test_a_REFRESHED_shelf_is_not_a_negative_buy(self):
        """After a blue gem the shelf goes 0 → 684. That is stock arriving, not goods
        leaving, and a negative purchase is impossible."""
        self.assertEqual(_shelf_drop(_grid(Pig=0), _grid(Pig=684), "Pig"), 0)

    def test_the_lookup_is_case_insensitive_like_the_reader(self):
        self.assertEqual(_qty_of(_grid(Pig=684), "PIG"), 684)
        self.assertEqual(_shelf_drop(_grid(Pig=684), _grid(Pig=0), "pig"), 684)

    def test_a_missing_quantity_reads_as_None_not_zero(self):
        self.assertIsNone(_qty_of(_grid(Pig=None), "Pig"))
        self.assertIsNone(_qty_of(_grid(), "Pig"))
