"""Live wiring for the barter mission — the deterministic planning core plus one
executor per sub-task kind, composed from sanctioned primitives (safe taps only).

Task: barter <good> at <village>, sell at <sell_port> (or at the end of a route). Flow:
  plan (live check quantities × rounds → material needs → gather route + purchase
  assignment) → GATHER (sail to each source port → Market → buy) → SUPPLY-VERIFY →
  SAIL to village → BARTER rounds (panel-driven) → route/sail tail → SELL.

Entry point is `brain.barter_command.run_barter_command`; the executors here are driven
by the sub-task scheduler in `brain/mission.py`. The rigid 5-phase `run_barter_task_live`
runner and its make_*_fn phase factories were removed 2026-08-20 (superseded and unused).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from loguru import logger

from memory.barter_kb import BarterRecipe
from brain import mission_progress, owned_state
from brain.gathering_solver import plan_gathering, assign_purchases


# ── THE FAÇADE, AND WHY IT IS STILL HERE ─────────────────────────────────────
#
# FUTURE ENHANCEMENT, deliberately deferred (user, 2026-08-28: "lets mark this
# barter_mission_live facade as future enhancement, and log all the calls and see if they
# cause issues").
#
# This module is a task module that still reaches the UI: 8 UI imports, and functions that
# capture screens and drive `run_goal` themselves. Two modules that are otherwise clean —
# `brain/barter_command.py` and `brain/barter_task.py` — reach the UI THROUGH it, which is
# how `barter_command` measured 0 UI imports while using the whole barter panel. That second
# hop is declared in `tests/test_the_layering_is_enforced.py::KNOWN_SECOND_HOP` so it cannot
# grow.
#
# What is left, and who owns it when it moves:
#
#   current_position / _current_port    "where are we?" — the dispatcher perceives it
#   _at_a_market_port / supply_verify   the same question, again
#   gather / sell / sell_surplus / barter   run_goal drivers — market work orders
#   _enter_market_at / _exit_market_to_overworld   transitions the dispatcher should route
#
# The market work is the bulk of it and is being left alone for now. Until then, every call
# through the façade is LOGGED with its caller, so a live run says whether these paths are
# actually causing trouble rather than merely being in the wrong place. A quiet log is
# evidence the deferral is safe; a noisy one names the first thing to move.


def _facade(fn):
    """Log a call through the façade, with the caller that made it.

    Not a deprecation — these are the paths the mission still runs on. It is a MEASUREMENT:
    the point of deferring this refactor is to find out whether it costs anything, and that
    cannot be answered from the code. It has to be watched.
    """
    import functools
    import inspect as _inspect

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        caller = "?"
        for fr in _inspect.stack()[1:]:
            if fr.filename != __file__:
                caller = f"{fr.filename.rsplit('/', 1)[-1]}:{fr.lineno} in {fr.function}()"
                break
        logger.info(f"[facade] {fn.__name__} <- {caller}")
        return fn(*args, **kwargs)

    return wrapped



# The village menu animates in and a standby gate can cover it for a moment, so one
# empty capture is not proof the menu is absent — look again before concluding.
_MENU_READ_ATTEMPTS = 3


# ── Deterministic planning core (unit-tested) ──────────────────────────────────

def compute_material_needs(recipe: BarterRecipe, rounds: int) -> dict:
    """{material: total units needed} = ratio × rounds for each recipe input."""
    return {i.material: i.ratio * rounds for i in recipe.inputs}


def material_sources_from_recipe(recipe: BarterRecipe, village: Optional[str] = None) -> dict:
    """{material: [source ports]} for the materials THIS VILLAGE wants.

    Village-scoped because the same good takes different materials at different villages
    — sourcing from the union would send the fleet shopping for something the village
    will not accept. Materials with no known source come back empty → read the pins."""
    return {i.material: list(i.source_ports or [])
            for i in recipe.inputs_for(village)}


@dataclass
class TaskPlan:
    good: str
    village: str
    sell_port: str
    rounds: int
    needs: dict                 # {material: qty}
    gather_route: list          # ordered source ports
    purchases: dict             # {port: {material: qty}}
    unsourced: list             # materials with no known source (read live)
    total_output: int


def plan_barter_task(recipe: BarterRecipe, village: str, sell_port: str,
                     rounds: int, port_coords: Mapping[str, tuple],
                     start: tuple, needs: Optional[Mapping[str, int]] = None,
                     output_per_round: Optional[int] = None,
                     already_held: Optional[Mapping[str, int]] = None) -> TaskPlan:
    """Build the full task plan from the KB recipe. Deterministic.

    `needs` / `output_per_round` override the KB numbers with the LIVE ones from a
    remote village check (quantities refresh every ~6h, so the KB copy is only a
    fallback for callers that have no fresh check)."""
    needs = dict(needs) if needs is not None else compute_material_needs(recipe, rounds)

    # BUY ONLY THE SHORTFALL. `already_held` is what the hold was just measured to contain —
    # after a surplus clear the mission has that number in hand, so the gather legs for
    # materials it already carries are not needed at all (user 2026-08-22: "after sell
    # surplus ... it should just go to the village without checking anymore, because we
    # already checked before selling the surplus").
    #
    # Without this the mission sails to each gather port to re-discover what it already
    # knows: on 2026-08-22, holding {Ebony 700, Coral 797, Textiles 920} against a need of
    # {175, 263, 263}, it still set course for Kolkata.
    if already_held:
        short = {}
        for m, q in needs.items():
            have = int(already_held.get(m.lower(), 0))
            if q - have > 0:
                short[m] = q - have
        if not short:
            logger.info(f"[plan] the hold already covers {needs} — no gather legs needed")
        else:
            logger.info(f"[plan] buying only the shortfall {short} of {needs} "
                        f"(hold covers the rest)")
        needs = short

    sources = material_sources_from_recipe(recipe, village)
    gp = plan_gathering(list(needs), sources, port_coords, start, quantities=needs)
    purchases = assign_purchases(gp.route, sources, needs)
    out_per_round = output_per_round if output_per_round is not None else (
        (recipe.output_per_round or {}).get("Neutral") or 0)
    return TaskPlan(good=recipe.good, village=village, sell_port=sell_port,
                    rounds=rounds, needs=needs, gather_route=gp.route,
                    purchases=purchases, unsourced=sorted(gp.unsourced),
                    total_output=out_per_round * rounds)


def _strip_accents(s: str) -> str:
    """Accent-insensitive key: 'Malé' → 'male', 'Málaga' → 'malaga'. The material
    Source panel reads port names WITHOUT accents (OmniParser 'Male'), but the
    catalogue stores them accented ('malé') — normalise both sides to match."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c)).lower()


