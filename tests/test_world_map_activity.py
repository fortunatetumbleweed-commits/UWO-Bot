"""The world map as a context: look, then type, and scroll only as the backup.

It replaces the map half of `open_world_map` — 151 lines and two nested loops doing five
jobs at once: waking an OS lock, exiting a building, refusing a village, tapping, verifying.
Four of those belong elsewhere now. Live 2026-08-28 that function was handed a lock screen,
a daily-news popup and an Investment Season banner, and all three of its available responses
were wrong: it waited for a TransientActivity it reached ZERO times, read "Season" out of the
banner, accepted it as a port name, and reported "Overworld confirmed".
"""
import types
from unittest import mock

import pytest

import brain.world_map_context as C
from brain.activities.world_map import ChooseDestination, WorldMapActivity
from brain.dispatcher import BLOCKED, FINISHED, UNRECOGNISED, WORKING

MAP = types.SimpleNamespace(state="world_map", port=None)
GOAL = ChooseDestination("Lisboa", "port")


def _act(context, *, found=None, list_open=True, **kw):
    """`list_open` is the PRECONDITION of the rail handlers, not part of what they do.

    `_on_list` verifies which list is on screen before reading it — for a port goal as well
    as a village one since 2026-09-01 — and these tests exercise the SEARCH inside an open
    list. Saying so plainly keeps them about looking, typing and scrolling. The verification
    itself is covered in `test_both_lists_are_verified`.
    """
    log = kw.setdefault("log", [])
    return WorldMapActivity(
        port_list_open_fn=lambda: list_open,
        village_list_open_fn=lambda: list_open,
        context_fn=lambda _s: context,
        require_tab_fn=lambda t: log.append(f"tab:{t}") or True,
        find_fn=lambda w: found,
        tap_fn=lambda x, y: log.append(f"tap:{x},{y}"),
        open_list_fn=lambda: log.append("open_list"),
        type_fn=lambda p: log.append(f"type:{p}"),
        scroll_fn=lambda: log.append("scroll"),
        commit_fn=lambda where: log.append(f"commit:{where}") or True,
        capture_fn=lambda: object()), log


def test_the_tab_is_settled_before_the_rail_is_touched():
    """The rail belongs to whichever tab is lit. Touching it first reads another list's rows
    — which is how "Barc" was typed into the Explore rail and returned `carved horn`."""
    act, log = _act(C.MAP_OPEN, found=(100, 200))
    act.work(GOAL, MAP)
    assert log[0] == "tab:port", "the tab comes first"


def test_a_village_goal_wants_the_explore_tab():
    act, log = _act(C.MAP_OPEN, found=(1, 2))
    act.work(ChooseDestination("Svear Village", "village"), MAP)
    assert log[0] == "tab:explore"


def test_looking_comes_before_typing():
    """Step 1 of the ladder, and the step the port path never had. Searching CHANGES the
    screen it searches — the keyboard covers half the map — so a search that runs before
    looking degrades the read that would have succeeded."""
    act, log = _act(C.DESTINATION_LIST, found=(240, 400))
    res = act.work(GOAL, MAP)
    assert "tap:240,400" in log
    assert not any(e.startswith("type") for e in log), "do not type what is already visible"
    assert res.status == WORKING


def test_typing_comes_before_scrolling():
    """Scrolling is the BACKUP, not a cheaper middle step: at Hutu two scrolls cost ~18s
    where three characters would have found the row at once."""
    act, log = _act(C.DESTINATION_LIST, found=None)
    act.work(GOAL, MAP)
    assert any(e.startswith("type:") for e in log)
    assert "scroll" not in log


