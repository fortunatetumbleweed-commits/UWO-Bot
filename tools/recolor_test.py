"""Test the yellow-pirate-confusion hypothesis by recoloring the
pirate's yellow pixels to red and re-running the CNN.

If the hypothesis is correct (the CNN confuses yellow pirate face
with the player's yellow sonar fan), a red pirate should produce a
visibly different — and hopefully more correct — prediction.

Usage
─────
  python -m tools.recolor_test \\
      --session ai_nav_2026-06-30T07-59-21 --ticks 140,141
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from scipy.ndimage import label as cc_label

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from brain.ai_nav.learned.heading_cnn import HeadingCNN, decode_heading


def _yellow_mask(rgb: np.ndarray) -> np.ndarray:
    """Detect pirate-sprite + sonar yellow pixels.

    The game's yellow is a saturated R≈G ≫ B colour.  This catches
    both the pirate face and the sonar fan — that's actually the
    point: BOTH the sonar fan (we want to keep its info) and the
    pirate face (we want to recolor) are yellow, so we need a
    spatial criterion afterwards.
    """
    R, G, B = (rgb[..., c].astype(np.int16) for c in range(3))
    return (R > 180) & (G > 150) & (B < 130) & (np.abs(R - G) < 40)


def _isolate_pirate(yellow_mask: np.ndarray,
                    rgb: np.ndarray) -> np.ndarray:
    """The pirate sprite has a yellow hat, skin face, mustache, and
    dark outline.  Yellow-only detection misses the face & beard.

    Strategy: take the largest yellow blob (= hat) as the seed, then
    DILATE 15 px to capture the surrounding head, then within that
    dilation include all bright/saturated pixels (skin, beard,
    outline).  Explicitly exclude the player ship's green pixels.
    """
    from scipy.ndimage import binary_dilation
    lab, n = cc_label(yellow_mask)
    if n == 0:
        return np.zeros_like(yellow_mask)
    sizes = np.bincount(lab.ravel())[1:]
    if sizes.max() < 100:
        return np.zeros_like(yellow_mask)
    biggest = int(np.argmax(sizes)) + 1
    seed = (lab == biggest)
    head_region = binary_dilation(seed, iterations=15)

    # Within the dilated head region, accept any non-water-blue pixel
    # (face skin, brown beard, dark outline).  Reject the player's
    # green ship pixels explicitly.
    R, G, B = (rgb[..., c].astype(np.int16) for c in range(3))
    green = (G > 140) & (G - R > 30) & (G - B > 30)
    bright = (R.astype(int) + G + B) > 240  # not deep-water dark
    return head_region & ~green & bright


def _recolor(rgb: np.ndarray, mask: np.ndarray,
             new_rgb: tuple[int, int, int]) -> np.ndarray:
    """Replace masked pixels with new_rgb, preserving luminance
    variation so the recolored region still has structure (eyes,
    mouth, shadows) — not a flat block.
    """
    out = rgb.copy()
    if not mask.any():
        return out
    # Use channel ratios to preserve luminance
    base = np.array(new_rgb, dtype=np.float32) / 255.0
    masked_pixels = out[mask].astype(np.float32) / 255.0
    luminance = masked_pixels.mean(axis=1, keepdims=True)
    recolored = base[None, :] * luminance * 2.0  # ×2 to keep brightness
    out[mask] = np.clip(recolored * 255.0, 0, 255).astype(np.uint8)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--ticks", required=True,
                    help="comma-separated tick numbers")
    ap.add_argument("--ckpt", default="data/heading_cnn/best.pt")
    args = ap.parse_args()

    ticks = [int(x) for x in args.ticks.split(",")]
    sess_dir = REPO / "data" / "sessions" / args.session

    device = torch.device("cpu")
    model = HeadingCNN().to(device).eval()
    ckpt = torch.load(args.ckpt, map_location=device, weights_only=False)
    model.load_state_dict(ckpt["model"])

    out_dir = REPO / "data" / "heading_samples_review" / "recolor_test"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"{'tick':>4} {'orig_hdg':>9} {'orig_conf':>9} "
          f"{'red_hdg':>8} {'red_conf':>8}  {'Δhdg':>6}")
    print("-" * 60)

    for t in ticks:
        fp = sess_dir / f"tick_{t:04d}.png"
        if not fp.exists():
            continue
        rgb_full = np.asarray(Image.open(fp).convert("RGB"))
        # Same crop logic as the eval tool
        R, G, B = (rgb_full[..., c].astype(np.int16) for c in range(3))
        green = (G > 140) & (G - R > 30) & (G - B > 30)
        H, W, _ = rgb_full.shape
        if int(green.sum()) >= 10:
            lab, _ = cc_label(green)
            sizes = np.bincount(lab.ravel())[1:]
            biggest = int(np.argmax(sizes)) + 1
            ys, xs = np.where(lab == biggest)
            cy, cx = float(ys.mean()), float(xs.mean())
        else:
            cy, cx = H / 2.0, W / 2.0
        half = 40
        cy = int(round(max(half, min(H - half, cy))))
        cx = int(round(max(half, min(W - half, cx))))

        orig_crop = rgb_full[cy - half:cy + half, cx - half:cx + half]

        # Detect yellow in the FULL frame, then crop the mask so we
        # don't accidentally catch sonar inside the ship crop.
        yellow_full = _yellow_mask(rgb_full)
        pirate_full = _isolate_pirate(yellow_full, rgb_full)
        pirate_crop = pirate_full[cy - half:cy + half, cx - half:cx + half]

        red_crop = _recolor(orig_crop, pirate_crop, (220, 60, 60))

        # Save side-by-side comparison
        side = np.concatenate([orig_crop, red_crop], axis=1)
        Image.fromarray(side).save(out_dir / f"t{t:04d}_orig_vs_red.png")
        Image.fromarray(red_crop).save(out_dir / f"t{t:04d}_red.png")
        Image.fromarray(pirate_crop.astype(np.uint8) * 255).save(
            out_dir / f"t{t:04d}_pirate_mask.png")

        # Run CNN on both
        def predict(crop):
            arr = crop.astype(np.float32) / 255.0
            ten = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)
            with torch.no_grad():
                sc, cf = model(ten)
            return float(decode_heading(sc).item()), float(cf.item())

        orig_hdg, orig_conf = predict(orig_crop)
        red_hdg, red_conf = predict(red_crop)
        delta = abs(((orig_hdg - red_hdg + 540) % 360) - 180)
        n_pirate = int(pirate_crop.sum())

        print(f"{t:>4} {orig_hdg:>8.1f}° {orig_conf:>9.2f} "
              f"{red_hdg:>7.1f}° {red_conf:>8.2f}  {delta:>5.1f}°  "
              f"(pirate_px={n_pirate})")


if __name__ == "__main__":
    main()
