# actions/task_runner.py
#
# Reads a YAML task file and executes the steps autonomously.
#
# Supported actions:
#   sell_all    — sell entire cargo at current port market
#   buy_all     — auto-buy recommended goods at current port market
#   sail_to     — sail to destination port (destination: <port>)
#
# Usage:
#   python run_task.py tasks/trade_ceylon_aceh.yaml
#   python run_task.py tasks/trade_ceylon_aceh.yaml --dry-run

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from loguru import logger

from actions.sail_actions import sail_to_port, where_am_i, navigate_to_building
from capture.adb_capture import capture_screen
from memory.voyage_log import log_leg, log_round
from vision.ocr import read_port_name


# ── Result tracking ────────────────────────────────────────────────────────────

@dataclass
class StepResult:
    action:      str
    port:        Optional[str]
    ok:          bool
    profit:      int = 0      # ducats gained (negative = loss)
    duration_s:  float = 0.0  # wall-clock seconds this step took
    notes:       str = ""


@dataclass
class TaskReport:
    task_name:          str
    rounds_completed:   int = 0
    steps:              list[StepResult] = field(default_factory=list)
    unrecognized:       list[str] = field(default_factory=list)
    started_at:         Optional[datetime] = None
    ended_at:           Optional[datetime] = None

    @property
    def total_profit(self) -> int:
        return sum(s.profit for s in self.steps)

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds()
        return sum(s.duration_s for s in self.steps)

    @property
    def ducats_per_hour(self) -> Optional[float]:
        secs = self.elapsed_seconds
        if secs > 0:
            return self.total_profit / secs * 3600
        return None

    def print(self) -> None:
        logger.info("=" * 60)
        logger.info(f"Task Report: {self.task_name}")
        logger.info(f"  Rounds completed : {self.rounds_completed}")
        logger.info(f"  Total profit     : {self.total_profit:+,} ducats")
        elapsed = self.elapsed_seconds
        if elapsed > 0:
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            logger.info(f"  Elapsed time     : {mins}m {secs}s")
        dph = self.ducats_per_hour
        if dph is not None:
            logger.info(f"  Ducats / hour    : {dph:,.0f}")
        logger.info("")
        logger.info("  Step breakdown:")
        for i, s in enumerate(self.steps, 1):
            status = "✓" if s.ok else "✗"
            profit_str = f"  {s.profit:+,}d" if s.profit else ""
            dur_str = f"  {s.duration_s/60:.1f}min" if s.duration_s > 0 else ""
            logger.info(f"    {i:2d}. [{status}] {s.action:<12} @ {s.port or '?':<15}"
                        f"{profit_str}{dur_str}  {s.notes}")
        if self.unrecognized:
            logger.info("")
            logger.info(f"  Unrecognized screens ({len(self.unrecognized)}):")
            for u in self.unrecognized:
                logger.info(f"    - {u}")
        logger.info("=" * 60)

    def save(self, path: Optional[Path] = None) -> None:
        """Persist this report as JSON for later analysis."""
        if path is None:
            log_dir = Path("memory/knowledge/trade_log")
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = (self.started_at or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H-%M-%S")
            path = log_dir / f"{ts}__{self.task_name.replace(' ', '_')}.json"
        record = {
            "task_name":        self.task_name,
            "started_at":       self.started_at.isoformat() if self.started_at else None,
            "ended_at":         self.ended_at.isoformat()   if self.ended_at   else None,
            "elapsed_seconds":  self.elapsed_seconds,
            "rounds_completed": self.rounds_completed,
            "total_profit":     self.total_profit,
            "ducats_per_hour":  self.ducats_per_hour,
            "steps": [
                {
                    "action":     s.action,
                    "port":       s.port,
                    "ok":         s.ok,
                    "profit":     s.profit,
                    "duration_s": s.duration_s,
                    "notes":      s.notes,
                }
                for s in self.steps
            ],
        }
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        logger.info(f"Trade log saved → {path}")


# ── Action implementations ─────────────────────────────────────────────────────

def _ports_from_steps(steps: list[StepResult], start_port: Optional[str]) -> list[str]:
    """Extract ordered port sequence from a round's steps for voyage log."""
    ports: list[str] = []
    if start_port:
        ports.append(start_port)
    for s in steps:
        if s.action == "sail_to" and s.port and s.ok:
            if not ports or ports[-1].lower() != s.port.lower():
                ports.append(s.port)
    return ports


def _current_port() -> Optional[str]:
    frame = capture_screen()
    return read_port_name(frame)


def _navigate_to_market(port: str, home_port: Optional[str] = None) -> bool:
    logger.info(f"Navigating to Market at {port}…")
    # Ensure we are at port_overworld before attempting building navigation.
    # recover_to_port_overworld handles: stuck dialogs, flows, sea, main menu, etc.
    from brain.recovery import recover_to_port_overworld
    result = recover_to_port_overworld(home_port=home_port or port, timeout=300.0)
    if result.state != "port_overworld":
        logger.error(f"  Could not reach port_overworld before market navigation: {result.state!r}")
        return False
    time.sleep(1.5)   # let overworld UI settle after any recent screen transition
    return navigate_to_building("market")


def _exit_to_overworld() -> bool:
    from actions.sail_actions import exit_to_overworld
    return exit_to_overworld(timeout=30.0)


def run_sell_all(port: str, report: TaskReport, dry_run: bool,
                 exit_after: bool = True) -> StepResult:
    logger.info(f"[sell_all] @ {port}")
    if dry_run:
        return StepResult("sell_all", port, ok=True, notes="dry-run")

    if not _navigate_to_market(port, home_port=port):
        return StepResult("sell_all", port, ok=False, notes="could not enter market")

    from actions.market_actions import sell_all_cargo
    try:
        result = sell_all_cargo(port=port)  # returns List[SellResult]
        profit = sum(r.profit for r in result) if result else 0
        goods_sold = len(result) if result else 0
        if exit_after:
            _exit_to_overworld()
        # Empty result = no cargo to sell — that's fine (first run, or already sold).
        # ok=False only on exception (caught below).
        return StepResult("sell_all", port, ok=True, profit=profit,
                          notes=f"sold {goods_sold} goods  +{profit:,}d")
    except Exception as exc:
        logger.error(f"  sell_all failed: {exc}")
        _exit_to_overworld()
        return StepResult("sell_all", port, ok=False, notes=str(exc))


def run_buy_all(
    port: str,
    report: TaskReport,
    dry_run: bool,
    destination: Optional[str] = None,
    enter_market: bool = True,
) -> StepResult:
    logger.info(f"[buy_all] @ {port}" + (f" → {destination}" if destination else ""))
    if dry_run:
        return StepResult("buy_all", port, ok=True, notes="dry-run")

    if enter_market and not _navigate_to_market(port, home_port=port):
        return StepResult("buy_all", port, ok=False, notes="could not enter market")

    from actions.market_actions import auto_buy
    try:
        result = auto_buy(port=port, destination=destination)  # returns BuyResult dataclass
        ok = result is not None
        spent = result.purchase_cost if result else 0
        goods_bought = len(result.goods) if result and result.goods else 0
        dest_note = f" (→{destination})" if destination else ""
        _exit_to_overworld()
        return StepResult("buy_all", port, ok=ok, profit=-spent,
                          notes=f"bought {goods_bought} goods  -{spent:,}d{dest_note}")
    except Exception as exc:
        logger.error(f"  buy_all failed: {exc}")
        _exit_to_overworld()
        return StepResult("buy_all", port, ok=False, notes=str(exc))


def run_sail_to(
    destination: str,
    report: TaskReport,
    dry_run: bool,
    from_port: Optional[str] = None,
) -> StepResult:
    logger.info(f"[sail_to] → {destination}")
    if dry_run:
        return StepResult("sail_to", destination, ok=True, notes="dry-run")

    # Check supply before departing — resupply at a nearby port if needed
    _ensure_supply(destination)

    departed_at = datetime.now(timezone.utc)
    t0 = time.monotonic()

    # Goal-driven tick loop: perceive → dispatch → one action → loop.
    # Replaces the sequential sail_to_port() call.  Each tick calls
    # perceive() for ground truth and dispatches one phase-level action.
    # Phase transitions are driven by perceive() results between ticks.
    import random
    from brain.goals.sail_to import SailToGoal

    goal = SailToGoal(destination=destination, from_port=from_port)
    while not goal.is_complete and not goal.is_failed:
        result = goal.tick()
        # Inter-tick anti-cheat jitter.  See run.py:308 for the same
        # rationale — perception already supplies most of the human-
        # scale delay between transactional taps.
        delay = result.delay if result.delay > 0 else random.uniform(1.5, 2.5)
        time.sleep(delay)

    duration = time.monotonic() - t0
    ok = goal.is_complete
    arrived_at = datetime.now(timezone.utc)

    if not ok:
        logger.warning(
            f"[sail_to] sail to {destination!r} failed"
            f" — {goal._fail_reason}"
            " — escalating via recovery"
        )
        from brain.recovery import recover_to_port_overworld
        recover_to_port_overworld(home_port=from_port)

    if from_port:
        log_leg(
            from_port=from_port,
            to_port=destination,
            departed_at=departed_at,
            arrived_at=arrived_at,
            ok=ok,
        )

    return StepResult("sail_to", destination, ok=ok, duration_s=duration,
                      notes="arrived" if ok else f"failed: {goal._fail_reason}")


def _ensure_supply(destination: str) -> None:
    """
    Read the sea HUD supply days and ETA.  If supply is insufficient for the
    voyage, sail to the nearest resupply port first.

    Only acts when the bot is actually at sea (sail_to was called mid-voyage
    or after a cinematic skip).  If the bot is in port, the harbour's
    Supply Departure button already handles restocking automatically.
    """
    from actions.sail_actions import read_sea_hud, where_am_i

    loc = where_am_i()
    if loc.get("location") not in ("sea", "sea_cinematic"):
        return   # in port — Supply Departure handles it

    hud = read_sea_hud()
    supply = hud.get("supply_days")
    eta    = hud.get("eta_days")

    if supply is None or eta is None:
        logger.debug("  Supply check skipped — HUD values not readable")
        return

    logger.info(f"  Supply check: {supply}d remaining, ETA {eta}d to {destination}")

    # Add a 2-day buffer so we don't cut it too close
    if supply >= eta + 2:
        return   # sufficient supply

    logger.warning(
        f"  Insufficient supply ({supply}d) for voyage to {destination} (ETA {eta}d) "
        f"— diverting to resupply"
    )

    # Find the nearest known port along the route as a resupply stop
    from brain.route_planner import plan_route
    current_port = loc.get("port")
    if current_port:
        waypoints = plan_route(current_port, destination)
        # First intermediate stop is the resupply candidate
        if len(waypoints) > 2:
            resupply_port = waypoints[1]
        else:
            resupply_port = waypoints[-1]   # destination itself if no stops
    else:
        logger.warning("  Cannot determine current port for resupply routing")
        return

    logger.info(f"  Diverting to {resupply_port!r} for resupply")
    sail_to_port(resupply_port, from_building=False)
    # Supply Departure at the resupply port is handled by the harbour flow


# ── Core executor ──────────────────────────────────────────────────────────────

def run_explore(step: dict, report: TaskReport, dry_run: bool) -> StepResult:
    """§13.29 — explore voyage step.

    Reads voyage params from the YAML step (side, optional dest, etc.),
    departs from the current port if needed, then runs the HugShoreGoal
    loop.  See `actions/explore_voyage.py` for the underlying primitive.
    """
    side = step.get("side")
    if not side:
        return StepResult("explore", "?", ok=False,
                           notes="missing required 'side' (port|starboard|left|right)")
    if dry_run:
        return StepResult("explore", f"side={side}", ok=True,
                           notes=f"dry-run; would explore {side}")

    from actions.explore_voyage import run_explore_voyage
    out = run_explore_voyage(
        side=side,
        endpoint_lat=step.get("endpoint_lat"),
        endpoint_lon=step.get("endpoint_lon"),
        max_ticks=step.get("max_ticks", 550),
        hud_every=step.get("hud_every", 1),
        driver_mode=step.get("driver_mode", "point_pursuit"),
        waypoint_generator=step.get("waypoint_generator", "phase_a"),
        astern_expansion=step.get("astern_expansion", False),
        graduated_density=step.get("graduated_density", False),
        junction_picker=step.get("junction_picker", "none"),
    )
    return StepResult(
        "explore", f"side={side}", ok=out["ok"],
        notes=out["notes"],
    )


def run_explore_port(port: str, report: TaskReport, dry_run: bool) -> StepResult:
    """Walk every building + sub-menu visible from the port map.

    Persists frames + KB records; no Claude calls.  Seeding layout `.md`
    files is a separate batch pass via `tools/seed_layout_from_frame.py`.
    """
    logger.info(f"[explore_port] @ {port}")
    from actions.explore_actions import explore_port
    try:
        summary = explore_port(port=port, dry_run=dry_run)
    except Exception as exc:
        logger.error(f"  explore_port failed: {exc}")
        return StepResult("explore_port", port, ok=False, notes=str(exc))

    visited_n = len(summary.get("visited", []))
    total_n   = len(summary.get("buildings", []))
    locked_n  = sum(1 for b in summary.get("buildings", []) if b.get("locked"))
    notes = (
        f"visited {visited_n}/{total_n - locked_n}; locked={locked_n}; "
        f"session={summary.get('session')}"
    )
    return StepResult("explore_port", port, ok=visited_n > 0 or dry_run,
                      notes=notes)


_ACTION_HANDLERS = {
    "sell_all":     run_sell_all,
    "buy_all":      run_buy_all,
    "sail_to":      run_sail_to,
    "explore":      run_explore,
    "explore_port": run_explore_port,
}


def run_smart_trade(path: Path, dry_run: bool = False) -> TaskReport:
    """Decision-driven trade over a cycle of ports (yaml `mode: smart_trade`).

    Instead of a fixed sell→buy→sail at every port, at each port it:
      1. (profit-aware) sells only cargo that PROFITS there — carries the rest,
         so it never dumps goods at a loss / at the port they were bought;
      2. reads the resulting cargo fill (market kept open);
      3. lets brain.trade_policy.decide_trade_action choose BUY (hold has room AND
         there are goods profitable for the NEXT port, from the price KB) vs SAIL
         (hold already loaded / nothing here worth buying);
      4. sails to the next port.
    One market visit per port.  Yaml: {mode: smart_trade, ports: [A, B, ...], rounds: N}.
    """
    from brain.trade_policy import TradeState, decide_trade_action
    task = yaml.safe_load(path.read_text())
    ports = task.get("ports", [])
    rounds = task.get("rounds", 1)
    report = TaskReport(task_name=task.get("name", path.stem),
                        started_at=datetime.now(timezone.utc))
    if len(ports) < 2:
        logger.error("[smart_trade] need at least 2 ports in 'ports:'")
        return report

    logger.info("=" * 60)
    logger.info(f"Smart trade: {' → '.join(ports)} → (loop)   {rounds} round(s)")
    logger.info("=" * 60)

    # Start the cycle from where the ship ACTUALLY is: rotate the route so it
    # begins at the current port; if the ship is off-route, sail to the first
    # route port first.  (The ship may not be at ports[0] — e.g. drifted to a
    # neighbouring port.)
    if not dry_run:
        loc = where_am_i()
        here = (loc.get("port") or _current_port() or "").strip()
        idx = next((i for i, p in enumerate(ports)
                    if here and p.lower() in here.lower()), None)
        if idx:
            ports = ports[idx:] + ports[:idx]
            logger.info(f"[smart_trade] ship is at {here!r} — starting the cycle there")
        elif idx is None and here:
            logger.info(f"[smart_trade] ship at {here!r} is off-route — sailing to {ports[0]} first")
            report.steps.append(run_sail_to(ports[0], report, dry_run))

    for round_num in range(1, rounds + 1):
        logger.info(f"\n── Round {round_num}/{rounds} ─────────────────────────────")
        for i in range(len(ports)):
            port, dest = ports[i], ports[(i + 1) % len(ports)]
            logger.info(f"\n[smart_trade] at {port}, next → {dest}")
            if dry_run:
                report.steps.append(StepResult("smart_trade", port, ok=True, notes=f"dry-run → {dest}"))
                continue

            # 1. Profit-aware sell (self-limiting); keep the market open for the buy.
            report.steps.append(run_sell_all(port, report, dry_run, exit_after=False))

            # 2. Cargo fill (market still open) + goods profitable to buy for the dest.
            from actions.market_actions import _read_cargo_capacity
            from capture.adb_capture import capture_screen
            used, total = _read_cargo_capacity(capture_screen())
            try:
                from memory.market_kb import profitable_routes
                buyable = [r["good"] for r in profitable_routes(port, dest)]
            except Exception as exc:
                buyable = []
                logger.debug(f"[smart_trade] profitable_routes failed: {exc}")

            # 3. Decide BUY vs SAIL from the observed state.
            action = decide_trade_action(TradeState(
                port, dest, used, total, sellable_profit_here=[], buyable_for_dest=buyable))
            logger.info(f"[smart_trade] decision @ {port}: {action['op'].upper()} — {action['why']}")
            try:
                from actions import action_trace
                if action_trace.active():
                    action_trace.record_decision(
                        inputs={"port": port, "destination": dest,
                                "cargo": f"{used}/{total}", "sellable_profit_here": [],
                                "buyable_for_dest": buyable},
                        output=action, model="brain.trade_policy.decide_trade_action")
            except Exception as _exc:
                logger.debug(f"[smart_trade] record_decision failed: {_exc}")

            has_room = total > 0 and used < total * 0.85
            if action["op"] == "buy":
                report.steps.append(run_buy_all(port, report, dry_run,
                                                destination=dest, enter_market=False))
            elif has_room and not buyable:
                # Cold start: the price KB has no known-profitable route port→dest
                # yet.  With room in the hold, do an EXPLORATORY buy (auto_buy's
                # culture-filtered specialties) to bootstrap the price data — safe
                # because the profit-aware sell at the destination won't dump it at
                # a loss.  Once the KB fills, buys become route-driven.
                logger.info(f"[smart_trade] no KB route {port}→{dest} yet + hold has room "
                            f"— exploratory buy to bootstrap prices")
                report.steps.append(run_buy_all(port, report, dry_run,
                                                destination=dest, enter_market=False))
            else:
                _exit_to_overworld()

            # 4. Sail to the next port.
            report.steps.append(run_sail_to(dest, report, dry_run))

    report.print()
    report.save()
    return report


def run_task(task_path: str | Path, dry_run: bool = False) -> TaskReport:
    """
    Load a YAML task file and execute it. Returns a TaskReport.
    Dispatches to run_self_grow when mode: autonomous, run_smart_trade when
    mode: smart_trade.
    """
    path = Path(task_path)
    if not path.exists():
        raise FileNotFoundError(f"Task file not found: {path}")

    task = yaml.safe_load(path.read_text())

    if task.get("mode") == "autonomous":
        from actions.self_grow import run_self_grow
        return run_self_grow(path, dry_run=dry_run)

    if task.get("mode") == "smart_trade":
        return run_smart_trade(path, dry_run=dry_run)
    name    = task.get("name", path.stem)
    rounds  = task.get("rounds", 1)
    loop    = task.get("loop", [])
    start_port = task.get("start_port")

    report = TaskReport(task_name=name, started_at=datetime.now(timezone.utc))

    logger.info("=" * 60)
    logger.info(f"Starting task: {name}")
    logger.info(f"  Rounds : {rounds}")
    logger.info(f"  Steps  : {len(loop)} per round")
    if dry_run:
        logger.info("  Mode   : DRY RUN (no ADB actions)")
    logger.info("=" * 60)

    # Verify starting location
    if start_port and not dry_run:
        loc = where_am_i()
        current = loc.get("port") or _current_port()
        if current and start_port.lower() not in current.lower():
            logger.warning(f"Expected to start at {start_port!r} but where_am_i says {current!r}")
        else:
            logger.info(f"Starting at {current or start_port!r} — confirmed")

    # Execute rounds
    for round_num in range(1, rounds + 1):
        logger.info(f"\n── Round {round_num}/{rounds} ─────────────────────────────")

        round_ok = True
        market_open = False   # True when sell_all stayed in market, buy_all skips re-entry
        last_known_port = start_port or "?"
        for step_idx, step in enumerate(loop):
            action           = step.get("action", "")
            destination      = step.get("destination")
            expected_port    = step.get("port")       # optional: skip step if wrong port
            note             = step.get("note", "")

            if note:
                logger.info(f"  Note: {note}")

            # Determine current port for context
            if not dry_run:
                if market_open:
                    # Still inside market from previous sell_all — port is unchanged.
                    # where_am_i() would return "building" with no port name here.
                    port     = last_known_port
                    loc_type = "building"
                else:
                    loc = where_am_i()
                    loc_type = loc.get("location", "unknown")
                    port = loc.get("port") or _current_port() or "?"
                # If port name is suspiciously short (OCR noise right after a
                # screen transition), wait and retry once — but not when market_open
                # (we trust last_known_port in that case).
                if not market_open and port and len(port) < 3:
                    logger.debug(f"  Port name {port!r} looks garbled — retrying in 3s")
                    time.sleep(3.0)
                    loc = where_am_i()
                    loc_type = loc.get("location", "unknown")
                    port = loc.get("port") or _current_port() or "?"
                if port and port != "?":
                    last_known_port = port
                # Track unrecognized screens
                if loc_type == "unknown":
                    report.unrecognized.append(
                        f"Round {round_num}, before {action!r}: {loc.get('detail', '')}"
                    )
                # Skip market/navigation steps while at sea — there's no port map
                # available and taps will hit sea-view UI elements instead.
                # explore runs the steering loop directly from sea, so it
                # is exempt from this skip.
                if (loc_type in ("sea", "sea_cinematic")
                        and action not in ("sail_to", "explore")):
                    logger.warning(
                        f"  At sea — skipping {action!r} (no port; waiting for next sail_to)"
                    )
                    result = StepResult(action, port or "at sea", ok=False,
                                        notes="skipped — bot is at sea")
                    report.steps.append(result)
                    round_ok = False
                    continue

                # Skip sell/buy steps if we're at the wrong port.
                # This prevents the bot from selling London goods at Lisbon
                # when a sail_to Port Royal failed and the game auto-returned.
                if expected_port and action in ("sell_all", "buy_all") and port != "?":
                    ep_lower = expected_port.lower()
                    p_lower  = port.lower()
                    if ep_lower not in p_lower and p_lower not in ep_lower:
                        logger.warning(
                            f"  Wrong port — expected {expected_port!r} but at {port!r}; "
                            f"skipping {action!r}"
                        )
                        result = StepResult(action, port, ok=False,
                                            notes=f"skipped — at {port!r}, expected {expected_port!r}")
                        report.steps.append(result)
                        round_ok = False
                        continue
            else:
                port = start_port or "?"
                loc_type = "unknown"

            handler = _ACTION_HANDLERS.get(action)
            if handler is None:
                logger.warning(f"  Unknown action {action!r} — skipping")
                continue

            # Look ahead for two purposes:
            # 1. buy_all needs the next sail_to destination for culture-aware filtering
            # 2. sell_all followed immediately by buy_all at the same port → stay in market
            next_destination: Optional[str] = None
            sell_then_buy = False
            if action in ("sell_all", "buy_all"):
                for future_step in loop[step_idx + 1:]:
                    fut_action = future_step.get("action")
                    if fut_action == "sail_to":
                        next_destination = future_step.get("destination")
                        break
                    if fut_action == "buy_all" and action == "sell_all":
                        # buy_all is the very next market step — stay in market
                        fut_port = future_step.get("port", port)
                        if not fut_port or fut_port.lower() in port.lower() or port.lower() in fut_port.lower():
                            sell_then_buy = True

            # sail_to uses destination as the "port" arg
            t0 = time.monotonic()
            if action == "sail_to":
                result = run_sail_to(destination, report, dry_run,
                                     from_port=last_known_port if last_known_port != "?" else None)
            elif action == "sell_all":
                result = run_sell_all(port, report, dry_run, exit_after=not sell_then_buy)
                market_open = sell_then_buy and result.ok
            elif action == "buy_all":
                result = run_buy_all(port, report, dry_run,
                                     destination=next_destination,
                                     enter_market=not market_open)
                market_open = False
            elif action == "explore":
                # explore needs the full step dict for side/dest/max_ticks
                result = run_explore(step, report, dry_run)
                market_open = False
            else:
                result = handler(port, report, dry_run)
                market_open = False
            # Fill duration for non-sail steps (sail_to sets its own)
            if result.duration_s == 0.0:
                result.duration_s = time.monotonic() - t0
            report.steps.append(result)

            if not result.ok:
                logger.error(f"  Step failed: {action} @ {result.port} — {result.notes}")
                round_ok = False
                # Don't abort the whole task on one step failure — log and continue

        # Log this round to the voyage log (regardless of ok — partial data is useful)
        round_elapsed = sum(s.duration_s for s in report.steps[-len(loop):])
        round_profit  = sum(s.profit for s in report.steps[-len(loop):])
        ports_visited = _ports_from_steps(report.steps[-len(loop):], start_port)
        round_dph = (round_profit / round_elapsed * 3600) if round_elapsed > 0 else None
        if not dry_run:
            log_round(
                ports_visited=ports_visited,
                profit=round_profit,
                elapsed_s=round_elapsed,
                ducats_per_hour=round_dph,
            )

        if round_ok:
            report.rounds_completed += 1

    report.ended_at = datetime.now(timezone.utc)
    report.print()
    report.save()
    return report
