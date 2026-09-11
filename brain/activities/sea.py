"""At sea: watch the supply, and let the tick do the looking.

THE SEA IS SOMEWHERE THE DISPATCHER SHOULD MEET. Until now it never did — `"no activity for
state 'sea'"` appears ZERO times in the 2026-08-28 logs, not because the sea was handled but
because two private loops (`drive_sail_to`, `_await_route_arrival`) held the fleet for the
whole voyage and never handed back.

That cost a run the same day. Arriving at London the screen was the idle lock with the daily
news panel on top of it, and `_await_route_arrival` polled `_current_port()` — a FIELD READ,
not a perceive — so it received neither the interruptor pass that dismisses the news nor the
`IdleLockActivity` that clears the lock. It would have polled for four hours past a popup one
ordinary perceive removes. The idle lock even NAMED the port it was waiting for.

WHY THIS EXISTS AT ALL, when `port_overworld` has no activity and works fine: a port tick has
an intent to dispatch (enter a building), so falling through to "ask for a goal" is right
there. A voyage has nothing to dispatch — the fleet is already going — so every tick would
report UNRECOGNISED, and `run_goal` counts those as stalls and gives up after six. Only an
activity can say WORKING: *one step taken, same context, ask me again*. That is precisely
what sailing is, ticked slowly.

WHAT IT DOES NOT DO: navigate. Arrival is not this activity's business and not an event it
waits for — when perceive stops saying `sea`, the dispatcher does not call it, and the port's
ordinary path takes over. Nothing here asks "have I arrived?" (docs/activity_as_context.md
§11: arrival is a context change to notice, not an event to wait for).
"""
from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from dataclasses import dataclass

from brain.dispatcher import ActivityResult, BLOCKED, FINISHED, WORKING


@dataclass(frozen=True)
class ArriveAshore:
    """Keep going until the fleet is no longer at sea. Carries no destination.

    The destination was chosen when the departure was committed; repeating it here would be
    a second copy of a decision already made, and a stale one the moment the world moves
    (CLAUDE.md #2). `what` is for the log only.
    """
    what: str = "the voyage"

    def __str__(self) -> str:
        return f"sail until ashore ({self.what})"

@dataclass(frozen=True)
class ReadHold:
    """What the ship can carry and what is aboard. Ashore, behind the ☰.

    A UI RECIPE, NOT A BUSINESS TASK — the same test `RemoteCheck` passes. Opening the main
    menu and reading two numbers off the fleet panel is about the SCREEN; deciding how many
    rounds those numbers buy is about the MISSION, and stays with the task runner.

    This replaces a sixty-line ladder in `brain/barter_command.py`: read the fleet, and if it
    came back empty sweep for blockers and read again, and if it STILL came back empty and
    the reason mentioned a missing ☰, walk to the port overworld and read a third time. Every
    rung of that already has an owner — the sweep is the dispatcher's `unblock`, which runs
    every tick, and the walk is its routing. What was left is one read.
    """

    def __str__(self) -> str:
        return "read the hold"


@dataclass(frozen=True)
class ClearOfTheVillage:
    """Be somewhere the world map can be opened from. Carries no destination.

    A village has no world-map control, so the tail cannot start from inside one. Leaving a
    PLACE is never done to satisfy a state test — the task decided the position can be spent,
    and this is that decision travelling as a work order instead of as a loop.

    NOTHING SERVES THIS GOAL, deliberately. There is no activity to run: being clear is a
    fact about WHERE the fleet is, which the task runner reads off `state` on every consult,
    and getting clear is a TRANSITION, which is the dispatcher's. So the dispatcher finds no
    activity, asks for a goal, is handed this one again, and turns it into one Back — the
    same shape as any other routing answer.
    """

    def __str__(self) -> str:
        return "clear of the village"


# Days of supply below which the fleet must not simply sail on. Reported, never acted on
# here — a fleet that is running dry is a decision for the task runner, which knows whether
# the destination is reachable and what it would cost to turn back.
_SUPPLY_FLOOR_DAYS = 1.0

# THE ETA IS THE PACING RULE: wake by `eta_days x GAME_DAY_SECONDS` — one game day is 90
# real seconds, so a one-day leg is looked at again in 1.5 minutes (user, 2026-08-29). Supply
# only ever shortens that; it can never extend it past the end of the leg.
#
# The ceiling is NOT the pacing rule — it is a backstop on a misread. The HUD reader that
# returned `413` days of supply on a fleet carrying 13.5 can misread an ETA the same way, and
# a single bad digit must not buy hours of silence. Set well above any real leg's wake time
# (13 days) so it governs nothing but nonsense: a genuinely long voyage simply gets a few
# extra looks. Sleeping long enough for the phone to idle-lock is fine — the bootstrap clears
# a lock on waking, and it did so twice this session; sleeping THROUGH the end of the leg is
# not, and that is what happened when supply alone set the timer.
_MIN_CHECKBACK_S = 60.0
_MAX_CHECKBACK_S = 1200.0


