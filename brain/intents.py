"""Turning goals into transitions, and performing them.

The dispatcher takes two collaborators it does not implement: `to_intent`, which reads a goal
plus where the bot is and names the transition that would make the goal workable, and
`dispatch`, which performs it. This module is both.

TWO RULES THIS FILE EXISTS TO KEEP:

A transition is a SWITCH OF ACTIVITY, however long it takes (user, 2026-08-26). `dispatch`
taps and returns. It does not wait for the new screen, does not poll, and does not confirm it
arrived — `loading` is simply the state the next tick perceives on the way. Every
wait-for-the-transition loop in this codebase has been a source of stalls, and there is nowhere
here for another one to live.

And a goal never names a screen. `Hold({"Iron": 242})` says what the hold should contain; THIS
file is the only place that knows a market is a building you enter by tapping its name in a
panel. Move that knowledge up and the task layer starts issuing UI instructions again, which
is the inversion the whole design removes.
"""

from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from brain.activities.harbor import HarborActivity
from brain.activities.market import MarketActivity
from brain.activities.port import PortActivity
from brain.activities.sea import (ArriveAshore, ClearOfTheVillage, ReadHold,
                                  SeaActivity)
from brain.activities.world_map import WorldMapActivity
from brain.dispatcher import Intent


# Where the world map can be opened from: at sea, on the map already, or standing on a port
# overworld — the globe is right there. Leaving a VILLAGE is the only case that needs backing
# out at all.
_TAIL_CAN_SAIL_FROM = ("sea", "sea_cinematic", "world_map", "port_overworld", "port_map")

# Screens where Back means "Exit Game?", not "go up one level".
_NEVER_BACK_FROM = ("port_overworld", "main_menu")

# WHERE A VOYAGE CAN HAVE ENDED. Not one activity's `SERVES` any more: the port overworld and
# the village are owned by different activities now, and arrival is true on both. Written as
# the UNION of the two screens a ship can arrive at, which is what the goal actually means.
_ASHORE_STATES = (PortActivity.SERVES + ("village",))


def _ends_by_finishing(where: Any) -> bool:
    """States that are LEFT BY FINISHING, never by Back.

    The idle lock ends with the gesture its own prompt names; a full-screen notice ends by
    being dismissed; an unnameable chromed screen ends by going back or home on its own terms.
    Back does nothing to any of them, and pressing it there spends attempts on a screen that
    cannot answer — live 2026-08-26 at Stockholm the leave-the-village loop pressed Back at
    the lock and would have spent all four that way.

    DERIVED, NOT LISTED. A clearing activity is exactly an activity that declares no GOALS —
    it is in the way of every goal and must run whatever the order is — so the states that
    end by finishing are the states those activities serve. A new one protects itself; a
    hardcoded list would be wrong the day one is added (Guiding Principle #3).
    """
    from brain.run_goal import default_activities

    for state, activities in default_activities().items():
        if state == where:
            return any(not getattr(a, "GOALS", None) for a in activities)
    return False


def _is_inside(where: Any) -> bool:
    """True for a building or a sub-menu — anywhere with a Back and no ☰.

    A RULE, NOT A LIST. Enumerating the buildings would be wrong the day one is added, and
    the naming already carries the fact: `building:market`, `sub_menu:barter`. The ☰ belongs
    to the overworld, so being anywhere with a prefix is being somewhere it is not.

    A VILLAGE IS ONE OF THESE TOO, and the rule above already said so — it is the prefix
    test that missed it, because `village` carries no prefix. The screen is chromed: a back
    arrow at the top left, the Explore/Gifting/Loot/Recruit/Barter menu, no ☰ and NO GLOBE.
    There is no world map control in a village at all; you leave to the sea and open it from
    the minimap.
    #
    Live 2026-08-29: the barter finished at Svear with the Birch Tree aboard, the mission
    asked for Lisboa, and OPEN_WORLD_MAP was dispatched at the village every tick —
    "no world-map control on 'village'" — until the run stopped one leg from done.
    #
    Sailing is not the village's work (user, 2026-08-29): its order is finished, so it exits,
    and the dispatcher asks for the next order from wherever that lands. One Back per tick,
    never a loop waiting for the screen to change.
    """
    return (isinstance(where, str)
            and (where.startswith(("building:", "sub_menu:")) or where == "village"))

# WHICH goals the market serves and WHERE it serves them are both the activity's own
# declarations — asked, never restated. This file kept its own copy of each, and each copy
# drifted in turn on 2026-08-26: first the states, so ENTER_BUILDING was dispatched on every
# tick while the bot stood inside the market; then the goals, when `TrimHold` was added to
# the activity and not here, which would have left `sell_surplus` on the port overworld
# waiting for a market it never walked into.


