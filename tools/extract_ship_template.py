"""Extract the green ship sprite from a minimap frame, rotate it
to canonical bow-up (north) orientation, and save as an RGBA
template.

The canonical orientation means the matcher tests heading H by
simply rotating the template by -H (compass CW → PIL CCW negate),
no source-frame offset to subtract.

Use a source frame where the ship is fully visible, unoccluded,
and where the recorded PCA heading is known to be correct.

Why template matching works: the bow and stern have different
shapes, not just different brightness — so any overlay that
brightens half the ship can't flip the matched rotation by 180°.
Fix for the PCA 180° flip failure mode preserved in
data/reference/pca_corruption_cases/.

Usage
─────
  python -m tools.extract_ship_template \\
      data/sessions/ai_nav_live_palma/tick_0187.png \\
      --source-heading 32 \\
      --out data/reference/ship_template_raw.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def find_green_bbox(rgb: np.ndarray, pad: int = 4) -> tuple[int, int, int, int]:
    """Tight bbox around the green ship pixels (matches the PCA
    detector's green criterion)."""
    R, G, B = (rgb[..., c].astype(np.int16) for c in range(3))
    green = (G > 140) & (G - R > 30) & (G - B > 30)
    if not green.any():
        raise ValueError("no green ship pixels found")
    ys, xs = np.where(green)
    y1 = max(0, int(ys.min()) - pad)
    y2 = min(rgb.shape[0], int(ys.max()) + pad + 1)
    x1 = max(0, int(xs.min()) - pad)
    x2 = min(rgb.shape[1], int(xs.max()) + pad + 1)
    return y1, y2, x1, x2


def extract_ship_rgba(rgb: np.ndarray) -> Image.Image:
    """Crop the ship region, make non-ship pixels transparent.
    Returns an RGBA PIL Image at the natural orientation in the
    source frame."""
    y1, y2, x1, x2 = find_green_bbox(rgb)
    crop = rgb[y1:y2, x1:x2]
    R, G, B = (crop[..., c].astype(np.int16) for c in range(3))
    green = (G > 140) & (G - R > 30) & (G - B > 30)
    # Include green pixels + their immediate neighbours as alpha=255;
    # also include darker pixels that are likely "stern shadow" (lower
    # luminance but adjacent to green).  Keep it tight to avoid
    # background bleed.
    from scipy.ndimage import binary_dilation
    keep = binary_dilation(green, iterations=2)
    rgba = np.zeros((crop.shape[0], crop.shape[1], 4), dtype=np.uint8)
    rgba[..., :3] = crop
    rgba[..., 3] = np.where(keep, 255, 0)
    return Image.fromarray(rgba, mode="RGBA")


def rotate_to_north(ship_img: Image.Image, source_heading_deg: float
                    ) -> Image.Image:
    """Rotate the extracted ship so the bow points north (up).

    Source frame had the ship at compass heading source_heading_deg
    (0=N, CW).  PIL.rotate is CCW positive, so rotating by
    +source_heading_deg brings the bow from compass `source_heading_deg`
    to compass 0° (north).  BICUBIC + expand=True preserves all
    pixels through the rotation.
    """
    return ship_img.rotate(
        source_heading_deg, resample=Image.BICUBIC, expand=True,
    )


def square_pad(img: Image.Image, size: int) -> Image.Image:
    """Center the image in a transparent square of given size.  Lets
    the template hold every rotation without losing corners."""
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ox = (size - img.size[0]) // 2
    oy = (size - img.size[1]) // 2
    out.paste(img, (ox, oy), img)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frame", type=Path,
                   help="Source minimap frame (tick_NNNN.png)")
    ap.add_argument("--source-heading", type=float, required=True,
                   help="Compass heading (0=N, CW degrees) of the "
                        "ship on the source frame.  Used once at "
                        "extraction time to rotate the sprite to "
                        "canonical bow-up orientation; NOT stored "
                        "downstream.  The matcher then just rotates "
                        "the template by -H to test heading H.")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--canvas-size", type=int, default=80,
                   help="Square canvas size for the template (lets "
                        "rotations hold every angle without crop)")
    args = ap.parse_args()

    rgb = np.asarray(Image.open(args.frame).convert("RGB"))
    print(f"loaded frame: {rgb.shape}")

    ship = extract_ship_rgba(rgb)
    print(f"extracted ship: {ship.size}")

    # Rotate to bow-north so the template's canonical orientation
    # matches the matcher's "compass=0" convention.  One BICUBIC
    # interpolation here, one NEAREST at match time — quality is
    # fine empirically.
    aligned = rotate_to_north(ship, args.source_heading)
    print(f"rotated +{args.source_heading}° CCW (bow → north): "
          f"{aligned.size}")

    template = square_pad(aligned, args.canvas_size)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    template.save(args.out)
    print(f"→ {args.out}  ({template.size})  canonical (bow-up)")


if __name__ == "__main__":
    main()