def test_scrolling_is_the_backup_when_typing_does_not_take():
    """It earns its place because the soft keyboard can defeat the search outright.

    The fallback is now bounded by typings SENT, not typings counted: a typing only spends
    the search's budget once the box shows the query (2026-09-01), so a keyboard that never
    accepts anything is stopped by `_MAX_TYPE_ATTEMPTS` instead. This stub never lets a
    typing land, which is exactly that case — so it takes the attempts, then scrolls.
    """
    from brain.activities.world_map import _MAX_TYPE_ATTEMPTS
    act, log = _act(C.DESTINATION_LIST, found=None)
    for _ in range(_MAX_TYPE_ATTEMPTS + 2):
        act.work(GOAL, MAP)
    assert any(e.startswith("type:") for e in log)
    assert "scroll" in log, "once typing cannot land, fall back to scrolling"


def test_the_search_gives_up_and_says_so():
    """A rail that never surfaces it is a fact to report, not to grind at."""
    act, log = _act(C.DESTINATION_LIST, found=None)
    res = None
    # Enough ticks for every typing attempt AND every scroll — the scroll cap went 6 -> 20
    # because six swipes did not reach Barcelona from Tripoli (2026-09-01).
    from brain.activities.world_map import _MAX_SCROLLS, _MAX_TYPE_ATTEMPTS
    for _ in range(_MAX_TYPE_ATTEMPTS + _MAX_SCROLLS + 4):
        res = act.work(GOAL, MAP)
        if res.status != WORKING:
            break
    assert res.status == BLOCKED
    assert "looked, typed and scrolled" in res.detail


def test_the_panel_commits_and_finishes():
    act, log = _act(C.DESTINATION_PANEL)
    res = act.work(GOAL, MAP)
    assert "commit:Lisboa" in log and res.status == FINISHED


def test_it_never_claims_where_the_commit_landed():
    """Where a departure leaves us is the next perceive's business."""
    act, _log = _act(C.DESTINATION_PANEL)
    res = act.work(GOAL, MAP)
    assert "sea" not in str(res.observed) and "arriv" not in res.detail.lower()


def test_somewhere_else_hands_back():
    act, _log = _act(C.MAP_OPEN)
    assert act.work(GOAL, types.SimpleNamespace(state="sea")).status == UNRECOGNISED


def test_a_goal_it_cannot_serve_is_refused():
    act, _log = _act(C.MAP_OPEN)
    assert act.work("sail somewhere", MAP).status == BLOCKED


def test_a_MISS_hands_back():
    act, _log = _act(C.MISS)
    assert act.work(GOAL, MAP).status == UNRECOGNISED


def test_the_attempt_counters_belong_to_the_destination():
    """One activity is registered and reused, so counts kept across goals would report the
    previous search's attempts as this one's."""
    act, _log = _act(C.DESTINATION_LIST, found=None)
    act.work(GOAL, MAP)
    act.work(GOAL, MAP)
    # ATTEMPTS, not landings: this stub's typing never reaches a box, and a typing that did
    # not land must not spend the search's budget (2026-09-01).
    assert act._type_attempts == 2
    assert act._typed == 0
    act.work(ChooseDestination("Bordeaux", "port"), MAP)
    assert act._type_attempts == 1, "a new destination starts from zero"


def test_every_context_state_has_a_handler():
    assert set(WorldMapActivity._HANDLERS) == set(WorldMapActivity.CONTEXT_STATES)


def test_it_is_registered_for_the_world_map():
    from brain.run_goal import default_activities
    assert any(isinstance(a, WorldMapActivity) for a in default_activities()["world_map"])


class TheMapHasSeveralErrands:
    """One activity, several goals — the reason `Intent.extras` carries a purpose. A
    departure, a route selection, a remote check and an event read all enter the same screen
    and behave differently once there."""


def test_the_event_schedule_is_read_and_nothing_is_decided():
    """WHICH event is worth sailing for depends on the CARGO and belongs to the task runner —
    naming a category by hand once sent the fleet after goods it was not carrying."""
    from brain.activities.world_map import ReadEventSchedule
    act = WorldMapActivity(context_fn=lambda _s: C.EVENT_SCHEDULE,
                           require_tab_fn=lambda t: True, capture_fn=lambda: object(),
                           read_events_fn=lambda: ["Bremen bazaar", "Lisboa fair"])
    res = act.work(ReadEventSchedule(), MAP)
    assert res.status == FINISHED
    assert res.observed["events"] == ["Bremen bazaar", "Lisboa fair"]


