"""The `barter` command — one typed line drives a whole barter mission.

    barter Box of Nutmeg at Melanesian Village, then take the route jakarta to london
    barter Camas at Apache Village, and sail to Edinburgh
    barter Pulque at Apache Village

Grammar:  barter <good> at <village>[, then take the route <name> | and sail to <port>]
(`then`/`and` are interchangeable and the comma is optional.)

Flow (docs/barter_command_flow.md):

    parse → CHECK the village REMOTELY from port → PLAN (capacity + supply aware)
          → GATHER → supply-verify → sail → BARTER → route/sail tail → SELL

The CHECK runs BEFORE the graph is built, not as a node inside it: every downstream
node is parameterised by what it returns (how many rounds are left today, this 6-hour
window's material quantities, which ports sell them), so it cannot be scheduled
alongside the work it configures.  It is also what makes an unknown village work with
no manual KB entry — the check writes the invariants back on first read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

from loguru import logger

from brain import mission_progress
from brain.activities.bootstrap import establish_position

# barter <good> at <village> [ [,] (then|and) (take the route <name> | sail to <port>) ]
_COMMAND_RE = re.compile(
    r"^\s*barter\s+(?P<good>.+?)\s+at\s+(?P<village>.+?)"
    r"(?:\s*,?\s*(?:then|and)\s+(?:"
    r"take\s+the\s+route\s+(?P<route>.+?)"
    r"|sail(?:\s+(?:back\s+)?to)\s+(?P<port>.+?)"
    r"))?\s*[.!]?\s*$",
    re.IGNORECASE,
)


@dataclass
class BarterCommand:
    good: str
    village: str
    tail_kind: str = "none"                 # 'route' | 'sail' | 'none'
    tail_value: Optional[str] = None

    def describe(self) -> str:
        tail = {"route": f", then take the route {self.tail_value}",
                "sail": f", and sail to {self.tail_value}"}.get(self.tail_kind, "")
        return f"barter {self.good} at {self.village}{tail}"


def parse_barter_command(text: str) -> Optional[BarterCommand]:
    """Parse a barter command line, or None if it isn't one.

    Names are taken verbatim (minus surrounding punctuation) — village and port
    matching is the navigator's job, and it is already fuzzy."""
    m = _COMMAND_RE.match(text or "")
    if not m:
        return None
    good = _clean(m.group("good"))
    village = _clean(m.group("village"))
    if not good or not village:
        return None
    if m.group("route"):
        return BarterCommand(good, village, "route", _clean(m.group("route")))
    if m.group("port"):
        return BarterCommand(good, village, "sail", _clean(m.group("port")))
    return BarterCommand(good, village)


def _clean(s: Optional[str]) -> str:
    return " ".join((s or "").strip(" \t,.;:").split())


# ── Live driver ───────────────────────────────────────────────────────────────

def _last_known_hold(max_age_s: float = 3600.0):
    """The hold as last SEEN at a market, if recent enough — else None.

    The hold can only be read at a market, but it does not stop being true once the fleet
    sails. Live 2026-08-22 a mission carrying three rounds' materials planned zero rounds
    because the clear step reported "not at a port — cannot sell surplus here" and the
    planner then priced every round as if the hold were empty.

    Bounded by age and dropped outright whenever the bot buys, sells or barters, because a
    hold the bot has since changed is worse than no hold at all.
    """
    from memory.observed_facts import recall
    seen = recall("hold", max_age_s=max_age_s)
    if seen is None:
        return None
    hold, age = seen
    logger.info(f"[barter_command] hold not readable here — using the last sighting "
                f"{hold} from {age:.0f}s ago")
    return hold


def _complete_materials_from_kb(trade, good: str, village: str = "") -> None:
    """Add materials the KB knows and this read did not return, at their last-known ratio.

    Mutates `trade`. Loud on purpose: a filled ratio is a guess where every other number came
    off the screen, and the log is the only place that distinction survives.
    """
    try:
        from memory.barter_kb import load_recipe
        known = load_recipe(good)
    except Exception as exc:
        logger.debug(f"[barter_command] could not load the KB recipe for {good!r}: {exc}")
        return
    if not known or not getattr(known, "inputs", None):
        return

    have = {(m or "").strip().lower() for m in (getattr(trade, "materials", {}) or {})}
    for inp in known.inputs:
        name = (inp.material or "").strip()
        if not name or name.lower() in have:
            continue
        trade.materials[name] = int(inp.ratio)
        logger.warning(
            f"[barter_command] {good!r} at {village or 'this village'}: the read did not "
            f"return {name!r}, which the KB knows is required. Planning with its last-known "
            f"ratio {inp.ratio} — A GUESS, where every other quantity was read from the "
            "screen. Without it the fleet would arrive unable to barter at all.")


def _recipe_is_partial(trade, good: str) -> bool:
    """True when the cached recipe names fewer materials than the KB knows for `good`.

    Materials are invariant, so fewer can only mean the read that produced the cache scrolled
    short of the whole list. Compared by NAME — the quantities are volatile and re-roll every
    few hours, but which materials a recipe takes does not.
    """
    try:
        from memory.barter_kb import load_recipe
        known = load_recipe(good)
    except Exception as exc:
        logger.debug(f"[barter_command] could not load the KB recipe for {good!r}: {exc}")
        return False
    if not known or not getattr(known, "inputs", None):
        return False
    known_names = {(i.material or "").strip().lower() for i in known.inputs}
    cached_names = {(m or "").strip().lower() for m in (getattr(trade, "materials", {}) or {})}
    missing = known_names - cached_names
    if missing:
        logger.warning(f"[barter_command] cached recipe for {good!r} is missing "
                       f"{sorted(missing)} — the KB knows {sorted(known_names)}")
    return bool(missing)


