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
        # AN UNREAD NAME NEVER STOPS A VOYAGE — only a CONFIDENT reading of somewhere else.
        #
        # The rule (user, 2026-09-07): "Unable to read port name should not stop things except
        # on the world map. No other activities really depend on port name... If the bot does
        # not know if it has arrived at a port it needs to buy stuff, should still go to the
        # market to check." That is already how this behaves — an `arrived_at` of None returns
        # above — and the misreading that stranded the Faro leg no longer reaches here at all:
        # `read_port_name` rejects prose, and the title picker returns `Faro` rather than the
        # 'north?' it had taken from an NPC bubble.
        #
        # WHAT SURVIVES IS NARROWER AND STILL WORTH KEEPING. A name that IS read and IS a
        # different port is not a bad read: live 2026-09-01 it was Tripoli, the port the fleet
        # had never left, reported as the arrival for a Barcelona leg — a voyage that never
        # happened, checking itself off. Absence of evidence lets the mission go and look;
        # evidence of the wrong place does not.
        #
        # Where the name genuinely DECIDES something is the world map, and that is guarded
        # there — `world_map._on_location_info` refuses to commit a panel it cannot confirm.
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
    # What the last gather actually READ off the sell grid, per material. An OBSERVATION,
    # kept because it is what tells us how many rounds the materials can fund — and a
    # material missing from it is UNREAD, never zero.
    _materials_aboard: dict = field(default_factory=dict)
    # The per-material breakdown the last gather reported — what is short, and how much was
    # wanted. Read by the reroute, which needs to know WHICH material a port failed to supply.
    _last_materials: dict = field(default_factory=dict)
    # Material -> the port that reported it scarce this season. The market says so outright;
    # the reroute acts on it without having to infer a gap from a breakdown it may not have.
    _cannot_supply: dict = field(default_factory=dict)

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
            # A PORT SAYING WHAT IT CANNOT SUPPLY, in its own words. The market reports
            # `season: low, good: Raisin` when it finds a scarce shelf, and that is a direct
            # statement — stronger than inferring a gap from the have/want breakdown, which
            # is exactly what was missing at Madeira when the ledger had just been cleared.
            if _obs.get("season") == "low" and _obs.get("good"):
                port = str(_obs.get("port") or getattr(self._leg, "location", "") or "")
                self._cannot_supply[str(_obs["good"]).lower()] = port

        for _ in range(len(self.subtasks) * 4 + 8):     # a leg cannot need more turns
            if self._runner is None:
                if not self._start_next_leg(state):
                    return None
                result = None                            # a fresh step gets a fresh start

            goal = self._runner.next_goal(result, state)
            if goal is not None:
                return goal

            if self._runner.status != DONE:              # the step gave up
                # ...WHICH IS NOT ALWAYS THE END OF THE MISSION. `_finish_leg` STEPS OVER a
                # refused trim — trimming is support, not a leg — and leaves the mission
                # RUNNING. Returning None here anyway threw that decision away: the runner
                # said "carry on without it" and then reported it had nothing to ask for.
                #
                # Live 2026-09-06 at Tripoli, with every material aboard and the barter one
                # sail away:
                #
                #     sell_surplus refused (trim to Candle 709, Iron 822, Matchlock Gun 411)
                #       — trimming is support, so the mission carries on without it
                #     MissionRunner has nothing more to ask for after 37 step(s)
                #       — status running
                #
                # `status running` with no work order is the shape of this bug: a FAILED
                # mission is meant to stop, a RUNNING one is meant to be asked again. So ask
                # the same question the DONE path below already asks.
                self._finish_leg()
                if self.status != RUNNING:
                    return None
                result = None
                continue
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
        return self._capped_to_fundable_rounds(wanted)

    def _capped_to_fundable_rounds(self, wanted: dict) -> dict:
        """Never want more of a material than the rounds we can actually run will consume.

        A ROUND CONSUMES ALL ITS MATERIALS, so the barter is capped by the SCARCEST one, and
        every unit of the others bought past that cap is dead weight — money, gems, hold
        space, and the refreshes that fetched it.

        Live 2026-09-06, the Hutu run: Pig 1,828 bought and 1,099 used; Raisin 1,100 bought
        and 1,099 used. Raisin capped the barter at 6 rounds, so 729 Pig — 40% of what was
        bought — was carried to the village and back unused. The plan had bought each
        material to its own padded 1,755 as though they were independent.

        WHAT THIS DOES NOT FIX, stated because the numbers above are exactly the case it
        misses: Pig was gathered FIRST, before Raisin's shortfall could be known, so no cap
        computed here could have seen it coming. This guard bites when the scarce material is
        gathered first, or when a later leg would top up a material already past the cap.
        Knowing in ADVANCE that Madeira's Raisin is thin is the season flag's job
        (`docs/low_stock_as_a_planning_input.md`), and the two are meant to compound.

        Silent when the recipe is unknown: a cap computed from a guess is worse than none.
        """
        recipe = self._per_round_needs()
        if not recipe or not wanted:
            return wanted
        # THE BOTTLENECK RULE RUNS FIRST, and that ordering is the whole of it: the cap below
        # needs a FINISHED material and returns early without one — which is the very case
        # this is for, two materials both still being gathered. Placed after, it was dead code
        # in the situation it exists to fix.
        capped = self._not_ahead_of_the_bottleneck(dict(wanted), recipe)
        rounds = self._rounds_fundable(recipe, still_shopping=set(wanted))
        if rounds is None:
            return capped
        for material, per_round in recipe.items():
            if per_round <= 0:
                continue
            key = next((k for k in capped if k.lower() == material.lower()), None)
            if key is None:
                continue
            cap = rounds * per_round
            if cap < capped[key]:
                logger.info(f"[mission_runner] {key}: wanting {cap} rather than {capped[key]} "
                            f"— the scarcest material funds {rounds} round(s), and a round "
                            "consumes them all")
                capped[key] = cap
        return capped

    def _not_ahead_of_the_bottleneck(self, wanted: dict, recipe: dict) -> dict:
        """Stop topping up a material that is already further ahead than the bottleneck.

        THE HOLD IS THE THING THIS PROTECTS. Rounds are limited by the SCARCEST material, so
        buying more of a plentiful one buys no rounds at all — it buys cargo space away from
        the material that would.

        Live 2026-09-07 at Faro. Pig read 1,505 against a padded 1,755 and so read SHORT, and
        the buy round did what it was told: it kept buying. Raisin stood at 881 — three rounds
        — which needs 654 Pig, so the hold already carried more than twice what any round
        could use. The ship finished at 4,952/4,952, FULL OF PIG, with no room left for the
        Raisin that actually gates the barter.

        The padded target was not wrong, it was answering the wrong question: "how much would
        seven rounds take?" rather than "how much can we currently use?".

        NOT THE SAME AS CAPPING ON WHAT IS ABOARD, which is self-fulfilling and was caught by
        a test earlier: the BOTTLENECK itself is never capped, so it always keeps buying. Only
        materials that are AHEAD of it stop, and they resume the moment it catches up.
        """
        rounds = {}
        for material, per_round in recipe.items():
            have = self._materials_aboard.get(str(material).lower())
            if have is None:
                return wanted                 # unread — no honest bottleneck to find
            rounds[str(material).lower()] = have // per_round
        if len(rounds) < 2:
            return wanted
        floor = min(rounds.values())
        for material in list(wanted):
            key = str(material).lower()
            if key not in rounds or rounds[key] <= floor:
                continue                      # the bottleneck, or level with it — keep buying
            have = self._materials_aboard.get(key, 0)
            if have < wanted[material]:
                logger.info(f"[mission_runner] {material}: {have} is already "
                            f"{rounds[key]} round(s) against the bottleneck's {floor} — not "
                            "topping it up while that is short; the hold is needed for the "
                            "material that is behind")
                wanted[material] = have
        return wanted

    def _per_round_needs(self) -> dict:
        """The pinned recipe's per-round materials, or {} when it is not known."""
        try:
            from brain import mission_progress
            cur = mission_progress.current() or {}
            return {str(m): int(q) for m, q in (cur.get("recipe") or {}).items() if int(q) > 0}
        except Exception as exc:              # noqa: BLE001 — no cap is better than a wrong one
            logger.debug(f"[mission_runner] could not read the recipe: {exc}")
            return {}

    def _rounds_fundable(self, recipe: dict, *, still_shopping: set) -> Optional[int]:
        """How many rounds the FINISHED materials can run, or None while that is unknown.

        ONLY A MATERIAL WE HAVE STOPPED BUYING IS A CONSTRAINT. One still on the shopping
        list can still grow, and capping on it makes the cap self-fulfilling — the want falls
        to what is already aboard, so no more can ever be bought. (Caught by
        `test_a_material_still_UNDER_the_cap_is_untouched`: Pig at 200 mid-gather would have
        capped the whole mission to one round.)

        A material we have not READ yet makes the answer unknown, not zero — the same rule
        the ledger follows, and for the same reason: an unread amount is not an absent one.
        """
        held = self._materials_aboard
        if not held:
            return None
        shopping = {str(m).lower() for m in still_shopping}
        rounds = None
        for material, per_round in recipe.items():
            key = str(material).lower()
            if key in shopping:
                continue                      # still being gathered — not a constraint yet
            have = held.get(key)
            if have is None:
                return None                   # unread — no honest cap to compute
            rounds = have // per_round if rounds is None else min(rounds, have // per_round)
        return rounds

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
        # AN ABSENT BREAKDOWN IS NOT AN EMPTY ONE. This assigned unconditionally, so any
        # result without a `materials` key wiped what the last good reading said — and the
        # reroute below is driven entirely by this field.
        #
        # Live 2026-09-07 at Madeira, the exact sequence that cost the mission a barter round:
        #
        #     15:38:41  the shelf could not be read across that purchase — re-reading the hold
        #     15:38:41  market -> finished {'sold': [], 'port': 'Madeira',
        #                                   'stopped_because': "'Raisin' is scarce here..."}
        #
        # That re-read clears the ledger on purpose, and `_observed` gates `materials` on the
        # ledger — so the result that ENDS a scarce gather is the one most likely to carry no
        # breakdown, because an unreadable shelf is the same condition that makes a port
        # scarce. The reroute then saw nothing short and returned silently, and the fleet
        # sailed with Raisin 1,211 of 1,712.
        #
        # Same rule as `_sell_page` returning None rather than []: "I could not look" and
        # "I looked and there is nothing" are different answers, and only one of them is news.
        if materials:
            self._last_materials = dict(materials)
        for material, st in (materials or {}).items():
            have = (st or {}).get("have")
            if have is not None:
                self._materials_aboard[str(material).lower()] = int(have)
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
            # THE BARTER IS THE PHASE BOUNDARY. Past it the goods are aboard and nothing
            # upstream may be revisited — which is exactly what the next run needs to know.
            if leg.kind == "barter":
                self._record_progress("advance", "sailing_route")
            if leg.kind == "gather":
                self._reroute_what_this_port_cannot_supply(leg, runner)
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

    def _reroute_what_this_port_cannot_supply(self, leg, runner) -> None:
        """A material this port cannot supply gets another port — now, not next run.

        STOPPING THE WASTE IS ONLY HALF OF IT. The buy round already refuses to grind a
        scarce shelf: "'Raisin' is scarce here this season — 2 refresh(es) is all this port is
        worth". But the mission then carried on regardless, and live 2026-09-07 it set off for
        San Village with ONE Raisin against 248 a round — zero barter rounds, a voyage spent
        to arrive unable to trade. The user asked for the other half: "it records the data and
        replans immediately".

        Raisin has three sources — Bordeaux, Madeira, Trabzon — and only Madeira was flagged.
        There was somewhere to go.

        A PORT IS ONLY TRIED ONCE, and a port already known scarce for that material is not
        tried at all; when neither leaves a candidate the mission carries on short, exactly as
        before, because a leg that cannot help is worse than no leg.

        `brain/mission.py::recover` does this for the OLD `run_mission` path, which the live
        mission stopped using — it is imported and unreachable. This is the same idea where
        the mission actually runs.
        """
        # Nothing reported yet is a real state — a gather that never reached a market has no
        # breakdown to reroute from.
        reported = getattr(self, "_last_materials", None) or {}
        short = [m for m, st in reported.items() if (st or {}).get("state") == "short"]
        # A PORT THAT SAID SO OUTRANKS AN INFERENCE. `_cannot_supply` holds what the market
        # itself reported scarce — `season: low, good: Raisin` — and that needs no breakdown
        # to be true. At Madeira the breakdown was the one thing missing, because the shelf
        # that could not be read is what made the port scarce in the first place.
        # MATCHED BY NAME, NOT BY SPELLING. `_cannot_supply` is keyed lower-case and
        # `_last_materials` however the market reported it, so `"raisin" not in ["Raisin"]`
        # was true and the same material was rerouted twice. Live 2026-09-07 at Bordeaux:
        #
        #     [mission_runner] Raisin: no other source to try — carrying on short
        #     [mission_runner] raisin: no other source to try — carrying on short
        #
        # It cost nothing there because both attempts reached the same answer, but with a
        # candidate available it would have added two identical gather legs.
        seen = {str(m).lower() for m in short}
        for material, where in (getattr(self, "_cannot_supply", None) or {}).items():
            if (str(where).lower() == str(leg.location or "").lower()
                    and str(material).lower() not in seen):
                short.append(material)
                seen.add(str(material).lower())
        if not short:
            return
        tried = {str((getattr(t, "params", None) or {}).get("port") or t.location).lower()
                 for t in self.subtasks if t.kind == "gather"}
        # A MATERIAL A PENDING LEG ALREADY COVERS NEEDS NO NEW SOURCE. Short HERE is not
        # short everywhere: the plan may simply not have reached the port that sells it.
        #
        # Live 2026-09-07 at Madeira, this fired twice — rightly for Raisin, and wrongly for
        # Pig, which `gather:Faro` was on its way to buy. It added `gather:Gijon:Pig` for a
        # material that was one leg from being met. (The pre-sail settle would have closed it
        # again, so it cost nothing this time; that is luck, not design.)
        still_planned = {str(m).lower()
                         for t in self.subtasks if t.kind == "gather" and not t.done
                         for m in ((getattr(t, "params", None) or {}).get("orders") or {})}
        for material in short:
            if str(material).lower() in still_planned:
                logger.info(f"[mission_runner] {material} is short here, but a pending leg "
                            "already goes where it is sold — not rerouting it")
                continue
            want = (reported.get(material) or {}).get("want")
            alt = self._another_source(material, tried)
            if alt is None:
                logger.info(f"[mission_runner] {material}: no other source to try — "
                            "carrying on short")
                continue
            ident = f"gather:{alt}:{material}"
            logger.warning(f"[mission_runner] {material} is short and {leg.location} cannot "
                           f"supply it — adding {ident}")
            self.subtasks.append(type(leg)(id=ident, kind="gather", location=alt,
                                           params={"port": alt,
                                                   "orders": {material: int(want or 0)}}))
            # AND NOTHING DOWNSTREAM MAY START WITHOUT IT. Adding the leg is not the same as
            # ORDERING it: the tail is a dependency chain built at plan time —
            # `sell_surplus` deps on the ORIGINAL gather ids, `sail_to_village` on
            # `supply_verify` — so a leg added later hangs outside it and the runner is free
            # to pick anything else that is runnable.
            #
            # Live 2026-09-07 it did exactly that: `adding gather:Bordeaux:Raisin`, then
            # sell_surplus, supply_verify, and `next leg: sail_to_village` — sailing for San
            # with 331 Raisin against 248 a round. Past that departure there is no more
            # gathering at all (`_departed_for_the_village`), so the leg would never have run.
            self._make_everything_downstream_wait_for(ident)
            tried.add(alt.lower())

    def _make_everything_downstream_wait_for(self, ident: str) -> None:
        """Every pending leg that is not itself a gather now depends on `ident`.

        Blunt on purpose. The tail is ordered among itself already, so adding one more
        predecessor to each pending member cannot reorder it — it only stops the whole tail
        from starting before the material is aboard. Gathers are left alone because they are
        mutually unordered by design and ranked by cost.
        """
        for other in self.subtasks:
            if other.done or other.kind == "gather" or other.id == ident:
                continue
            if ident not in (other.deps or ()):
                other.deps = tuple(other.deps or ()) + (ident,)

    def _another_source(self, material: str, tried: set) -> Optional[str]:
        """A PORT that sells `material`, untried and not known scarce there.

        PORTS ONLY. A village is a real source — some materials are sold at both, some at
        only one — but reaching one is a barter on a different world-map tab, not a market
        visit, and this reroute builds a `gather` leg that buys. `source_villages` is
        recorded beside the ports for the day that leg exists; sending a buyer to a village
        would fail at the shelf instead of failing here.

        Live 2026-09-08: `gather:Chinook:Matchlock Gun`. 'Chinook' was half of 'Chinook
        Village', which was never a source at all — it was a row of the world map's village
        list showing behind the Source dialog, read because the reader used a fixed window
        instead of the dialog's own box. The fleet searched the PORT list for it twenty
        times, typed it, scrolled it, and rightly refused. Seville, two entries further
        down and a real port that sells the gun, was never reached.
        """
        try:
            from memory.barter_kb import load_recipe
            from memory.market_kb import season_of
            from memory.places import resolve_source_port
            recipe = load_recipe(self.good)
            for inp in (getattr(recipe, "inputs", None) or []):
                if str(inp.material).lower() != str(material).lower():
                    continue
                for port in (inp.source_ports or []):
                    if str(port).lower() in tried:
                        continue
                    # A NAME THAT IS NOT A PORT IS NOT A DESTINATION. The catalogue is the
                    # same one the reader checks against; a stale entry cannot become a
                    # course again just because it is sitting in the KB.
                    if resolve_source_port(port) is None:
                        logger.info(f"[mission_runner] {port!r} is not a port — not routing "
                                    f"{material} there")
                        continue
                    if season_of(port, material) == "low":
                        logger.info(f"[mission_runner] skipping {port} for {material} — "
                                    "recorded scarce there this season")
                        continue
                    return port
        except Exception as exc:              # noqa: BLE001 — no alternative is not a crash
            logger.debug(f"[mission_runner] could not look for another source: {exc}")
        return None

    def _record_progress(self, what: str, phase: str = "") -> None:
        """Tell `mission_progress` how far this mission has got. Never fails the mission.

        THE DISPATCHER PATH RECORDED ONLY ITS START, and that was enough to send a fleet back
        across the map. `mission_runner` advanced the phase to "bartering" when the sail to
        the village began and then never touched it again — no `sailing_route`, no `finish`
        — so a mission that ran to completion still LOOKED in-flight to the next launch:

            [barter_command] already bartering for Svear Village (3705s ago) — skipping the
                             check and the plan, arriving and bartering with what is aboard
            [barter_command] not at a village (state is 'sub_menu:sell') — sailing to Svear

        Live 2026-09-06: the fleet was standing in Lisboa's market with 3,668 Birch Tree
        aboard, its barter six rounds finished an hour earlier, and it set sail for Svear
        Village to barter again. The record is only stale for six hours, so this cannot be
        left to expire — it is the window in which a relaunch is most likely.
        """
        try:
            from brain import mission_progress
            if what == "finish":
                mission_progress.finish()
            else:
                mission_progress.advance(phase)
        except Exception as exc:              # noqa: BLE001 — bookkeeping, not the mission
            logger.debug(f"[mission_runner] could not record progress: {exc}")

    def _settle_gathers_already_aboard(self) -> None:
        """Close a gather leg whose materials are ALREADY ABOARD — before the sail, not after.

        `_settle_gathers` runs on ARRIVAL, from the market's own per-material read, so a leg
        that needs nothing is only discovered once the voyage has been spent. Live
        2026-09-06: the fleet finished at Tripoli, sailed to Barcelona, read the hold, found
        Iron and Matchlock Gun both already aboard —

            gather:Barcelona settled — Iron, Matchlock Gun already aboard, bought elsewhere

        — and sailed BACK to Tripoli for the Candle. Everything needed to skip it was in the
        ledger before the fleet left.

        This is the predicate half of `docs/the_plan_is_a_checklist.md`: "an item is done when
        the WORLD says so". The world had already said so.

        AN UNREAD MATERIAL NEVER CLOSES A LEG. `_materials_aboard` holds what was actually
        read off a sell grid; a material missing from it is unknown, not absent, and skipping
        a voyage on an unknown would strand the material — the same rule `_settle_gathers`
        follows for exactly the same reason.
        """
        held = self._materials_aboard
        if not held:
            return
        wanted = self._everything_still_wanted()
        for leg in self.subtasks:
            if leg.done or leg.kind != "gather":
                continue
            orders = (getattr(leg, "params", None) or {}).get("orders") or {}
            if not orders:
                continue
            covered = []
            for material in orders:
                have = held.get(str(material).lower())
                want = wanted.get(material)
                if have is None or want is None or have < want:
                    covered = None
                    break
                covered.append(material)
            if covered:
                leg.done = True
                self.completed.append(leg.id)
                logger.info(f"[mission_runner] {leg.id} needs nothing — "
                            f"{', '.join(covered)} already aboard, so no voyage is spent")

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
            # THE MISSION IS OVER, SO SAY SO WHERE THE NEXT RUN WILL LOOK. `mission_progress`
            # survives the process; a mission left recorded as in-flight is what the next
            # launch RESUMES, and resuming skips planning entirely.
            self._record_progress("finish")
            logger.info("[mission_runner] every leg is done")
            return False

        self._settle_gathers_already_aboard()
        pending = [t for t in self.subtasks if not t.done]
        if not pending:
            self.status = DONE
            self._record_progress("finish")
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
            # CLEARING AND TRIMMING ARE TWO JOBS, and `trim_before_gather` asks for both:
            # `params={"good": ..., "keep_qty": ..., "clear": True}`. This branched on
            # `clear` and returned, dropping `keep_qty` on the floor — so the node its own
            # comment describes as "clear the non-materials AND trim the materials to plan
            # before buying anything" only ever did the first half.
            #
            # They are not interchangeable. `FreeHold` protects everything in `self.keep`,
            # which is exactly the barter's materials — so a clear cannot touch a MATERIAL
            # surplus by construction. Live 2026-09-07 the hold reached San Village carrying
            # 2,876 Pig against a plan of 1,505; `trim_before_gather` had run, sold nothing,
            # and reported done. 1,812 of that Pig came home unused.
            #
            # Clear first: it disposes of whole goods and shortens the grid the trim then
            # has to read.
            keep_qty = p.get("keep_qty") or {}
            steps = []
            if p.get("clear"):
                steps.append(_OneGoalLeg(FreeHold(keep=tuple(self.keep))))
            if keep_qty:
                steps.append(_OneGoalLeg(TrimHold(keep_qty=dict(keep_qty))))
            return steps or None
        return None