@_facade
def catalogue_coords() -> dict:
    """{port: (x, y)} from the COMPLETE 224-port catalogue
    (memory/knowledge/world_map/port_coordinates.json via load_port_catalogue),
    keyed by ACCENT-STRIPPED title-case so accent-free reads (Male, Malaga) match.

    This is the CANONICAL port list (the old config/port_positions.json duplicate was
    removed 2026-08-15; actions/world_map.PORT_POSITIONS now derives from this
    catalogue too)."""
    from vision.world_map_parser import load_port_catalogue
    out = {}
    for name, rec in load_port_catalogue().items():
        if isinstance(rec, dict) and rec.get("x") is not None:
            out[_strip_accents(name).title()] = (rec["x"], rec["y"])
    return out


# ── Live executors for the sub-task mission runner (brain/mission.py) ───────────
# Bridges the tested EXECUTE plumbing (dynamic scheduler + recovery) to the validated
# primitives — one executor per SubTask.kind, each doing ONE sub-task. This is the
# opportunity-driven path (docs/opportunity_driven_architecture.md); it replaced the rigid
# 5-phase runner, which was deleted 2026-08-20 once nothing called it.

@_facade
def current_position(coords: Mapping[str, tuple], fallback=None, tries: int = 3):
    """The bot's current position as (x, y) for the scheduler — from where_am_i()'s port,
    mapped through the (accent-stripped) catalogue.

    Returns `fallback` (None by default) when the port cannot be read. It used to default
    to (0,0), and that is NOT a harmless guess: the scheduler picks the cheapest runnable
    gather FROM THE CURRENT POSITION, so a fabricated origin reorders the whole voyage.
    Live 2026-08-21 it sent the fleet toward Atuona — 5,948 units away — when Masulipatnam
    was 294 away, because Atuona happens to be nearest the map origin. A port ALWAYS has a
    name, so an unreadable one is an anomaly worth retrying and then reporting, never
    papering over (CLAUDE.md: never act blind)."""
    import time as _t
    from actions.sail_actions import where_am_i
    from capture.adb_capture import capture_screen
    from brain.observation import screen_shows_settlement
    from memory.observed_facts import recall, remember

    def _place(port: str, how: str, *, record: bool = False):
        pos = coords.get(_strip_accents(port).title())
        if pos is not None:
            if record:
                # Remember only a name the CATALOGUE recognises. `read_port_name` returns an
                # unmatched read raw, so sea-region chrome reaches here as a "port" — live
                # 2026-08-22 it offered 'Lauless Waters' (the Lawless Waters sea region, best
                # similarity 0.50). Storing that would hand a fabricated origin to every later
                # lookup, which is the exact failure the fallback exists to prevent.
                remember("settlement", port)
            logger.info(f"[mission] start position: {port} {pos}{how}")
            return pos
        logger.warning(f"[mission] port {port!r} is not in the catalogue")
        return fallback

    for attempt in range(max(1, tries)):
        try:
            here = where_am_i(capture_screen())
            port, state = here.get("port"), here.get("location")
        except Exception as exc:
            logger.debug(f"[mission] current_position read failed: {exc}")
            port, state = None, None
        if port:
            return _place(port, "", record=True)   # stamped, so its age is answerable later
        # Only the port/village OVERWORLD paints the name. Retrying on any other screen is
        # futile by construction, so ask what we last SAW instead of looking again. Live
        # 2026-08-22 this loop retried three times on the MAIN MENU and aborted the mission,
        # two minutes after 'Kolkata' had been confirmed twice on the overworld.
        if not screen_shows_settlement(state):
            seen = recall("settlement")
            if seen is not None:
                name, age = seen
                logger.info(f"[mission] {state!r} does not show the port name — "
                            f"using {name!r}, seen {age:.0f}s ago")
                return _place(name, f" (remembered {age:.0f}s ago)")
            logger.warning(f"[mission] {state!r} does not show the port name and none is "
                           "remembered — cannot place the fleet on the map")
            return fallback
        if attempt + 1 < tries:
            _t.sleep(1.5)
    logger.warning("[mission] current port unreadable — cannot place the fleet on the map")
    return fallback


