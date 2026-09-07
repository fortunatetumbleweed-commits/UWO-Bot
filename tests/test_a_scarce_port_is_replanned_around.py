"""Stopping the waste is only half of it — the material still has to come from somewhere.

The buy round already refuses to grind a scarce shelf:

    'Raisin' is scarce here this season — 2 refresh(es) is all this port is worth

but the mission then carried on regardless. Live 2026-09-07 it set off for San Village with
ONE Raisin against 248 a round — zero barter rounds, a voyage spent to arrive unable to trade.
The user asked for the other half: "it records the data and replans immediately".

Raisin has three sources — Bordeaux, Madeira, Trabzon — and only Madeira was flagged. There
was somewhere to go.

`brain/mission.py::recover` does this for the OLD `run_mission` path, which the live mission
stopped using — imported and unreachable. This is the same idea where the mission runs.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from brain.mission import SubTask
from brain.mission_runner import DONE, MissionRunner

RAISIN_SOURCES = ["Bordeaux", "Madeira", "Trabzon"]


def _recipe():
    return types.SimpleNamespace(inputs=[
        types.SimpleNamespace(material="Raisin", source_ports=list(RAISIN_SOURCES)),
        types.SimpleNamespace(material="Pig", source_ports=["Faro", "Gijon"]),
    ])


def _runner(materials, legs=None):
    m = MissionRunner.__new__(MissionRunner)
    m.good, m.completed = "Bambara Groundnut", []
    m.subtasks = legs if legs is not None else [
        SubTask(id="gather:Madeira", kind="gather", location="Madeira",
                params={"port": "Madeira", "orders": {"Raisin": 1712}}),
    ]
    m._last_materials = dict(materials)
    return m


def _reroute(m, seasons=None):
    leg = m.subtasks[0]
    # `_finish_leg` marks the leg done BEFORE calling the reroute, and it matters: a leg still
    # pending would look like "somewhere else already goes there" and suppress the reroute.
    leg.done = True
    with mock.patch("memory.barter_kb.load_recipe", return_value=_recipe()), \
         mock.patch("memory.market_kb.season_of",
                    side_effect=lambda p, g: (seasons or {}).get((p, g))):
        m._reroute_what_this_port_cannot_supply(leg, types.SimpleNamespace(status=DONE))
    return [t.id for t in m.subtasks]


SHORT = {"Raisin": {"have": 1, "want": 1712, "state": "short"}}


class AShortMaterialGetsAnotherPort(unittest.TestCase):

    def test_a_new_gather_leg_is_added(self):
        m = _runner(SHORT)
        self.assertIn("gather:Bordeaux:Raisin", _reroute(m))

    def test_the_new_leg_carries_the_outstanding_want(self):
        m = _runner(SHORT)
        _reroute(m)
        added = [t for t in m.subtasks if t.id == "gather:Bordeaux:Raisin"][0]
        self.assertEqual(added.params["orders"], {"Raisin": 1712})
        self.assertEqual(added.location, "Bordeaux")

    def test_A_PORT_KNOWN_SCARCE_IS_NOT_TRIED(self):
        """The record exists precisely so the same mistake is not repeated."""
        m = _runner(SHORT)
        ids = _reroute(m, seasons={("Bordeaux", "Raisin"): "low"})
        self.assertIn("gather:Trabzon:Raisin", ids)
        self.assertNotIn("gather:Bordeaux:Raisin", ids)

    def test_a_port_ALREADY_TRIED_is_not_tried_again(self):
        """Tried means FINISHED with. A port still pending is covered by the next test."""
        m = _runner(SHORT, legs=[
            SubTask(id="gather:Madeira", kind="gather", location="Madeira",
                    params={"port": "Madeira", "orders": {"Raisin": 1712}}),
            SubTask(id="gather:Bordeaux", kind="gather", location="Bordeaux", done=True,
                    params={"port": "Bordeaux", "orders": {"Raisin": 1712}}),
        ])
        self.assertIn("gather:Trabzon:Raisin", _reroute(m))

    def test_A_MATERIAL_A_PENDING_LEG_ALREADY_COVERS_IS_NOT_REROUTED(self):
        """Short HERE is not short everywhere — the plan may simply not have reached the port
        that sells it.

        Live 2026-09-07 at Madeira the reroute fired twice: rightly for Raisin, and wrongly
        for Pig, which `gather:Faro` was on its way to buy. It added `gather:Gijon:Pig` for a
        material one leg from being met."""
        m = _runner({"Pig": {"have": 1505, "want": 1755, "state": "short"}}, legs=[
            SubTask(id="gather:Madeira", kind="gather", location="Madeira",
                    params={"port": "Madeira", "orders": {"Pig": 1755}}),
            SubTask(id="gather:Faro", kind="gather", location="Faro",
                    params={"port": "Faro", "orders": {"Pig": 1755}}),
        ])
        self.assertEqual(_reroute(m), ["gather:Madeira", "gather:Faro"])

    def test_NOWHERE_LEFT_carries_on_short(self):
        """A leg that cannot help is worse than no leg — the mission proceeds as before."""
        m = _runner(SHORT)
        ids = _reroute(m, seasons={(p, "Raisin"): "low" for p in RAISIN_SOURCES})
        self.assertEqual(ids, ["gather:Madeira"])

    def test_a_material_that_is_MET_is_left_alone(self):
        m = _runner({"Raisin": {"have": 1712, "want": 1712, "state": "met"}})
        self.assertEqual(_reroute(m), ["gather:Madeira"])

    def test_nothing_reported_reroutes_nothing(self):
        m = _runner({})
        self.assertEqual(_reroute(m), ["gather:Madeira"])


if __name__ == "__main__":
    unittest.main()


class TheNewLegRunsBEFORETheTail(unittest.TestCase):
    """Adding a leg is not the same as ORDERING it.

    The tail is a dependency chain built at plan time — `sell_surplus` deps on the ORIGINAL
    gather ids, `sail_to_village` on `supply_verify` — so a leg added later hangs outside it
    and the runner is free to pick anything else that is runnable.

    Live 2026-09-07 it did exactly that: "adding gather:Bordeaux:Raisin", then sell_surplus,
    supply_verify, and "next leg: sail_to_village" — sailing for San with 331 Raisin against
    248 a round, and past that departure there is no more gathering at all.
    """

    def _mission(self):
        legs = [
            SubTask(id="gather:Madeira", kind="gather", location="Madeira",
                    params={"port": "Madeira", "orders": {"Raisin": 1712}}),
            SubTask(id="sell_surplus", kind="sell_surplus", location="",
                    deps=("gather:Madeira",)),
            SubTask(id="supply_verify", kind="supply_verify", location="",
                    deps=("sell_surplus",)),
            SubTask(id="sail_to_village", kind="sail_to_village", location="San Village",
                    deps=("supply_verify",)),
        ]
        m = _runner(SHORT, legs=legs)
        _reroute(m)
        return {t.id: t for t in m.subtasks}

    def test_the_sail_to_the_village_waits_for_it(self):
        by_id = self._mission()
        self.assertIn("gather:Bordeaux:Raisin", by_id["sail_to_village"].deps)

    def test_every_pending_tail_leg_waits_for_it(self):
        by_id = self._mission()
        for ident in ("sell_surplus", "supply_verify", "sail_to_village"):
            self.assertIn("gather:Bordeaux:Raisin", by_id[ident].deps, ident)

    def test_the_ORIGINAL_deps_are_kept(self):
        by_id = self._mission()
        self.assertIn("supply_verify", by_id["sail_to_village"].deps)

    def test_gathers_are_left_unordered(self):
        """They are mutually unordered by design and ranked by cost."""
        by_id = self._mission()
        self.assertEqual(by_id["gather:Madeira"].deps, ())

    def test_a_leg_ALREADY_DONE_is_not_given_a_new_dependency(self):
        legs = [
            SubTask(id="gather:Madeira", kind="gather", location="Madeira",
                    params={"port": "Madeira", "orders": {"Raisin": 1712}}),
            SubTask(id="sell_surplus", kind="sell_surplus", location="", done=True),
        ]
        m = _runner(SHORT, legs=legs)
        _reroute(m)
        self.assertEqual({t.id: t for t in m.subtasks}["sell_surplus"].deps, ())
