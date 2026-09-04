"""The left rail IS the lit tab's list, so every rail operation must READ the tab first.

The same bug happened twice, in opposite directions:
  * 2026-08-25 — a village list hunted on the PORT tab, opening the trade-goods filter;
  * 2026-08-27 — `_try_port_search` typing "Barc" into the EXPLORE rail, getting back
    `carved horn`, and persisting the VILLAGE icon (70,290) as `port_list_icon`.

Each was fixed where it was found and neither fix reached the other, because the codebase
could COMMAND the tab and not ASK it — the reader lived inside `select_world_map_tab` as a
means to its own end. Exposing the observation is the fix at the level of the rule.
"""
import inspect

import pytest

from actions import sail_actions


def test_the_active_tab_can_be_ASKED_not_only_set():
    """The gap that let the bug recur: no way to observe the tab, only to command it."""
    assert callable(getattr(sail_actions, "active_world_map_tab", None))
    src = inspect.getsource(sail_actions.active_world_map_tab)
    assert "selected_tab_index" in src, "it must read the LIT tab, not infer one"


@pytest.mark.parametrize("fn,tab", [("_try_port_search", "port"),
                                    ("_try_village_search", "explore")])
def test_both_rail_searches_check_their_tab(fn, tab):
    """Both sides, so a fix to one can no longer miss the other."""
    src = inspect.getsource(getattr(sail_actions, fn))
    assert f'require_world_map_tab("{tab}"' in src, f"{fn} must require the {tab} tab"


@pytest.mark.parametrize("fn", ["_try_port_search", "_try_village_search"])
def test_a_wrong_tab_aborts_rather_than_reading_the_rail(fn):
    """On the wrong tab the rail's icons belong to another list, and the usual acceptance
    test ('did a panel open?') is satisfied by every one of them — every list has a search
    box. So the only safe response is to stop."""
    src = inspect.getsource(getattr(sail_actions, fn))
    gate = src.index("require_world_map_tab")
    after = src[gate:gate + 400]
    assert "return None" in after or "return False" in after


def test_select_uses_the_same_reader_it_exposes():
    """One canonical implementation: the command path and the question path must agree."""
    src = inspect.getsource(sail_actions.select_world_map_tab)
    assert "active_world_map_tab" in src
