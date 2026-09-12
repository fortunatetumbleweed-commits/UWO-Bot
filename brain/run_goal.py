"""Drive the dispatcher for one goal, and return what came of it.

The migration path. Each task node moves from "a function that drives screens" to "a goal run
through the loop", one at a time, and this is the shim that lets a node do that while
`run_mission` still owns the sequence around it. It disappears when the task runner drives the
dispatcher directly.

The task runner here is deliberately trivial — hand back the same goal until an activity
finishes it, then say nothing. That is the whole contract: "nothing" is a valid answer and the
dispatcher invents no work after it.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from loguru import logger

from brain.dispatcher import (ActivityResult, BLOCKED, Dispatcher, FINISHED,
                              UNRECOGNISED, WORKING)
from brain.intents import dispatch, to_intent


# The last port the SCREEN named. A building interior does not carry the port's name — there
# is nothing on a market's screen that says "Lisboa" — so a market activity that asks the
# state where it is gets nothing and reports port=''. That is what happened on 2026-08-26:
# the sell leg worked and logged `'port': ''`.
#
# This is the repository in miniature, under the repository's rule: the SCREEN OUTRANKS IT.
# A remembered port is used only when the current look names none, and any named port
# replaces it immediately.
#
# IT IS PLACE DATA, AND PLACE DATA DIES ON LEAVING (user, 2026-09-02). This comment used to
# end "it can only ever be stale in the one direction that matters least — the bot walks into
# a building and out again at the same port", and that is true only of the case it was
# written for. Going to sea is LEAVING, and the fleet arrives somewhere else.
#
# Live 2026-09-02: four seconds after the sea, Faro's UI had not finished drawing, so
# `read_port_name` correctly returned None — and this handed the voyage 'Lisboa', last named
# 2.5 minutes and one departure earlier. The arrival check compared 'Lisboa' against Faro and
# failed the mission while the fleet stood in Faro.
#
# So it is offered ONLY where the screen legitimately has no banner: inside a building or a
# sub-menu, which is the whole of its original purpose. Everywhere else a missing name means
# the name is missing — mid-render at a port, or no port at all at sea — and inventing one is
# how a stale place becomes a decision.
_last_named_port: Optional[str] = None

# Screens that HAVE no port banner by design, so a missing name there says nothing about
# where the fleet is. Anywhere else, a missing name is a fact about the frame.
_NO_BANNER_BY_DESIGN = ("building", "sub_menu")

# Being here means the port is behind us, so the remembered name is not merely unusable — it
# is wrong, and must go rather than wait to be overwritten by the next port.
_PORT_IS_BEHIND_US = ("sea", "sea_cinematic")


def _refined_state():
    """Perceive, and key the state by WHICH building when there is one.

    `location` alone is too coarse to choose an activity: the harbour, the market and the
    shipyard are all 'building', and handing a sell goal to whichever one the bot happens to
    be standing in is exactly the class of mistake this architecture removes. The classifier
    already carries the name in `detail` ("building: market"), so use it.
    """
    from brain.perceive import perceive as _perceive
    res = _perceive()
    loc = res.to_location_dict()
    where, detail = loc.get("location"), (loc.get("detail") or "")
    if where in ("building", "sub_menu") and ":" in detail:
        name = detail.split(":", 1)[1].strip().split(" — ", 1)[0].strip().lower()
        if name:
            where = f"{where}:{name}"
    global _last_named_port
    port = loc.get("port")
    base = str(where or "").split(":", 1)[0]
    if port:
        _last_named_port = port
    elif base in _PORT_IS_BEHIND_US:
        # LEAVING KILLS IT. Not "unused while at sea" — cleared, so that the next screen that
        # cannot name a port cannot be handed the one we sailed away from.
        if _last_named_port is not None:
            logger.info(f"[run_goal] at sea — forgetting {_last_named_port!r}; a port we "
                        "have left is not where we are")
            _last_named_port = None
    elif base in _NO_BANNER_BY_DESIGN:
        port = _last_named_port          # a market interior never says "Lisboa"
    # else: a port_overworld with no readable name is a frame that has not finished drawing.
    # Saying so is the useful answer; substituting a remembered name is how one became an
    # arrival verdict against the wrong port.

    # CARRY THE EVIDENCE, NOT JUST THE VERDICT. `sub_menu` and `scene_type` are two
    # INDEPENDENT reads of what screen this is, and dropping them here is what forced
    # `_at_a_village` to go and perceive a second time to recover them — a task module
    # re-observing because the central observation threw away what it had. Centralize the
    # observation, localize the interpretation (Guiding Principle #1): the interpretation is
    # the caller's, but the evidence has to survive the trip.
    import types
    return types.SimpleNamespace(state=where, port=port, detail=detail,
                                 sub_menu=getattr(res, "sub_menu", None),
                                 scene_type=getattr(res, "scene_type", None),
                                 frame=getattr(res, "frame", None))


def default_activities() -> Mapping[str, Any]:
    """Which activity serves which state. Keyed finely enough to be unambiguous.

    Each activity declares its own `SERVES`, so this registry is BUILT from them rather than
    listing states itself. Two lists of where the market works is what caused ENTER_BUILDING
    to be dispatched on every step while the bot stood inside the market (2026-08-26).
    """
    from brain.activities.harbor import HarborActivity
    from brain.activities.idle_lock import IdleLockActivity
    from brain.activities.main_menu import MainMenuActivity
    from brain.activities.market import MarketActivity
    from brain.activities.bootstrap import PositionKnownActivity
    from brain.activities.port import PortActivity
    from brain.activities.sea import SeaActivity
    from brain.activities.world_map import WorldMapActivity
    from brain.activities.transient import TransientActivity
    from brain.activities.unrecognized_chromed import UnrecognizedChromedActivity
    from brain.activities.village import VillageActivity

    # A STATE MAY HAVE MORE THAN ONE ACTIVITY, resolved by its GOALS filter — every workable
    # state is served by its own activity AND by `PositionKnownActivity` for the bootstrap.
    # Keying one activity per state silently overwrote whichever registered first.
    registry: dict = {}
    for activity in (MarketActivity(), HarborActivity(), VillageActivity(), SeaActivity(),
                     PortActivity(), WorldMapActivity(), PositionKnownActivity(),
                     IdleLockActivity(), TransientActivity(), MainMenuActivity(),
                     UnrecognizedChromedActivity()):
        for where in activity.SERVES:
            registry.setdefault(where, []).append(activity)
    # The lock, a full-screen notice and an unnameable chromed screen are STATES, not errors:
    # each has a repertoire of one action and an unknown destination, and each finishes rather
    # than routing. They are ordinary entries in the list above — no caller learns what a lock
    # or a notice is, and nothing here knows their state names, which is the point.
    return registry


def affordances(where: Any, activities: Mapping[str, Any]) -> Optional[frozenset]:
    """Which intents can be STARTED from `where`, or None when nobody has said.

    DERIVED FROM THE ACTIVITIES, exactly as `default_activities` derives the registry — for
    the same reason. A second table of what each world affords is a second thing to keep in
    step, and the first one drifting is what dispatched ENTER_BUILDING on every step while
    the bot stood inside the market (2026-08-26).

    The facts were already in the codebase, spread across the places that discovered them:
    `tap_world_map_control`'s branches say where a globe or a minimap exists, `_is_inside`
    says a village has neither, `_NEVER_BACK_FROM` says Back would leave the game at an
    overworld. Each was learned from a live wedge and written down where it was learned.
    Collecting them lets the dispatcher ask ONE question before it acts.

    A world's affordances are the UNION over the activities serving it, because two can serve
    one world and each knows its own part. `CAN_START` may be a flat tuple (the same
    everywhere that activity serves) or a dict keyed by state. Every activity declares a FLAT
    one today: the dict form existed for `AshoreActivity`, which served `port_overworld` and
    `village` at once and so had to answer differently in each. Splitting it into
    `PortActivity` removed the need. The dict form stays supported because an activity serving
    two worlds is legal, not because one does.

    None means UNCONSTRAINED — no activity serving this world declared anything, so we do not
    know and must not refuse. An empty frozenset is different: somebody declared that nothing
    can be started here, which is true of the lock, a notice, and an unnameable chromed
    screen.
    """
    found = activities.get(where)
    # ONE ACTIVITY OR A LIST OF THEM. `default_activities` builds lists, but the registry is
    # injectable and a single object is the natural thing to pass — the dispatcher's own
    # resolution normalises the same way, and must not disagree with this one.
    served = ([] if found is None
              else list(found) if isinstance(found, (list, tuple)) else [found])
    declared, out = False, set()
    for activity in served:
        can = getattr(activity, "CAN_START", None)
        if can is None:
            continue
        declared = True
        if isinstance(can, Mapping):
            out.update(can.get(where, ()))
        else:
            out.update(can)
    return frozenset(out) if declared else None


def _lands_at(where: Any, intent_name: str, activities: Mapping[str, Any]) -> set:
    """Which worlds `intent_name` reaches FROM `where`, per the activities' `LEADS_TO`."""
    found = activities.get(where)
    served = ([] if found is None
              else list(found) if isinstance(found, (list, tuple)) else [found])
    out = set()
    for activity in served:
        leads = getattr(activity, "LEADS_TO", None) or {}
        table = leads.get(where, leads) if isinstance(leads.get(where, None), Mapping) else leads
        dest = table.get(intent_name)
        if dest:
            out.add(dest)
    return out


