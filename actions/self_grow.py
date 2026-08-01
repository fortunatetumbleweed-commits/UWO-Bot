# actions/self_grow.py
#
# Autonomous growth loop.
#
# Each round the bot:
#   1. Picks the best destination from the candidate list using the route planner
#   2. Sells + buys at home port, sails to destination via plan_route hops
#   3. Sells + buys at destination, sails home via plan_route hops
#   4. Logs the round to the voyage log
#   5. Checks the explore trigger — if due, appends a newly discovered port
#      to the candidate list in self_grow.yaml for future rounds
#
# The London ↔ Port Royal seed route runs first (round 1) so there is always
# one known-good reference before the planner starts scoring alternatives.
#
# Usage:
#   python run_task.py tasks/self_grow.yaml

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from loguru import logger

from actions.task_runner import (
    TaskReport, StepResult,
    run_sell_all, run_buy_all, run_sail_to,
    _current_port, _exit_to_overworld,
)
from brain.recovery import assert_at_port, RecoveryError
from brain.explore_trigger import ExploreTrigger
from brain.route_planner import plan_route
from memory.trade_priors import leg_profit_score
from memory.voyage_log import load_rounds, log_round


# ── UCB destination picker ─────────────────────────────────────────────────────

_UCB_C = 1.5


def _pick_destination(home_port: str, candidates: list[str]) -> str:
    """
    Score each candidate destination and return the best one.

    Score = prior_profit_score * UCB_bonus
    UCB_bonus = C * sqrt(log(total_rounds + 1) / max(visits_to_candidate, 1))

    Unvisited ports get a large exploration bonus; well-observed routes converge
    to their real prior score.
    """
    rounds = load_rounds()
    total  = max(len(rounds), 1)

    # Count how many rounds ended at each candidate
    visits: dict[str, int] = {}
    for r in rounds:
        ports = r.get("ports", [])
        if ports:
            dest = ports[-1].lower()
            visits[dest] = visits.get(dest, 0) + 1

    best_score = -1.0
    best_port  = candidates[0]

    for dest in candidates:
        prior  = leg_profit_score(home_port, dest)
        n      = visits.get(dest.lower(), 0)
        bonus  = _UCB_C * math.sqrt(math.log(total + 1) / max(n, 1))
        score  = prior * (1 + bonus)
        logger.debug(
            f"  [self_grow] {dest:20s}  prior={prior:.2f}  visits={n}  "
            f"bonus={bonus:.2f}  score={score:.2f}"
        )
        if score > best_score:
            best_score = score
            best_port  = dest

    logger.info(f"  [self_grow] Destination chosen: {best_port!r}  (score={best_score:.2f})")
    return best_port


# ── One trade round ────────────────────────────────────────────────────────────

def _run_trade_round(
    home_port: str,
    destination: str,
    report: TaskReport,
    round_num: int,
    dry_run: bool,
) -> bool:
    """
    Execute one complete out-and-back trade round.
    Returns True if the round completed without fatal errors.
    """
    logger.info(f"\n── Round {round_num}: {home_port} ↔ {destination} ─────────────────")

    forward_hops = plan_route(home_port, destination)
    return_hops  = plan_route(destination, home_port)

    steps_ok = True

    # ── Forward leg ──
    r = run_sell_all(home_port, report, dry_run)
    report.steps.append(r)
    steps_ok &= r.ok

    r = run_buy_all(home_port, report, dry_run, destination=destination)
    report.steps.append(r)
    steps_ok &= r.ok

    prev = home_port
    for hop in forward_hops[1:]:
        r = run_sail_to(hop, report, dry_run, from_port=prev)
        report.steps.append(r)
        steps_ok &= r.ok
        prev = hop

    # ── Return leg ──
    r = run_sell_all(destination, report, dry_run)
    report.steps.append(r)
    steps_ok &= r.ok

    r = run_buy_all(destination, report, dry_run, destination=home_port)
    report.steps.append(r)
    steps_ok &= r.ok

    prev = destination
    for hop in return_hops[1:]:
        r = run_sail_to(hop, report, dry_run, from_port=prev)
        report.steps.append(r)
        steps_ok &= r.ok
        prev = hop

    return steps_ok


# ── Explore trigger integration ────────────────────────────────────────────────

def _maybe_add_new_destination(
    task_path: Path,
    task: dict,
    home_port: str,
    destination: str,
) -> None:
    """
    Check the explore trigger.  If a detour is due and a new port is suggested,
    append it to candidate_destinations in self_grow.yaml.
    """
    trigger = ExploreTrigger()
    stats   = trigger.stats()
    logger.info(
        f"  [explore] {stats['voyages_since_last_detour']} / "
        f"{stats['detour_interval']} voyages since last detour  "
        f"(due={stats['detour_due']})"
    )
    if not stats["detour_due"]:
        return

    # The explore trigger needs a list of nearby port names to choose from.
    # We pull these from the trade priors region table as known candidates,
    # excluding ports already in our list.
    from memory.trade_priors import SEA_REGIONS
    current_candidates = {p.lower() for p in task.get("candidate_destinations", [])}
    known_ports = [p.title() for p in SEA_REGIONS if p not in current_candidates]

    new_port = trigger.maybe_detour(
        ocr_ports=known_ports,
        current_route=[home_port, destination],
    )
    if not new_port:
        return

    candidates: list[str] = task.get("candidate_destinations", [])
    if new_port not in candidates:
        candidates.append(new_port)
        task["candidate_destinations"] = candidates
        task_path.write_text(yaml.dump(task, default_flow_style=False, allow_unicode=True))
        logger.info(f"  [self_grow] Added new candidate destination: {new_port!r}")
        trigger.record_detour_taken()