def test_location_info_departs_by_its_OWN_move_button():
    """The row's pin only OPENS Location Info; it is that panel's gold Move that sails."""
    from brain.activities.world_map import MoveViaLocationInfo
    log = []
    act = WorldMapActivity(context_fn=lambda _s: C.LOCATION_INFO,
                           require_tab_fn=lambda t: True, capture_fn=lambda: object(),
                           commit_fn=lambda where: log.append(f"move:{where}") or True)
    res = act.work(MoveViaLocationInfo("Bremen"), MAP)
    assert res.status == FINISHED and log == ["move:Bremen"]
    assert res.observed["via"] == "location_info"


def test_a_location_info_without_a_move_is_reported():
    from brain.activities.world_map import MoveViaLocationInfo
    act = WorldMapActivity(context_fn=lambda _s: C.LOCATION_INFO,
                           require_tab_fn=lambda t: True, capture_fn=lambda: object(),
                           commit_fn=lambda where: False)
    res = act.work(MoveViaLocationInfo("Bremen"), MAP)
    assert res.status == BLOCKED and "no Move button" in res.detail


def test_every_goal_the_map_serves_has_somewhere_to_land():
    """`GOALS` and the handler table must not drift: a goal with no reachable state, or a
    state with no handler, is a silent dead end."""
    assert set(WorldMapActivity._HANDLERS) == set(WorldMapActivity.CONTEXT_STATES)
    assert len(WorldMapActivity.GOALS) >= 4


# ── The free read unpacks what the reader ACTUALLY returns ──────────────────
#
# `_find_port_on_world_map` returns a PAIR, and the activity took it for a position. A miss is
# `(None, [names it could see])` — truthy — so a village that was NOT on screen "was found"
# and the tap went to x=None. Live 2026-08-29:
#
#     [world_map] 'Svear Village' is already on screen @ (None, ['porto', 'copenhagen', …])
#     FAILED at step check: could not read Svear Village
#
# The unit tests missed it because they inject a fake finder returning the shape the activity
# ASSUMED. These assert against the real function instead — the same lesson as reading a
# dataclass's fields off a log line.


def test_the_real_reader_returns_a_pair():
    import inspect

    from actions.sail_actions import _find_port_on_world_map

    ret = inspect.signature(_find_port_on_world_map).return_annotation
    assert "Tuple" in str(ret), "the finder's contract changed; the activity unpacks a pair"


def test_a_miss_is_not_read_as_a_hit():
    from unittest import mock

    from brain.activities.world_map import WorldMapActivity

    act = WorldMapActivity()
    act._tick_frame = object()
    with mock.patch("actions.sail_actions._find_port_on_world_map",
                    return_value=(None, ["porto", "copenhagen"])):
        assert act._find_on_screen("Svear Village") is None


def test_a_hit_returns_just_the_position():
    from unittest import mock

    from brain.activities.world_map import WorldMapActivity

    act = WorldMapActivity()
    act._tick_frame = object()
    with mock.patch("actions.sail_actions._find_port_on_world_map",
                    return_value=((1200, 640), ["porto"])):
        assert act._find_on_screen("Lisboa") == (1200, 640)


# ── The rail's icon depends on the TAB, and the reader on the KIND ──────────
#
# Both were regressions from replacing `_try_village_search` with the activity. That path
# knew the village list is the SECOND icon on Explore and verified by effect; it also used
# the village reader for villages. The activity asked for the topmost icon unconditionally
# and used the PORT reader for everything, so it tapped the wrong icon eight times running
# while the map never changed, and could never have recognised a village row if it had.