# ENTER_BUILDING IS PARAMETERISED — it lands in whichever building was asked for, so there is
# no single destination to declare. What matters for a route is that from a world which can
# start it, EVERY building and sub-menu is one hop away.
_BUILDING_PREFIXES = ("building:", "sub_menu:")


def first_hop_toward(where: Any, targets, activities: Mapping[str, Any],
                     *, max_hops: int = 3) -> Optional[str]:
    """The name of the intent that STARTS a route from `where` to any of `targets`.

    None when we are already there, or when no route is known. Breadth-first, so the answer
    is the first step of a SHORTEST route — and only the first step, because the world after
    it is observed rather than assumed (CLAUDE.md #2: the next look says whether the hop
    landed, and the route is recomputed from there).

    WHY THIS EXISTS. `CAN_START` alone can only refuse. Live 2026-09-01 the capability check
    turned a market order inside the HARBOUR into a hard stop: the harbour affords only
    EXIT_BUILDING, so ENTER_BUILDING was refused — correctly — and there the matter ended,
    when the actual answer is two hops, out to the overworld and in again. Declining is not
    routing.

    It also cannot route through everything, and says so rather than guessing: closing the
    world map lands wherever the fleet was standing, which nothing records, so
    `WorldMapActivity` declares no destination and no route is planned through it. And no
    route crosses the SEA — sailing is not one of these transitions, it is a voyage the
    mission owns.
    """
    targets = set(targets or ())
    if not targets or where in targets:
        return None
    seen, frontier = {where}, [(where, None)]
    for _hop in range(max_hops):
        nxt = []
        for world, first in frontier:
            for name in sorted(affordances(world, activities) or ()):
                dests = _lands_at(world, name, activities)
                if name == "ENTER_BUILDING":
                    dests |= {t for t in targets
                              if str(t).startswith(_BUILDING_PREFIXES)}
                for dest in dests:
                    if dest in targets:
                        return first or name
                    if dest not in seen:
                        seen.add(dest)
                        nxt.append((dest, first or name))
        frontier = nxt
    return None


