"""Stitch a Nile world canvas from a voyage session.

Usage:
    python -m tools.build_nile_canvas <session_dir> [--px-per-deg 200] \
        [--out-dir data/nile_canvas]

Reads the session's `trace.jsonl` + `tick_NNNN.png` mini-map crops,
blends them into a big world canvas indexed by (lat, lon), and saves
three files to the output dir:

    nile_rgb.png      — stitched RGB world (looks like a giant mini-map)
    nile_water.png    — boolean water mask at the same scale
    nile_canvas.json  — {lat_origin, lon_origin, px_per_deg, ...}

The sim (`sim/nile_sim.py`, coming next) crops mini-map-sized windows
from these to render frames for the ai_nav pipeline in closed loop.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sim.nile_canvas import build_canvas, save_canvas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--px-per-deg", type=float, default=100.0,
                    help="Pixels per degree in the world canvas.  "
                         "100 → mini-map ≈ 2° of latitude (measured).")
    ap.add_argument("--out-dir", type=Path,
                    default=Path("data/nile_canvas"))
    args = ap.parse_args()

    if not args.session_dir.exists():
        sys.exit(f"Session not found: {args.session_dir}")

    rgb, water, meta = build_canvas(args.session_dir,
                                    px_per_deg=args.px_per_deg)
    save_canvas(rgb, water, meta, args.out_dir)
    print(f"→ RGB canvas:  {args.out_dir}/nile_rgb.png "
          f"({meta.width}×{meta.height})")
    print(f"→ Water mask:  {args.out_dir}/nile_water.png")
    print(f"→ Meta:        {args.out_dir}/nile_canvas.json")


if __name__ == "__main__":
    main()