def _cached_trade(prog: Optional[dict]):
    """The recipe recorded when this task started, or None if there is none.

    Re-reading it every run is what dragged the fleet back to the world map. The snapshot
    can go stale — the window re-rolls roughly every six hours — and that is accepted: a
    stale ratio yields less output, while a re-check risks the position we already hold.
    """
    recipe = (prog or {}).get("recipe")
    if not isinstance(recipe, dict) or not recipe.get("materials"):
        return None
    return SimpleNamespace(good=recipe.get("good"), obtain=int(recipe.get("obtain") or 0),
                           materials=dict(recipe["materials"]))


def _depart_village_to_sea(max_backs: int = 4) -> bool:
    """Back out of a village until the world map can be opened. True when it can.

    A village is a PLACE, so leaving it is never done to satisfy a state test — but the tail
    genuinely requires it, and that is the task's call to make (see
    docs/one_loop_task_drives_state.md, "the state machine owns the HOW, the task owns the
    WHETHER"). This is still the task making it; what changed is that the decision now
    travels as a WORK ORDER instead of running its own loop.

    It was a `for attempt in range(max_backs)` that captured a screen, ran `where_am_i`,
    decided, and pressed — the shape Guiding Principle #5 removes. Every part has an owner:
    the perceiving is the dispatcher's and arrives as `state`; the pressing is one Back per
    tick from `to_intent`; and the screens that end by FINISHING rather than by Back (the
    idle lock, `loading`) are the clearing activities' business, which is where they belonged
    all along — `finish_current_activity()` was firing on ordinary villages and skipping the
    Back it was standing in for, which is why
    `test_it_backs_out_until_it_reaches_the_sea` counted one press where two were due.
    """
    from brain.barter_runner import HAVE_CLEAR, NEED_CLEAR, BarterTaskRunner
    from brain.run_goal import run_task

    runner = run_task(BarterTaskRunner(village="", good="", status=NEED_CLEAR),
                      max_ticks=max_backs * 2)
    if runner.status != HAVE_CLEAR:
        logger.warning(f"[barter_command] could not reach the sea from the village — "
                       f"{runner.reason or 'the tail cannot open the world map from here'}")
        return False
    return True



def _resume_tail(cmd, prog: dict) -> dict:
    """Run the TAIL phase: take the route (or sail to the sell port), then sell.

    Reached when the barter already succeeded. Re-running the barter here would spend
    another of the day's limited rounds on materials that are no longer aboard, so the
    phase record is what stops it.
    """
    from brain.barter_mission_live import make_live_executors
    from brain.mission import SubTask

    ex = make_live_executors()

    # LEAVE THE VILLAGE FIRST. The tail needs the world map, and a village has no world-map
    # control — the fleet must be at sea before the Route tab can be reached (user,
    # 2026-08-23). `open_world_map` now refuses from a village rather than tapping the
    # calibrated port globe into whatever sits at that coordinate, so without a deliberate
    # departure the tail simply cannot start.
    _depart_village_to_sea()

    if cmd.tail_kind == "route":
        leg = ex["sail_route"](SubTask(id="sail_route", kind="sail_route",
                                       location=cmd.tail_value or "",
                                       params={"route": cmd.tail_value}))
    elif cmd.tail_kind == "sail":
        leg = ex["sail_to_sell"](SubTask(id="sail_to_sell", kind="sail_to_sell",
                                         location=cmd.tail_value or "",
                                         params={"sell_port": cmd.tail_value}))
    else:
        leg = {"ok": True, "reason": "no tail requested"}
    if not leg.get("ok"):
        return {"ok": False, "step": "tail", "command": cmd,
                "reason": leg.get("reason", "could not run the tail leg")}

    res = ex["sell"](SubTask(id="sell", kind="sell", location=cmd.tail_value or "",
                             params={"sell_port": cmd.tail_value, "good": cmd.good}))
    if res.get("ok"):
        mission_progress.finish()
    return {"ok": bool(res.get("ok")), "step": "mission", "command": cmd, "sell": res,
            "reason": res.get("reason", "")}


def _try_barter_here(cmd, position: dict = None) -> Optional[dict]:
    """Already at the village? Then read the ratio off the PANEL and barter. No world map.

    ENTRY BY PERCEPTION, not by rote. A task is a set of steps with preconditions, not a
    fixed sequence to replay from the top: the bot perceives, works out which steps are
    already satisfied, and joins at the first one that is not (user, 2026-08-23).

    Here that matters concretely. A recipe's MATERIALS are fixed knowledge — Box of Nutmeg at
    Melanesian Village always takes Ebony, Coral and Textiles — and the only volatile part is
    the RATIO, which the barter panel shows directly. Standing in the village, the bot can tap
    Barter, tap the good, and read it. The remote check exists to read a village the fleet has
    NOT sailed to; using it here means opening the world map, and a village has no world-map
    control — so the fleet must leave the very place it needs to be. Live 2026-08-23:

        [open_world_map] the fleet is AT 'village' — cannot open the world map from here
        FAILED at step check: could not open the world map

    Returns a mission result when the barter was done here, or None to fall through to the
    ordinary check-and-plan path.
    """
    from brain.barter_runner import HAVE_PANEL, NEED_PANEL, BarterTaskRunner
    from brain.run_goal import run_task

    at_village, why = _at_a_village(position)
    if not at_village:
        logger.info(f"[barter_command] not at the village ({why}) — planning the trip")
        return None

    logger.info(f"[barter_command] at the village ({why}) — reading the ratio from the "
                "panel instead of opening the world map")
    # ASKED FOR, NOT DONE HERE (Guiding Principle #7). This opened the panel, selected the
    # good and read it back — three UI steps in the task runner. They were invisible to the
    # layering guard because they arrived through `barter_mission_live`, a task module that
    # imported thirteen UI modules on this one's behalf; one hop was enough to hide them.
    probe = run_task(BarterTaskRunner(village=cmd.village, good=cmd.good, status=NEED_PANEL),
                     max_ticks=12)
    if probe.status != HAVE_PANEL:
        logger.info(f"[barter_command] the panel did not answer here ({probe.reason}) — "
                    "falling back to the check-and-plan path")
        return None

    state = probe.panel
    if state is None or state.rounds_remaining < 1:
        short = getattr(state, "shortfall", None) if state else None
        logger.info(f"[barter_command] the hold funds no full round here "
                    f"(short {short}) — falling back to the check-and-plan path")
        return None

    logger.info(f"[barter_command] the panel funds {state.rounds_remaining} round(s) — "
                "skipping the check and the plan, bartering now")
    mission_progress.start(cmd.village, cmd.good, state.rounds_remaining)
    mission_progress.advance("gathering")
    mission_progress.advance("bartering")
    return _resume_at_village(cmd, {"rounds": state.rounds_remaining})