def _activity_that_records(sink):
    """A WorldMapActivity whose taps land in `sink` instead of on the phone."""
    act = WorldMapActivity(tap_fn=lambda x, y: sink.append((x, y)))
    act._tick_frame = object()
    return act, sink


def _icons(*ys):
    return [(70, y) for y in ys]


def test_a_village_list_is_the_second_icon():
    goal = ChooseDestination(where="Svear Village", kind="village")
    act, tapped = _activity_that_records(tapped := [])
    with mock.patch("actions.sail_actions._explore_left_icons",
                    return_value=_icons(184, 289, 407, 526)):
        act._open_list(goal)
    assert tapped[0] == (70, 289), "the house is the second icon on Explore"


def test_a_port_list_is_the_first_icon():
    goal = ChooseDestination(where="Lisboa", kind="port")
    act, tapped = _activity_that_records(tapped := [])
    with mock.patch("actions.sail_actions._explore_left_icons",
                    return_value=_icons(184, 289, 407, 526)):
        act._open_list(goal)
    assert tapped[0] == (70, 184)


def test_a_list_that_will_not_open_is_reported_not_walked_around():
    """It used to walk the rail — the goal's icon, then every other one in turn.

    User, 2026-09-08: "if it can not see a list, it should not try another icon, that is
    almost always wrong." Each icon opens a DIFFERENT list, and which one this goal wants is
    known from the goal, so a second icon is not a retry — it is a different question.

    Live 2026-09-08 (frames 31-33 of trace_barter_cmd_2026-09-08T00-43-13): the port icon
    opened the port list correctly, the next tick failed to SEE it, and walking on tapped the
    GOODS icon and discarded the open list. The bound printed "attempt 4 of 2" while not
    holding, and the leg stalled until the no-progress guard ended the mission.
    """
    from brain.activities.world_map import _MAX_LIST_TAPS
    goal = ChooseDestination(where="Svear Village", kind="village")
    act, tapped = _activity_that_records(tapped := [])
    with mock.patch("actions.sail_actions._explore_left_icons",
                    return_value=_icons(184, 289, 407, 526)):
        for _ in range(4):
            act._open_list(goal)
    assert tapped[0] == (70, 289), "it still tries the goal's own icon"
    assert len(set(tapped)) == 1, f"tapped more than one icon: {set(tapped)}"
    assert len(tapped) == _MAX_LIST_TAPS, f"kept tapping: {tapped}"


def test_a_village_is_looked_for_with_the_VILLAGE_reader():
    """`_find_port_on_world_map` indexes ports, so asking it for a village returns None
    however plainly the village is drawn — which is why 'look first' appeared dead for them."""
    act = WorldMapActivity()
    act._tick_frame = object()
    row = types.SimpleNamespace(cx=310, cy=640)
    with mock.patch("actions.sail_actions._visible_row", return_value=row) as vis, \
         mock.patch("vision.omniparser.parse_fast_cached", return_value=[]), \
         mock.patch("actions.sail_actions._find_port_on_world_map") as port:
        got = act._find_on_screen("Svear Village", "village")
    assert got == (310, 640)
    assert vis.called and not port.called


def test_a_port_is_still_looked_for_with_the_port_reader():
    act = WorldMapActivity()
    act._tick_frame = object()
    with mock.patch("actions.sail_actions._find_port_on_world_map",
                    return_value=((900, 500), ["porto"])) as port, \
         mock.patch("actions.sail_actions._visible_row") as vis:
        got = act._find_on_screen("Lisboa", "port")
    assert got == (900, 500)
    assert port.called and not vis.called


# ── Never match your own typing ─────────────────────────────────────────────
#
# The search box holds what we typed into it, so it fuzzy-matches the thing we are searching
# for BY CONSTRUCTION — and it is never the answer. Live 2026-08-29 a previous run left 'Svea'
# in the field; this one read it back as 'sveal', scored it 0.80 against 'Svear Village',
# announced "already on screen @ (301,142) — tapping", and tapped the text field. Tapping a
# text field changes nothing, so it did that until the budget ran out.


