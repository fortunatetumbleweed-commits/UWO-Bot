"""Selling, one action per tick — and FC-4 made unreachable.

Live 2026-09-05 at London, the mission's payoff: 4,461 Bambara Groundnut at 39,642
profit/unit, about 177M ducats.

    tap (1450,314)   "load-to-sell Bambara Groundnut"    <- swallowed
    sold at London: nothing
    blocked: 'no Sell button after loading basket'       <- the leg failed here

The aim was right: (1451,316) staged and sold Argan Oil on the same screen an hour earlier.
The game drops about one tap in twenty. And the disproof was on the frame the loop already
held — the panel read "Select the goods you'd like to sell." The loop did not look; it went
hunting for a Sell button that only exists once something IS staged.
"""

from __future__ import annotations

import types
import unittest

from brain.activities.market_sell import on_sell_page, page_signature
from brain.market_state import MarketState


def _good(name, qty=100, profit=500):
    return types.SimpleNamespace(name=name, owned_qty=qty, profit_per_unit=profit,
                                 tap_x=1450, tap_y=314)


class _Sell:
    """Drives `on_sell_page` with a scripted page and commit button."""

    def __init__(self, goods, commit=None, cart_empty=True):
        self.goods, self.commit = goods, commit
        # What the right-hand panel says. "Select the goods you'd like to sell" is the ONLY
        # safe licence to tap a tile again — a second tap on a STAGED tile un-stages it.
        self.cart_empty = cart_empty
        self.taps = []

    def run(self, state, goal):
        import brain.activities.market_sell as MS
        from unittest import mock
        with mock.patch("actions.sell_goods._sell_page", lambda f: self.goods), \
             mock.patch("actions.sell_goods._find_sell_commit",
                        lambda f, e: self.commit), \
             mock.patch.object(MS, "_scroll", lambda: self.taps.append("scroll")):
            panel = [types.SimpleNamespace(label="Select the goods")] if self.cart_empty else []
            return on_sell_page(state, goal, frame=object(), capture_fn=lambda: object(),
                                tap_fn=lambda x, y: self.taps.append((x, y)),
                                omni_fn=lambda f: panel)


SELL_ALL = types.SimpleNamespace(__class__=type("SellHold", (), {}), exclude=())


class _Goal:
    def __init__(self, exclude=()):
        self.exclude = exclude


class StagingIsVerifiedAgainstTheBasket(unittest.TestCase):

    def test_a_first_look_stages(self):
        s = MarketState()
        run = _Sell([_good("Bambara Groundnut")])
        out = run.run(s, _Goal())
        self.assertEqual(out["do"], "staged")
        self.assertEqual(run.taps, [(1450, 314)])

    def test_a_loaded_basket_commits(self):
        """The Sell button carrying a value IS the screen saying the basket is loaded —
        nothing is remembered about it."""
        s = MarketState()
        commit = types.SimpleNamespace(cx=1960, cy=997)
        out = _Sell([_good("Bambara Groundnut")], commit=commit).run(s, _Goal())
        self.assertEqual(out["do"], "committed")

    def test_a_swallowed_stage_is_retried_not_reported_as_failure(self):
        """THE FC-4 CASE. Same page, same goods, nothing staged: the tap was swallowed."""
        s = MarketState()
        goods = [_good("Bambara Groundnut")]
        run = _Sell(goods)
        run.run(s, _Goal())                      # tick 1 — stages
        out = run.run(s, _Goal())                # tick 2 — page unchanged
        self.assertEqual(out["do"], "staged", "a swallowed tap is retried, not a failure")
        self.assertEqual(len(run.taps), 2)

    def test_a_basket_that_may_be_staged_is_NOT_re_tapped(self):
        """THE DESTRUCTIVE RETRY, refused. No commit button has two causes: the tap was
        swallowed, or the detector missed a button that is there. Re-staging on that reading
        un-stages a correct basket — `purchase_goods` removed exactly this retry in August
        after it toggled a correctly filled cart."""
        s = MarketState()
        run = _Sell([_good("Bambara Groundnut")], cart_empty=False)
        run.run(s, _Goal())                      # tick 1 — stages
        out = run.run(s, _Goal())                # tick 2 — page same, cart NOT read as empty
        self.assertEqual(out["do"], "waited")
        self.assertEqual(len(run.taps), 1, "the second tick must not tap at all")

    def test_a_tile_that_never_takes_a_tap_is_reported(self):
        """Bounded: the third identical page says the control is not responding."""
        s = MarketState()
        run = _Sell([_good("Bambara Groundnut")])
        for _ in range(3):
            out = run.run(s, _Goal())
        self.assertEqual(out["do"], "blocked")
        self.assertIn("not taking taps", out["why"])

    def test_a_page_that_CHANGED_is_not_a_swallowed_tap(self):
        """Staging worked and the grid re-flowed — the count must not accumulate."""
        s = MarketState()
        _Sell([_good("Pig", qty=100)]).run(s, _Goal())
        out = _Sell([_good("Pig", qty=50)]).run(s, _Goal())
        self.assertEqual(out["do"], "staged")
        self.assertEqual(s.attempts.get("stage", 0), 0, "a changed page clears the count")


class NothingSellableInViewIsNotAnEmptyHold(unittest.TestCase):

    def test_it_scrolls_before_believing_the_hold_is_clear(self):
        s = MarketState()
        run = _Sell([_good("Water")])
        out = run.run(s, _Goal(exclude=("Water",)))
        self.assertEqual(out["do"], "scrolled")
        self.assertEqual(run.taps, ["scroll"])

    def test_only_an_exhausted_scroll_finishes(self):
        s = MarketState()
        run = _Sell([_good("Water")])
        for _ in range(6):
            out = run.run(s, _Goal(exclude=("Water",)))
        self.assertEqual(out["do"], "finished")


class AnUnreadableGridIsNotAnEmptyOne(unittest.TestCase):
    def test_None_is_not_empty(self):
        out = _Sell(None).run(MarketState(), _Goal())
        self.assertEqual(out["do"], "blocked")
        self.assertIn("could not read", out["why"])


class TheSignatureIsTheEvidence(unittest.TestCase):
    def test_it_changes_when_the_hold_does(self):
        self.assertNotEqual(page_signature([_good("Pig", 100)]),
                            page_signature([_good("Pig", 50)]))

    def test_it_ignores_ordering(self):
        a, b = _good("Pig"), _good("Raisin")
        self.assertEqual(page_signature([a, b]), page_signature([b, a]))
