"""Sample 80×80 background crops from real minimap frames.

These are 'what's behind the ship' — water, shore, junctions, lake
edges, village markers — but WITHOUT the player's ship.  Used by the
heading-CNN synthesizer to composite a ship-only sprite over a
realistic minimap background.

Strategy: for each minimap frame, pick crops that are >= MIN_DIST px
from the ship's centroid, so the sampled patch can't accidentally
include any of the ship.  Reject patches that contain green pixels
above MAX_GREEN (in case the ship moved into the patch area between
ticks, or there's a second ship sprite nearby).

Usage
─────
  python -m tools.sample_minimap_backgrounds --per-session 30
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


CROP = 80
HALF = CROP // 2
MIN_DIST_FROM_SHIP = 80          # pixels
MAX_GREEN_IN_PATCH = 10          # green pixels = potential ship contamination


def _green_mask(rgb: np.ndarray) -> np.ndarray:
    R, G, B = (rgb[..., c].astype(np.int16) for c in range(3))
    return (G > 140) & (G - R > 30) & (G - B > 30)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels-root", default="data/heading_labels")
    ap.add_argument("--out-root", default="data/heading_backgrounds")
    ap.add_argument("--per-session", type=int, default=30)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    labels_root = Path(args.labels_root)
    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    manifest_path = out_root / "manifest.jsonl"

    next_id = 0
    counts = {"saved": 0, "rejected_green": 0, "rejected_dist": 0,
              "frame_missing": 0}

    with manifest_path.open("w") as man_f:
        for sess_dir in sorted(labels_root.iterdir()):
            labels_path = sess_dir / "labels.jsonl"
            if not labels_path.exists():
                continue
            # Mine ticks where mining was confident (gold) — most
            # likely to have a clean ship locatable.
            ticks_used = []
            for line in labels_path.open():
                rec = json.loads(line)
                if rec.get("agreement_class") in ("gold", "silver"):
                    ticks_used.append(rec)
            rng.shuffle(ticks_used)
            saved_this_session = 0
            for rec in ticks_used:
                if saved_this_session >= args.per_session:
                    break
                frame_path = Path(rec["frame_path"])
                if not frame_path.exists():
                    counts["frame_missing"] += 1
                    continue
                img = Image.open(frame_path).convert("RGB")
                rgb = np.asarray(img)
                H, W, _ = rgb.shape
                green = _green_mask(rgb)
                ys, xs = np.where(green)
                if len(ys) < 10:
                    continue
                ship_cy, ship_cx = float(ys.mean()), float(xs.mean())

                # Try a few random crops in this frame
                for _ in range(5):
                    if saved_this_session >= args.per_session:
                        break
                    cy = rng.randint(HALF, H - HALF)
                    cx = rng.randint(HALF, W - HALF)
                    dist = ((cy - ship_cy) ** 2 + (cx - ship_cx) ** 2) ** 0.5
                    if dist < MIN_DIST_FROM_SHIP:
                        counts["rejected_dist"] += 1
                        continue
                    crop = rgb[cy - HALF:cy + HALF, cx - HALF:cx + HALF]
                    g = _green_mask(crop)
                    if int(g.sum()) > MAX_GREEN_IN_PATCH:
                        counts["rejected_green"] += 1
                        continue
                    bid = f"{next_id:06d}"
                    next_id += 1
                    Image.fromarray(crop).save(out_root / f"bg_{bid}.png")
                    man_f.write(json.dumps({
                        "bg_id": bid,
                        "source_session": sess_dir.name,
                        "source_tick": rec["tick"],
                        "crop_at": [cy, cx],
                    }) + "\n")
                    counts["saved"] += 1
                    saved_this_session += 1

    print(f"\n=== BACKGROUND SAMPLING ===")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print(f"backgrounds at {out_root}/")


if __name__ == "__main__":
    main()
