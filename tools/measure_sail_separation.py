"""Measure the ship-sprite sail-pair separation across a session.

Run after a sprite art change (or to update SAIL_PAIR_SEPARATION /
SAIL_PAIR_TOLERANCE constants in vision/minimap_navigation_view.py).

The sail-pair strategy in ShipHeadingDetector picks the two largest
yellow blobs on the ship hull and returns their angular bisector as the
bow direction.  This script walks all tick_*.png in a session, finds
ticks where two sail-sized candidates are visible, and prints the
separation angle so you can recompute the empirical bounds.

Usage:
    python tools/measure_sail_separation.py \\
        --session hug_debug_20260529_205124
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import label as ndi_label, binary_dilation

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from vision.minimap_navigation_view import (  # noqa: E402
    _color_masks, _clean_ship_green, ShipHeadingDetector,
)


def measure(crop_path: Path) -> dict | None:
    img = Image.open(crop_path).convert("RGB")
    arr = np.array(img)
    masks = _color_masks(arr)
    green = _clean_ship_green(masks["ship_green"])
    yellow = masks["yellow_any"]
    if green.sum() < ShipHeadingDetector.MIN_GREEN_PIXELS:
        return None
    gys, gxs = np.where(green)
    cx, cy = float(gxs.mean()), float(gys.mean())
    det = ShipHeadingDetector()
    principal = det._principal_axis(gxs, gys, cx, cy)
    cands = det._collect_yellow_candidates(green, yellow, cx, cy, principal)

    # Sail pool — same filter the sail_pair strategy uses.
    amin, amax = ShipHeadingDetector.SAIL_AREA_RANGE
    pool = [c for c in cands
            if amin <= c["area"] <= amax
            and c["dist"] <= ShipHeadingDetector.SAIL_MAX_DIST
            and c["on_axis"] and c["touches_green"]]
    if len(pool) < 2:
        return {"sails": pool, "separation": None}

    # Pick the two with separation closest to the configured ideal.
    best = None
    best_err = float("inf")
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            a1, a2 = pool[i]["angle"], pool[j]["angle"]
            sep = abs(((a1 - a2 + 540) % 360) - 180)
            err = abs(sep - ShipHeadingDetector.SAIL_PAIR_SEPARATION)
            if err < best_err:
                best_err = err
                best = (pool[i], pool[j], sep)
    s1, s2, sep = best
    return {"sails": [s1, s2], "separation": sep}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True,
                    help="session directory name under data/sessions/")
    args = ap.parse_args()

    session_dir = _ROOT / "data" / "sessions" / args.session
    if not session_dir.exists():
        raise SystemExit(f"no such session: {session_dir}")

    crops = sorted(session_dir.glob("tick_*.png"))
    if not crops:
        raise SystemExit(f"no tick_*.png in {session_dir}")

    seps = []
    print(f"{'tick':>5} {'sails':>5} {'sep°':>6}  detail")
    for crop in crops:
        r = measure(crop)
        if r is None:
            continue
        tick = crop.stem.replace("tick_", "").lstrip("0") or "0"
        if r["separation"] is None:
            n = len(r["sails"])
            print(f"{tick:>5} {n:>5} {'—':>6}  fewer than 2 sail candidates")
            continue
        seps.append(r["separation"])
        s1, s2 = r["sails"]
        print(f"{tick:>5} {2:>5} {r['separation']:>6.1f}  "
              f"sail1=(a={s1['area']}, d={s1['dist']:.0f}, "
              f"hdg={s1['angle']:.0f}°)  "
              f"sail2=(a={s2['area']}, d={s2['dist']:.0f}, "
              f"hdg={s2['angle']:.0f}°)")

    if not seps:
        print("\nno two-sail ticks measured.")
        return 0

    seps_arr = np.array(seps)
    print()
    print(f"sail-pair separation summary over {len(seps)} ticks:")
    print(f"  mean   = {seps_arr.mean():.2f}°")
    print(f"  median = {np.median(seps_arr):.2f}°")
    print(f"  stdev  = {seps_arr.std():.2f}°")
    print(f"  range  = {seps_arr.min():.2f}° to {seps_arr.max():.2f}°")
    cur = ShipHeadingDetector.SAIL_PAIR_SEPARATION
    tol = ShipHeadingDetector.SAIL_PAIR_TOLERANCE
    out_of_band = int(((seps_arr < cur - tol) | (seps_arr > cur + tol)).sum())
    print(f"  configured center  = {cur}°  ±{tol}°  "
          f"(in-band ticks: {len(seps) - out_of_band}/{len(seps)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