def _at_a_village(position: dict = None) -> tuple:
    """(are we at a village?, why). Includes the village's SUB-SCREENS, not just its landing.

    `state == "village"` alone is too narrow. Live 2026-08-23 a run began with the BARTER
    sub-menu still open from the previous run; the classifier's first verdict was 'building'
    (corrected afterwards to 'village'), the narrow test failed, and the fleet sailed away
    from the village it was standing in — for the second time that day.

    INTERPRETED, NOT RE-OBSERVED (Guiding Principle #7). This used to capture a screen, sweep
    for blockers, capture again, run `where_am_i`, and then run OmniParser a second time to
    read the left menu — five perception calls to answer a question about a screen that had
    already been perceived. The sweep is the dispatcher's `unblock` and the perceiving is the
    dispatcher's; what belongs here is only the reading of the evidence.

    THE EVIDENCE IS STILL PLURAL, which is the part that mattered. The left menu was decisive
    because it is INDEPENDENT of the classifier that got it wrong — and `sub_menu` and
    `scene_type` are two more independent reads, carried through from the same observation.
    Any one of them naming a village is enough; the classifier alone was what was too narrow.

    And the left-menu read is not lost by removing it from here: `brain/perceive.py` runs the
    SAME `{barter, gifting}` check inside `_classify_nav_state` now. This copy was written
    when the classifier did not, and it has been re-deriving a signal already folded into the
    verdict it was second-guessing.
    """
    # ESTABLISHED ONCE, NOT PER CALLER. `run_barter_command` has just bootstrapped — three
    # perceives and a full clearing pass — and this used to do the whole thing again seconds
    # later, on a screen nothing had touched. Live 2026-08-30 that was 9 captures and 84
    # seconds before the first tap, with "position established: 'village'" logged twice.
    #
    # A caller that already knows passes it; one that does not still asks.
    if position is None:
        from brain.activities.bootstrap import establish_position
        position = establish_position()
        if not position.get("ok"):
            return False, position.get("reason") or "position could not be established"

    state = position.get("state")
    sub_menu = (position.get("sub_menu") or "").lower()
    scene = (position.get("scene_type") or "").lower()

    if state in _VILLAGE_STATES:
        return True, f"state is {state!r}"
    if sub_menu in _VILLAGE_SUB_MENUS:
        return True, f"state is {state!r} but the {sub_menu!r} sub-menu is a village's"
    if scene == "village":
        return True, f"state is {state!r} but the scene reads as a village"
    return False, f"state is {state!r}, sub_menu={sub_menu or None}, scene={scene or None}"


# The states and sub-screens that ARE a village. Kept beside VillageActivity.SERVES, which
# is the same fact for the dispatcher; they are asserted equal in the tests so the two cannot
# drift apart the way two lists of where the market works once did.
_VILLAGE_STATES = ("village", "sub_menu:barter")
_VILLAGE_SUB_MENUS = ("barter", "gifting")


# Each pass must commit something or the loop stops; this only bounds a pathological
# case where the panel keeps claiming rounds that never commit.
_MAX_BARTER_PASSES = 4


