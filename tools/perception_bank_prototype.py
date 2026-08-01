"""Standalone bank-detection prototype — ray-cast from ship to banks.

NOT plumbed into live perception.  This is an evaluation harness for the
ray-cast approach so we can iterate on the algorithm before wiring it in.

Algorithm
─────────
1. Per-pixel category classifier (cheap):
     - LAND        — saturated tan/sand colour
     - WATER       — saturated blue colour
     - HALO        — washed-out light blue (halo over water OR over land)
     - SPRITE      — white / yellow / green high-luminance icons
     - TEXT        — light-gray text overlay (village names)
     - UNKNOWN     — everything else (HUD chrome, background sky, etc.)

2. From `ship_xy`, cast N rays at uniform angular spacing (default 360,
   so 1° resolution).  Each ray marches outward 1 px at a time.  Stop
   when we hit LAND (= bank).  Pass through SPRITE / TEXT (they're
   features inside the water, not bank).  Pass through HALO unless we
   already saw WATER on this ray and the halo→land transition is what
   we're calling the bank.  If we exit the viewport before hitting
   land, record an OPEN boundary at the viewport edge.

3. Collect bank hits in angular order → a closed polygon (with
   possibly OPEN sections at viewport edges).  Fill the polygon to get
   the detected water mask.

4. Compare against red-pen ground truth via IoU on the water region
   (flood-fill the area inside the red lines from `ship_xy`).

5. Render a 3×3 grid showing each scenario's detected mask vs ground
   truth with the IoU per cell.

Usage
─────
  python -m tools.perception_bank_prototype

  All inputs are baked in (the 9 labeled frames + GT masks already
  produced).  Output is /tmp/bank_raycast_eval.png and a printout of
  per-scenario IoU.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import matplotlib.pyplot as plt
from PIL import Image
from scipy.ndimage import binary_dilation, binary_fill_holes, label as cc_label
from skimage.draw import polygon as sk_polygon
from skimage.morphology import skeletonize, binary_opening, disk


# ── Per-pixel category classifier ─────────────────────────────────────────

LAND = 1
WATER = 2
HALO = 3
SPRITE = 4
TEXT = 5
UNKNOWN = 0


def classify_pixels(img_rgb: np.ndarray) -> np.ndarray:
    """Return an int8 category map.

    Calibrated from /tmp/sample_colors.py readings (2026-06-08):
      WATER       — dark saturated blue   lum < ~120  B > R, B > G
      HALO        — light blue-tinted     lum 130-220 B slightly > R,G
      LAND/sky    — uniform light gray    lum > 180   R≈G≈B (no blue tint)
      SPRITE      — saturated bright       white/yellow/green icons
    """
    R = img_rgb[:, :, 0].astype(np.int16)
    G = img_rgb[:, :, 1].astype(np.int16)
    B = img_rgb[:, :, 2].astype(np.int16)
    lum = (R + G + B) // 3
    # Blueness ratio — B vs the mean of R,G.  Positive = blue-leaning.
    blueness = B - (R + G) // 2

    # WATER: dark, saturated blue.
    water = (lum < 130) & (blueness > 10) & (B > R + 10)

    # SPRITE: high-saturation icons.  Yellow (R+G high, B low), green
    # (G high), or pure-white markers (all 3 very high & uniform).
    sat_yellow = (R > 180) & (G > 150) & (B < 130)
    sat_green  = (G > 140) & (G - R > 30) & (G - B > 30)
    pure_white = (R > 220) & (G > 220) & (B > 220) & (abs(R - G) < 10)
    sprite     = sat_yellow | sat_green | pure_white

    # LAND / sky: uniform-gray background, light.  This is the stop
    # signal for the ray-cast.  Require near-zero blue tint and high
    # luminance.
    uniform = (np.abs(R - G) < 12) & (np.abs(G - B) < 12) & (np.abs(R - B) < 12)
    land    = (~sprite) & uniform & (lum > 180)

    # HALO: light blue-tinted, doesn't qualify as water or as land.
    halo = (~water) & (~land) & (~sprite) & (lum > 130) & (blueness > 3)

    # TEXT: light gray with mild non-uniformity (anti-aliased edges).
    # Mostly only matters for letters bigger than a few px.  We treat
    # near-uniform pixels above 170 lum but below 195 as "could be text
    # interior" — the ray-cast should pass through these so they don't
    # become false bank hits.  Reuse LAND classification but exclude
    # the brightest core.
    text = (~sprite) & uniform & (lum > 170) & (lum <= 180)

    cat = np.full(img_rgb.shape[:2], UNKNOWN, dtype=np.int8)
    cat[halo]   = HALO
    cat[water]  = WATER
    cat[text]   = TEXT
    cat[land]   = LAND
    cat[sprite] = SPRITE
    return cat


# ── Ship-icon localization (avoid hard dependency on MinimapNavigationView) ──

def find_ship_xy(img_rgb: np.ndarray) -> Optional[Tuple[int, int]]:
    """Try MinimapNavigationView's heading-detector ship localization
    first (most robust — handles overlays and edge cases).  Fall back
    to a centre-biased largest-green-blob heuristic."""
    try:
        # Wrap the minimap crop in a synthetic full frame and let
        # MinimapNavigationView find the ship.
        from vision.minimap_navigation_view import (
            MinimapNavigationView, MINIMAP_CROP,
        )
        from PIL import Image as _PILImage
        h, w = img_rgb.shape[:2]
        full = _PILImage.new("RGB", (2400, 1080), (0, 0, 0))
        full.paste(_PILImage.fromarray(img_rgb),
                   (MINIMAP_CROP[0], MINIMAP_CROP[1]))
        nav = MinimapNavigationView.from_frame(full)
        if nav.ship_xy is not None:
            sx, sy = nav.ship_xy
            # Within bounds of the crop we're working with?
            if 0 <= int(sx) < w and 0 <= int(sy) < h:
                return (int(sx), int(sy))
    except Exception:
        pass
    # Fallback: largest green blob biased toward image center
    R = img_rgb[:, :, 0].astype(np.int16)
    G = img_rgb[:, :, 1].astype(np.int16)
    B = img_rgb[:, :, 2].astype(np.int16)
    green = (G > 140) & (G - R > 30) & (G - B > 30)
    if not green.any():
        h, w = img_rgb.shape[:2]
        return (w // 2, h // 2)
    labels, n = cc_label(green)
    if n == 0:
        h, w = img_rgb.shape[:2]
        return (w // 2, h // 2)
    h, w = img_rgb.shape[:2]
    cx, cy = w / 2.0, h / 2.0
    # Score = size / (1 + distance_to_center)
    best_score = -1.0
    best_xy = (w // 2, h // 2)
    for lab in range(1, n + 1):
        ys, xs = np.where(labels == lab)
        if ys.size == 0:
            continue
        mx, my = float(xs.mean()), float(ys.mean())
        d = math.hypot(mx - cx, my - cy)
        score = ys.size / (1.0 + d)
        if score > best_score:
            best_score = score
            best_xy = (int(mx), int(my))
    return best_xy


# ── Ray-cast bank detection ───────────────────────────────────────────────

def raycast_banks(
    img_rgb: np.ndarray,
    ship_xy: Tuple[int, int],
    n_rays: int = 360,
    max_radius: int = 300,
) -> Tuple[np.ndarray, List[Tuple[int, int, str]]]:
    """Cast rays from ship_xy outward.  Each ray stops at the first
    LAND pixel.  Returns:
      - water_mask: bool array, True where ray-reachable from ship
        through non-LAND pixels.
      - bank_points: list of (x, y, kind) where kind is 'closed' (hit
        land) or 'open' (exited viewport).
    """
    h, w = img_rgb.shape[:2]
    cat = classify_pixels(img_rgb)
    water = np.zeros((h, w), dtype=bool)
    # Mark a tiny disc at the ship as water seed
    sx, sy = ship_xy
    water[max(sy-2, 0):sy+3, max(sx-2, 0):sx+3] = True

    bank_points: List[Tuple[int, int, str]] = []

    for i in range(n_rays):
        theta = 2.0 * math.pi * i / n_rays
        dx = math.sin(theta)
        dy = -math.cos(theta)
        # State machine: we're "seeking water" until we've seen WATER
        # pixels for at least MIN_WATER_HITS steps.  After that, the
        # first NON-WATER non-sprite pixel is the bank.  SPRITE/TEXT
        # never change state — they're features inside water.
        # Only need ONE water hit before the first non-water counts as
        # the bank.  2 was too strict — rays that briefly cross the
        # channel can fail to "register" and continue past the bank
        # (visible at t300 where the gap between two river segments
        # was being marked as water, and t571 where the narrow Bari
        # channel let some rays cross to the far bank).
        MIN_WATER_HITS = 1
        seen_water = 0
        passed: List[Tuple[int, int]] = []
        hit_kind = None
        last_water: Optional[Tuple[int, int]] = None
        for r in range(1, max_radius + 1):
            x = int(round(sx + r * dx))
            y = int(round(sy + r * dy))
            if not (0 <= x < w and 0 <= y < h):
                hit_kind = "open"
                break
            c = cat[y, x]
            passed.append((x, y))
            if c == SPRITE or c == TEXT:
                continue                 # invisible to the state machine
            if c == WATER:
                seen_water += 1
                last_water = (x, y)
                continue
            # HALO / LAND / UNKNOWN  — non-water
            if seen_water >= MIN_WATER_HITS:
                # This is the bank.  Record the LAST water pixel seen
                # as the closed bank point (the inside edge of the
                # bank, which is what the user's red pen also traces).
                bx, by = last_water if last_water else (x, y)
                bank_points.append((bx, by, "closed"))
                hit_kind = "closed"
                break
            # otherwise: still seeking water; keep marching through halo
        # Everything we marched through (water + sprites + skipped halo
        # while seeking) counts as water-region for the polygon fill,
        # bounded by where we declared the bank.
        if hit_kind == "closed" and last_water:
            cutoff = passed.index(last_water) + 1
        else:
            cutoff = len(passed)
        for (x, y) in passed[:cutoff]:
            water[y, x] = True
        if hit_kind == "open" and passed:
            bank_points.append((*passed[-1], "open"))

    return water, bank_points


# ── Ground-truth water mask: flood-fill inside the red bank lines ─────────

def gt_water_mask(bank_line_mask: np.ndarray,
                  ship_xy: Tuple[int, int]) -> np.ndarray:
    """Build a tight GT water mask from the user's red bank lines.

    Step 1: row-wise candidate.  For each ROW that contains bank pixels,
            fill BETWEEN consecutive bank pixels in that row.  This
            captures the water sliver between left & right banks for
            vertical rivers.
    Step 2: column-wise candidate.  Same but per column — captures
            water for horizontal segments of the river.
    Step 3: candidate water = union of both, MINUS the bank pixels
            themselves.
    Step 4: take the connected component containing ship_xy.
            That gives water that's actually reachable through the
            river (excludes land gaps in a Y-junction etc).
    """
    h, w = bank_line_mask.shape
    cand = np.zeros((h, w), dtype=bool)
    # Row-wise fills
    for y in range(h):
        xs_in_row = np.where(bank_line_mask[y, :])[0]
        if xs_in_row.size >= 2:
            for i in range(len(xs_in_row) - 1):
                a, b = int(xs_in_row[i]), int(xs_in_row[i + 1])
                if b - a > 1:
                    cand[y, a + 1:b] = True
    # Column-wise fills
    for x in range(w):
        ys_in_col = np.where(bank_line_mask[:, x])[0]
        if ys_in_col.size >= 2:
            for i in range(len(ys_in_col) - 1):
                a, b = int(ys_in_col[i]), int(ys_in_col[i + 1])
                if b - a > 1:
                    cand[a + 1:b, x] = True
    # Exclude bank line pixels themselves
    cand &= ~bank_line_mask
    # Connected component containing ship
    labels, n = cc_label(cand)
    sx, sy = ship_xy
    if 0 <= sy < h and 0 <= sx < w:
        ship_label = labels[sy, sx]
        if ship_label > 0:
            return labels == ship_label
    if n > 0:
        sizes = np.bincount(labels.ravel())
        sizes[0] = 0
        biggest = int(sizes.argmax())
        return labels == biggest
    return np.zeros_like(bank_line_mask, dtype=bool)


def iou(a: np.ndarray, b: np.ndarray) -> float:
    a = a.astype(bool); b = b.astype(bool)
    inter = (a & b).sum()
    union = (a | b).sum()
    return float(inter / union) if union else 0.0


def extract_skel(water_mask: np.ndarray) -> Tuple[np.ndarray, List[Tuple[int, int]], List[Tuple[int, int]]]:
    """Smooth the ray-cast polygon, then skeletonize.

    The ray-cast polygon has hundreds of small bumps along its
    perimeter (each ray's stopping pixel becomes a "vertex").  Raw
    skeletonization would sprout one spur per bump.  Pre-filtering:

      1. fill_holes — close any wedge-shaped gaps between rays AND
         any enclosed sprite-holes that may remain
      2. binary_dilation then binary_opening with a disc — smooths the
         perimeter at a length-scale of a few pixels, so small bumps
         get absorbed but real channel shape is preserved
    """
    m = binary_fill_holes(water_mask)
    m = binary_dilation(m, iterations=2)
    m = binary_opening(m, disk(3))
    sk = skeletonize(m)
    # Count 8-connected skeleton neighbours per skeleton pixel
    h, w = sk.shape
    ys, xs = np.where(sk)
    branches, endpoints = [], []
    for (y, x) in zip(ys, xs):
        cnt = 0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0: continue
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and sk[ny, nx]:
                    cnt += 1
        if cnt >= 3:
            branches.append((y, x))
        elif cnt == 1:
            endpoints.append((y, x))
    return sk, branches, endpoints


def bank_line_distance(
    gt_bank_mask: np.ndarray,
    detected_bank_pts: List[Tuple[int, int, str]],
) -> Tuple[float, float]:
    """Bank line matching: for every detected CLOSED bank point, find
    its nearest GT bank pixel and report the mean distance.  Lower is
    better — 0 = perfect overlap.  Also returns the recall side:
    mean distance from every GT bank pixel to its nearest detected
    bank point."""
    closed = [(x, y) for x, y, k in detected_bank_pts if k == "closed"]
    if not closed:
        return float("nan"), float("nan")
    gys, gxs = np.where(gt_bank_mask)
    if gys.size == 0:
        return float("nan"), float("nan")
    gt_pts = np.column_stack([gxs, gys])    # (N, 2) in (x, y)
    det_pts = np.array(closed)               # (M, 2)
    # M×N distances — fine at this resolution
    dists = np.linalg.norm(
        det_pts[:, None, :] - gt_pts[None, :, :], axis=-1
    )
    precision = float(dists.min(axis=1).mean())  # mean distance from each det to nearest GT
    recall    = float(dists.min(axis=0).mean())  # mean distance from each GT to nearest det
    return precision, recall


# ── Driver: run on 9 labeled frames, render evaluation grid ───────────────

LABELED = [
    (5,   "delta_apex",    "/tmp/tick_0005.png",
     "/tmp/bank_ground_truth/tick_0005_delta_apex_bank_mask.png"),
    (100, "clean_channel", "/tmp/tick_0100.png",
     "/tmp/bank_ground_truth/tick_0100_clean_channel_bank_mask.png"),
    (152, "nubia_village", "/tmp/tick_0152.png",
     "/tmp/bank_ground_truth/tick_0152_nubia_village_bank_mask.png"),
    (300, "viewport_bend", "/tmp/tick_0300.png",
     "/tmp/bank_ground_truth/tick_0300_viewport_bend_bank_mask.png"),
    (475, "Y_junction",    "/tmp/tick_0475.png",
     "/tmp/bank_ground_truth/tick_0475_Y_junction_bank_mask.png"),
    (500, "dead_end",      "/tmp/tick_0500.png",
     "/tmp/bank_ground_truth/tick_0500_dead_end_bank_mask.png"),
    (571, "bari_village",  "/tmp/tick_0571_bari_village.png",
     "/tmp/bank_ground_truth/tick_0571_bari_village_bank_mask.png"),
    (750, "U_bend",        "/tmp/tick_0750_y_branch.png",
     "/tmp/bank_ground_truth/tick_0750_U_bend_bank_mask.png"),
    (786, "lake_mouth",    "/tmp/tick_0786_lake_at_end.png",
     "/tmp/bank_ground_truth/tick_0786_lake_mouth_bank_mask.png"),
]


def _try_paths(*paths) -> Optional[Path]:
    """Return the first path that exists, or None."""
    for p in paths:
        if Path(p).exists():
            return Path(p)
    return None


def main():
    fig, axes = plt.subplots(3, 3, figsize=(16, 12))
    ious = []
    scores = []
    print("scenario             ship_xy        img_size      "
          "GT water pixels      GT % of frame")
    print("-" * 90)
    pct_summary = []

    for i, (tk, scenario, raw_path, gt_path) in enumerate(LABELED):
        raw_p = _try_paths(raw_path,
                           f"/tmp/tick_{tk:04d}.png",
                           f"/tmp/tick_{tk:04d}_{scenario}.png")
        gt_p = Path(gt_path)
        if raw_p is None or not gt_p.exists():
            ax = axes[i // 3][i % 3]
            ax.text(0.5, 0.5,
                    f"missing files for t{tk} {scenario}\n"
                    f"src tried: {raw_path}\ngt: {gt_path}",
                    ha="center", va="center", fontsize=10, color="red",
                    transform=ax.transAxes)
            ax.axis("off")
            continue

        img = np.array(Image.open(raw_p).convert("RGB"))
        gt_mask = np.array(Image.open(gt_p).convert("L")) > 127
        # GT bank mask is sized to the annotated image, which may have
        # been cropped a few pixels differently from raw.  Resize if so.
        if gt_mask.shape != img.shape[:2]:
            gt_pil = Image.open(gt_p).convert("L").resize(
                (img.shape[1], img.shape[0]), Image.NEAREST)
            gt_mask = np.array(gt_pil) > 127

        ship_xy = find_ship_xy(img)
        if ship_xy is None:
            continue

        det_water, bank_pts = raycast_banks(img, ship_xy)
        gt_water = gt_water_mask(gt_mask, ship_xy)
        score = iou(det_water, gt_water)
        prec, rec = bank_line_distance(gt_mask, bank_pts)
        ious.append((tk, scenario, score))
        scores.append((tk, scenario, score, prec, rec))

        n_closed = sum(1 for *_, k in bank_pts if k == "closed")
        n_open   = sum(1 for *_, k in bank_pts if k == "open")
        total_px = img.shape[0] * img.shape[1]
        gt_pct = 100.0 * gt_water.sum() / total_px
        pct_summary.append((tk, scenario, int(gt_water.sum()), total_px, gt_pct))
        print(f"  t{tk:>3} {scenario:<16} ({ship_xy[0]:>3},{ship_xy[1]:>3})   "
              f"{img.shape[1]}×{img.shape[0]} = {total_px:>6,}   "
              f"{int(gt_water.sum()):>6,}            "
              f"{gt_pct:>5.1f}%")

        sk_raw = skeletonize(det_water)
        sk, branches, endpoints = extract_skel(det_water)

        # Compute GT water percentage of image
        total_px = img.shape[0] * img.shape[1]
        gt_pct = 100.0 * gt_water.sum() / total_px

        ax = axes[i // 3][i % 3]
        ax.imshow(img)
        # GT water region — high-opacity bright red fill so it's
        # unmistakable.  The user asked specifically to see this.
        gt_overlay = np.zeros((*img.shape[:2], 4))
        gt_overlay[gt_water] = [1, 0, 0, 0.65]
        ax.imshow(gt_overlay, zorder=1)
        # Detected water polygon — cyan outline only (not filled) so it
        # doesn't obscure the GT below.
        det_outline = det_water & ~binary_dilation(~det_water,
                                                    iterations=1)
        det_overlay = np.zeros((*img.shape[:2], 4))
        det_overlay[det_water] = [0, 1, 1, 0.12]
        ax.imshow(det_overlay, zorder=2)
        # GT BANK LINE — bright red ON TOP, visible even where cyan
        # overlaps.  This is what the user actually drew with red pen.
        gys, gxs = np.where(gt_mask)
        ax.scatter(gxs, gys, color="red", s=4, alpha=1.0,
                   zorder=8, label=f"GT bank line ({gt_mask.sum()} px)")
        # Detected bank dots — bright yellow ON TOP of everything else
        if bank_pts:
            cx = [p[0] for p in bank_pts if p[2] == "closed"]
            cy = [p[1] for p in bank_pts if p[2] == "closed"]
            ox = [p[0] for p in bank_pts if p[2] == "open"]
            oy = [p[1] for p in bank_pts if p[2] == "open"]
            ax.scatter(cx, cy, color="yellow", s=8,
                       edgecolor="black", linewidth=0.3, zorder=9,
                       label=f"{n_closed} det bank")
            if ox:
                ax.scatter(ox, oy, color="magenta", s=4, zorder=7,
                           label=f"{n_open} open")
        # Skeletons (drawn UNDER the bank dots so they don't hide them)
        ys_r, xs_r = np.where(sk_raw)
        ax.scatter(xs_r, ys_r, color="darkorange", s=1.5, alpha=0.5,
                   zorder=4, label=f"raw skel ({int(sk_raw.sum())} px)")
        ys, xs = np.where(sk)
        ax.scatter(xs, ys, color="black", s=1.8, alpha=0.85,
                   zorder=5, label=f"smoothed ({int(sk.sum())} px)")
        if endpoints:
            ax.scatter([p[1] for p in endpoints], [p[0] for p in endpoints],
                       color="lime", s=22, marker="s", linewidth=0,
                       zorder=6, label=f"{len(endpoints)} ep")
        ax.scatter([ship_xy[0]], [ship_xy[1]], color="lime", s=110,
                   marker="o", edgecolor="white", linewidth=1.8,
                   zorder=10)
        ax.set_title(f"t{tk} {scenario}   prec={prec:.1f}px  "
                     f"GT water = {gt_water.sum():,} px ({gt_pct:.1f}% of "
                     f"{total_px:,})",
                     fontsize=9)
        ax.legend(loc="lower right", fontsize=6)
        ax.axis("off")

    if ious:
        mean = sum(s for *_, s in ious) / len(ious)
    else:
        mean = 0.0
    fig.suptitle(f"Ray-cast bank detection vs GT  —  mean IoU = {mean:.3f}\n"
                 "red=GT water, cyan=detected water, yellow=closed bank, "
                 "magenta=open (viewport edge), green=ship",
                 fontsize=12, fontweight="bold")
    fig.tight_layout()
    out = "/tmp/bank_raycast_eval.png"
    fig.savefig(out, dpi=130, bbox_inches="tight")
    print()
    print(f"saved: {out}")
    print(f"mean IoU: {mean:.3f}")


if __name__ == "__main__":
    main()
