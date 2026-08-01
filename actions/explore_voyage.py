"""§13.29 — Explore voyage action.

A `sail-and-hug-shore` task that:
  - departs from the current port if the bot starts there;
  - skips destination selection on the world map (the whole point of
    exploring is that there's no fixed destination port);
  - runs `HugShoreGoal` with the requested side and optional
    endpoint lat/lon for goal-bearing guidance.

Designed so the YAML task looks like::

    - action: explore
      side: port              # required: port|starboard|left|right
      endpoint_lat: 5.71   # optional
      endpoint_lon: 32.12  # optional
      max_ticks: 550          # optional, default 550
      hud_every: 1            # optional, default 1 (every tick)

The action is dispatched from `actions/task_runner.py`.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger


# Aliases for the YAML `side` field.  Canonical form is the same as
# HugShoreGoal's: "port" / "starboard".  We accept "left" / "right" as
# convenience aliases ("explore left" reads naturally to a human).
_SIDE_ALIASES = {
    "port":      "port",
    "starboard": "starboard",
    "left":      "port",
    "right":     "starboard",
}


def _ensure_at_sea() -> bool:
    """Depart from port if currently in one; no-op when already at sea.

    Returns True when the bot is confirmed at sea on exit, False on
    failure (could not enter the harbour, could not tap Depart, etc.).
    """
    from actions.sail_actions import (
        where_am_i,
        _navigate_to_harbour,
        _depart_from_harbour,
    )

    loc = where_am_i()
    loc_type = (loc.get("location") or "").lower()

    if loc_type in ("sea", "sea_cinematic", "sea_overworld"):
        logger.info("[explore] already at sea — skipping harbour departure")
        return True

    # `building` covers the case where the bot is already inside the
    # harbour (or any other building) at task start.  `navigate_to_building`
    # handles back-out-and-re-enter if needed, so we just route both
    # cases through the same departure flow.
    if loc_type not in ("port_overworld", "in_town", "building"):
        logger.error(
            f"[explore] unsupported start location {loc_type!r}; "
            f"explore must start at sea or in a port"
        )
        return False

    logger.info(f"[explore] starting at {loc_type!r} — opening harbour")
    if not _navigate_to_harbour():
        logger.error("[explore] could not open harbour panel")
        return False
    if not _depart_from_harbour():
        logger.error("[explore] depart-from-harbour failed")
        return False

    # _depart_from_harbour already waits for the sea-view confirmation,
    # so we're at sea by the time it returns True.
    return True


def run_explore_voyage(
    side: str,
    endpoint_lat: Optional[float] = None,
    endpoint_lon: Optional[float] = None,
    max_ticks: int = 550,
    hud_every: int = 1,
    driver_mode: Optional[str] = "point_pursuit",
    waypoint_generator: str = "phase_a",
    astern_expansion: bool = False,
    graduated_density: bool = False,
    junction_picker: str = "none",
    debug_dir: Optional[Path] = None,
) -> dict:
    """Run a single explore voyage.

    Returns a dict with summary fields the task runner can surface in
    its `StepResult.notes`.
    """
    canonical_side = _SIDE_ALIASES.get((side or "").lower())
    if canonical_side is None:
        return {
            "ok": False,
            "phase": "FAILED",
            "ticks": 0,
            "notes": f"unknown side {side!r}; expected port|starboard|left|right",
        }

    if not _ensure_at_sea():
        return {
            "ok": False,
            "phase": "FAILED",
            "ticks": 0,
            "notes": "could not get to sea (departure failed)",
        }

    # Honour the env-var-based waypoint_generator gate that the CLI uses,
    # so the YAML task and the CLI invocation behave identically.
    if waypoint_generator:
        os.environ["UWO_WAYPOINT_GENERATOR"] = waypoint_generator
    # §13.30 — graduated density is opt-in.  When enabled, HugShoreGoal
    # reads this env var at config construction and bumps
    # land_fraction_ceiling + density_penalty_weight on its VFHPlusConfigs.
    os.environ["UWO_GRADUATED_DENSITY"] = "1" if graduated_density else "0"

    from brain.goals.hug_shore import run_hug_shore_loop

    if debug_dir is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_dir = Path(f"data/sessions/explore_{canonical_side}_{stamp}")
    debug_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"[explore] voyage params: side={canonical_side} "
                f"dest=({endpoint_lat},{endpoint_lon}) "
                f"max_ticks={max_ticks} hud_every={hud_every} "
                f"driver_mode={driver_mode} debug_dir={debug_dir}")

    goal = run_hug_shore_loop(
        side=canonical_side,
        max_ticks=max_ticks,
        hud_every=hud_every,
        endpoint_lat=endpoint_lat,
        endpoint_lon=endpoint_lon,
        driver_mode=driver_mode,
        astern_expansion=astern_expansion,
        junction_picker=junction_picker,
        debug_dir=debug_dir,
    )

    phase_name = goal.phase.name
    ok = phase_name == "COMPLETE"
    return {
        "ok": ok,
        "phase": phase_name,
        "ticks": goal.tick_count,
        "debug_dir": str(debug_dir),
        "notes": f"phase={phase_name} ticks={goal.tick_count}",
    }
