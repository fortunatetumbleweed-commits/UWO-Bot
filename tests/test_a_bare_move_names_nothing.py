"""A saved route's commit control says only `Move`, so an unconfirmed panel is a refusal.

Live 2026-09-05, the Hutu run. The route list was opened over route 2's own panel, the
classifier called the screen `location_info`, and the panel handler pressed the bare `Move`:

    'Sans to London' is not on screen — opening the list
    [panel-commit] bare 'Move' button @ (1136,1009) — a saved route's commit control
    world_map -> finished {'where': 'Sans to London', 'kind': 'route'}
    [sea] sailing to sailing route 2 (supply=6d eta=39d)

Thirty-nine days of sailing on six days of supply, reported as success for a route it never
selected. `location_panel_is_for` crops the CITY panel's region and matches the destination's
FIRST WORD — 'Sans' of 'Sans to London' — so on a route panel it read nothing and returned
None, and "unknown is not no" let the commit through.

That policy is right for a city: the panel is the city's and the button says "Go to City".
A route has neither, so when the panel cannot be confirmed NOTHING ties the tap to the
destination.
"""

import types

import brain.world_map_context as C
from brain.activities.world_map import ChooseDestination, WorldMapActivity
from brain.dispatcher import BLOCKED, FINISHED

MAP = types.SimpleNamespace(state="world_map", port=None)


def _act(panel_for, *, log=None):
    log = [] if log is None else log
    return WorldMapActivity(
        context_fn=lambda _s: C.LOCATION_INFO,
        require_tab_fn=lambda t: True,
        panel_for_fn=lambda where: panel_for,
        commit_fn=lambda where: log.append(f"commit:{where}") or True,
        capture_fn=lambda: object()), log


def _route(act):
    return act.work(ChooseDestination("Sans to London", "route"), MAP)


def test_an_unreadable_panel_does_not_commit_a_route():
    """THE CASE THAT SAILED. None means nothing identified the destination."""
    act, log = _act(None)
    res = _route(act)
    assert res.status == BLOCKED
    assert log == [], "a bare Move must not be pressed on an unconfirmed panel"


def test_a_panel_for_another_route_does_not_commit():
    act, log = _act(False)
    res = _route(act)
    assert res.status == BLOCKED
    assert log == []


def test_a_confirmed_route_panel_still_sails():
    """The fix must not strand a correct departure — that is what it is guarding."""
    act, log = _act(True)
    res = _route(act)
    assert res.status == FINISHED
    assert log == ["commit:Sans to London"]


def test_a_city_still_sails_on_an_unreadable_panel():
    """Unchanged for ports: the panel IS the city's and the button says "Go to City", so
    refusing an unreadable read would strand a correct departure."""
    act, log = _act(None)
    res = act.work(ChooseDestination("Lisboa", "port"), MAP)
    assert res.status == FINISHED
    assert log == ["commit:Lisboa"]


def test_a_city_panel_for_somewhere_else_is_still_refused():
    act, log = _act(False)
    res = act.work(ChooseDestination("Barcelona", "port"), MAP)
    assert res.status == BLOCKED
    assert log == []