@_facade
def _exit_market_to_overworld() -> None:
    """Leave the market back to port_overworld so the next sub-task's sail starts from
    a KNOWN state (the frame-19 stall was SailToGoal inheriting a market it should never
    have seen — see docs/action_verification_and_recovery_design.md §6). Best-effort:
    a failure here shouldn't fail the buy/sell that already happened."""
    try:
        from actions.sail_actions import exit_to_overworld
        if not exit_to_overworld(timeout=60.0):
            logger.warning("[mission] exit to port_overworld not confirmed after buy/sell")
    except Exception as exc:
        logger.debug(f"[mission] exit_to_overworld skipped: {exc}")


@_facade
def _enter_market_at(port: str) -> dict:
    """Get into the Market at *port*. Returns {"ok": bool, "reason": str}.

    `navigate_to_building` no longer walks the bot out of wherever it happens to be — it
    reports and returns (see docs/one_loop_task_drives_state.md). That was the right fold:
    the primitive pressing Back on a screen it did not recognise is what cancelled a
    successful departure four times over. But it means SOMEONE has to do the walking, and
    at this layer that someone is the task.

    So: try to enter; if the screen was not a port overworld, reorient to one and try once
    more. `may_leave_a_place=False` — this is stepping out of a PANEL, never out of the
    settlement the mission sailed to.
    """
    from actions.sail_actions import navigate_to_building
    from brain.nav_step import reorient_to, ARRIVED

    if navigate_to_building("Market"):
        return {"ok": True, "reason": ""}

    logger.info(f"[mission] Market at {port} not reachable from here — reorienting")
    step = reorient_to("port_overworld", may_leave_a_place=False)
    if step.outcome != ARRIVED:
        return {"ok": False, "reason": f"could not reach the {port} overworld: {step.reason}"}
    if navigate_to_building("Market"):
        return {"ok": True, "reason": ""}
    return {"ok": False, "reason": f"could not reach Market at {port}"}


