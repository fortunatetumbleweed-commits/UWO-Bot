"""Each migrated node, driven end to end against a fake world.

The unit tests cover the activities and the intent routing separately. Nothing covered a
NODE's whole path — state a goal, dispatch the transition, walk into the building, do the
work, translate the answer — and that is where the pieces meet.

It is the gap this file exists to close, and the gap was not hypothetical: `sell_surplus`
would have failed on every live run, because `TrimHold` was missing from the market's goal
list and `to_intent` therefore answered None, which the dispatcher reads as "already where
the work happens". The suite was green. Every unit test passed. The node simply stood on the
port overworld waiting for a market it never walked into.

So these tests assert the thing a green suite did not: that the bot ARRIVES.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

import brain.barter_mission_live as bml


class _World:
    """Starts wherever it is told; ENTER_BUILDING walks into the named building."""

    def __init__(self, where="port_overworld", port="Amsterdam"):
        self.where, self.port, self.entered = where, port, []

    def perceive(self):
        return types.SimpleNamespace(state=self.where, port=self.port, detail="", frame=None)

    def enter(self, name):
        self.entered.append(name)
        self.where = f"building:{name}"
        return {"tapped": True}


def _panel(rounds=0, materials=None, amity=(64769, 100000)):
    return types.SimpleNamespace(selected_good="Birch Tree", rounds_remaining=rounds, materials=dict(materials or {}),
                                 amity_points=amity, partial_fraction=0.0,
                                 binding=None, shortfall=0)


def _activities(world, **market_kw):
    from brain.activities.market import MarketActivity
    from brain.activities.village import VillageActivity

    registry = {}
    market = MarketActivity(port_fn=lambda: world.port, show_grid_fn=lambda: None, **market_kw)
    for where in MarketActivity.SERVES:
        registry[where] = market
    # Three rounds, then a grey Exchange. Under the context model that is three separate
    # calls that each commit one round, not one call that runs a loop — so the stub counts
    # them and flips the context when they are done, the way the panel would.
    import brain.village_context as C
    committed = {"n": 0}

    def _village_context(_state):
        return C.BARTER_PANEL_READY if committed["n"] < 3 else C.BARTER_PANEL_BLOCKED

    def _commit():
        committed["n"] += 1
        return {"ok": True}

    village = VillageActivity(
        open_panel_fn=lambda: True, select_fn=lambda g, r: True,
        read_panel_fn=lambda: _panel(materials={"Wares": 3}),
        context_fn=_village_context, commit_fn=_commit,
        overflow_fn=lambda: 0, saw_fn=lambda: {}, exchange_live_fn=lambda: False,
        recipe_fn=lambda g: None)
    for where in VillageActivity.SERVES:
        registry[where] = village
    return registry


def _run(node, params, world, **market_kw):
    from brain.run_goal import run_goal as real_run_goal
    acts = _activities(world, **market_kw)
    with patch("actions.port_panel.tap_building_entry", side_effect=world.enter), \
         patch("brain.run_goal.run_goal",
               side_effect=lambda g, **k: real_run_goal(g, perceive=world.perceive,
                                                        activities=acts)), \
         patch("brain.barter_mission_live._exit_market_to_overworld"), \
         patch("brain.barter_mission_live._current_port", return_value=world.port), \
         patch("brain.mission_progress.record_rounds"), \
         patch("brain.mission_progress.advance"):
        return bml.make_live_executors()[node](params)


class GatherReachesTheMarket(unittest.TestCase):

    def _gather(self, world):
        task = types.SimpleNamespace(params={"port": "Amsterdam",
                                             "orders": {"Iron": 242, "Candle": 411}})
        with patch("brain.barter_mission_live._sail_to", return_value={"ok": True}), \
             patch("brain.barter_mission_live.orders_already_held", return_value=False):
            return _run("gather", task, world,
                        buy_fn=lambda port, goal, **k: {"met": True, "bought_total": 445,
                                                        "ok": True})

    def test_it_walks_into_the_market(self):
        world = _World()
        self._gather(world)
        self.assertEqual(world.entered, ["market"])

    def test_it_reports_what_arrived(self):
        res = self._gather(_World())
        self.assertTrue(res["ok"])
        self.assertEqual(res["bought_total"], 445)

    def test_the_whole_order_goes_in_one_goal(self):
        seen = []
        world = _World()
        with patch("brain.barter_mission_live._sail_to", return_value={"ok": True}), \
             patch("brain.barter_mission_live.orders_already_held", return_value=False):
            _run("gather", types.SimpleNamespace(
                    params={"port": "Amsterdam", "orders": {"Iron": 242, "Candle": 411}}),
                 world, buy_fn=lambda port, goal, **k: seen.append(dict(goal)) or
                                                       {"met": True, "bought_total": 1})
        self.assertEqual(seen, [{"Iron": 242, "Candle": 411}],
                         "one visit told the whole list, not one good per visit")


class SellSurplusReachesTheMarket(unittest.TestCase):
    """The node that was broken. It never failed a test — it just never arrived."""

    def test_it_walks_into_the_market(self):
        world = _World()
        _run("sell_surplus", types.SimpleNamespace(params={"keep_qty": {"Iron": 100}}), world,
             sell_down_fn=lambda port, kq: {"ok": True, "trimmed": {"Iron": 45},
                                            "reason": "trimmed"})
        self.assertEqual(world.entered, ["market"],
                         "TrimHold must route into the market like every other market goal")

    def test_it_reports_what_it_trimmed(self):
        res = _run("sell_surplus", types.SimpleNamespace(params={"keep_qty": {"Iron": 100}}),
                   _World(),
                   sell_down_fn=lambda port, kq: {"ok": True, "trimmed": {"Iron": 45},
                                                  "reason": "trimmed"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["trimmed"], {"Iron": 45})


class BarterRunsFromTheVillage(unittest.TestCase):

    def _barter(self):
        world = _World(where="village", port="Svear Village")
        task = types.SimpleNamespace(params={"good": "Birch Tree",
                                             "village": "Svear Village", "rounds": 1})
        return _run("barter", task, world), world

    def test_it_needs_no_transition_once_at_the_village(self):
        _res, world = self._barter()
        self.assertEqual(world.entered, [], "the village activity opens its own panel")

    def test_it_reports_the_rounds_and_why_it_stopped(self):
        """The wording changed with the context model. 'the village refused' collapsed two
        different screens; a grey Exchange with the panel OPEN means rounds REMAIN and
        something is short, while the day running out CLOSES the submenu. What is pinned
        is unchanged: the count and a reason the task can act on."""
        res, _w = self._barter()
        self.assertTrue(res["ok"])
        self.assertEqual(res["committed"], 3)
        self.assertIn("grey", res["reason"])

    def test_it_hands_back_no_number_to_re_enter_on(self):
        res, _w = self._barter()
        self.assertEqual(res["more_rounds_fundable"], 0)

    def test_the_plans_round_count_is_ignored(self):
        """params says rounds=1; the panel funded 3 and 3 is what came back."""
        res, _w = self._barter()
        self.assertEqual(res["committed"], 3)


if __name__ == "__main__":
    unittest.main()
