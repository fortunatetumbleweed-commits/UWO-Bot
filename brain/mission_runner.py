"""The task runner for a whole barter mission. PASSIVE — it is consulted, it does not drive.

Guiding Principle #7, at the level above the sail leg. The dispatcher settles to a world,
consults this for a work order, and dispatches it. This picks WHICH leg of the mission is next
and WHAT that leg wants; it never sails, buys or barters, and it never looks at a screen.

WHAT THIS REPLACES. `run_mission` walks the same graph, but it EXECUTES each leg: it calls
`executors[kind](task)`, and each executor runs a whole leg to completion with its own loops
inside — sail there, walk to the market, buy, come back. So the dispatcher never saw the
mission at all. It was handed "sail to Amsterdam" by an executor that had already decided to
gather there, and the reason WHY — Iron 822, at Amsterdam, because the plan says so — lived a
layer above anything the dispatcher could consult (user, 2026-08-29: "it should be still at
gathering part, and it should include what is on the next list at where").

Two levels of task-running became one. The leg runners below are the same passive shape as
`SailRunner`, so a leg that is a voyage IS a `SailRunner`, and the mission simply asks it what
it wants next.

WHAT IT STILL DOES NOT OWN. Which port stocks what, whether a tile is greyed, how a panel
reads — none of that is here. This answers only "which leg, and what does it want", from the
graph and from where the fleet is.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from loguru import logger

from brain.activities.market import FreeHold, Hold, SellHold, TrimHold
from brain.activities.village import Barter
from brain.dispatcher import BLOCKED, FINISHED, UNRECOGNISED, WORKING
from brain.activities.sea import ReadHold
from brain.sail_runner import _same_place, ARRIVED, SailRunner

RUNNING = "running"
DONE = "done"
FAILED = "failed"


# ── the small runners, one per kind of leg ───────────────────────────────────
#
# Each has the same interface as any other task runner: `next_goal(result, state)` returns a
# work order or None, and `status` says how it ended. A leg that is a voyage delegates to
# `SailRunner` rather than reimplementing it.


# Legs whose work happens INSIDE A BUILDING, which means the fleet has to be ashore for them
# to be possible at all. The graph gives them no location because they run wherever the fleet
# already is — "the port name is decoration" — and that is true right up until the fleet is
# not at a port.
_ASHORE_ONLY = ("sell_surplus", "sell")

# ...and "ashore" is not enough: a VILLAGE is ashore and has no market. Its left menu is
# Explore / Gifting / Loot / Recruit Crew / Barter — there is no building list to open, so
# ENTER_BUILDING has nothing to tap and the tap that reaches for one changes nothing.
#
# Live 2026-08-30: a run started at Svear with the mission's optional trim first. The market
# intent went out at the village on every tick — "already dispatched ... nothing changed" —
# and the run stopped on the no-progress guard before the barter it had sailed there for.
#
# THAT LIST USED TO LIVE HERE as ("sea", "sea_cinematic", "village"), beside the dispatcher's
# separate `CAN_START` declarations — two hand-written answers to "can this world do that",
# free to drift apart. `run_goal.port_is_underfoot` derives it now from the SERVES every
# activity already declares. See its docstring for why the mission's question and the
# dispatcher's cannot yet be the same one.


@dataclass
class _VoyageStep:
    """Getting somewhere. The voyage itself is `SailRunner`, which is also what the resume
    paths use — this only adapts it to the step interface.

    IT DOES NOT SAIL. The sea activity does; `SailRunner` asks for a course, a departure and
    then `ArriveAshore`, and it is the sea that watches the ETA (user, 2026-08-29: "sailing is
    just a sub state in the sea activity").
    """

    destination: str
    kind: str = "port"
    min_supply_days: Optional[float] = None

    status: str = RUNNING
    reason: str = ""
    _sail: Any = None

    def next_goal(self, result, state):
        if self._sail is None:
            self._sail = SailRunner(destination=self.destination, kind=self.kind,
                                    min_supply_days=self.min_supply_days)
        goal = self._sail.next_goal(result, state)
        if goal is None:
            self.status = DONE if self._sail.status == ARRIVED else FAILED
            self.reason = self._sail.reason
            if self.status == DONE:
                self._check_we_are_where_we_meant_to_be()
        return goal

    def _check_we_are_where_we_meant_to_be(self) -> None:
        """The step's own postcondition: getting somewhere means being THERE.

        `SailRunner` hands this over in as many words — "ARRIVED. Which port it is belongs to
        the CALLER: this runner was told where to sail, not how to recognise success" — and
        records the arrival port for us to read. Nobody read it. The step said DONE on the
        runner's status alone, so "we are ashore" and "we are ashore at Barcelona" were the
        same answer, and a voyage that never happened checked itself off (live 2026-09-01).

        This is what makes a leg's done-ness a QUESTION ABOUT THE WORLD rather than a flag an
        action sets: the port name is on the screen, so a fresh look can prove it false.

        UNKNOWN IS NOT WRONG. A village interior reports no name and the port reader has come
        back None before; refusing on that would strand voyages that were fine. Only a name
        that is READ and DIFFERENT is a failure.
        """
        arrived_at = getattr(self._sail, "port", None)
        if not arrived_at or self.kind != "port":
            return
        if _same_place(arrived_at, self.destination):
            return
        self.status = FAILED
        self.reason = (f"the voyage ended at {arrived_at!r}, not {self.destination!r} — "
                       "the leg is not done")
        logger.warning(f"[mission_runner] {self.reason}")


@dataclass
class _OneGoalLeg:
    """A leg that is a single work order — the market ones, and the barter.

    It ends when the goal does. BLOCKED is a refusal the mission must weigh, not a crash: a
    market with nothing to sell and a village with no rounds left both come back that way.
    """

    goal: Any
    status: str = RUNNING
    reason: str = ""
    _asked: bool = False

    def next_goal(self, result, state):
        if result is not None and self._asked:
            s = getattr(result, "status", None)
            if s == FINISHED:
                self.status = DONE
                return None
            if s == BLOCKED:
                self.status = FAILED
                self.reason = getattr(result, "detail", "") or "the leg was blocked"
                return None
            # WORKING / UNRECOGNISED: still going, or the dispatcher is routing to it.
            # This runner had it right from the start, and `brain.dispatcher.still_working`
            # is that judgement made shared — after `barter_runner` reached the opposite one
            # and abandoned a barter mid-flight (2026-09-04). Falling through re-asks for the
            # same goal, which is what "still going" means.
        self._asked = True
        return self.goal




# ── the mission ──────────────────────────────────────────────────────────────


def _same_port(a, b) -> bool:
    """Two port names for the same place — accent-blind and case-blind.

    The catalogue, the mission graph and the screen do not agree on accents ('Lubeck' /
    'Lübeck'), so comparing them raw would answer "not here" while standing in the port.
    """
    if not a or not b:
        return False
    # FOLDED HERE, NOT BORROWED. Reaching into `barter_mission_live` for `_strip_accents`
    # pulls this module's imports through a façade that still touches UI — the layering
    # ratchet caught it (tests/test_the_layering_is_enforced.py). Comparing two names needs
    # no help from the UI layer.
    import unicodedata

    def _fold(name):
        return "".join(c for c in unicodedata.normalize("NFD", str(name))
                       if unicodedata.category(c) != "Mn").casefold()

    return _fold(a) == _fold(b)


@dataclass
class MissionRunner:
    """The graph, walked one work order at a time."""

    subtasks: list
    coords: Mapping[str, tuple]
    good: str
    village: str
    keep: tuple = ()
    min_supply_days: Optional[float] = None

    status: str = RUNNING
    reason: str = ""
    completed: list = field(default_factory=list)

    _leg: Any = None               # the SubTask being worked
    _runner: Any = None            # the step being worked
    _steps: list = field(default_factory=list)   # the steps left in this leg

    # A LEG IS ONE OR MORE STEPS, and the mission advances through them exactly as it
    # advances through legs. `_SailThenLeg` used to nest a sequencer inside a sequencer to
    # say "sail there, then buy"; two levels of the same walking, one of them hidden
    # (user, 2026-08-29). A gather is now simply [voyage, purchase].

    # ── the interface the dispatcher uses ────────────────────────────────────

    def where_it_stopped(self) -> str:
        """What the mission was doing when it stopped, for a caller whose loop ran out.

        `run_task` gives up after enough ticks with no progress, and the runner is still
        RUNNING at that point — so its `reason` is empty and the caller reported only "the
        mission stopped". Which leg it was on is the whole of the useful answer.
        """
        if self.reason:
            return self.reason
        if self._leg is not None:
            return f"stalled on {self._leg.id} ({self._leg.kind})"
        return "stopped before any leg started"

    def next_goal(self, result: Optional[Any], state: Any) -> Optional[Any]:
        """The next work order, or None when the mission is over.

        A LOOP, NOT A LADDER. A step can finish the moment it is asked — a voyage to the port
        the fleet is already standing in is done immediately — so advancing has to keep going
        until something actually wants doing. Written as a ladder, that case returned None on
        the first call and the mission read as finished before it started.
        """
        # A BUY THAT MET THE WHOLE LIST FINISHES EVERY GATHER LEG. The leg carries the
        # outstanding union, so `met` means nothing is left to fetch anywhere — and the legs
        # still pending name PORTS, not materials nobody has.
        #
        # This was written and never called (live 2026-08-29): Amsterdam bought all three
        # materials and reported met, Barcelona then bought two of them AGAIN, and the fleet
        # was on its way to Tripoli for a third helping of Candle. The union shrank each time
        # only because the previous leg was marked done, which hid the repetition behind a
        # smaller order every port.
        if self._leg is not None and getattr(self._leg, "kind", None) == "gather":
            _obs = getattr(result, "observed", None) or {}
            self._settle_gathers(bool(_obs.get("met")), _obs.get("materials"))

        for _ in range(len(self.subtasks) * 4 + 8):     # a leg cannot need more turns
            if self._runner is None:
                if not self._start_next_leg(state):
                    return None
                result = None                            # a fresh step gets a fresh start

            goal = self._runner.next_goal(result, state)
            if goal is not None:
                return goal

            if self._runner.status != DONE:              # the step gave up
                self._finish_leg()
                return None
            if self._steps:                              # more of THIS leg to do
                self._runner = self._steps.pop(0)
            else:
                self._finish_leg()
                if self.status != RUNNING:
                    return None
            result = None

        self.status = FAILED
        self.reason = "the mission advanced without ever asking for anything"
        logger.warning(f"[mission_runner] {self.reason}")
        return None


    # ── walking the graph ────────────────────────────────────────────────────

    def _everything_still_wanted(self) -> dict:
        """Every material any unfinished gather leg is waiting for.

        The union, not this leg's share — a market that has a material we still need is the
        cheapest place we will ever buy it, and it is on the screen either way.
        """
        wanted: dict = {}
        for t in self.subtasks:
            if t.done or t.kind != "gather":
                continue
            for material, qty in (getattr(t, "params", None) or {}).get("orders", {}).items():
                wanted[material] = max(wanted.get(material, 0), int(qty))
        return wanted

    def _settle_gathers(self, met: bool, materials: Optional[dict] = None) -> None:
        """Finish every gather leg whose OWN materials are aboard.

        TWO FLAGS, NOT ONE (user, 2026-09-02): a material is met or it is not, and the
        GATHERING is done only when every material is met. Sending each port the whole
        outstanding list is the optimisation that makes one visit buy several materials —
        Barcelona stocks Iron and Matchlock Gun both — so the list is what to BUY, and the
        per-material states are what to JUDGE BY. Judging a leg by the whole-order verdict
        conflates them, and that was the gap recorded here since 2026-08-30:

            Iron was pinned to Amsterdam; Barcelona sources it too and bought the whole
            outstanding list there, finishing Iron at 972 against 822. Candle was still
            short, so `met` was False, so `gather:Amsterdam` stayed pending — and the fleet
            sailed to Amsterdam for a material it already had 150 spare of.

        `met` still settles everything at once: nothing is left to fetch anywhere. Below
        that, a leg closes when each material IT wants reads "met". A material whose amount
        is UNKNOWN never closes one — the ledger says unknown precisely so someone goes and
        reads the sell grid, and treating it as covered would strand the material.

        What made the breakdown available: `material_states` used to be built inside
        `_goal_met` and flattened to a sentence, so the only thing that reached here was a
        boolean over the union — which is why the comment above used to say there was "no
        breakdown to give".
        """
        if met:
            for t in self.subtasks:
                if not t.done and t.kind == "gather":
                    logger.info(f"[mission_runner] {t.id} settled — its materials came aboard")
                    t.done = True
            return
        if not materials:
            return
        for t in self.subtasks:
            if t.done or t.kind != "gather":
                continue
            orders = (getattr(t, "params", None) or {}).get("orders") or {}
            if not orders:
                continue
            states = {m: (materials.get(m) or {}).get("state") for m in orders}
            if all(s == "met" for s in states.values()):
                logger.info(f"[mission_runner] {t.id} settled — {', '.join(orders)} "
                            "already aboard, bought elsewhere")
                t.done = True

    def gathering_is_complete(self) -> bool:
        """Every material this mission still wants is aboard.

        The SECOND flag. Distinct from "no gather legs remain": a leg stops when its runner
        stops, which can happen with a material short — live 2026-09-02 Tripoli finished
        `met: False` at Candle 206/704 and the leg was marked done anyway. Anything that
        means "gathering is behind us" must ask this, not the leg list.
        """
        return not self._everything_still_wanted()

    def _finish_leg(self) -> None:
        leg, runner = self._leg, self._runner
        self._leg, self._runner = None, None
        if runner.status == DONE:
            leg.done = True
            self.completed.append(leg.id)
            logger.info(f"[mission_runner] {leg.id} done")
            return
        # TRIMMING IS SUPPORT, NOT A LEG OF THE MISSION (CLAUDE.md: "the task is gather,
        # barter, sell; trim, supply and capacity are SUPPORT — opportunistic when the place
        # affords them, never mandatory legs"). It frees space, and a hold that stays full is
        # something the barter already handles: the overflow dialog trades space for product.
        #
        # Live 2026-09-05 at Madeira the Sell tab did not open — the game drops roughly one
        # tap in twenty — so the trim REFUSED rather than reporting a hold it never saw,
        # which is correct. That refusal then failed the whole mission: the fleet sat one
        # port from Hutu with both materials aboard, and the run ended having bartered
        # nothing. The trim was over-stock by 108 Pig.
        #
        # So a refused trim is reported and stepped over. Every other leg still fails the
        # mission, because gather, barter and sell ARE the mission.
        if leg.kind == "sell_surplus":
            self.completed.append(leg.id)
            leg.done = True
            logger.warning(f"[mission_runner] {leg.id} refused ({runner.reason or 'failed'}) "
                           "— trimming is support, so the mission carries on without it")
            return

        # A LEG THAT REFUSED IS THE MISSION'S PROBLEM, not the leg's. Reported rather than
        # retried here: what to do about a port that will not sell needs the plan.
        self.status = FAILED
        self.reason = f"{leg.id}: {runner.reason or 'failed'}"
        logger.warning(f"[mission_runner] {self.reason}")

    def _can_run_here(self, leg, state) -> Optional[str]:
        """Why this leg cannot run from where the fleet is, or None if it can.

        A market leg at sea is not a failure of the market — there is no market. The graph
        gives these legs no location because they run wherever the fleet already is, which
        assumed the fleet was at a port. Live 2026-08-29 it was adrift, the trim was picked
        anyway, and `tap_building_entry` refused: "Not the building list (no tab icons)".
        It was right to refuse; nothing had told it we were nowhere near a market.
        """
        where = getattr(state, "state", None) or getattr(state, "location", None)
        if leg.kind not in _ASHORE_ONLY:
            return None
        from brain.run_goal import port_is_underfoot
        ashore = port_is_underfoot(where)
        if ashore is False:
            return (f"{leg.id} happens inside a market and there is none on {where!r}")
        # None means the screen COVERS a world rather than replacing it — the map, a notice,
        # the lock. We cannot see what is underneath, so we do not judge; the dispatcher
        # clears it and the next look answers.
        return None

    # Everything the mission does BEFORE it reaches the village. Standing there settles all
    # of it at once — see `_arrived_at_the_village`.
    _THE_APPROACH = ("sell_surplus", "gather", "supply_verify", "sail_to_village")

    def _departed_for_the_village(self, state) -> None:
        """ONCE THE TASK DEPARTS FOR THE VILLAGE THERE IS NO MORE GATHERING (user,
        2026-08-30). The trim and the checks all happen BEFORE that departure; after it the
        mission is just the barter and the sail to sell.

        The departure, not the arrival, is the line — and it is `mission_progress` that
        remembers it, whose own phases already say so: "Leaving this phase ends cargo checking
        for the rest of the task", "Leaving this phase means the goods are aboard; the barter
        is not re-run". Being COMPANY-owned, it survives a restart, so a run interrupted
        mid-voyage does not re-plan itself back into gathering.

        Standing in the village settles it too, as a backstop: being here is what "got here"
        means, and it costs nothing to notice.

        Live 2026-08-30 without this: a run started at Svear with the Birch Tree already
        aboard, skipped the trim (no market in a village), and picked `gather:Amsterdam` —
        setting out to fetch materials it was carrying. It could not leave either, and the
        guard stopped it having done nothing.

        The mechanism it replaces: a village reads `port=None`, so "a leg underfoot wins"
        cannot fire, every distance collapses to 0.0, and `min()` returns whatever leg came
        first in the graph.

        NOTE the assumption, since it is worth seeing: this trusts that A village with a
        pending barter is THE village. The name is not read here (`name=None` on these
        frames), and the mission carries exactly one barter. If missions ever carry two, this
        needs the name.
        """
        if not any(t.kind == "barter" and not t.done for t in self.subtasks):
            return
        where = getattr(state, "state", None) or getattr(state, "location", None)
        past_it = where == "village"
        if not past_it:
            try:
                from brain import mission_progress
                past_it = mission_progress.at_least("bartering")
            except Exception as exc:
                logger.debug(f"[mission_runner] no mission phase to read: {exc}")
        if not past_it:
            return
        for t in self.subtasks:
            if not t.done and t.kind in self._THE_APPROACH:
                logger.info(f"[mission_runner] {t.id} settled — the mission has left for "
                            "the village; gathering is over")
                t.done = True

    def _start_next_leg(self, state) -> bool:
        self._departed_for_the_village(state)
        pending = [t for t in self.subtasks if not t.done]
        if not pending:
            self.status = DONE
            logger.info("[mission_runner] every leg is done")
            return False

        done_ids = {t.id for t in self.subtasks if t.done}
        runnable = [t for t in pending if all(d in done_ids for d in t.deps)]
        if not runnable:
            self.status = FAILED
            self.reason = f"deadlock: nothing runnable (pending={[t.id for t in pending]})"
            logger.warning(f"[mission_runner] {self.reason}")
            return False

        # STANDING ON IT BEATS EVERY DISTANCE. A gather leg names the port that SOURCES its
        # material — Amsterdam has the Iron, Tripoli has the Candle — so a leg whose port is
        # the one under our feet is not merely the nearest, it is the one that costs no
        # voyage at all. Sailing away from a material we are standing on is wrong at any
        # distance, and saying so as a rule means a bad position read can no longer trade it
        # away for a arithmetic comparison.
        #
        # Live 2026-08-29: the fleet was in Amsterdam and the port name read as 'Peking' (a
        # 'Herring' label snapped to the nearest port at 0.62). Ranked by distance from
        # Peking, Tripoli won, and the mission left the Iron it had come for behind.
        here_port = getattr(state, "port", None)
        underfoot = [t for t in runnable if _same_port(t.location, here_port)]
        if underfoot:
            leg = underfoot[0]
        else:
            # THE CHEAPEST FROM WHERE WE ARE NOW, re-decided every time — the same rule
            # `run_mission` used, and the reason it re-evaluated after every leg. Where we
            # are is read from `state`, not remembered from the last leg's destination.
            here = self._here(state)
            leg = min(runnable, key=lambda t: self._distance(here, t.location))

        # CHECK BEFORE ACTING, NOT AFTER (Guiding Principle #6). A leg whose precondition the
        # world does not meet is reported now, rather than dispatched and refused downstream
        # where the reason is a UI message about tab icons.
        cannot = self._can_run_here(leg, state)
        if cannot:
            if getattr(leg, "optional", False):
                # SKIPPED, NOT FAILED. Only the trim before the VILLAGE is required — the
                # barter output has to fit somewhere. The pre-gather clear frees space before
                # buying, which is worth doing and not worth stranding a mission over, and
                # the gather port has a market of its own.
                logger.info(f"[mission_runner] skipping {leg.id}: {cannot}")
                leg.done = True
                return self._start_next_leg(state)
            self.status, self.reason = FAILED, cannot
            logger.warning(f"[mission_runner] {cannot}")
            return False

        self._leg = leg
        steps = self._runner_for(leg)
        self._steps = list(steps) if steps else []
        self._runner = self._steps.pop(0) if self._steps else None
        if self._runner is None:
            self.status = FAILED
            self.reason = f"no work order for a {leg.kind!r} leg ({leg.id})"
            logger.warning(f"[mission_runner] {self.reason}")
            return False
        if leg.kind == "sail_to_village":
            # THE LINE ITSELF. Picking this leg is the departure, and after it the mission
            # never gathers or trims again — recorded where it survives a restart.
            #
            # ONLY IF GATHERING ACTUALLY FINISHED. This marker is monotonic by design
            # ("Never moves backwards") and is read after a restart to SKIP the check and
            # the plan entirely, so writing it on a false premise cannot be undone by
            # anything later observed — only by six hours passing.
            #
            # Live 2026-09-02: Tripoli's Candle buy returned `met: False` at 206/704, the
            # leg was marked done regardless, this line ran, and the run was killed
            # mid-voyage. The relaunch read "already bartering (3387s ago) — skipping the
            # check and the plan", sailed straight to Svear with 206 Candle, and bartered
            # 2 rounds where 6 were planned. It never entered a market at all.
            #
            # Re-planning after a restart is cheap and self-correcting: the plan buys only
            # the SHORTFALL, so a full hold orders nothing. That is a worse-case extra
            # remote check, against a voyage spent carrying materials for a third of the job.
            if not self.gathering_is_complete():
                logger.warning(
                    "[mission_runner] sailing to the village with gathering INCOMPLETE — "
                    f"still wanted: {self._everything_still_wanted()}. Not recording "
                    "'bartering': a restart must re-plan and finish the gather, not resume "
                    "on the assumption that the hold is loaded.")
            else:
                try:
                    from brain import mission_progress
                    mission_progress.advance("bartering")
                except Exception as exc:
                    logger.debug(f"[mission_runner] could not record the departure: {exc}")
        logger.info(f"[mission_runner] next leg: {leg.id} ({leg.kind})")
        return True

    def _here(self, state) -> Optional[tuple]:
        port = getattr(state, "port", None)
        return self.coords.get(port) if port else None

    def _distance(self, here, location) -> float:
        there = self.coords.get(location)
        if here is None or there is None:
            return 0.0                          # unknown is not far, it is unranked
        return ((here[0] - there[0]) ** 2 + (here[1] - there[1]) ** 2) ** 0.5

    # ── a leg, as work orders ────────────────────────────────────────────────

    def _runner_for(self, leg):
        """The steps this leg is made of, or None if nothing here knows the kind.

        Saying so beats inventing a work order for a leg nobody designed.
        """
        p = leg.params
        if leg.kind == "gather":
            # THE SHELF IS THE AUTHORITY, NOT THE PLAN. Once the fleet is standing in a
            # market, which port the KB thinks sources a material stops mattering — the
            # goods are right there on the page (user, 2026-08-29). So the leg carries the
            # WHOLE outstanding list and buys whatever of it this market has.
            #
            # `assign_purchases` pins each material to the FIRST port of the planned route
            # that sources it, which was sound while `run_mission` walked that route in
            # order. This runner re-decides the next leg from where the fleet actually is,
            # so the moment the itinerary diverges the assignment describes a journey that
            # never happened — a stored conclusion (Guiding Principle #2).
            #
            # Live 2026-08-29: Iron was pinned to Amsterdam because Amsterdam led the route.
            # A misread port sent the fleet to Tripoli first, and when it reached Barcelona —
            # a known Iron source, with Iron on the shelf — it bought only the Matchlock Gun
            # it had been assigned and left the Iron behind.
            return [_VoyageStep(p["port"]),
                    _OneGoalLeg(Hold(orders=self._everything_still_wanted()))]
        if leg.kind == "sail_to_village":
            return [_VoyageStep(p["village"], kind="village",
                                min_supply_days=self.min_supply_days),
                    _OneGoalLeg(Barter(good=self.good, village=p["village"]))]
        if leg.kind == "barter":
            return [_OneGoalLeg(Barter(good=p.get("good", self.good), village=self.village))]
        if leg.kind == "sail_route":
            return [_VoyageStep(p["route"], kind="route"),
                    _OneGoalLeg(SellHold(exclude=tuple(self.keep)))]
        if leg.kind == "sail_to_sell":
            return [_VoyageStep(p["sell_port"]),
                    _OneGoalLeg(SellHold(exclude=tuple(self.keep)))]
        if leg.kind == "sell":
            return [_OneGoalLeg(SellHold(exclude=tuple(self.keep)))]
        if leg.kind == "supply_verify":
            # "Can this leg be supplied at all? A port can; being adrift or in a building
            # cannot." That is what a successful hold read establishes — the fleet panel
            # opens at a port and at sea, and the dispatcher routes out of a building to
            # reach it. The days aboard ride along on the same reading, and the village leg
            # weighs them against its own floor.
            return [_OneGoalLeg(ReadHold())]

        if leg.kind == "sell_surplus":
            keep_qty = p.get("keep_qty") or {}
            if p.get("clear"):
                return [_OneGoalLeg(FreeHold(keep=tuple(self.keep)))]
            return [_OneGoalLeg(TrimHold(keep_qty=dict(keep_qty)))]
        return None
