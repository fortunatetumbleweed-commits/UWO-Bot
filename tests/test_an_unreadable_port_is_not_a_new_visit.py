"""A failed reading of the port's name is not a different port.

The market's state is keyed to (goal, port) so one visit's cart, scroll position and ledger
never serve another. But an UNREADABLE name is "unknown", not "somewhere else", and treating
it as a new key throws the whole visit away.

Live 2026-09-06 at Madeira: `'port': ''` appears 12 times in the log, and the hold was re-read
12 times for 11 purchases. Each flip between ('Hold','Madeira') and ('Hold','') handed back a
fresh MarketState with `ledger=None`, so `_seed_ledger` ran again — switching to the Sell tab,
scrolling the whole grid, switching back — and the next tick, reading the name successfully,
flipped it straight back.

The re-read was never needed: the ledger is seeded once and every purchase after that is
credited from the shelf drop (user: "if the Result dialog is shown, the number is added, no
need to check every time").
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from brain.activities.market import Hold, MarketActivity
from brain.market_ledger import MarketLedger


class TheVisitSurvivesAnUnreadableName(unittest.TestCase):

    def _activity(self, ports):
        seeds = []
        act = MarketActivity(context_fn=lambda _f: "purchase_page",
                             capture_fn=lambda: object(), tap_fn=lambda *a: None,
                             omni_fn=lambda _f: [])
        act._port_name = lambda _s=None: ports.pop(0) if ports else ""
        act._seed_ledger = lambda _g: seeds.append(1) or MarketLedger()
        return act, seeds

    def _tick(self, act, goal):
        """The state-keying half of a tick, without driving the whole activity."""
        port = act._port_name(None)
        if port:
            act._state = act._state.for_goal((type(goal).__name__, port))
        if act._state.ledger is None:
            act._state.ledger = act._seed_ledger(goal)

    def test_the_ledger_survives_a_name_that_did_not_read(self):
        goal = Hold(orders={"Raisin": 1755})
        act, seeds = self._activity(["Madeira", "", "Madeira", "", "Madeira"])
        for _ in range(5):
            self._tick(act, goal)
        self.assertEqual(len(seeds), 1, "re-read the hold on every flip")

    def test_a_REAL_change_of_port_still_starts_a_new_visit(self):
        """The keying exists for a reason — one visit's cart must never serve another."""
        goal = Hold(orders={"Raisin": 1755})
        act, seeds = self._activity(["Madeira", "Madeira", "Faro"])
        for _ in range(3):
            self._tick(act, goal)
        self.assertEqual(len(seeds), 2)

    def test_a_change_of_GOAL_still_starts_a_new_visit(self):
        act, seeds = self._activity(["Madeira", "Madeira"])
        self._tick(act, Hold(orders={"Raisin": 1755}))
        self._tick(act, Hold(orders={"Pig": 900}))
        self.assertEqual(len(seeds), 1, "same goal type and port — one visit")


if __name__ == "__main__":
    unittest.main()


class ThePortIsAskedOfThePlaceNotTheScreen(unittest.TestCase):
    """A building belongs to a port, and that is the lifetime the answer already has.

    The name is painted on the overworld and nowhere else — `observation` says so as
    `SETTLEMENT_NAME_VISIBLE_ON = {"port_overworld"}` — so `_current_port()`, which gates on
    exactly that, returns None from inside a market every time, after three captures and two
    seconds of sleeping. `last_known_settlement` is the PLACE-scoped value: written when the
    overworld paints it, carried through the buildings above it, dropped on reaching sea.
    """

    def _market(self):
        return MarketActivity(context_fn=lambda _f: "purchase_page",
                              capture_fn=lambda: object(), tap_fn=lambda *a: None,
                              omni_fn=lambda _f: [])

    def test_it_uses_the_settlement_when_the_screen_cannot_say(self):
        act = self._market()
        held = types.SimpleNamespace(last_known_settlement="Madeira")
        with mock.patch("brain.observation.current", return_value=held):
            self.assertEqual(act._port_name(types.SimpleNamespace(port=None)), "Madeira")

    def test_the_screen_still_wins_when_it_CAN_say(self):
        """The record is a cache, never a substitute for looking — the screen outranks it."""
        act = self._market()
        held = types.SimpleNamespace(last_known_settlement="Madeira")
        with mock.patch("brain.observation.current", return_value=held):
            self.assertEqual(act._port_name(types.SimpleNamespace(port="Faro")), "Faro")

    def test_it_does_not_go_hunting_the_overworld_from_a_building(self):
        """`_current_port` captures three times to answer None. Never called from here."""
        act = self._market()
        with mock.patch("brain.observation.current", return_value=None), \
             mock.patch("brain.observation._ensure_persisted_loaded", return_value=None), \
             mock.patch("brain.barter_mission_live._current_port") as hunt:
            self.assertEqual(act._port_name(types.SimpleNamespace(port=None)), "")
        hunt.assert_not_called()
