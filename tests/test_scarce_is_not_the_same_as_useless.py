"""A scarce port is refused only when it cannot cover a round's worth.

Live 2026-09-10 the San Village mission refused before sailing:

    [plan] every known source is scarce this season for ['Pig'] — not profitable now

Faro and Gijón both read `low` for Pig, which was true. But Gijón had supplied three barter
rounds' worth the night before, and the planner had no way to know: a season record held
`{state, seen_at}` and no quantity, so it knew a port was scarce and nothing more.

A port stocks the same amount all season and a refresh returns that same amount again, so a
shelf covering a seventh of the need is at most seven gems from covering it entirely. That
is the test `market_buy` already applies at the shelf; this is the same test applied before
setting out, so the two layers cannot disagree.
"""

from __future__ import annotations

import unittest

from brain.gathering_solver import plan_gathering

PORTS = {"Faro": (4430, 1820), "Gijon": (4200, 1500)}
SOURCES = {"Pig": ["Faro", "Gijón"]}


def _plan(shelves, want=1254, seasons=None):
    seasons = seasons if seasons is not None else {"Faro": "low", "Gijón": "low"}
    return plan_gathering(
        ["Pig"], SOURCES, PORTS, start=(4430, 1820), quantities={"Pig": want},
        season_fn=lambda p, m: seasons.get(p),
        shelf_fn=lambda p, m: shelves.get(p))


class ScarceButSufficientIsWorthSailingTo(unittest.TestCase):

    def test_the_run_that_was_wrongly_refused(self):
        gp = _plan({"Faro": 457, "Gijón": 400})
        self.assertEqual(set(), gp.low_everywhere,
                         "both clear a seventh of 1,254 — three gems each, not futile")

    def test_one_sufficient_port_is_enough(self):
        gp = _plan({"Faro": 20, "Gijón": 400})
        self.assertEqual(set(), gp.low_everywhere)

    def test_all_of_them_too_thin_still_refuses(self):
        gp = _plan({"Faro": 20, "Gijón": 30})
        self.assertEqual({"Pig"}, gp.low_everywhere,
                         "sixty gems is not a plan — the rule's real job is untouched")

    def test_the_boundary_is_a_seventh(self):
        want = 1254
        self.assertEqual(set(), _plan({"Faro": want // 7 + 1, "Gijón": 0}).low_everywhere)
        self.assertEqual({"Pig"}, _plan({"Faro": want // 7 - 1, "Gijón": 0}).low_everywhere)


class WhatIsNotKnownIsNotAssumed(unittest.TestCase):

    def test_no_quantity_recorded_behaves_exactly_as_before(self):
        """Conservative on purpose: an unmeasured port must not be talked up."""
        self.assertEqual({"Pig"}, _plan({}).low_everywhere)

    def test_a_port_that_is_not_scarce_is_never_in_the_set(self):
        gp = _plan({}, seasons={"Faro": "low", "Gijón": None})
        self.assertEqual(set(), gp.low_everywhere,
                         "unread or ordinary is not scarce — the 2026-09-09 fix, still held")


class TheQuantityIsRecordedAndReadBack(unittest.TestCase):

    def setUp(self):
        import tempfile
        from pathlib import Path
        import memory.market_kb as kb
        self._dir, self._tmp = kb._MARKETS_DIR, tempfile.mkdtemp()
        kb._MARKETS_DIR = Path(self._tmp)
        self.addCleanup(setattr, kb, "_MARKETS_DIR", self._dir)

    def test_a_shelf_survives_the_round_trip(self):
        from memory.market_kb import note_season, season_of, shelf_of
        note_season("Gijón", "Pig", "low", shelf=400)
        self.assertEqual("low", season_of("Gijón", "Pig"))
        self.assertEqual(400, shelf_of("Gijón", "Pig"))

    def test_a_season_recorded_without_one_reads_back_as_unknown(self):
        from memory.market_kb import note_season, shelf_of
        note_season("Faro", "Pig", "low")
        self.assertIsNone(shelf_of("Faro", "Pig"))

    def test_an_ordinary_tile_clears_both(self):
        from memory.market_kb import note_season, season_of, shelf_of
        note_season("Gijón", "Pig", "low", shelf=400)
        note_season("Gijón", "Pig", None)
        self.assertIsNone(season_of("Gijón", "Pig"))
        self.assertIsNone(shelf_of("Gijón", "Pig"))


if __name__ == "__main__":
    unittest.main()
