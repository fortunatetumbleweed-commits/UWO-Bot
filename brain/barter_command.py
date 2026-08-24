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
    """Back out of a village until the fleet is at sea. True when it reaches sea.

    A village is a PLACE, so leaving it is never done to satisfy a state test — but the tail
    genuinely requires it, and that is the task's call to make (see
    docs/one_loop_task_drives_state.md, "the state machine owns the HOW, the task owns the
    WHETHER"). This is the task making it, deliberately and in one place.
    """
    from actions.sail_actions import press_back, where_am_i
    from capture.adb_capture import capture_screen
    import time as _t

    for attempt in range(max_backs):
        try:
            state = where_am_i(capture_screen()).get("location")
        except Exception as exc:
            logger.warning(f"[barter_command] could not perceive while leaving the village: "
                           f"{exc}")
            return False
        if state in ("sea", "sea_cinematic", "world_map"):
            logger.info(f"[barter_command] at {state!r} — clear of the village, the tail can "
                        "open the world map")
            return True
        logger.info(f"[barter_command] leaving the village for the tail "
                    f"(state={state!r}, back {attempt + 1}/{max_backs})")
        press_back()
        _t.sleep(2.0)
    logger.warning("[barter_command] could not reach the sea from the village — the tail "
                   "cannot open the world map from here")
    return False


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


def _try_barter_here(cmd) -> Optional[dict]:
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
    from brain.barter_mission_live import (_open_barter_panel, _read_panel_state,
                                           _select_trade_good, make_live_executors)
    from brain.mission import SubTask

    at_village, why = _at_a_village()
    if not at_village:
        logger.info(f"[barter_command] not at the village ({why}) — planning the trip")
        return None

    logger.info(f"[barter_command] at the village ({why}) — reading the ratio from the "
                "panel instead of opening the world map")
    if not _open_barter_panel():
        logger.info("[barter_command] could not open the Barter panel here — falling back "
                    "to the remote check")
        return None
    if not _select_trade_good(cmd.good, None):
        logger.info(f"[barter_command] {cmd.good!r} is not on offer here — falling back")
        return None

    state = _read_panel_state()
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


def _at_a_village() -> tuple:
    """(are we at a village?, why). Includes the village's SUB-SCREENS, not just its landing.

    `state == "village"` alone is too narrow. Live 2026-08-23 a run began with the BARTER
    sub-menu still open from the previous run; the classifier's first verdict was 'building'
    (corrected afterwards to 'village'), the narrow test failed, and the fleet sailed away
    from the village it was standing in — for the second time that day.

    The left menu settles it. A village's menu — barter / explore / gifting / loot /
    recruit crew — belongs to no other screen, and it stays visible on every sub-screen,
    which is exactly the case the state alone gets wrong. Read via the canonical region
    detector (vision.region_detectors.left_menu).
    """
    from actions.sail_actions import where_am_i
    from capture.adb_capture import capture_screen
    try:
        frame = capture_screen()
        # CLEAR WHAT IS IN THE WAY BEFORE DECIDING. A blocked screen is not evidence about
        # where the fleet is. Live 2026-08-23 the game had dropped to its standby lock while
        # idle — state read as 'learned_on_standby_at_sea_slide_up_to_unlock', the left menu
        # was unreadable, and the guard was about to sail away from the village behind the
        # lock. Same rule as brain/goals/sail_to._handle_unknown: never navigate on blindness.
        try:
            from brain.unexpected_dialog import clear_blockers
            if clear_blockers(frame).get("cleared"):
                logger.info("[barter_command] a blocker was covering the screen — cleared it, "
                            "re-perceiving before deciding whether to sail")
                frame = capture_screen()
        except Exception as exc:
            logger.debug(f"[barter_command] blocker check failed: {exc}")
        state = where_am_i(frame).get("location")
    except Exception as exc:
        logger.warning(f"[barter_command] could not perceive: {exc}")
        return False, f"perceive failed: {exc}"
    if state == "village":
        return True, "state is 'village'"
    try:
        from vision.omniparser import parse_fast_cached
        from vision.region_detectors.left_menu import detect_left_menu
        menu = detect_left_menu(list(parse_fast_cached(frame)), frame.width, frame.height)
        labels = {l.strip().lower() for l in (menu.labels() if menu else [])}
    except Exception as exc:
        logger.debug(f"[barter_command] left-menu read failed: {exc}")
        labels = set()
    if _VILLAGE_MENU_MARKERS <= labels:
        return True, f"state is {state!r} but the left menu is a village's ({sorted(labels)})"
    return False, f"state is {state!r}, menu={sorted(labels) or 'unreadable'}"