def _resume_at_village(cmd, prog: dict, position: dict = None) -> dict:
    """Run the BARTER phase: get to the village, then barter. Nothing else.

    No village check and no cargo check — both questions were closed by the phases before
    this one. The materials are aboard or they are not, and nothing readable from the water
    or the village changes that now.

    Sails only when the fleet is NOT already at a village — `drive_sail_to` is emphatically
    not a no-op there (see the comment below).
    """
    from brain.barter_mission_live import make_live_executors
    from brain.mission import SubTask

    # OPT-IN: let the ONE loop drive this phase instead of the direct calls below. Flagged
    # because the loop is unproven against real screens and this mission finally works
    # end to end — see docs/one_loop_task_drives_state.md. UWO_TASK_LOOP=1 to try it.
    from brain import barter_task
    if barter_task.enabled():
        res = barter_task.run_barter_phase(cmd.village, cmd.good,
                                           int(prog.get("rounds") or 0))
        if res.get("ok"):
            mission_progress.finish()
        return {"ok": bool(res.get("ok")), "step": "mission", "command": cmd,
                "barter": res, "reason": res.get("reason", "")}

    ex = make_live_executors()

    # ALREADY THERE? Then do not sail. `drive_sail_to` is NOT a no-op at a village: its INIT
    # phase sees `state='village'` with `port=None` — a village's name cannot be read from
    # its interior — assumes it is in the wrong place, and presses Back to exit to sea.
    # Live 2026-08-23 that took the fleet OUT of Melanesian Village and set it searching the
    # world map for the village it had been standing in.
    #
    # Being in a village while the mission is in its BARTERING phase is the evidence that
    # matters: the phase is only reached by sailing here. This mirrors what the task-loop
    # path already does (brain/barter_task.BarterPhaseTask.next_step).
    at_village, why = _at_a_village(position)
    logger.info(f"[barter_command] perceived before deciding: at_village={at_village} ({why})")

    if not at_village:
        logger.info(f"[barter_command] not at a village ({why}) — sailing to {cmd.village}")
        sailed = ex["sail_to_village"](SubTask(id="sail_to_village", kind="sail_to_village",
                                               location=cmd.village,
                                               params={"village": cmd.village}))
        if not sailed.get("ok"):
            return {"ok": False, "step": "sail_to_village", "command": cmd,
                    "reason": sailed.get("reason", "could not reach the village")}
    else:
        logger.info(f"[barter_command] already at a village and the mission is bartering "
                    f"for {cmd.village} — not sailing anywhere")

    res = ex["barter"](SubTask(id="barter", kind="barter", location=cmd.village,
                               params={"rounds": int(prog.get("rounds") or 0),
                                       "good": cmd.good, "village": cmd.village}))
    if not res.get("ok"):
        return {"ok": False, "step": "mission", "command": cmd, "barter": res,
                "reason": res.get("reason", "")}

    # KEEP BARTERING WHILE THE MATERIALS LAST. One successful call is not the end of the
    # phase: the ratio moves with amity tiers and the stock refresh, so more rounds can be
    # fundable after a batch that the plan never anticipated. Bounded, because each pass must
    # make progress or stop.
    for _ in range(_MAX_BARTER_PASSES):
        if not res.get("more_rounds_fundable"):
            break
        logger.info(f"[barter_command] {res['more_rounds_fundable']} more round(s) fundable "
                    "— bartering again before the tail")
        again = ex["barter"](SubTask(id="barter", kind="barter", location=cmd.village,
                                     params={"rounds": int(res["more_rounds_fundable"]),
                                             "good": cmd.good, "village": cmd.village}))
        if not again.get("ok") or not again.get("committed"):
            logger.info("[barter_command] no further round committed — done bartering")
            break
        res = again

    # THE TAIL IS PART OF THE TASK. `barter <good> at <village>, then take the route <name>`
    # is one job, and the barter node has just advanced the phase to `sailing_route`.
    # Finishing here would drop the route and the sale on the floor — live 2026-08-23 the
    # resume path did exactly that.
    if cmd.tail_kind in ("route", "sail"):
        logger.info(f"[barter_command] bartered — now the tail: {cmd.tail_kind} "
                    f"{cmd.tail_value!r}")
        tail = _resume_tail(cmd, prog)
        return {**tail, "barter": res}

    mission_progress.finish()
    return {"ok": True, "step": "mission", "command": cmd, "barter": res,
            "reason": res.get("reason", "")}

