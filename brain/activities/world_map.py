"""The world map: choose a destination and commit to it. One action per tick.

It replaces the map half of `open_world_map` (151 lines, two nested loops) and of
`_try_port_search` / `_try_village_search`. Those did five jobs at once — waking an OS lock,
exiting a building, refusing a village, tapping, and verifying — and four of them belong
elsewhere now: the bootstrap establishes position, the clearing activities peel interruptions,
and the dispatcher perceives after every action. What is left is the map's own work.

FINDING A PLACE, in order of cost (memory: look-first-then-type-never-scroll):

    1. LOOK — is it already in the OmniParser results, on the map or in the open rail?
    2. TYPE a prefix into the search box. Typing is FASTER than scrolling.
    3. SCROLL — the BACKUP, for when the soft keyboard breaks the search.

Searching CHANGES the screen it is searching: the keyboard covers the lower half of the map,
so a search that runs before looking degrades the very read that would have succeeded. That
is why the free read is first and why the scroll exists at all.

PORTS AND VILLAGES ARE ON DIFFERENT TABS, and the left rail is whichever tab is lit — so the
tab is READ before the rail is touched, never assumed. Not doing that is how "Barc" was typed
into the Explore rail and came back with `carved horn`, and how the VILLAGE icon was learned
and persisted as the port-list icon (memory: world-map-state-dies-with-the-map).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

from brain import world_map_context as _ctx
from brain.dispatcher import ActivityResult, BLOCKED, FINISHED, UNRECOGNISED, WORKING


@dataclass(frozen=True)
class ChooseDestination:
    """Select `where` on the world map and commit to it.

    `kind` is 'port', 'village' or 'route' — it decides which TAB the rail belongs to, which
    is a fact about the game, not about this bot: ports are on Port, villages on Explore,
    saved routes on Route.
    """
    where: str
    kind: str = "port"

    def __str__(self) -> str:
        return f"choose {self.kind} {self.where!r} on the world map"


@dataclass(frozen=True)
class RemoteCheck:
    """Read a village's trade list from the world map, without sailing to it.

    A UI RECIPE, NOT A BUSINESS TASK. It opens Village Info, switches Base -> Barter, and
    scrolls a list — a sequence of screen operations whose only product is a reading. The
    test is not "is it complicated?" (buying materials is at least as involved and nobody
    doubts that is the market's) but "is it about the SCREEN or the MISSION?".

    What would make it the mission's: deciding WHICH village to check, or whether the ratio
    is worth sailing for. Neither happens here. See docs/activity_as_context.md §14.3.
    """
    village: str
    good: Optional[str] = None      # the good whose materials must be complete before stopping

    kind: str = "village"           # so `to_intent` puts us on the Explore tab
    where: str = ""                 # filled from `village` below

    def __post_init__(self):
        object.__setattr__(self, "where", self.village)

    def __str__(self) -> str:
        return f"remote-check {self.village!r}" + (f" for {self.good!r}" if self.good else "")


@dataclass(frozen=True)
class ReadEventSchedule:
    """Read the world map's Trade Event Schedule — which cities have events, and when.

    A reading, not a decision. Which event is worth sailing for depends on the CARGO and the
    mission, and belongs to the task runner: naming a category by hand once sent the fleet
    after goods it was not carrying (memory: trade-event-selling-flow).
    """
    kind: str = "port"          # the schedule button lives on the Port tab
    where: str = ""

    def __str__(self) -> str:
        return "read the trade event schedule"


@dataclass(frozen=True)
class MoveViaLocationInfo:
    """Depart to `where` through a city pin's Location Info panel.

    A DIFFERENT departure path from the destination panel, and the distinction is load-bearing:
    the row's location pin only OPENS Location Info — it is that panel's gold Move button that
    sails (memory: trade-event-selling-flow).
    """
    where: str
    kind: str = "port"

    def __str__(self) -> str:
        return f"move to {self.where!r} via Location Info"


def _keyable_query(name: str, limit: int | None = None) -> str:
    """The best thing we can actually type to filter the list down to `name`.

    THE SEARCH MATCHES A SUBSTRING, NOT A PREFIX (user, 2026-09-09: "for Malaga you can type
    laga or lag, it searches for the sub string"). Verified live on the world map: typing
    `laga` left one row, `Málaga`. So the query need not start the name — it only has to
    appear in it, which is what makes an accented name reachable at all.

    AND IT MUST BE TYPEABLE. `adb shell input text` cannot carry a non-ASCII character; the
    input service hands the Binder a null array and throws, which is not a failed search but
    a dead run:

        typing 'Gijó' (prefix of 'Gijón')
        ADB error: java.lang.NullPointerException: Attempt to get length of null array

    Live 2026-09-09, one leg short of Gijón with the Pig aboard.

    CUTTING, NEVER STRIPPING. The game filters on ITS spelling, which keeps the accent, so
    `Gijo` matches nothing — character four is `ó`, not `o`. The longest run of ASCII inside
    the name is both typeable and a true substring of what the game holds:

        Gijón -> 'Gij'      Málaga -> 'laga'     Ávila -> 'vila'
        Malé  -> 'Mal'      Lübeck -> 'beck'     Mérida -> 'rida'

    A name with no ASCII in it at all yields '', and the caller scrolls instead — an empty
    query would CLEAR the filter, which is worse than never typing.
    """
    limit = _PREFIX_LEN if limit is None else limit   # defined below the class
    runs, current = [], ""
    for ch in str(name or ""):
        if ch.isascii():
            current += ch
        else:
            runs.append(current)
            current = ""
    runs.append(current)
    best = max(runs, key=len) if runs else ""
    return best.strip()[:limit]


class WorldMapActivity:
    """Read the tab, find the place, commit. One step per call."""

    name = "world_map"
    SERVES = ("world_map",)

    # The map is a full-screen overlay over the place the fleet is standing in, so the only
    # transition it starts is closing itself. Opening it from here is meaningless.
    CAN_START = ("CLOSE_WORLD_MAP",)

    # CLOSING THE MAP LANDS WHERE WE WERE, and nothing here knows where that was — the map is
    # an overlay over a place it does not record. Declaring no destination is the honest
    # answer: a route may not be planned THROUGH the map, only to it.
    LEADS_TO: dict = {}
    CONTEXT_STATES = _ctx.CONTEXT_STATES
    GOALS: tuple = (ChooseDestination, RemoteCheck, ReadEventSchedule, MoveViaLocationInfo)

    _TAB_FOR = {"port": "port", "village": "explore", "route": "route"}

    _tick_frame = None
    _list_taps = 0
    _free_taps = 0
    _list_opened = False

    def __init__(self, *, context_fn=None, tab_fn=None, require_tab_fn=None,
                 find_fn=None, tap_fn=None, type_fn=None, scroll_fn=None,
                 scroll_panel_fn=None, kb_up_fn=None, kb_dismiss_fn=None,
                 commit_fn=None, panel_for_fn=None, capture_fn=None, open_list_fn=None,
                 read_base_fn=None, switch_tab_fn=None, read_screen_fn=None,
                 merge_fn=None, complete_fn=None, read_events_fn=None,
                 village_list_open_fn=None, port_list_open_fn=None, back_fn=None) -> None:
        self._read_events_fn = read_events_fn
        self._village_list_open_fn = village_list_open_fn
        self._port_list_open_fn = port_list_open_fn
        self._back = back_fn
        self._open_list_fn = open_list_fn
        self._read_base_fn = read_base_fn
        self._switch_tab_fn = switch_tab_fn
        self._read_screen_fn = read_screen_fn
        self._merge_fn = merge_fn
        self._complete_fn = complete_fn
        self._context_fn = context_fn
        self._tab = tab_fn
        self._require_tab = require_tab_fn
        self._find = find_fn
        self._tap = tap_fn
        self._type = type_fn
        self._scroll = scroll_fn
        self._scroll_panel = scroll_panel_fn
        self._kb_up_fn = kb_up_fn
        self._kb_dismiss_fn = kb_dismiss_fn
        self._kb_clears = 0
        self._kb_hygiene = 0
        self._last_rail_sig = None
        self._commit = commit_fn
        # Injectable like every other collaborator here: the default READS THE
        # SCREEN, so tests must be able to answer it without one.
        self._panel_for = panel_for_fn
        self._capture = capture_fn
        self._typed = 0            # typings that LANDED in the box
        self._shortenings = 0      # a filter that came back empty, retyped shorter
        self._panel_closes = 0     # another place's panel, closed so the map
                                   # can be searched — bounded like the rest
        self._type_attempts = 0    # typings SENT — the backstop against a
                                   # keyboard that never accepts anything
        self._pending_query = None
        self._scrolls = 0
        # The remote check accumulates ACROSS TICKS — one screen read, one scroll, repeat.
        # It belongs to this goal, not to the activity: the registry reuses one instance, so
        # screens kept across goals would merge one village's list into another's.
        self._screens: list = []
        self._base: dict = {}
        self._goal_key: Optional[tuple] = None

    # ── the one entry point ──────────────────────────────────────────────────
    def work(self, goal: Any, state: Any) -> ActivityResult:
        # The tick's frame, for the helpers that take no state. Cleared implicitly each
        # tick because it is set each tick.
        self._tick_frame = getattr(state, "frame", None)
        where = getattr(state, "state", None) or getattr(state, "location", None)
        if where is not None and where not in self.SERVES:
            return ActivityResult(UNRECOGNISED, {"state": where},
                                  detail=f"not on the world map ({where!r})")
        # Check against GOALS, not one class — the world map serves several errands and a
        # second one was added the moment `RemoteCheck` arrived. Naming a single class here
        # is the same drift `SERVES`/`GOALS` exist to prevent.
        if not isinstance(goal, tuple(self.GOALS)):
            return ActivityResult(BLOCKED, {}, detail=f"the world map cannot serve {goal!r}")

        # The attempt counters belong to THIS destination. The registry reuses one activity,
        # so a count kept across goals would report the previous search's attempts as this
        # one's (Guiding Principle #4 — a datum outliving its owner).
        key = (goal.where, goal.kind)
        if key != self._goal_key:
            self._goal_key, self._typed, self._scrolls = key, 0, 0
            self._type_attempts, self._pending_query = 0, None
            self._kb_clears = 0
            self._panel_closes = 0
            self._shortenings = 0
            self._kb_hygiene = 0
            self._last_rail_sig = None
            self._list_taps = 0
            self._free_taps = 0
            self._list_opened = False
            self._screens, self._base = [], {}

        # THE TAB BEFORE THE RAIL. The rail belongs to whichever tab is lit, so touching it
        # first is reading another list's rows.
        wanted_tab = self._TAB_FOR.get(goal.kind, "port")
        if not self._ensure_tab(wanted_tab, goal):
            return ActivityResult(BLOCKED, {"tab": wanted_tab},
                                  detail=f"could not select the {wanted_tab!r} tab — refusing "
                                         "to read another tab's rail")

        # THE KEYBOARD IS PUT AWAY BEFORE THE CONTEXT IS DECIDED (user, 2026-09-03).
        #
        # It was first placed inside `_on_list`, which was too late — that path is never
        # reached, because the keyboard defeats the CONTEXT CLASSIFIER itself. `_village_list_open`
        # reads the left panel, the keyboard covers it, so an OPEN list reads as `map_open`:
        #
        #     11:07:59  destination_list -> _on_list      types 'San ', keyboard comes up
        #     11:08:19  map_open -> _on_map               "not on screen — opening the list"
        #     11:08:31  map_open -> _on_map               ...and the rail tap TOGGLES IT SHUT
        #
        # San Village was in that list the whole time — the user watched the typing land and
        # the row appear — and the bot closed the list instead of tapping it, twice over.
        # A tap issued then is consumed dismissing the IME rather than selecting anything.
        #
        # So it is asked here, before `where` is turned into a context and before anything is
        # read or tapped.
        #
        # LOOK AT THE LIST WHEN THE KEYBOARD IS GONE (user, 2026-09-03). This used to be
        # conditioned on the TYPING BUDGET as well, and that left a hole in precisely the
        # state the guard exists for: typing SUCCEEDS, `_typed` reaches its max, the keyboard
        # is still up over the list it just filtered — and the guard switches off. `_on_list`
        # then looks (line ~341) at rows the IME is covering, finds nothing, and falls through
        # to scrolling the map for a port sitting in plain view. Whether we may type again has
        # no bearing on whether we can SEE; the budget belonged to the retype, not to the look.
        #
        # `_kb_hygiene` alone bounds it, so the poisoned-query recovery in `_on_list` — which
        # dismisses AND resets the attempts — is still reached once these are spent.
        if self._kb_hygiene < _MAX_KB_HYGIENE and self._keyboard_is_up():
            self._kb_hygiene += 1
            logger.info("[world_map] the soft keyboard is up — putting it away before "
                        f"deciding what this screen is ({self._kb_hygiene}/{_MAX_KB_HYGIENE}); "
                        "it covers the list, so an open list reads as a bare map")
            self._dismiss_keyboard()
            return ActivityResult(WORKING, {"did": "put the keyboard away"}, detail=str(goal))

        local = self._classify(state)
        # Remember that the rail opened at all: the icon that produced it is settled, and a
        # later failure must not send the search back to a different icon.
        if local in (_ctx.DESTINATION_LIST, _ctx.ROUTE_LIST):
            self._list_opened = True

        entry = self._HANDLERS.get(local)
        if entry is None:
            return ActivityResult(UNRECOGNISED, {"state": where, "context": local},
                                  detail=f"no world-map screen matched ({local})")
        handler, serves = entry

        # THIS SCREEN DOES NOT ANSWER THIS ERRAND — SO DO NOT ACT ON IT.
        #
        # A context the errand cannot use is one of two things, and from here they look the
        # same: a screen a stray tap opened, or a MISREAD. Tapping Back commits to the first
        # reading. When it is the second, the tap leaves the very screen the errand needed —
        # live 2026-08-29 a port's info panel read as EVENT_SCHEDULE and the back-out walked
        # off the panel carrying Move, so the course could never be committed.
        #
        # The activity has no way to tell them apart, and it does not have to. It never
        # tapped the Trade Event button, so it has no business handling that dialog either
        # way. Hand back instead: UNRECOGNISED ends the attempt now and the dispatcher
        # regains bearings with a fresh perceive, which is where an unexpected screen is
        # someone's job (memory: lost-means-reorient-not-reread).
        if not isinstance(goal, serves):
            logger.info(f"[world_map] {local} does not answer {goal!r} — lost, handing back")
            return ActivityResult(UNRECOGNISED,
                                  {"state": where, "context": local},
                                  detail=f"{local} cannot serve {goal} — not acting on it")

        logger.info(f"[world_map] {local} -> {handler.__name__}")
        return handler(self, goal)

    # ── one handler per context state ────────────────────────────────────────
    def _on_map(self, goal: ChooseDestination) -> ActivityResult:
        """LOOK FIRST. If the place is already drawn on the map, tap it — no rail, no
        keyboard, no state change. This is the step the port path never had.

        BUT ONLY ONCE. The free read scans the whole map, and the map is covered in text that
        is not a port: live 2026-08-29 something at (1484,494) matched 'Amsterdam', and
        tapping it opened the EVENT SCHEDULE. The screen-goal guard caught that and backed
        out — and the next tick read the same frame, found the same thing, and tapped it
        again.

        A look that does not pay off is not evidence to look harder. After one unrewarded tap
        the rail is used instead, which is searchable and cannot be mistaken for scenery.
        """
        found = self._find_on_screen(goal.where, goal.kind) if self._free_taps < _MAX_FREE_TAPS \
            else None
        if found:
            self._free_taps += 1
            logger.info(f"[world_map] {goal.where!r} is already on screen @ {found} — tapping")
            self._tap_at(*found)
            return ActivityResult(WORKING, {"did": f"tapped {goal.where} on the map"},
                                  detail=str(goal))
        # A QUERY WE TYPED THAT FOUND NOTHING IS A WRONG QUERY, NOT A CLOSED LIST.
        #
        # The game filters on ITS spelling and we type OURS, and the two differ wherever the
        # name carries an accent: the port is `Gijón`, our KB and the map OCR both flatten it
        # to `Gijon`, and the prefix `Gijo` matches nothing because character four is `ó`.
        # The list then holds ZERO rows — and an empty list looks exactly like no list, so
        # this branch re-opened it, which TOGGLES the rail shut. Live 2026-09-06:
        #
        #     typing 'Gijo' (prefix of 'Gijon') — attempt 1/4, 0/2 landed
        #     'Gijon' is not on screen — opening the list          (x3)
        #     NOTHING CHANGED for 3 ticks ... choose port 'Gijon'
        #
        # The box read `gijo` with the rows below it empty and the map showing through. Every
        # "is the list open?" check says no, because they all look for the search FIELD and
        # the parse returned only the 58px word we typed — undetectable exactly when it
        # matters. So the screen cannot answer this one; our own RECORDED INTENT can. We know
        # we typed, and we know nothing came back.
        #
        # Shortening is the remedy because a shorter prefix is a SUPERSET: it cannot exclude
        # the destination, only filter less. An accent can sit anywhere in a name — `Málaga`
        # fails at two, `Ávila` at one — so this walks down rather than guessing a safe length.
        if self._typed and self._shortenings < _MAX_SHORTENINGS:
            shorter = _keyable_query(goal.where,
                                     limit=max(1, _PREFIX_LEN - self._shortenings - 1))
            self._shortenings += 1
            logger.info(f"[world_map] the filter for {goal.where!r} came back empty — its "
                        f"name is spelled differently in the game (an accent, most likely), "
                        f"so retyping the shorter {shorter!r} "
                        f"({self._shortenings}/{_MAX_SHORTENINGS})")
            self._pending_query = shorter
            self._typed = self._type_attempts = 0
            self._type_prefix(shorter)
            return ActivityResult(WORKING, {"did": f"typed {shorter!r}"}, detail=str(goal))

        logger.info(f"[world_map] {goal.where!r} is not on screen — opening the list")
        self._open_list(goal)
        return ActivityResult(WORKING, {"did": "opened the destination list"}, detail=str(goal))

    def _settle_last_typing(self) -> None:
        """Count the previous typing only if the box now holds it. Never raises."""
        prefix, self._pending_query = self._pending_query, None
        if not prefix:
            return
        try:
            from actions.sail_actions import search_box_holds
            landed = search_box_holds(prefix, self._frame())
        except Exception as exc:                   # noqa: BLE001 — unknown is not "landed"
            logger.debug(f"[world_map] could not read the search box: {exc}")
            return
        if landed:
            self._typed += 1
            logger.info(f"[world_map] the box holds {prefix!r} — the search is filtered")
        else:
            logger.info(f"[world_map] {prefix!r} never reached the box — not spending the "
                        "attempt on a typing that did not land")

    def _on_list(self, goal: ChooseDestination) -> ActivityResult:
        """The rail is open. Make sure it is OURS, then look, then type, then scroll.

        A RAIL IS NOT THE RAIL WE WANT. The Explore tab has four icons and each opens its own
        list; a village goal needs the second one. Looking at whatever happens to be open is
        how the bot read its own leftover query out of another list's search box and called it
        the village (live 2026-08-29). Which list is open is checked BEFORE its contents are
        believed — the same order as reading the tab before the rail.
        """
        # BOTH LISTS ARE VERIFIED, not just the village one (user, 2026-09-01). This asked
        # "is the village list open?" for a village goal and took the classifier's word for a
        # port goal — so when a bare map was wrongly called `destination_list`, the port path
        # went straight to reading and typing into a list that was not there, and the prefix
        # landed on the MAP at frames 100 and 103. The village branch existed because that
        # case had bitten once; the port one had not yet.
        if not self._list_is_open_for(goal.kind):
            logger.info(f"[world_map] the {goal.kind} list is not open — opening it before "
                        "reading it")
            self._open_list(goal)
            return ActivityResult(WORKING, {"did": f"opened the {goal.kind} list"},
                                  detail=str(goal))

        found = self._find_on_screen(goal.where, goal.kind, in_list=True)
        if found:
            logger.info(f"[world_map] {goal.where!r} is in the open list @ {found} — tapping "
                        "without searching")
            self._tap_at(*found)
            return ActivityResult(WORKING, {"did": f"tapped {goal.where} in the list"},
                                  detail=str(goal))

        # A TYPING IS SPENT WHEN THE QUERY IS IN THE BOX, not when the tap was sent (user,
        # 2026-09-01). `self._typed += 1` recorded that we had PERFORMED a type — a stored
        # conclusion — and nothing ever asked whether the text arrived. Live 2026-09-01 the
        # bare map was mistaken for an open list, both attempts were typed INTO THE MAP at
        # frames 100 and 103, and when the real list finally opened at frame 108 the budget
        # was gone: the search could only scroll, six times, and never reached Barcelona.
        #
        # Typing is the recovery, so burning it on a fumbled tap disables the remedy exactly
        # when something has already gone wrong. The box shows the query, so this is a
        # question the screen answers.
        self._settle_last_typing()
        # A QUERY THAT IS ALREADY IN THE BOX IS NOT TYPED AGAIN (user, 2026-09-03).
        #
        # `_type_prefix` taps the search field and types with clear_first, so retyping WIPES
        # the box and un-filters the list — undoing the very filter that was about to show the
        # destination. Live 2026-09-03 hunting Faro:
        #
        #     11:54:27  'Faro' not found      -> typing (1/4, 0/2 landed)
        #     11:54:53  typing (2/4, 1/2 landed)   the query had LANDED; this cleared it
        #     11:56:54  'Faro' is in the open list @ (209,200) — tapping
        #
        # It got there in the end, two minutes and a wasted filter later. `_MAX_TYPED` allowed
        # a second typing whether or not the first worked, and "did it work?" is a question
        # the box answers — `search_box_holds` is already how `_settle_last_typing` counts it.
        #
        # So: look, and if the filter is already applied, look again or scroll. Never retype a
        # query that is sitting in the box.
        already = False
        try:
            from actions.sail_actions import search_box_holds
            already = search_box_holds(goal.where[:_PREFIX_LEN], self._frame())
        except Exception as exc:                   # noqa: BLE001 — unknown is not "already"
            logger.debug(f"[world_map] could not read the search box: {exc}")
        if already:
            logger.info(f"[world_map] the box already holds the query for {goal.where!r} — "
                        "the list is filtered; not retyping, which would clear it")
        if (not already and self._typed < _MAX_TYPED
                and self._type_attempts < _MAX_TYPE_ATTEMPTS):
            self._type_attempts += 1
            prefix = _keyable_query(goal.where)
            if not prefix:
                logger.info(f"[world_map] nothing in {goal.where!r} can be typed — "
                            "scrolling the list instead")
                self._open_list(goal)
                return ActivityResult(WORKING, {"did": "opened the destination list"},
                                      detail=str(goal))
            logger.info(f"[world_map] typing {prefix!r} (of {goal.where!r}) "
                        f"— attempt {self._type_attempts}/{_MAX_TYPE_ATTEMPTS}, "
                        f"{self._typed}/{_MAX_TYPED} landed")
            self._pending_query = prefix
            self._type_prefix(prefix)
            return ActivityResult(WORKING, {"did": f"typed {prefix!r}"}, detail=str(goal))

        # A SWIPE OVER THE SOFT KEYBOARD IS TYPING, NOT SCROLLING. This branch used to say
        # "the keyboard may be in the way" and then swipe anyway. On a phone the keyboard
        # covers the lower half, so (300,700) lands on the keys and GLIDE-TYPES: live
        # 2026-08-30 the search box went from 'gijo' to 'gijo aww awwawwaww', one 'aww' per
        # scroll. Every retry made the query worse, so the port could never surface and the
        # leg ended "looked, typed and scrolled" having sabotaged its own search.
        #
        # The keyboard is not something to work around by feel — the OS says whether it is
        # up. Put it away, and retype: `_type_search_prefix` clears the box, which is what
        # removes the junk already in it.
        if self._kb_clears < _MAX_KB_CLEARS and self._keyboard_is_up():
            self._kb_clears += 1
            logger.info(f"[world_map] the soft keyboard is up — putting it away before "
                        f"looking again ({self._kb_clears}/{_MAX_KB_CLEARS}); a swipe over it "
                        f"would type, not scroll")
            self._dismiss_keyboard()
            # The query may be poisoned; a retype clears the box. Reset the ATTEMPTS too —
            # keeping them would leave the retype unable to run, which is the whole point of
            # having put the keyboard away.
            self._typed = self._type_attempts = 0
            self._pending_query = None
            return ActivityResult(WORKING, {"did": "put the keyboard away"}, detail=str(goal))

        # A SWIPE THAT MOVED NOTHING IS THE END OF THE LIST, NOT A REASON TO SWIPE AGAIN
        # (user, 2026-09-03). This branch used to swipe and report the fixed string "scrolled
        # the list" — so it could not tell a list that was paging from one that was not moving,
        # and the report carried nothing that changed. Live 2026-09-03 it swiped five times at
        # Faro and the stall guard ended the mission for three indistinguishable ticks.
        #
        # The building list has had this test since 2026-08-19 and `explore_port` has its own
        # copy; the live path was the one without it. Now all three ask
        # `actions.ui.lists.signature`. This activity does one action per tick, so it compares
        # ACROSS ticks rather than looping.
        sig = self._rail_signature()
        if sig is not None and sig == self._last_rail_sig:
            logger.info(f"[world_map] the rail did not move on the last swipe — "
                        f"{goal.where!r} is not in the {goal.kind} list")
            return ActivityResult(BLOCKED, {"where": goal.where},
                                  detail=f"{goal.where!r} is not in the {goal.kind} list — "
                                         "the list stopped moving")
        if self._scrolls < _MAX_SCROLLS:
            self._scrolls += 1
            logger.info(f"[world_map] the search did not surface {goal.where!r} — scrolling "
                        f"({self._scrolls}/{_MAX_SCROLLS})")
            self._last_rail_sig = sig
            self._scroll_list()
            # The count goes in the report so consecutive scrolls are DISTINGUISHABLE: the
            # stall guard compares what a step amounted to, and "scrolled the list" every time
            # reads as a step that changed nothing.
            return ActivityResult(WORKING, {"did": f"scrolled the list ({self._scrolls})"},
                                  detail=str(goal))

        return ActivityResult(BLOCKED, {"where": goal.where},
                              detail=f"{goal.where!r} is not in the {goal.kind} list — "
                                     "looked, typed and scrolled")

    def _on_panel(self, goal: ChooseDestination) -> ActivityResult:
        """A destination is selected and the commit control is showing. Tap it and finish —
        where that leaves us is the next perceive's business, not a claim to make here."""
        if not self._commit_departure(goal.where):
            return ActivityResult(BLOCKED, {"where": goal.where},
                                  detail="the destination panel offered no commit control")
        return ActivityResult(FINISHED, {"where": goal.where, "kind": goal.kind},
                              detail=f"committed to {goal.where}")

    def _on_route_list(self, goal: ChooseDestination) -> ActivityResult:
        """Saved routes are a list like any other — the same look/type/scroll ladder."""
        return self._on_list(goal)

    def _on_village_info_base(self, goal) -> ActivityResult:
        """Base carries amity and the day's remaining rounds. Read it, then switch to Barter.

        Read BEFORE switching, not after: the Base tab is the only place the rounds appear,
        and coming back for them would mean a second trip through the panel.
        """
        # A COURSE CAN BE SET FROM HERE. Tapping a village in the rail opens THIS panel, and
        # it carries the gold 'Move to Village' button — so for a `ChooseDestination` this
        # screen is not a wrong turn, it is the destination panel under another name.
        #
        # Live 2026-08-29: with the materials all aboard, the fleet found Svear Village in
        # the list, tapped it, and then handed back three times running because
        # `village_info_base` did not answer `ChooseDestination` — the mission stopped one
        # tap short of the village it had spent an hour gathering for. The city path had
        # already been given both errands; the village path was never carried across.
        if isinstance(goal, ChooseDestination):
            return self._on_location_info(goal)
        if not isinstance(goal, RemoteCheck):
            return ActivityResult(BLOCKED, {}, detail="Village Info is open but the goal is "
                                                      "not a remote check")
        if not self._base:
            self._base = self._read_base() or {}
            logger.info(f"[world_map] {goal.village}: base tab reads {self._base}")
        self._switch_to_barter()
        return ActivityResult(WORKING, {"did": "read the base tab"}, detail=str(goal))

    def _write_back(self, goal, trades) -> None:
        """Persist what a COMPLETE remote read learned, exactly as the on-site check does.

        The remote path never did. `village_check.write_back_invariants` is called when the
        fleet reads a village it is standing in; a read taken from the world map produced the
        same knowledge and threw it away — so `_run_mission_for`'s `load_recipe`, whose own
        comment says "written back by the check moments ago", found nothing.

        Live 2026-09-04, the first mission for a good the KB had never seen: Berber Village
        read cleanly (Argan Oil, 651 from 73 Myrrh + 146 Mutton + 146 Almond), the plan was
        built from it — 7 rounds, 2,940 units of material — and the run then died on

            FAILED at step plan: no recipe for 'Argan Oil' even after the check wrote back

        with recipes.json still holding the same thirteen goods it started with. Every run
        against an unknown good would have failed the same way, and every run against a known
        one hid it, because the KB already had what the write-back would have added.

        ONLY ON A COMPLETE READ. A partial list is refused above and must not be persisted
        either — materials are invariant, so a short read is a reading failure, and writing it
        would teach the KB a recipe with an ingredient missing.

        Never fatal: this is bookkeeping about a reading that has already succeeded, so a
        failure here is logged and the check still stands.
        """
        try:
            from actions.village_check import write_back_invariants
            write_back_invariants(self._as_check(goal, trades))
        except Exception as exc:                       # noqa: BLE001 — bookkeeping only
            logger.warning(f"[world_map] {goal.village}: could not write the check back to "
                           f"the KB ({exc}) — the mission still has this reading")

    def _as_check(self, goal, trades):
        """The duck `write_back_invariants` reads: trades, village and the Base tab."""
        from types import SimpleNamespace
        base = dict(self._base or {})
        used, total = base.get("barters_used"), base.get("barters_total")
        remaining = None
        if used is not None and total is not None:
            remaining = max(0, int(total) - int(used))
        return SimpleNamespace(
            village=goal.village, trades=list(trades),
            # SOURCE PORTS ARE NOT SOMETHING A REMOTE READ CAN KNOW — they are learned at
            # markets, not from the village panel. Empty is honest, and `write_back_invariants`
            # already falls back to whatever the KB holds rather than clearing it.
            sources={},
            amity_grade=base.get("amity_grade"),
            # `write_back_invariants` reads amity_points as a SEQUENCE — `(pts or (None,))[0]`.
            amity_points=((base.get("amity_points"),)
                          if base.get("amity_points") is not None else None),
            barters_used=used, barters_total=total, rounds_remaining=remaining)

    def _on_village_info_barter(self, goal) -> ActivityResult:
        """ONE SCREEN OF THE TRADE LIST PER TICK, then one scroll.

        `read_village_barter_remote` did this in a `for i in range(max_scrolls)` loop. Here
        the screens accumulate on the goal and the dispatcher does the looping, so an
        interruption mid-list is cleared by whoever owns it rather than being read through —
        which is how a partial list came to be certified complete in the first place.
        """
        # Same panel, other tab — and the Move button belongs to the panel, not the tab.
        if isinstance(goal, ChooseDestination):
            return self._on_location_info(goal)
        if not isinstance(goal, RemoteCheck):
            return ActivityResult(BLOCKED, {}, detail="the trade list is open but the goal "
                                                      "is not a remote check")

        # THE PANEL DOES NOT ALWAYS OPEN ON BASE. `_on_village_info_base` reads the rounds and
        # then switches here, so this assumed the rounds were already in hand — but the panel
        # reopens on whichever tab it was last left on, and a previous run left it on Barter.
        # The trade list then read perfectly, the rounds were never read at all, and the
        # mission died on "Daily Barter Progress unreadable" holding a complete recipe (live
        # 2026-08-29).
        #
        # The rounds live ONLY on the Base tab, so go and get them rather than finish without
        # them. One action, and the next tick comes back here.
        if not self._base:
            logger.info(f"[world_map] {goal.village}: the panel opened on Barter and the "
                        "day's rounds have not been read — going to the Base tab first")
            self._switch_to_base()
            return ActivityResult(WORKING, {"did": "switched to the base tab"},
                                  detail=str(goal))

        screen = self._read_trade_screen()
        if screen is not None:
            self._screens.append(screen)

        if self._read_is_complete(goal):
            trades = self._merge_screens()
            logger.info(f"[world_map] {goal.village}: read complete after "
                        f"{len(self._screens)} screen(s)")
            self._write_back(goal, trades)
            return ActivityResult(FINISHED,
                                  {"village": goal.village, "trades": trades,
                                   "base": dict(self._base),
                                   "screens": len(self._screens)},
                                  detail=str(goal))

        if self._scrolls >= _MAX_LIST_SCROLLS:
            # A PARTIAL READ IS SAID SO, NEVER PASSED OFF AS COMPLETE. A recipe does not lose
            # an ingredient — fewer materials than known means the read fell short, and
            # certifying it is how a two-of-three recipe was planned from for five runs.
            return ActivityResult(BLOCKED,
                                  {"village": goal.village, "trades": self._merge_screens(),
                                   "base": dict(self._base), "partial": True},
                                  detail=f"the trade list did not complete in "
                                         f"{self._scrolls} scrolls — PARTIAL")

        self._scrolls += 1
        if not self._scroll_trade_list():
            return ActivityResult(BLOCKED,
                                  {"village": goal.village, "trades": self._merge_screens(),
                                   "base": dict(self._base), "partial": True},
                                  detail="the trade list is open but its panel could not be "
                                         "located, so there is nothing to scroll")
        return ActivityResult(WORKING, {"did": f"scrolled the trade list "
                                               f"({self._scrolls})"}, detail=str(goal))

    def _on_event_schedule(self, goal) -> ActivityResult:
        """Read the schedule and finish. WHICH event to act on is the task runner's."""
        events = self._read_events()
        logger.info(f"[world_map] the schedule lists {len(events)} event(s)")
        return ActivityResult(FINISHED, {"events": events}, detail=str(goal))

    def _on_location_info(self, goal) -> ActivityResult:
        """The pin's own panel. Its gold Move button is what sails — the pin only opened it.

        WHOSE PANEL IS IT? The commit control is found by its own label, "Go to City", which
        says what it does and nothing about where. Live 2026-09-01 the map could not find
        Barcelona, taps meant for the port list opened FARO's panel, and the commit went in on
        that — the right button, the wrong city. The game asked "Moving to Faro after Auto
        Supply. Continue?", and the mission believed its course was set for Barcelona.
        """
        where = getattr(goal, "where", None)
        for_us = self._panel_is_for(where)

        # A BARE `Move` NAMES NOTHING, SO UNKNOWN IS A REFUSAL HERE (live 2026-09-05).
        #
        # "Unknown is not no" is right for a CITY: its panel is the city's, the control says
        # "Go to City", and refusing an unreadable panel would strand a correct departure.
        # A saved route has neither — its commit control says only `Move`, so if the panel
        # cannot be confirmed as ours then NOTHING on the screen ties the tap to the
        # destination, and pressing it sails wherever the panel already pointed.
        #
        # At Hutu the route list was opened over route 2's panel. `location_panel_is_for`
        # crops the CITY panel's region and matches the destination's FIRST WORD — 'Sans' of
        # 'Sans to London' — so on a route panel it read nothing, returned None, and the bare
        # Move went in. The fleet sailed 'sailing route 2': ETA 39 days on 6 days of supply,
        # and the activity reported FINISHED for 'Sans to London'.
        kind = getattr(goal, "kind", "port")
        if kind == "route" and for_us is not True:
            logger.warning(f"[world_map] the open panel cannot be confirmed as the route "
                           f"{where!r} (reads {for_us!r}) — a bare 'Move' names nothing, so "
                           "this would commit whichever route is already selected")
            return ActivityResult(BLOCKED, {"where": where, "why": "route not confirmed"},
                                  detail=f"cannot confirm the open panel is {where!r}")
        if for_us is False:
            # NOT ours. Sailing from here goes somewhere nobody chose — but REFUSING IS NOT
            # ENOUGH, because nothing else closes it.
            #
            # Live 2026-09-06, the Seville leg. A stale Marseille panel was already open when
            # the activity took control; this branch reported BLOCKED, `sail_runner` counted
            # the attempt and dispatched again, met the IDENTICAL screen, and the leg died
            # after two:
            #
            #     the open Location Info panel is not 'Seville' — not committing
            #     could not set a course for 'Seville' after 2 attempts
            #
            # "Hand back and let the map be searched again" was the intent, and it does not
            # follow from handing back: the search cannot run while the panel covers it. So
            # take the ONE action that changes the screen — close it — and hand back, which
            # is the pattern this activity exists to follow. The map's own state dies with it
            # (`memory/world-map-state-dies-with-the-map`), so nothing is lost by closing.
            if self._panel_closes < _MAX_PANEL_CLOSES:
                self._panel_closes += 1
                logger.warning(f"[world_map] the open Location Info panel is not {where!r} — "
                               f"closing it so the map can be searched "
                               f"({self._panel_closes}/{_MAX_PANEL_CLOSES})")
                self._close_panel()
                return ActivityResult(WORKING, {"where": where,
                                                "did": "closed another place's panel"},
                                      detail=f"Location Info was open for another place")
            logger.warning(f"[world_map] the Location Info panel is still not {where!r} after "
                           f"{_MAX_PANEL_CLOSES} closes — reporting rather than tapping on")
            return ActivityResult(BLOCKED, {"where": where, "why": "panel is another place"},
                                  detail=f"Location Info is open, but not for {where!r}")
        if not self._commit_departure(where):
            return ActivityResult(BLOCKED, {"where": where},
                                  detail="Location Info is open but has no Move button")
        return ActivityResult(FINISHED, {"where": where,
                                         "via": "location_info"}, detail=str(goal))

    def _close_panel(self) -> None:
        """Close whatever panel is over the map, through the seam already declared.

        `back_fn` has been a constructor argument with no caller since the activity was
        written; this is the case it was for. On the world map Back closes the panel, and if
        it closes the MAP too that is equally fine — the map reopens on the Port tab and
        holds no state worth keeping.
        """
        if self._back is not None:
            self._back()
            return
        from actions import ui
        ui.back(why="a Location Info panel for another place is covering the map")


    def _panel_is_for(self, where) -> Optional[bool]:
        """Whether the open panel belongs to `where`. None when it cannot be read — unknown
        is not "no", and refusing on an unreadable panel would strand a correct departure."""
        if self._panel_for is not None:
            return self._panel_for(where)
        if not where:
            return None
        from actions.sail_actions import location_panel_is_for
        return location_panel_is_for(self._frame(), where)

    # ── helpers, all injectable: the defaults CAPTURE THE SCREEN ─────────────
    def _classify(self, state) -> str:
        if self._context_fn is not None:
            return self._context_fn(state)
        frame = self._frame(state)
        return _ctx.classify(frame)

    def _frame(self, state=None):
        """The frame for THIS tick — the one the dispatcher classified, wherever possible.

        Most callers here have no `state` to pass, so without the per-tick copy they each
        captured a fresh screen: several captures inside one tick, every one of them a
        DIFFERENT screen from the one that routed us here. The copy is set at the top of
        `work()` and belongs to the tick, the way `_goal_key` belongs to the goal.
        """
        got = getattr(state, "frame", None) if state is not None else self._tick_frame
        if got is not None:
            return got
        if self._capture is not None:
            return self._capture()
        from capture.adb_capture import capture_screen
        return capture_screen()

    def _ensure_tab(self, tab: str, goal) -> bool:
        if self._require_tab is not None:
            return bool(self._require_tab(tab))
        from actions.sail_actions import active_world_map_tab, require_world_map_tab
        # Pass the tick's frame: the guard READS which tab is live, and reading it from a
        # screen taken after the one we were routed on is how a guard ends up answering about
        # a different moment than the decision it guards.
        frame = self._frame()
        was_already_lit = active_world_map_tab(frame) == tab
        ok = bool(require_world_map_tab(tab, why=f"the {goal.kind} list lives on it",
                                        frame=frame))

        # SWITCHING TABS IS A SCREEN CHANGE, SO THE TICK'S FRAME IS SPENT (live 2026-09-05).
        #
        # `_tick_frame` exists so every reader in one tick answers about the SAME screen —
        # right, until something in that tick CHANGES the screen. This runs before the
        # context is classified and before any handler, so after a switch the whole rest of
        # the tick was reading the tab we just left.
        #
        # At Hutu, taking the route home: the tick opened on the PORT tab, this switched to
        # Route, and everything downstream still read the port screen. The classifier called
        # it MAP_OPEN instead of ROUTE_LIST; `_find_on_screen` OCR'd 32 tokens with no route
        # in them and reported 'Sans to London' not on screen (it was on screen, and matches
        # fuzzily — the frame was simply the wrong one); `_open_list` then took the PORT
        # rail's icon at (69,170) and tapped it into the route list that had appeared, where
        # that point is the divider between 'Sailing Route 2' and 'san to london'. It
        # selected the row above, the bare Move committed it, and the fleet sailed a 39-day
        # route on 6 days of supply.
        #
        # Dropping the frame costs one capture and only when a switch actually happened.
        if ok and not was_already_lit:
            logger.info("[world_map] the tab changed, so the tick's frame is spent — "
                        "re-reading rather than deciding from the tab we just left")
            self._tick_frame = None
        return ok

    def _find_on_screen(self, where: str, kind: str = "port", *, in_list: bool = False):
        """(cx, cy) of `where` if it is already visible, else None.

        WHICH READER DEPENDS ON WHAT WE ARE LOOKING FOR. `_find_port_on_world_map` indexes
        PORTS, so asking it for a village returns None however plainly the village is drawn —
        which is why "look first" appeared dead for villages. The village reader is
        `_visible_row`, which matches occluded and merged labels fuzzily: Svear Village sits
        beside a discovery marker that covers part of its name.

        `in_list` narrows the search to the left-hand rail. Scanning the whole frame matched
        a village's name in the right-hand Village Info PANEL and tapped that instead of the
        list row (live 2026-08-24, Svear at x=1961), which left the list unopened and the
        read returned the wrong good entirely.

        AND THE PORT READER RETURNS A PAIR — (position | None, the port names it could see).
        This took the pair for the position, and a miss is `(None, [...])`, which is truthy:
        a village that was not on screen "was found" and the tap went to x=None. Live
        2026-08-29: "'Svear Village' is already on screen @ (None, ['porto', 'copenhagen',
        ...])". The names in that line were the tell — every one a PORT, from a reader that
        does not index villages at all.
        """
        if self._find is not None:
            return self._find(where)
        try:
            if kind == "village":
                from actions.sail_actions import _VILLAGE_LIST_MAX_X, _visible_row
                from vision.omniparser import parse_fast_cached
                frame = self._frame()
                els = [e for e in parse_fast_cached(frame)
                       if not self._is_the_search_box(frame, e)]
                hit = _visible_row(els, where,
                                   x_max=_VILLAGE_LIST_MAX_X if in_list else None)
                return (hit.cx, hit.cy) if hit is not None else None
            from actions.sail_actions import _find_port_on_world_map
            # NARROWED TO THE RAIL WHEN READING A LIST — the same fix the village reader
            # already carries, and for the same reason. The list is on the LEFT; the panel on
            # the right shows whatever is selected, under the same name. Live 2026-08-29 the
            # port reader matched '# Amsterdam' at x=1974 in that panel, tapped it, landed on
            # the event schedule, and the course was never set.
            from actions.sail_actions import _VILLAGE_LIST_MAX_X
            frame = self._frame()
            pos, _seen = _find_port_on_world_map(
                frame, where, x_max=_VILLAGE_LIST_MAX_X if in_list else None)

            # NEVER MATCH YOUR OWN TYPING — on this path too. Having typed 'Amst' into the
            # box, the box now reads 'Amst', which matches 'Amsterdam' as surely as any row
            # does. The village reader excludes the field; this one did not, so it tapped the
            # search box at (255,199) and the list never opened a destination (live
            # 2026-08-29). The rule belongs to the SCREEN, not to one reader.
            #
            # A BACKSTOP SINCE 2026-09-03, because this was the rule applied at the WRONG
            # LAYER: it REJECTED THE ANSWER instead of EXCLUDING THE CANDIDATE. The box scores
            # 1.00 on our own typing and the row 0.62, so the box won the reader's `best_conf`
            # — and this guard then threw away the whole read, taking the real row down with
            # it. Hunting Faro that day the row was on screen at (209,200) the entire time and
            # `_find_on_screen` returned None, so the search fell through to swiping the map
            # for a port in plain view. `_find_port_on_world_map` now drops the box from the
            # candidates, which lets the row win; this stays as a second line of defence for
            # the case where the box is the only match.
            if pos is not None and self._is_the_search_box(
                    frame, _AtPoint(cx=pos[0], cy=pos[1])):
                logger.info(f"[world_map] the {where!r} match sits in the search box — that "
                            "is what we typed, not a result")
                return None
            return pos
        except Exception as exc:
            logger.debug(f"[world_map] free read failed: {exc}")
            return None

    def _list_is_open_for(self, kind) -> bool:
        """Is the list this goal needs actually on screen?

        The rail's identity is what is IN it, never which icon we believe we tapped — the
        icons carry no label, so that belief is the thing needing checking.

        CANNOT TELL IS NOT NO, the same answer the village check has always given: reading an
        unreadable frame as "not ours" reopens the list on every tick forever, and tapping the
        rail while a list is open toggles it SHUT (measured 2026-08-24). Both checks must
        agree on this, or a vision hiccup behaves oppositely depending on the goal's kind.
        """
        if kind == "village":
            return self._village_list_is_open()
        try:
            if self._port_list_open_fn is not None:
                return bool(self._port_list_open_fn())
            from actions.sail_actions import _port_list_open
            return bool(_port_list_open(self._frame()))
        except Exception as exc:                   # noqa: BLE001 — see above
            logger.debug(f"[world_map] port-list check failed: {exc}")
            return True

    def _village_list_is_open(self) -> bool:
        """Is the rail on screen the VILLAGE list, rather than some other list?

        Identified by what is in it — a search box with `<name> Village` rows beneath — not by
        which icon we believe we tapped. The icons carry no label, so belief about which one
        was tapped is exactly the thing that needs checking.
        """
        try:
            if self._village_list_open_fn is not None:
                return bool(self._village_list_open_fn())
            from actions.sail_actions import _village_list_open
            from vision.omniparser import parse_fast_cached
            frame = self._frame()
            return bool(_village_list_open(frame, list(parse_fast_cached(frame))))
        except Exception as exc:
            # CANNOT TELL IS NOT NO. Reading it as "not ours" would reopen the list on every
            # tick forever. The guard covers whichever implementation raised, because which
            # one it was is not the point.
            logger.debug(f"[world_map] village-list check failed: {exc}")
            return True

    def _is_the_search_box(self, frame, el) -> bool:
        # `el` needs only a `cy`; `_AtPoint` lets a bare coordinate be checked the same way an
        # element is, so both readers apply one rule rather than two copies of it.
        """NEVER MATCH YOUR OWN TYPING.

        The search box holds what we typed into it, so it fuzzy-matches the thing we are
        searching for BY CONSTRUCTION — and it is never the answer. Live 2026-08-29 a previous
        run left 'Svea' in the field; this one read it back as 'sveal', scored it 0.80 against
        'Svear Village', announced the village was already on screen, and tapped the search box
        instead. Tapping a text field changes nothing, so it did that until the budget ran out.

        The row is identified by POSITION, not by what it says — what it says is the problem.
        """
        # ONE TEST, SHARED. This band and `_find_port_on_world_map`'s exclusion were two
        # copies of the same rule and they disagreed — the reader's looked for the search
        # FIELD, which is only findable when the box is empty, so on 2026-09-06 it failed to
        # exclude a box holding 'gijo' and this backstop then discarded the real 'Gijon' with
        # it. Both now ask `sail_actions.is_the_search_box`, which answers by POSITION.
        try:
            from actions.sail_actions import is_the_search_box
            return is_the_search_box(frame, getattr(el, "cx", 0), el.cy)
        except Exception:
            return False

    def _tap_at(self, x, y) -> None:
        if self._tap is not None:
            self._tap(x, y)
            return
        from actions import ui
        ui.tap_at(int(x), int(y), why="world map: the destination we are looking for")

    def _open_list(self, goal=None) -> None:
        """Open the rail by the DETECTED icon, never a remembered point.

        `ChromeDetector`-style template positions and calibrated constants both go stale;
        worse, a learned one was persisted as `port_list_icon` after it opened the VILLAGE
        list, and because the saved value RANKED the candidate sweep it pulled later attempts
        back toward the same wrong icon. Nothing about the map is persisted now
        (memory: world-map-state-dies-with-the-map).

        WHICH ICON DEPENDS ON THE TAB (user, 2026-08-29): on Port the list is the FIRST icon
        in the rail, on Explore the village list is the SECOND. This asked for the topmost
        one unconditionally — a regression, because the path it replaced knew that and this
        did not. It tapped the wrong icon eight times running while the map never changed.

        The rail's icons carry no label at all — OmniParser returns four bare 'icon's — so
        there is no word to match on and position is the only identity available. Which is
        why the icon is tried IN ORDER OF LIKELIHOOD and the next tick judges it: if the list
        did not open, the context is still `map_open` and the next candidate is tried. The
        old path did the same thing in a loop with its own verification; here the loop is the
        dispatcher's and the verification is the next perceive.
        """
        if self._open_list_fn is not None:
            self._open_list_fn()
            return

        # THE ROUTE TAB HAS NO LIST ICON — ITS LIST *IS* THE TAB (user, 2026-09-05: "it
        # should not try to tap the Port icon when it is on another tab, because the port
        # list icon is only on the Port tab").
        #
        # The rail's icons belong to whichever tab is lit: index 0 opens the PORT list,
        # index 1 the Explore/village list. A route goal falls through to `else 0` and so
        # aims at the PORT icon — a control that does not exist on the Route tab. There the
        # saved routes are already listed, so that coordinate is over the LIST, and tapping
        # it cannot open anything; it can only select a row.
        #
        # Live 2026-09-05 it landed on the divider between 'Sailing Route 2' and 'san to
        # london', selected the row above, and the bare `Move` that appeared as a RESULT of
        # that selection was then taken for a destination panel and pressed. The fleet sailed
        # a 39-day route on 6 days of supply.
        #
        # Nothing to open means nothing to tap: hand back, and the next tick classifies the
        # list that is already there (ROUTE_LIST) and searches it by name.
        if (getattr(goal, "kind", None) or "port") == "route":
            logger.info("[world_map] the Route tab lists its routes itself — there is no list "
                        "icon to open, so looking again rather than tapping the port rail")
            return

        from actions.sail_actions import (_explore_left_icons, _RAIL_FALLBACK_POINTS,
                                          _VILLAGE_LIST_ICON_INDEX)

        icons = _explore_left_icons(self._frame())
        detected = bool(icons)
        if not icons:
            # NOTHING TO LOOK UP HERE, AND THAT IS THE MEASUREMENT, NOT A GUESS. The rail's
            # icons are only detectable once a list is open behind them; over the bare map
            # the parse returns nothing at their x at all. Refusing outright left the search
            # with no way in — live 2026-09-01 it declined every tick and the leg stalled.
            #
            # The rail is fixed chrome, so its position is knowable; and the tap is JUDGED
            # the same way a detected icon's is, by whether a list opened. See
            # `_RAIL_FALLBACK_POINTS`.
            icons = list(_RAIL_FALLBACK_POINTS)
            logger.warning("[world_map] no icon rail detected — the rail is invisible to the "
                           "parse over a bare map, so trying its CALIBRATED positions; the "
                           "next tick says whether a list opened")

        kind = getattr(goal, "kind", None) or "port"
        first = _VILLAGE_LIST_ICON_INDEX if kind == "village" else 0
        if len(icons) <= first:
            logger.warning(f"[world_map] no rail position for the {kind} list — handing back")
            return
        pick = icons[first]

        # THE SAME ICON, OR NOTHING (user, 2026-09-08: "if it can not see a list, it should
        # not try another icon, that is almost always wrong").
        #
        # This walked the rail: the goal's own icon first, then every other one in turn, on
        # the theory that they carry no label so only the next tick can judge them. But the
        # rail's icons are not interchangeable candidates for one thing — each opens a
        # DIFFERENT list, and the one we want is known from the goal. Tapping a second is not
        # a retry, it is asking a different question.
        #
        # Live 2026-09-08 (frames 31-33 of trace_barter_cmd_2026-09-08T00-43-13): the port
        # icon at (66,171) opened the port list correctly — frame 32 shows it, search box and
        # all — and the next tick failed to SEE it, so this moved on to the next position
        # (70,300), the goods icon, and threw the open list away. It then reported "attempt 4
        # of 2", the bound having no effect, and the leg stalled until the no-progress guard
        # ended the mission.
        #
        # So a list that will not open is reported, not worked around. Re-tapping the SAME
        # point is the one honest retry — the game drops about one tap in twenty — and past
        # that the screen is refusing rather than dropping.
        if self._list_opened:
            self._list_taps = 0
        self._list_taps += 1
        if self._list_taps > _MAX_LIST_TAPS:
            logger.warning(f"[world_map] the {kind} list did not open after "
                           f"{_MAX_LIST_TAPS} taps at {pick} — reporting rather than trying "
                           "another icon, which would open a different list")
            return
        logger.info(f"[world_map] opening the {kind} list — "
                    f"{'icon' if detected else 'CALIBRATED point'} {pick} "
                    f"(attempt {self._list_taps} of {_MAX_LIST_TAPS}); the rail's icons carry "
                    f"no label, so the next tick says whether it opened")
        self._tap_at(*pick)

    def _type_prefix(self, prefix: str) -> None:
        """Tap the search box and type the query, at a human interval (anti-cheat).

        `_keyable_query` decides WHAT to type; this only sends it. The guard stays because a
        non-ASCII character reaching the input service ends the run rather than the search.
        """
        if not prefix or not prefix.isascii():
            logger.warning(f"[world_map] refusing to type {prefix!r} — the keyboard cannot "
                           "send it, and an empty query would clear the filter")
            return
        if self._type is not None:
            self._type(prefix)
            return
        from actions.sail_actions import _map_search_box, _type_search_prefix
        box = _map_search_box(self._frame())
        _type_search_prefix(box, prefix)

    def _keyboard_is_up(self) -> bool:
        """Is the soft keyboard showing? THE OS KNOWS — do not infer it from pixels."""
        if self._kb_up_fn is not None:
            return bool(self._kb_up_fn())
        try:
            from actions.adb_actions import shell_out as _shell_out
            out = _shell_out(["shell", "dumpsys", "input_method"]) or ""
        except Exception as exc:
            logger.debug(f"[world_map] could not ask about the keyboard: {exc}")
            return False
        for line in out.splitlines():
            if "mInputShown=" in line:
                return line.split("mInputShown=")[1].strip().lower().startswith("true")
        return False

    def _dismiss_keyboard(self) -> None:
        """Back puts the IME away without leaving the screen underneath it."""
        if self._kb_dismiss_fn is not None:
            self._kb_dismiss_fn()
            return
        from actions.adb_actions import press_back as _press_back
        _press_back()

    # The rail's rows, as (label, x, y) — the shape `actions.ui.lists` works in. The crop is
    # the left panel, which is where this list lives; OCR of the whole frame would pick up the
    # map's own labels and the Village Info panel on the right.
    _RAIL_CROP = (0, 150, 650, 1080)

    def _rail_rows(self, frame=None):
        try:
            from actions.sail_actions import _ocr_frame
            fr = frame if frame is not None else self._frame()
            x0, y0 = self._RAIL_CROP[0], self._RAIL_CROP[1]
            return [(t, int(cx) + x0, int(cy) + y0)
                    for t, _conf, cx, cy in _ocr_frame(fr.crop(self._RAIL_CROP))]
        except Exception as exc:
            logger.debug(f"[world_map] could not read the rail rows: {exc}")
            return []

    def _rail_signature(self, frame=None):
        """Where the rail is scrolled to, comparable across ticks.

        THE SHARED TEST (actions.ui.lists.signature), used here rather than a fourth copy.
        This activity does ONE action per tick, so it cannot loop the way `find_in_list`
        does — it remembers the signature instead and compares on the next look. Same
        question, asked across ticks."""
        from actions.ui.lists import signature
        rows = self._rail_rows(frame)
        return signature(rows) if rows else None

    def _scroll_list(self) -> None:
        """Scroll the WORLD MAP's own destination list, down the left side."""
        if self._scroll is not None:
            self._scroll()
            return
        from actions import ui
        ui.scroll(300, 700, -260, why="world map list — the search did not surface it")

    def _scroll_trade_list(self) -> bool:
        """Scroll the VILLAGE PANEL's trade list. A DIFFERENT LIST, on the other side.

        Live 2026-08-30 at Hutu Village: this shared `_scroll_list` with the map's own list,
        so it swiped (300,700) — the left rail, which held one already-selected result and
        nothing to move — while the trade list it was reading sat in the panel on the RIGHT.
        Every scroll re-read the same screen, the read never completed, and the mission
        stopped after sixteen ticks holding a partial recipe. The `why=` string went with it,
        logging "the search did not surface it" about a search that was not running.

        The panel knows where it is, so ask it rather than pick a second coordinate: a bbox
        that moves with the layout cannot drift out of date the way a literal would.
        """
        if self._scroll_panel is not None:
            self._scroll_panel()
            return True
        from actions import ui
        from vision.omniparser import parse_fast_cached
        from vision.region_detectors.panels import detect_right_panel
        frame = self._frame()
        w = getattr(frame, "width", 0) or 0
        h = getattr(frame, "height", 0) or 0
        panel = detect_right_panel(list(parse_fast_cached(frame)), w, h) if (w and h) else None
        bbox = getattr(panel, "bbox", None) if panel is not None else None
        if not bbox:
            logger.warning("[world_map] the trade list needs scrolling but the right panel "
                           "was not located — not guessing where to swipe")
            return False
        x1, y1, x2, y2 = bbox
        ui.scroll((x1 + x2) // 2, (y1 + y2) // 2, -260, why="the village panel's trade list")
        return True

    # ── the remote check's own readers, injectable like the rest ────────────
    def _read_base(self) -> dict:
        if self._read_base_fn is not None:
            return self._read_base_fn()
        from actions.village_check import parse_base_tab
        from vision.omniparser import parse_fast_cached
        return parse_base_tab(parse_fast_cached(self._frame())) or {}

    def _switch_to_base(self) -> None:
        """Back to the Base tab, where the day's rounds are. The panel remembers which tab it
        was left on, so this is not a step that can be assumed to have happened already."""
        if self._switch_tab_fn is not None:
            self._switch_tab_fn("base")
            return
        from actions import ui
        ui.tap_text(self._frame(), "base", why="Village Info -> Base tab (the day's rounds)")

    def _switch_to_barter(self) -> None:
        if self._switch_tab_fn is not None:
            self._switch_tab_fn("barter")
            return
        from actions import ui
        ui.tap_text(self._frame(), "barter", why="Village Info -> Barter tab")

    def _read_trade_screen(self):
        if self._read_screen_fn is not None:
            return self._read_screen_fn()
        from actions.village_check import parse_trade_list, trade_list_elements
        try:
            return parse_trade_list(trade_list_elements(self._frame()))
        except Exception as exc:
            logger.debug(f"[world_map] trade screen unreadable: {exc}")
            return None

    def _merge_screens(self):
        if self._merge_fn is not None:
            return self._merge_fn(self._screens)
        from actions.village_check import merge_trade_screens
        return merge_trade_screens([s for s in self._screens if s])

    def _read_is_complete(self, goal) -> bool:
        """Stop when the good we came for is fully read, or the list has ended.

        MATERIALS ARE INVARIANT: a recipe does not lose an ingredient, so fewer than the KB
        knows means the read fell short, not that the recipe changed. That check is what
        `_target_complete` exists for, and it must be asked of the MERGED screens — asking
        the per-village list written by this same partial read is how a two-of-three recipe
        certified itself complete (2026-08-27).
        """
        if self._complete_fn is not None:
            return bool(self._complete_fn(self._merge_screens(), goal))
        if not goal.good:
            return bool(self._screens)          # nothing named — one screen is the answer
        try:
            from actions.village_check import _target_complete
            return bool(_target_complete(self._merge_screens(), goal.good, goal.village))
        except Exception as exc:
            logger.debug(f"[world_map] completeness check failed: {exc}")
            return False

    def _read_events(self) -> list:
        if self._read_events_fn is not None:
            return self._read_events_fn()
        try:
            from vision.trade_event_reader import read_trade_events
            return list(read_trade_events(self._frame()) or [])
        except Exception as exc:
            logger.debug(f"[world_map] event schedule unreadable: {exc}")
            return []

    def _commit_departure(self, where: str) -> bool:
        """Tap the commit control on the panel that is ALREADY open, then clear the notice.

        NOT `commit_departure` — that primitive opens the world map and navigates to the
        destination itself, which is the whole flow. Called from a panel with the place
        already selected it would re-open the map and start again. (It was also called with
        no destination at all, so it raised on every live attempt from f9b31a6 until now.)

        The control is found where the action layer has always said it lives: the bottom
        action bar, verb+noun. Returns whether the tap went in — never whether the fleet
        moved, which is the next perceive's business.
        """
        if self._commit is not None:
            return bool(self._commit(where))
        from actions.sail_actions import (confirm_departure_notice,
                                          destination_commit_control)
        # `allow_bare_move`: a saved route's button says only `Move`. Opt-in, because a bare
        # `Move` is also the sea-waypoint marker — see `destination_commit_control`. By the
        # time we are here the classifier has already established this is a destination panel.
        pos = destination_commit_control(self._frame(), allow_bare_move=True)
        if pos is None:
            return False
        self._tap_at(*pos)
        notice = confirm_departure_notice(where)
        return not (notice.get("seen") and not notice.get("confirmed"))


# A prefix, not the whole name: OCR mangles accents and the game filters as you type.
_PREFIX_LEN = 4
# How far the prefix may be walked back when the filter comes back empty. Three
# gets `Gijo` down to `G`, which cannot exclude anything; past that the query is
# not the problem.
_MAX_SHORTENINGS = 3

# One re-tap answers a dropped tap, which the game does about once in twenty. Past that the
# rail is refusing, and the answer is to REPORT — never to tap a different icon, which opens
# a different list (user, 2026-09-08).
_MAX_LIST_TAPS = 2
# Typing twice on the same destination means the box did not take it. Scrolling is next.
# How far from the search box's centre still counts as the search box's own row. The field is
# a single line; anything sharing its band is its text or its furniture, never a list row.
_SEARCH_BOX_BAND = 40

@dataclass(frozen=True)
class _AtPoint:
    """A bare coordinate, shaped like an element, so the search-box test works on both.

    One rule, applied by both readers. Two copies would drift, and the one that drifted would
    tap a text field.
    """

    cx: int
    cy: int


# How many times to trust the free read of the map before falling back to the rail. Once: a
# look that did not pay off is not a reason to look again at an unchanged screen.
_MAX_FREE_TAPS = 1

_MAX_TYPED = 2              # typings that must LAND before we fall back to scrolling
_MAX_TYPE_ATTEMPTS = 4      # ...and how many may be SENT trying to land them
# Another place's panel, closed so the map can be searched. TWO, because a stale panel
# is cleared by one Back and a second says the Back is not landing — at which point it
# is a fact to report, not something to grind at.
_MAX_PANEL_CLOSES = 2
_MAX_KB_CLEARS = 2
# Putting the keyboard away as routine hygiene BEFORE reading or tapping, budgeted apart
# from _MAX_KB_CLEARS above. That one is the poisoned-query recovery and resets the typing
# attempts; sharing a budget would let hygiene exhaust the remedy.
_MAX_KB_HYGIENE = 4
# The backup's own bound — a rail that never surfaces it is a fact to report, not to grind at.
# The port list is long and unsorted, and six swipes did not reach Barcelona from
# Tripoli even once the list was open (live 2026-09-01). Typing is the real
# mechanism; scrolling is the fallback, and it needs enough room to finish.
_MAX_SCROLLS = 20

# The trade list's own bound. It is the BACKUP for a list that will not surface what we
# came for, not a plan — the read stops when the good is complete or the list ends.
_MAX_LIST_SCROLLS = 8

# WHICH GOAL EACH SCREEN ANSWERS FOR. The world map serves four errands and has eight
# sub-screens, and a handler is written for ONE errand — but only two of them checked, so the
# rest answered for whatever goal arrived.
#
# Live 2026-08-29: a `ChooseDestination` tap landed on the event schedule, `_on_event_schedule`
# reported FINISHED ("the schedule lists 0 events"), and `SailRunner` read that as the course
# being set. The ship then sat at sea with no destination while everything upstream believed
# it was under way. A handler answering for a goal it was not written for is worse than one
# that fails, because it succeeds.
WorldMapActivity._HANDLERS = {
    _ctx.MAP_OPEN:             (WorldMapActivity._on_map,
                                (ChooseDestination, RemoteCheck, MoveViaLocationInfo)),
    _ctx.DESTINATION_LIST:     (WorldMapActivity._on_list,
                                (ChooseDestination, RemoteCheck)),
    _ctx.DESTINATION_PANEL:    (WorldMapActivity._on_panel, (ChooseDestination,)),
    _ctx.ROUTE_LIST:           (WorldMapActivity._on_route_list, (ChooseDestination,)),
    # BOTH ERRANDS, like the city panel above. This panel is what a village pin opens, and
    # its gold 'Move to Village' button is how a course to a village is committed.
    _ctx.VILLAGE_INFO_BASE:    (WorldMapActivity._on_village_info_base,
                                (RemoteCheck, ChooseDestination)),
    _ctx.VILLAGE_INFO_BARTER:  (WorldMapActivity._on_village_info_barter,
                                (RemoteCheck, ChooseDestination)),
    _ctx.EVENT_SCHEDULE:       (WorldMapActivity._on_event_schedule, (ReadEventSchedule,)),
    # BOTH ERRANDS. This panel and the bottom bar are the same fact seen twice — a place is
    # selected and its gold Move button is showing — and both handlers do the same thing.
    # When a map pin opens the panel, the panel wins the classification, so refusing
    # ChooseDestination here would hand back on the one screen that can commit it.
    _ctx.LOCATION_INFO:        (WorldMapActivity._on_location_info,
                                (MoveViaLocationInfo, ChooseDestination)),
}
assert set(WorldMapActivity._HANDLERS) == set(WorldMapActivity.CONTEXT_STATES), \
    "every declared context state needs a handler, and vice versa"