# Menu entries that together belong only to a village. 'barter' alone is not enough — the
# word turns up elsewhere — but barter+gifting does not occur on any port screen.
_VILLAGE_MENU_MARKERS = {"barter", "gifting"}


# Each pass must commit something or the loop stops; this only bounds a pathological
# case where the panel keeps claiming rounds that never commit.
_MAX_BARTER_PASSES = 4


def _resume_at_village(cmd, prog: dict) -> dict:
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
    at_village, why = _at_a_village()
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
    from actions.village_check import read_village_barter_remote
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
        return {**_resume_at_village(cmd, prog), "resumed_from": prog.get("phase")}

    # ENTRY BY PERCEPTION: if the fleet is already standing in the village, the ratio is on
    # the panel in front of it and the whole check-and-plan preamble is moot.
    if not dry_run and prog is None:
        done_here = _try_barter_here(cmd)
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
    if trade is not None:
        logger.info(f"[barter_command] reusing the recipe from the start of this task "
                    f"({prog.get('age_s', 0):.0f}s ago): {cmd.good} {trade.obtain} ← "
                    f"{trade.materials} — not re-checking {cmd.village}")
    else:
        check = read_village_barter_remote(cmd.village, good=cmd.good)
        if not check.ok:
            return {"ok": False, "step": "check", "reason": check.reason, "command": cmd}
        trade = check.trade_for(cmd.good)
        if trade is None:
            return {"ok": False, "step": "check", "command": cmd,
                    "reason": f"{cmd.village} does not barter {cmd.good!r} — it offers "
                              f"{[t.good for t in check.trades]}"}
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
    if capacity is None or used is None:
        from actions.fleet_status import read_fleet_status
        status = read_fleet_status()
        capacity = capacity if capacity is not None else status.get("cargo_capacity")
        used = used if used is not None else status.get("cargo_used")
    if capacity is None or used is None:
        # Assuming an empty hold would over-plan the gather; assuming a full one would
        # abandon a good mission.  Neither is a guess worth making (never act blind).
        missing = "capacity" if capacity is None else "current cargo"
        # `cleared_surplus` rides along even on this early exit — if cargo was sold, the
        # caller must be told, whatever step we stopped at.
        return {"ok": False, "step": "plan", "command": cmd, "cleared_surplus": cleared,
                "reason": f"cargo {missing} unreadable — pass cargo_capacity=/cargo_used= "
                          "to plan against known numbers"}
    free_space = free_space_for_barter(capacity, used)
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
            from actions.fleet_status import read_fleet_status
            status = read_fleet_status()
            used = status.get("cargo_used", used)
            capacity = status.get("cargo_capacity", capacity)
            free_space = free_space_for_barter(capacity, used)
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
        # The gather ORDER is chosen by distance from here, so a made-up origin sends the
        # fleet the wrong way across the world (live 2026-08-21: Atuona at 5,948 instead
        # of Masulipatnam at 294). Refuse rather than guess.
        return {"ok": False, "step": "gather-plan",
                "reason": "current port unreadable, so the gather route cannot be ordered "
                          "— pass from_port= (CLI: --from=<port>) or re-run where the port "
                          "name is legible"}
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
    result = run_mission(graph, coords, make_live_executors(opp), start=start,
                         recover=make_barter_recover(recipe))
    return {"ok": result.ok, "step": "mission", "reason": result.reason,
            "mission": result, "task_plan": task_plan}
