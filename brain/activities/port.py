"""The port overworld: the one world where a building can be entered.

WHY THIS EXISTS, AND WHAT IT REPLACES. It was `AshoreActivity`, which served the port
overworld AND the village — two worlds that afford nothing in common. A port has the ☰, the
globe and the building list; a village is a chromed screen with a back arrow and the
Explore/Gifting/Loot/Recruit/Barter menu, and none of the three. That forced the only
per-world capability declarations in the codebase:

    CAN_START = {"port_overworld": ("OPEN_WORLD_MAP", "ENTER_BUILDING"),
                 "village":        ("EXIT_BUILDING",)}

An activity that must ask WHICH WORLD IT WOKE UP IN before it can say what it can do is two
activities sharing a name (user, 2026-09-11: *"village having a AshoreActivity seems odd ...
consistent both would need its own activity"*). Every other activity is named by a state the
classifier emits; there was never an `ashore` state.

The village half needed nothing: `VillageActivity` already declared
`CAN_START = ("EXIT_BUILDING",)` and `LEADS_TO {"EXIT_BUILDING": "sea"}`, so those entries
were a duplicate, not a dependency. It now answers `ArriveAshore` at its own screen, which is
the same rule stated once — THE ACTIVITY THAT OWNS A SCREEN IS THE ONE THAT SAYS YOU HAVE
ARRIVED ON IT.

WHAT THE SPLIT FIXES ON ITS OWN. `ReadHold` reads the fleet panel from behind the ☰, and a
village has no ☰. `AshoreActivity` declared `GOALS = (ArriveAshore, ReadHold)` for BOTH its
worlds, so routing answered "already where the work happens" at a village, handed the read to
an activity that could not do it, and got BLOCKED back. Live 2026-08-29 at Svear that was dead
on tick 2, before touching the game. `ReadHold` is not served here for the village any more,
so the same order now routes OUT to the sea, where the hamburger exists.

WHAT THIS ACTIVITY DOES NOT YET OWN, and should. The port's real work still lives inside
`actions.sail_actions.tap_building_entry`, which the dispatcher calls to perform ONE
transition and which, in that one call, checks for a nameplate, selects the Buildings tab
(trying each candidate and verifying by re-reading the list), reads the menu, pages it up to
four times, falls back to opening the port map, and taps. Two nested loops inside a primitive,
in the one world that had nobody to own them — and it has already misfired: at Bordeaux the
Tasks tab was showing and the fuzzy match hit the word "market" inside a QUEST OBJECTIVE
(`actions/sail_actions.py`, `tap_building_entry`).

Those are CONTEXTS of this screen, in the market's sense: which of Tasks / Buildings / Players
is lit, the location pin that toggles independently of those three
(`memory/port-tab-strip-two-can-be-lit`), whether a building nameplate is up, whether the
wanted row is below the fold. See `docs/activity_architecture.md` §6.
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from brain.activities.sea import ArriveAshore, ReadHold, read_the_hold
from brain.dispatcher import ActivityResult, FINISHED

STATE = "port_overworld"


class PortActivity:
    """The port overworld. Says you have arrived, and reads the hold.

    NARROW BY CONSTRUCTION, and deliberately so: it declares `GOALS`, so `_pick` resolves it
    only for those two orders and the port's ordinary behaviour is unchanged — no activity
    for a `Hold`, so the dispatcher asks the task runner for a goal and dispatches an intent.
    The dispatcher's own comment says why that must not be short-circuited.
    """

    name = "port"

    SERVES = (STATE,)

    # WHAT THIS WORLD CAN BEGIN. The globe opens the world map; the building list is the way
    # into a building. Both are transitions, and both are the dispatcher's to perform — an
    # activity declares that they are POSSIBLE here, never takes them.
    #
    # A flat tuple again, which is the point of the split: there is one world now, so there
    # is one answer.
    CAN_START = ("OPEN_WORLD_MAP", "ENTER_BUILDING")

    # ...AND WHERE IT LANDS, for the half that has a single destination. ENTER_BUILDING is
    # parameterised and lands in whichever building was asked for, so it declares none; the
    # router already treats every building and sub-menu as one hop from a world that can
    # start it.
    LEADS_TO = {"OPEN_WORLD_MAP": "world_map"}

    GOALS: tuple = (ArriveAshore, ReadHold)

    def work(self, goal: Any, state: Any) -> ActivityResult:
        where = getattr(state, "state", None) or getattr(state, "location", None)
        port = getattr(state, "port", None)
        logger.info(f"[port] {goal} — {where!r}{f' at {port}' if port else ''}")

        if isinstance(goal, ReadHold):
            return read_the_hold(where, port)

        # ARRIVAL IS AN OBSERVATION, NOT AN ACTION. Being on this screen IS the answer, so
        # there is nothing to do but say so. `voyage_runner` reads the port out of `observed`
        # to record where the voyage ended, which is why it is carried here rather than left
        # to the next perceive.
        #
        # This is the shape that argues the goal should carry its own done-condition and need
        # no activity at all (`docs/the_plan_is_a_checklist.md`); until it does, something has
        # to answer, and the activity that owns the screen is the right something.
        return ActivityResult(FINISHED, {"port": port, "state": where},
                              detail=f"ashore at {port or where}")