def make_live_executors(opp=None) -> dict:
    """kind -> executor(SubTask) -> {ok, reason, ...}. Reuses the sanctioned primitives
    (safe tap). Each returns a structured result so run_mission's recovery ladder can
    act (e.g. an out-of-stock gather → reroute).

    `opp` is accepted for the graph path's call signature but is not read by any executor
    (verified 2026-08-23) — every executor takes what it needs from its SubTask params. It
    defaults to None so callers that have no Opportunity, such as the resume path, can build
    the executors too. Live 2026-08-23 the resume path crashed on exactly this: the phase
    model routed a mission straight to `bartering`, and `make_live_executors()` raised
    TypeError before a single tap.
    """

    @_facade
    def gather(task) -> dict:
        from brain.activities.market import Hold
        from brain.run_goal import run_goal
        port = task.params["port"]
        orders = task.params["orders"]

        # DO WE STILL NEED THIS PORT? `buy_to_goal` already refuses to buy what we own, but
        # it only finds out at the market — AFTER sailing there. So the fleet crossed to a
        # port, walked to the Market, read the Sell tab, learned it needed nothing, and left
        # (live 2026-08-22: with every material already aboard it still set course for
        # Kolkata). Ask here, while we are still at a market that can answer, and skip the
        # whole leg when the orders are already satisfied.
        if orders_already_held(orders, port=port):
            return {"ok": True, "skipped": True,
                    "reason": f"already own the {port} orders"}

        sail = _sail_to(port)
        if not sail.get("ok"):
            return {"ok": False, "reason": f"sail to {port}: {sail.get('reason')}"}

        # THE MARKET IS TOLD THE WHOLE LIST, not one material per visit. Only the market
        # activity can see what this port actually stocks — it may carry one of the three or
        # all — so splitting the list forces this layer to guess. And a visit that knows the
        # whole list will not clear Iron out of the hold to make room for Candle.
        #
        # Getting to the market is an INTENT the loop dispatches; the tab tap, the shelf
        # rounds and the gem refresh are the activity's business and no longer appear here.
        result = run_goal(Hold(dict(orders)))
        if result is None:
            return {"ok": False, "reason": f"could not reach the market at {port}"}
        observed = dict(result.observed)
        _exit_market_to_overworld()   # clean hand-off: leave port_overworld for the next leg
        return {"ok": bool(result.ok), "port": port,
                "bought_total": observed.get("bought_total"),
                "met": observed.get("met"),
                "reason": observed.get("stopped_because") or result.detail}

    @_facade
    def sail_to_village(task) -> dict:
        # Sailing to a village is the SAME operation as sailing to a port — SailToGoal
        # dispatches Explore-tab village selection and its arrival check accepts the
        # 'village' state (user 2026-08-17: "no difference except the arrival check").
        # The difference that matters is SUPPLY: a village has no harbour, so this leg
        # carries a round-trip floor and the at-sea watch enforces it the whole way.
        from brain.supply_planner import VILLAGE_LEG_RESERVE_DAYS
        # Committed. From here the materials aboard are the materials we barter with, and
        # re-checking them can only cost us the position we are sailing to.
        # Gathering is over by construction — the graph runs gathers and the surplus
        # clear before this node. From here on, no cargo checks.
        mission_progress.advance("bartering")
        # The village is on the Explore tab, which `ChooseDestination(kind='village')`
        # already knows. The round-trip floor still rides along, but it is now WEIGHED by the
        # task runner from what `SeaActivity` reports each tick — an activity that decides to
        # turn back is deciding the mission's business.
        return _sail_to(task.params["village"], kind="village",
                        min_supply_days=task.params.get("min_days",
                                                        VILLAGE_LEG_RESERVE_DAYS))

    @_facade
    def barter(task) -> dict:
        """BARTER AT THE VILLAGE — the goal, and nothing about how.

        This node was 219 lines and was BOTH the goal and the procedure: it opened the panel,
        picked the tile, tapped Exchange, confirmed the dialog, cleared overflow, counted
        rounds and decided whether to sail. Nearly every bug of 2026-08-26 lived in it.

        Now it states a goal and hands it to the loop. `Barter("Birch Tree", "Svear Village")`
        mentions no panel, no tile and no round count — the panel bounds the rounds, and the
        village activity reports what it did in task vocabulary.

        `rounds` is still accepted in params and is deliberately IGNORED. It was the pre-sail
        estimate, computed from a remote check hours old on arrival and systematically too
        small: it priced each round at its peak footprint while a round actually FREES space
        (2026-08-23 — 648 units of materials out, 497 of product in, a net -151, so a plan of
        1 against materials that funded 3).
        """
        from brain.activities.village import Barter
        from brain.run_goal import run_goal

        good, village = task.params["good"], task.params.get("village", "")
        result = run_goal(Barter(good, village))
        if result is None:
            return {"ok": False, "committed": 0,
                    "reason": "could not reach the village barter panel"}

        observed = dict(result.observed)
        committed = int(observed.get("committed") or observed.get("rounds_committed") or 0)
        mission_progress.record_rounds(committed)

        # A STALL WITH NOTHING LEFT TO BARTER IS COMPLETION, NOT FAILURE. The activity has
        # already asked the only question that separates them — is Exchange still live — so
        # nothing here re-derives it from materials. Live 2026-08-23 the fleet bartered until
        # Coral hit 2 against a need of 3, the last tap changed nothing, and the mission
        # FAILED with its cargo aboard because a stall was read as an error.
        ok = bool(result.ok)
        if ok:
            mission_progress.advance("sailing_route")

        # `more_rounds_fundable` is 0 BY CONSTRUCTION now. The activity bartered until the
        # village refused, so there is nothing left for a caller to re-enter for — and
        # re-entering on a number the task re-derived from materials is precisely what caused
        # the 74-round loop at Svear (the phase declared itself finished, the outer check said
        # "74 more fundable", and it only stopped when it was killed). `barter_command`'s
        # re-entry loop still exists and now breaks immediately; it should be deleted once
        # this has run live.
        return {"ok": ok, "committed": committed, "more_rounds_fundable": 0,
                "good": observed.get("good", good),
                "amity": observed.get("amity"),
                "materials_left": observed.get("materials_left"),
                "reason": observed.get("stopped_because") or result.detail}


    @_facade
    def sail_to_sell(task) -> dict:
        return _sail_to(task.params["sell_port"])

    @_facade
    def sell_surplus(task) -> dict:
        """Trim each material down to what the plan needs, before the village leg.

        `buy_to_goal` buys by the shelf, so the hold arrives with more than the plan asked
        for; the excess is dead weight the barter output then cannot fit. This trims only
        goods the plan NAMED (whole-good disposal of unrelated cargo is the separate,
        opt-in pre-gather clear) and sells nothing it could not verify — see
        `actions.sell_goods.sell_down_to`."""
        from brain.activities.market import FreeHold, TrimHold
        from brain.run_goal import run_goal
        keep_qty = task.params.get("keep_qty") or {}

        # BEFORE the gather this also CLEARS: anything that is not a material or a supply is
        # sold outright (user, 2026-08-26 — "if it is not a material we should just sell
        # them"). Space is what gathering needs, and a shorter cargo list is also what keeps
        # the bought good's tile on the first page where the buy loop can see it.
        if task.params.get("clear"):
            from actions.sell_goods import barter_materials_exclude
            keep = barter_materials_exclude(task.params.get("good", ""))
            freed = run_goal(FreeHold(tuple(keep)))
            logger.info(f"[mission.sell_surplus] pre-gather clear: "
                        f"{dict(freed.observed) if freed else 'could not reach the market'}")
        if not keep_qty:
            return {"ok": True, "reason": "no material targets to trim against"}
        # NO PORT GATE. Everything this node does happens INSIDE THE MARKET (user,
        # 2026-08-27), so the port name is decoration — a log line and a field in the
        # result — and it gated nothing.
        #
        # It cost a mission anyway: `_current_port()` answers only on the port overworld,
        # the pre-gather clear had already walked into the market, and this aborted the
        # whole run from inside Barcelona having just successfully sold the surplus. A
        # precondition that is not a precondition can still fail, and it fails for reasons
        # that have nothing to do with the work.
        #
        # The name is still WANTED for the log, so it is looked up and allowed to be absent.
        # `owned_state` holds it under PLACE — which survives entering a building and dies
        # when the fleet puts to sea — so it is usually there.
        port = _current_port() or owned_state.recall("port") or ""
        result = run_goal(TrimHold(dict(keep_qty)))
        if result is None:
            return {"ok": False,
                    "reason": f"could not reach the market{f' at {port}' if port else ''}"}
        _exit_market_to_overworld()
        observed = dict(result.observed)
        logger.info(f"[mission.sell_surplus] {port or 'this port'}: "
                    f"{observed.get('stopped_because')}")
        return {"ok": bool(result.ok), "reason": observed.get("stopped_because") or "",
                "trimmed": observed.get("trimmed"), "port": port}

    @_facade
    def supply_verify(task) -> dict:
        """Confirm the fleet is somewhere it can supply — NOT a days-of-supply gate.

        Supply is loaded as part of Supply Departure, so **0 supply in port is normal**
        and gating on it here would abort a perfectly good mission (user 2026-08-21; the
        earlier version did exactly that, having read 0/0 at Diu).  Days-of-supply is only
        meaningful AT SEA, where the HUD reports it — that check rides on the village leg
        itself (`sail_to_village` passes a round-trip floor to the at-sea watch, which
        diverts to resupply if it ever drops short).

        So this node answers the one question that IS answerable in port: can this leg be
        supplied at all?  A port can; being adrift or in a building cannot."""
        from actions.sail_actions import where_am_i
        from brain.supply_planner import VILLAGE_LEG_RESERVE_DAYS
        min_days = task.params.get("min_days", VILLAGE_LEG_RESERVE_DAYS)
        loc = (where_am_i() or {}).get("location")
        if loc in ("sea", "sea_cinematic"):
            from actions.fleet_status import verify_supply_for_leg
            res = verify_supply_for_leg(min_days)
            logger.info(f"[mission.supply_verify] at sea — {res['reason']}")
            return res
        if loc == "port_overworld":
            logger.info(f"[mission.supply_verify] at port — supplies load at departure; "
                        f"the {min_days}d floor is enforced at sea on the village leg")
            return {"ok": True, "reason": "at port — supplies load at Supply Departure"}
        return {"ok": False, "reason": f"cannot confirm supply readiness from {loc!r} — "
                                       "expected a port or open sea"}

    @_facade
    def sail_route(task) -> dict:
        """Tail leg via a pre-planned in-game ROUTE (Route tab → Move). The route
        auto-resupplies at its port waypoints, so the only supply question is whether
        the fleet covers the LONGEST leg (user: routes are planned to ≤ 6 days)."""
        from actions.route_execution import execute_route
        from brain.supply_planner import ROUTE_LEG_DAYS
        start = execute_route(task.params["route"], longest_leg_days=int(ROUTE_LEG_DAYS))
        if not start.ok:
            return {"ok": False, "reason": start.reason}
        # THE DISPATCHER SAILS IT, not a poll loop. `_await_route_arrival` slept and read
        # `_current_port()` — a FIELD READ, not a perceive — so it received neither the
        # interruptor pass that dismisses a daily-news popup nor `IdleLockActivity`. Live
        # 2026-08-28 it would have polled four hours at London past an idle lock that
        # DISPLAYED the port name it was waiting for.
        #
        # Now: tick, perceive, `SeaActivity` reports WORKING and how long to wait, and the
        # leg ends when perceive stops saying `sea` — arrival is a context change to notice,
        # not an event to wait for (docs/activity_as_context.md §11).
        # THE ROUTE IS THE DESTINATION. If the ship turns out not to be moving, the course
        # is re-set by selecting the same route on the Route tab — `ChooseDestination` with
        # kind='route' — not by picking a port.
        return _sail_until_ashore(f"route {task.params['route']!r}",
                                  destination=task.params["route"], kind="route")

    @_facade
    def sell(task) -> dict:
        """FIRST NODE MIGRATED TO THE DISPATCHER (2026-08-26).

        It used to navigate: `_enter_market_at(port)` — perceive, tap, reorient, retry — and
        then sell. That navigation is a TRANSITION written by hand at the task layer, which is
        why it had to be patched into four call sites at once, and why a tail that "knew" it
        was in a village pressed Back at a port overworld and raised "Exit Game?".

        Now it states a goal and hands it to the loop. `SellHold(exclude=...)` mentions no
        tab, no tile and no port: the dispatcher works out that the market is a building to be
        entered, the market activity does the selling, and the answer comes back in task
        words. See docs/architecture_DRAFT.md, "Worked example: splitting `barter`".
        """
        from actions.sell_goods import barter_materials_exclude
        from brain.activities.market import SellHold
        from brain.run_goal import run_goal

        # A route tail ends wherever the route ends — resolve the port live rather than
        # planning it (a port_overworld ALWAYS has a name; unreadable = anomaly, not None).
        port = task.params.get("sell_port") or _current_port()
        if not port:
            return {"ok": False, "reason": "arrival port unreadable — cannot sell here"}

        keep = barter_materials_exclude(task.params["good"])
        result = run_goal(SellHold(tuple(keep)))
        if result is None:
            return {"ok": False, "port": port,
                    "reason": f"could not sell at {port} — the goal did not finish"}

        sold = list(result.observed.get("sold") or [])
        _exit_market_to_overworld()   # clean hand-off: leave port_overworld for the next leg
        if result.ok:
            mission_progress.advance("done")
        return {"ok": bool(result.ok), "port": port, "sold": sold,
                "reason": result.observed.get("stopped_because") or result.detail}

    return {"gather": gather, "sail_to_village": sail_to_village, "barter": barter,
            "sell_surplus": sell_surplus, "supply_verify": supply_verify,
            "sail_route": sail_route, "sail_to_sell": sail_to_sell, "sell": sell}


