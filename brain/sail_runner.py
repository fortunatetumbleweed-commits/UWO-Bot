"""The task runner for one sail leg, port to port or port to village. PASSIVE.

Guiding Principle #7. It answers one question per consult: given where the fleet is and what
just came back, what should the dispatcher do next?

    in a building        -> nothing to ask; the dispatcher routes out
    at a port, departing -> Depart (the harbour, so the leg starts topped up)
    at sea, no course    -> ChooseDestination
    at sea, under way    -> ArriveAshore
    ashore after sailing -> done

WHAT THIS REPLACES. `SailToGoal` is an eight-phase state machine — INIT, EXIT_BUILDING,
GO_TO_HARBOR, FLEET_CHECK, DEPART, SEA_NAVIGATE, WORLD_MAP, SAILING — that walks to the
harbour, opens the world map, picks the destination and then POLLS FOR ARRIVAL in its own
SAILING phase. Every one of those has an owner now:

  EXIT_BUILDING / GO_TO_HARBOR   the dispatcher's routing (ENTER_BUILDING, EXIT_BUILDING)
  DEPART                          HarborActivity
  SEA_NAVIGATE / WORLD_MAP        ChooseDestination -> OPEN_WORLD_MAP -> WorldMapActivity
  SAILING                         SeaActivity, which watches the ETA

The SAILING phase is the one that mattered most. It was a second sea watch that did not
perceive through the dispatcher, so it received neither the interruptor pass that dismisses a
daily-news popup nor `IdleLockActivity` — the exact failure `_await_route_arrival` was deleted
for, still live on every port and village leg.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from brain.activities.harbor import Depart
from brain.activities.sea import ArriveAshore
from brain.activities.world_map import ChooseDestination
from brain.dispatcher import still_working, BLOCKED, FINISHED, UNRECOGNISED, WORKING

UNDER_WAY = "under-way"
ARRIVED = "arrived"
FAILED = "failed"

# Where a voyage can end. Anywhere ashore, in other words — which port it is gets checked
# against the destination by the caller, not here.
_ASHORE = ("port_overworld", "village", "port_map")
_AT_SEA = ("sea", "sea_cinematic")


@dataclass
class SailRunner:
    """One leg to `destination`.

    `resupply` departs through the harbour rather than straight off the map, so the leg
    starts topped up — supplies load at Supply Departure, which is why 0 days in port is
    normal and gating on it aborts good missions.
    """

    destination: str
    kind: str = "port"                 # 'port', 'village' or 'route'
    resupply: bool = True
    min_supply_days: Optional[float] = None

    status: str = UNDER_WAY
    reason: str = ""
    port: Optional[str] = None

    # Bounds. Each is a backstop against a step that reports success and changes nothing,
    # never a policy: the world decides when a leg is done, and these decide when to stop
    # asking it the same question.
    _MAX_DEPARTS: int = 2
    _MAX_COURSES: int = 2

    # TWO DIFFERENT FACTS, and collapsing them made a leg arrive before it left.
    #   `_seen_sea`      — the fleet has been OBSERVED at sea. Only this can mean arrival.
    #   `_departure_ok`  — the harbour CONFIRMED a departure. Only this suppresses re-asking.
    # Setting one flag for both read "departed and ashore" as arrival on the tick between the
    # harbour's confirmation and the ship actually leaving, so the leg finished at its origin.
    _seen_sea: bool = field(default=False)
    _departure_ok: bool = field(default=False)
    _course_set: bool = field(default=False)
    _departs: int = field(default=0)
    _courses: int = field(default=0)
    _pending: Any = None

    # ── the interface the dispatcher uses ────────────────────────────────────

    def next_goal(self, result: Optional[Any], state: Any) -> Optional[Any]:
        if result is not None and self._absorb(result):
            return None

        where = getattr(state, "state", None) or getattr(state, "location", None)

        if where in _AT_SEA:
            # BEING AT SEA IS THE DEPARTURE, whatever produced it. The fleet may have been at
            # sea when the leg began, so this is read from the world rather than remembered
            # from a Depart that may never have been asked for (Guiding Principle #2).
            self._seen_sea = True

        # ALREADY THERE? THEN DO NOT SAIL. `drive_sail_to` checked this and it is not
        # optional: the scheduler can pick the port the fleet is standing in, and a leg that
        # departs anyway crosses the map to arrive where it started. Live 2026-08-17 (Malé)
        # it did exactly that, because the port name read None and the check was skipped.
        #
        # The name is compared loosely because OCR is: 'Lisboa' may come back with a trailing
        # mark or a dropped accent, and a prefix on either side is enough when the answer only
        # decides whether to sail at all.
        if not self._seen_sea and where in _ASHORE and _same_place(
                getattr(state, "port", None), self.destination):
            self.status = ARRIVED
            self.port = getattr(state, "port", None)
            self.reason = f"already at {self.destination}"
            logger.info(f"[sail] {self.reason} — not sailing")
            self._pending = None
            return None

        if self._seen_sea and where in _ASHORE:
            # ARRIVED. Which port it is belongs to the caller: this runner was told where to
            # sail, not how to recognise success, and re-checking the name here would be a
            # second copy of a decision the mission already owns.
            self.status, self.port = ARRIVED, getattr(state, "port", None)
            self._pending = None
            return None

        # THE GOAL PERSISTS UNTIL THE PHASE ENDS, whatever screen we are looking at.
        #
        # Returning None for a screen this runner does not recognise ended the leg: None
        # means FINISHED to the loop, so standing in the harbour — where the Depart is
        # actually worked — stopped the task. It is also what makes routing possible at all,
        # since `to_intent` needs a goal to turn into ENTER_BUILDING or EXIT_BUILDING. So the
        # phase decides what to ask for, and where we are decides only whether the phase has
        # moved on.
        # THE COURSE COMES FIRST, THEN THE DEPARTURE (user, 2026-08-29).
        #
        # This asked for the departure first and set the course afterwards, at sea. The ship
        # duly left London and sat there: HUD destination None, ETA None, speed 0.0 — put to
        # sea with nowhere to go. Supply Departure sails toward a destination that has
        # ALREADY been chosen; without one it is just leaving.
        #
        # The order is safe whichever way the game behaves. If committing a destination on
        # the map sails immediately, the next tick reads `sea`, `_departed` becomes true by
        # OBSERVATION and the harbour step is skipped. If it only sets the course, the
        # harbour then departs toward it — topped up, which is why the leg goes that way at
        # all.
        if not self._course_set:
            return self._ask_for_a_course()
        if not (self._seen_sea or self._departure_ok):
            return self._ask_to_depart()
        self._pending = ArriveAshore(f"the leg to {self.destination}")
        return self._pending

    # ── what to ask for ──────────────────────────────────────────────────────

    def _ask_to_depart(self):
        if not self.resupply:
            # Straight off the map. The course itself is what puts the fleet to sea.
            return self._ask_for_a_course()
        if isinstance(self._pending, Depart):
            return self._pending          # the same attempt, still being worked
        if self._departs >= self._MAX_DEPARTS:
            self.status = FAILED
            self.reason = f"could not put to sea after {self._departs} attempts"
            logger.warning(f"[sail] {self.reason}")
            self._pending = None
            return None
        self._departs += 1
        self._pending = Depart(destination=self.destination)
        logger.info(f"[sail] departing for {self.destination!r} "
                    f"({self._departs}/{self._MAX_DEPARTS})")
        return self._pending

    def _ask_for_a_course(self):
        if isinstance(self._pending, ChooseDestination):
            return self._pending          # the same attempt, still being worked
        if self._courses >= self._MAX_COURSES:
            self.status = FAILED
            self.reason = (f"could not set a course for {self.destination!r} after "
                           f"{self._courses} attempts")
            logger.warning(f"[sail] {self.reason}")
            self._pending = None
            return None
        self._courses += 1
        self._pending = ChooseDestination(where=self.destination, kind=self.kind)
        logger.info(f"[sail] setting the course for {self.destination!r} "
                    f"({self._courses}/{self._MAX_COURSES})")
        return self._pending

    # ── updating task status ─────────────────────────────────────────────────

    def _absorb(self, result: Any) -> bool:
        """True when there is nothing more to ask for."""
        status = getattr(result, "status", None)
        observed = getattr(result, "observed", None) or {}

        # THE SUPPLY FLOOR IS WEIGHED HERE, because it is a MISSION question. `SeaActivity`
        # reports the days aboard on every tick and decides nothing — whether to press on or
        # turn back needs the destination, which is this runner's. A village has no harbour,
        # so its leg carries a round-trip floor and running dry means the game force-returns
        # the fleet (the gather run died that way on 2026-08-17).
        #
        # Reported, not diverted: where to resupply is the mission's, one layer further up
        # again. Saying so beats inventing a recovery here.
        days = observed.get("supply_days")
        if (self.min_supply_days is not None and days is not None
                and float(days) < float(self.min_supply_days)):
            self.status = FAILED
            self.reason = (f"supply is down to {days}d, below the "
                           f"{self.min_supply_days}d floor for the leg to {self.destination}")
            logger.warning(f"[sail] {self.reason}")
            self._pending = None
            return True

        # ONLY A TERMINAL RESULT ENDS AN ATTEMPT.
        #
        # WORKING means an activity is part-way through, and UNRECOGNISED means no activity
        # served this goal HERE so the dispatcher is routing toward one — a transition in
        # flight. Neither is a failed attempt, and clearing the pending goal on either makes
        # the next tick count a fresh one.
        #
        # Live 2026-08-29: `Depart` at a port_overworld is served by nobody (AshoreActivity is
        # there but serves ArriveAshore and ReadHold), so every tick of the walk to the
        # harbour reported "no activity for state 'port_overworld'". The two-attempt bound was
        # spent in two ticks and the leg gave up while the character was still walking.
        if status in (WORKING, UNRECOGNISED):
            return False

        pending, self._pending = self._pending, None

        # WHICH GOAL PRODUCED THIS? FINISHED means something different for each, and reading
        # it without the goal ends the leg at the first success of any kind.
        if isinstance(pending, ChooseDestination):
            # AN IN-PROGRESS TICK IS NOT A REFUSAL. The world map takes several actions to
            # set a course — open the list, type, tap the row, press Move — and says WORKING
            # through all of them. Reading that as "did not take" recorded a failure on every
            # step of a working course-set and logged it as one. See `still_working`.
            if still_working(result):
                return False
            self._course_set = status == FINISHED
            if not self._course_set:
                logger.info(f"[sail] the course did not take: "
                            f"{getattr(result, 'detail', '') or status}")
            return False

        if isinstance(pending, Depart):
            if status == FINISHED:
                # A CONFIRMED DEPARTURE IS COMMITTED, even though the fleet is not at sea
                # yet. The harbour said it tapped Supply Departure and the ship left; what
                # follows is the departure cinematic, and asking again during it is repeating
                # an action whose effect has not landed — which the dispatcher refuses to do
                # for an intent, and this must refuse for an activity's action.
                #
                # Live 2026-08-29: "departed for Amsterdam via supply_depart", then one tick
                # still read as the harbour, so it asked again, found no departure button
                # (there is none once you have sailed), and that doomed second attempt
                # exhausted the bound. The leg failed on a departure that had worked.
                logger.info(f"[sail] {self.destination}: the harbour confirmed the "
                            "departure — watching for the sea rather than asking again")
                self._departure_ok = True
                return False
            if status == BLOCKED:
                self.reason = getattr(result, "detail", "") or "the harbour refused to depart"
                logger.info(f"[sail] {self.reason}")
            return False

        if isinstance(pending, ArriveAshore):
            if status == FINISHED:
                # ASHORE IS NOT ARRIVED UNTIL WE HAVE BEEN AT SEA. `ArriveAshore` means
                # "keep going until the fleet is no longer at sea", and standing in the port
                # we started from satisfies that on the tick it is asked. The two-flag note
                # at the top of this class is about exactly this — "collapsing them made a
                # leg arrive before it left" — and it was applied to the other arrival path
                # and not to this one.
                #
                # Live 2026-09-01: the harbour tapped Supply Departure with no course set,
                # `ArriveAshore` was dispatched at `port_overworld` at TRIPOLI, came back
                # FINISHED at once, and the leg to Barcelona was ARRIVED before the ship had
                # moved. The mission went on to the market order and spent twenty minutes at
                # sea asking to enter a building.
                #
                # Not a failure — the departure simply has not landed. Returning False keeps
                # the phase running, so a course and a departure are asked for again, and
                # `_MAX_COURSES`/`_MAX_DEPARTS` bound how long that can go on.
                if not self._seen_sea:
                    logger.info(f"[sail] ashore at {observed.get('port') or 'somewhere'} "
                                f"having never been at sea — the departure for "
                                f"{self.destination!r} has not landed, not arrival")
                    return False
                self.status, self.port = ARRIVED, observed.get("port")
                return True
            if status == BLOCKED and observed.get("moving") is False:
                # NOT MOVING. The course did not take after all — ask for it again rather
                # than watching an ETA that will never fall.
                logger.info("[sail] at sea but not moving — the course needs setting again")
                self._course_set = False
                return False
            if status == BLOCKED:
                self.status = FAILED
                self.reason = getattr(result, "detail", "") or "the leg was blocked"
                return True

        return False


def _same_place(port: Optional[str], destination: str) -> bool:
    """Is the port we are standing in the one we were going to?

    A prefix match either way, five characters minimum: OCR drops accents and adds marks, and
    a village's own interior reports no name at all — which is why this returns False for a
    missing port rather than guessing, and the leg proceeds.
    """
    import unicodedata

    def _flat(x: str) -> str:
        # The map panel reads names WITHOUT accents ('Male') while the catalogue stores them
        # with ('Malé'), so a comparison that keeps them never matches the one port it most
        # needs to: the one the fleet is standing in.
        return "".join(c for c in unicodedata.normalize("NFKD", (x or "").strip().lower())
                       if not unicodedata.combining(c))

    a, b = _flat(port), _flat(destination)
    if not a or not b:
        return False
    n = min(5, len(a), len(b))
    return a[:n] == b[:n]
