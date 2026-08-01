"""V11-based perception adapter for the closed-loop sim.

The sim's existing `live` perception_mode uses production's
`MinimapNavigationView` for every output.  This module exposes the same
shape but with V11's brightness-threshold + channel-extraction water
mask substituted into the sector computation — so the sim can run
HugShoreGoal against V11 perception and we can compare trajectories
against the `live` (production) baseline.

The V11 mask differs from production's `water_mask` in two ways the sim
will feel:
  1. **No sprite punch-holes** — V11 does not subtract sprite pixels,
     so the ship's own sprite + sonar fan are NOT carved out.  The
     mask near the ship represents real channel topology.
  2. **Channel polygon, not all water** — V11's CC extraction keeps
     the largest water region (and ≥ 2000 px secondaries).  Spurious
     small water blobs in the land area are dropped.

What we keep from production:
  - Ship centroid detection (`_clean_ship_green` + permissive expand).
  - Nav-area mask (`_nav_area_mask`).
  - Sector binning algorithm (`_compute_sectors`).

What we use from V11:
  - The water mask itself.
  - Land = `nav & ~water` for sector computation.
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
from PIL import Image

from tools.perception_v10_channel_prototype import v11_brightness_channel_mask
from vision.minimap_navigation_view import (
    _bbox_permissive_expand,
    _clean_ship_green,
    _color_masks,
    _compute_sectors,
    _nav_area_mask,
    _ship_centroid,
)
from vision.navigation_view import SectorReading


def v11_perception(
    crop: Image.Image,
    heading_deg: Optional[float] = None,
) -> Tuple[np.ndarray, Optional[Tuple[float, float]], Tuple[SectorReading, ...]]:
    """V11 perception on a 381×184 minimap crop.

    Args:
      crop:        PIL RGB Image at the minimap crop size.
      heading_deg: bow direction in degrees.  When supplied, sectors are
                   rotated into ship-relative coords; when None, sectors
                   stay in compass coords (caller can detect).

    Returns:
      water_mask  — V11 channel mask (bool array, same shape as crop).
      ship_xy     — (x, y) tuple in crop coords, or None on detection
                    failure.
      sectors     — tuple of `SectorReading` from the standard sector
                    binner applied to V11's land mask (nav & ~water).
    """
    rgb = np.asarray(crop)
    color = _color_masks(rgb)
    ship_green = _clean_ship_green(color["ship_green"])
    ship_green = _bbox_permissive_expand(ship_green, rgb)
    ship_green = _clean_ship_green(ship_green)
    ship_xy = _ship_centroid(ship_green)

    water, _ = v11_brightness_channel_mask(rgb)
    nav = _nav_area_mask(rgb.shape[:2])
    land = nav & ~water

    sectors = _compute_sectors(land, ship_xy, nav, heading_deg)
    return water, ship_xy, sectors