def to_intent(goal: Any, state: Any) -> Optional[Intent]:
    """The transition that would make `goal` workable from here, or None if it already is.

    None means "do the work" — not "nothing to do". The dispatcher dispatches nothing and the
    activity resolved for this state gets the goal on the next tick.
    """
    if goal is None:
        return None
    where = getattr(state, "state", None) or getattr(state, "location", None)

    # THE WORLD MAP IS A FULL-SCREEN OVERLAY, AND MOST WORK IS NOT ON IT.
    #
    # This has to be decided BEFORE the goal branches, not after them. Each branch answers
    # "where does this work happen", and from the map they answer wrongly or not at all:
    # `ReadHold` returned None and stalled, and `Depart` would have tapped a harbour entrance
    # through the open map. Only two kinds of goal belong here — the ones the map SERVES, and
    # `ClearOfTheVillage`, which the map satisfies because the tail opens it from there.
    #
    # Live 2026-08-29: the remote check finished and left the fleet on the map, then
    # `ReadHold` asked for the ☰ — which lives on the port overworld. No activity serves
    # `world_map`, so nothing was dispatched and the tick reported "activity is lost" until
    # the budget ran out.
    if where == "world_map" and not isinstance(goal, WorldMapActivity.GOALS) \
            and not isinstance(goal, ClearOfTheVillage):
        return Intent("CLOSE_WORLD_MAP", {"purpose": str(goal)})

    if isinstance(goal, MarketActivity.GOALS):
        if where in MarketActivity.SERVES:
            return None                       # already where the work happens
        return Intent("ENTER_BUILDING", {"name": "market", "purpose": str(goal)})

    # THE HARBOUR IS A BUILDING, so reaching it is the same transition as reaching the
    # market. `SailToGoal` had a GO_TO_HARBOR phase that walked there by hand, which is a
    # transition written at the task layer — the class of thing this dispatch exists to own.
    if isinstance(goal, HarborActivity.GOALS):
        if where in HarborActivity.SERVES:
            return None                       # already where the work happens
        return Intent("ENTER_BUILDING", {"name": "harbor", "purpose": str(goal)})

    # THE WORLD MAP IS ENTERED FOR A PURPOSE, AND THE PURPOSE RIDES IN THE EXTRAS.
    #
    # One intent, not one per errand. `Intent.extras` is Android's generic container and its
    # docstring already named this case: "the world map is entered to set sail or to make a
    # remote check, and behaves differently for each, so the purpose is part of the request
    # rather than something the activity has to infer."
    #
    # The caller states a GOAL and never mentions the map — `ChooseDestination('Lisboa')`,
    # not `open_world_map()` then navigate. That is the same shape as `Hold(orders=...)`
    # mentioning no tab and `Barter(good, village)` mentioning no panel, and it is what let
    # a map-opener end up holding an OS lock screen on 2026-08-28: the caller navigated
    # first and asked what it was looking at afterwards.
    if isinstance(goal, WorldMapActivity.GOALS):
        if where in WorldMapActivity.SERVES:
            return None                       # already where the work happens
        # THE GLOBE IS ON THE OVERWORLD, so from inside a building there is nothing to tap
        # and the transition is OUT first. Exactly the two-step the hold read below already
        # makes for the ☰ — same asymmetry, same answer, and it was missing here.
        #
        # Live 2026-08-29: the trim finished inside the market's Sell submenu and the next
        # leg wanted Amsterdam. OPEN_WORLD_MAP was dispatched at 'sub_menu:sell' on every
        # tick, the screen never changed, and the run stopped on the no-progress guard after
        # six of them.
        if _is_inside(where) and not _ends_by_finishing(where):
            return Intent("EXIT_BUILDING", {"purpose": str(goal), "from": where})
        return Intent("OPEN_WORLD_MAP", {"purpose": str(goal),
                                         "kind": getattr(goal, "kind", None),
                                         "where": getattr(goal, "where", None)})

    # THE HOLD IS READ FROM ASHORE, BEHIND THE ☰ — and the ☰ exists only on the overworld.
    #
    # The old ladder discovered this at the third rung: read, sweep, read, notice the reason
    # says "no ☰ there", walk out, read again. Standing in the wrong place is a ROUTING
    # problem, and routing is the dispatcher's, so it is answered here as a transition rather
    # than by a reader that can only re-perceive and cannot move.
    # ONE ACTIVITY, TWO GOALS, DIFFERENT REACH — asked per goal, not per activity.
    #
    # `PortActivity` serves both, and its `SERVES` answers for `ReadHold` alone. Arrival is
    # true in a VILLAGE too, and a village is `_is_inside`, so sharing one test would have
    # answered a fleet that had just arrived with a Back out of the place it sailed to.
    #
    # This is the fact `docs/per_goal_serving.md` records and declines to generalise: the
    # reach belongs to the CONTROL (the ☰ is on the overworlds and at sea, nowhere else), not
    # to the goal, and a per-goal table would be a third copy of it. Two goals are written out
    # here rather than a mechanism invented for them.
    if isinstance(goal, ArriveAshore):
        if where in _ASHORE_STATES:
            return None                       # the voyage is over; nothing to press
        if _is_inside(where) and not _ends_by_finishing(where):
            return Intent("EXIT_BUILDING", {"purpose": str(goal), "from": where})
        return None                           # at sea: SeaActivity is watching the HUD

    if isinstance(goal, ReadHold):
        if where in PortActivity.SERVES or where in SeaActivity.SERVES:
            return None                       # the ☰ is here
        # A VILLAGE HAS NO ☰, AND USED TO CLAIM IT DID. `AshoreActivity.SERVES` listed
        # `village`, so this answered "already where the work happens", handed the read to an
        # activity that could not do it and took BLOCKED back on every tick. Live 2026-08-29
        # at Svear that was dead on tick 2, before touching the game. Leaving is the answer:
        # a village exits to the sea, and the hamburger is there.
        if _is_inside(where) and not _ends_by_finishing(where):
            return Intent("EXIT_BUILDING", {"purpose": str(goal), "from": where})
        return None                           # mid-transition: nothing to press

    # LEAVING A VILLAGE IS ROUTING, ONE BACK AT A TIME.
    #
    # This was a `for attempt in range(4)` loop that perceived, decided and pressed, and it
    # carried two hard-won exceptions. Both survive here, as conditions rather than branches:
    # never Back from an overworld or the main menu (there it raises "Exit Game?" — live
    # 2026-08-26 it pressed twice at Stockholm and stood one positive tap from quitting), and
    # a screen that ends by FINISHING rather than by Back is now the clearing activities'
    # business, which is where the idle lock was always meant to be handled.
    if isinstance(goal, ClearOfTheVillage):
        if where in _TAIL_CAN_SAIL_FROM or where in _NEVER_BACK_FROM:
            return None                       # clear already, or a screen Back must not touch
        if _ends_by_finishing(where):
            return None                       # its clearing activity owns it, one per tick
        return Intent("EXIT_BUILDING", {"purpose": str(goal), "from": where})

    logger.debug(f"[intent] no transition known for {goal!r} from {where!r}")
    return None


