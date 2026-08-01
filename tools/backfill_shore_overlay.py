"""Re-run the shore picker over a saved live_centerline session and
back-fill trace.jsonl with shore_pts / path_pts / waypoint_px so
tick_viewer can render the picker's view of the world.

The original session was captured before run_centerline_live started
writing these fields.  We reconstruct them from the saved
tick_NNNN.png raw minimap crops and the recorded heading.

Usage
─────
  python -m tools.backfill_shore_overlay \\
    data/sessions/live_centerline_2026-06-15T15-57-02 \\
    --side port --walk 80 --safe 25
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.centerline_extraction_prototype import channel_mask_from_rgb  # noqa: E402
from tools.shore_waypoint_picker import extract_shore_path  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--side", choices=("port", "starboard"), default="port")
    ap.add_argument("--walk", type=int, default=80,
                    help="shore_walk_px (must match the live run)")
    ap.add_argument("--safe", type=int, default=25,
                    help="shore_safe_px (must match the live run)")
    args = ap.parse_args()

    sess = args.session_dir
    trace_path = sess / "trace.jsonl"
    if not trace_path.exists():
        sys.exit(f"no trace.jsonl in {sess}")

    out_path = sess / "trace.jsonl"
    backup = sess / "trace.jsonl.pre_shore_backfill"
    if not backup.exists():
        backup.write_text(trace_path.read_text())
        print(f"backed up original → {backup.name}")

    new_lines: list[str] = []
    n_filled = 0
    n_skipped = 0
    for line in backup.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        tick = rec.get("tick")
        crop_name = rec.get("crop") or f"tick_{tick:04d}.png"
        crop_path = sess / crop_name
        heading = rec.get("commanded_deg_this") or rec.get("heading_deg")
        if not crop_path.exists() or heading is None:
            n_skipped += 1
            new_lines.append(json.dumps(rec))
            continue
        rgb_raw = np.asarray(Image.open(crop_path).convert("RGB"))
        _, mask = channel_mask_from_rgb(rgb_raw)
        H_mm, W_mm = mask.shape
        ship_center = (W_mm // 2, H_mm // 2)
        shore_pts, path_pts = extract_shore_path(
            mask, ship_center, float(heading),
            side=args.side,
            walk_distance_px=args.walk,
            safe_distance_px=args.safe,
        )
        rec["shore_pts"] = [list(p) for p in shore_pts]
        rec["path_pts"] = [list(p) for p in path_pts]
        rec["waypoint_px"] = list(path_pts[-1]) if path_pts else None
        rec["picker"] = "shore"
        rec["picker_side"] = args.side
        n_filled += 1
        new_lines.append(json.dumps(rec))

    out_path.write_text("\n".join(new_lines) + "\n")
    print(f"filled {n_filled} ticks, skipped {n_skipped}.  Wrote {out_path}")


if __name__ == "__main__":
    main()
