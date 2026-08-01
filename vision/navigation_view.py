"""Source-agnostic navigation perception.

The bot's steering logic consumes one `NavigationView` per tick.  Two
backends implement this Protocol:

    MinimapNavigationView   — image processing on the radar crop.
    YoloNavigationView      — YOLO bbox detection on the 3D scene.

A config flag in brain/perceive.py picks which backend is active per
tick.  See docs/navigation_view_design.md for the design rationale.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


# ── Sector convention ─────────────────────────────────────────────────────
# 16 uniform 22.5° sectors, ship-relative.  Bearing 0 = dead ahead.
#
# §13.20 migration (2026-06-02): bumped from 8 sectors (45° wide each) to
# 16 sectors (22.5° wide).  At 8 sectors, adjacent sectors differ by 45°
# so any picker tiebreak between bow-stbd and bow-port forces a 90° swing,
# and narrow channels with walls 13-21 px out report `dist` in all of
# {0, 45, 90} as masked even when a narrow wedge between them is clear.
# Simulation on t15 of hug_debug_20260601_144548 showed the +67.5° wedge
# (idx 3 of 16) had frac=0.00 / dist=None where the 8-sector picker was
# forced to commit -90° away from goal.  See
# `docs/heading_pca_yellow_anchor.md` and the §13.18 spec.
#
# Yim & Park 2014 ("Analysis of mobile robot navigation using VFH") found
# VFH degrades rapidly below ~36 sectors and breaks in narrow paths
# below ~18.  16 sits just under that floor — a deliberate step rather
# than a final number; classical VFH uses 72 sectors of 5°.

SECTOR_COUNT = 16
SECTOR_WIDTH_DEG = 360.0 / SECTOR_COUNT       # 22.5

SECTOR_LABELS = {
    0:   "ahead",
    45:  "ahead-starboard",
    90:  "starboard",
    135: "astern-starboard",
    180: "astern",
    225: "astern-port",
    270: "port",
    315: "ahead-port",
}


# ── Public dataclasses ────────────────────────────────────────────────────


@dataclass(frozen=True)
class SectorReading:
    """One angular sector around the ship, ship-relative.

    `bearing_deg` is the sector's centre bearing relative to the ship's
    bow.  0 = dead ahead, 90 = starboard beam, 270 = port beam.
    """
    bearing_deg:   float
    land_fraction: float            # 0..1
    nearest_dist:  Optional[float]  # 0..1, normalized to view radius; None if no land
    is_observed:   bool             # False = source can't see this sector

    @property
    def label(self) -> str:
        return SECTOR_LABELS.get(int(round(self.bearing_deg)) % 360, f"{self.bearing_deg:.0f}°")


@dataclass(frozen=True)
class Target:
    """A point of interest detected on the navigable view."""
    kind:        str                # "port_known" | "port_unvisited" | "village"
    bearing_deg: float              # ship-relative
    distance:    float              # 0..1, normalized to view radius
    name:        Optional[str] = None


# ── Protocol ──────────────────────────────────────────────────────────────


@runtime_checkable
class NavigationView(Protocol):
    """Source-agnostic spatial view.  Both image-processing (mini-map)
    and ML-detection (YOLO 3D scene) implement this.

    Consumers must check `is_observed` on each sector — partial-view
    backends mark unseen sectors as not observed rather than
    fabricating zero-land values.
    """
    ship_heading_deg: Optional[float]
    sectors:          tuple[SectorReading, ...]
    targets:          tuple[Target, ...]

    def is_reachable(self, target: Target) -> bool: ...


# ── Helpers ───────────────────────────────────────────────────────────────


def sector_index_for_bearing(bearing_deg: float) -> int:
    """Map an arbitrary bearing (0-360) to a sector index 0..7.
    Bearings within ±SECTOR_WIDTH/2 of sector 0 (ahead) land in 0, etc.
    """
    half = SECTOR_WIDTH_DEG / 2
    shifted = (bearing_deg + half) % 360
    return int(shifted // SECTOR_WIDTH_DEG)


def empty_sectors(observed_indices: set[int] | None = None) -> tuple[SectorReading, ...]:
    """Construct a default sector tuple — all empty, optionally with
    selected indices flagged as observed.  Useful for backends that
    only cover a partial arc (YOLO).
    """
    if observed_indices is None:
        observed_indices = set(range(SECTOR_COUNT))
    out = []
    for i in range(SECTOR_COUNT):
        bearing = i * SECTOR_WIDTH_DEG
        out.append(SectorReading(
            bearing_deg=bearing,
            land_fraction=0.0,
            nearest_dist=None,
            is_observed=(i in observed_indices),
        ))
    return tuple(out)
