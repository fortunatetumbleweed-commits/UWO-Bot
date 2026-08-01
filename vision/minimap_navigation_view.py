"""Mini-map navigation view — classical image-processing backend.

Implements the `NavigationView` Protocol (see `vision/navigation_view.py`)
for sea ticks by reading the top-right mini-map crop:

  * Locate the ship icon (saturated green) → polar origin.
  * Estimate ship heading (PCA + bow-asymmetry).
  * Threshold bright pixels to a land mask, excluding sprite pixels.
  * Bin sectors around the ship in ship-relative bearings.
  * Detect target sprites (yellow anchors = unvisited ports, white
    sprites = known ports / villages / merchant fleets / text).
  * BFS over the water mask for reachability.

Promoted from `tools/explore_minimap_geometry.py` after pixel-level
calibration on a variety of frames (see docs/navigation_view_design.md
and the original exploration tool for the visual debug equivalent).

Cost: ~3-5 ms per frame.  No model file, no training data.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import math

import numpy as np
from loguru import logger
from PIL import Image

from vision.navigation_view import (
    NavigationView,
    SECTOR_COUNT,
    SECTOR_WIDTH_DEG,
    SectorReading,
    Target,
)
from vision.water_skeleton import (
    extract_skeleton,
    trace_skeleton_forward,
)


# ── Calibrated constants ──────────────────────────────────────────────────
# Crop tightened against user-drawn reference on 2026-05-27.  See
# docs/navigation_view_design.md and the commit history of
# tools/explore_minimap_geometry.py for the calibration trail.

MINIMAP_CROP = (1984, 205, 2379, 395)           # 395×190 on a 2400×1080 frame.
# 2026-07-24: shrunk by +5 L / -5 R / +3 T from the raw OmniParser
# bbox to land cleanly on the mini-map disc rim.  OmniParser's bbox
# includes a ~5-px overworld sliver on each side and a few icon-strip
# pixels on top; those pixels get classified as water by the V11 mask
# (seen as full-height water contamination on t13, t374, t375 across
# earlier sessions).  The corresponding runtime offsets are applied in
# tools/run_ai_nav_live.py::_auto_calibrate_ui.
# 2026-07-21: recalibrated against a fresh live OmniParser reading.
# Prior "fix" to (1863, 202, 2264, 395) was based on a transient UI
# state where the disc had temporarily shifted ~120 px left; on the
# next live capture it was back at ~(1979, 2384) matching this value.
# The mini-map position MAY drift with game state — the pre-flight
# check in run_ai_nav_live.py catches this before we burn a voyage.
# 2026-07-14: reverted from an incorrect 2026-07-13 recalibration
# (1810, 145, 2245, 335) — that bbox was pointed at a different UI
# panel that happened to have green pixels (NPC boats + port markers),
# not the actual minimap disc.  User re-annotated the correct bbox
# on a live screencap and the bounds are essentially the same as the
# 2026-06-12 value; there was NO layout drift.
# The 2026-07-13 sea_actions.py recalibration DID find real drift
# for the steering controls — that recalibration stands.
# Prior values:
#   (1985, 205, 2385, 395) — 2026-06-12 calibration (400×190)
#   (1993, 211, 2374, 395) — 2026-06-12 first re-tune; clipped the
#                            last digit of the lon HUD text.
#   (1886, 191, 2267, 375) — 2026-05-29 re-tune, regressed (grabbed
#                            left-side route-overlay chrome, not the
#                            minimap)
#   (2003, 191, 2384, 375) — 2026-05-27 calibration (381×184)
# Current value: 400×190 (was 381×184) — expanded a few pixels on
# left, right, and top to keep the lat/lon HUD's last "5" inside the
# crop and capture the full minimap circle.  Downstream code that
# hardcodes 381×184 may need to be updated.
NAV_AREA = (0, 0, 400, 190)                     # Full crop is navigable.

# Translucent radar disc around the ship icon — its overlay washes
# pixels brighter, so this region is treated as always-navigable for
# the reachability BFS (the bot is already inside the disc).
DISC_RADIUS = 75
DISC_FALLBACK_CENTER = (190, 92)                # Used when ship icon missing.

# Land threshold + sprite-recovery cluster cap.
LAND_BRIGHT_V = 175                              # pixels with V > this are candidate land
LAND_BLOB_MIN_PX = 10                            # CC smaller than this is perception noise
LIGHT_GRAY_V_LOW = 185
LIGHT_GRAY_V_HIGH = 220
LIGHT_GRAY_SPREAD_MAX = 30
VERY_WHITE_V_MIN = 220
VERY_WHITE_SPREAD_MAX = 25
VILLAGE_CLUSTER_AREA_MAX = 600                  # max px of a "sprite" recovery cluster

# BFS slack — see _reachable() docstring.
START_SLACK = 20
TARGET_SLACK = 4

# Sprite sizing.
SPRITE_AREA_YELLOW_MIN = 25
SPRITE_AREA_YELLOW_MAX = 300
SPRITE_AREA_WHITE_MIN = 15
SPRITE_AREA_WHITE_MAX = 400
SPRITE_NEAR_SHIP_PX2 = 64                       # squared distance for "this is the ship icon, not a target"

# Threshold for `village_overlap`: when a target's pixel position is
# within this distance of the ship's centroid, the village/port icon
# and its label text are rendered ON TOP of the local water/land
# pixels — the water mask, skeleton, sector readings, AND HUD lat/lon
# are all corrupted.  See memory `project_hud_pirate_sprite_occlusion.md`
# and `project_minimap_template_matching_future.md`.  Empirically:
# at t571 of `reference_nile_full_20260606_141824` the "Bari Village"
# text + icon spans ~80 px and the ship's centroid sits ≈ 25 px from
# the village icon — 40 px covers the case where text overlay is
# disrupting perception without false-flagging far-bearing markers.
VILLAGE_OVERLAP_RADIUS_PX = 40


# ── Low-level helpers ─────────────────────────────────────────────────────


def _crop_minimap(frame: Image.Image) -> Image.Image:
    """Crop the mini-map area.  Scales the canonical crop if the frame
    isn't 2400×1080 (rare — phone resolution is fixed per CLAUDE.md)."""
    w, h = frame.size
    cx0, cy0, cx1, cy1 = MINIMAP_CROP
    if (w, h) != (2400, 1080):
        sx, sy = w / 2400.0, h / 1080.0
        cx0, cy0 = int(cx0 * sx), int(cy0 * sy)
        cx1, cy1 = int(cx1 * sx), int(cy1 * sy)
    return frame.crop((cx0, cy0, cx1, cy1))