# SCREENS THAT COVER A WORLD RATHER THAN REPLACING IT. Standing on one tells you nothing
# about the place underneath: the map is opened from on top of a port, a notice and the lock
# are drawn over whatever was there. `owned_state` states the same fact for the same reason —
# "treating them as 'left the port' would drop a market's contents because a notice appeared
# over it" — and the only world in its list that genuinely means the port is behind us is the
# sea, which is why the sea is NOT here.
_COVERS_A_WORLD = ("world_map", "loading", "transient", "idle_lock")


def port_is_underfoot(where: Any) -> Optional[bool]:
    """Is the fleet ashore at a PORT — where a market is at most a door away? None = unknown.

    The question the MISSION asks, distinct from `affordances`, which is what the DISPATCHER
    asks. Both are "can this world do that", and they were answered by two hand-written lists
    until they were reconciled here: `mission_runner._NO_MARKET` and each activity's
    `CAN_START`. Two lists of one fact is what dispatched ENTER_BUILDING on every step while
    the bot stood inside the market (2026-08-26), and the lists here would have drifted the
    same way.

    DERIVED FROM `SERVES`, which already names every port-side world: the market's and the
    harbour's, plus the overworld and the port map. A village is deliberately absent — it is
    ashore WITHOUT being a port, its left menu is Explore/Gifting/Loot/Recruit/Barter, and
    there is no building list to open. `sub_menu:barter` is the village's own menu and is
    absent for the same reason.

    NOT DERIVABLE FROM `CAN_START` — and it is worth saying why, because it looks as though
    it should be. `CAN_START` declares what a world can START, never where that lands. From a
    building you step out and into another and a market is two transitions away; from a
    village you also step out, and land at SEA. Same declared capability, opposite answer.
    Closing that gap needs the destination of each transition — `docs/intent_graph.md`, still
    open — so until then the two questions stay separate and are pinned to agree by
    `tests/test_a_world_declares_what_can_start_in_it`.
    """
    if where in _COVERS_A_WORLD:
        return None
    from brain.activities.harbor import HarborActivity
    from brain.activities.market import MarketActivity
    ashore = set(MarketActivity.SERVES) | set(HarborActivity.SERVES) | {"port_overworld",
                                                                        "port_map"}
    return where in ashore