# ── Main entry point ───────────────────────────────────────────────────────────

def run_self_grow(task_path: str | Path, dry_run: bool = False) -> TaskReport:
    """
    Autonomous growth loop.  Runs until interrupted (Ctrl+C) or max rounds reached.
    """
    path = Path(task_path)
    task = yaml.safe_load(path.read_text())

    home_port  = task.get("home_port", "London")
    max_rounds = task.get("rounds", 9999)
    seed_route = task.get("seed_route", [home_port])
    candidates = task.get("candidate_destinations", ["Port Royal"])
    name       = task.get("name", "self_grow")

    report = TaskReport(task_name=name, started_at=datetime.now(timezone.utc))

    logger.info("=" * 60)
    logger.info(f"Self-grow loop starting — home: {home_port}")
    logger.info(f"  Candidates: {candidates}")
    logger.info(f"  Seed route: {seed_route}")
    if dry_run:
        logger.info("  Mode: DRY RUN")
    logger.info("=" * 60)

    # Phase 3.3: invalidate Moondream family cache at task start so the
    # classifier's early-arbiter gate gets a fresh family verdict on the
    # very next tick.
    try:
        from brain import moondream_family_cache as _mfc
        _mfc.invalidate("task_start")
    except Exception as e:
        logger.debug(f"[self_grow] moondream cache invalidate failed: {e}")

    # ── Determine actual current port ────────────────────────────────────────
    # The bot may have been stopped mid-route at a port other than home_port.
    # Read the real current location before assuming we start from home.
    if not dry_run:
        try:
            assert_at_port(context="self_grow startup", home_port=home_port)
        except RecoveryError as e:
            logger.error(f"  [self_grow] Cannot start — recovery failed: {e}")
            report.ended_at = datetime.now(timezone.utc)
            return report
        actual_port = _current_port()
        if actual_port:
            actual_port_norm = actual_port.strip().title()
            home_norm = home_port.strip().title()
            if actual_port_norm.lower() != home_norm.lower():
                logger.info(
                    f"  [self_grow] Detected current port {actual_port_norm!r} "
                    f"≠ home {home_norm!r} — sailing home first"
                )
                r = run_sail_to(home_port, report, dry_run, from_port=actual_port_norm)
                report.steps.append(r)
                if not r.ok:
                    # Recovery + Claude/human escalation already fired inside run_sail_to.
                    # Don't abort — continue from wherever the bot ended up after escalation.
                    logger.warning(
                        f"  [self_grow] Could not reach home port {home_port!r} — "
                        "continuing from current location after escalation"
                    )
            else:
                logger.info(f"  [self_grow] Already at home port {actual_port_norm!r}")
        else:
            logger.warning("  [self_grow] Could not read current port — assuming home port")

    round_start = datetime.now(timezone.utc)

    for round_num in range(1, max_rounds + 1):
        round_start = datetime.now(timezone.utc)

        # Round 1: always run the seed route so we have a reference baseline
        if round_num == 1 and len(seed_route) >= 2:
            destination = seed_route[-1]
            logger.info(f"  Round 1: running seed route to {destination!r}")
        else:
            # Re-read candidates from YAML each round (may have grown)
            task       = yaml.safe_load(path.read_text())
            candidates = task.get("candidate_destinations", candidates)
            destination = _pick_destination(home_port, candidates)

        round_ok = _run_trade_round(
            home_port, destination, report, round_num, dry_run
        )

        # Log round to voyage log
        round_elapsed = (datetime.now(timezone.utc) - round_start).total_seconds()
        round_profit  = sum(s.profit for s in report.steps[-10:])  # last ~10 steps
        round_dph     = (round_profit / round_elapsed * 3600) if round_elapsed > 0 else None
        if not dry_run:
            ports = [home_port] + [
                s.port for s in report.steps[-10:]
                if s.action == "sail_to" and s.ok and s.port
            ]
            log_round(
                ports_visited=ports,
                profit=round_profit,
                elapsed_s=round_elapsed,
                ducats_per_hour=round_dph,
            )

        dph_str = f"  {round_dph:,.0f} duc/hr" if round_dph else ""
        logger.info(
            f"  Round {round_num} complete: {round_profit:+,}d  "
            f"{round_elapsed/60:.1f}min{dph_str}"
        )

        if round_ok:
            report.rounds_completed += 1

        # Check explore trigger — may append new destination to YAML
        if not dry_run:
            task = yaml.safe_load(path.read_text())
            _maybe_add_new_destination(path, task, home_port, destination)

    report.ended_at = datetime.now(timezone.utc)
    report.print()
    report.save()
    return report
