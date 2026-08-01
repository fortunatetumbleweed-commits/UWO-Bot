"""Diagnostic — overlay the perception's land-pixel mask on a frame.

Usage:
    python tools/diag_land_overlay.py <frame.png> <out.png>

Loads the frame, runs the same crop + masks the navigation pipeline
uses, then writes an annotated PNG showing:
  - the cropped mini-map
  - all pixels classified as "land" tinted red
  - the sprite mask outlined in cyan
  - the ship centroid marked
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np
from PIL import Image, ImageDraw

from vision.minimap_navigation_view import (
    _crop_minimap, _color_masks, _build_sprite_mask, _nav_area_mask,
    _land_mask, _ship_centroid, _disc_mask, DISC_RADIUS,
)


def annotate(frame_path: str, out_path: str) -> None:
    src = Image.open(frame_path).convert("RGB")
    # Saved tick PNGs are already cropped to the mini-map (381×184).
    # Only crop again if the frame is the full screen.
    if src.size == (2400, 1080):
        crop = _crop_minimap(src)
    else:
        crop = src
    rgb = np.asarray(crop)
    color = _color_masks(rgb)
    nav = _nav_area_mask(rgb.shape[:2])
    sprites = _build_sprite_mask(color)
    gray = np.asarray(crop.convert("L"))
    land = _land_mask(gray, nav & ~sprites)

    ship_xy = _ship_centroid(color["ship_green"])

    # Upscale for readability.
    SCALE = 3
    out = crop.resize(
        (crop.width * SCALE, crop.height * SCALE), Image.NEAREST,
    ).convert("RGBA")
    overlay = Image.new("RGBA", out.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Land pixels in red (alpha so the original shows through).
    land_xy = np.argwhere(land)  # (y, x)
    for y, x in land_xy:
        draw.rectangle(
            (x * SCALE, y * SCALE,
             x * SCALE + SCALE - 1, y * SCALE + SCALE - 1),
            fill=(255, 0, 0, 200),
        )

    # Ship centroid in bright green crosshair.
    if ship_xy is not None:
        cx, cy = ship_xy
        cx, cy = int(cx * SCALE), int(cy * SCALE)
        r = 8
        draw.line((cx - r, cy, cx + r, cy), fill=(0, 255, 0, 255), width=2)
        draw.line((cx, cy - r, cx, cy + r), fill=(0, 255, 0, 255), width=2)

    final = Image.alpha_composite(out, overlay)
    final.convert("RGB").save(out_path)

    n_land = int(land.sum())
    n_sprite = int(sprites.sum())
    print(f"{frame_path}: land px={n_land}  sprite px={n_sprite}  "
          f"ship_xy={ship_xy}  → {out_path}")


if __name__ == "__main__":
    annotate(sys.argv[1], sys.argv[2])
