"""Coverage alone treats every shelf as equal, and they are not.

A drained port still refreshes — the gem is spent, the tile goes active, the loop is happy —
it just pays about a quarter the yield. Live 2026-09-06: Faro returned ~457 Pig per blue-gem
refresh and Madeira ~110 Raisin for the same 11 gems, and Raisin still finished 655 short,
capping the barter at 6 rounds instead of 7.

So a material that is scarce at a port is worth less than one there, and the weight IS that
measured ratio (110/457 ~ 0.24) rather than a knob: a low-season port must cover about four
times as many materials to beat an ordinary one.
"""

from __future__ import annotations

import unittest

from brain.gathering_solver import _LOW_SEASON_WEIGHT, plan_gathering

COORDS = {"Faro": (0, 0), "Madeira": (0, 0), "Bordeaux": (0, 0)}


def _plan(sources, season=None, start=None, needed=None):
    return plan_gathering(list(needed or sources), sources, COORDS, start,
                          season_fn=(lambda p, m: (season or {}).get((p, m))))


class AScarcePortRanksBelowAnOrdinaryOne(unittest.TestCase):

    def test_the_ordinary_port_is_chosen(self):
        plan = _plan({"Raisin": ["Madeira", "Bordeaux"]},
                     season={("Madeira", "Raisin"): "low"})
        self.assertEqual(plan.route, ["Bordeaux"])

    def test_WITHOUT_a_season_coverage_still_decides(self):
        """The old behaviour must survive where nothing is known."""
        plan = _plan({"Raisin": ["Madeira"], "Pig": ["Madeira", "Faro"]})
        self.assertEqual(plan.route, ["Madeira"], "covers both")

    def test_a_scarce_port_can_still_win_on_coverage(self):
        """It is a weight, not a veto — four thin materials beat one ordinary."""
        sources = {m: ["Madeira"] for m in ("A", "B", "C", "D", "E")}
        sources["A"] = ["Madeira", "Faro"]
        season = {("Madeira", m): "low" for m in ("A", "B", "C", "D", "E")}
        plan = _plan(sources, season=season)
        self.assertEqual(plan.route[0], "Madeira")

    def test_the_weight_is_the_measured_ratio(self):
        self.assertAlmostEqual(_LOW_SEASON_WEIGHT, 0.24, places=2)


class EverySourceScarceIsItsOwnAnswer(unittest.TestCase):
    """Not a failure to plan — a fact about the world, and the one case where giving up early
    is right (user: "if all ports have low stock, then just abandon the task")."""

    def test_it_is_reported(self):
        plan = _plan({"Raisin": ["Madeira", "Bordeaux"]},
                     season={("Madeira", "Raisin"): "low", ("Bordeaux", "Raisin"): "low"})
        self.assertEqual(plan.low_everywhere, {"Raisin"})

    def test_one_ordinary_source_is_enough_to_stay_quiet(self):
        plan = _plan({"Raisin": ["Madeira", "Bordeaux"]},
                     season={("Madeira", "Raisin"): "low"})
        self.assertEqual(plan.low_everywhere, set())

    def test_it_is_NOT_the_same_as_having_no_source(self):
        """One is worth waiting out; the other never will be."""
        plan = plan_gathering(["Raisin", "Ebony"], {"Raisin": ["Madeira"]}, COORDS, None,
                              season_fn=lambda p, m: "low")
        self.assertEqual(plan.low_everywhere, {"Raisin"})
        self.assertEqual(plan.unsourced, {"Ebony"})

    def test_a_season_lookup_that_throws_does_not_fail_the_plan(self):
        def boom(_p, _m):
            raise RuntimeError("kb unreadable")
        plan = plan_gathering(["Raisin"], {"Raisin": ["Madeira"]}, COORDS, None,
                              season_fn=boom)
        self.assertEqual(plan.route, ["Madeira"])
        self.assertEqual(plan.low_everywhere, set())


if __name__ == "__main__":
    unittest.main()
