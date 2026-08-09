"""Populate the world model from perception.

Each tick's `PerceivedState` reliably carries *where* the bot is (at sea / at a
port / inside which building) — this updater folds that into the world model's
active fleet and learns newly-seen ports. Fields that only appear on specific
screens (currencies, crew, supply, ship life, cargo) are passed in by the
specialized HUD readers that extract them, when available.

See docs/bot_architecture_layers.md (Layer 2). This is the "world model reads/
updates from PerceivedState" step.
"""
from __future__ import annotations

from typing import Optional

from brain.perceived_state import (
    PerceivedState, BASE_OVERWORLD, BASE_PANEL, MODE_SEA, MODE_PORT,
)
from brain.world_model import WorldModel, Fleet, Ship

AT_SEA = "at_sea"


def _ensure_fleet(wm: WorldModel) -> Fleet:
    if wm.fleet is None:
        wm.fleets.append(Fleet())
        wm.active_fleet_idx = len(wm.fleets) - 1
    return wm.fleet


def update_from_perceived(
    wm: WorldModel,
    perceived: Optional[PerceivedState],
    *,
    port_name: Optional[str] = None,
) -> WorldModel:
    """Update the active fleet's location / current_building from the structured
    state, and learn any newly-seen port. Transient screens (world_map, loading)
    leave location untouched.

    `port_name` overrides the port identity when the caller has a cleaner read
    (e.g. the classifier's corrected port name); otherwise the perceived
    identity/context is used.
    """
    if perceived is None:
        return wm
    fleet = _ensure_fleet(wm)
    base = perceived.base

    if base == BASE_OVERWORLD:
        fleet.current_building = None            # not inside a building
        if perceived.mode == MODE_SEA:
            fleet.location = AT_SEA
        elif perceived.mode == MODE_PORT:
            port = port_name or perceived.identity or perceived.context
            if port:
                fleet.location = port
                _remember_port(wm, port)
    elif base == BASE_PANEL:
        # inside a building at a port — keep the port location, set the building
        if perceived.context:
            fleet.current_building = perceived.context
    # BASE_WORLD_MAP / BASE_LOADING / unknown → transient; leave location as-is
    return wm


def _remember_port(wm: WorldModel, port: str) -> None:
    if port and port != AT_SEA and port not in wm.discovered_ports:
        wm.discovered_ports.append(port)


# ── optional field updates from specialized HUD readers ───────────────────────
# These are called by whatever extracts each figure (top-bar currencies, the
# fleet/depart panel for crew, the market for cargo, the ship-status/shipyard for
# ship life). Kept as thin, explicit setters so the readers own the extraction.

def set_currencies(wm: WorldModel, currencies: dict) -> WorldModel:
    wm.currencies.update({k: v for k, v in currencies.items() if v is not None})
    return wm


def set_crew(wm: WorldModel, current: Optional[int], capacity: Optional[int]) -> WorldModel:
    f = _ensure_fleet(wm)
    if current is not None:
        f.crew_current = current
    if capacity is not None:
        f.crew_capacity = capacity
    return wm


def set_supply_days(wm: WorldModel, days: Optional[int]) -> WorldModel:
    if days is not None:
        _ensure_fleet(wm).supply_days = days
    return wm


def set_cargo(wm: WorldModel, used: Optional[int], capacity: Optional[int],
              contents: Optional[dict] = None) -> WorldModel:
    f = _ensure_fleet(wm)
    if used is not None:
        f.cargo_used = used
    if capacity is not None:
        f.cargo_capacity = capacity
    if contents is not None:
        f.cargo = dict(contents)
    return wm


def set_ships(wm: WorldModel, ships: list) -> WorldModel:
    """ships: list of (name, life) tuples or Ship objects."""
    f = _ensure_fleet(wm)
    f.ships = [s if isinstance(s, Ship) else Ship(name=s[0], life=s[1]) for s in ships]
    return wm
