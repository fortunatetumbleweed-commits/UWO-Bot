"""Goals become transitions here, and nowhere else.

`Hold({"Iron": 242})` says what the hold should contain. THIS module is the only place that
knows a market is a building you enter by tapping its name in a panel. Move that knowledge up
and the task layer starts issuing UI instructions again — which is the inversion the whole
design removes, and which cost a run on 2026-08-26 when a tail that "knew" it was in a village
pressed Back at a port overworld and raised "Exit Game?".
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from brain.activities.market import FreeHold, Hold, SellHold
from brain.dispatcher import Intent
from brain.intents import dispatch, to_intent


def _state(where, port=None):
    return types.SimpleNamespace(state=where, port=port)


class AGoalBecomesATransitionOnlyWhenOneIsNeeded(unittest.TestCase):

    def test_a_market_goal_from_a_port_asks_to_enter_the_market(self):
        i = to_intent(Hold({"Iron": 242}), _state("port_overworld"))
        self.assertEqual(i.name, "ENTER_BUILDING")
        self.assertEqual(i.extras["name"], "market")

    def test_the_purpose_travels_with_it(self):
        """An intent carries WHY, as Android's do — so the far side never has to infer it."""
        i = to_intent(Hold({"Iron": 242}), _state("port_overworld"))
        self.assertIn("Iron", i.extras["purpose"])

    def test_no_transition_is_needed_once_the_bot_is_there(self):
        """None means 'do the work', not 'nothing to do'.

        The states come from the activity itself — see OneDeclarationOfWhereAnActivityWorks
        below for why they are not restated here."""
        from brain.activities.market import MarketActivity
        for where in MarketActivity.SERVES:
            with self.subTest(where=where):
                self.assertIsNone(to_intent(Hold({"Iron": 242}), _state(where)))

    def test_all_three_market_goals_route_the_same_way(self):
        for goal in (Hold({"Iron": 1}), FreeHold(("Water",)), SellHold(("Food",))):
            with self.subTest(goal=str(goal)):
                i = to_intent(goal, _state("port_overworld"))
                self.assertEqual(i.extras["name"], "market")

    def test_no_goal_means_no_intent(self):
        self.assertIsNone(to_intent(None, _state("sea")))

    def test_an_unknown_goal_asks_for_nothing_rather_than_guessing(self):
        self.assertIsNone(to_intent("do something clever", _state("sea")))


class DispatchTapsAndReturns(unittest.TestCase):
    """A transition is a switch of activity, however long it takes. Nothing here waits for it:
    `loading` is the state the next tick perceives on the way, and every
    wait-for-the-transition loop in this codebase has been a source of stalls."""

    def test_entering_taps_the_building_entry(self):
        with patch("actions.port_panel.tap_building_entry",
                   return_value={"tapped": True, "via": "list"}) as tap:
            dispatch(Intent("ENTER_BUILDING", {"name": "market"}))
        # Through `brain.activities.port.enter_building` — the dispatcher names the PORT
        # now, not the reader (2026-09-11). The call it makes is unchanged.
        tap.assert_called_once_with("market")

    def test_it_does_not_wait_or_verify_arrival(self):
        """`tapped` means a control was pressed, not that the bot is inside. Entering takes a
        walk across the port, and there is no local signal separating "walking" from "the tap
        missed" — so nobody here tries to tell them apart."""
        with patch("actions.port_panel.tap_building_entry",
                   return_value={"tapped": True}), \
             patch("time.sleep") as slept, \
             patch("capture.adb_capture.capture_screen") as cap:
            dispatch(Intent("ENTER_BUILDING", {"name": "market"}))
        slept.assert_not_called()
        cap.assert_not_called()

    def test_a_refused_tap_is_reported_not_retried(self):
        with patch("actions.port_panel.tap_building_entry",
                   return_value={"tapped": False, "reason": "not the building list"}) as tap:
            res = dispatch(Intent("ENTER_BUILDING", {"name": "market"}))
        self.assertFalse(res["tapped"])
        self.assertEqual(tap.call_count, 1, "retrying is the loop's job, not the dispatch's")

    def test_an_unknown_intent_does_nothing(self):
        self.assertIsNone(dispatch(Intent("TELEPORT", {"to": "Lisboa"})))


