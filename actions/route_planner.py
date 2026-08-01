# actions/route_planner.py
#
# Multi-hop route planning with resupply stops.
#
# The bot cannot sail indefinitely — it has a supply limit (days of sailing).
# Long voyages (e.g. Ceylon → London) require intermediate resupply stops.
#
# This module answers: "given a start and destination, what ports do I stop at?"
#
# Routes are loaded from memory/knowledge/config/routes.json.
# To add a new route or resupply stop: edit that file — no code change needed.
#
# Phase 2 (future): dynamic routing — use the port KB to compute optimal paths
# based on supply consumption, port locations, and trade opportunities en route.
#
# Usage:
#   from actions.route_planner import plan_voyage, ROUTES
#   stops = plan_voyage("Ceylon", "London")
#   # → ["Ceylon", "Hormuz", "Aden", "Lisbon", "London"]

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Route segment data ─────────────────────────────────────────────────────────

@dataclass
class RouteSegment:
    """One hop in a multi-port voyage."""
    from_port:   str
    to_port:     str
    est_days:    Optional[int] = None   # estimated voyage days (in-game)
    sea_region:  str = ""               # sea region name (for requirements)
    notes:       str = ""


@dataclass
class Route:
    """A complete route from origin to destination, possibly multi-hop."""
    name:     str
    waypoints: list[str]              # ordered list of ports, including start and end
    segments:  list[RouteSegment] = field(default_factory=list)
    notes:     str = ""

    @property
    def origin(self) -> str:
        return self.waypoints[0] if self.waypoints else ""

    @property
    def destination(self) -> str:
        return self.waypoints[-1] if self.waypoints else ""

    @property
    def intermediate_stops(self) -> list[str]:
        return self.waypoints[1:-1]


# ── Route loading ──────────────────────────────────────────────────────────────
# Routes are stored in memory/knowledge/config/routes.json.
# Bidirectional routes are stored once; plan_voyage() handles both directions.

_ROUTES_FILE = Path("memory/knowledge/config/routes.json")


def _load_routes() -> list[Route]:
    try:
        raw = json.loads(_ROUTES_FILE.read_text())
        routes: list[Route] = []
        for r in raw:
            segments = [
                RouteSegment(
                    from_port=s["from_port"],
                    to_port=s["to_port"],
                    est_days=s.get("est_days"),
                    sea_region=s.get("sea_region", ""),
                    notes=s.get("notes", ""),
                )
                for s in r.get("segments", [])
            ]
            routes.append(Route(
                name=r["name"],
                waypoints=r["waypoints"],
                segments=segments,
                notes=r.get("notes", ""),
            ))
        return routes
    except Exception as exc:
        from loguru import logger
        logger.error(f"Failed to load routes.json: {exc}")
        return []


ROUTES: list[Route] = _load_routes()


def reload_routes() -> None:
    """Reload ROUTES from disk (e.g. after editing routes.json)."""
    global ROUTES
    ROUTES = _load_routes()
    from loguru import logger
    logger.info(f"Reloaded {len(ROUTES)} routes")


# ── Route lookup ───────────────────────────────────────────────────────────────

def _normalize(port: str) -> str:
    return port.lower().strip()


def find_route(origin: str, destination: str) -> Optional[Route]:
    """
    Find the best known route from *origin* to *destination*.
    Checks both directions (bidirectional routes).
    Returns None if no route is known.
    """
    o_lower = _normalize(origin)
    d_lower = _normalize(destination)

    for route in ROUTES:
        waypoints_lower = [_normalize(w) for w in route.waypoints]
        if waypoints_lower[0] == o_lower and waypoints_lower[-1] == d_lower:
            return route
        if waypoints_lower[-1] == o_lower and waypoints_lower[0] == d_lower:
            # Return the reversed route
            return Route(
                name=route.name + " (reversed)",
                waypoints=list(reversed(route.waypoints)),
                notes=route.notes,
            )

    return None


def plan_voyage(origin: str, destination: str) -> list[str]:
    """
    Return the ordered list of ports for a voyage from *origin* to *destination*,
    including all resupply stops.

    If a known route exists, use it.  Otherwise return just [origin, destination]
    (direct sail — the caller must ensure supply is sufficient).

    Examples:
      plan_voyage("London", "Port Royal")
        → ["London", "Lisbon", "Canary Islands", "Port Royal"]

      plan_voyage("Ceylon", "Aceh")
        → ["Ceylon", "Aceh"]
    """
    route = find_route(origin, destination)
    if route:
        return route.waypoints

    # No known route — direct sail.  May run out of supply for long voyages.
    return [origin, destination]


