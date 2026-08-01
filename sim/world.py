"""World model — shore polygons + ship state.

Coordinates:
    - Unit square [0, 1] × [0, 1] (extensible if needed).
    - +x = east, +y = north (compass-positive).
    - Headings in degrees: 0° = North, 90° = East.  Matches
      `vision.navigation_view.NavigationView.ship_heading_deg`.

Shore polygons are `shapely.geometry.Polygon` instances so that
collision tests, ray casting, and "point in land" queries all
share the same well-tested geometry library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from shapely.geometry import Polygon as ShapelyPolygon
from shapely.geometry import Point as ShapelyPoint


# Re-export shapely's Polygon under a friendlier name so callers don't
# have to import shapely directly.  Factory helpers below convert
# vertex lists to ShapelyPolygon instances.
Polygon = ShapelyPolygon


@dataclass
class Ship:
    """Pose + dynamics of the simulated ship.

    Headings: 0° = North.  Speed and rotation rate are the "true plant"
    we want the policy to discover via online calibration.
    """
    x: float
    y: float
    heading_deg: float                 # 0 = north, compass convention
    speed: float = 0.02                # world-units per second
    rotation_rate_dps: float = 60.0    # true plant rotation when holding

    def pos(self) -> Tuple[float, float]:
        return (self.x, self.y)


@dataclass
class World:
    """A simulated environment.

    `shore` is a list of polygons (land masses).  Multiple polygons
    are allowed so we can model archipelagos.
    """
    shore: List[Polygon]
    ship: Ship
    bounds: Tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    sim_time: float = 0.0
    collided: bool = False
    # Tick history — pushed by run_sim().  Useful for rendering &
    # for tests that check "did the ship ever enter region X?"
    trail: List[Tuple[float, float, float]] = field(default_factory=list)

    def ship_in_land(self) -> bool:
        """Is the ship currently inside any shore polygon?"""
        p = ShapelyPoint(self.ship.x, self.ship.y)
        return any(poly.contains(p) for poly in self.shore)

    def push_trail(self) -> None:
        self.trail.append((self.ship.x, self.ship.y, self.ship.heading_deg))


# ── Polygon factory helpers ──────────────────────────────────────────

def rect(x0: float, y0: float, x1: float, y1: float) -> Polygon:
    """Axis-aligned rectangle as a shore polygon."""
    return ShapelyPolygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def half_plane(side: str, edge: float) -> Polygon:
    """Half-plane shore (everything on `side` of `edge`).

        side="south", edge=0.3  → all points with y <= 0.3 are land
        side="north", edge=0.7  → all points with y >= 0.7 are land
        side="west",  edge=0.3  → all points with x <= 0.3 are land
        side="east",  edge=0.7  → all points with x >= 0.7 are land
    """
    # We use the world bounds [-1, 2] (extended) so the half-plane
    # extends well past the simulation area.
    if side == "south":
        return ShapelyPolygon([(-1, -1), (2, -1), (2, edge), (-1, edge)])
    if side == "north":
        return ShapelyPolygon([(-1, edge), (2, edge), (2, 2), (-1, 2)])
    if side == "west":
        return ShapelyPolygon([(-1, -1), (edge, -1), (edge, 2), (-1, 2)])
    if side == "east":
        return ShapelyPolygon([(edge, -1), (2, -1), (2, 2), (edge, 2)])
    raise ValueError(f"side must be one of south/north/west/east, got {side!r}")