class SeaActivity:
    """Observe, and answer ONE question: is the ship still moving?

    Its whole repertoire is looking. It commits no action, chooses no destination, and
    weighs no supply — a voyage is time passing, and the only thing that can go wrong
    without anybody noticing is that it ISN'T passing.

    HOW MOVING IS ESTABLISHED: the SPEED, and the ETA only when the speed cannot be read.
    Not "the HUD reads sea", not "we set a course earlier" — a fleet that never departed, or
    whose Move was swallowed, sits at sea with a perfectly good HUD and an ETA that never
    changes.

    Speed answers in ONE reading where the ETA needs three, and it answers a case the ETA
    cannot answer at all: a SHORT hop, where a 1-day ETA has no finer granularity to be seen
    falling (live 2026-08-24, the fleet reached Bremen while the ETA test was still deciding).

    A ZERO IS NOT SYMMETRICAL WITH A POSITIVE. Any speed above zero means under way, full
    stop. A 0.0 does not mean stopped on its own — the ship may still be accelerating out of
    port — so it is confirmed by a second reading before anything is concluded. And an
    UNREADABLE speed decides nothing at all: it falls through to the ETA, rather than being
    treated as a zero.

    AN UNREADABLE ETA IS NOT A STOPPED SHIP. The idle lock covers the screen while the
    voyage continues underneath it (user, 2026-08-28: "we do not need to unlock on the sea,
    as long as among certain ticks the ETA goes down"). So a tick that cannot read the ETA
    contributes nothing rather than counting against the ship — the readings that exist are
    compared, and the ones that do not are waited through. Clearing the lock is the
    dispatcher's business anyway: it routes to `IdleLockActivity` when it sees one, and this
    activity never learns that a lock exists.

    WHAT IT DOES NOT DO. Supply is reported, never acted on: whether to press on or turn
    back needs the destination and the mission, which belong to the task runner. And when the
    ship is NOT moving, this reports it — the answer is to set a destination, which means
    opening the world map, which is a full-screen change and therefore a TRANSITION for the
    dispatcher to route, not an action for an activity to take (CLAUDE.md: a loop inside a
    primitive may only wait for that primitive's own effect, and the boundary is the screen).
    """

    name = "sea"
    SERVES = ("sea", "sea_cinematic")

    # WHAT CAN BE STARTED FROM HERE. The minimap opens the world map; there is nothing else
    # at sea. No building list, no globe, nothing to leave — `tap_world_map_control` says so
    # by construction ("the globe at a port, the minimap centre at sea. That is all").
    #
    # Live 2026-09-01 the fleet departed with no destination, the mission still wanted the
    # gather, and ENTER_BUILDING was dispatched at sea every tick: `tap_building_entry` read
    # the minimap's own tab strip as a building list and cycled its four icons for minutes.
    # The refusal was correct every time and concluded nothing, so it repeated.
    CAN_START = ("OPEN_WORLD_MAP",)

    # ...AND WHERE IT LANDS. `CAN_START` says what a world can begin; this says where that
    # gets you, and the pair is what makes a ROUTE computable rather than a single hop.
    LEADS_TO = {"OPEN_WORLD_MAP": "world_map"}

    GOALS: tuple = (ArriveAshore, ReadHold)

    # How many ETA readings to collect before calling a ship stopped. Two would do it in
    # principle; three tolerates a single misread without sending the fleet back to the map.
    # Only reached when the speed cannot be read at all.
    _READINGS_BEFORE_JUDGING = 3

    # How many consecutive 0.0 readings before believing them. One is what a ship still
    # accelerating out of port looks like.
    _ZEROS_BEFORE_STOPPED = 2

    # How long to wait before looking again while movement is still UNCONFIRMED. The
    # supply-derived cadence is minutes long, which is right for a voyage under way and far
    # too slow to notice a fleet that never left: three ~6-minute waits to learn that nothing
    # happened. Once the ship is confirmed moving, pacing goes back to the state's own.
    _UNCONFIRMED_CHECKBACK_S = 20.0
    # LOOK A FEW TIMES BEFORE LOOKING AWAY (user, 2026-08-29). The first reading of a voyage
    # is one sample of a reader that is known to slip, and pacing on it alone let a single
    # '415 days' buy 618 minutes of silence. Three short looks cost about a minute and give
    # the readings below something to disagree with.
    _READINGS_BEFORE_PACING = 3
    # Distinct from the unconfirmed wait above, and deliberately longer: a ship that has not
    # been seen to move needs looking at SOONEST, and collapsing the two would lose that
    # ordering. This is a warm-up, not an alarm.
    _WARMUP_CHECKBACK_S = 30.0

    def __init__(self, *, hud_fn=None, checkback_fn=None, speed_fn=None) -> None:
        self._hud = hud_fn
        self._checkback = checkback_fn
        self._speed = speed_fn
        self._goal_key = None
        self._etas: list = []
        self._supplies: list = []
        self._zeros = 0
        self._moving = None

    def work(self, goal: Any, state: Any) -> ActivityResult:
        """One look at the HUD, and a verdict on whether the ship is moving.

        Except for a `ReadHold`, which the sea serves as readily as a port does — the fleet
        panel is behind the hamburger and the hamburger is there at sea.
        """
        if isinstance(goal, ReadHold):
            where = getattr(state, "state", None) or getattr(state, "location", None)
            return read_the_hold(where, getattr(state, "port", None))

        # THE HISTORY BELONGS TO THIS VOYAGE, NOT TO THE ACTIVITY. `default_activities()`
        # builds one SeaActivity and the dispatcher reuses it, so ETAs kept across goals would
        # compare this voyage against the last one (Guiding Principle #4).
        key = str(goal)
        if key != self._goal_key:
            self._goal_key, self._etas, self._zeros, self._moving = key, [], 0, None
            self._supplies = []

        hud = self._read_hud(state)
        days = hud.get("supply_days")
        eta = hud.get("eta_days")
        dest = hud.get("destination")

        if eta is not None:
            self._etas.append(float(eta))
        if days is not None:
            self._supplies.append(float(days))

        speed = self._read_speed(state)
        moving = self._is_moving(speed)
        self._moving = moving
        observed = {"supply_days": days, "eta_days": eta, "destination": dest,
                    "speed": speed, "moving": moving, "etas_seen": len(self._etas),
                    "checkback_s": self._checkback_seconds(days, eta)}

        if moving is False:
            # NOT MOVING IS A REPORT, NOT A DECISION. Where to sail is the mission's, and
            # opening the map to say so is the dispatcher's.
            logger.warning(f"[sea] the ETA has not moved across {len(self._etas)} readings "
                           f"({self._etas}) — the ship does not appear to be under way")
            return ActivityResult(BLOCKED, observed,
                                  detail="at sea but not moving — no destination set")

        logger.info(f"[sea] sailing{f' to {dest}' if dest else ''} "
                    f"(supply={days}d eta={eta}d, {len(self._etas)} reading(s)) — next look "
                    f"in {observed['checkback_s'] / 60:.1f} min")
        return ActivityResult(WORKING, observed, detail="at sea")

    # ── helpers ─────────────────────────────────────────────────────────────
    def _is_moving(self, speed):
        """True / False / None — None while there is not enough to say.

        Speed first, because it answers immediately. The ETA is the fallback for a HUD whose
        speed tile cannot be read, and it compares the readings that EXIST: ticks with an
        unreadable ETA are absent from the list rather than recorded as zeros, so a stretch
        behind the idle lock delays the verdict instead of manufacturing one.
        """
        if speed is not None:
            if speed > 0:
                self._zeros = 0
                return True
            self._zeros += 1
            if self._zeros >= self._ZEROS_BEFORE_STOPPED:
                return False
            return None                      # one zero is a ship still gathering way

        if len(self._etas) < self._READINGS_BEFORE_JUDGING:
            return None                      # not enough evidence yet — keep sailing
        return self._etas[-1] < self._etas[0]

    def _read_speed(self, state):
        """Knots, or None. 0.0 IS A READING, not a failure — telling them apart is the
        whole point, so this must never turn an unreadable tile into a zero."""
        if self._speed is not None:
            return self._speed()
        try:
            from vision.sea_hud import read_speed
            frame = getattr(state, "frame", None)
            if frame is None:
                from capture.adb_capture import capture_screen
                frame = capture_screen()
            # `locate=True` finds the tile from the mini-map's real position instead of a
            # fixed crop the UI has drifted ~120px away from. It costs an OmniParser parse,
            # which a tick paced in minutes can well afford.
            return read_speed(frame, locate=True)
        except Exception as exc:
            logger.debug(f"[sea] speed unreadable: {exc}")
            return None

    def _read_hud(self, state) -> dict:
        if self._hud is not None:
            return self._hud() or {}
        try:
            from actions.sail_actions import read_sea_hud
            frame = getattr(state, "frame", None)
            return read_sea_hud(frame) or {}
        except Exception as exc:
            logger.debug(f"[sea] HUD unreadable: {exc}")
            return {}

    def _checkback_seconds(self, days: Optional[float],
                           eta_days: Optional[float] = None) -> float:
        """How long before looking again — PACING BELONGS TO THE STATE, not to a loop.

        `_await_route_arrival` derived this from supply so a fleet with five days aboard was
        re-checked in ~6 real minutes rather than every few seconds. That reasoning is sound
        and survives; what changes is where it lives. It is reported to the caller as part of
        the observation, and the tick loop honours it.
        """
        if self._moving is not True:
            # NOT CONFIRMED MOVING — look again soon. The supply cadence is right for a
            # voyage under way and useless for noticing one that never began.
            return self._UNCONFIRMED_CHECKBACK_S
        if len(self._etas) < self._READINGS_BEFORE_PACING:
            # UNDER WAY, BUT BARELY LOOKED AT. Sleeping on the first reading means the whole
            # voyage is paced by one sample.
            return self._WARMUP_CHECKBACK_S
        if self._checkback is not None:
            return float(self._checkback(days))
        # THE LOWEST READING WINS, in both directions. Supply only falls and the ETA only
        # falls, so across a voyage's readings the smallest is the closest to now — and a
        # spuriously HIGH one (415 days, 115 days) can only ever lengthen a sleep, which is
        # the failure being guarded. Taking the minimum makes a bad reading harmless without
        # needing to decide which reading was bad.
        if self._supplies:
            days = min(self._supplies) if days is None else min(days, min(self._supplies))
        if self._etas:
            eta_days = min(self._etas) if eta_days is None else min(eta_days, min(self._etas))
        if days is None:
            wait = 120.0
        else:
            try:
                from brain.supply_planner import supply_checkback_seconds
                wait = float(supply_checkback_seconds(days))
            except Exception:
                wait = 120.0

        # NEVER SLEEP PAST THE END OF THE LEG. Supply says when the fleet runs dry; the ETA
        # says when the voyage is over, and that is sooner. This is not a test of whether the
        # leg is done — that is the shore's to answer, never the sea's — only a bound on how
        # long it is safe to look away.
        #
        # Pacing on supply alone waited out a reading of 413 days — (413-1) x 90s = 618
        # minutes — on a leg whose ETA was ONE day. The arithmetic was right; the question
        # was.
        if eta_days is not None:
            try:
                from brain.supply_planner import GAME_DAY_SECONDS
                wait = min(wait, float(eta_days) * GAME_DAY_SECONDS)
            except Exception:
                pass

        # AND NEVER ON ONE READING ALONE. `413` was a misread — every other look that voyage
        # said 13.5 — and a single bad digit bought ten hours of silence. A cap costs one
        # extra look on a genuinely long leg and bounds what any misread can do. It also
        # keeps the phone awake: live 2026-08-29 the screen idle-locked while the bot slept,
        # which is an observation, not a theory (user).
        return max(_MIN_CHECKBACK_S, min(wait, _MAX_CHECKBACK_S))


