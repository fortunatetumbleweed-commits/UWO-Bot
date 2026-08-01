"""Synthesise a NavigationView from world + ship via ray casting.

8 sectors, 45° each.  Sector i covers ship-relative angles
[i·45 − 22.5, i·45 + 22.5].  We cast `RAYS_PER_SECTOR` rays evenly
across each arc, find the nearest shore intersection per ray, then
aggregate:

  - `land_fraction` = (# rays that hit land within VIEW_RADIUS) / N
  - `nearest_dist`  = (min hit distance) / VIEW_RADIUS,  None if no hit

This matches the live `MinimapNavigationView` data contract.

Sector index convention (ship-relative, NavigationView):
    0 = ahead          (heading offset    0°)
    1 = ahead-stbd     (heading offset  +45°)
    2 = starboard      (heading offset  +90°)
    3 = astern-stbd    (heading offset +135°)
    4 = astern         (heading offset +180°)
    5 = astern-port    (heading offset +225° = -135°)
    6 = port           (heading offset +270° = -90°)
    7 = ahead-port     (heading offset +315° = -45°)
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

from shapely.geometry import LineString, Point as ShapelyPoint

from vision.navigation_view import SectorReading
from .world import World


VIEW_RADIUS: float = 0.35       # how far the mini-map "sees" in world units
N_SECTORS: int = 8
RAYS_PER_SECTOR: int = 7        # sample rays across each 45° arc
_SECTOR_HALF_WIDTH: float = 22.5


def _ray_hit_distance(start: Tuple[float, float],
                      world_heading_deg: float,
                      shore_polygons) -> Optional[float]:
    """Cast one ray from `start` toward `world_heading_deg`.  Returns
    distance to nearest shore intersection within VIEW_RADIUS, or None
    if no hit."""
    # Compass: 0° = N (+y), 90° = E (+x).
    rad = math.radians(world_heading_deg)
    dx = math.sin(rad)
    dy = math.cos(rad)
    end = (start[0] + dx * VIEW_RADIUS, start[1] + dy * VIEW_RADIUS)
    ray = LineString([start, end])

    nearest = None
    for poly in shore_polygons:
        inter = poly.boundary.intersection(ray)
        if inter.is_empty:
            continue
        # Intersection may be a Point, MultiPoint, or GeometryCollection.
        for pt in _iter_intersection_points(inter):
            d = math.hypot(pt[0] - start[0], pt[1] - start[1])
            if nearest is None or d < nearest:
                nearest = d
    return nearest


def _iter_intersection_points(geom):
    """Yield (x, y) tuples from a shapely geometry, regardless of type."""
    gt = geom.geom_type
    if gt == "Point":
        yield (geom.x, geom.y)
    elif gt == "MultiPoint":
        for p in geom.geoms:
            yield (p.x, p.y)
    elif gt == "LineString":
        # Tangent — take endpoints.  Rare; mainly for edge-grazing rays.
        for coord in geom.coords:
            yield coord
    elif gt in ("MultiLineString", "GeometryCollection"):
        for sub in geom.geoms:
            yield from _iter_intersection_points(sub)
    # Other geometry types (Polygon etc.) shouldn't appear from
    # boundary.intersection(line) — silently ignore.


def synthesize_nav_view(world: World):
    """Build a NavigationView-shaped object from `world`.

    Returns an object that satisfies the `vision.navigation_view.NavigationView`
    Protocol — has `ship_heading_deg` and `sectors` attributes.
    """
    ship = world.ship
    start = (ship.x, ship.y)
    # If the ship is already inside land, every sector is "blocked".
    if world.ship_in_land():
        sectors = tuple(
            SectorReading(
                bearing_deg=float(i * 45),
                land_fraction=1.0,
                nearest_dist=0.0,
                is_observed=True,
            )
            for i in range(N_SECTORS)
        )
        return _SimNavView(ship.heading_deg, sectors)

    sectors = []
    for sector_idx in range(N_SECTORS):
        # Sector centre in ship-relative degrees (0=ahead).
        sector_center_rel = sector_idx * 45.0
        ray_dists = []
        for ray_i in range(RAYS_PER_SECTOR):
            # Offset in [-22.5, +22.5] across the sector.
            frac = ray_i / (RAYS_PER_SECTOR - 1)
            offset = (frac - 0.5) * (2 * _SECTOR_HALF_WIDTH)
            world_heading = (ship.heading_deg + sector_center_rel + offset) % 360.0
            d = _ray_hit_distance(start, world_heading, world.shore)
            ray_dists.append(d)

        hits = [d for d in ray_dists if d is not None]
        land_fraction = len(hits) / RAYS_PER_SECTOR
        nearest_dist = (min(hits) / VIEW_RADIUS) if hits else None
        sectors.append(SectorReading(
            bearing_deg=float(sector_center_rel),
            land_fraction=land_fraction,
            nearest_dist=nearest_dist,
            is_observed=True,
        ))

    return _SimNavView(ship.heading_deg, tuple(sectors))


class _SimNavView:
    """Concrete NavigationView for simulated frames.  Satisfies the
    Protocol structurally without inheriting from it (Protocol is a
    duck-type contract, not a base class)."""
    __slots__ = ("ship_heading_deg", "sectors")

    def __init__(self, ship_heading_deg: float, sectors: Tuple[SectorReading, ...]):
        self.ship_heading_deg = ship_heading_deg
        self.sectors = sectors
