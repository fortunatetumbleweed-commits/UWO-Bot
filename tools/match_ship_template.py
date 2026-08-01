"""Rotational template matcher for ship heading.

Loads the canonical ship template (stored at its raw source-frame
orientation, NOT north-aligned — the source heading is recorded
in PNG metadata).  For each test frame:

  1. Centroid the visible green pixels in a tight disc around the
     minimap's geometric center
  2. For each candidate heading 0°..359° (step `--step`):
     - Rotate the bow-up template by `heading` clockwise
     - Score it against the test window:
         - IoU on filled regions when the ship is mostly visible
           (filled asymmetry breaks the 180° tie, robust to text)
         - Forward edge chamfer when the ship is only partially
           visible (sums distances at visible test-edge pixels;
           asymmetric — missing template parts don't penalize)
  3. Best score = matched heading

Replaces PCA + brightness-tiebreaker.  Robust to overlays that
brighten one side of the ship — overlays don't change the SHAPE
of the green sprite, so the rotation-matched heading is stable.

Usage
─────
  python -m tools.match_ship_template \\
      data/reference/ship_template_raw.png \\
      data/sessions/ai_nav_live_palma \\
      --ticks 34,35,84,85,89,91,187 \\
      --step 5

Output:
  per-tick: matched_heading | recorded_heading | truth (if known)
  + saves a viz PNG showing best-match overlay
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def green_mask(rgb: np.ndarray) -> np.ndarray:
    R, G, B = (rgb[..., c].astype(np.int16) for c in range(3))
    return (G > 140) & (G - R > 30) & (G - B > 30)


def template_green_mask(template_rgba: Image.Image) -> np.ndarray:
    """The template's green pixels.  Template is canonical bow-up
    (rotated at extraction time)."""
    arr = np.asarray(template_rgba)
    if arr.shape[-1] == 4:
        rgb = arr[..., :3]
        alpha = arr[..., 3] > 64
        return green_mask(rgb) & alpha
    return green_mask(arr)


def rotate_template_mask(template_mask: np.ndarray,
                         compass_deg: float) -> np.ndarray:
    """Rotate the bow-up template to test compass-heading `compass_deg`.

    Template is canonical (bow points north = compass 0°).  To test
    heading H, rotate by H clockwise.  PIL rotate is CCW positive,
    so we pass -compass_deg.
    """
    pil = Image.fromarray((template_mask * 255).astype(np.uint8))
    rot = pil.rotate(-compass_deg, resample=Image.NEAREST, expand=False)
    return np.asarray(rot) > 128


def match_heading(test_rgb: np.ndarray,
                  template_mask: np.ndarray,
                  step: float = 5.0,
                  center_disc_radius: int = 25,
                  partial_match: str | bool = "auto",
                  tight_disc_radius: int = 15,
                  partial_pixel_floor: int = 200,
                  ) -> tuple[float, float, dict]:
    """Find best-matching compass heading for the ship in test_rgb.

    The ship is ALWAYS at the center of the minimap — but it has a
    few pixels of natural offset from the array's geometric center
    (the MINIMAP_CROP region isn't perfectly aligned to the in-game
    anchor point).  So:
      - look ONLY at a small disc around the geometric center
        (radius `center_disc_radius`) — small enough to exclude
        text overlays + adjacent sprites
      - compute centroid within that disc — handles the few-pixel
        offset between geometric center and actual ship anchor
      - test window centered on that centroid; IoU vs rotated template

    Returns (best_heading, best_iou, all_scores).
    """
    H, W = test_rgb.shape[:2]
    cy_geo, cx_geo = H // 2, W // 2

    # Disc-bounded green mask: ship's natural pixels, excludes
    # contamination from text overlays and adjacent NPC sprite edges.
    yy, xx = np.ogrid[:H, :W]
    disc = (yy - cy_geo) ** 2 + (xx - cx_geo) ** 2 <= center_disc_radius ** 2
    test_mask_full = green_mask(test_rgb) & disc
    if not test_mask_full.any():
        return 0.0, 0.0, {}
    ys, xs = np.where(test_mask_full)
    cy, cx = int(ys.mean()), int(xs.mean())

    th, tw = template_mask.shape
    half_h, half_w = th // 2, tw // 2
    y1 = max(0, cy - half_h); y2 = min(H, cy + half_h)
    x1 = max(0, cx - half_w); x2 = min(W, cx + half_w)
    test_window_mask = test_mask_full[y1:y2, x1:x2]
    ty1 = half_h - (cy - y1); ty2 = ty1 + (y2 - y1)
    tx1 = half_w - (cx - x1); tx2 = tx1 + (x2 - x1)

    n_test = int(test_window_mask.sum())
    scores: dict[float, float] = {}
    best_h, best_score = 0.0, -1.0

    from scipy.ndimage import (
        binary_dilation as _dilate, binary_erosion as _erode,
        distance_transform_edt as _dt,
    )
    test_edge = test_window_mask & ~_erode(test_window_mask, iterations=1)
    n_test_edge = int(test_edge.sum())

    # Mode selection: auto picks between "iou" (full ship visible,
    # asymmetric filled region breaks 180° tie) and "chamfer"
    # (partial visibility, edge-only matching is the right tool).
    #
    # The threshold: how many green pixels in a tight disc around the
    # geometric center?  If most of the ship is there, use filled IoU.
    # If only a fragment, use edge chamfer.
    if partial_match == "auto":
        # Count green pixels in a tight disc — closer than the full
        # disc used for centroid so that off-center contamination
        # (text glyphs, anti-aliasing) doesn't pump the count.
        tight = (yy - cy_geo) ** 2 + (xx - cx_geo) ** 2 \
                <= tight_disc_radius ** 2
        n_tight = int((green_mask(test_rgb) & tight).sum())
        mode = "chamfer" if n_tight < partial_pixel_floor else "iou"
    elif partial_match is True:
        mode = "chamfer"
    else:
        mode = "iou"

    h = 0.0
    while h < 360.0:
        rot = rotate_template_mask(template_mask, h)[ty1:ty2, tx1:tx2]
        if mode == "chamfer":
            # Classical asymmetric chamfer on edges (partial match).
            tpl_edge = rot & ~_erode(rot, iterations=1)
            if not tpl_edge.any() or n_test_edge == 0:
                score = 0.0
            else:
                dt = _dt(~tpl_edge)
                avg_dist = float(dt[test_edge].mean())
                score = 1.0 / (1.0 + avg_dist)
        else:
            # Classic IoU on filled regions (full ship).  Asymmetry
            # of the filled shape breaks the 180° tie.
            inter = int((rot & test_window_mask).sum())
            union = int((rot | test_window_mask).sum())
            score = inter / union if union > 0 else 0.0
        scores[round(h, 1)] = score
        if score > best_score:
            best_score = score
            best_h = h
        h += step
    return best_h, best_score, scores


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("template", type=Path)
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--ticks", required=True)
    ap.add_argument("--step", type=float, default=5.0)
    args = ap.parse_args()

    template_img = Image.open(args.template).convert("RGBA")
    template_mask = template_green_mask(template_img)
    print(f"template: {template_img.size}  green_px={int(template_mask.sum())}  "
          f"(canonical bow-up)")

    # Load trace for recorded headings (when available)
    trace_path = args.session_dir / "trace.jsonl"
    by_tick: dict[int, dict] = {}
    if trace_path.exists():
        for line in trace_path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                by_tick[r["tick"]] = r

    ticks = [int(t) for t in args.ticks.split(",")]
    print(f"\n{'tick':>5} | {'matched':>8} | {'pca/recorded':>12} | {'iou':>6} | "
          f"{'Δ':>6}")
    print("-" * 60)
    results = []
    for t in ticks:
        path = args.session_dir / f"tick_{t:04d}.png"
        if not path.exists():
            print(f"t{t:>4} | (missing)")
            continue
        rgb = np.asarray(Image.open(path).convert("RGB"))
        best, iou, scores = match_heading(rgb, template_mask, step=args.step)
        recorded = by_tick.get(t, {}).get("heading_deg", None)
        delta = None
        if recorded is not None:
            delta = abs(((best - recorded + 540) % 360) - 180)
        rec_str = f"{recorded:>8.1f}°" if recorded is not None else "    n/a"
        delta_str = f"{delta:>5.1f}°" if delta is not None else "  n/a"
        results.append((t, best, iou, recorded, delta))
        print(f"t{t:>4} | {best:>7.0f}° | {rec_str:>12} | "
              f"{iou:>5.2f} | {delta_str:>6}")

    print("\nNote: delta > 90° likely indicates a PCA 180° flip the "
          "matcher caught.")


if __name__ == "__main__":
    main()