class OneDeclarationOfWhereAnActivityWorks(unittest.TestCase):
    """`to_intent` and the activity registry must agree about where the market works.

    They did not, for about an hour on 2026-08-26. `to_intent` compared against
    ('building', 'sub_menu', 'market') while the registry was keyed ('building:market', ...),
    so "already there" was never recognised: the bot stood INSIDE the market at Lisboa and the
    dispatcher issued ENTER_BUILDING on every tick. Only `tap_building_entry` refusing to tap
    a screen that is not the building list kept it from tapping at random in there.

    The activity declares it now, and everyone else asks.
    """

    def test_the_registry_is_built_from_what_the_activity_serves(self):
        from brain.activities.market import MarketActivity
        from brain.run_goal import default_activities
        keys = set(default_activities())
        for where in MarketActivity.SERVES:
            self.assertIn(where, keys)

    def test_no_transition_is_asked_for_from_any_state_it_serves(self):
        """The exact failure: standing in the market and being told to enter it."""
        from brain.activities.market import MarketActivity
        for where in MarketActivity.SERVES:
            with self.subTest(where=where):
                self.assertIsNone(to_intent(Hold({"Iron": 1}), _state(where)))

    def test_a_plain_building_is_still_not_a_market(self):
        """`building` alone is too coarse — it is also the harbour and the shipyard."""
        self.assertIsNotNone(to_intent(Hold({"Iron": 1}), _state("building")))
        self.assertIsNotNone(to_intent(Hold({"Iron": 1}), _state("building:harbor")))


class NoSecondListOfWhatTheMarketSERVES(unittest.TestCase):
    """This file kept its own copy of the market's states, then its own copy of the market's
    goals, and both drifted within a day of each other (2026-08-26).

    First the states: `brain.intents` compared against ('building', 'sub_menu', 'market')
    while the registry was keyed ('building:market', ...), so `to_intent` never recognised
    "already there" and ENTER_BUILDING was dispatched on every tick INSIDE the market.

    Then the goals: `TrimHold` was added to the activity and not to the tuple here, so
    `to_intent` answered None for it — which the dispatcher reads as "already where the work
    happens" — and `sell_surplus` would have stood on the port overworld waiting for a market
    it never walked into. Nothing failed loudly; it would simply never have worked.

    These tests are about the SHAPE of that mistake, not the two instances of it. A goal added
    to the activity and forgotten here must fail a test, not a live run.
    """

    def test_every_goal_the_market_defines_is_a_goal_it_is_routed_for(self):
        import dataclasses
        import inspect
        from brain.activities import market as m

        defined = {obj for _n, obj in inspect.getmembers(m, inspect.isclass)
                   if dataclasses.is_dataclass(obj) and obj.__module__ == m.__name__}
        missing = defined - set(m.MarketActivity.GOALS)
        self.assertEqual(missing, set(),
                         f"defined in market.py but not in MarketActivity.GOALS: "
                         f"{sorted(c.__name__ for c in missing)} — `to_intent` will answer "
                         "None for these and the bot will never enter the market")

    def test_every_declared_goal_routes_into_the_market_from_outside(self):
        from brain.activities.market import MarketActivity
        for goal_cls in MarketActivity.GOALS:
            with self.subTest(goal=goal_cls.__name__):
                goal = goal_cls(*_a_plausible_argument(goal_cls))
                i = to_intent(goal, _state("port_overworld"))
                self.assertIsNotNone(i, f"{goal_cls.__name__} never enters the market")
                self.assertEqual(i.name, "ENTER_BUILDING")

    def test_every_declared_goal_is_left_alone_once_inside(self):
        from brain.activities.market import MarketActivity
        for goal_cls in MarketActivity.GOALS:
            for where in MarketActivity.SERVES:
                with self.subTest(goal=goal_cls.__name__, where=where):
                    goal = goal_cls(*_a_plausible_argument(goal_cls))
                    self.assertIsNone(to_intent(goal, _state(where)))