def _color_masks(rgb: np.ndarray) -> dict[str, np.ndarray]:
    hsv = np.array(Image.fromarray(rgb).convert("HSV"))
    H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    R, G, B = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    spread = (np.maximum(np.maximum(R, G), B).astype(np.int16)
              - np.minimum(np.minimum(R, G), B).astype(np.int16))
    # Ship-yellow vs land-orange separation: in orange-desert regions
    # the broad `(H>15) & (H<50) & (S>120) & (V>120)` mask floods with
    # the entire shoreline (live t=25 of 2026-05-30_114024: 38 504 px
    # yellow vs ~50 normal).  Tighter thresholds + a green-channel guard
    # (`G ≥ R*0.85`) reject the orange land (where R notably exceeds G)
    # while keeping the bright saturated ship-sprite yellow.
    return {
        "ship_green":  (H > 60) & (H < 130) & (S > 100) & (V > 100),
        "yellow_any":  ((H > 18) & (H < 50)
                       & (S > 150) & (V > 150)
                       & (G.astype(np.int16) >= R.astype(np.int16) * 0.85)),
        "very_white":  (V > VERY_WHITE_V_MIN) & (spread < VERY_WHITE_SPREAD_MAX),
        "light_gray":  ((V > LIGHT_GRAY_V_LOW) & (V <= LIGHT_GRAY_V_HIGH)
                        & (spread < LIGHT_GRAY_SPREAD_MAX)),
        "saturated":   S > 100,
    }


def _nav_area_mask(shape: tuple[int, int]) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    l, t, r, b = NAV_AREA
    mask[t:b, l:r] = True
    return mask


def _disc_mask(shape: tuple[int, int], center, radius: int = DISC_RADIUS) -> np.ndarray:
    h, w = shape
    cx, cy = center
    yy, xx = np.ogrid[:h, :w]
    return ((xx - cx) ** 2 + (yy - cy) ** 2) <= radius * radius


def _ship_centroid(green: np.ndarray):
    ys, xs = np.where(green)
    if xs.size == 0:
        return None
    return float(xs.mean()), float(ys.mean())


def _text_mask_from_bboxes(shape: tuple[int, int],
                            bboxes: list[tuple[int, int, int, int]],
                            pad: int = 4) -> np.ndarray:
    """Build a boolean mask covering text bbox rectangles (in
    mini-map-local coords), padded by `pad` px on each side."""
    h, w = shape
    mask = np.zeros((h, w), dtype=bool)
    for (x0, y0, x1, y1) in bboxes:
        x0 = max(0, int(x0) - pad)
        y0 = max(0, int(y0) - pad)
        x1 = min(w, int(x1) + pad)
        y1 = min(h, int(y1) + pad)
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = True
    return mask


def _text_close_green(green: np.ndarray,
                       text_mask: np.ndarray,
                       kernel_radius: int = 3) -> np.ndarray:
    """§13.19 — Morphologically close `ship_green` THROUGH text bbox
    regions to bridge gaps text glyphs cut through the ship hull.

    Closing fills small concavities (text-stroke gaps), reconstructing
    the ship silhouette where glyphs overwrote it.  Restricted to the
    text-bbox region so that unrelated green pixels (land, vegetation
    near shore) aren't merged into the ship blob."""
    if not text_mask.any():
        return green
    try:
        from scipy.ndimage import binary_closing
    except ImportError:
        return green
    k = kernel_radius * 2 + 1
    kernel = np.ones((k, k), dtype=bool)
    closed = binary_closing(green, structure=kernel, iterations=1)
    return np.where(text_mask, closed, green)


def _bbox_permissive_expand(
    strict: np.ndarray,
    rgb: np.ndarray,
    margin: int = 4,
) -> np.ndarray:
    """§13.31 — Reconstruct ship pixels lost to text occlusion.

    The strict HSV mask (`ship_green` after `_clean_ship_green`)
    reliably finds the ship's location but loses pixels under text
    glyphs ("Nubia" village label, port names, etc.).  A more permissive
    `G > R & G > B & G ≥ R+15` criterion catches the text-occluded
    body pixels — but globally that criterion also catches the green
    Nile bank vegetation and NPC sprite colors, which destroys the
    PCA principal-axis direction.

    Compromise: union the strict mask with permissive-but-bounded
    pixels — i.e. only admit permissive pixels that fall inside the
    strict mask's bounding box (plus a small margin for the bow/sail
    overhang).  This recovers pixels under text without absorbing
    shore vegetation or NPC bobs outside the ship's footprint.

    Live data 2026-06-03 t94 (Nubia village overlap):
        strict alone   :  220 px, PCA axis 272° (Δ67° from SSW truth)
        global permissive: 1507 px, PCA axis 238° (Δ34°) — better but
                            includes village vegetation
        bbox-permissive  : ~350-450 px (target), PCA axis closer to
                            truth while excluding the village pixels.

    Returns the expanded mask; falls back to `strict` on degenerate
    input (no strict pixels at all).
    """
    if not strict.any():
        return strict
    ys, xs = np.where(strict)
    h, w = strict.shape
    y0 = max(0, int(ys.min()) - margin)
    y1 = min(h, int(ys.max()) + margin + 1)
    x0 = max(0, int(xs.min()) - margin)
    x1 = min(w, int(xs.max()) + margin + 1)

    R = rgb[..., 0].astype(np.int16)
    G = rgb[..., 1].astype(np.int16)
    B = rgb[..., 2].astype(np.int16)
    # Green-dominant + margin filter rejects text glyphs (R≈G≈B) and
    # most water (B-dominant), while admitting the ship's hull pixels
    # under text where the strict HSV mask fails.
    permissive = (G >= R + 15) & (G >= B + 15)
    bbox_mask = np.zeros_like(strict)
    bbox_mask[y0:y1, x0:x1] = True
    return strict | (permissive & bbox_mask)