def _sleep_jittered(seconds: float) -> None:
    """Wait, never on a fixed cadence — the anti-cheat fingerprints regular intervals
    (CLAUDE.md, anti-cheat tap discipline)."""
    import random
    import time
    time.sleep(max(0.0, seconds) * random.uniform(0.9, 1.1))


def _build_dispatcher(next_goal, perceive, activities, unblock):
    """The one place a Dispatcher is wired. Shared by `run_goal` and `run_task`."""
    return Dispatcher(perceive=perceive or _refined_state,
                      activities=dict(activities or default_activities()),
                      next_goal=next_goal, to_intent=to_intent, dispatch=dispatch,
                      unblock=unblock)


# AT MOST THREE GOES AT THE SAME THING (user, 2026-09-01). Three identical steps is
# not a wait, it is a loop, and the fourth will not differ either.
_MAX_RETRIES = 3


def _what_this_step_amounted_to(record, d, runner_status):
    """Everything a step can move, as one comparable value. Equal twice running = a stall.

    PROGRESS IS CHANGE, NOT ACTION — the rule both loops already stated, and neither applied
    to a WORKING tick. `status == WORKING` reset the counter unconditionally, on the reading
    that an activity saying "I did a thing, come back" is progress. It is not: it is an
    activity's word about itself, and CLAUDE.md's second principle is that a conclusion is
    exactly what must not be believed.

    Live 2026-09-01 the world map opened the port list, judged it closed, re-tapped the rail —
    which toggles an open list SHUT — and did that SEVENTEEN times over four minutes, every
    tick returning WORKING {'did': 'opened the port list'}. Nothing stopped it, because
    something was always "happening".

    So the activity's OBSERVED data joins the tuple. It is what separates a barter committing
    round after round (`rounds_committed` 1, 2, 3...) from a rail being tapped at the same
    point with the same result — both report WORKING every tick, and only one is getting
    anywhere.
    """
    observed = getattr(d.last, "observed", None) or {}
    return (record.get("state"), str(record.get("goal")), str(record.get("intent")),
            runner_status, getattr(d.last, "status", None),
            tuple(sorted((str(k), str(v)) for k, v in observed.items())))


