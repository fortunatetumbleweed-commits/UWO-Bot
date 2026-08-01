"""Show a broader sample of training data per direction bucket.

For each of the 12 buckets, render 6 random synthesized training
samples using the current synthesis pipeline.  Each cell samples a
random sprite from the bucket and a random occlusion strategy from
the training distribution — matching what an epoch actually shows.
"""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from brain.ai_nav.learned.heading_synthesis import synthesize, _is_water_heavy


SNAPSHOT = REPO / "data/heading_training_snapshot_v5_2026-07-01"


def main():
    manifest = [json.loads(l) for l in
                (SNAPSHOT/"sprites/manifest.jsonl").open()]

    # Load a pool of water-heavy backgrounds
    print("filtering backgrounds to water-heavy pool...")
    bgs = []
    for p in sorted((SNAPSHOT/"backgrounds").glob("bg_*.png")):
        arr = np.asarray(Image.open(p).convert("RGB"))
        if _is_water_heavy(arr, min_frac=0.6):
            bgs.append(arr)
    print(f"water-heavy backgrounds: {len(bgs)}")
    if not bgs:
        raise SystemExit("no water-heavy backgrounds")

    # Canvas: 12 rows × 6 samples per row
    cell = 180
    header_h = 30
    label_w = 110
    n_rows = 12
    n_cols = 6
    W = label_w + n_cols * cell + 20
    H = header_h + n_rows * cell + 20
    canvas = Image.new("RGB", (W, H), (20, 20, 25))
    draw = ImageDraw.Draw(canvas)
    try:
        f_title = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 14)
        f_lbl = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 10)
    except (OSError, IOError):
        f_title = f_lbl = ImageFont.load_default()

    draw.text((15, 8),
              "training samples per 30° bucket "
              "(6 random draws each, current synthesis pipeline)",
              fill=(230, 230, 235), font=f_title)

    random.seed(42)  # reproducible

    for i in range(n_rows):
        lo, hi = i * 30, (i + 1) * 30
        center_deg = i * 30 + 15
        y0 = header_h + i * cell + 5
        # Row label
        rows_in_bucket = [r for r in manifest
                          if lo <= r["heading_deg"] < hi]
        n_avail = len(rows_in_bucket)
        marker = "  ★" if n_avail < 20 else ""
        draw.text((8, y0 + cell // 2 - 24),
                  f"{lo:3d}-{hi:3d}°\ncenter\n= {center_deg}°\n"
                  f"n={n_avail}{marker}",
                  fill=(220, 220, 235), font=f_lbl)

        if not rows_in_bucket:
            for j in range(n_cols):
                x = label_w + j * cell + 5
                draw.rectangle([x, y0, x + cell - 15, y0 + cell - 15],
                               outline=(70, 40, 40), width=2)
                draw.text((x + 20, y0 + 60), "no sprite",
                          fill=(180, 100, 100), font=f_lbl)
            continue

        for j in range(n_cols):
            rec = random.choice(rows_in_bucket)
            sprite = np.asarray(
                Image.open(SNAPSHOT/"sprites"/f"sprite_{rec['sprite_id']}.png"
                           ).convert("RGBA"))
            bg = random.choice(bgs)
            # Rotate to bucket's center + small jitter to show variety
            rot_deg = ((center_deg + random.uniform(-10, 10))
                       - rec['heading_deg']) % 360
            # synthesize picks a random strategy from _STRATEGIES
            ex = synthesize(sprite, rec['heading_deg'], bg,
                            rotation_deg=rot_deg, strategy=None)
            img = ex.image
            img_big = img.resize((cell - 15, cell - 15), Image.NEAREST)
            # Compass arrow at true heading
            img_big = _draw_compass(img_big, ex.heading_deg)
            x = label_w + j * cell + 5
            canvas.paste(img_big, (x, y0))
            # Caption
            draw.text((x + 4, y0 + cell - 25),
                      f"{rec['sprite_id']}  →{ex.heading_deg:.0f}°  "
                      f"vis={ex.visibility_frac:.0%}",
                      fill=(220, 220, 235), font=f_lbl)

    out = REPO / "data/bucket_samples_v5.png"
    canvas.save(out)
    print(f"wrote {out}")


def _draw_compass(img: Image.Image, heading_deg: float) -> Image.Image:
    out = img.copy()
    draw = ImageDraw.Draw(out, "RGBA")
    W, H = out.size
    cx, cy = W // 2, H // 2
    r = min(W, H) // 3
    draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                 outline=(255, 255, 100, 100), width=1)
    th = math.radians(heading_deg)
    dx, dy = math.sin(th), -math.cos(th)
    x2, y2 = cx + r * 0.85 * dx, cy + r * 0.85 * dy
    draw.line([(cx, cy), (x2, y2)],
              fill=(255, 200, 40, 255), width=3)
    for sign in (-1, 1):
        ah = th + sign * math.radians(25) + math.pi
        ax = x2 + 10 * math.sin(ah)
        ay = y2 - 10 * math.cos(ah)
        draw.line([(x2, y2), (int(ax), int(ay))],
                  fill=(255, 200, 40, 255), width=2)
    return out


if __name__ == "__main__":
    main()