@_facade
def orders_already_held(orders: Mapping[str, int], port: str = "") -> bool:
    """True when the hold already satisfies `orders`, so the leg can be skipped.

    Answered HERE, before sailing. `buy_to_goal` also refuses to buy what we own, but only
    discovers it at the destination market — so the fleet crosses, walks to the Market,
    reads the Sell tab, learns it needs nothing, and leaves (live 2026-08-22: with every
    material already aboard the mission still set course for Kolkata).

    Conservative by construction: an unreadable hold, or being away from a market, returns
    False. "Unknown" must never mean "satisfied" — a leg skipped on a failed read arrives
    at the village empty-handed.
    """
    if not orders:
        return True
    if not _at_a_market_port():
        return False
    owned = _owned_here()
    if owned is None:
        return False
    have = {m: owned.get(m.lower(), 0) for m in orders}
    if all(have[m] >= q for m, q in orders.items()):
        logger.info(f"[gather] {port or 'port'}: already hold {dict(orders)} "
                    f"(own {have}) — skipping the leg")
        return True
    return False


@_facade
def _at_a_market_port() -> bool:
    """True when the fleet is at a port whose Market we could read right now."""
    try:
        from actions.sail_actions import where_am_i
        return where_am_i().get("location") in ("port_overworld", "building", "sub_menu")
    except Exception:
        return False


