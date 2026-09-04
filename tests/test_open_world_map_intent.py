"""The world map is entered by an INTENT, and the purpose rides in the extras.

`Intent.extras` is Android's generic container, and its docstring already named this case:
"the world map is entered to set sail or to make a remote check, and behaves differently for
each, so the purpose is part of the request rather than something the activity has to infer."
Every caller of `open_world_map()` says on the next line what it went there to do — the
intent existed in the code and was simply not carried.
"""
import types

import pytest

from brain.activities.world_map import ChooseDestination
from brain.intents import to_intent


def _state(where):
    return types.SimpleNamespace(state=where, port=None)


def test_a_world_map_goal_from_elsewhere_produces_the_intent():
    intent = to_intent(ChooseDestination("Lisboa", "port"), _state("sea"))
    assert intent.name == "OPEN_WORLD_MAP"


def test_the_purpose_rides_in_the_extras():
    """One intent, not one per errand — which is what lets remote check, route selection and
    a departure share it."""
    intent = to_intent(ChooseDestination("Svear Village", "village"), _state("port_overworld"))
    assert intent.extras["kind"] == "village"
    assert intent.extras["where"] == "Svear Village"
    assert "Svear Village" in intent.extras["purpose"]


def test_already_on_the_map_dispatches_nothing():
    """None means 'do the work', not 'nothing to do'."""
    assert to_intent(ChooseDestination("Lisboa"), _state("world_map")) is None


def test_the_caller_never_mentions_the_map():
    """`ChooseDestination('Lisboa')` says nothing about globes, minimaps or tabs — the same
    shape as Hold(orders=...) mentioning no tab and Barter(good, village) no panel."""
    goal = ChooseDestination("Lisboa", "port")
    for ui_word in ("map", "globe", "minimap", "tab", "tap"):
        assert ui_word not in repr(goal).lower().replace("world map", "")


class TheTapDoesOneThing:
    """`tap_world_map_control` replaces a ten-attempt loop that was doing FIVE jobs: waking
    an OS lock, exiting a building, refusing a village, tapping, and verifying. Four have
    owners now — the bootstrap, the dispatcher's routing, and the next perceive."""


def test_the_tap_reports_only_that_it_tapped():
    """It cannot know whether the map opened, so it does not claim to."""
    import inspect

    from actions.sail_actions import tap_world_map_control
    src = inspect.getsource(tap_world_map_control)
    assert "for attempt" not in src and "while " not in src, "one tap, no loop"
    assert "_is_on_world_map" not in src, "verifying is the next perceive's job"
    assert "_wake_if_locked" not in src, "waking belongs to the bootstrap"
    assert "exit_to_overworld" not in src, "leaving a building is the dispatcher's routing"


def test_it_refuses_the_port_globe_on_a_village():
    """A village's left menu is not a port's, and the calibrated globe point would land on
    something else there — corroborate before tapping a remembered coordinate."""
    import inspect

    from actions.sail_actions import tap_world_map_control
    assert "_looks_like_a_village" in inspect.getsource(tap_world_map_control)


def test_the_tap_actually_RUNS():
    """The other tests here read this function's SOURCE. This one calls it.

    `tap_world_map_control` did `from brain.perceive import where_am_i`, which does not exist
    there — it is defined in sail_actions itself. Every call raised ImportError, and since
    `dispatch()` calls this for an OPEN_WORLD_MAP intent, the map could not be opened at all.
    The source-reading tests stayed green throughout: `inspect.getsource` never executes a
    line. A test that reads code cannot find a broken import.
    """
    from unittest import mock

    from PIL import Image

    from actions.sail_actions import tap_world_map_control

    with mock.patch("actions.sail_actions.where_am_i",
                    return_value={"location": "port_overworld"}), \
         mock.patch("actions.sail_actions.tap") as tap:
        res = tap_world_map_control(Image.new("RGB", (2400, 1080)))

    assert res.get("tapped") is True, res
    assert tap.call_count == 1


def test_the_map_is_reached_from_the_overworld_not_from_inside():
    """THE GLOBE IS ON THE OVERWORLD. From inside a building or a sub-menu there is nothing
    to tap, so the transition is OUT first — the same two-step the hold read makes for the ☰.

    Live 2026-08-29: the trim finished inside the market's Sell submenu and the next leg
    wanted Amsterdam. OPEN_WORLD_MAP was dispatched at 'sub_menu:sell' every tick, the screen
    never changed, and the run stopped on the no-progress guard after six.
    """
    import types

    from brain.activities.world_map import ChooseDestination
    from brain.intents import to_intent

    goal = ChooseDestination(where="Amsterdam", kind="port")
    for inside in ("sub_menu:sell", "building:market"):
        got = to_intent(goal, types.SimpleNamespace(state=inside))
        assert got is not None and got.name == "EXIT_BUILDING", inside

    for outside in ("port_overworld", "sea"):
        got = to_intent(goal, types.SimpleNamespace(state=outside))
        assert got is not None and got.name == "OPEN_WORLD_MAP", outside

    assert to_intent(goal, types.SimpleNamespace(state="world_map")) is None