def run_barter_command(text: str, *, cargo_capacity: Optional[int] = None,
                       cargo_used: Optional[int] = None, dry_run: bool = False,
                       cushion: Optional[float] = None,
                       clear_surplus: bool = False,
                       from_port: Optional[str] = None) -> dict:
    """Parse → remote check → plan → run the mission.  Returns a structured result;
    every early exit says exactly which step could not be completed.

    `cargo_capacity` / `cargo_used` override the live fleet read (useful when the main
    menu can't be read, or to plan against a hypothetical hold).  `cushion` overrides
    `AMITY_CUSHION`, the hedge against the check going stale under the mission (6-hour
    re-roll, or an amity tier crossing mid-barter); 0.0 plans on the snapshot exactly.
    `dry_run` stops after the plan — it still performs the remote check, because the plan
    has no meaning without this window's live quantities."""
    from brain.barter_quantity import (AMITY_CUSHION, free_space_for_barter,
                                       plan_barter_rounds)

    # ALREADY UNDER WAY? Then the gathering questions are settled and re-asking them is
    # what does the damage. Live 2026-08-22 a fleet standing IN Melanesian Village opened
    # the world map to run a REMOTE check on that same village, could not open the map from
    # a village, pressed Back three times, and ended up at sea — a voyage undone by a step
    # that had already been completed (user: "if it has started sailing to the village,
    # then no check for material anymore, just arrive and barter").
    cmd = parse_barter_command(text)
    if cmd is None:
        return {"ok": False, "step": "parse", "reason":
                f"could not parse {text!r} — expected: barter <good> at <village>"
                "[, then take the route <name> | and sail to <port>]"}
    logger.info(f"[barter_command] {cmd.describe()}")

    # WHERE ARE WE? — BEFORE ANY WORK ORDER IS ACCEPTED.
    #
    # A fresh run knows nothing: the phone may be locked, the daily news up, a perk banner
    # over the port name — and any of those makes sea and port indistinguishable. Taking the
    # order first means the preamble starts navigating on an unknown position, which is how
    # `open_world_map` came to be holding a lock screen on 2026-08-28: it waited for a
    # TransientActivity it reached zero times, then read "Season" out of an Investment Season
    # banner, called it a port name, and reported "Overworld confirmed".
    #
    # The dispatcher peels one layer per tick — lock, notice, unnameable chromed screen —
    # and stops at a state the task runner can be asked about. Nothing is guessed, and a
    # position that cannot be established is reported as unknown rather than invented.
    if not dry_run:
        here = establish_position()
        if not here.get("ok"):
            return {"ok": False, "step": "bootstrap", "command": cmd,
                    "reason": here.get("reason", "position could not be established")}
        logger.info(f"[barter_command] starting from {here.get('port') or here.get('state')}")

    prog = None if dry_run else mission_progress.current()
    if prog and (prog.get("village") or "").lower() != cmd.village.lower():
        prog = None                                   # a different village — not our mission

    if prog and mission_progress.at_least("sailing_route", village=cmd.village):
        logger.info(f"[barter_command] the barter is done for {cmd.village} "
                    f"({prog.get('age_s', 0):.0f}s ago) — resuming at the TAIL: take the "
                    "route and sell. The barter is not re-run.")
        return {**_resume_tail(cmd, prog), "resumed_from": prog.get("phase")}

    if prog and mission_progress.at_least("bartering", village=cmd.village):
        logger.info(f"[barter_command] already {prog.get('phase')} for {cmd.village} "
                    f"({prog.get('age_s', 0):.0f}s ago) — skipping the check and the plan, "
                    "arriving and bartering with what is aboard")
        return {**_resume_at_village(cmd, prog, position=None if dry_run else here),
                "resumed_from": prog.get("phase")}

    # ENTRY BY PERCEPTION: if the fleet is already standing in the village, the ratio is on
    # the panel in front of it and the whole check-and-plan preamble is moot.
    if not dry_run and prog is None:
        done_here = _try_barter_here(cmd, position=None if dry_run else here)
        if done_here is not None:
            return {**done_here, "entered_at": "barter"}

    # ── CHECK: ONCE, when the task starts. ────────────────────────────────────
    # Thereafter the recipe recorded then is the recipe we use, even if the window has
    # re-rolled underneath us (user 2026-08-22: "we just use the originally recipe, even if
    # the time has changed and refreshed, it is ok, it just means we may get less"). Getting
    # less is a cost; re-checking costs a trip to the world map every run, and it is that
    # trip which sailed the fleet out of the village it had already reached.
    check = None
    trade = _cached_trade(prog)
    rounds_remaining = (prog or {}).get("rounds_remaining")
    # A CACHED RECIPE MISSING A MATERIAL THE KB KNOWS IS A PARTIAL READ, NOT A RECIPE.
    #
    # Materials are INVARIANT — a recipe does not lose an ingredient between runs — so if the
    # cache names fewer than the KB does, what was cached is a trade-list read that scrolled
    # short. `write_back_invariants` already protects the KB from this ("keeping known
    # material 'Matchlock Gun' that this read did not return"); nothing protected the PLAN.
    #
    # Live 2026-08-26: the cached recipe held {Iron, Candle} while the KB held {Iron, Candle,
    # Matchlock Gun}, and every run that day planned from it. The fleet would have reached
    # Svear with two of three materials and been unable to barter at all — after two voyages
    # to fetch them (user: "if the plan missed matchlock we need to replan, otherwise the
    # barter will not work").
    if trade is not None and _recipe_is_partial(trade, cmd.good):
        logger.warning("[barter_command] the cached recipe is missing a material the KB "
                       "knows — discarding it and re-checking the village")
        mission_progress.finish()
        prog, trade, rounds_remaining = None, None, None

    if trade is not None:
        logger.info(f"[barter_command] reusing the recipe from the start of this task "
                    f"({prog.get('age_s', 0):.0f}s ago): {cmd.good} {trade.obtain} ← "
                    f"{trade.materials} — not re-checking {cmd.village}")
    else:
        # THE RECIPE IS ASKED FOR, NOT FETCHED (Guiding Principle #7).
        #
        # This was `read_village_barter_remote(...)` — a task module reaching past the
        # dispatcher to drive the world map itself, with its own `for i in range(max_scrolls)`
        # inside. Now the passive runner returns a `RemoteCheck` work order, `run_task` turns
        # the crank, and `WorldMapActivity` does the reading one screen per tick. The partial
        # -read refusal moved with it: the activity will not certify a short list, and the
        # runner will not plan from one.
        from brain.barter_runner import FAILED as _TASK_FAILED, BarterTaskRunner
        from brain.run_goal import run_task

        runner = run_task(BarterTaskRunner(village=cmd.village, good=cmd.good))
        if runner.status == _TASK_FAILED or not runner.trades:
            return {"ok": False, "step": "check", "command": cmd,
                    "reason": runner.reason or f"could not read {cmd.village}"}
        # The runner IS the reading: it carries the trades and the day's rounds, and answers
        # `trade_for` / `rounds_remaining` exactly as `VillageCheck` did. Adapting it into a
        # `VillageCheck` would mean importing one from `actions` — the reach this change
        # exists to remove.
        check = runner
        trade = check.trade_for(cmd.good)
        if trade is None:
            return {"ok": False, "step": "check", "command": cmd,
                    "reason": f"{cmd.village} does not barter {cmd.good!r} — it offers "
                              f"{[t.good for t in check.trades]}"}
        # A FRESH READ CAN BE PARTIAL TOO. `_recipe_is_partial` guards the CACHED path; this
        # is the same condition arriving by the other route, and it was left open — live
        # 2026-08-27 the read reached the bottom of the list, still never returned Matchlock
        # Gun, and the plan was built from two of three materials anyway.
        #
        # The material list is INVARIANT, so a missing one is a reading failure and not a
        # recipe change. Completing it from the KB is what lets the mission proceed; refusing
        # would be safe and would also block every run until the read is fixed, and arriving
        # with two of three materials is the one outcome that guarantees no barter at all.
        #
        # The RATIO is filled from the KB too, and that is an estimate — ratios re-roll (Iron
        # went 102 -> 126 overnight). It only has to be close: the plan's numbers are a
        # guide, the panel decides each round, and the +15% cushion is sized for exactly this.
        _complete_materials_from_kb(trade, cmd.good, cmd.village)
        rounds_remaining = check.rounds_remaining
    if rounds_remaining is None:
        return {"ok": False, "step": "check", "command": cmd,
                "reason": "Daily Barter Progress unreadable — cannot size the mission"}
    if rounds_remaining <= 0:
        return {"ok": False, "step": "check", "command": cmd,
                "reason": f"no barter rounds left at {cmd.village} today "
                          f"({check.barters_used}/{check.barters_total} used)"}

    # ── CLEAR SURPLUS (opt-in): free the hold BEFORE the cargo read, so the plan
    # sizes against real space rather than space we are about to make. ───────────
    cleared = None
    if clear_surplus and not dry_run:
        from brain.barter_mission_live import clear_surplus_at_current_port
        cleared = clear_surplus_at_current_port(cmd.good)
        if not cleared.get("ok"):
            return {"ok": False, "step": "clear-surplus", "command": cmd,
                    "reason": f"could not clear surplus: {cleared.get('reason')}"}
        logger.info(f"[barter_command] cleared surplus at {cleared.get('port')}: "
                    f"{cleared.get('reason')}")

    # ── PLAN: rounds bounded by the daily allowance and the free hold ────────
    capacity, used = cargo_capacity, cargo_used
    # THE HOLD IS NOT READ TO START A MISSION (user, 2026-08-29).
    #
    # Two numbers came back and the plan uses ONE of them: `free_space_for_barter(capacity,
    # 0)` passes the cargo as literal zero, deliberately — "what is already in the hold is
    # deliberately NOT subtracted" (user, 2026-08-27). And capacity is a property of the
    # SHIP, constant for the whole mission.
    #
    # So the opening move was a read of the ☰ for a constant. The ☰ lives on the overworlds,
    # and live 2026-08-29 a run started in a village died on tick 2 — "cannot open the main
    # menu from 'village' (no ☰ there)" — before touching the game. Reading the hold is no
    # more the village's work than sailing was.
    #
    # Remembered instead: the capacity is stamped whenever the hold IS read somewhere it can
    # be. Only a fleet never once read falls through to asking.
    if capacity is None:
        try:
            from memory.observed_facts import recall
            seen = recall("fleet_capacity", max_age_s=None)
            if seen is not None:
                capacity, _age = seen[0], seen[1]
                logger.info(f"[barter_command] capacity {capacity} — the ship, remembered; "
                            "not re-reading the hold to start")
        except Exception as exc:
            logger.debug(f"[barter_command] no remembered capacity: {exc}")
    used = 0 if used is None and capacity is not None else used
    if capacity is None or used is None:
        # THE HOLD IS ASKED FOR, NOT FETCHED (Guiding Principle #7).
        #
        # This was a sixty-line ladder: read the fleet; if it came back empty, sweep for
        # blockers and read again; if it STILL came back empty and the reason mentioned a
        # missing ☰, walk to the port overworld and read a third time. Every rung had an
        # owner already. The sweep is the dispatcher's `unblock`, which runs on every tick.
        # The walk is its routing — `ReadHold` from inside a building now yields an
        # EXIT_BUILDING intent, one Back per tick, instead of an FSM path executed inside a
        # single call. What is left is one read, done by the activity that owns the screen.
        from brain.barter_runner import (FAILED as _TASK_FAILED, NEED_HOLD,
                                         BarterTaskRunner)
        from brain.run_goal import run_task

        hold = run_task(BarterTaskRunner(village=cmd.village, good=cmd.good,
                                         status=NEED_HOLD))
        if hold.status != _TASK_FAILED:
            capacity = capacity if capacity is not None else hold.capacity
            used = used if used is not None else hold.used
        _hold_reason = hold.reason
    if capacity is None or used is None:
        # Assuming an empty hold would over-plan the gather; assuming a full one would
        # abandon a good mission.  Neither is a guess worth making (never act blind).
        missing = "capacity" if capacity is None else "current cargo"
        # SAY WHY, NOT JUST WHAT. "cargo capacity unreadable" describes the symptom and
        # points at the market reader; twice at Bordeaux the actual cause was a full-screen
        # arrival gate hiding the ☰ so the main menu never opened. An unreadable value means
        # the wrong screen, something covering it, or a read aimed at the wrong place
        # (user, 2026-08-24) — the layer above can only choose between those if it is told.
        # THE LAST OBSERVATION, NOT A NEW ONE. This called `perceive()` purely to name the
        # screen in the message below — a full OmniParser pass for a log line, from inside a
        # task module (Guiding Principle #7). It cost 90-120s per test and made this file look
        # like it hung. `last_seen()` returns what was already observed and captures nothing;
        # it also answers the more useful question, since a fresh pass would describe the
        # screen AFTER the failure rather than during it.
        try:
            from brain.perceive import last_seen
            _where = getattr(last_seen(), "location", None)
        except Exception:
            _where = None
        logger.error(f"[barter_command] fleet unreadable while the screen reads {_where!r} "
                     f"— {_hold_reason}")
        # `cleared_surplus` rides along even on this early exit — if cargo was sold, the
        # caller must be told, whatever step we stopped at.
        return {"ok": False, "step": "plan", "command": cmd, "cleared_surplus": cleared,
                "reason": f"cargo {missing} unreadable on screen {_where!r} — the main menu "
                          "never opened (a gate or popup may be covering it); pass "
                          "cargo_capacity=/cargo_used= to plan against known numbers"}
    # SIZE THE MISSION ON THE SHIP, NOT ON TODAY'S CLUTTER. The budget is capacity minus a
    # supply reserve; what is already in the hold is deliberately NOT subtracted. Trade
    # goods aboard are fungible with the mission — they get trimmed, exchanged away by the
    # barter itself, or dumped on overflow — so counting them as unavailable makes the hold
    # its own obstacle (user, 2026-08-27).
    #
    # Subtracting them was circular: a full hold shrank the plan, and the trim then sized
    # itself on that shrunken plan and sold material the mission needed. Live 2026-08-27 at
    # Barcelona a hold of 3,040/4,952 planned ONE round of seven available and set out to
    # trim Iron 2,099 → 102 and Candle 148 → 61, when five rounds fit and it should have
    # been keeping 506 of each. `plan_barter_rounds` still caps rounds by what the OUTPUT
    # needs, so space remains a real limit — just not a self-inflicted one.
    free_space = free_space_for_barter(capacity, 0)
    plan = plan_barter_rounds(trade.obtain, trade.materials, rounds_remaining, free_space,
                              materials_on_hand=((cleared or {}).get("owned")
                                                 or _last_known_hold()),
                              cushion=AMITY_CUSHION if cushion is None else cushion)
    logger.info(f"[barter_command] plan: {plan.rounds} round(s) "
                f"(limited by {plan.limited_by}), buy {plan.total_needs} "
                f"(+{plan.cushion:.0%} amity/refresh cushion), "
                f"expect ~{plan.output_qty} {cmd.good}")
    summary = {"command": cmd, "check": check, "plan": plan, "cleared_surplus": cleared,
               "cargo_capacity": capacity, "cargo_used": used}
    if plan.rounds <= 0 and cleared is None and not dry_run:
        # BLOCKED BY SPACE, and the hold has not been cleared yet. This is precisely the
        # condition surplus-clearing exists to resolve, so do it and re-plan once rather
        # than reporting a dead end.
        #
        # Without this the mission deadlocks: the hold is too full to plan any rounds, and
        # the `sell_surplus` node that would empty it lives INSIDE the graph that is only
        # built when rounds > 0. Live 2026-08-21: a run left the hold at 3823/4108 after an
        # over-buy, and every subsequent mission exited at "plan: 0 round(s)" without ever
        # selling anything — the capability was present twice over and reachable neither way.
        # What the mission WOULD need if the hold were free. Used as the trim target: an
        # over-bought material is surplus above this, and trimming it is the only thing that
        # frees space when the hold is entirely materials (which whole-good clearing keeps).
        ideal = plan_barter_rounds(trade.obtain, trade.materials, rounds_remaining,
                                   free_space_for_barter(capacity, 0),
                                   cushion=AMITY_CUSHION if cushion is None else cushion)
        trim_to = {m: q for m, q in (ideal.total_needs or {}).items() if q > 0}
        logger.info(f"[barter_command] no feasible rounds with {free_space} free — clearing "
                    f"surplus and trimming materials to {trim_to}, then re-planning")
        from brain.barter_mission_live import clear_surplus_at_current_port
        cleared = clear_surplus_at_current_port(cmd.good, trim_to=trim_to or None)
        if cleared.get("ok"):
            logger.info(f"[barter_command] cleared surplus at {cleared.get('port')}: "
                        f"{cleared.get('reason')}")
            # RE-MEASURE AFTER CLEARING — asked for, not fetched. Same `ReadHold` work
            # order as the first read; the numbers changed because the hold did.
            from brain.barter_runner import NEED_HOLD, BarterTaskRunner
            from brain.run_goal import run_task

            re_read = run_task(BarterTaskRunner(village=cmd.village, good=cmd.good,
                                                status=NEED_HOLD))
            used = re_read.used if re_read.used is not None else used
            capacity = re_read.capacity if re_read.capacity is not None else capacity
            free_space = free_space_for_barter(capacity, 0)
            plan = plan_barter_rounds(trade.obtain, trade.materials, rounds_remaining,
                                      free_space,
                                      materials_on_hand=(cleared.get("owned")
                                                         or _last_known_hold()),
                                      cushion=AMITY_CUSHION if cushion is None else cushion)
            logger.info(f"[barter_command] re-plan after clearing: {plan.rounds} round(s), "
                        f"buy {plan.total_needs}, expect ~{plan.output_qty} {cmd.good}")
        else:
            logger.warning(f"[barter_command] surplus clear failed: {cleared.get('reason')}")
        summary = {**summary, "plan": plan, "cleared_surplus": cleared,
                   "cargo_capacity": capacity, "cargo_used": used}

    if plan.rounds <= 0:
        return {"ok": False, "step": "plan", **summary,
                "reason": f"no feasible rounds: free space {free_space} < "
                          f"{plan.reserved_per_round} reserved for one round "
                          f"({plan.peak_per_round} nominal +{plan.cushion:.0%} cushion)"}
    if dry_run:
        return {"ok": True, "step": "plan", "dry_run": True, **summary}

    if prog is None:                      # first run of this task — pin the recipe now
        mission_progress.start(cmd.village, cmd.good, plan.rounds,
                  recipe={"good": cmd.good, "obtain": trade.obtain,
                          "materials": dict(trade.materials)},
                  rounds_remaining=rounds_remaining)
    return {**summary, **_run_mission_for(
        cmd, trade, plan, from_port=from_port,
        # What the hold was measured to contain during the surplus clear, so the gather
        # legs cover only the shortfall instead of re-checking each port.
        already_held=(cleared or {}).get("owned") if isinstance(cleared, dict) else None)}


