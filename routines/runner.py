# routines/runner.py
# Executes step strings and named routines.
#
# Each step is a plain-English string like "goto castle" or "buy icon".
# Unknown / not-yet-implemented steps are logged as warnings and skipped
# so that routines can be defined ahead of their implementations.

from __future__ import annotations

import random
import time

from loguru import logger

from brain.states.exploring_port import go_to_building
from routines.definitions import ROUTINES


# Pause between consecutive steps — gives the game time to settle and looks
# more like a human who pauses briefly between actions.
INTER_STEP_MIN: float = 2.0
INTER_STEP_MAX: float = 5.0


def run_step(step: str) -> bool:
    """
    Parse and execute a single step string.
    Returns True on success or if the step is a non-fatal stub.
    Returns False when the step fails in a way that should abort the routine.
    """
    parts = step.strip().lower().split()
    if not parts:
        return True

    action = parts[0]
    args = parts[1:]

    # ── explore ──────────────────────────────────────────────────────────────
    if action == "explore":
        from brain.states.exploring_port import explore_all_buildings
        explore_all_buildings()
        return True

    # ── goto <building> ──────────────────────────────────────────────────────
    if action in ("goto", "go"):
        if not args:
            logger.error(f"Step '{step}': missing building name")
            return False
        target = " ".join(args)
        return go_to_building(target)

    # ── collect rewards ──────────────────────────────────────────────────────
    if action == "collect" and args and args[0] == "rewards":
        logger.warning("Step 'collect rewards' not yet implemented — skipping")
        return True  # non-fatal: continue the routine

    # ── buy <item> ───────────────────────────────────────────────────────────
    if action == "buy":
        item = " ".join(args) if args else "(unknown)"
        logger.warning(f"Step 'buy {item}' not yet implemented — skipping")
        return True  # non-fatal

    # ── unknown ──────────────────────────────────────────────────────────────
    logger.warning(f"Unknown step: '{step}' — skipping")
    return True  # unknown steps are non-fatal to avoid breaking routines mid-run


def run_routine(name: str) -> bool:
    """
    Execute a named routine from definitions.py in order.
    Stops and returns False if any step returns False.
    Returns True when all steps complete.
    """
    if name not in ROUTINES:
        logger.error(
            f"Routine '{name}' not found. "
            f"Available: {', '.join(ROUTINES)}"
        )
        return False

    steps = ROUTINES[name]
    logger.info(f"Starting routine '{name}' — {len(steps)} step(s)")

    for i, step in enumerate(steps, 1):
        logger.info(f"  [{i}/{len(steps)}] {step}")
        success = run_step(step)
        if not success:
            logger.error(f"Routine '{name}' aborted at step {i}: '{step}'")
            return False

        if i < len(steps):
            pause = random.uniform(INTER_STEP_MIN, INTER_STEP_MAX)
            logger.debug(f"Pausing {pause:.1f}s before next step")
            time.sleep(pause)

    logger.info(f"Routine '{name}' complete")
    return True