def _els(*items):
    return [types.SimpleNamespace(label=lab, cx=x, cy=y, y2=y + 20) for lab, x, y in items]


def test_the_search_box_is_not_a_search_result():
    act = WorldMapActivity()
    act._tick_frame = object()
    with mock.patch("actions.sail_actions._map_search_box", return_value=(420, 141)), \
         mock.patch("vision.omniparser.parse_fast_cached",
                    return_value=_els(("sveal", 302, 142))):
        assert act._find_on_screen("Svear Village", "village") is None


def test_a_real_row_below_the_box_is_still_found():
    act = WorldMapActivity()
    act._tick_frame = object()
    with mock.patch("actions.sail_actions._map_search_box", return_value=(420, 141)), \
         mock.patch("vision.omniparser.parse_fast_cached",
                    return_value=_els(("sveal", 302, 142),
                                      ("Svear Village", 310, 320))):
        assert act._find_on_screen("Svear Village", "village") == (310, 320)


def test_it_still_works_when_there_is_no_search_box():
    """On the map itself there is no field to confuse us with."""
    act = WorldMapActivity()
    act._tick_frame = object()
    with mock.patch("actions.sail_actions._map_search_box", return_value=None), \
         mock.patch("vision.omniparser.parse_fast_cached",
                    return_value=_els(("Svear Village", 310, 320))):
        assert act._find_on_screen("Svear Village", "village") == (310, 320)


# ── A rail is not necessarily THE rail ──────────────────────────────────────
#
# The Explore tab has four icons and each opens its own list. Looking at whatever happens to
# be open is how the bot read its own leftover query out of another list's search box and
# called it the village (live 2026-08-29).


def _rail_act(**kw):
    a = WorldMapActivity(**kw)
    a._tick_frame = object()
    return a


def test_the_wrong_rail_is_replaced_before_it_is_read():
    tapped = []
    act = _rail_act(tap_fn=lambda x, y: tapped.append((x, y)),
               village_list_open_fn=lambda: False,
               find_fn=lambda where: (310, 320))       # would "find" the village if consulted
    with mock.patch("actions.sail_actions._explore_left_icons",
                    return_value=[(70, 184), (70, 289), (70, 407)]):
        res = act._on_list(ChooseDestination(where="Svear Village", kind="village"))
    assert res.observed["did"] == "opened the village list"
    assert tapped == [(70, 289)], "it should open OUR list, not read someone else's"


def test_our_own_rail_is_read_not_reopened():
    """Reopening a list that is already ours toggles it shut — measured 2026-08-24."""
    tapped = []
    act = _rail_act(tap_fn=lambda x, y: tapped.append((x, y)),
               village_list_open_fn=lambda: True,
               find_fn=lambda where: (310, 320))
    res = act._on_list(ChooseDestination(where="Svear Village", kind="village"))
    assert tapped == [(310, 320)], "it should tap the ROW it found"
    assert "tapped" in res.observed["did"]


def test_a_port_goal_does_not_ask_about_villages():
    act = _rail_act(tap_fn=lambda x, y: None,
               village_list_open_fn=lambda: False,     # would divert a village goal
               port_list_open_fn=lambda: True,         # ...but the PORT list IS open
               find_fn=lambda where: (900, 400))
    res = act._on_list(ChooseDestination(where="Lisboa", kind="port"))
    assert "tapped" in res.observed["did"]


def test_an_unreadable_check_does_not_loop_on_reopening():
    """Cannot tell is not the same as no. Treating it as 'not ours' would reopen forever."""
    def _boom():
        raise RuntimeError("vision down")

    act = _rail_act(tap_fn=lambda x, y: None, village_list_open_fn=_boom,
               find_fn=lambda where: None)
    res = act._on_list(ChooseDestination(where="Svear Village", kind="village"))
    assert res.observed["did"] != "opened the village list"


