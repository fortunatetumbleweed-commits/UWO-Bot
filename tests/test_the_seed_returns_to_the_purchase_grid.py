"""Reading the hold switches TABS, so the buy round must be handed back its own page.

`_read_owned_via_sell` goes to the Sell tab — that is where per-good quantities are legible —
and the buy round that follows reads whatever page is up: `read_market_page_omni(...,
tab="purchase")` labels what it finds "purchase" without checking.

Live 2026-09-06 at Faro. Frame 69 of trace_barter_cmd_2026-09-06T20-48-41 catches it exactly:
the Purchase page, Pig stamped Sold Out, and the restock control right there — `00:15:07 ↻ 11
gems`. The tap recorded on that same frame is (174,276), the rail's `+ Sell`. From then on the
"shop" was the hold (Madeira Wine, Keris, Sugar Cane), Pig and Raisin read as sold out, and:

    'Pig' is sold out and still wanted here — restocking
    no refresh for 'Pig' — no restock control (market fresh or not on Purchase grid)

the SECOND of those two causes, never the first. Two ports, two legs, ~1,300 units unbought.

`buy_to_goal` always switched back — "pre-check left us on the Sell tab → back to Purchase" —
and the port dropped the line.
"""

from __future__ import annotations

import unittest
from unittest import mock

from brain.activities.market import MarketActivity


class TheSeedHandsBackThePurchaseGrid(unittest.TestCase):

    def _seed(self, owned=None, boom=False):
        shown = []
        act = MarketActivity(context_fn=lambda _f: "purchase_page",
                             capture_fn=lambda: object(), tap_fn=lambda *a: None,
                             omni_fn=lambda _f: [],
                             show_grid_fn=lambda: shown.append("purchase"))
        read = mock.Mock(side_effect=RuntimeError("tab never opened")) if boom \
            else mock.Mock(return_value=owned or {})
        with mock.patch("actions.buy_materials._read_owned_via_sell", read):
            led = act._seed_ledger(object())
        return led, shown

    def test_it_returns_to_the_purchase_grid(self):
        _led, shown = self._seed({"pig": 457})
        self.assertEqual(shown, ["purchase"], "left the buy round on the Sell page")

    def test_it_returns_even_when_the_hold_read_finds_nothing(self):
        """An empty read still moved the screen."""
        _led, shown = self._seed({})
        self.assertEqual(shown, ["purchase"])

    def test_it_returns_even_when_the_hold_read_THREW(self):
        """A seed that failed has still switched tabs — hence `finally`."""
        _led, shown = self._seed(boom=True)
        self.assertEqual(shown, ["purchase"])

    def test_the_hold_is_still_seeded(self):
        """The seed is what stops a leg re-buying what it carries — it must survive."""
        led, _shown = self._seed({"pig": 457, "raisin": 110})
        self.assertEqual(led.believed("Pig"), 457)


if __name__ == "__main__":
    unittest.main()
