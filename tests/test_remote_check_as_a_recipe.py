"""The remote check is a UI RECIPE, and recipes belong to the activity.

It opens Village Info, switches Base -> Barter, and scrolls a list — a sequence of screen
operations whose only product is a reading. The test is not "is it complicated?" (buying
materials is at least as involved and nobody doubts that is the market's) but "is it about
the SCREEN or the MISSION?". It would be the mission's if it decided WHICH village to check
or whether the ratio is worth sailing for. It decides neither.

`read_village_barter_remote` did it in 167 lines with a `for i in range(max_scrolls)` loop.
Here one screen is read per tick and the dispatcher does the looping, so an interruption
mid-list is cleared by whoever owns it instead of being read through — which is how a
two-of-three recipe came to certify itself complete and be planned from for five runs.
"""
import types

import pytest

import brain.world_map_context as C
from brain.activities.world_map import RemoteCheck, WorldMapActivity
from brain.dispatcher import BLOCKED, FINISHED, WORKING

MAP = types.SimpleNamespace(state="world_map", port=None)
GOAL = RemoteCheck("Svear Village", "Birch Tree")


def _act(context, *, screens=None, complete_after=None, log=None):
    log = log if log is not None else []
    seq = list(screens or [])
    seen = {"n": 0}

    def read_screen():
        seen["n"] += 1
        return seq.pop(0) if seq else ["row"]

    def complete(merged, goal):
        return complete_after is not None and seen["n"] >= complete_after

    act = WorldMapActivity(
        context_fn=lambda _s: context,
        require_tab_fn=lambda t: log.append(f"tab:{t}") or True,
        capture_fn=lambda: object(),
        read_base_fn=lambda: log.append("read_base") or {"rounds": 5},
        switch_tab_fn=lambda t: log.append(f"switch:{t}"),
        read_screen_fn=read_screen,
        merge_fn=lambda ss: [r for s in ss for r in (s or [])],
        complete_fn=complete,
        # TWO LISTS, TWO SCROLLS. `scroll_fn` is the world map's own destination rail down
        # the left; the trade list lives in the Village Info panel on the RIGHT and scrolls
        # inside it. They shared one helper until 2026-08-30, when a run sat swiping the rail
        # — which held one already-selected result — while the trade list it was reading never
        # moved. Injecting both keeps this test honest about which one it is exercising.
        scroll_fn=lambda: log.append("map-rail-scroll"),
        scroll_panel_fn=lambda: log.append("scroll"))

    # A TEST THAT STARTS ON THE BARTER TAB HAS ALREADY BEEN THROUGH BASE. The activity now
    # refuses to finish a read whose day's-rounds are unread — the panel reopens on whichever
    # tab it was left on, and a run that landed straight on Barter finished holding a perfect
    # recipe and no rounds ("Daily Barter Progress unreadable", live 2026-08-29). These tests
    # are about the trade list, so they are given the rounds the real flow would have.
    if context == C.VILLAGE_INFO_BARTER:
        # Seeded through the goal key too: `work()` clears `_base` when the goal changes
        # (the reading belongs to the village, not to the activity), so setting it here alone
        # would be wiped on the first tick.
        act._goal_key = (GOAL.where, GOAL.kind)
        act._base = {"barters_used": 0, "barters_total": 5}
    return act, log


def test_a_remote_check_wants_the_explore_tab():
    """Villages are on Explore. The rail belongs to whichever tab is lit."""
    act, log = _act(C.VILLAGE_INFO_BASE)
    act.work(GOAL, MAP)
    assert log[0] == "tab:explore"


def test_the_base_tab_is_read_before_switching():
    """Base is the only place the day's rounds appear; coming back for them would mean a
    second trip through the panel."""
    act, log = _act(C.VILLAGE_INFO_BASE)
    res = act.work(GOAL, MAP)
    assert log.index("read_base") < log.index("switch:barter")
    assert res.status == WORKING


