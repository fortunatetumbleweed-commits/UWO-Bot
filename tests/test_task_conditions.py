"""Task done-conditions — observable success predicates over world model + obs."""
import unittest

from brain.world_model import WorldModel, Fleet
from brain.perceived_state import PerceivedState
from brain.reasoning_loop import Observation
from brain.task_conditions import (
    snapshot, crew_increased, cargo_bought, cargo_sold, arrived_at, on_screen,
    done_condition_for,
)


def _wm(crew=None, crew_cap=None, cargo=None, ducats=None, location="unknown"):
    wm = WorldModel(fleets=[Fleet(location=location, crew_current=crew,
                                  crew_capacity=crew_cap, cargo_used=cargo)])
    if ducats is not None:
        wm.currencies["ducat"] = ducats
    return wm


def _obs(context="", menu_item="", base="panel"):
    return Observation(perceived=PerceivedState(base=base, context=context,
                                                menu_item=menu_item))


class SnapshotTests(unittest.TestCase):
    def test_captures_crew_cargo_ducats(self):
        s = snapshot(_wm(crew=100, cargo=50, ducats=999))
        self.assertEqual((s["crew"], s["cargo"], s["ducats"]), (100, 50, 999))


class RecruitTests(unittest.TestCase):
    def test_crew_increased(self):
        base = snapshot(_wm(crew=100, crew_cap=500))
        done = crew_increased(base)
        self.assertFalse(done(_wm(crew=100, crew_cap=500), _obs()))   # unchanged
        self.assertTrue(done(_wm(crew=300, crew_cap=500), _obs()))    # went up

    def test_crew_full_is_done_without_baseline(self):
        done = crew_increased(None)
        self.assertTrue(done(_wm(crew=500, crew_cap=500), _obs()))    # at capacity


class BuySellTests(unittest.TestCase):
    def test_buy_needs_cargo_up_and_ducats_down(self):
        base = snapshot(_wm(cargo=100, ducats=1000))
        done = cargo_bought(base)
        self.assertFalse(done(_wm(cargo=100, ducats=1000), _obs()))          # nothing
        self.assertFalse(done(_wm(cargo=200, ducats=1000), _obs()))          # cargo up, ducats same
        self.assertTrue(done(_wm(cargo=200, ducats=800), _obs()))            # bought

    def test_sell_needs_ducats_up_and_cargo_down(self):
        base = snapshot(_wm(cargo=200, ducats=1000))
        done = cargo_sold(base)
        self.assertFalse(done(_wm(cargo=200, ducats=1000), _obs()))
        self.assertTrue(done(_wm(cargo=50, ducats=1500), _obs()))            # sold


class ArriveScreenTests(unittest.TestCase):
    def test_arrived_at(self):
        done = arrived_at("Seville")
        self.assertFalse(done(_wm(location="London"), _obs()))
        self.assertTrue(done(_wm(location="seville"), _obs()))               # case-insensitive

    def test_on_screen(self):
        done = on_screen(context="market", menu_item="purchase")
        self.assertFalse(done(WorldModel(), _obs(context="market", menu_item="sell")))
        self.assertTrue(done(WorldModel(), _obs(context="Market", menu_item="Purchase")))


class LookupTests(unittest.TestCase):
    def test_done_condition_for_maps_tasks(self):
        base = snapshot(_wm(crew=100, crew_cap=500))
        self.assertTrue(done_condition_for("recruit_crew", base)(_wm(crew=300, crew_cap=500), _obs()))
        self.assertIsNone(done_condition_for("sail", base))         # no predicate here


if __name__ == "__main__":
    unittest.main()