def read_the_hold(where, port) -> ActivityResult:
    """What the ship can carry and what is aboard. ONE implementation, two activities.

    THE HOLD READS AT SEA TOO. It was served only from ashore, so a `ReadHold` while the fleet
    was at sea found no activity and no transition and simply stalled — the same missing-route
    shape as the world map before CLOSE_WORLD_MAP. Checked against the real game with the
    fleet in open water: cargo 639/4952, supply 11.6d. The hamburger is there.

    GATE ON THE NUMBERS, NOT ON AN `ok` FLAG: a read that returned the capacity and the cargo
    has done its job whether or not it set one, and treating a missing flag as failure re-read
    a hold that had already been read.
    """
    from actions.fleet_status import read_fleet_status

    status = read_fleet_status() or {}
    capacity, used = status.get("cargo_capacity"), status.get("cargo_used")
    if capacity is None or used is None:
        # BLOCKED, not FINISHED-with-nothing. The dispatcher clears obstructions and
        # re-perceives between ticks, so the retry that used to be spelled out here happens
        # by itself — and if the hamburger is missing because we are standing in a building,
        # the next tick routes out of it rather than reading a third time.
        return ActivityResult(BLOCKED, {"port": port, "state": where,
                                        "reason": status.get("reason")},
                              detail=f"the hold did not read: {status.get('reason')}")
    # CAPACITY IS THE SHIP, NOT THE CARGO. It changes when the fleet changes, not when
    # anything is bought, sold or bartered — so it is worth remembering, and remembering it
    # is what lets a mission start somewhere the ☰ does not exist.
    try:
        from memory.observed_facts import remember
        remember("fleet_capacity", int(capacity))
    except Exception as exc:
        logger.debug(f"[hold] could not remember the capacity: {exc}")
    return ActivityResult(FINISHED,
                          {"cargo_capacity": capacity, "cargo_used": used,
                           "supply_days": status.get("supply_days"),
                           "port": port, "state": where},
                          detail=f"hold {used}/{capacity} at {port or where}")