def _clean_ship_green(green: np.ndarray) -> np.ndarray:
    """Filter `ship_green` to just the actual ship icon.

    Two problems with the raw color mask:
      1. The mini-map disc is semi-transparent over the world view, so
         land/sky/cloud pixels behind the disc bleed through.  Some of
         those pixels (cream/sand, dawn cyan-green tints) pass the
         green HSV threshold and add false greens scattered around the
         crop.
      2. The actual ship icon is a single ~400-pixel connected blob.
         Bleed-through pixels are smaller, scattered components.

    Filter: pick the largest connected component of green.  Falls back
    to the full mask if labelling fails (no scipy etc).

    Confirmed live 2026-05-29: ticks 9-19 of hug_debug_20260529_114939
    showed +78 to +135 false green pixels shifting the centroid up to
    44 px from the real ship position, causing the wedge bow detector
    to lose its candidate and flip 180° via PCA fallback.
    """
    try:
        from scipy.ndimage import label as ndi_label
    except Exception:
        return green   # scipy missing — degrade gracefully

    labels, n = ndi_label(green)
    if n <= 1:
        return green   # 0 or 1 component — nothing to filter
    # Largest component by pixel count.
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0   # background label
    keep = int(sizes.argmax())
    return labels == keep


@dataclass(frozen=True)
class HeadingReading:
    """Bow-resolved heading + provenance.

    `bearing_deg` — 0..360, compass (0=N, 90=E).
    `strategy`    — which detection strategy fired (for diagnostics and
                    confidence-aware downstream policies).
    `confidence`  — 0..1 estimate of how trustworthy this reading is.
                    Sail-pair beats single-wedge beats PCA fallback.
    """
    bearing_deg: float
    strategy: str        # "sail_pair" | "bow_wedge" | "pca_fallback"
    confidence: float