@_facade
def _owned_here() -> Optional[dict]:
    """{good: units} from the Market's Sell tab, or None if it cannot be read.

    None means "unknown", never "nothing" — a caller that skipped a gather leg on a failed
    read would arrive at the village empty-handed.
    """
    try:
        from actions.buy_materials import _read_owned_via_sell
        from capture.adb_capture import capture_screen
        from actions.adb_actions import tap
        if not _enter_market_at(_current_port() or "this port")["ok"]:
            return None
        owned = _read_owned_via_sell(capture_screen, tap, 1.2) or None
        # PUT THE FLEET BACK. This function reads like a question — `orders_already_held()
        # -> bool` — but it walks into the Market to answer, three calls below the plan.
        # Nothing above knows the fleet moved, so live 2026-08-23 the third hold check left
        # it on the Market greeting page and `sell_surplus` aborted with "not at a port".
        #
        # This restores the position because the GRAPH path has no way to declare where a
        # step must run. The real fix is a step that declares `needs_state` and lets the
        # loop navigate (see brain/barter_task.py) — then navigation stops being a side
        # effect of a predicate. See docs/one_loop_task_drives_state.md.
        try:
            _exit_market_to_overworld()
        except Exception as exc:
            logger.debug(f"[gather] could not leave the market after reading the hold: {exc}")
        return owned
    except Exception as exc:
        logger.debug(f"[gather] could not read the hold here: {exc}")
        return None


@_facade
def clear_surplus_at_current_port(good: str, trim_to: Optional[Mapping[str, int]] = None) -> dict:
    """Free hold space: sell everything that is NOT a barter material or a supply, and —
    when `trim_to` is given — cut over-stocked MATERIALS back to those quantities.

    `sell_goods(goal="clear")` sells regardless of profit; that is the point (space for the
    run), and why the whole step is opt-in: it can dump cargo the player was carrying to
    sell somewhere better.

    `trim_to` = {material: units to keep}. Without it this step cannot help a hold that is
    full of MATERIALS, because materials are exactly what it keeps. Live 2026-08-21: an
    over-buy left 1,681 Ebony against a need of 350, the hold at 3823/4108, and every
    following mission died at "plan: 0 round(s) (limited by space)". Clearing ran and
    reported "nothing to sell" — correctly, since the surplus WAS the material. Trimming is
    the only thing that frees that space.

    Runs BEFORE the gather so the freed space is real before buy targets are sized —
    `buy_to_goal` stops early with "sell surplus to free space" when the hold is full."""
    from actions.sell_goods import sell_goods, sell_down_to, barter_materials_exclude
    # This one DOES need the name — `_enter_market_at` walks to that port's market. But
    # `_current_port()` answers only on the overworld, so standing in a building made it
    # refuse: live 2026-08-26 a --clear-surplus run failed here from inside Barcelona's
    # market. PLACE survives entering a building, so ask what is remembered before giving up.
    port = _current_port() or owned_state.recall("port")
    if not port:
        return {"ok": False, "reason": "cannot tell which port this is — cannot sell surplus"}
    entered = _enter_market_at(port)
    if not entered["ok"]:
        return entered
    keep = barter_materials_exclude(good)
    logger.info(f"[clear_surplus] {port}: selling all non-kept cargo (keeping {keep})")
    res = sell_goods(port, goal="clear", keep=keep)

    trimmed, tr_owned = None, None
    if trim_to:
        logger.info(f"[clear_surplus] {port}: trimming over-stocked materials to {dict(trim_to)}")
        tr = sell_down_to(port, keep=trim_to)
        trimmed, tr_owned = tr.get("trimmed"), tr.get("owned")
        logger.info(f"[clear_surplus] {port}: trimmed {trimmed} "
                    f"({tr.get('reason') or 'ok'})")
        # Trimming is what frees space when the hold is all materials, so a successful trim
        # makes this step a success even if there was no whole good to dispose of.
        if tr.get("ok") and trimmed:
            res = {"ok": True, "sold": res.get("sold"),
                   "reason": f"trimmed {trimmed}"}

    # Hand back the measured hold, so the planner can buy only the shortfall instead of
    # re-checking each gather port.
    #
    # Taken from the trim's OWN grid read. The Sell page's centre grid is the hold, so the
    # counts were already on screen; re-reading them meant navigating back to that page,
    # and the label search matched the page TITLE "Sell" beside the back arrow — live
    # 2026-08-22 that tap left the market entirely and the hold came back unknown, after
    # the very same grid had been read correctly twice seconds earlier.
    owned = (tr_owned or None) if trim_to else None
    if owned is None:
        try:
            from actions.buy_materials import _read_owned_via_sell
            from capture.adb_capture import capture_screen
            from actions.adb_actions import tap as _tap
            owned = _read_owned_via_sell(capture_screen, _tap, 1.2) or None
        except Exception as exc:
            logger.debug(f"[clear_surplus] could not measure the hold afterwards: {exc}")

    _exit_market_to_overworld()
    return {"ok": bool(res.get("ok")), "port": port, "sold": res.get("sold"),
            "trimmed": trimmed, "owned": owned, "reason": res.get("reason", "")}