# ── A screen answers only the errand it was written for ─────────────────────
#
# The world map serves four errands and has eight sub-screens, and a handler is written for
# ONE errand — but only two of them checked, so the rest answered for whatever goal arrived.
#
# Live 2026-08-29: a ChooseDestination tap landed on the event schedule, `_on_event_schedule`
# reported FINISHED ("the schedule lists 0 events"), and SailRunner read that as the course
# being set. The ship sat at sea with no destination while everything upstream believed it was
# under way. A handler answering for a goal it was not written for is worse than one that
# fails, because it succeeds.


def _wm(context, **kw):
    act = WorldMapActivity(context_fn=lambda _s: context,
                           require_tab_fn=lambda t: True,
                           capture_fn=lambda: object(), **kw)
    act._tick_frame = object()
    return act


def test_the_event_schedule_does_not_finish_a_course():
    import brain.world_map_context as ctx

    backs = []
    act = _wm(ctx.EVENT_SCHEDULE, back_fn=lambda: backs.append(1),
              read_events_fn=lambda: [])
    res = act.work(ChooseDestination(where="Amsterdam", kind="port"), MAP)

    assert res.status != FINISHED, "it reported the course was set from the event schedule"
    # AND IT DOES NOT TAP. A context the errand cannot use may be a screen a stray tap
    # opened — or a misread. Backing out commits to the first reading, and when it is the
    # second the press walks off the screen the errand needed. Hand back; the dispatcher
    # regains bearings from a fresh perceive.
    assert res.status == UNRECOGNISED, "it should hand back as lost"
    assert backs == [], "it must not act on a screen it did not expect"


def test_the_event_schedule_still_answers_its_own_errand():
    import brain.world_map_context as ctx

    from brain.activities.world_map import ReadEventSchedule

    act = _wm(ctx.EVENT_SCHEDULE, read_events_fn=lambda: [{"good": "Candle"}])
    res = act.work(ReadEventSchedule(), MAP)
    assert res.status == FINISHED
    assert res.observed["events"] == [{"good": "Candle"}]


def test_every_handler_declares_what_it_answers():
    """Two of eight declared it; the rest answered for anything. The table is the record."""
    for context, entry in WorldMapActivity._HANDLERS.items():
        assert isinstance(entry, tuple) and len(entry) == 2, context
        _handler, serves = entry
        assert serves, f"{context} declares no goals"
        for goal in serves:
            assert goal in WorldMapActivity.GOALS, f"{context} claims {goal}, not a map goal"


def test_a_port_row_is_looked_for_in_the_RAIL_not_the_panel():
    """The list is on the LEFT; the panel on the right shows whatever is selected, under the
    same name. The village reader was narrowed after Svear was tapped in the Village Info
    panel at x=1961; the port reader was not, so it matched '# Amsterdam' at x=1974, tapped
    it, landed on the event schedule, and the course was never set."""
    act = WorldMapActivity()
    act._tick_frame = object()
    seen = {}

    def _find(frame, where, x_max=None):
        seen["x_max"] = x_max
        return ((1974, 987), []) if x_max is None else (None, [])

    with mock.patch("actions.sail_actions._find_port_on_world_map", _find):
        in_list = act._find_on_screen("Amsterdam", "port", in_list=True)
        on_map = act._find_on_screen("Amsterdam", "port")

    assert in_list is None, "it took the right-hand panel for a list row"
    assert on_map == (1974, 987), "reading the MAP should still see the whole frame"


def test_the_rail_bound_is_shared_with_the_village_reader():
    """One number for one fact — where the rail ends. Two would drift."""
    import inspect

    from actions.sail_actions import _VILLAGE_LIST_MAX_X

    src = inspect.getsource(WorldMapActivity._find_on_screen)
    assert "_VILLAGE_LIST_MAX_X" in src
    assert _VILLAGE_LIST_MAX_X < 1900, "the rail must not reach the right-hand panel"