class ShipHeadingDetector:
    """All ship-sprite-specific heading logic lives here.

    Why a class: previously the detection was three free functions plus
    constants embedded in `_color_masks` — scattering that breaks under
    any sprite art change.  Encapsulating it means a future game update
    that redesigns the ship icon only requires re-measuring the constants
    here and updating one strategy method.

    Strategies are tried in order, best-first:

      1. `_sail_pair`     — angular bisector of the two yellow sail blobs
                            on the ship sprite.  Most robust because each
                            sail is ~50 px and the 68° geometry between
                            them is a strong sanity signal.  Empirical
                            separation 68° ± 7° measured across 24 ticks
                            of the 2026-05-29_205124 session — see
                            `tools/measure_sail_separation.py`.

      2. `_bow_wedge`     — small yellow triangle at the bow tip.  Reads
                            the actual heading glyph but the wedge is
                            only ~2 px and can degrade to 1 px under
                            anti-alias / wave-overlay noise (live t=31
                            of 2026-05-29_205124).

      3. `_pca_fallback`  — PCA + tip-asymmetry on the hull alone.  Used
                            only when no yellow is available.  Fragile
                            (can flip 180° on a single-pixel imbalance).

    All three return `HeadingReading` so downstream code can act on the
    `strategy` / `confidence` fields (e.g. tighten heading-rejection
    when fallback fired).
    """
    # ── Sprite anatomy ── (revisit if game art changes)
    BOW_WEDGE_MAX_AREA   = 15   # synthetic test wedge is 13 px; real ~2 px
    BOW_WEDGE_MAX_DIST   = 18   # px from hull centroid
    SAIL_AREA_RANGE      = (20, 80)
    SAIL_MAX_DIST        = 20
    SAIL_PAIR_SEPARATION = 68.0  # ship-frame angle between port + stbd sails
    SAIL_PAIR_TOLERANCE  = 7.0   # ± slop, see measure_sail_separation.py
    AXIS_PROJECTION_MIN  = 0.7   # cos(~45°) — candidates must lie on hull axis
    MIN_GREEN_PIXELS     = 8

    def detect(self, green: np.ndarray,
               yellow: Optional[np.ndarray] = None
               ) -> Optional[HeadingReading]:
        gys, gxs = np.where(green)
        if gxs.size < self.MIN_GREEN_PIXELS:
            return None
        cx, cy = float(gxs.mean()), float(gys.mean())
        principal = self._principal_axis(gxs, gys, cx, cy)

        if yellow is not None:
            cands = self._collect_yellow_candidates(
                green, yellow, cx, cy, principal,
            )
            r = self._sail_pair(cands)
            if r is not None:
                return r
            r = self._bow_wedge(cands)
            if r is not None:
                return r
            # §13.19 — PCA on the (possibly reconstructed) green hull
            # for the bow-stern axis; yellow centroid breaks the 180°
            # ambiguity (yellow lives only at the bow side).  Robust
            # against single-sail-visible cases that defeat sail_pair
            # /bow_wedge but where pca_fallback's tip-asymmetry flips
            # randomly.  See docs/heading_pca_yellow_anchor.md.
            r = self._pca_yellow_anchor(green, yellow)
            if r is not None:
                return r
        return self._pca_fallback(gxs, gys, cx, cy)

    # ── Strategies ──

    def _sail_pair(self, cands: list[dict]) -> Optional[HeadingReading]:
        """Find two on-hull on-axis candidates ~68° apart; return bisector."""
        amin, amax = self.SAIL_AREA_RANGE
        pool = [c for c in cands
                if amin <= c["area"] <= amax
                and c["dist"] <= self.SAIL_MAX_DIST
                and c["on_axis"] and c["touches_green"]]
        if len(pool) < 2:
            return None
        best_pair = None
        best_err = float("inf")
        for i in range(len(pool)):
            for j in range(i + 1, len(pool)):
                a1, a2 = pool[i]["angle"], pool[j]["angle"]
                sep = abs(((a1 - a2 + 540) % 360) - 180)
                err = abs(sep - self.SAIL_PAIR_SEPARATION)
                if err < best_err:
                    best_err = err
                    best_pair = (a1, a2)
        if best_pair is None or best_err > self.SAIL_PAIR_TOLERANCE:
            return None
        a1, a2 = best_pair
        # Bisector on the *shorter* arc.
        diff = ((a2 - a1 + 540) % 360) - 180
        bisector = (a1 + diff / 2.0) % 360.0
        # Confidence: 1.0 at ideal separation, scales linearly down to
        # 0.7 at the tolerance edge.
        conf = 1.0 - 0.3 * (best_err / self.SAIL_PAIR_TOLERANCE)
        return HeadingReading(bisector, "sail_pair", conf)

    def _bow_wedge(self, cands: list[dict]) -> Optional[HeadingReading]:
        """Find the smallest on-axis candidate close to centroid."""
        pool = [c for c in cands
                if 2 <= c["area"] <= self.BOW_WEDGE_MAX_AREA
                and c["dist"] <= self.BOW_WEDGE_MAX_DIST
                and c["on_axis"] and c["touches_green"]]
        if not pool:
            return None
        winner = min(pool, key=lambda c: 0.5 * c["area"] + c["dist"])
        return HeadingReading(winner["angle"], "bow_wedge", 0.5)

    def _pca_yellow_anchor(self, green: np.ndarray,
                            yellow: np.ndarray,
                            ) -> Optional[HeadingReading]:
        """§13.19 — PCA on green for direction + yellow centroid to
        break the 180° bow/stern ambiguity.

        Why this beats `_pca_fallback`'s tip-asymmetry heuristic:
        the ship sprite has WIDE bow (sail spread) and NARROW stern,
        so "side with more pixels = bow" is inverted.  And when only
        one sail is visible (text occlusion), the green→yellow
        centroid vector is biased toward THAT sail rather than the
        bow apex.  Decoupling the two responsibilities — PCA owns
        DIRECTION, yellow owns BOW-END CHOICE — fixes both.

        Yellow filter: only count connected components that touch the
        green hull (within 2 px dilation), so NPC sprites and
        village-roof shadows don't pollute the centroid.
        """
        from scipy.ndimage import binary_dilation, label as ndi_label
        gys, gxs = np.where(green)
        if gxs.size < self.MIN_GREEN_PIXELS:
            return None
        yel_labels, n_yel = ndi_label(yellow)
        if n_yel == 0:
            return None
        green_dil = binary_dilation(green, iterations=2)
        touching = np.zeros_like(yellow)
        for lbl in range(1, n_yel + 1):
            comp = (yel_labels == lbl)
            if (comp & green_dil).any():
                touching |= comp
        if int(touching.sum()) < 4:
            return None
        yys, yxs = np.where(touching)
        cx, cy = float(gxs.mean()), float(gys.mean())
        ycx, ycy = float(yxs.mean()), float(yys.mean())
        pts = np.column_stack([gxs - cx, gys - cy]).astype(float)
        try:
            cov = np.cov(pts, rowvar=False)
            eigvals, eigvecs = np.linalg.eigh(cov)
            principal = eigvecs[:, np.argmax(eigvals)]
        except Exception:
            return None
        # Yellow always lives at the bow side.  Pick the axis end with
        # positive projection of (yellow - green) as bow direction.
        dx, dy = ycx - cx, ycy - cy
        proj = dx * principal[0] + dy * principal[1]
        bow_vec = principal if proj > 0 else -principal
        ang = float(
            (np.degrees(np.arctan2(bow_vec[0], -bow_vec[1])) + 360) % 360
        )
        return HeadingReading(ang, "pca_yellow_anchor", 0.65)

    def _pca_fallback(self, gxs, gys, cx, cy) -> Optional[HeadingReading]:
        """PCA principal axis + tip-asymmetry tiebreaker.  Fragile."""
        pts = np.column_stack([gxs - cx, gys - cy]).astype(float)
        try:
            cov = np.cov(pts, rowvar=False)
            eigvals, eigvecs = np.linalg.eigh(cov)
            principal = eigvecs[:, np.argmax(eigvals)]
        except Exception:
            return None
        proj = pts @ principal
        bow_vec = (principal if (proj < 0).sum() > (proj > 0).sum()
                   else -principal)
        ang = float((np.degrees(np.arctan2(bow_vec[0], -bow_vec[1])) + 360) % 360)
        return HeadingReading(ang, "pca_fallback", 0.2)

    # ── Helpers ──

    def _principal_axis(self, gxs, gys, cx, cy):
        """Unit vector along the hull's long axis (PCA principal).
        Used both as an axis guard for yellow candidates and as the
        backbone of `_pca_fallback`."""
        pts = np.column_stack([gxs - cx, gys - cy]).astype(float)
        try:
            cov = np.cov(pts, rowvar=False)
            eigvals, eigvecs = np.linalg.eigh(cov)
            principal = eigvecs[:, np.argmax(eigvals)]
            return principal / max(np.linalg.norm(principal), 1e-9)
        except Exception:
            return None

    def _collect_yellow_candidates(self, green, yellow, cx, cy, principal
                                   ) -> list[dict]:
        """Walk the yellow connected components once.  For each, record
        the fields every strategy needs: area, distance from centroid,
        angle (compass bearing from centroid), on-axis flag, and whether
        it touches the green hull (within 2 px dilation)."""
        from scipy.ndimage import binary_dilation, label as ndi_label
        yel_labels, n_yel = ndi_label(yellow)
        if n_yel == 0:
            return []
        green_dil = binary_dilation(green, iterations=2)
        out = []
        for lbl in range(1, n_yel + 1):
            comp = (yel_labels == lbl)
            area = int(comp.sum())
            cys, cxs = np.where(comp)
            wx, wy = float(cxs.mean()), float(cys.mean())
            dx, dy = wx - cx, wy - cy
            dist = math.hypot(dx, dy)
            if dist < 1e-3:
                continue
            touches = bool((comp & green_dil).any())
            on_axis = True
            if principal is not None:
                proj = abs(dx * principal[0] + dy * principal[1])
                on_axis = (proj / dist) >= self.AXIS_PROJECTION_MIN
            ang = (math.degrees(math.atan2(dx, -dy)) + 360) % 360
            out.append({
                "area": area, "dist": dist, "angle": ang,
                "wx": wx, "wy": wy,
                "touches_green": touches, "on_axis": on_axis,
            })
        return out


# Module-level singleton — class holds no per-instance state.
_ship_heading_detector = ShipHeadingDetector()


