"""Apply HugShoreGoal actions to a simulated World, advance the ship,
detect collisions.

The action strings produced by `HugShoreGoal.tick()` are matched here:

    "hold"                          → no rudder, ship sails forward at heading
    "hold_left:NNNms(~AA°@RR°/s)"   → rotate left during NNN ms while sailing
    "hold_right:NNNms(~AA°@RR°/s)"  → rotate right
    "wait" / "stop" / unrecognised  → no-op for the dynamics

We use the *true* `Ship.rotation_rate_dps` as the plant rate, not the
hold's commanded `RR°/s` (which is the policy's *estimate*).  That
asymmetry is the whole point — it's what online calibration is for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .world import World
from .perception import synthesize_nav_view


_HOLD_RE = re.compile(r'^hold_(left|right):(\d+)ms')


@dataclass
class TickRecord:
    """One simulated tick — what the bot saw, what it decided, what
    actually happened.  Useful for replay & visualization."""
    tick: int
    sim_time_s: float
    ship_x: float
    ship_y: float
    ship_heading_before: float
    nav: object                       # SimNavView (NavigationView-shaped)
    action: str
    phase: str
    note: str
    ship_heading_after: float
    collided: bool


def apply_action(world: World, action: str) -> None:
    """Update `world.ship` in place according to `action`.

    Time advances by however long the action takes (1 tick of "hold"
    advances by `HOLD_TICK_S` of pure sailing; a hold gesture advances
    by its own duration).  No external `dt` parameter — duration is
    derived from the action string.
    """
    if not action or action in ("wait", "stop"):
        # No motion this tick — perception-only or terminal.
        return

    if action == "hold":
        # Pure forward sail at current heading for one perception tick.
        _advance(world, seconds=HOLD_TICK_S)
        return

    m = _HOLD_RE.match(action)
    if m:
        side = m.group(1)
        ms = int(m.group(2))
        seconds = ms / 1000.0
        sign = -1.0 if side == "left" else +1.0
        # Rotate + sail forward simultaneously, in small substeps so the
        # ship's path curves rather than turning-in-place.
        _hold_for_duration(world, seconds, sign)
        return

    # Unrecognised action — log nothing, just don't move.


HOLD_TICK_S: float = 1.0
"""Seconds of forward sailing for a `"hold"` (no-rudder) tick.  Pick a
value that matches the live perception cadence (~1 s)."""

_SUBSTEPS_PER_SECOND: int = 20
"""How finely to subdivide a hold gesture into rotation+motion steps.
20 substeps/sec → 50 ms each — good enough that the curving path
doesn't visibly chord."""


def _hold_for_duration(world: World, seconds: float, rotation_sign: float) -> None:
    """Rotate `world.ship` at the plant's rotation rate while
    simultaneously sailing forward.  Stops early if the ship enters
    land (collision)."""
    n_substeps = max(1, int(seconds * _SUBSTEPS_PER_SECOND))
    dt = seconds / n_substeps
    for _ in range(n_substeps):
        world.ship.heading_deg = (
            world.ship.heading_deg
            + rotation_sign * world.ship.rotation_rate_dps * dt
        ) % 360.0
        if not _advance(world, seconds=dt):
            return   # collided — stop substepping


def _advance(world: World, seconds: float) -> bool:
    """Move the ship forward by `seconds` of sailing.  Returns False
    if the new position would be inside a shore polygon (collision)."""
    import math
    ship = world.ship
    rad = math.radians(ship.heading_deg)
    dx = math.sin(rad) * ship.speed * seconds
    dy = math.cos(rad) * ship.speed * seconds
    new_x = ship.x + dx
    new_y = ship.y + dy
    # Check collision via ShapelyPoint containment.
    from shapely.geometry import Point as ShapelyPoint
    test_pt = ShapelyPoint(new_x, new_y)
    if any(poly.contains(test_pt) for poly in world.shore):
        world.collided = True
        return False
    ship.x = new_x
    ship.y = new_y
    world.sim_time += seconds
    return True


# ─────────────────────────────────────────────────────────────────────
# Top-level runner — load a world + goal, advance N ticks, record.
# ─────────────────────────────────────────────────────────────────────

def run_sim(world: World, goal, max_ticks: int = 60) -> List[TickRecord]:
    """Drive the policy in the simulator.

    Returns a list of `TickRecord` — one per tick — including the
    perception view, the policy's action, and the resulting ship
    state.  Stops early on `goal.is_complete` or on collision.
    """
    from brain import observation as _obs

    trail: List[TickRecord] = []
    world.push_trail()

    for t in range(max_ticks):
        nav = synthesize_nav_view(world)
        _obs.update(nav=nav, frame_id=f"sim-t{t}")

        heading_before = world.ship.heading_deg
        result = goal.tick()
        apply_action(world, result.action)
        world.push_trail()

        trail.append(TickRecord(
            tick=t + 1,
            sim_time_s=world.sim_time,
            ship_x=world.ship.x,
            ship_y=world.ship.y,
            ship_heading_before=heading_before,
            nav=nav,
            action=result.action,
            phase=result.phase.name,
            note=result.note,
            ship_heading_after=world.ship.heading_deg,
            collided=world.collided,
        ))

        if world.collided:
            break
        if getattr(goal, "is_complete", False):
            break

    return trail