def test_the_free_read_of_the_map_gets_one_chance():
    """The map is covered in text that is not a port. Live 2026-08-29 something at
    (1484,494) matched 'Amsterdam' and tapping it opened the EVENT SCHEDULE; the screen-goal
    guard backed out, the next tick read the same frame, found the same thing, and tapped it
    again. A look that does not pay off is not evidence to look harder."""
    import brain.world_map_context as ctx

    taps, opened = [], []
    act = WorldMapActivity(context_fn=lambda _s: ctx.MAP_OPEN,
                           require_tab_fn=lambda t: True,
                           capture_fn=lambda: object(),
                           find_fn=lambda where: (1484, 494),      # always "finds" it
                           tap_fn=lambda x, y: taps.append((x, y)),
                           open_list_fn=lambda: opened.append(1))
    goal = ChooseDestination(where="Amsterdam", kind="port")

    act.work(goal, MAP)
    act.work(goal, MAP)
    act.work(goal, MAP)

    assert len(taps) == 1, f"it tapped the same scenery {len(taps)} times"
    assert opened, "after the free read failed it should use the rail"


def test_a_free_read_that_works_is_not_penalised():
    """One chance is one chance to be WRONG — a read that lands correctly ends the step, so
    the count never matters."""
    import brain.world_map_context as ctx

    taps = []
    act = WorldMapActivity(context_fn=lambda _s: ctx.MAP_OPEN,
                           require_tab_fn=lambda t: True,
                           capture_fn=lambda: object(),
                           find_fn=lambda where: (900, 500),
                           tap_fn=lambda x, y: taps.append((x, y)))
    res = act.work(ChooseDestination(where="Amsterdam", kind="port"), MAP)
    assert taps == [(900, 500)]
    assert res.status == WORKING


def test_the_count_belongs_to_the_destination():
    """A new destination looks afresh — the previous one's bad luck is not evidence."""
    import brain.world_map_context as ctx

    taps = []
    act = WorldMapActivity(context_fn=lambda _s: ctx.MAP_OPEN,
                           require_tab_fn=lambda t: True,
                           capture_fn=lambda: object(),
                           find_fn=lambda where: (1484, 494),
                           tap_fn=lambda x, y: taps.append((x, y)),
                           open_list_fn=lambda: None)
    act.work(ChooseDestination(where="Amsterdam", kind="port"), MAP)
    act.work(ChooseDestination(where="Amsterdam", kind="port"), MAP)
    act.work(ChooseDestination(where="Barcelona", kind="port"), MAP)
    assert len(taps) == 2, "the new destination should get its own look"


def test_an_icon_that_opened_the_list_is_not_second_guessed():
    """The rail's icons carry no label, so candidates are tried in order and the next tick
    judges each — but only the question "did the list open?" is its to judge.

    Live 2026-08-29 the first icon opened the port list correctly, a later step failed for its
    own reasons, and the retry moved on to the SECOND icon and opened something else. A
    failure downstream is not evidence against the icon.
    """
    import brain.world_map_context as ctx

    taps = []
    contexts = iter([ctx.MAP_OPEN,            # look, miss, open the list
                     ctx.DESTINATION_LIST,    # it opened — this icon is right
                     ctx.MAP_OPEN,            # something later went wrong, back on the map
                     ctx.MAP_OPEN])
    act = WorldMapActivity(context_fn=lambda _s: next(contexts),
                           require_tab_fn=lambda t: True,
                           capture_fn=lambda: object(),
                           find_fn=lambda where: None,
                           tap_fn=lambda x, y: None,
                           type_fn=lambda p: None,
                           open_list_fn=None)
    act._tick_frame = object()
    goal = ChooseDestination(where="Amsterdam", kind="port")

    with mock.patch("actions.sail_actions._explore_left_icons",
                    return_value=[(70, 171), (72, 289), (72, 407)]), \
         mock.patch.object(WorldMapActivity, "_tap_at",
                           lambda self, x, y: taps.append((x, y))):
        for _ in range(4):
            act.work(goal, MAP)

    icons = [t for t in taps if t[0] < 200]
    assert icons, "it never opened the list"
    assert len(set(icons)) == 1, f"it wandered to another icon: {icons}"
    assert icons[0] == (70, 171), "the port list is the first icon"


