"""Closed-loop Nile simulator.

Uses the world canvas built by `tools.build_nile_canvas` as a static
water/land map, plus simple ship kinematics + collision, to drive
`AiNavPipeline` end-to-end without ADB.

Design:
  - Ground-truth perception (SimHeading, SimSegmentation) — the sim
    knows the ship's true heading and the world's true water mask, so
    the pipeline gets those directly.  The rendered mini-map is only
    for the viewer / for future perception-in-the-loop experiments.
  - Physics: 120°/sec turn calibration; forward motion at `speed_kt`
    with first-order lag (τ ≈ 1.5 s → 95% of cruise in 5 s).
  - Collision: proposed next position sampled on water_mask.  If land,
    stay put + speed → 0 + heading += U(90°, 270°) (bounce).
  - Rendering: crop the world water mask at ship position, paint
    water blue / land tan, composite a green ship sprite at centre.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from brain.ai_nav.layers.heading import Heading, HeadingLayer
from brain.ai_nav.state import NavState
from brain.ai_nav.vision_input import VisionFrame, VisionSource
from sim.nile_canvas import (
    MINIMAP_H, MINIMAP_W, load_canvas, CanvasMeta,
)


# ── constants ────────────────────────────────────────────────────────

TURN_RATE_DPS = 120.0            # steering calibration (real game)
CRUISE_KT = 8.0                  # target sailing speed
# Game time is highly accelerated: live voyage advances ~0.05° lat per
# tick at ~8 kt (real ships at 8 kt = 0.514 m/s = 4.6e-6 deg/s).
# Calibrated from voyage 2026-07-15: ~0.006 deg/(s·kt).
KT_TO_DEG_PER_S = 0.006
SPEED_TAU_S = 1.5                # 4-5 s to full speed after collision
BOUNCE_TURN_DEG_MIN = 90.0
BOUNCE_TURN_DEG_MAX = 270.0

# Screen geometry — where the mini-map lives on the 2400×1080 full frame,
# so the sim's rendered frame passes through the pipeline as-if from ADB.
# Must match brain/ai_nav/vision_input.MINIMAP_CROP.
# Where the simulator PASTES its rendered mini-map. This is a drawing position, not a
# perception one, but it must agree with where perception looks or the sim is not faithful —
# so it follows the canonical crop rather than keeping its own literal (which had drifted to
# (1984, 202, 2389, 395), 3px up and 10px wider than what the nav code actually crops).
from vision.minimap_navigation_view import get_minimap_crop as _get_minimap_crop

_MINIMAP_CROP = _get_minimap_crop()

_WATER_RGB = np.array([50, 90, 130], dtype=np.uint8)
_LAND_RGB  = np.array([160, 145, 125], dtype=np.uint8)

# Real game ship sprite, extracted from a live tick with heading ~0°
# (bow up).  We rotate it per sim heading before compositing so that
# the same CNN + PCA that ship in production recognise the sim ship.
# See data/heading_sprites_v6plus_2026-07-02/manifest.jsonl.
_SHIP_SPRITE_PATH = (Path(__file__).parent.parent
                     / "data/heading_sprites_v6plus_2026-07-02"
                     / "sprite_000015.png")
_SHIP_SPRITE_HEADING_DEG = 0.84   # canonical heading of the source sprite


# ── sim state + physics ──────────────────────────────────────────────

@dataclass
class SimState:
    lat: float
    lon: float
    heading_deg: float
    speed_kt: float = CRUISE_KT
    cruise_kt: float = CRUISE_KT
    last_action: Optional[str] = None
    last_hold_ms: int = 0
    bounced_this_tick: bool = False


class NileSim:
    def __init__(
        self, canvas_dir: Path, initial: SimState, dt_s: float = 1.0,
        rng_seed: Optional[int] = None,
    ):
        rgb, water, meta = load_canvas(Path(canvas_dir))
        self._rgb = rgb                    # only used for debug preview
        self._water = water                # authoritative land/water
        self._meta: CanvasMeta = meta
        self.state = initial
        self.dt_s = dt_s
        self._rng = random.Random(rng_seed)

    # ── ground truth for perception layers ─────────────────────────

    def ground_truth_heading(self) -> Heading:
        return Heading(
            bearing_deg=self.state.heading_deg,
            confidence=1.0,
            source="sim_ground_truth",
        )

    def ground_truth_water_mask(self) -> np.ndarray:
        """Water mask CROP around ship, at mini-map resolution."""
        cx, cy = self._meta.world_to_px(self.state.lat, self.state.lon)
        y0 = cy - MINIMAP_H // 2
        x0 = cx - MINIMAP_W // 2
        H, W = self._water.shape
        crop = np.zeros((MINIMAP_H, MINIMAP_W), dtype=bool)
        wy0, wy1 = max(0, y0), min(H, y0 + MINIMAP_H)
        wx0, wx1 = max(0, x0), min(W, x0 + MINIMAP_W)
        ty0, ty1 = wy0 - y0, wy0 - y0 + (wy1 - wy0)
        tx0, tx1 = wx0 - x0, wx0 - x0 + (wx1 - wx0)
        crop[ty0:ty1, tx0:tx1] = self._water[wy0:wy1, wx0:wx1]
        return crop

    def _is_land(self, lat: float, lon: float) -> bool:
        cx, cy = self._meta.world_to_px(lat, lon)
        H, W = self._water.shape
        if not (0 <= cx < W and 0 <= cy < H):
            return True                     # off-map = land
        return not bool(self._water[cy, cx])

    def _best_escape_heading(self, n_dirs: int = 16, probe_px: int = 30
                             ) -> float:
        """Pick the compass direction with the most open water ahead.

        Samples n_dirs headings evenly around the compass; for each,
        walks a small ray outward and counts water pixels reached
        before hitting land.  Returns the winning heading plus a small
        random jitter so successive escapes don't oscillate.
        """
        cx, cy = self._meta.world_to_px(self.state.lat, self.state.lon)
        H, W = self._water.shape
        best_heading = self.state.heading_deg
        best_reach = -1
        for i in range(n_dirs):
            hdg = i * (360.0 / n_dirs)
            r = math.radians(hdg)
            dx, dy = math.sin(r), -math.cos(r)   # +y = south in canvas
            reach = 0
            for step in range(1, probe_px + 1):
                px = int(cx + step * dx)
                py = int(cy + step * dy)
                if not (0 <= px < W and 0 <= py < H):
                    break
                if not self._water[py, px]:
                    break
                reach += 1
            if reach > best_reach:
                best_reach = reach
                best_heading = hdg
        return (best_heading + self._rng.uniform(-10, 10)) % 360.0

    # ── physics step ───────────────────────────────────────────────

    def apply_action(self, action: Optional[str], hold_ms: int) -> None:
        """Apply one tick of physics given the reflex layer's action."""
        s = self.state
        s.bounced_this_tick = False
        s.last_action = action
        s.last_hold_ms = hold_ms or 0

        # 1. Turn (instantaneous at TURN_RATE_DPS × hold_s)
        if action in ("hold_left", "hold_right") and hold_ms:
            delta = TURN_RATE_DPS * (hold_ms / 1000.0)
            if action == "hold_left":
                delta = -delta
            s.heading_deg = (s.heading_deg + delta) % 360.0

        # 2. Speed lag toward cruise (first-order)
        s.speed_kt += (s.cruise_kt - s.speed_kt) * (
            1.0 - math.exp(-self.dt_s / SPEED_TAU_S)
        )

        # 3. Forward step
        deg_per_s = s.speed_kt * KT_TO_DEG_PER_S
        step_deg = deg_per_s * self.dt_s
        # Convention: heading 0° = north, 90° = east, 180° = south, 270° = west.
        # cos(0)=1 → north → lat+; cos(180)=-1 → south → lat−.  So d_lat = +cos.
        heading_rad = math.radians(s.heading_deg)
        d_lat = step_deg * math.cos(heading_rad)
        d_lon = step_deg * math.sin(heading_rad)
        new_lat = s.lat + d_lat
        new_lon = s.lon + d_lon

        # 4. Collision
        if self._is_land(new_lat, new_lon):
            s.bounced_this_tick = True
            s.speed_kt = 0.0
            s.heading_deg = self._best_escape_heading()
        else:
            s.lat, s.lon = new_lat, new_lon

    # ── rendering ──────────────────────────────────────────────────

    def render_minimap(self) -> Image.Image:
        """Binary-style mini-map at the ship's current position."""
        crop = self.ground_truth_water_mask()
        rgb = np.where(crop[..., None], _WATER_RGB, _LAND_RGB).astype(np.uint8)
        # Composite ship — rotated placeholder, ~15×20 px oriented per heading
        _draw_ship(rgb, self.state.heading_deg)
        img = Image.fromarray(rgb)
        _draw_latlon_hud(img, self.state.lat, self.state.lon)
        return img

    def render_full_frame(self) -> Image.Image:
        """Full 2400×1080 frame with the sim mini-map pasted at MINIMAP_CROP."""
        canvas = Image.new("RGB", (2400, 1080), (0, 0, 0))
        canvas.paste(self.render_minimap(), _MINIMAP_CROP[:2])
        return canvas