def _a_plausible_argument(goal_cls):
    """One argument of the right shape — a mapping for quantity goals, a tuple for the rest."""
    import dataclasses
    field = dataclasses.fields(goal_cls)[0]
    return ({"Iron": 1},) if "Mapping" in str(field.type) else ((),)


# ── Leaving the world map ───────────────────────────────────────────────────
#
# The map is a full-screen overlay over the place the fleet is actually standing in, and most
# work cannot be done on it. Live 2026-08-29: the remote check finished and left the fleet on
# the map, then `ReadHold` asked for the hamburger — which lives on the port overworld. No
# activity serves `world_map` and no branch dispatched anything, so the tick reported
# "activity is lost" every time until the budget ran out.


def _on_the_map():
    import types
    return types.SimpleNamespace(state="world_map", location="world_map", port=None)


def test_work_that_is_not_on_the_map_closes_it():
    from brain.activities.harbor import Depart
    from brain.activities.market import Hold
    from brain.activities.sea import ReadHold

    for goal in (ReadHold(), Depart("Svear"), Hold({"Iron": 10})):
        intent = to_intent(goal, _on_the_map())
        assert intent is not None, f"{goal} stalled on the world map"
        assert intent.name == "CLOSE_WORLD_MAP", f"{goal} -> {intent}"


def test_the_maps_own_errands_stay_on_it():
    from brain.activities.world_map import ChooseDestination, RemoteCheck

    for goal in (ChooseDestination("Svear Village", "village"),
                 RemoteCheck(village="Svear Village")):
        assert to_intent(goal, _on_the_map()) is None, f"{goal} was sent off the map"


def test_being_clear_of_the_village_is_satisfied_by_the_map():
    """The tail opens the map from there, so standing on it IS clear."""
    from brain.activities.sea import ClearOfTheVillage

    assert to_intent(ClearOfTheVillage(), _on_the_map()) is None


def test_it_is_decided_before_the_goal_branches():
    """`Depart` would otherwise tap a harbour entrance THROUGH the open map, and `ReadHold`
    would fall to the Ashore branch's `return None` and stall. Both are answered by where we
    are, before anything asks where the work happens."""
    from brain.activities.harbor import Depart

    assert to_intent(Depart("Svear"), _on_the_map()).name == "CLOSE_WORLD_MAP"
    import types
    ashore = types.SimpleNamespace(state="port_overworld", location="port_overworld", port=None)
    assert to_intent(Depart("Svear"), ashore).name == "ENTER_BUILDING"


def test_closing_the_map_is_one_back():
    from unittest import mock

    from brain.dispatcher import Intent

    with mock.patch("actions.adb_actions.press_back") as back:
        dispatch(Intent("CLOSE_WORLD_MAP", {"purpose": "read the hold"}))
    assert back.call_count == 1


def test_a_village_is_somewhere_you_leave_to_reach_the_map():
    """SAILING IS NOT THE VILLAGE'S WORK (user, 2026-08-29).

    A village screen is chromed — back arrow, the Explore/Gifting/Loot/Recruit/Barter menu,
    no hamburger and NO GLOBE. There is no world-map control in a village at all: you leave
    to the sea and open it from the minimap. So when the barter is done and the mission asks
    for a port, the village's order is finished and it exits; the dispatcher then asks for
    the next order from wherever that lands.

    `_is_inside` already described this — "anywhere with a Back and no ☰" — and the prefix
    test missed it because `village` carries no prefix. Live 2026-08-29 the barter finished
    at Svear with the Birch Tree aboard and OPEN_WORLD_MAP went out at the village on every
    tick until the run stopped one leg from done.
    """
    import types

    from brain.activities.village import Barter
    from brain.activities.world_map import ChooseDestination
    from brain.intents import to_intent

    def _st(where):
        return types.SimpleNamespace(state=where)

    course = ChooseDestination(where="Lisboa", kind="port")
    assert to_intent(course, _st("village")).name == "EXIT_BUILDING"
    assert to_intent(course, _st("sea")).name == "OPEN_WORLD_MAP"
    assert to_intent(course, _st("port_overworld")).name == "OPEN_WORLD_MAP"

    # ...and the village keeps the work that IS its own.
    assert to_intent(Barter(good="Birch Tree", village="Svear Village"),
                     _st("village")) is None
