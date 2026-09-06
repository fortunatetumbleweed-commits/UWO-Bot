"""Switching tabs is a screen change, so everything after it must look again.

Live 2026-09-05, the Hutu run taking the route home. `_ensure_tab` runs BEFORE the context
is classified and before any handler, and the tick opened on the PORT tab:

    19:04:52  rail belongs to 'port', need 'route' — switching
    19:05:02  'route' is now selected              <- the screen has changed
    19:05:05  World map OCR (32 tokens)            <- not one route row in them
    19:05:05  'Sans to London' is not on screen — opening the list
    19:05:05  opening the route list — icon (69, 170)

Every read after the switch still answered about the port screen. The classifier called it
MAP_OPEN instead of ROUTE_LIST; the name search missed a route that WAS on screen and does
match fuzzily; and `_open_list` took the PORT rail's icon position and tapped it into the
route list that had appeared, where (69,170) is the divider between 'Sailing Route 2' and
'san to london'. It selected the row above, a bare Move committed it, and the fleet sailed a
39-day route on 6 days of supply.
"""

from __future__ import annotations

import types
from unittest import mock

import brain.world_map_context as C
from brain.activities.world_map import ChooseDestination, WorldMapActivity

MAP = types.SimpleNamespace(state="world_map", port=None)
ROUTE = ChooseDestination("san to london", "route")


def _activity(*, tab_now, captures):
    """`captures` is what a FRESH look returns, in order — the post-switch screens."""
    seen = iter(captures)
    act = WorldMapActivity(
        context_fn=lambda _s: C.MAP_OPEN,
        capture_fn=lambda: next(seen),
        find_fn=lambda w: None,
        open_list_fn=lambda: None,
        tap_fn=lambda x, y: None,
        commit_fn=lambda where: True)
    act._tick_frame = "STALE port-tab frame"
    return act


def _ensure(act, *, already_lit):
    """Drive _ensure_tab with the real implementation and a stubbed action layer."""
    fake = types.SimpleNamespace(
        active_world_map_tab=lambda frame: "route" if already_lit else "port",
        require_world_map_tab=lambda tab, why, frame: True)
    with mock.patch.dict("sys.modules", {"actions.sail_actions": fake}):
        return act._ensure_tab("route", ROUTE)


def test_a_switch_drops_the_stale_frame():
    """THE CASE THAT SAILED. After a switch, the next reader must capture again."""
    act = _activity(tab_now="port", captures=["FRESH route-tab frame"])
    assert _ensure(act, already_lit=False) is True
    assert act._frame() == "FRESH route-tab frame", (
        "reads after the switch must not answer about the tab we just left")


def test_no_switch_keeps_the_tick_frame():
    """One observation per tick is the rule; this only bends it when the screen changed."""
    act = _activity(tab_now="route", captures=["should not be captured"])
    assert _ensure(act, already_lit=True) is True
    assert act._frame() == "STALE port-tab frame", (
        "with no switch there is nothing to re-read, and a capture costs ~2-3s")


def test_a_failed_switch_does_not_drop_the_frame():
    """If the tab did not change, the frame still describes the screen."""
    act = _activity(tab_now="port", captures=["should not be captured"])
    fake = types.SimpleNamespace(
        active_world_map_tab=lambda frame: "port",
        require_world_map_tab=lambda tab, why, frame: False)
    with mock.patch.dict("sys.modules", {"actions.sail_actions": fake}):
        assert act._ensure_tab("route", ROUTE) is False
    assert act._frame() == "STALE port-tab frame"


def test_the_injected_tab_fn_is_still_honoured():
    """Tests inject `require_tab_fn`; that path must keep working unchanged."""
    calls = []
    act = WorldMapActivity(context_fn=lambda _s: C.MAP_OPEN,
                           require_tab_fn=lambda t: calls.append(t) or True,
                           capture_fn=lambda: "x")
    assert act._ensure_tab("route", ROUTE) is True
    assert calls == ["route"]
