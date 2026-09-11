"""A scarce port is still worth working if it covers a seventh of what is left.

The rule this replaces abandoned a low-season port outright, on the argument that a refresh
there returns about a quarter the goods for the same gem. True, and not the whole story —
the numbers in that very comment make the case for the new rule:

    Faro     ~457 Pig    per refresh, against a need of 1,081   -> worth staying for
    Madeira  ~110 Raisin per refresh, against a need of 1,081   -> not

A seventh of 1,081 is 154. Faro clears it three times over; Madeira does not come close, and
ten refreshes there still left the barter capped at six rounds.

A seventh is one round's worth of a seven-round plan — the smallest amount that still buys
something the mission can use (user, 2026-09-09: "we only record it as low stock and abandon
buying if the stock is less than 1/7 of the need... But we continue to buy if it has more
than 1/7 of needed materials").

THE SEASON IS RECORDED EITHER WAY. `_note_seasons` writes it on sight, so the next plan can
prefer another port regardless of what this visit decides. The threshold governs only
whether to keep buying HERE, now.
"""

from __future__ import annotations

import unittest
import unittest.mock as mock


class ASeventhIsTheLine(unittest.TestCase):

    def _worth(self, seen, want, have=0):
        from brain.activities.market_buy import _a_port_worth_working
        state = mock.Mock(shelf_seen={"pig": seen}, ledger=object())
        with mock.patch("actions.buy_materials.material_states",
                        lambda ledger, orders: {"Pig": {"want": want, "have": have}}):
            return _a_port_worth_working(state, "Pig", {"Pig": want})

    def test_the_two_ports_that_set_the_rule(self):
        self.assertTrue(self._worth(457, 1081), "Faro — three rounds' worth a refresh")
        self.assertFalse(self._worth(110, 1081), "Madeira — ten refreshes, still six rounds")

    def test_the_boundary_is_a_seventh_exactly(self):
        self.assertTrue(self._worth(155, 1081))
        self.assertFalse(self._worth(154, 1081))

    def test_what_is_already_held_counts_against_the_need(self):
        """The need is what is OUTSTANDING, not the original order."""
        self.assertTrue(self._worth(50, 1081, have=800),
                        "281 left; 50 clears a seventh of that")
        self.assertFalse(self._worth(50, 1081, have=0))

    def test_a_met_material_is_never_worth_another_gem(self):
        self.assertFalse(self._worth(900, 1081, have=1081))

    def test_an_empty_shelf_is_never_worth_it(self):
        self.assertFalse(self._worth(0, 1081))


class TheShelfIsRememberedOnSight(unittest.TestCase):
    """By the time the decision is made the shelf reads 0, which says nothing."""

    def test_the_largest_reading_of_the_visit_is_kept(self):
        from brain.activities.market_buy import _note_shelf
        state = mock.Mock(shelf_seen={})
        good = lambda q: mock.Mock(available_qty=q)
        _note_shelf(state, {"Pig": 1081}, {"pig": good(457)})
        _note_shelf(state, {"Pig": 1081}, {"pig": good(0)})      # bought out
        self.assertEqual(457, state.shelf_seen["pig"],
                         "an emptied shelf must not erase what the port was seen to hold")

    def test_an_unreadable_quantity_is_skipped_not_counted_as_zero(self):
        from brain.activities.market_buy import _note_shelf
        state = mock.Mock(shelf_seen={"pig": 457})
        _note_shelf(state, {"Pig": 1081}, {"pig": mock.Mock(available_qty=None)})
        self.assertEqual(457, state.shelf_seen["pig"])


class TheSeasonIsRecordedWhicheverWayItGoes(unittest.TestCase):
    """Planning must be able to prefer another port even when this visit carries on."""

    def test_the_season_is_written_on_sight_regardless(self):
        import inspect
        from brain.activities import market_buy
        src = inspect.getsource(market_buy.on_purchase_page)
        self.assertIn("_note_seasons(port, orders, goods)", src)
        self.assertLess(src.index("_note_seasons"), src.index("_a_port_worth_working"),
                        "recorded before anything decides whether to stay")


if __name__ == "__main__":
    unittest.main()