def _ship_heading(green: np.ndarray,
                  yellow: Optional[np.ndarray] = None) -> Optional[float]:
    """Bow-resolved heading 0..360 (0=N, 90=E).

    Thin wrapper around `ShipHeadingDetector` that returns only the
    bearing.  Callers wanting the strategy/confidence fields should
    instantiate the detector directly.
    """
    r = _ship_heading_detector.detect(green, yellow)
    return r.bearing_deg if r is not None else None


def _ship_heading_wedge(green: np.ndarray,
                        yellow: np.ndarray) -> Optional[float]:
    """Backward-compatible wrapper: returns the bearing if the sail-pair
    or bow-wedge strategy fires, else None.  Does NOT fall through to
    PCA — callers that want the PCA fallback should use `_ship_heading`.
    """
    gys, gxs = np.where(green)
    if gxs.size < _ship_heading_detector.MIN_GREEN_PIXELS:
        return None
    cx, cy = float(gxs.mean()), float(gys.mean())
    principal = _ship_heading_detector._principal_axis(gxs, gys, cx, cy)
    cands = _ship_heading_detector._collect_yellow_candidates(
        green, yellow, cx, cy, principal,
    )
    r = _ship_heading_detector._sail_pair(cands)
    if r is None:
        r = _ship_heading_detector._bow_wedge(cands)
    return r.bearing_deg if r is not None else None


def _ship_heading_pca(green: np.ndarray) -> Optional[float]:
    """Backward-compatible wrapper: PCA + tip-asymmetry only."""
    gys, gxs = np.where(green)
    if gxs.size < _ship_heading_detector.MIN_GREEN_PIXELS:
        return None
    cx, cy = float(gxs.mean()), float(gys.mean())
    r = _ship_heading_detector._pca_fallback(gxs, gys, cx, cy)
    return r.bearing_deg if r is not None else None


def _build_sprite_mask(color: dict[str, np.ndarray]) -> np.ndarray:
    """Combine sprite-bearing color masks PLUS the village-roof recovery.

    Village icons are partly very-white (windows) and partly light-gray
    (roof).  Component-level recovery keeps light-gray clusters that
    (a) touch a very-white pixel AND (b) are small enough to be sprites
    rather than land.  This rescues the roof without sweeping in land.
    """
    from scipy.ndimage import binary_dilation, label
    seed = color["very_white"]
    lg_labels, _ = label(color["light_gray"])
    seed_neighbor = binary_dilation(seed, iterations=1)
    touched_labels = set(np.unique(lg_labels[seed_neighbor & (lg_labels > 0)]))
    village_recovery = np.zeros_like(color["light_gray"])
    for lbl in touched_labels:
        if lbl == 0:
            continue
        comp_mask = (lg_labels == lbl)
        if int(comp_mask.sum()) < VILLAGE_CLUSTER_AREA_MAX:
            village_recovery |= comp_mask
    return (color["ship_green"]
            | color["yellow_any"]
            | color["very_white"]
            | village_recovery
            | color["saturated"])


def _land_mask(gray: np.ndarray, allowed: np.ndarray) -> np.ndarray:
    raw = allowed & (gray > LAND_BRIGHT_V)
    return _drop_small_blobs(raw, LAND_BLOB_MIN_PX)


def _drop_small_blobs(mask: np.ndarray, min_px: int) -> np.ndarray:
    """Keep only 8-connected components with area >= min_px.

    Filters out perception-noise pixels (anti-aliased edges of NPC ship
    icons / port-marker diamonds touching the player ship) that slip
    through the sprite mask and would otherwise be reported as land
    1–5 px from the ship — see project_t_dist_blob_filter memory.
    """
    if min_px <= 1 or not mask.any():
        return mask
    try:
        from scipy.ndimage import label as ndi_label
    except ImportError:
        return mask
    labels, n = ndi_label(mask)
    if n == 0:
        return mask
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0
    keep = np.isin(labels, np.where(sizes >= min_px)[0])
    return keep


def _connected_components(mask: np.ndarray) -> list[dict]:
    """Lightweight 8-connected component finder.  Returns area, centroid,
    bbox, aspect per blob.  Pure numpy + BFS."""
    visited = np.zeros_like(mask, dtype=bool)
    h, w = mask.shape
    blobs = []
    for sy in range(h):
        for sx in range(w):
            if not mask[sy, sx] or visited[sy, sx]:
                continue
            q = deque([(sy, sx)])
            ys, xs = [], []
            while q:
                y, x = q.popleft()
                if y < 0 or y >= h or x < 0 or x >= w:
                    continue
                if visited[y, x] or not mask[y, x]:
                    continue
                visited[y, x] = True
                ys.append(y); xs.append(x)
                q.extend([(y+1, x), (y-1, x), (y, x+1), (y, x-1),
                          (y+1, x+1), (y-1, x-1), (y+1, x-1), (y-1, x+1)])
            if len(ys) < 3:
                continue
            blobs.append({
                "area":     len(ys),
                "centroid": (float(np.mean(xs)), float(np.mean(ys))),
                "bbox":     (min(xs), min(ys), max(xs), max(ys)),
                "aspect":   (max(xs) - min(xs) + 1) / max(1, (max(ys) - min(ys) + 1)),
            })
    blobs.sort(key=lambda b: -b["area"])
    return blobs


def _reachable(water: np.ndarray, start, target,
               start_slack: int = START_SLACK,
               target_slack: int = TARGET_SLACK) -> bool:
    """BFS over water from start; True if any cell within
    `target_slack` of target is reached.  See exploration tool for the
    rationale on the slack values."""
    h, w = water.shape
    sx, sy = int(round(start[0])), int(round(start[1]))
    tx, ty = int(round(target[0])), int(round(target[1]))
    if not (0 <= sx < w and 0 <= sy < h and 0 <= tx < w and 0 <= ty < h):
        return False
    if not water[sy, sx]:
        found = False
        for r in range(1, start_slack + 1):
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if abs(dy) != r and abs(dx) != r:
                        continue
                    ny, nx = sy + dy, sx + dx
                    if 0 <= ny < h and 0 <= nx < w and water[ny, nx]:
                        sy, sx = ny, nx
                        found = True
                        break
                if found:
                    break
            if found:
                break
        if not found:
            return False
    visited = np.zeros_like(water, dtype=bool)
    visited[sy, sx] = True
    q = deque([(sy, sx)])
    target_set = {(ty + dy, tx + dx)
                  for dy in range(-target_slack, target_slack + 1)
                  for dx in range(-target_slack, target_slack + 1)}
    while q:
        y, x = q.popleft()
        if (y, x) in target_set:
            return True
        for dy, dx in [(0, 1), (0, -1), (1, 0), (-1, 0)]:
            ny, nx = y + dy, x + dx
            if (0 <= ny < h and 0 <= nx < w
                    and not visited[ny, nx] and water[ny, nx]):
                visited[ny, nx] = True
                q.append((ny, nx))
    return False