def run_goal(goal: Any, *, max_ticks: int = 120, max_stalled: int = _MAX_RETRIES, perceive=None,
             activities: Optional[Mapping[str, Any]] = None) -> Optional[ActivityResult]:
    """Tick until an activity finishes `goal`, or the budget runs out. Returns its result.

    The budget is a backstop, not a policy. Every step either does work, dispatches a
    transition, or recovers bearings, so a goal that needs many steps is a goal meeting many
    obstacles — which is worth reporting rather than grinding at.
    """
    outcome: dict = {}

    def next_goal(result, _state):
        if result is None:
            return goal
        if result.status in (FINISHED, BLOCKED):
            outcome["result"] = result
            return None
        # WORKING: one step done inside the same context, the goal is not. Hand the SAME goal
        # back — this is the tick that replaces a flow sub-loop, so it must not look like
        # completion. See brain.dispatcher.WORKING.
        # UNRECOGNISED: the dispatcher has already regained its bearings; ask again.
        return goal

    def _unblock(state) -> bool:
        """Clear an unsolicited popup laid OVER the world — daily news, a promo, the store.

        THE DISPATCHER HAD THIS AND IT WAS NEVER WIRED. `Dispatcher.step` has cleared
        obstructions since it was written — `if self._unblock is not None` — but production
        passed no `unblock`, so the branch never ran (found 2026-08-28). That is why
        `buy_to_goal` grew FIVE `clear_blockers` calls of its own: the shared one existed,
        was correct, and was switched off.

        An interruption is ORTHOGONAL to the state, never a state of its own — it can sit
        over a village, a market or the sea. Clear it, then re-perceive, then decide.
        """
        from brain.unexpected_dialog import clear_blockers
        try:
            frame = getattr(state, "frame", None)
            return bool((clear_blockers(frame) or {}).get("cleared"))
        except Exception as exc:
            logger.debug(f"[run_goal] blocker check skipped: {exc}")
            return False

    d = _build_dispatcher(next_goal, perceive, activities, _unblock)

    # THE BUDGET COUNTS OBSTACLES, NOT TICKS.
    #
    # It used to be a flat `max_ticks=10`, on the premise that "every tick either does work,
    # dispatches a transition, or recovers bearings, so a goal that needs many ticks is a
    # goal meeting many obstacles". Under the context model that premise inverts: a tick is
    # now ONE action, so a four-round barter legitimately spends twenty of them and many
    # ticks is normal rather than evidence of trouble.
    #
    # So the backstop measures what it always MEANT to measure — consecutive ticks that made
    # no progress. WORKING is progress and resets the count; anything else does not. The
    # absolute ceiling stays as a runaway guard, far above any real goal.
    stalled = 0
    seen = None
    # A HAND-BACK LOOP, NOT A TICK LOOP (user, 2026-08-31). Each pass is "the activity did one
    # thing and returned", never "sample the world again": there is no event source to react
    # to (Guiding Principle #0), so this loop is the substitute for one. Pacing left here when
    # the waiting moved to the dispatcher; what remains is repeat, detect completion, detect a
    # stall, and bound runaway.
    #
    # NOTE the counters now bound ACTIONS rather than time. A long sea leg spends almost no
    # steps while a busy market spends many — arguably the more useful bound, but `max_ticks`
    # meant something different when it was written.
    for step in range(1, max_ticks + 1):
        record = d.step() or {}
        if "result" in outcome:
            logger.info(f"[run_goal] {goal} finished on step {step}: "
                        f"{outcome['result'].status}")
            return outcome["result"]
        # PACING BELONGS TO THE STATE, AND THE WAITING BELONGS TO THE DISPATCHER
        # (user, 2026-08-31). The activity states how long it wants before the next look
        # by reporting `checkback_s`; it never sleeps. The dispatcher records that as a
        # wake timer and waits it out at the top of its next tick — see
        # `Dispatcher._wait_out_the_wake_timer` — because what to do on waking is a
        # ROUTING decision (arrived at a port? still at sea? idle-locked?) and routing is
        # the dispatcher's, not this loop's.
        #
        # It is also the one moment the bot did nothing while the world moved, which is
        # why `expect_changed` lives there and nowhere else.
        # EVERY step is judged, WORKING included — see `_what_this_step_amounted_to`.
        here = _what_this_step_amounted_to(record, d, None)
        if here != seen:
            stalled = 0
        else:
            stalled += 1
            if stalled >= max_stalled:
                logger.error(f"[run_goal] {goal}: NOTHING CHANGED for {stalled} steps "
                             f"(state={here[0]!r} intent={here[2]}) — stopping. A step that "
                             "repeats itself with the same result is not making progress.")
                return None
        seen = here
    logger.warning(f"[run_goal] {goal} hit the {max_ticks}-step ceiling")
    return None


