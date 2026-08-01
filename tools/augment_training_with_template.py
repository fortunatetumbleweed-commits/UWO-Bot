"""Augment a training snapshot with synthetic template-derived sprites.

Rotates `data/reference/ship_template_raw.png` to N target headings
(default: every 5° = 72 sprites), converting each to the ship-only
80×80 RGBA format used by extract_ship_sprites.  Adds them to a new
manifest.jsonl that unions the source snapshot's real sprites with
the synthetic template rotations.

Purpose: cover starved / empty direction buckets with a clean
appearance-authoritative sample.  Class-balanced sampling then
guarantees the CNN sees each direction at least via the template
even when real captures are missing.

Usage
─────
  python -m tools.augment_training_with_template \\
      --source data/heading_training_snapshot_v5_2026-07-01 \\
      --out    data/heading_training_snapshot_v7plus_template_2026-07-02 \\
      --step-deg 5
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _prep_template(template_rgba: np.ndarray) -> np.ndarray:
    """Take a raw 100×100 RGBA template (bow-up) and prepare an 80×80
    ship-only RGBA suitable for the extract_ship_sprites format:
    green + close-yellow-not-stripped, centered.
    """
    from scipy.ndimage import binary_dilation
    src = template_rgba
    # Grow slightly to guarantee ship fills the 80×80 crop
    H, W = src.shape[:2]
    # Center-crop to 80x80
    y0 = (H - 80) // 2
    x0 = (W - 80) // 2
    crop = src[y0:y0+80, x0:x0+80]
    return crop


def _extract_ship_alpha(rgb: np.ndarray) -> np.ndarray:
    """Green-mask + 2 px dilation, strip yellow, per the runtime
    pipeline in CNNHeading.  Returns a boolean alpha mask."""
    from scipy.ndimage import binary_dilation
    R, G, B = (rgb[..., c].astype(np.int16) for c in range(3))
    green = (G > 140) & (G - R > 30) & (G - B > 30)
    yellow = (R > 180) & (G > 150) & (B < 130) & (np.abs(R - G) < 40)
    ship = binary_dilation(green, iterations=2)
    return ship & ~yellow


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path,
                    default=REPO/"data/heading_training_snapshot_v5_2026-07-01")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--template", type=Path,
                    default=REPO/"data/reference/ship_template_raw.png")
    ap.add_argument("--step-deg", type=float, default=5.0,
                    help="Angular step for synthetic rotations.  5° "
                         "gives 72 synthetic sprites.")
    args = ap.parse_args()

    src_root = args.source
    out_root = args.out
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "sprites").mkdir(exist_ok=True)

    # Copy existing sprites via hardlink (fast, saves disk)
    src_sprites = src_root / "sprites"
    real_manifest_path = src_sprites / "manifest.jsonl"
    real = [json.loads(l) for l in real_manifest_path.open()]
    for rec in real:
        for suffix in (".png", "_mask.png"):
            name = f"sprite_{rec['sprite_id']}{suffix}"
            src = src_sprites / name
            dst = out_root / "sprites" / name
            if not dst.exists() and src.exists():
                try:
                    dst.hardlink_to(src)
                except OSError:
                    shutil.copy2(src, dst)
    print(f"linked {len(real)} real sprites")

    # Symlink backgrounds + labels
    for sub in ("backgrounds", "labels"):
        s = src_root / sub
        d = out_root / sub
        if s.exists() and not d.exists():
            d.symlink_to(s.resolve())

    # Load template + prepare canonical 80×80 bow-up sprite
    tpl_rgba = np.asarray(Image.open(args.template).convert("RGBA"))
    tpl_80 = _prep_template(tpl_rgba)

    # For each synthetic angle, rotate the template and save as a new
    # sprite whose "source heading" claim matches the rotation.
    # Naming: sprite_S00000.png ... to distinguish from real ones.
    synthetic = []
    next_id = max(int(r['sprite_id']) for r in real) + 1
    n_synth = int(round(360.0 / args.step_deg))
    for i in range(n_synth):
        angle = i * args.step_deg
        # Rotate the template.  Positive rotate = counter-clockwise in
        # PIL, so pass -angle to make the ship's bow point at angle°.
        rotated = Image.fromarray(tpl_80, "RGBA").rotate(
            -angle, resample=Image.BILINEAR, expand=False,
            fillcolor=(0, 0, 0, 0))
        rot_arr = np.asarray(rotated)
        rgb = rot_arr[..., :3]
        ship_alpha = _extract_ship_alpha(rgb)
        # Save RGBA where alpha is the ship-only mask
        rgba = np.zeros((80, 80, 4), dtype=np.uint8)
        rgba[..., :3] = rgb
        rgba[..., 3] = ship_alpha.astype(np.uint8) * 255
        sid = f"S{next_id:05d}"
        Image.fromarray(rgba, "RGBA").save(
            out_root / "sprites" / f"sprite_{sid}.png")
        Image.fromarray(
            (ship_alpha.astype(np.uint8) * 255)
        ).save(out_root / "sprites" / f"sprite_{sid}_mask.png")
        ys, xs = np.where(ship_alpha)
        centroid_y = float(ys.mean()) if len(ys) else 40.0
        centroid_x = float(xs.mean()) if len(xs) else 40.0
        synthetic.append({
            "sprite_id": sid,
            "source_session": "SYNTHETIC:reference_template",
            "source_tick": i,
            "heading_deg": round(angle, 2),
            "n_green": int(ship_alpha.sum()),
            "largest_component_frac": 1.0,
            "centroid_yx": [round(centroid_y, 1),
                            round(centroid_x, 1)],
            "_synthetic": True,
        })
        next_id += 1

    # Write combined manifest — real + synthetic
    combined_path = out_root / "sprites" / "manifest.jsonl"
    with combined_path.open("w") as f:
        for rec in real:
            f.write(json.dumps(rec) + "\n")
        for rec in synthetic:
            f.write(json.dumps(rec) + "\n")
    print(f"wrote {len(real) + len(synthetic)} entries "
          f"({len(real)} real + {len(synthetic)} synthetic) to "
          f"{combined_path}")

    # Show new bucket distribution
    manifest = real + synthetic
    n_buckets = int(round(360.0 / args.step_deg))
    buckets = [0] * n_buckets
    for rec in manifest:
        buckets[int(rec['heading_deg'] // args.step_deg) % n_buckets] += 1
    print(f"\ndistribution across {n_buckets} × {args.step_deg}° buckets after augmentation:")
    empty = sum(1 for c in buckets if c == 0)
    print(f"  empty buckets: {empty}")
    print(f"  min: {min(buckets)}   max: {max(buckets)}")


if __name__ == "__main__":
    main()
