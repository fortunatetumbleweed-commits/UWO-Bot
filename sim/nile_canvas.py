"""Stitch a Nile world canvas from a real voyage's mini-map crops.

Each saved `tick_NNNN.png` is a 405×193 mini-map centered on the ship
at the recorded (lat, lon).  We paste all of them into one big
canvas in world coordinates and blend by running mean, masking out
the ship sprite + sonar fan so they don't burn into the world.

Two outputs:
  - `nile_rgb.png`   — stitched RGB world (looks like a giant mini-map)
  - `nile_water.png` — boolean water mask at the same scale, from V11

Both are indexed by world → pixel via a linear transform:
    px_x = (lon - lon_origin) * px_per_deg_lon
    px_y = (lat_origin - lat) * px_per_deg_lat   # y grows south

`lat_origin`, `lon_origin`, `px_per_deg_lat`, `px_per_deg_lon` are
saved alongside as `nile_canvas.json`.

Ship / sonar sprite masking:
  - Ship pixels: strict green rule `g>110 & g-r>60 & g-b>50` (same
    as ship-only CNN training).
  - Sonar pixels: yellow, `r>180 & g>150 & (r-g)<40 & b<130`.
  - Any pixel matching either is excluded from the RGB blend on that
    tick.  Since the ship is centered every tick, the naive stitch
    would burn a permanent ship into the canvas — masking prevents
    that.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image
from scipy import ndimage


DEFAULT_PX_PER_DEG = 100.0     # empirically measured — mini-map ≈ 2° tall
MINIMAP_W, MINIMAP_H = 405, 193
# Only trust each frame's V11 water-mask within this pixel radius of the
# ship centre.  Edges of the mini-map are far from the ship and
# contribute mis-scaled pixels that produce horizontal-band artefacts
# when unioned across the trajectory.  60 px ≈ 0.6° ≈ 66 km at 100 px/°.
TRUST_RADIUS_PX = 60
# Filter out lat/lon OCR spikes that would blow up the world size.
# Cairo → Lake Victoria voyages are in ~0-31° lat, ~28-36° lon.
LAT_MIN, LAT_MAX = -5.0, 32.0
LON_MIN, LON_MAX = 24.0, 40.0


@dataclass
class CanvasMeta:
    """Coordinate transform metadata for the stitched world canvas."""
    lat_origin: float             # world lat at pixel y=0
    lon_origin: float             # world lon at pixel x=0
    px_per_deg_lat: float
    px_per_deg_lon: float
    width: int
    height: int
    source_session: str
    source_ticks: int

    def world_to_px(self, lat: float, lon: float) -> tuple[int, int]:
        x = int(round((lon - self.lon_origin) * self.px_per_deg_lon))
        y = int(round((self.lat_origin - lat) * self.px_per_deg_lat))
        return x, y


# ── sprite masks ─────────────────────────────────────────────────────

def _ship_mask(rgb: np.ndarray) -> np.ndarray:
    r = rgb[..., 0].astype(np.int16)
    g = rgb[..., 1].astype(np.int16)
    b = rgb[..., 2].astype(np.int16)
    return (g > 110) & ((g - r) > 60) & ((g - b) > 50)


def _sonar_mask(rgb: np.ndarray) -> np.ndarray:
    r = rgb[..., 0].astype(np.int16)
    g = rgb[..., 1].astype(np.int16)
    b = rgb[..., 2].astype(np.int16)
    return (r > 180) & (g > 150) & ((r - g) < 40) & (b < 130)


def _v11_water_mask(rgb: np.ndarray) -> np.ndarray:
    """V11-style brightness segmentation on a mini-map crop.

    Compatible with `brain/ai_nav/layers/segmentation.py` — water is
    dark bluish, land is brighter.  Sprites are NOT water.
    """
    r = rgb[..., 0].astype(np.int16)
    g = rgb[..., 1].astype(np.int16)
    b = rgb[..., 2].astype(np.int16)
    brightness = (r + g + b) / 3.0
    blueish = (b >= r) & (b >= g - 10)
    water = (brightness < 120) & blueish
    sprites = _ship_mask(rgb) | _sonar_mask(rgb)
    return water & ~sprites


# ── main entry point ─────────────────────────────────────────────────

def build_canvas(
    session_dir: Path,
    px_per_deg: float = DEFAULT_PX_PER_DEG,
    trust_radius_px: int = TRUST_RADIUS_PX,
    lat_bounds: tuple[float, float] = (LAT_MIN, LAT_MAX),
    lon_bounds: tuple[float, float] = (LON_MIN, LON_MAX),
) -> tuple[np.ndarray, np.ndarray, CanvasMeta]:
    """Stitch a world canvas from a voyage session.

    Uses union-blend within a `trust_radius_px` disc around each frame's
    ship centre.  See module docstring for why this beats full-frame
    mean-blend.

    Returns (rgb_canvas HxWx3 uint8, water_canvas HxW bool, meta).
    The RGB canvas is a rough visual reference; the *authoritative*
    ground truth for the sim is the boolean `water` mask.
    """
    session_dir = Path(session_dir)
    trace = [json.loads(l) for l in open(session_dir / "trace.jsonl")]

    # Filter to on-map ticks (drop OCR spikes)
    def in_bounds(t):
        lat, lon = t.get("lat"), t.get("lon")
        return (lat is not None and lon is not None
                and lat_bounds[0] <= lat <= lat_bounds[1]
                and lon_bounds[0] <= lon <= lon_bounds[1])
    ticks = [t for t in trace if in_bounds(t)]
    if not ticks:
        raise ValueError(f"No in-bounds ticks in {session_dir}")

    lats = [t["lat"] for t in ticks]
    lons = [t["lon"] for t in ticks]
    # World box has to cover mini-map extent AROUND each tick's centre.
    half_lat = (MINIMAP_H / 2) / px_per_deg
    half_lon = (MINIMAP_W / 2) / px_per_deg
    lat_origin = max(lats) + half_lat        # top edge
    lat_min_world = min(lats) - half_lat
    lon_origin = min(lons) - half_lon        # left edge
    lon_max_world = max(lons) + half_lon

    W = int(math.ceil((lon_max_world - lon_origin) * px_per_deg))
    H = int(math.ceil((lat_origin - lat_min_world) * px_per_deg))
    print(f"[nile_canvas] {len(ticks)} in-bounds ticks; "
          f"canvas {W}×{H} px (≈{W*H/1e6:.1f} MP)")

    accum = np.zeros((H, W, 3), dtype=np.float64)
    count = np.zeros((H, W), dtype=np.int32)
    water_union = np.zeros((H, W), dtype=bool)

    # Disc mask: only pixels within trust_radius_px of ship centre in
    # each frame contribute to the world canvas.
    yy, xx = np.mgrid[:MINIMAP_H, :MINIMAP_W]
    cy_local, cx_local = MINIMAP_H // 2, MINIMAP_W // 2
    trust_disc = ((yy - cy_local) ** 2
                  + (xx - cx_local) ** 2) <= trust_radius_px ** 2

    meta = CanvasMeta(
        lat_origin=lat_origin,
        lon_origin=lon_origin,
        px_per_deg_lat=px_per_deg,
        px_per_deg_lon=px_per_deg,   # rough — ignores longitude convergence
        width=W, height=H,
        source_session=session_dir.name,
        source_ticks=len(ticks),
    )

    kept = 0
    for t in ticks:
        crop_path = session_dir / f"tick_{t['tick']:04d}.png"
        if not crop_path.exists():
            continue
        img = np.asarray(Image.open(crop_path).convert("RGB"))
        H_img, W_img = img.shape[:2]
        if (H_img, W_img) != (MINIMAP_H, MINIMAP_W):
            # Rescale if the crop dims drifted (e.g. old sessions saved
            # a slightly different crop size).
            img = np.asarray(
                Image.fromarray(img).resize((MINIMAP_W, MINIMAP_H))
            )
        sprite_mask = _ship_mask(img) | _sonar_mask(img)
        water_mask = _v11_water_mask(img) & trust_disc
        # Ship centre is always on water — anchor that fact
        water_mask[cy_local - 3:cy_local + 4,
                   cx_local - 3:cx_local + 4] = True

        cx, cy = meta.world_to_px(t["lat"], t["lon"])
        # Paste box in world canvas
        y0, y1 = cy - MINIMAP_H // 2, cy - MINIMAP_H // 2 + MINIMAP_H
        x0, x1 = cx - MINIMAP_W // 2, cx - MINIMAP_W // 2 + MINIMAP_W
        # Clip to canvas
        ty0 = max(0, -y0); ty1 = MINIMAP_H - max(0, y1 - H)
        tx0 = max(0, -x0); tx1 = MINIMAP_W - max(0, x1 - W)
        wy0 = max(0, y0);  wy1 = min(H, y1)
        wx0 = max(0, x0);  wx1 = min(W, x1)
        if wy1 <= wy0 or wx1 <= wx0:
            continue

        # RGB accumulation — mask out sprites
        m = ~sprite_mask[ty0:ty1, tx0:tx1]
        patch = img[ty0:ty1, tx0:tx1].astype(np.float64)
        for c in range(3):
            accum[wy0:wy1, wx0:wx1, c] += patch[..., c] * m
        count[wy0:wy1, wx0:wx1] += m

        # Water mask: union within the trust disc
        water_union[wy0:wy1, wx0:wx1] |= water_mask[ty0:ty1, tx0:tx1]
        kept += 1

    print(f"[nile_canvas] blended {kept} crops")

    with np.errstate(invalid="ignore"):
        rgb = (accum / np.maximum(count[..., None], 1)).astype(np.uint8)
    unseen = count == 0
    rgb[unseen] = 0  # leave black where no data
    # Light closing knits any 1-2 px scroll gaps in the union mask.
    water = ndimage.binary_closing(water_union, iterations=2)
    # Fill any land pockets fully enclosed by water — these are V11
    # false-positive islands the sim would ground on.  Real islands
    # would be re-added later from a curated source; for the trunk
    # Nile every internal void is an artefact.
    water = ndimage.binary_fill_holes(water)
    return rgb, water, meta


def save_canvas(
    rgb: np.ndarray, water: np.ndarray, meta: CanvasMeta, out_dir: Path,
) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb).save(out_dir / "nile_rgb.png")
    Image.fromarray((water.astype(np.uint8) * 255)).save(
        out_dir / "nile_water.png"
    )
    with open(out_dir / "nile_canvas.json", "w") as f:
        json.dump(asdict(meta), f, indent=2)
    print(f"[nile_canvas] saved to {out_dir}")


def load_canvas(canvas_dir: Path):
    canvas_dir = Path(canvas_dir)
    rgb = np.asarray(Image.open(canvas_dir / "nile_rgb.png").convert("RGB"))
    water = (np.asarray(Image.open(canvas_dir / "nile_water.png")) > 128)
    with open(canvas_dir / "nile_canvas.json") as f:
        meta = CanvasMeta(**json.load(f))
    return rgb, water, meta
