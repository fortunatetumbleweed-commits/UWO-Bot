"""A clear-out taps Load All. A loop of tile taps is both slower and unsafe.

Live 2026-09-09 at Bordeaux, `sell_surplus` staged four goods in one tick:

    (571,315) (1007,316) (1451,316) (572,556)   — the sell grid, row by row

`Put In Bulk` was unchecked, so the FIRST tap opened a Trade Goods Info card covering
x 546-1860, and the other three landed inside it, on the description and the price chart.
Four taps, one intended. A loop that acts four times and looks none cannot see the world
change under its first action.

The card then went unanswered — `FreeHold` was not among the goals that own it, so the
market disowned a card its own tap had raised, and the dispatcher found only `Cancel` on it:
"a confirmation dialog nobody will answer". The trim leg died, 3,465 Birch Tree stayed
aboard, and the gather that followed hit a full hold.

The page has a button for exactly this (user, 2026-09-09: "it can tap Load All on 30, that
loads everything, that is used for regular trade runs").
"""

from __future__ import annotations

import unittest
import unittest.mock as mock


def _good(name, x, y, qty=100):
    """`name` is Mock's own kwarg, so a good needs a plain object rather than a Mock."""
    from types import SimpleNamespace
    return SimpleNamespace(name=name, tap_x=x, tap_y=y, owned_qty=qty, profit_per_unit=1)


class AClearOutUsesLoadAll(unittest.TestCase):

    def _run(self, goal, wanted, load_all_at=(1560, 1009)):
        import brain.activities.market_sell as ms
        taps = []
        state = mock.MagicMock(last_intent=None, scrolled_pages=0, sold=[],
                               award_claimed=True, sold_pending=[])
        with mock.patch("actions.sell_goods._find_sell_commit", lambda *a, **k: None), \
             mock.patch("actions.sell_goods._sell_page", lambda *a, **k: wanted), \
             mock.patch.object(ms, "selection_for", lambda *a, **k: wanted), \
             mock.patch.object(ms, "_load_all_button", lambda f: load_all_at), \
             mock.patch.object(ms, "_cargo_this_pass_declined", lambda *a, **k: []):
            out = ms.on_sell_page(state, goal, frame=object(), capture_fn=lambda: None,
                                  tap_fn=lambda x, y: taps.append((x, y)),
                                  omni_fn=lambda f: [])
        return out, taps

    def test_freehold_keeping_nothing_taps_load_all_once(self):
        from brain.activities.market import FreeHold
        wanted = [_good("Iron", 571, 315), _good("Birch Tree", 1007, 316),
                  _good("Matchlock Gun", 1451, 316), _good("Candle", 572, 556)]
        out, taps = self._run(FreeHold(keep=()), wanted)
        self.assertEqual([(1560, 1009)], taps,
                         "one tap on Load All, not one per tile")
        self.assertEqual("staged", out["do"])

    def test_a_goal_that_keeps_something_stages_instead(self):
        """Load All would load the kept goods too, so it is not the button for this."""
        from brain.activities.market import FreeHold
        wanted = [_good("Iron", 571, 315), _good("Birch Tree", 1007, 316)]
        _out, taps = self._run(FreeHold(keep=("Raisin",)), wanted)
        self.assertEqual([(571, 315)], taps)

    def test_no_load_all_button_falls_back_to_staging(self):
        from brain.activities.market import FreeHold
        wanted = [_good("Iron", 571, 315), _good("Birch Tree", 1007, 316)]
        _out, taps = self._run(FreeHold(keep=()), wanted, load_all_at=None)
        self.assertEqual([(571, 315)], taps, "one tile, then hand back")


class StagingIsOneTileAtATime(unittest.TestCase):
    """The burst is what let a modal swallow three taps."""

    def test_only_the_first_wanted_good_is_tapped(self):
        import brain.activities.market_sell as ms
        from brain.activities.market import SellHold
        wanted = [_good("Iron", 571, 315), _good("Birch Tree", 1007, 316),
                  _good("Matchlock Gun", 1451, 316)]
        taps = []
        state = mock.MagicMock(last_intent=None, scrolled_pages=0, sold=[],
                               award_claimed=True, sold_pending=[])
        with mock.patch("actions.sell_goods._find_sell_commit", lambda *a, **k: None), \
             mock.patch("actions.sell_goods._sell_page", lambda *a, **k: wanted), \
             mock.patch.object(ms, "selection_for", lambda *a, **k: wanted), \
             mock.patch.object(ms, "_cargo_this_pass_declined", lambda *a, **k: []):
            out = ms.on_sell_page(state, SellHold(), frame=object(),
                                  capture_fn=lambda: None,
                                  tap_fn=lambda x, y: taps.append((x, y)),
                                  omni_fn=lambda f: [])
        self.assertEqual(1, len(taps), "one action per tick, then look")
        self.assertEqual(["Iron"], out["goods"], "and only that one is pending")


class TheClearOutOwnsTheCardItOpens(unittest.TestCase):

    def test_freehold_is_among_the_goals_that_answer_the_goods_card(self):
        import inspect
        from brain.activities.market import MarketActivity
        src = inspect.getsource(MarketActivity.on_dialog)
        self.assertIn("FreeHold", src.split("_mine = {")[1].split("}")[0],
                      "a card the clear-out's own tap raised must not be disowned")

    def test_it_answers_with_max_then_load(self):
        """Nothing was specified, so the whole holding is the figure (user, 2026-09-09)."""
        import inspect
        from brain.activities.market import MarketActivity
        src = inspect.getsource(MarketActivity._on_goods_info)
        self.assertIn("(Hold, FreeHold)", src)


if __name__ == "__main__":
    unittest.main()
