"""The whole loop, with the real collaborators: goal -> intent -> transition -> activity.

This is the first piece of the split wired together — task runner, dispatcher, intents and the
market activity — with only the screen faked. It is the shape every migrated node will use.

Read the sequence in `test_a_gather_runs_start_to_finish`: the task wants Iron, the bot is on
the port overworld, so the dispatcher dispatches ENTER_BUILDING; the next tick perceives the
market and the activity buys; the tick after that the task runner has what it wanted. Nowhere
in it does the task mention a tab, and nowhere does the activity mention a port.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from brain.activities.market import Hold, MarketActivity
from brain.dispatcher import ActivityResult, Dispatcher, FINISHED
from brain.intents import dispatch, to_intent


class _Screen:
    """A fake game: ENTER_BUILDING moves it from the port into the market."""

    def __init__(self, where="port_overworld"):
        self.where = where
        self.taps = []

    def perceive(self):
        return types.SimpleNamespace(state=self.where, port="Amsterdam")

    def enter(self, name):
        self.taps.append(name)
        self.where = "building:market"
        return {"tapped": True, "via": "list"}


class TheMarketLoop(unittest.TestCase):

    def _run(self, ticks=3):
        screen = _Screen()
        asked, bought = [], {}

        def next_goal(result, state):
            # The market reports a TOTAL, not a per-good breakdown — `buy_to_goal` counts
            # across the whole order and the bulk control buys by the shelf, not by the line,
            # so a per-good figure would be invented (2026-08-26).
            if result is not None and result.observed.get("bought_total"):
                bought["Iron"] = bought.get("Iron", 0) + result.observed["bought_total"]
            asked.append(getattr(state, "state", None))
            return None if bought.get("Iron", 0) >= 242 else Hold({"Iron": 242})

        market = MarketActivity(
            buy_fn=lambda port, goal, **k: {"met": True, "bought_total": 445},
            show_grid_fn=lambda: None, port_fn=lambda: "Amsterdam")

        with patch("actions.sail_actions.tap_building_entry", side_effect=screen.enter):
            d = Dispatcher(perceive=screen.perceive,
                           activities={"building:market": market},
                           next_goal=next_goal, to_intent=to_intent, dispatch=dispatch)
            records = [d.step() for _ in range(ticks)]
        return screen, asked, bought, records

    def test_a_gather_runs_start_to_finish(self):
        screen, _asked, bought, _r = self._run()
        self.assertEqual(screen.taps, ["market"], "entered the market exactly once")
        self.assertEqual(bought, {"Iron": 445})

    def test_the_transition_happens_before_the_work(self):
        """Tick 1 is on the port overworld: no market activity exists there, so the dispatcher
        dispatches the transition rather than trying to buy from the wrong screen."""
        screen, _asked, _bought, records = self._run(ticks=1)
        self.assertEqual(screen.where, "building:market")
        self.assertEqual(records[0]["intent"].name, "ENTER_BUILDING")

    def test_the_task_runner_only_ever_sees_task_words(self):
        _s, _a, _b, records = self._run()
        for r in records:
            if r["result"] is None:
                continue
            for word in ("tab", "tile", "checkbox", "coordinate", "tap"):
                self.assertNotIn(word, f"{r['result'].observed}".lower())

    def test_it_stops_when_the_task_says_so(self):
        """'Nothing' is a valid answer, and the loop must not invent work after it."""
        screen, _asked, _bought, records = self._run(ticks=4)
        self.assertIsNone(records[-1]["goal"])
        self.assertEqual(screen.taps, ["market"], "no further transitions after the goal was met")
