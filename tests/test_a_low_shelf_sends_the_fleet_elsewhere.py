"""A scarce shelf is a reason to LEAVE, and the mission has to hear it.

Live 2026-09-07 at Madeira. Every part of this worked except the handoff, and the mission
sailed to San Village with Raisin 1,211 against 1,712 — five barter rounds instead of six,
and 1,812 Pig stranded aboard because no remaining round could use them.

    15:29:35  'Raisin' season ribbon: low            <- read correctly, on all six looks
    15:29:53  'Raisin' tile is greyed — sold out
    15:30:50  'Raisin' is sold out and still wanted here — tapping the restock  (a blue gem)
    ...
    15:38:41  the shelf could not be read across that purchase — re-reading the hold
    15:38:41  market -> finished {'sold': [], 'port': 'Madeira',
                                  'stopped_because': "'Raisin' is scarce here this season"}
    15:38:50  gather:Madeira done          <- and the reroute said nothing at all

Three separate faults in one line of consequence:

  * it PAID to grind a shelf the season had already condemned (user: "if the stock is low,
    instead of refreshing, just buy what is at the market and leave the market and report
    low stock");
  * the market's own report — `season: low, good: Raisin` — was dropped by `_as_result`,
    which forwarded only the sentence;
  * and the field the reroute reads, `_last_materials`, had just been wiped by a result
    carrying no breakdown — because the unreadable shelf that clears the ledger is the SAME
    condition that makes a port scarce. The reroute goes blind exactly when it is needed.

Bordeaux sells Raisin. Nothing ever asked it.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock


class TheShelfIsLeftRatherThanGroundDown(unittest.TestCase):

    def test_a_low_season_empty_shelf_pays_no_gem_at_all(self):
        from tests.test_a_scarce_season_is_recorded_and_respected import Tile, _run, _state
        st = _state()
        with mock.patch("memory.market_kb.season_of", return_value="low"), \
             mock.patch("memory.market_kb.note_season"):
            out = _run(st, [Tile("Raisin", 0, sold_out=True, active=False)])
        self.assertEqual(out["do"], "finished")
        self.assertEqual((out["season"], out["good"]), ("low", "Raisin"))


class TheReportReachesTheMission(unittest.TestCase):

    def test_the_activity_carries_season_and_good_up(self):
        """`_as_result` forwarded only `stopped_because`, so the two fields that name the
        problem never left the market."""
        from brain.activities.market import Hold, MarketActivity
        from brain.dispatcher import FINISHED
        act = MarketActivity(capture_fn=lambda: object(), tap_fn=lambda *a: None,
                             omni_fn=lambda _f: [])
        out = {"do": "finished", "season": "low", "good": "Raisin", "why": "scarce here"}
        res = act._as_result(out, Hold(orders={"Raisin": 1712}), "Madeira", what="buy")
        self.assertEqual(res.status, FINISHED)
        self.assertEqual(res.observed["season"], "low")
        self.assertEqual(res.observed["good"], "Raisin")


class AnAbsentBreakdownDoesNotEraseAGoodOne(unittest.TestCase):
    """`None` and `{}` are different answers — the same rule as `_sell_page`."""

    def _runner(self):
        from brain.mission_runner import MissionRunner
        r = MissionRunner.__new__(MissionRunner)
        r._last_materials = {}
        r._materials_aboard = {}
        r.subtasks = []
        return r

    def test_a_result_with_no_materials_leaves_the_last_reading_alone(self):
        r = self._runner()
        r._settle_gathers(False, {"Raisin": {"have": 1211, "want": 1712, "state": "short"}})
        r._settle_gathers(False, None)          # the unreadable-shelf result
        self.assertEqual(r._last_materials["Raisin"]["state"], "short")

    def test_a_real_breakdown_still_replaces_it(self):
        r = self._runner()
        r._settle_gathers(False, {"Raisin": {"have": 1211, "want": 1712, "state": "short"}})
        r._settle_gathers(False, {"Raisin": {"have": 1712, "want": 1712, "state": "met"}})
        self.assertEqual(r._last_materials["Raisin"]["state"], "met")


class TheRerouteActsOnWhatThePortSaid(unittest.TestCase):
    """A port stating it cannot supply a material outranks inferring it from a breakdown."""

    def _runner_at(self, port, *, materials, cannot):
        from brain.mission_runner import MissionRunner
        r = MissionRunner.__new__(MissionRunner)
        r._last_materials = dict(materials)
        r._cannot_supply = dict(cannot)
        r.subtasks = []
        r._another_source = lambda m, tried: "Bordeaux"
        r._make_everything_downstream_wait_for = lambda ident: None
        return r

    def _leg(self, port):
        return types.SimpleNamespace(id=f"gather:{port}", kind="gather", location=port,
                                     params={"port": port, "orders": {"Raisin": 1712}},
                                     done=True)

    def test_it_reroutes_on_the_report_even_with_no_breakdown(self):
        """The Madeira case exactly: the breakdown is gone, the report is not."""
        r = self._runner_at("Madeira", materials={}, cannot={"raisin": "Madeira"})
        leg = self._leg("Madeira")
        with mock.patch("brain.mission_runner.logger"):
            r._reroute_what_this_port_cannot_supply(leg, types.SimpleNamespace())
        self.assertIn("gather:Bordeaux:raisin", [t.id for t in r.subtasks])

    def test_a_report_from_ANOTHER_port_does_not_reroute_this_leg(self):
        r = self._runner_at("Faro", materials={}, cannot={"raisin": "Madeira"})
        leg = self._leg("Faro")
        with mock.patch("brain.mission_runner.logger"):
            r._reroute_what_this_port_cannot_supply(leg, types.SimpleNamespace())
        self.assertEqual(r.subtasks, [], "Faro was never asked for Raisin")


if __name__ == "__main__":
    unittest.main()