def describe_voyage(origin: str, destination: str) -> str:
    """
    Return a human-readable description of the planned voyage.
    """
    waypoints = plan_voyage(origin, destination)
    route = find_route(origin, destination)

    if len(waypoints) == 2:
        return (
            f"{origin} → {destination}  (direct — no known resupply stops)\n"
            "  Note: ensure supply days ≥ voyage length before departing."
        )

    stops = waypoints[1:-1]
    lines = [f"{origin} → {destination}  ({len(stops)} resupply stop(s))"]
    for i, (a, b) in enumerate(zip(waypoints[:-1], waypoints[1:])):
        seg = None
        if route and route.segments:
            seg = next((s for s in route.segments
                        if _normalize(s.from_port) == _normalize(a)
                        and _normalize(s.to_port) == _normalize(b)), None)
        days = f"  ~{seg.est_days}d" if seg and seg.est_days else ""
        role = "(resupply)" if a != origin and a != destination else ""
        lines.append(f"  {'→':2s} {b}{days}  {role}".rstrip())
    if route and route.notes:
        lines.append(f"  Note: {route.notes}")
    return "\n".join(lines)


# ── Task file generator ────────────────────────────────────────────────────────

def generate_trade_task_yaml(
    origin: str,
    destination: str,
    rounds: int = 1,
) -> str:
    """
    Generate a YAML task file for a trade route from *origin* to *destination*,
    automatically inserting resupply stops where needed.

    The generated YAML can be saved to tasks/ and run with run_task.py.
    """
    waypoints = plan_voyage(origin, destination)
    route = find_route(origin, destination)

    route_notes = route.notes.replace(":", " —") if route and route.notes else ""
    stops_str = (
        f"via {', '.join(waypoints[1:-1])}"
        if len(waypoints) > 2 else "direct"
    )
    lines = [
        f'name: "{origin} to {destination} — {rounds} Round Trip"',
        f'description: "{origin} to {destination} ({stops_str})"',
        f'',
        f'start_port: {origin}',
        f'rounds: {rounds}',
        f'',
        f'loop:',
    ]

    # Forward leg
    lines.append(f'  # ── Forward leg: {origin} → {destination} ──')
    lines.append(f'  - action: sell_all')
    lines.append(f'    port: {origin}')
    lines.append(f'    note: "Sell cargo at {origin}"')
    lines.append(f'  - action: buy_all')
    lines.append(f'    port: {origin}')
    lines.append(f'    note: "Buy recommended goods at {origin}"')
    for stop in waypoints[1:]:
        lines.append(f'  - action: sail_to')
        lines.append(f'    destination: {stop}')

    # Return leg
    lines.append(f'')
    lines.append(f'  # ── Return leg: {destination} → {origin} ──')
    lines.append(f'  - action: sell_all')
    lines.append(f'    port: {destination}')
    lines.append(f'    note: "Sell cargo at {destination}"')
    lines.append(f'  - action: buy_all')
    lines.append(f'    port: {destination}')
    lines.append(f'    note: "Buy recommended goods at {destination}"')
    for stop in reversed(waypoints[:-1]):
        lines.append(f'  - action: sail_to')
        lines.append(f'    destination: {stop}')

    lines.extend([
        '',
        'report:',
        '  - profit_loss',
        '  - per_round',
        '  - unrecognized_scenes',
    ])

    return "\n".join(lines) + "\n"


# ── Standalone ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from pathlib import Path as _Path

    args = sys.argv[1:]

    # --save <file>  flag: write YAML only (no human text) to a file
    save_path = None
    if "--save" in args:
        idx = args.index("--save")
        save_path = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    if len(args) >= 2:
        origin, destination = args[0], args[1]
        rounds = int(args[2]) if len(args) > 2 else 1
        yaml_text = generate_trade_task_yaml(origin, destination, rounds)

        if save_path:
            _Path(save_path).write_text(yaml_text)
            print(f"Saved to {save_path}")
            print(describe_voyage(origin, destination))
        else:
            # Human-readable summary to stderr, YAML to stdout
            # (so `python -m actions.route_planner London "Port Royal" > file.yaml` works)
            import sys as _sys
            print(describe_voyage(origin, destination), file=_sys.stderr)
            print(yaml_text)
    else:
        print("Known routes:")
        for r in ROUTES:
            stops = len(r.waypoints) - 2
            stop_str = f" ({stops} stop{'s' if stops != 1 else ''})" if stops else " (direct)"
            print(f"  {r.waypoints[0]:20s} ↔ {r.waypoints[-1]:20s}{stop_str}")
        print()
        print("Usage: python -m actions.route_planner <origin> <destination> [rounds]")
        print("       python -m actions.route_planner <origin> <destination> [rounds] --save tasks/file.yaml")
