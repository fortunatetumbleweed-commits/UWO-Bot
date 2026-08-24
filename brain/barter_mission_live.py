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
from brain import mission_progress
from brain.gathering_solver import plan_gathering, assign_purchases


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

    def gather(task) -> dict:
        import time as _t
        from actions.sail_actions import navigate_to_building
        from actions.adb_actions import tap
        from actions.market_actions import MARKET_COORDS
        from actions.buy_materials import buy_to_goal
        from brain.goals.sail_to import drive_sail_to
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

        sail = drive_sail_to(port)
        if not sail.get("ok"):
            return {"ok": False, "reason": f"sail to {port}: {sail.get('reason')}"}
        if not navigate_to_building("Market"):
            return {"ok": False, "reason": f"could not reach Market at {port}"}
        tap(*MARKET_COORDS["purchase"])          # show the goods grid (market opens on the greeting)
        _t.sleep(2.0)
        # Buy toward the goal, refreshing the market with blue gems if a material sells
        # out before the target is met (rather than waiting ~20 min for the timer).
        # Scale the round bound to the goal (each round buys ~one shelf or refreshes); the
        # loop stops early once the goal is met, so this is only a backstop. No gem-budget
        # guard (user 2026-08-18: "just raise the bound"); the cap keeps a tiny-stock /
        # huge-goal case from burning unbounded blue gems.
        goal_total = sum(orders.values())
        max_rounds = min(60, max(4, -(-goal_total // 20)))
        res = buy_to_goal(port, orders, max_rounds=max_rounds)
        # buy_to_goal returns "already own ... >= goal" as a VALUE; without this the log
        # showed a market visit that bought nothing and no reason why.
        if res.get("met") and not res.get("rounds"):
            logger.info(f"[gather] {port}: {res.get('reason')}")
        _exit_market_to_overworld()   # clean hand-off: leave port_overworld for the next leg
        return {"ok": bool(res.get("ok")), "reason": res.get("reason", ""), **res}

    def sail_to_village(task) -> dict:
        # Sailing to a village is the SAME operation as sailing to a port — SailToGoal
        # dispatches Explore-tab village selection and its arrival check accepts the
        # 'village' state (user 2026-08-17: "no difference except the arrival check").
        # The difference that matters is SUPPLY: a village has no harbour, so this leg
        # carries a round-trip floor and the at-sea watch enforces it the whole way.
        from brain.goals.sail_to import drive_sail_to
        from brain.supply_planner import VILLAGE_LEG_RESERVE_DAYS
        # Committed. From here the materials aboard are the materials we barter with, and
        # re-checking them can only cost us the position we are sailing to.
        # Gathering is over by construction — the graph runs gathers and the surplus
        # clear before this node. From here on, no cargo checks.
        mission_progress.advance("bartering")
        return drive_sail_to(task.params["village"],
                             min_supply_days=task.params.get("min_days",
                                                             VILLAGE_LEG_RESERVE_DAYS))

    def barter(task) -> dict:
        """BARTER at the village, sized by the PANEL, not by the pre-sail plan.

        The plan was built from a remote check that is hours old by the time the fleet
        arrives, and the cargo may not have survived the voyage (2026-08-20: a fleet death
        took 75% of the materials and nothing noticed until afterwards, because this loop
        read the panel and threw the reading away).  The panel's X/Y is ground truth, and
        it BOUNDS the round target — the per-round gate alone would keep attempting rounds
        the hold cannot fund whenever a read comes back stale."""
        from brain.barter_mission import run_barter_phase
        from actions.barter_executor import barter_commit_verified

        planned = task.params["rounds"]
        arrival = _read_panel_state()
        seen = {"on_arrival": None, "shortfall": None, "partial_left": None}
        target = planned

        if arrival is None:
            # OPEN THE BARTER SUB-MENU FIRST. `barter_commit_verified` acts on an already-open
            # panel — it does not navigate — so arriving at the village INTERIOR and going
            # straight to a commit hunts for a positive button on a screen that has none.
            # Live 2026-08-22: the fleet reached Melanesian Village, perceive reported the
            # left menu verbatim as ['barter', 'explore', 'gifting', 'loot', 'recruit crew'],
            # and the mission aborted with "no positive button found — settled after 0 tap(s)"
            # twice without ever tapping the 'barter' item it had just read.
            panel_open = _open_barter_panel()
            if panel_open == "unavailable":
                # The day's rounds are spent. Finish the phase so the tail can run.
                mission_progress.advance("sailing_route")
                return {"ok": True, "exhausted": True, "committed": 0,
                        "reason": "the village's barters for today are used up",
                        "planned_rounds": planned, "attempted_rounds": 0}
            if panel_open:
                arrival = _read_panel_state()
                # The panel opens with NOTHING selected — it reads "Select Trade Good." and
                # the tiles carry no names, only a thumbnail, a stock status and a category.
                # Live 2026-08-23 that came back as good=None, materials=[], which is not a
                # failure to open: it is a selection still to make.
                if arrival is None or not getattr(arrival, "materials", None):
                    good = task.params.get("good") or ""
                    prog = mission_progress.current() or {}
                    recipe = ((prog.get("recipe") or {}).get("materials")
                              if isinstance(prog.get("recipe"), dict) else None)
                    if _select_trade_good(good, recipe):
                        arrival = _read_panel_state()
            if arrival is None:
                # STOP. Not knowing where we are is a reason to re-establish position, never
                # a reason to start tapping: `barter_commit_verified` assumes an open panel,
                # so committing from an unidentified screen taps whatever happens to look
                # positive on it. The screen is ground truth — report what it actually shows
                # and let the caller correct itself, rather than acting out a state we only
                # believe we are in.
                fail = _no_panel_failure()
                if panel_open:      # the panel IS open — say what actually went wrong
                    fail = {**fail, "reason": f"the Barter panel is open but {task.params.get('good')!r} "
                                              "could not be selected from the tradable goods"}
                    logger.warning(f"[mission.barter] {fail['reason']}")
                return {**fail, "planned_rounds": planned, "attempted_rounds": 0}
        else:
            seen["on_arrival"] = dict(arrival.materials)
            seen["partial_left"] = arrival.partial_fraction
            # THE PANEL BOUNDS THE ROUNDS, NOT THE PRE-SAIL PLAN. Keep bartering while the
            # materials fund a round — the per-round gate below stops on a full hold or on
            # consumption (user, 2026-08-23: "should continue until the ship is fully loaded
            # or the materials are all gone").
            #
            # `planned` is computed before sailing and is systematically too small: it prices
            # each round at the PEAK footprint, while a round actually FREES space — live
            # 2026-08-23, 648 units of materials left and 497 of product arrived, a net -151.
            # So the plan said 1 round where the materials funded 3, and the mission stopped
            # with two rounds' worth of cargo still aboard.
            # BOTH bounds are real, so take the smaller: the PANEL bounds by materials
            # actually aboard, and the PLAN bounds by cargo space and the daily barter limit.
            #
            # Neither may be dropped. `max()` re-opened the 2026-08-20 fleet-death case — a
            # stale plan of 6 against materials for 1 would attempt 6. Ignoring the plan
            # instead would over-commit past the space and daily limits it encodes.
            #
            # The "only 1 round when 3 were funded" problem (2026-08-23) was never this
            # line: `planned` had been computed from an UNKNOWN hold and came out too small.
            # That is fixed where it belongs — `_try_barter_here` sizes the rounds from the
            # panel when the fleet is already at the village.
            target = min(int(planned), int(arrival.rounds_remaining))
            logger.info(f"[mission.barter] panel: {arrival.materials} → "
                        f"{arrival.rounds_remaining} full round(s) fundable "
                        f"(planned {planned}) → committing {target}")
            if arrival.rounds_remaining < planned:
                seen["shortfall"] = {"planned": planned,
                                     "fundable": arrival.rounds_remaining,
                                     "binding": arrival.binding,
                                     "short_by": arrival.shortfall}
                logger.warning(
                    f"[mission.barter] MATERIAL SHORTFALL — planned {planned} round(s), "
                    f"the hold funds {arrival.rounds_remaining}. Limited by "
                    f"{arrival.binding!r}; short {arrival.shortfall} for one more round. "
                    "Cargo does not match the plan (a loss en route, or the ratio moved "
                    "since the remote check).")
            if arrival.rounds_remaining == 0 and arrival.partial_fraction > 0:
                logger.info(f"[mission.barter] {arrival.partial_fraction:.1%} of a further "
                            "round is fundable — not spending a daily round on a partial")

        def read_state():
            """Per-round gate: re-read the panel so consumption stops the loop on ground
            truth rather than on the round counter.  `overflow` reports the units the
            game is holding PENDING because the hold is full — non-zero hands control to
            `jettison_fn`, which must clear it before the goods are discarded."""
            from actions.overflow_dialog import read_overflow
            from capture.adb_capture import capture_screen
            from vision.omniparser import parse_fast_cached
            overflow = 0
            try:
                ov = read_overflow(parse_fast_cached(capture_screen()))
                if ov is not None and ov.pending:
                    overflow = int(ov.pending)
                    logger.warning(f"[mission.barter] OVERFLOW — {overflow} unit(s) pending, "
                                   "the hold is full; clearing before they are discarded")
            except Exception as exc:
                logger.debug(f"[mission.barter] overflow probe skipped: {exc}")
            state = _read_panel_state()
            if state is None:
                return {"rounds_remaining": 1, "overflow": overflow}
            seen["partial_left"] = state.partial_fraction
            return {"rounds_remaining": state.rounds_remaining, "overflow": overflow}

        def jettison(overflow_units) -> dict:
            """Clear the overflow dialog: dump the cheapest cargo, keep the supply
            reserve, never dump the output, then Receive.  Dismissing the dialog would
            LOSE the pending goods, so this is not optional once it is up."""
            from actions.overflow_dialog import clear_overflow
            from brain.supply_planner import supply_needed_each, VILLAGE_LEG_RESERVE_DAYS
            reserve = supply_needed_each(VILLAGE_LEG_RESERVE_DAYS)
            res = clear_overflow(output_good=task.params.get("good", ""),
                                 reserves={"water": reserve, "food": reserve})
            seen["overflow_cleared"] = res
            if res.get("sacrificed"):
                logger.warning(f"[mission.barter] {res['sacrificed']} unit(s) of "
                               f"{task.params.get('good')} given up — the supply reserve "
                               "could not be preserved any other way")
            logger.info(f"[mission.barter] overflow: {res.get('reason')}")
            return res

        play = type("P", (), {"rounds": target})()
        res = run_barter_phase(play, read_state_fn=read_state,
                               commit_fn=barter_commit_verified,
                               jettison_fn=jettison)
        # A STALL WITH NOTHING LEFT TO BARTER IS COMPLETION, NOT FAILURE.
        #
        # `run_barter_phase` reports ok=False when a commit produces no amity/cargo change —
        # correct when something went wrong, wrong when the game simply refused because the
        # materials are spent. Live 2026-08-23 the fleet bartered until Coral hit 2 against a
        # need of 3, the last tap changed nothing, and the mission FAILED — so the tail never
        # ran and the fleet sat in the village with its cargo.
        #
        # The panel distinguishes the two: if no further barter is affordable, the phase is
        # done. Only a stall with a barter still available is a real failure.
        #
        # It must also have COMMITTED something. Arriving unable to barter at all is not
        # completion — the materials never made it, and that is worth reporting.
        mission_progress.record_rounds(int(res.get("committed") or 0))

        # "Committed" spans the MISSION, not this run. A later run finds the same empty panel
        # whether the materials were spent by earlier rounds or never arrived at all — and
        # only the mission's running total tells them apart. Live 2026-08-23 a mission that
        # had bartered three times reported "bartered 0 round(s)" on the run that found
        # nothing left, failed, and never took its route.
        _bartered = (int(res.get("committed") or 0) >= 1
                     or mission_progress.committed_total() >= 1)
        if not res.get("ok") and _bartered and not _exchange_still_live():
            done_state = _read_panel_state()
            short = getattr(done_state, "shortfall", None) if done_state else None
            logger.info("[mission.barter] the commit stalled and Exchange is greyed — the "
                        f"materials are spent (short {short}); treating the bartering as "
                        "FINISHED rather than failed")
            res = {**res, "ok": True, "exhausted": True,
                   "reason": "bartered until the materials ran out"}

        if res.get("ok"):
            # "A round succeeded" is NOT "the bartering is finished". Advance to the tail only
            # when the panel says no further FULL round is fundable — otherwise the mission
            # sails away with materials still aboard. Live 2026-08-23 it committed one round,
            # advanced the phase, and left for London with 2-3 rounds' worth unspent (user).
            #
            # Re-read rather than reason from the plan: amity tiers and the stock refresh move
            # the ratio, so what is fundable AFTER the rounds is not derivable from before.
            after = _read_panel_state()
            more = int(getattr(after, "rounds_remaining", 0) or 0) if after else 0
            # THE GAME'S OWN ANSWER OUTRANKS OURS. A live (yellow) Exchange button means the
            # game will accept another barter — it has already applied every rule we would be
            # re-deriving: materials, the daily count, the stock state. Live 2026-08-23 the
            # bot did 2 rounds and stopped while Exchange was still enabled (user), so the
            # material arithmetic disagreed with the game and the game was right.
            if _exchange_still_live():
                logger.info("[mission.barter] Exchange is still live — the game will accept "
                            "another barter, whatever the material maths says")
                more = max(more, 1)
            if more >= 1:
                logger.info(f"[mission.barter] {more} more full round(s) still fundable — "
                            "staying in the BARTERING phase; the tail can wait")
            else:
                logger.info("[mission.barter] no further full round is fundable — the "
                            "bartering is finished; moving to the tail")
                mission_progress.advance("sailing_route")
            res = {**res, "more_rounds_fundable": more}
        return {**res, "planned_rounds": planned, "attempted_rounds": target,
                "panel_on_arrival": seen["on_arrival"],
                "material_shortfall": seen["shortfall"],
                "partial_round_left": seen["partial_left"],
                "overflow_cleared": seen.get("overflow_cleared")}

    def sail_to_sell(task) -> dict:
        from brain.goals.sail_to import drive_sail_to
        return drive_sail_to(task.params["sell_port"])

    def sell_surplus(task) -> dict:
        """Trim each material down to what the plan needs, before the village leg.

        `buy_to_goal` buys by the shelf, so the hold arrives with more than the plan asked
        for; the excess is dead weight the barter output then cannot fit. This trims only
        goods the plan NAMED (whole-good disposal of unrelated cargo is the separate,
        opt-in pre-gather clear) and sells nothing it could not verify — see
        `actions.sell_goods.sell_down_to`."""
        from actions.sail_actions import navigate_to_building
        from actions.sell_goods import sell_down_to
        keep_qty = task.params.get("keep_qty") or {}
        if not keep_qty:
            return {"ok": True, "reason": "no material targets to trim against"}
        port = _current_port()
        if not port:
            return {"ok": False, "reason": "not at a port — cannot trim surplus"}
        if not navigate_to_building("Market"):
            return {"ok": False, "reason": f"could not reach Market at {port}"}
        res = sell_down_to(port, keep_qty)
        _exit_market_to_overworld()
        logger.info(f"[mission.sell_surplus] {port}: {res.get('reason')}")
        return {"ok": bool(res.get("ok")), "reason": res.get("reason", ""),
                "trimmed": res.get("trimmed"), "port": port}

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

    def sail_route(task) -> dict:
        """Tail leg via a pre-planned in-game ROUTE (Route tab → Move). The route
        auto-resupplies at its port waypoints, so the only supply question is whether
        the fleet covers the LONGEST leg (user: routes are planned to ≤ 6 days)."""
        from actions.route_execution import execute_route
        from brain.supply_planner import ROUTE_LEG_DAYS
        start = execute_route(task.params["route"], longest_leg_days=int(ROUTE_LEG_DAYS))
        if not start.ok:
            return {"ok": False, "reason": start.reason}
        return _await_route_arrival(start, task.params["route"])

    def sell(task) -> dict:
        from actions.sail_actions import navigate_to_building
        from actions.sell_goods import sell_goods, barter_materials_exclude
        # A route tail ends wherever the route ends — resolve the port live rather than
        # planning it (a port_overworld ALWAYS has a name; unreadable = anomaly, not None).
        port = task.params.get("sell_port") or _current_port()
        if not port:
            return {"ok": False, "reason": "arrival port unreadable — cannot sell here"}
        if not navigate_to_building("Market"):
            return {"ok": False, "reason": f"could not reach Market at {port}"}
        res = sell_goods(port, exclude=barter_materials_exclude(task.params["good"]))
        _exit_market_to_overworld()   # clean hand-off: leave port_overworld for the next leg
        if res.get("ok"):
                mission_progress.advance("done")
        return {"ok": bool(res.get("ok")), "reason": res.get("reason", ""), "port": port, **res}

    return {"gather": gather, "sail_to_village": sail_to_village, "barter": barter,
            "sell_surplus": sell_surplus, "supply_verify": supply_verify,
            "sail_route": sail_route, "sail_to_sell": sail_to_sell, "sell": sell}


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


def _at_a_market_port() -> bool:
    """True when the fleet is at a port whose Market we could read right now."""
    try:
        from actions.sail_actions import where_am_i
        return where_am_i().get("location") in ("port_overworld", "building", "sub_menu")
    except Exception:
        return False


def _owned_here() -> Optional[dict]:
    """{good: units} from the Market's Sell tab, or None if it cannot be read.

    None means "unknown", never "nothing" — a caller that skipped a gather leg on a failed
    read would arrive at the village empty-handed.
    """
    try:
        from actions.sail_actions import navigate_to_building
        from actions.buy_materials import _read_owned_via_sell
        from capture.adb_capture import capture_screen
        from actions.adb_actions import tap
        if not navigate_to_building("Market"):
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
    from actions.sail_actions import navigate_to_building
    from actions.sell_goods import sell_goods, sell_down_to, barter_materials_exclude
    port = _current_port()
    if not port:
        return {"ok": False, "reason": "not at a port — cannot sell surplus here"}
    if not navigate_to_building("Market"):
        return {"ok": False, "reason": f"could not reach Market at {port}"}
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


def _no_panel_failure() -> dict:
    """Why we are refusing to barter, described by what the screen ACTUALLY shows.

    Reporting the perceived screen is the point: the caller's state is the thing that is
    wrong, and it can only correct itself if it is told what is really there.
    """
    from actions.sail_actions import where_am_i
    from actions.ui import active_submenu
    try:
        here = where_am_i()
        detail = here.get("detail") or here.get("location")
        submenu = active_submenu()
    except Exception as exc:
        detail, submenu = f"unreadable ({exc})", None
    logger.warning(f"[mission.barter] the Barter panel is not open and could not be opened — "
                   f"screen reads {detail!r} (sub-menu {submenu!r}). Not committing from here.")
    return {"ok": False, "reason": f"barter panel not open — screen shows {detail!r}",
            "screen": detail, "submenu": submenu}


def _open_barter_panel() -> bool:
    """Open the village's Barter sub-menu. True when the screen confirms we are on it.

    Goes through the LEFT MENU REGION, not a frame-wide label search. A chromed screen has a
    known layout (user, 2026-08-23): title top-left, the menu item list directly below it on
    the left, a centre panel, a right panel, and a top menu bar at the top right — except a
    VILLAGE, which has no top menu bar. Searching the whole frame for a word ignores that
    layout and picks up prose: live 2026-08-23 the village page carried "Barter" both as the
    menu item (x=183) and inside "Increase Barter Count by 3" in the Amity Effect panel
    (x=1052). The unconstrained search took the prose and the tap did nothing.

    `vision.region_detectors.left_menu` is the canonical reader for that region and also
    reports `is_locked` / `is_selected`, so this needs no geometry of its own.

    Confirmed by the TITLE, which in this game is always the sub-menu currently selected
    (`actions.ui.active_submenu`) — so "did the tap work?" is answered by reading, not
    assuming.
    """
    from actions import ui
    from actions.ui import on_submenu
    from capture.adb_capture import capture_screen
    from vision.omniparser import parse_fast_cached
    from vision.region_detectors.left_menu import detect_left_menu
    try:
        frame = capture_screen()
        if on_submenu("barter", frame):
            return True                      # already there — do not tap again

        menu = detect_left_menu(list(parse_fast_cached(frame)), frame.width, frame.height)
        item = menu.find("Barter") if menu else None
        if item is None:
            logger.warning(f"[mission.barter] no 'Barter' item in the left menu "
                           f"(menu reads {menu.labels() if menu else None})")
            return False
        if item.get("is_locked"):
            # NOT A FAILURE — this is the game saying the day's barters are used up. When the
            # last available round is spent the panel returns to the village top menu on its
            # own and the Barter item goes dark under a red "Unavailable" ribbon (user,
            # 2026-08-23). It means the ship should LEAVE, which is success, not an error.
            logger.info("[mission.barter] the Barter item is UNAVAILABLE — the day's barters "
                        "are used up; the bartering is finished and the fleet should leave")
            return "unavailable"

        ui.tap_element(item, why="village → Barter", dwell="dialog")
        opened = on_submenu("barter", capture_screen())
        logger.info(f"[mission.barter] Barter panel opened: {opened}")
        return opened
    except Exception as exc:
        logger.warning(f"[mission.barter] could not open the Barter panel: {exc}")
        return False


def _exchange_still_live() -> bool:
    """True when a LIVE (yellow) Exchange button is on screen.

    The game greys the button the moment another barter is impossible, so this is its own
    verdict on "can I barter again?" — ahead of any count we compute from the panel.
    """
    try:
        from capture.adb_capture import capture_screen
        from vision.omniparser import parse_fast_cached
        from brain.commit_actions import _yellow_commit_button
        frame = capture_screen()
        btn = _yellow_commit_button(parse_fast_cached(frame), frame, ["exchange"])
        return btn is not None
    except Exception as exc:
        logger.debug(f"[mission.barter] Exchange liveness check failed: {exc}")
        return False


def _select_trade_good(good: str, recipe: Optional[Mapping[str, int]] = None) -> bool:
    """Select `good` in the Tradable Trade Goods row. True when the panel confirms it.

    The tiles carry NO NAMES — only a thumbnail, a stock status and a category (user,
    2026-08-23). So the bot cannot search for "Box of Nutmeg": it taps a tile and READS BACK
    what the panel then shows, which is the verification the name would have given.

    Order matters only as an optimisation: the category is a strong hint (Box of Nutmeg is a
    spice), so a tile whose category matches is tried first, and the rest follow. Correctness
    comes from the read-back, never from the hint.

    A stock status of "Insufficient" or "Depleted" is NOT a reason to skip a tile — those
    reduce the YIELD, not the ability to trade (docs/game_mechanics.md).
    """
    from actions import ui
    from capture.adb_capture import capture_screen
    from vision.omniparser import parse_fast_cached

    frame = capture_screen()
    tiles = _tradable_tiles(parse_fast_cached(frame))
    if not tiles:
        logger.warning("[mission.barter] no tradable goods tiles found on the Barter panel")
        return False

    want = _category_hint(good)
    tiles.sort(key=lambda t: 0 if (want and t["category"] == want) else 1)
    logger.info(f"[mission.barter] {len(tiles)} tradable tile(s): "
                f"{[(t['category'], t['status']) for t in tiles]}"
                + (f" — trying {want!r} first" if want else ""))

    from actions.barter_reader import read_barter_panel
    for tile in tiles:
        ui.tap_at(tile["cx"], tile["cy"],
                  why=f"barter → select the {tile['category'] or 'unnamed'} good")
        # Verify against the RAW reading: it carries `selected_good` and each material's
        # label/need. `_read_panel_state` derives a PanelBarterState for the ROUND maths and
        # drops the good's name, so it cannot answer "is this the right good?".
        reading = read_barter_panel(capture_screen())
        if reading is None:
            continue
        if _panel_matches(reading, good, recipe):
            logger.info(f"[mission.barter] selected {good!r} via the "
                        f"{tile['category']!r} tile")
            return True
        logger.info(f"[mission.barter] the {tile['category']!r} tile is not {good!r} "
                    "— trying the next")
    logger.warning(f"[mission.barter] none of the tradable tiles is {good!r}")
    return False


def _tradable_tiles(elements) -> list:
    """The goods tiles: an icon with a CATEGORY label directly beneath it.

    Measured on the Melanesian Village barter panel: icons at cy≈424, status labels at
    cy≈509, category labels at cy≈552, in four columns at cx ≈ 424/560/693/828. The columns
    are found by pairing each category label with the icon above it, so nothing here is a
    fixed coordinate.
    """
    icons = [e for e in elements
             if getattr(e, "element_type", "") == "icon" and 350 < e.cy < 480]
    labels = [e for e in elements
              if (getattr(e, "label", "") or "").strip() and 530 < e.cy < 580]
    status = [e for e in elements
              if (getattr(e, "label", "") or "").strip().lower()
              in ("insufficient", "depleted", "sufficient", "abundant")]
    out = []
    for lab in labels:
        icon = min(icons, key=lambda e: abs(e.cx - lab.cx), default=None)
        if icon is None or abs(icon.cx - lab.cx) > 80:
            continue
        st = min(status, key=lambda e: abs(e.cx - lab.cx), default=None)
        out.append({"cx": icon.cx, "cy": icon.cy,
                    "category": (lab.label or "").strip(),
                    "status": (st.label or "").strip()
                              if st and abs(st.cx - lab.cx) <= 80 else ""})
    return out


# Output good -> the category its tile carries. A hint for ORDERING only; the panel read-back
# is what decides. Extend as goods are met.
_GOOD_CATEGORY = {"box of nutmeg": "Spices"}


def _category_hint(good: str) -> Optional[str]:
    return _GOOD_CATEGORY.get((good or "").strip().lower())


def _panel_matches(reading, good: str, recipe: Optional[Mapping[str, int]]) -> bool:
    """Is the selected good the one we want? By NAME when the panel gives one, else by the
    RECIPE — the materials and their per-round needs are a fingerprint we already hold."""
    name = (getattr(reading, "selected_good", None) or "").strip().lower()
    if name:
        return name == (good or "").strip().lower()
    mats = {(m.label or "").strip().lower(): m.need for m in getattr(reading, "materials", [])}
    if not mats or not recipe:
        return False
    return all(mats.get(k.lower()) == v for k, v in recipe.items())


def _read_panel_state():
    """The village barter panel as a `PanelBarterState`, or None if it isn't readable.

    ONE place converts the panel into rounds — the arrival bound and the per-round gate
    must not drift apart."""
    from brain.barter_quantity import panel_barter_state
    from actions.barter_reader import read_barter_panel
    from capture.adb_capture import capture_screen
    reading = read_barter_panel(capture_screen())
    if reading is None or not reading.materials:
        return None
    return panel_barter_state(reading.materials, reading.output_quantity or 0)


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