def test_the_port_reader_ignores_its_own_typing():
    """Having typed 'Amst' into the box, the box now reads 'Amst' — which matches 'Amsterdam'
    as surely as any row does. The village reader already excluded the field; the port reader
    did not, so it tapped the search box at (255,199) and the list never opened a destination.

    The rule belongs to the SCREEN, not to one reader.
    """
    act = WorldMapActivity()
    act._tick_frame = object()

    with mock.patch("actions.sail_actions._map_search_box", return_value=(420, 199)), \
         mock.patch("actions.sail_actions._find_port_on_world_map",
                    return_value=((255, 199), [])):
        assert act._find_on_screen("Amsterdam", "port", in_list=True) is None


def test_a_real_port_row_is_still_found():
    act = WorldMapActivity()
    act._tick_frame = object()

    with mock.patch("actions.sail_actions._map_search_box", return_value=(420, 199)), \
         mock.patch("actions.sail_actions._find_port_on_world_map",
                    return_value=((255, 340), [])):
        assert act._find_on_screen("Amsterdam", "port", in_list=True) == (255, 340)


def test_both_readers_use_the_same_search_box_rule():
    """Two copies of this rule would drift, and the one that drifted would tap a text field."""
    import inspect

    src = inspect.getsource(WorldMapActivity._find_on_screen)
    assert src.count("_is_the_search_box") >= 2, "one of the readers is not applying it"


def test_the_destination_reaches_the_commit():
    """THE SEAM PINNED THE WRONG CONTRACT. `commit_fn=lambda: ...` took no argument, so the
    real branch — `commit_departure()` with no destination — raised on every live attempt
    from f9b31a6 onward and no test could see it. The name has to travel."""
    import brain.world_map_context as ctx

    seen = []
    act = _wm(ctx.DESTINATION_PANEL, commit_fn=lambda where: seen.append(where) or True)
    res = act.work(ChooseDestination(where="Amsterdam", kind="port"), MAP)
    assert res.status == FINISHED
    assert seen == ["Amsterdam"]


# NOTE: `test_village_info_does_not_finish_a_course_either` lived here and asserted the
# opposite. It was written while we believed Village Info could not answer a destination
# errand — it can, via its own Move button, and believing otherwise stranded a live mission
# one tap from its village. The hand-back rule it was really testing is still pinned, by
# `test_the_event_schedule_does_not_finish_a_course`, on a context that genuinely cannot
# serve a course.
def test_the_village_panel_commits_a_course_too():
    """A VILLAGE PIN OPENS VILLAGE INFO, AND ITS GOLD 'MOVE TO VILLAGE' BUTTON SAILS.

    So for a ChooseDestination this panel is not a wrong turn — it is the destination panel
    under another name, exactly as City Info is on the port path.

    Live 2026-08-29: with Candle, Iron and Matchlock Gun all aboard, the fleet searched the
    Explore rail, found Svear Village, tapped it — and then handed back three times running
    because `village_info_base` did not answer `ChooseDestination`. The mission stopped one
    tap short of the village it had spent an hour gathering for. The city panel had been
    given both errands weeks earlier; the village panel was never carried across.
    """
    import brain.world_map_context as ctx

    for context in (ctx.VILLAGE_INFO_BASE, ctx.VILLAGE_INFO_BARTER):
        seen = []
        act = _wm(context, commit_fn=lambda where: seen.append(where) or True)
        res = act.work(ChooseDestination(where="Svear Village", kind="village"), MAP)
        assert res.status == FINISHED, context
        assert seen == ["Svear Village"], context