def test_one_screen_is_read_per_tick_then_one_scroll():
    act, log = _act(C.VILLAGE_INFO_BARTER, complete_after=3)
    res = act.work(GOAL, MAP)
    assert res.status == WORKING and log.count("scroll") == 1


def test_the_read_accumulates_across_ticks_and_finishes():
    act, log = _act(C.VILLAGE_INFO_BARTER,
                    screens=[["a"], ["b"], ["c"]], complete_after=3)
    for _ in range(5):
        res = act.work(GOAL, MAP)
        if res.status != WORKING:
            break
    assert res.status == FINISHED
    assert res.observed["trades"] == ["a", "b", "c"], "screens merge, they do not replace"
    assert res.observed["screens"] == 3


def test_a_list_that_never_completes_is_reported_PARTIAL_not_complete():
    """Materials are invariant: fewer than the KB knows means the read fell short. Certifying
    it is how a two-of-three recipe was planned from for five runs."""
    act, _log = _act(C.VILLAGE_INFO_BARTER, complete_after=None)
    res = None
    for _ in range(14):
        res = act.work(GOAL, MAP)
        if res.status != WORKING:
            break
    assert res.status == BLOCKED
    assert res.observed["partial"] is True
    assert "PARTIAL" in res.detail


def test_the_accumulation_belongs_to_the_village_not_the_activity():
    """One activity is registered and reused, so screens kept across goals would merge one
    village's list into another's."""
    act, _log = _act(C.VILLAGE_INFO_BARTER, screens=[["a"], ["b"]], complete_after=None)
    act.work(GOAL, MAP)
    act.work(GOAL, MAP)
    assert len(act._screens) == 2
    act.work(RemoteCheck("Hutu Village", "Bambara Groundnut"), MAP)
    # A new village starts from an empty read — and its FIRST tick goes for its own day's
    # rounds rather than reading a trade screen, because `_base` was cleared with the screens.
    # Both belong to the village; neither carries over.
    assert act._screens == [], "a new village starts from an empty read"
    assert act._base == {}, "and from its own unread rounds"


def test_it_decides_nothing_the_mission_owns():
    """Which village, and whether the ratio is worth sailing for, stay with the task runner."""
    import inspect
    src = inspect.getsource(WorldMapActivity)
    for mission_word in ("rounds_to_plan", "is_worth", "profit", "choose_village"):
        assert mission_word not in src


def test_every_context_state_still_has_a_handler():
    assert set(WorldMapActivity._HANDLERS) == set(WorldMapActivity.CONTEXT_STATES)


def test_the_panel_may_open_on_BARTER_and_the_rounds_are_still_read():
    """The Village Info panel reopens on whichever tab it was last left on.

    `_on_village_info_base` reads the day's rounds and then switches to Barter, so the barter
    handler assumed the rounds were already in hand. Live 2026-08-29 a previous run left the
    panel on Barter: the trade list read perfectly, the rounds were never read at all, and the
    mission died on "Daily Barter Progress unreadable" while holding a complete recipe.

    The rounds live ONLY on the Base tab, so the handler goes and gets them rather than
    finishing without them.
    """
    act, log = _act(C.VILLAGE_INFO_BARTER, screens=[["a"]], complete_after=1)
    act._base = {}                      # the panel opened on Barter; nothing read yet
    act._goal_key = (GOAL.where, GOAL.kind)

    res = act.work(GOAL, MAP)
    assert res.status == WORKING
    assert "switch:base" in log, "it finished without the day's rounds"
    assert act._screens == [], "it should not read the trade list before the rounds"


def test_it_does_not_go_back_for_rounds_it_already_has():
    """One trip through the panel, not a lap per tick."""
    act, log = _act(C.VILLAGE_INFO_BARTER, screens=[["a"]], complete_after=1)
    res = act.work(GOAL, MAP)
    assert "switch:base" not in log
    assert res.status in (WORKING, FINISHED)