def _to_ship_relative(compass_deg: float, heading_deg: Optional[float]) -> float:
    """Convert a compass bearing to ship-relative (0 = ahead)."""
    if heading_deg is None:
        return compass_deg
    return (compass_deg - heading_deg + 360) % 360


# ── Public class ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Reading:
    """Container for the precomputed per-frame state."""
    ship_xy:    Optional[tuple[float, float]]
    heading:    Optional[float]
    heading_strategy:   Optional[str] = None    # which ShipHeadingDetector strategy fired
    heading_confidence: Optional[float] = None  # detector confidence 0..1
    water:      np.ndarray = None             # reachability substrate
    sectors:    tuple[SectorReading, ...] = ()
    targets:    tuple[Target, ...] = ()
    target_pos: dict[int, tuple[float, float]] = None    # id(Target) → (x, y) in crop


class MinimapNavigationView:
    """Mini-map-driven NavigationView.

    Construct with `MinimapNavigationView.from_frame(frame)` — runs the
    full processing pipeline once and caches all derived state.
    """

    def __init__(self, reading: _Reading):
        self._reading = reading

    # ── Protocol surface ──

    @property
    def ship_heading_deg(self) -> Optional[float]:
        return self._reading.heading

    @property
    def heading_strategy(self) -> Optional[str]:
        """Which ShipHeadingDetector strategy produced this tick's
        heading: 'sail_pair' (most robust), 'bow_wedge', or
        'pca_fallback' (least robust).  None if no heading."""
        return self._reading.heading_strategy

    @property
    def heading_confidence(self) -> Optional[float]:
        """Detector confidence 0..1.  Downstream code (e.g. the
        hug-shore rejection logic) can widen its plausible-rotation
        threshold when confidence is low — a tentative reading is
        better than no reading."""
        return self._reading.heading_confidence

    @property
    def sectors(self) -> tuple[SectorReading, ...]:
        return self._reading.sectors

    @property
    def ship_xy(self) -> Optional[tuple[float, float]]:
        """Ship-icon centroid in minimap pixel coordinates (x, y).

        None when the ship_green mask was empty (heading detector also
        unavailable in that case).  Used by `vision.water_skeleton.
        compute_skeleton_tangent_deg` to locate the ship on the medial
        axis for the world-frame steering tangent.
        """
        return self._reading.ship_xy

    @property
    def water_mask(self) -> Optional[np.ndarray]:
        """Binary water mask from the minimap thresholding pipeline.

        2-D array, True where the minimap shows navigable water.  Used
        by `vision.water_skeleton` for medial-axis topology
        classification (LAKE detection + skeleton/arc-count
        cross-validation in `JunctionDetector.classify`).
        See docs/exploration_navigation_layers.md for the
        perception/mapping/planning split.
        """
        return self._reading.water

    @property
    def targets(self) -> tuple[Target, ...]:
        return self._reading.targets

    @property
    def village_overlap(self) -> bool:
        """True when a village/port icon's pixel position is close
        enough to the ship that the icon's label text overlay is
        corrupting local perception.

        When this fires, downstream steering should NOT trust:
          - The local water mask / skeleton (text glyphs are
            classified as land, breaking the medial axis).
          - The skeleton tangent (computed from corrupted water mask).
          - Re-derived shore tangent (sectors near the ship are wrong).
          - The HUD lat/lon read (overlay text occludes digits — see
            `project_hud_pirate_sprite_occlusion.md`).

        Recommended response: **dead-reckon on the §13.21 committed
        tangent direction** until `village_overlap` clears (the ship
        sails past the village).  This is the same primitive needed
        for any momentary perception failure — see
        `project_minimap_template_matching_future.md`.

        Returns False when there's no ship_xy (heading detector down
        — perception is already in fallback) or when no targets are
        in the minimap.
        """
        # === TEMPORARY EXPERIMENT 2026-06-07 ===========================
        # village_overlap was firing on 800/800 ticks of the Nile descent
        # because port markers persistently sit 15-17 px from the ship
        # (inside the 40-px overlap radius, outside the 8-px ship-sprite
        # filter).  Disabling unconditionally to measure how much that
        # detector was contributing vs. blocking real navigation.
        # Revert by deleting these two lines and re-enabling the loop.
        return False
        # === END EXPERIMENT ============================================
        if self._reading.ship_xy is None:
            return False
        if not self._reading.targets or self._reading.target_pos is None:
            return False
        sx, sy = self._reading.ship_xy
        for target in self._reading.targets:
            pos = self._reading.target_pos.get(id(target))
            if pos is None:
                continue
            tx, ty = pos
            if math.hypot(tx - sx, ty - sy) <= VILLAGE_OVERLAP_RADIUS_PX:
                return True
        return False

    @property
    def desired_waypoint_bearing_deg(self) -> Optional[float]:
        """Compass bearing toward the next point on the water skeleton,
        looking forward from the ship along the channel.

        Phase 2 of the skeleton-steering migration — the live counterpart
        to `SimNav.desired_waypoint` (which carries a lat/lon directly
        because the sim has reference data).  Here we expose only a
        bearing; the goal layer projects it to a lat/lon using HUD
        position so perception stays HUD-agnostic.

        Returns None when:
          - village_overlap is active (skeleton corrupted by overlay
            text — see `village_overlap` docs),
          - no water mask / no ship_xy / no ship heading,
          - skeleton extraction fails or yields no usable forward point.

        Caller (`brain.goals.hug_shore`, `point_pursuit` driver) treats
        None as "fall through to the existing picker/commit target."
        """
        if self.village_overlap:
            return None
        water_mask = self._reading.water
        ship_xy = self._reading.ship_xy
        heading = self._reading.heading
        if water_mask is None or ship_xy is None or heading is None:
            return None
        try:
            analysis = extract_skeleton(water_mask)
        except Exception as e:
            logger.debug(f"[minimap_nav] skeleton extract failed: {e}")
            return None
        target = trace_skeleton_forward(
            analysis, ship_xy, forward_hint_deg=heading,
        )
        if target is None:
            return None
        sx, sy = ship_xy
        tx, ty = target
        dx = float(tx) - float(sx)
        dy = float(ty) - float(sy)
        if dx == 0.0 and dy == 0.0:
            return None
        # Minimap is N-up: dy<0 is north, dx>0 is east.  Compass bearing
        # measured clockwise from north → atan2(dx, -dy).  Matches the
        # convention used by `trace_skeleton_forward` (forward_pdx=sin,
        # forward_pdy=-cos) and by `_compute_sectors`.
        bearing = math.degrees(math.atan2(dx, -dy)) % 360.0
        return bearing

    def is_reachable(self, target: Target) -> bool:
        pos = self._reading.target_pos.get(id(target))
        if pos is None or self._reading.ship_xy is None:
            return False
        return _reachable(self._reading.water, self._reading.ship_xy, pos)

    # ── Factory ──

    @classmethod
    def from_frame(cls, frame: Image.Image,
                   text_bboxes_local: Optional[
                       list[tuple[int, int, int, int]]] = None,
                   ) -> "MinimapNavigationView":
        reading = cls._compute(frame, text_bboxes_local=text_bboxes_local)
        return cls(reading)

    @staticmethod
    def _compute(frame: Image.Image,
                 text_bboxes_local: Optional[
                     list[tuple[int, int, int, int]]] = None,
                 ) -> _Reading:
        mm = _crop_minimap(frame).convert("RGB")
        rgb = np.array(mm)
        gray = np.array(mm.convert("L"))
        color = _color_masks(rgb)

        # §13.19 — Text bbox pre-pass: when the bot is close to a
        # labelled port/village, the label text glyphs overwrite the
        # ship_green pixels behind them.  Morphologically close green
        # within text-bbox regions to reconstruct the ship silhouette
        # before the largest-CC filter discards the fragments.  See
        # docs/heading_pca_yellow_anchor.md.
        if text_bboxes_local:
            text_mask = _text_mask_from_bboxes(
                color["ship_green"].shape, text_bboxes_local)
            color["ship_green"] = _text_close_green(
                color["ship_green"], text_mask)

        # Filter the green mask to the single largest connected
        # component before any consumer reads it.  This removes the
        # bleed-through specks from land/sky/clouds visible behind the
        # semi-transparent radar disc.  Without this, contamination
        # progressively shifted the centroid west across ticks 9-19 of
        # the 2026-05-29_114939 run.
        color["ship_green"] = _clean_ship_green(color["ship_green"])
        # §13.31 — bbox-bounded permissive expansion to recover pixels
        # lost to text occlusion (e.g. "Nubia" cutting through the hull).
        # See `_bbox_permissive_expand` docstring for the t94 numbers.
        color["ship_green"] = _bbox_permissive_expand(
            color["ship_green"], rgb,
        )
        # Re-run the largest-CC filter so any disconnected permissive
        # blobs that sneak through the bbox (NPC bob fragments, etc.)
        # are still discarded.
        color["ship_green"] = _clean_ship_green(color["ship_green"])
        ship_xy = _ship_centroid(color["ship_green"])
        heading_reading = _ship_heading_detector.detect(
            color["ship_green"], color["yellow_any"],
        )
        if heading_reading is not None:
            heading = heading_reading.bearing_deg
            heading_strategy = heading_reading.strategy
            heading_confidence = heading_reading.confidence
        else:
            heading = None
            heading_strategy = None
            heading_confidence = None

        nav = _nav_area_mask(gray.shape)
        disc_center = ship_xy if ship_xy is not None else DISC_FALLBACK_CENTER
        disc = _disc_mask(gray.shape, disc_center)
        sprites = _build_sprite_mask(color)
        land = _land_mask(gray, nav & ~sprites)
        water = nav & ~sprites & (~land | disc)

        sectors = _compute_sectors(land, ship_xy, nav, heading)
        targets, target_pos = _compute_targets(color, nav, ship_xy, heading)
        return _Reading(
            ship_xy=ship_xy,
            heading=heading,
            heading_strategy=heading_strategy,
            heading_confidence=heading_confidence,
            water=water,
            sectors=sectors,
            targets=targets,
            target_pos=target_pos,
        )