def dispatch(intent: Intent) -> Any:
    """Perform the transition. Tap, and return — the next tick sees where it landed."""
    if intent.name == "OPEN_WORLD_MAP":
        # ONE TAP, AND NOTHING ELSE. The globe at a port, the minimap at sea — and no
        # waking, no exiting a building, no verifying, none of the five jobs
        # `open_world_map`'s ten-attempt loop was doing at once. Waking belongs to the
        # bootstrap, exiting to the dispatcher's routing, and verifying to the next
        # perceive, which happens anyway.
        from actions.sail_actions import tap_world_map_control
        logger.info(f"[intent] {intent}")
        res = tap_world_map_control() or {}
        if not res.get("tapped"):
            logger.warning(f"[intent] could not start {intent}: {res.get('reason')}")
        return res

    if intent.name == "ENTER_BUILDING":
        from actions.sail_actions import tap_building_entry
        name = intent.extras.get("name") or ""
        logger.info(f"[intent] {intent}")
        res = tap_building_entry(name) or {}
        # NOT VERIFIED HERE, ON PURPOSE. `tapped` means a control was pressed, not that the
        # bot is inside — entering takes a walk across the port and there is no local signal
        # separating "walking" from "the tap missed". The dispatcher re-perceives after every
        # activity anyway, so the answer arrives without anybody waiting for it.
        if not res.get("tapped"):
            logger.warning(f"[intent] could not start {intent}: {res.get('reason')}")
        return res

    if intent.name == "CLOSE_WORLD_MAP":
        # One Back, like every other leaving. The map is an overlay over the place the fleet
        # is actually standing in, so closing it puts us back there — and the next perceive
        # says where that was, rather than this claiming to know.
        from actions.adb_actions import press_back
        logger.info(f"[intent] {intent}")
        press_back()
        return {"tapped": True}

    if intent.name == "EXIT_BUILDING":
        # ONE BACK, AND NOTHING ELSE. Not `reorient_to("port_overworld")`, which walks a
        # whole FSM path inside a single dispatch — that is the sub-loop this architecture
        # exists to remove. One press, then the next tick perceives where it landed and
        # presses again if it is still inside. The loop that repeats is the dispatcher's.
        from actions.adb_actions import press_back
        logger.info(f"[intent] {intent}")
        press_back()                      # returns nothing; the next perceive is the answer
        return {"tapped": True}

    logger.warning(f"[intent] no dispatcher for {intent} — nothing done")
    return None