_SHIP_SPRITE_CACHE: Optional[Image.Image] = None


def _load_ship_sprite() -> Image.Image:
    global _SHIP_SPRITE_CACHE
    if _SHIP_SPRITE_CACHE is None:
        _SHIP_SPRITE_CACHE = Image.open(_SHIP_SPRITE_PATH).convert("RGBA")
    return _SHIP_SPRITE_CACHE


def _draw_latlon_hud(img: Image.Image, lat: float, lon: float) -> None:
    """Paint the HUD-style 'LL.LL,LON.LO' at the bottom-right of the
    mini-map, matching the real game layout so downstream tools that
    OCR the HUD (and human viewers) see it in the same place."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    text = f"{lat:.2f},{lon:.2f}"
    # Approx game HUD size — pillow default font is small but readable
    x = img.width - 6 * len(text) - 8
    y = img.height - 16
    d.rectangle([(x - 2, y - 2), (img.width, img.height)], fill=(0, 0, 0))
    d.text((x, y), text, fill=(255, 255, 255))


def _draw_ship(rgb: np.ndarray, heading_deg: float) -> None:
    """Composite the real game ship sprite (rotated per heading) at the
    mini-map centre.  Using the same sprite family the CNN was trained
    on lets the real perception layers recognise the sim ship."""
    sprite = _load_ship_sprite()
    # PIL rotates counter-clockwise; game heading is compass (CW from N).
    # Also subtract the source sprite's known heading so a sim heading of
    # 0° yields a bow-up sprite.
    rotation_deg = -(heading_deg - _SHIP_SPRITE_HEADING_DEG)
    rotated = sprite.rotate(rotation_deg, resample=Image.BILINEAR)
    im = Image.fromarray(rgb).convert("RGBA")
    W, H = rotated.size
    cy, cx = MINIMAP_H // 2, MINIMAP_W // 2
    im.paste(rotated, (cx - W // 2, cy - H // 2), rotated)
    rgb[...] = np.asarray(im.convert("RGB"))


# ── VisionSource wrapping ─────────────────────────────────────────────

class SimulatedNileSource:
    """Yields a fresh full-frame VisionFrame each tick from the sim."""
    def __init__(self, sim: NileSim):
        self._sim = sim

    def capture(self, tick: int) -> VisionFrame:
        img = self._sim.render_full_frame()
        return VisionFrame(raw=img, tick=tick)


# ── Ground-truth perception layers ────────────────────────────────────

class SimHeading:
    """Heading layer that returns the sim's true bearing."""
    name = "sim_ground_truth"
    latency_budget_ms = 1.0

    def __init__(self, sim: NileSim):
        self._sim = sim

    def estimate(self, frame: VisionFrame,
                 prior: Optional[Heading]) -> Heading:
        return self._sim.ground_truth_heading()


class SimSegmentation:
    """Segmentation layer that returns the sim's true water mask."""
    name = "sim_ground_truth"

    def __init__(self, sim: NileSim):
        self._sim = sim

    def segment(self, frame: VisionFrame) -> np.ndarray:
        return self._sim.ground_truth_water_mask()