# ── Per-frame derivations (free functions to keep the class small) ────────


def _compute_sectors(
    land: np.ndarray,
    ship_xy: Optional[tuple[float, float]],
    nav: np.ndarray,
    heading_deg: Optional[float],
) -> tuple[SectorReading, ...]:
    """Bin the land mask into 8 ship-relative sectors.

    Bearings are computed as compass first (0=N=image-up) then rotated
    into ship-relative coords by subtracting `heading_deg`.  If heading
    is unknown, sectors are kept in compass coordinates and flagged
    with bearing=compass — the caller can detect this via
    `ship_heading_deg is None` and decide whether to trust the sectors.
    """
    h, w = land.shape
    if ship_xy is None:
        sx, sy = w / 2, h / 2
    else:
        sx, sy = ship_xy

    yy, xx = np.indices(land.shape)
    dx = xx - sx
    dy = -(yy - sy)
    compass_bearing = (np.degrees(np.arctan2(dx, dy)) + 360) % 360
    radius = np.sqrt(dx * dx + dy * dy)
    if nav.any():
        max_r = max(radius[nav].max(), 1)
    else:
        max_r = max(radius.max(), 1)

    sectors: list[SectorReading] = []
    half = SECTOR_WIDTH_DEG / 2
    rotation = heading_deg or 0.0
    for i in range(SECTOR_COUNT):
        ship_relative_centre = i * SECTOR_WIDTH_DEG
        # The image-compass bearing range for this ship-relative sector.
        compass_centre = (ship_relative_centre + rotation) % 360
        lower = (compass_centre - half) % 360
        upper = (compass_centre + half) % 360
        if lower < upper:
            in_sector = (compass_bearing >= lower) & (compass_bearing < upper)
        else:
            in_sector = (compass_bearing >= lower) | (compass_bearing < upper)
        in_sector &= nav
        total = int(in_sector.sum())
        land_here = in_sector & land
        if total > 0:
            frac = float(land_here.sum()) / total
        else:
            frac = 0.0
        if land_here.any():
            nearest = float(radius[land_here].min() / max_r)
        else:
            nearest = None
        sectors.append(SectorReading(
            bearing_deg=ship_relative_centre,
            land_fraction=frac,
            nearest_dist=nearest,
            is_observed=True,
        ))
    return tuple(sectors)


