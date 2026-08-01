"""Visualize what the model actually trains on: for each of the 12
direction buckets, pick a representative sprite, rotate it to the
bucket center, and synthesize it under the three focus occlusion
strategies (front_half, back_half, center_band) plus clean.

Layout: 12 rows (buckets) × 4 columns (clean, front, back, center).
Each cell is a synthesized 80×80 training image with the true
heading annotated.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from brain.ai_nav.learned.heading_synthesis import synthesize, _is_water_heavy


SNAPSHOT = REPO / "data/heading_training_snapshot_v5_2026-07-01"
CLEAN_BGS = REPO / "data/heading_backgrounds_clean_2026-07-02"


def _is_pale_ribbon(sprite_id: str) -> bool:
    """Reject UI-notice ribbons that leaked into the sprite pool: their
    core RGB is pale mint (R>150 AND B>150), unlike real ship green."""
    arr = np.asarray(
        Image.open(SNAPSHOT/"sprites"/f"sprite_{sprite_id}.png"
                   ).convert("RGBA"))
    core = arr[..., 3] > 200
    if core.sum() < 5:
        return False
    return arr[core, 0].mean() > 150 and arr[core, 2].mean() > 150


def pick_best_sprite_in_bucket(manifest: list[dict],
                               lo: float, hi: float) -> dict | None:
    """From all sprites whose heading falls in [lo, hi), pick the one
    with the highest largest_component_frac × n_green — a good visual
    quality proxy."""
    candidates = [r for r in manifest
                  if lo <= r["heading_deg"] < hi
                  and not _is_pale_ribbon(r["sprite_id"])]
    if not candidates:
        return None
    return max(candidates,
               key=lambda r: r.get("largest_component_frac", 0)
                             * r.get("n_green", 0))


def main():
    manifest = [json.loads(l) for l in
                (SNAPSHOT/"sprites/manifest.jsonl").open()]

    # Load from the CLEAN water background pool (strict-filtered + procedural)
    bg_root = CLEAN_BGS if CLEAN_BGS.exists() else SNAPSHOT/"backgrounds"
    print(f"loading backgrounds from: {bg_root}")
    all_paths = sorted(bg_root.glob("bg_*.png"))
    # Skip the first 98 (real, still-may-have-streaks) — use only the
    # procedural water backgrounds which are guaranteed clean.
    procedural_paths = all_paths[98:] if len(all_paths) > 98 else all_paths
    import random as _rnd
    _rnd.seed(42)
    bg_paths = _rnd.sample(procedural_paths,
                           min(60, len(procedural_paths)))
    bgs = [np.asarray(Image.open(p).convert("RGB")) for p in bg_paths]
    print(f"loaded {len(bgs)} backgrounds (random-sampled from {len(all_paths)})")

    # Set up canvas
    cell_size = 200        # display size per cell (upscaled from 80)
    header_h = 40
    label_w = 130
    n_rows = 12
    strategies = [("none",         "clean"),
                  ("front_half",   "front occluded"),
                  ("back_half",    "back occluded"),
                  ("center_band",  "center occluded")]
    n_cols = len(strategies)
    W = label_w + n_cols * cell_size + 20
    H = header_h + n_rows * cell_size + 20
    canvas = Image.new("RGB", (W, H), (25, 25, 30))
    draw = ImageDraw.Draw(canvas)
    try:
        f_title = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 14)
        f_hdr = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 12)
        f_lbl = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 11)
    except (OSError, IOError):
        f_title = f_hdr = f_lbl = ImageFont.load_default()

    # Column headers
    for j, (_, name) in enumerate(strategies):
        x = label_w + j * cell_size + cell_size // 2
        draw.text((x - 50, 10), name,
                  fill=(220, 220, 230), font=f_title)

    # For each of 12 buckets, pick a sprite and render 4 versions
    for i in range(12):
        lo, hi = i * 30, (i + 1) * 30
        center_deg = i * 30 + 15   # 15, 45, 75, ..., 345
        rec = pick_best_sprite_in_bucket(manifest, lo, hi)
        y_top = header_h + i * cell_size

        # Row label
        cnt = sum(1 for r in manifest if lo <= r['heading_deg'] < hi)
        marker = "  ★" if cnt < 20 else ""
        draw.text((8, y_top + cell_size // 2 - 20),
                  f"{lo:3d}-{hi:3d}°\ncenter\n= {center_deg}°\n"
                  f"n={cnt}{marker}",
                  fill=(220, 220, 230), font=f_lbl)

        if rec is None:
            # Empty bucket — draw placeholder
            for j in range(n_cols):
                x0 = label_w + j * cell_size + 10
                y0 = y_top + 10
                draw.rectangle([x0, y0, x0+cell_size-20, y0+cell_size-20],
                               outline=(80, 40, 40), width=2)
                draw.text((x0 + 20, y0 + 80), "no sprite\nin this\nbucket",
                          fill=(200, 100, 100), font=f_hdr)
            continue

        sprite = np.asarray(
            Image.open(SNAPSHOT/"sprites"/f"sprite_{rec['sprite_id']}.png"
                       ).convert("RGBA"))
        # Rotate the sprite so its heading matches the bucket center.
        # rotation_deg is ADDED to source_heading_deg to get target.
        # We want target = center_deg, so rotation_deg = center_deg - source.
        rot_deg = (center_deg - rec['heading_deg']) % 360

        for j, (strat, _) in enumerate(strategies):
            cell_bg = _rnd.choice(bgs)
            ex = synthesize(sprite, rec['heading_deg'], cell_bg,
                            rotation_deg=rot_deg, strategy=strat)
            img = ex.image
            # Upscale to cell_size for viewing
            img_big = img.resize((cell_size - 20, cell_size - 20),
                                 Image.NEAREST)
            # Draw compass arrow at true heading
            img_big = _draw_compass(img_big, ex.heading_deg)
            x0 = label_w + j * cell_size + 10
            y0 = y_top + 10
            canvas.paste(img_big, (x0, y0))
            # small caption showing sprite_id + true heading
            draw.text((x0 + 4, y0 + cell_size - 30),
                      f"{rec['sprite_id']}  →{ex.heading_deg:.0f}°",
                      fill=(220, 220, 230), font=f_lbl)

    out = REPO / "data/bucket_occlusions_v5.png"
    canvas.save(out)
    print(f"wrote {out}")


def _draw_compass(img: Image.Image, heading_deg: float) -> Image.Image:
    """Overlay a small compass arrow pointing at heading_deg."""
    out = img.copy()
    draw = ImageDraw.Draw(out, "RGBA")
    W, H = out.size
    cx, cy = W // 2, H // 2
    r = min(W, H) // 3
    # Ring
    draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                 outline=(255, 255, 100, 120), width=2)
    # Arrow — bright yellow toward heading
    th = math.radians(heading_deg)
    dx, dy = math.sin(th), -math.cos(th)
    x2, y2 = cx + r * 0.85 * dx, cy + r * 0.85 * dy
    draw.line([(cx, cy), (x2, y2)],
              fill=(255, 200, 40, 255), width=4)
    for sign in (-1, 1):
        ah = th + sign * math.radians(25) + math.pi
        ax = x2 + 12 * math.sin(ah)
        ay = y2 - 12 * math.cos(ah)
        draw.line([(x2, y2), (int(ax), int(ay))],
                  fill=(255, 200, 40, 255), width=3)
    return out


if __name__ == "__main__":
    main()