# A mission is many legs and every leg is many ticks — a voyage alone is dozens. The ceiling
# is a runaway guard, not a policy; `run_task` also stops on consecutive stalls, which is what
# actually catches a mission that is stuck.
_MISSION_MAX_TICKS = 600


def _village_leg_floor() -> float:
    """The round-trip supply floor a village leg carries — a village has no harbour."""
    from brain.supply_planner import VILLAGE_LEG_RESERVE_DAYS
    return float(VILLAGE_LEG_RESERVE_DAYS)


def _run_mission_for(cmd: BarterCommand, trade, plan, from_port: Optional[str] = None,
                     already_held: Optional[dict] = None) -> dict:
    """Build the sub-task graph from the live plan and run it."""
    from brain.barter_mission_live import (catalogue_coords, current_position,
                                           make_live_executors, plan_barter_task)
    from brain.mission import (Opportunity, MissionTail, build_barter_graph,
                               make_barter_recover, run_mission)
    from memory.barter_kb import load_recipe

    recipe = load_recipe(cmd.good)          # written back by the check moments ago
    if recipe is None:
        return {"ok": False, "step": "plan",
                "reason": f"no recipe for {cmd.good!r} even after the check wrote back"}

    sell_port = cmd.tail_value if cmd.tail_kind == "sail" else None
    coords = catalogue_coords()
    start = None
    if from_port:
        from brain.barter_mission_live import _strip_accents
        start = coords.get(_strip_accents(from_port).title())
        if start is None:
            return {"ok": False, "step": "gather-plan",
                    "reason": f"start port {from_port!r} is not in the catalogue"}
        logger.info(f"[barter_command] start position given: {from_port} {start}")
    if start is None:
        start = current_position(coords)
    if start is None:
        # AN UNREADABLE PORT IS A WORSE ROUTE, NOT A DEAD MISSION (user, 2026-08-31: "from
        # port is only for good logging, for sailing it is really not important").
        #
        # The origin ORDERS the gather legs by distance; it does not decide which ports to
        # visit or whether the fleet can sail. Without it the legs run in an arbitrary order
        # — some extra sailing, and every leg still reachable, because choosing a destination
        # on the world map never depended on knowing where we started.
        #
        # This refused instead, and it cost two runs on consecutive days. Both times the
        # cause was the same and had nothing to do with legibility: the REMOTE CHECK leaves
        # the fleet on the world map, which paints no port name, so the very step that reads
        # the recipe guarantees the next one cannot see a port.
        #
        # The 2026-08-21 case this guard was written for is still respected — a MADE-UP
        # origin sent the fleet 5,948 units the wrong way. Ordering by nothing is not the
        # same as ordering by a fiction: we drop the ordering rather than invent a place.
        logger.warning("[barter_command] no readable port to order the gather route from — "
                       "running the legs unordered. Pass --from=<port> for a shorter route; "
                       "the mission does not need it to sail.")
    # What the hold was measured to contain during the surplus clear. Passing it means the
    # gather legs cover only the SHORTFALL — the mission does not sail to a port to
    # rediscover materials it is already carrying.
    held = already_held
    task_plan = plan_barter_task(recipe, cmd.village, sell_port or "", plan.rounds,
                                 coords, start, needs=plan.total_needs,
                                 output_per_round=trade.obtain,
                                 already_held=held)
    if task_plan.unsourced:
        return {"ok": False, "step": "gather-plan", "task_plan": task_plan,
                "reason": f"no known source port for {task_plan.unsourced} — the check "
                          "could not read their location pins"}

    opp = Opportunity(kind="seasonal_barter", good=cmd.good, sell_port=sell_port or "",
                      village=cmd.village, rounds=plan.rounds, recipe=recipe)
    if cmd.tail_kind == "route":
        tail = MissionTail(kind="route", value=cmd.tail_value, sell_port=None)
    elif cmd.tail_kind == "sail":
        tail = MissionTail(kind="sail", value=sell_port, sell_port=sell_port)
    else:
        tail = MissionTail(kind="none")
        logger.warning("[barter_command] no destination given — the mission ends at the "
                       "village; add 'then take the route X' or 'and sail to Y' to sell")
    graph = build_barter_graph(opp, task_plan, tail=tail)
    # CARGO DECIDES WHEN GATHERING IS DONE. No gather legs means the hold already covers the
    # recipe, so that phase is finished — record it, and nothing downstream re-opens the
    # question (user 2026-08-22: "each time the bot checks its cargo, and if it has
    # materials, it should mark it as finished").
    mission_progress.advance("gathering")          # the plan is made — remote checking is over
    if not any(t.kind == "gather" for t in graph):
        # Nothing left to gather: the hold already covers the recipe, so the surplus clear
        # that precedes this point was the last cargo question. Enter the barter phase.
        mission_progress.advance("bartering")

    logger.info(f"[barter_command] graph: {[t.id for t in graph]}")

    # THE DISPATCHER WALKS THE MISSION NOW (user, 2026-08-29).
    #
    # `run_mission` walked the same graph but EXECUTED each leg — `executors[kind](task)`,
    # each running a whole leg to completion with its own loops inside. So the dispatcher
    # never saw the mission: it was handed "sail to Amsterdam" by an executor that had already
    # decided to gather there, and the reason why — Iron 822, at Amsterdam, because the plan
    # says so — lived a layer above anything it could consult.
    #
    # `MissionRunner` answers "which leg next, and what does that leg want" and nothing else.
    # The work order that reaches the dispatcher is `Hold({'Iron': 822})`, which names the
    # what and the where. Two levels of task-running became one.
    from brain.mission_runner import DONE, MissionRunner
    from brain.run_goal import run_task

    runner = run_task(MissionRunner(subtasks=graph, coords=coords, good=cmd.good,
                                    village=cmd.village,
                                    keep=("Water", "Food", *sorted(trade.materials)),
                                    min_supply_days=_village_leg_floor()),
                      max_ticks=_MISSION_MAX_TICKS)
    ok = runner.status == DONE
    return {"ok": ok, "step": "mission",
            "reason": "every leg is done" if ok else runner.where_it_stopped(),
            "completed": runner.completed, "task_plan": task_plan}
