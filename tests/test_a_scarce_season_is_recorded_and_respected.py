"""The season is on the tile, so read it, keep it, and stop paying to rediscover it.

The refresh WORKS at a low-season port — it just returns a quarter of the goods for the same
gem. Live 2026-09-06: Faro ~457 Pig per refresh, Madeira ~110 Raisin. Ten refreshes and 45
minutes still left Raisin at 1,100 of 1,755, capping the barter at 6 rounds instead of 7 and
leaving 729 Pig unused. The answer to a scarce season is another port, not another gem.

The signal, measured on the Madeira grid (frame 158 of
trace_barter_cmd_2026-09-06T21-45-01), over a 60x52 band at the tile's top-right:

    Sugar Cane   red 0.447  green 0.000     low
    Shea Butter  red 0.000  green 0.469     abundant
    ordinary tiles           0.000/0.000

AND THE TRAP: Raisin, sold out with no ribbon at all, reads red 0.138 — the Sold Out stamp
and its red `0`. A sold-out shelf is not a scarce season, and they want opposite things: a
blue gem restocks the shelf and can do nothing about the season.
"""

from __future__ import annotations

import time
import types
import unittest
from unittest import mock

from brain.activities.market import Hold
from brain.activities.market_buy import _MAX_LOW_SEASON_GEMS, on_purchase_page
from brain.market_ledger import MarketLedger
from brain.market_state import MarketState
from memory import market_kb


class _RestockControl:
    """What `find_restock_button` returns — the seam the buy tick now uses directly."""
    cx, cy, timer, currency = 1399, 160, "00.23.59", "blue_gem"

class Tile:
    def __init__(self, name, qty, season=None, sold_out=False, active=True):
        self.name, self.available_qty, self.season = name, qty, season
        self.sold_out, self.is_active = sold_out, active


def _state():
    st = MarketState(ledger=MarketLedger())
    st.ledger.seed({"raisin": 110})
    return st


def _run(state, tiles, port="Madeira", refresh=None):
    goal = Hold(orders={"Raisin": 1755})
    with mock.patch("vision.market_reader.read_market_page_omni", return_value=tiles), \
         mock.patch("actions.buy_materials._find_purchase_commit", return_value=None), \
         mock.patch("vision.region_detectors.market_restock.find_restock_button",
                    return_value=(refresh if refresh is not None else _RestockControl())):
        return on_purchase_page(state, goal, port, frame=object(),
                                capture_fn=lambda: object(), tap_fn=lambda *a: None,
                                omni_fn=lambda _f: [])


class TheSeasonIsRecordedOnSight(unittest.TestCase):
    """Written when seen, not when it has already cost ten refreshes."""

    def setUp(self):
        self.noted = []
        self.patch = mock.patch("memory.market_kb.note_season",
                                side_effect=lambda p, g, s, **k: self.noted.append((p, g, s)))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_a_low_ribbon_is_written_down(self):
        _run(_state(), [Tile("Raisin", 110, season="low")])
        self.assertIn(("Madeira", "Raisin", "low"), self.noted)

    def test_an_ORDINARY_tile_clears_the_record(self):
        """A season that has turned must not keep steering the plan away."""
        _run(_state(), [Tile("Raisin", 900, season=None)])
        self.assertIn(("Madeira", "Raisin", None), self.noted)

    def test_a_SOLD_OUT_tile_is_not_read_for_a_season(self):
        """It reads reddish from the stamp — a different fact, with the opposite remedy."""
        _run(_state(), [Tile("Raisin", 0, season=None, sold_out=True, active=False)])
        self.assertEqual(self.noted, [])


class AScarceSeasonIsNotGroundThrough(unittest.TestCase):

    def _sold_out_low(self):
        return [Tile("Raisin", 0, sold_out=True, active=False)]

    def test_it_stops_paying_after_a_couple_of_gems(self):
        st = _state()
        with mock.patch("memory.market_kb.season_of", return_value="low"), \
             mock.patch("memory.market_kb.note_season"):
            for _ in range(_MAX_LOW_SEASON_GEMS):
                self.assertEqual(_run(st, self._sold_out_low())["do"], "refreshed")
            out = _run(st, self._sold_out_low())
        self.assertEqual(out["do"], "finished")
        self.assertEqual(out["season"], "low")

    def test_an_ordinary_season_keeps_refreshing(self):
        """Faro's Pig came 457 at a time — that shelf is worth every gem."""
        st = _state()
        with mock.patch("memory.market_kb.season_of", return_value=None), \
             mock.patch("memory.market_kb.note_season"):
            for _ in range(_MAX_LOW_SEASON_GEMS + 3):
                self.assertEqual(_run(st, self._sold_out_low())["do"], "refreshed")


class TheRecordExpires(unittest.TestCase):
    """A season that has turned makes the record a conclusion whose evidence is gone."""

    def test_it_is_dropped_past_its_ttl(self):
        with mock.patch("memory.market_kb.load_market",
                        return_value={"port": "Madeira",
                                      "seasons": {"raisin": {"state": "low",
                                                             "seen_at": time.time()}}}):
            self.assertEqual(market_kb.season_of("Madeira", "Raisin"), "low")
            self.assertIsNone(market_kb.season_of(
                "Madeira", "Raisin", now=time.time() + market_kb.SEASON_TTL_S + 1))

    def test_the_ttl_is_recorded_as_the_guess_it_is(self):
        self.assertEqual(market_kb.SEASON_TTL_S, 72 * 3600)


if __name__ == "__main__":
    unittest.main()