def run_task(runner: Any, *, max_ticks: int = 200, max_stalled: int = _MAX_RETRIES,
             perceive=None, activities: Optional[Mapping[str, Any]] = None) -> Any:
    """Tick while a PASSIVE task runner supplies the work orders. Returns the runner.

    The difference from `run_goal` is only WHO decides what comes next. `run_goal` carries one
    fixed goal and stops when an activity finishes it; here the dispatcher consults `runner`
    after every result, and the task ends when the runner asks for nothing more.

    THIS IS WHERE DRIVING BELONGS. The task runner is consulted and never calls — Guiding
    Principle #7 — so something has to turn the crank, and it is the loop that already owns
    the crank. `brain/layers.py` gates the task modules at zero driver calls for exactly this
    reason: the entry point drives, the task logic does not.
    """
    done = {"idle": False}

    def next_goal(result, state):
        goal = runner.next_goal(result, state)
        if goal is None:
            done["idle"] = True
        return goal

    def _unblock(state) -> bool:
        from brain.unexpected_dialog import clear_blockers
        try:
            return bool((clear_blockers(getattr(state, "frame", None)) or {}).get("cleared"))
        except Exception as exc:
            logger.debug(f"[run_task] blocker check skipped: {exc}")
            return False

    d = _build_dispatcher(next_goal, perceive, activities, _unblock)

    stalled = 0
    seen = None
    for step in range(1, max_ticks + 1):
        try:
            record = d.step() or {}
        except Exception as exc:
            # A TICK THAT CANNOT LOOK MUST STOP, NOT RAISE. Perception failing is a normal
        # outcome — a frame that will not capture, a device that blinked — and the answer
        # is to stop rather than to act on nothing. Never navigate on blindness: nothing
        # is dispatched from a tick that did not perceive, because `to_intent` is never
        # reached, so stopping here presses nothing.
            logger.warning(f"[run_task] step {step} failed: {exc} — stopping")
            return runner
        if done["idle"]:
            logger.info(f"[run_task] {runner.__class__.__name__} has nothing more to ask "
                        f"for after {step} step(s) — status {getattr(runner, 'status', '?')}")
            return runner
        # THE WAITING MOVED TO THE DISPATCHER (user, 2026-08-31), along with the rule
        # that used to live here: NOT IF THE TICK HAS SINCE ACTED. The pause is what the
        # activity wanted given the world it saw; the tick then perceives AGAIN and may
        # find that world gone. Live 2026-08-29 the sea asked for 7.5 minutes mid-voyage,
        # and by the end of that same tick the fleet had reached Tripoli, been handed
        # 'hold Candle' and tapped into the market — then slept 7.5 minutes on the
        # market's doorstep. A dispatched intent is proof the world moved on: there is
        # something to look at NOW.
        #
        # See `Dispatcher._set_wake_timer` / `_wait_out_the_wake_timer`. It belongs there
        # because what to do on waking is ROUTING — arrived? still at sea? idle-locked? —
        # and this loop does not route.
        else:
            # PROGRESS IS CHANGE, NOT ACTION — and the tick already reports everything needed
        # to tell them apart. The guard used to read `last.status` alone, so a bot walking
        # across a port looked identical to one spinning: leaving a submenu, leaving a
        # building, reading the port and tapping into another is four ticks in which no
        # activity runs and every one reports 'unrecognised'. The old sub-loops did that
        # walking INSIDE a primitive, so this loop never saw those ticks; flattening made
        # them its own and the guard read them as failure.
        #
        # Live 2026-08-29 at Amsterdam: the run was stopped on the very tap that was
        # entering the market to buy the Iron the mission had just asked for.
        #
        # So a tick counts when NOTHING moved: not where we are, not what we are trying
        # to do, not what we are doing about it, not how far the task has got. Acting
        # does not excuse a tick — an action that changes nothing IS the stall. Ticks
        # that alternate between two of these are left to the `max_ticks` ceiling; no
        # cycle detector is written for a cycle nobody has seen.
            pass
        here = _what_this_step_amounted_to(record, d, getattr(runner, "status", None))
        if here != seen:
            stalled = 0
        else:
            stalled += 1
            if stalled >= max_stalled:
                logger.error(f"[run_task] NOTHING CHANGED for {stalled} ticks "
                             f"(state={here[0]!r} goal={here[1]} intent={here[2]}) — "
                             "stopping. A step that repeats itself with the same result is "
                             "not making progress.")
                return runner
        seen = here
    logger.warning(f"[run_task] hit the {max_ticks}-tick ceiling")
    return runner