def _compute_targets(
    color: dict[str, np.ndarray],
    nav: np.ndarray,
    ship_xy: Optional[tuple[float, float]],
    heading_deg: Optional[float],
) -> tuple[tuple[Target, ...], dict[int, tuple[float, float]]]:
    """Detect port/village sprites and convert to Target records."""
    def _far_from_ship(centroid):
        if ship_xy is None:
            return True
        bx, by = centroid
        sx, sy = ship_xy
        return (bx - sx) ** 2 + (by - sy) ** 2 > SPRITE_NEAR_SHIP_PX2

    yellow_blobs = [
        b for b in _connected_components(color["yellow_any"] & nav)
        if SPRITE_AREA_YELLOW_MIN < b["area"] < SPRITE_AREA_YELLOW_MAX
           and 0.5 < b["aspect"] < 2.0
           and _far_from_ship(b["centroid"])
    ]
    white_blobs = [
        b for b in _connected_components(color["very_white"] & nav)
        if SPRITE_AREA_WHITE_MIN < b["area"] < SPRITE_AREA_WHITE_MAX
           and _far_from_ship(b["centroid"])
    ]

    # Normalize distance by the half-diagonal of the nav area.
    h, w = color["very_white"].shape
    hd = np.sqrt(h * h + w * w) / 2

    out: list[Target] = []
    pos_map: dict[int, tuple[float, float]] = {}

    for b in yellow_blobs:
        x, y = b["centroid"]
        if ship_xy is None:
            continue
        compass = _bearing(ship_xy, (x, y))
        rel = _to_ship_relative(compass, heading_deg)
        dist = np.sqrt((x - ship_xy[0]) ** 2 + (y - ship_xy[1]) ** 2) / hd
        t = Target(kind="port_unvisited", bearing_deg=rel,
                   distance=float(dist), name=None)
        out.append(t)
        pos_map[id(t)] = (x, y)

    for b in white_blobs:
        x, y = b["centroid"]
        if ship_xy is None:
            continue
        compass = _bearing(ship_xy, (x, y))
        rel = _to_ship_relative(compass, heading_deg)
        dist = np.sqrt((x - ship_xy[0]) ** 2 + (y - ship_xy[1]) ** 2) / hd
        # Without shape templates we can't separate known-port-anchor
        # from village-building from merchant-diamond from text.  Mark
        # them all as port_known for now — steering code only needs
        # the bearing + reachability.
        t = Target(kind="port_known", bearing_deg=rel,
                   distance=float(dist), name=None)
        out.append(t)
        pos_map[id(t)] = (x, y)

    return tuple(out), pos_map


def _bearing(origin, target) -> float:
    dx = target[0] - origin[0]
    dy = -(target[1] - origin[1])
    return float((np.degrees(np.arctan2(dx, dy)) + 360) % 360)


# ── Cache + public factory ────────────────────────────────────────────────

# Cache key includes a small content fingerprint so id() collisions on
# garbage-collected PIL frames don't return stale views.
# Caught 2026-05-30 during calibration: three consecutive captures
# during a turn returned the same 302.1773527582137° heading to 12
# decimal places — actually the same cached `MinimapNavigationView`
# because Python allocated the new PIL.Image at the same address as
# the previous one after it was GC'd.
_CACHE: "dict[tuple[int, bytes], MinimapNavigationView]" = {}
_CACHE_MAX = 8


def _content_fingerprint(frame: Image.Image) -> bytes:
    """Cheap content hash — bytes from a 32-pixel band inside the
    mini-map area, where pixels actually change between distinct
    captures.  Doesn't need to be cryptographic — just enough
    entropy to distinguish frames captured seconds apart."""
    try:
        # Pull from MINIMAP_CROP centre — 32 px tall × 8 px wide strip,
        # bytes-only.  ~256 bytes — fast hash, distinct between frames.
        l, t, r, b = MINIMAP_CROP
        cx = (l + r) // 2
        cy = (t + b) // 2
        strip = frame.crop((cx - 4, cy - 16, cx + 4, cy + 16))
        return strip.tobytes()
    except Exception:
        return b""


def read_navigation_view(
    frame: Image.Image,
    text_bboxes_local: Optional[list[tuple[int, int, int, int]]] = None,
) -> MinimapNavigationView:
    """Public entry — returns a cached MinimapNavigationView per
    (id, content fingerprint) so multiple callers within ONE tick share
    work, but a re-allocated PIL.Image at the same address with
    different pixels gets re-computed (id() alone is insufficient — see
    cache docstring above).

    `text_bboxes_local` is an optional list of (x0,y0,x1,y1) bboxes in
    mini-map-crop-local coordinates marking text-label regions that
    overlap the ship icon (e.g. "Nubia Village" sitting on top of the
    bot's ship).  When provided, the ship_green pipeline runs a
    morphological-closing pre-pass through those regions to bridge the
    gaps text glyphs cut through the hull, improving heading detection
    in port/village proximity (§13.19).  See
    `docs/heading_pca_yellow_anchor.md`."""
    key = (id(frame), _content_fingerprint(frame),
           tuple(text_bboxes_local) if text_bboxes_local else ())
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    view = MinimapNavigationView.from_frame(
        frame, text_bboxes_local=text_bboxes_local)
    _CACHE[key] = view
    if len(_CACHE) > _CACHE_MAX:
        # Drop one arbitrary entry to keep the cache bounded.
        _CACHE.pop(next(iter(_CACHE)))
    return view


def _reset_for_test() -> None:
    _CACHE.clear()