# THE BARTER PANEL LIVES IN `actions/barter_panel.py`.
#
# `_no_panel_failure`, `_open_barter_panel`, `refresh_stale_panel`, `_exchange_still_live`,
# `_select_trade_good`, `_read_panel_state` and their tile helpers captured screens, parsed
# them and tapped — UI, in a task module. Import them from there.
#
# They were briefly re-exported here so callers could be moved one at a time, and that
# re-export was a TRAP: `from brain.barter_mission_live import _read_panel_state` binds the
# original function object, so patching `actions.barter_panel._read_panel_state` did not reach
# it. `VillageActivity._panel()` went on calling the real reader — a live screen capture — in
# a test that had stubbed the panel, and the stale value it returned failed a test that passed
# alone. A name is resolved where it is bound, not where it is written.



@_facade
def _current_port(tries: int = 3) -> Optional[str]:
    """The port we are standing in, re-read a few times (OmniParser is non-deterministic
    and a port_overworld always has a name — see the never-act-blind rule)."""
    import time as _t
    from actions.sail_actions import where_am_i
    from capture.adb_capture import capture_screen
    for _ in range(tries):
        loc = where_am_i(capture_screen())
        if loc.get("location") == "port_overworld" and loc.get("port"):
            return loc["port"]
        _t.sleep(1.0)
    return None


@_facade
def _sail_to(destination: str, *, kind: str = "port", resupply: bool = True,
             min_supply_days: float = None, max_ticks: int = 240) -> dict:
    """One sail leg, stated as work orders and driven by the dispatcher.

    Replaces `drive_sail_to`, which drove `SailToGoal` — eight phases that walked to the
    harbour, opened the world map, picked the destination and then POLLED FOR ARRIVAL in a
    SAILING phase of its own. That poll did not perceive through the dispatcher, so it got
    neither the interruptor pass that dismisses a daily-news popup nor `IdleLockActivity`:
    the same failure `_await_route_arrival` was deleted for, still live on every port and
    village leg until now.
    """
    from brain.run_goal import run_task
    from brain.sail_runner import ARRIVED, SailRunner

    runner = run_task(SailRunner(destination=destination, kind=kind, resupply=resupply,
                                 min_supply_days=min_supply_days),
                      max_ticks=max_ticks)
    if runner.status == ARRIVED:
        # ARRIVED SOMEWHERE IS NOT ARRIVED THERE. The runner takes a confirmed departure as
        # committed, which it must — the cinematic is not the sea yet — but that is a BELIEF,
        # and a departure that silently did not take would leave the fleet ashore at the
        # origin with the runner reporting arrival. Checking the name costs nothing and is
        # the only thing standing between that belief and a gather leg buying at the wrong
        # port.
        from brain.sail_runner import _same_place

        # A CARRIED NAME CANNOT DISPROVE AN ARRIVAL. `state.port` is painted only by a
        # settlement overworld. On the sea/arrival frame it still holds the ORIGIN, and a
        # VILLAGE never sets it at all — measured live 2026-08-30 at Hutu Village, where
        # perceive reports state='village', port=None.
        #
        # That night the fleet sailed Luanda -> Hutu Village, arrived, and this read the
        # carried 'Luanda' off the arrival frame, concluded "the departure did not take", and
        # ended the mission standing in the village it had been sent to, with six barter
        # rounds open and both materials aboard.
        #
        # The guard's real case is port-to-port: a departure that silently failed leaves the
        # fleet on the ORIGIN's overworld, which does paint its name, and that is worth
        # catching. A PORT name simply has nothing to say about arriving at a VILLAGE.
        if runner.port and not _same_place(runner.port, destination):
            # THE CALLER ALREADY KNOWS WHICH IT IS — `kind` says so, and the village leg
            # passes it. Deriving it from the NAME meant reaching into `vision` from the task
            # layer, which is exactly the import the layering rule forbids, to re-answer a
            # question the work order had already answered.
            if (kind or "").lower() == "village":
                logger.info(f"[mission.sail] {runner.port!r} is a port name and the leg was "
                            f"for the village {destination!r} — a village interior paints no "
                            f"name, so that reading was carried from before, not read here; "
                            f"taking the arrival")
            else:
                reason = (f"reported arrival at {runner.port!r} but the leg was for "
                          f"{destination!r} — the departure did not take")
                logger.warning(f"[mission.sail] {reason}")
                return {"ok": False, "reason": reason}
        logger.info(f"[mission.sail] arrived at {runner.port or destination}")
        return {"ok": True, "reason": f"arrived at {runner.port or destination}",
                "port": runner.port}
    reason = runner.reason or f"did not reach {destination}"
    logger.warning(f"[mission.sail] {reason}")
    return {"ok": False, "reason": reason}


