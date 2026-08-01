"""Replay the centerline-waypoint prototype over a recorded voyage.

For every tick frame in a session directory, run M6f centerline
extraction + 50-px-lookahead waypoint picking and emit:

  - JSONL log: one line per tick (nodes, edges, ship_xy, heading,
    waypoint, visited_node_ids, hug_side, lookahead_px, mask_px).
  - Stratified composite PNGs every STRIDE ticks showing the chosen
    waypoint.
  - Summary stats (% ticks with valid waypoint, % with junctions
    encountered, etc.).

This is per-tick analysis — each frame is processed with the
recorded ship position, NOT closed-loop simulation of ship movement
from the chosen waypoints.

Usage
─────
  python -m tools.replay_centerline_waypoint \\
      data/sessions/reference_nile_full_20260606_141824 \\
      --outbound-only 800 --stride 50 --hug left
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.centerline_extraction_prototype import (  # noqa: E402
    channel_mask_from_rgb, _ship_xy,
)
from tools.centerline_waypoint_prototype import (  # noqa: E402
    extract_tree, estimate_ship_heading, pick_waypoint, render,
    LOOKAHEAD_PX, HUG_SIDE,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--outbound-only", type=int, default=None,
                    help="stop after this tick (e.g. 800 for Nile outbound)")
    ap.add_argument("--stride", type=int, default=50,
                    help="render a composite every N ticks (0 to skip)")
    ap.add_argument("--hug", choices=("left", "right"), default=HUG_SIDE)
    ap.add_argument("--lookahead", type=int, default=LOOKAHEAD_PX)
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="directory for outputs (default: session_dir/centerline_waypoint_replay)")
    args = ap.parse_args()

    sess = args.session_dir
    if not sess.is_dir():
        sys.exit(f"session dir not found: {sess}")
    out_dir = args.out_dir or (sess / "centerline_waypoint_replay")
    out_dir.mkdir(parents=True, exist_ok=True)
    composites_dir = out_dir / "composites"
    composites_dir.mkdir(exist_ok=True)
    log_path = out_dir / "ticks.jsonl"

    frame_paths = sorted(sess.glob("tick_*.png"))
    if args.outbound_only:
        frame_paths = [p for p in frame_paths
                      if int(p.stem.split("_")[-1]) <= args.outbound_only]
    print(f"session   : {sess}")
    print(f"out       : {out_dir}")
    print(f"frames    : {len(frame_paths)}")
    print(f"hug       : {args.hug}   lookahead: {args.lookahead}px")
    print(f"stride    : every {args.stride} ticks render composite")
    print()

    counters = {
        "valid": 0, "no_ship": 0, "no_heading": 0, "no_tree": 0,
        "no_waypoint": 0, "junction_hit": 0,
    }
    elapsed_ms = []
    with log_path.open("w") as log_f:
        for i, fp in enumerate(frame_paths):
            tick = int(fp.stem.split("_")[-1])
            rgb_raw = np.asarray(Image.open(fp).convert("RGB"))
            t0 = time.perf_counter()
            tree, mask = extract_tree(rgb_raw)
            rgb = rgb_raw[6:, :-6] if rgb_raw.shape[1] > 6 else rgb_raw
            ship = _ship_xy(rgb)
            heading = estimate_ship_heading(rgb)
            dest, walked, visited = None, [], []
            why = None
            if not tree.nodes:                why = "no_tree"
            elif ship is None:                 why = "no_ship"
            elif heading is None:              why = "no_heading"
            else:
                dest, walked, visited = pick_waypoint(
                    tree, ship, heading, args.hug, args.lookahead)
                if dest is None:               why = "no_waypoint"
            elapsed_ms.append((time.perf_counter() - t0) * 1e3)

            if why is None:
                counters["valid"] += 1
                if any(tree.nodes[nid].kind == "junction" for nid in visited):
                    counters["junction_hit"] += 1
            else:
                counters[why] += 1

            log_f.write(json.dumps({
                "tick": tick,
                "mask_px": int(mask.sum()),
                "n_nodes": len(tree.nodes),
                "n_edges": len(tree.edges),
                "ship_xy": ship,
                "heading": heading,
                "waypoint": dest,
                "visited": visited,
                "skip_reason": why,
                "elapsed_ms": round(elapsed_ms[-1], 2),
            }) + "\n")

            if args.stride and tick % args.stride == 0:
                render(rgb, mask, tree, ship, heading, dest, walked,
                       args.hug, composites_dir / f"tick_{tick:04d}.png")

            if (i + 1) % 50 == 0:
                print(f"  ... {i+1}/{len(frame_paths)}  "
                      f"valid={counters['valid']}  "
                      f"avg_ms={np.mean(elapsed_ms[-50:]):.1f}")

    total = len(frame_paths)
    print(f"\nDone.  total={total}")
    print(f"  valid waypoint  : {counters['valid']:>4} ({100*counters['valid']/total:.1f}%)")
    print(f"   ↳ hit junction : {counters['junction_hit']:>4}")
    print(f"  no_tree         : {counters['no_tree']:>4}")
    print(f"  no_ship         : {counters['no_ship']:>4}")
    print(f"  no_heading      : {counters['no_heading']:>4}")
    print(f"  no_waypoint     : {counters['no_waypoint']:>4}")
    print(f"  avg perception+pick latency: {np.mean(elapsed_ms):.1f} ms")
    print(f"  p95             : {np.percentile(elapsed_ms, 95):.1f} ms")
    print(f"\nLog:        {log_path}")
    print(f"Composites: {composites_dir} (every {args.stride} ticks)")


if __name__ == "__main__":
    main()
