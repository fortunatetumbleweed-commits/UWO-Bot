# brain/explore_trigger.py
#
# During world-map navigation the bot occasionally passes near ports it has
# never departed from.  This module decides whether to take a detour to explore
# one of those ports.
#
# Heuristic:
#   - Every N=5 voyages, allow one detour.
#   - A detour port must appear in the world-map OCR near the current sail path.
#   - Prefer ports that are geographically close (appear often in the OCR scan)
#     and that have never been visited as a departure point.
#
# The caller (task_runner or sail_actions) passes a list of port names that
# were OCR'd from the world map, and this module returns a detour candidate
# (or None if no detour should be taken now).
#
# This is intentionally a thin advisory layer.  It does not execute any actions.
#
# Usage:
#   from brain.explore_trigger import ExploreTrigger
#   trigger = ExploreTrigger()
#   detour = trigger.maybe_detour(ocr_ports=["Azores","Seville","Cadiz"],
#                                  current_route=["London","Lisboa","Las Palmas"])
#   if detour:
#       logger.info(f"Detour candidate: {detour}")

from __future__ import annotations

from pathlib import Path
import json
from typing import Optional

from loguru import logger
from memory.port_graph import PortGraph
from memory.voyage_log import load_rounds

_STATE_FILE = Path(__file__).parent.parent / "memory" / "knowledge" / "explore_state.json"

# How many voyages between detours
_DETOUR_INTERVAL = 5


class ExploreTrigger:
    """
    Tracks voyage count and decides when to take an exploration detour.
    State is persisted in memory/knowledge/explore_state.json.
    """

    def __init__(self) -> None:
        self._graph = PortGraph.build()
        self._state = _load_state()

    def maybe_detour(
        self,
        ocr_ports: list[str],
        current_route: list[str],
    ) -> Optional[str]:
        """
        Given port names visible on the world map and the current planned route,
        return a detour port name if we should explore one, else None.

        Parameters
        ----------
        ocr_ports      : port names OCR'd from the current world-map view
        current_route  : ordered waypoints of the planned route (to exclude them)
        """
        rounds_completed = len(load_rounds())
        last_detour = self._state.get("voyages_at_last_detour", 0)
        due = (rounds_completed - last_detour) >= _DETOUR_INTERVAL

        if not due:
            remaining = _DETOUR_INTERVAL - (rounds_completed - last_detour)
            logger.debug(
                f"  [explore] Detour not due yet — {remaining} voyage(s) remaining"
            )
            return None

        # Ports not in the current planned route
        route_lower = {p.lower() for p in current_route}
        candidates = [p for p in ocr_ports if p.lower() not in route_lower]

        if not candidates:
            logger.debug("  [explore] No off-route ports visible — no detour")
            return None

        # Prefer ports we have never departed from
        unvisited = self._graph.unvisited_neighbors("__any__", candidates)
        # unvisited_neighbors checks departures; rebuild manually for "any departure"
        visited_departures = {
            leg["from"].strip().lower()
            for leg in _all_legs()
            if leg.get("ok")
        }
        truly_unvisited = [
            p for p in candidates
            if p.strip().lower() not in visited_departures
        ]

        pick = truly_unvisited[0] if truly_unvisited else candidates[0]
        logger.info(
            f"  [explore] Detour candidate: {pick!r}  "
            f"(unvisited={bool(truly_unvisited)}, {rounds_completed} rounds completed)"
        )
        return pick

    def record_detour_taken(self) -> None:
        """Call this after a detour is actually executed."""
        rounds_completed = len(load_rounds())
        self._state["voyages_at_last_detour"] = rounds_completed
        _save_state(self._state)
        logger.info(f"  [explore] Detour recorded at voyage #{rounds_completed}")

    def stats(self) -> dict:
        rounds_completed = len(load_rounds())
        last_detour = self._state.get("voyages_at_last_detour", 0)
        return {
            "rounds_completed":         rounds_completed,
            "voyages_since_last_detour": rounds_completed - last_detour,
            "detour_interval":          _DETOUR_INTERVAL,
            "detour_due":               (rounds_completed - last_detour) >= _DETOUR_INTERVAL,
        }


# ── State persistence ──────────────────────────────────────────────────────────

def _load_state() -> dict:
    if not _STATE_FILE.exists():
        return {}
    try:
        return json.loads(_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2))


def _all_legs() -> list[dict]:
    from memory.voyage_log import load_legs
    return load_legs()