@_facade
def _sail_until_ashore(what: str, *, destination: str = None, kind: str = "port",
                       max_ticks: int = 200) -> dict:
    """Tick until the fleet is no longer at sea. No loop of our own beyond the tick.

    `run_task` does the ticking: each tick PERCEIVES (so interruptors are dismissed and the
    idle lock is cleared by the activity that owns it), `SeaActivity` looks at the HUD and
    reports whether the ETA is still falling, and the leg ends when the sea stops matching.

    `destination` is what to re-select if the ship turns out NOT to be moving — a departure
    that never took, or a Move that was swallowed. Without one the leg can only report being
    adrift; with one, `VoyageRunner` asks for the course to be set again and the dispatcher
    opens the world map to do it.
    """
    from brain.run_goal import run_task
    from brain.voyage_runner import ARRIVED, VoyageRunner

    runner = run_task(VoyageRunner(what=what, destination=destination, kind=kind),
                      max_ticks=max_ticks)
    if runner.status == ARRIVED:
        port = runner.port
        logger.info(f"[mission.sail] {what} arrived{f' at {port}' if port else ''}")
        return {"ok": True, "reason": f"{what} arrived{f' at {port}' if port else ''}",
                "port": port}
    reason = runner.reason or f"{what} did not reach a port"
    logger.warning(f"[mission.sail] {reason}")
    return {"ok": False, "reason": reason}


# DEAD as of 2026-08-28 — no callers. Kept for one commit so the diff shows what the sea
# activity replaced; delete on the next pass.
#
# It is the sub-loop that does not perceive: `_current_port(tries=1)` and `read_sea_hud()`
# READ FIELDS. So it received neither the interruptor pass that dismisses a daily-news popup
# nor `IdleLockActivity`, and at London on 2026-08-28 it would have polled for four hours
# past an idle lock that DISPLAYED the port name it was waiting for.
def _await_route_arrival(start, route_name: str, timeout_s: float = 4 * 3600) -> dict:
    """Sleep-and-check until the route lands us in a port.

    Cadence follows the supply the fleet actually has (a game day is ~1.5 real minutes):
    a fleet with 5 days aboard is re-checked in ~6 minutes, not every few seconds.  Never leaves the fleet unattended-but-unbounded — it gives up with a
    structured failure so the mission layer decides."""
    import random
    import time as _t
    from actions.sail_actions import read_sea_hud
    from brain.supply_planner import supply_checkback_seconds

    deadline = _t.monotonic() + timeout_s
    wait = max(60.0, float(start.checkback_s or 300.0))
    while _t.monotonic() < deadline:
        _t.sleep(wait * random.uniform(0.9, 1.1))          # jitter: never a fixed cadence
        port = _current_port(tries=1)
        if port:
            logger.info(f"[mission.sail_route] route {route_name!r} arrived at {port}")
            return {"ok": True, "reason": f"route arrived at {port}", "port": port}
        hud = read_sea_hud()
        days = hud.get("supply_days")
        eta = hud.get("eta_days")
        logger.info(f"[mission.sail_route] still sailing {route_name!r} "
                    f"(supply={days}d eta={eta}d)")
        wait = supply_checkback_seconds(days) if days is not None else max(60.0, wait)
    return {"ok": False, "reason": f"route {route_name!r} did not arrive within "
                                   f"{timeout_s / 3600:.1f}h"}


def run_default_barter_mission(good: str = "Box of Nutmeg",
                               village: str = "Melanesian Village",
                               sell_port: str = "Jakarta", rounds: int = 6):
    """Run a barter mission via the opportunity-driven plumbing with the DEFAULT
    opportunity (event=None → seasonal barter, sell at sell_port, time ignored).
    Plans → builds the sub-task graph → runs the dynamic scheduler with live executors
    + out-of-stock reroute recovery. Returns a brain.mission.MissionResult."""
    from brain.mission import (default_opportunity, build_barter_graph, run_mission,
                               make_barter_recover)
    opp = default_opportunity(good=good, village=village, sell_port=sell_port, rounds=rounds)
    if opp.recipe is None:
        return run_mission([], {}, {}, start=(0.0, 0.0))  # no recipe → empty (ok=True, nothing to do)
    coords = catalogue_coords()
    start = current_position(coords)
    plan = plan_barter_task(opp.recipe, village, sell_port, rounds, coords, start)
    logger.info(f"[mission] plan: needs={plan.needs} purchases={plan.purchases} "
                f"unsourced={plan.unsourced}")
    if plan.unsourced:
        from brain.mission import MissionResult
        return MissionResult(ok=False, reason=f"unsourced materials {plan.unsourced} "
                             "— read source pins first")
    graph = build_barter_graph(opp, plan)
    execs = make_live_executors(opp)
    return run_mission(graph, coords, execs, start=start,
                       recover=make_barter_recover(opp.recipe))
