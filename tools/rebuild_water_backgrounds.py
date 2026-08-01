"""Rebuild the background pool with strict water-only filtering.

Filters existing backgrounds hard:
  - ≥90% blue-dominant pixels (was 60%)
  - Low per-channel variance (rules out big color streaks)
  - No strong high-luminance spots (rules out light-glare artifacts)
  - No dark spots below a floor (rules out shore silhouettes)

Then procedurally generates additional clean water backgrounds by
sampling a base water tone from the accepted pool and adding subtle
Perlin-like noise for texture.  Outputs to a new directory.
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "data/heading_training_snapshot_v5_2026-07-01/backgrounds"
DST_DEFAULT = REPO / "data/heading_backgrounds_clean_2026-07-02"


def is_strict_water(bg: np.ndarray) -> bool:
    """Strict water gate: predominantly blue, spatially uniform, no
    streaks or bright/dark spots."""
    R = bg[..., 0].astype(np.int16)
    G = bg[..., 1].astype(np.int16)
    B = bg[..., 2].astype(np.int16)
    water = (B > R + 5) & (B > G + 5)
    if water.mean() < 0.95:
        return False
    # Per-channel std over water pixels — reject if high variance
    # (implies light/dark streaks).  Tight threshold (20) since real
    # game water is subtly textured, not banded.
    if bg[water].std(axis=0).max() > 20:
        return False
    gray = bg.mean(axis=-1)
    # Reject bright glare or streaks (anything meaningfully lighter
    # than the water average).
    if (gray > gray.mean() + 40).mean() > 0.01:
        return False
    # Reject dark shore silhouettes
    if (gray < 30).mean() > 0.01:
        return False
    # Row/column variance — a horizontal streak has one very-bright row.
    if gray.mean(axis=1).std() > 12 or gray.mean(axis=0).std() > 12:
        return False
    return True


def _fbm_noise(shape: tuple[int, int], octaves: int = 4) -> np.ndarray:
    """Cheap fractal-Brownian-motion noise — smoother than white noise,
    good approximation of subtle water ripple texture."""
    H, W = shape
    out = np.zeros((H, W), dtype=np.float32)
    amp = 1.0
    for o in range(octaves):
        step = max(1, 2 ** (octaves - o - 1))
        lo = np.random.rand(H // step + 1, W // step + 1).astype(np.float32)
        # Upscale (nearest-neighbour is fine; we're aiming for texture)
        img = Image.fromarray((lo * 255).astype(np.uint8))
        img = img.resize((W, H), Image.BILINEAR)
        out += np.asarray(img).astype(np.float32) / 255.0 * amp
        amp *= 0.5
    out /= out.max()
    return out


def synthesize_water_bg(base_color: np.ndarray) -> np.ndarray:
    """Procedural water background with subtle ripple texture."""
    H, W = 80, 80
    noise = _fbm_noise((H, W), octaves=4)
    # Modulate base colour: darker/lighter per pixel by 15% envelope
    modulation = 0.85 + 0.30 * noise    # in [0.85, 1.15]
    out = base_color[None, None, :].astype(np.float32) * modulation[..., None]
    return np.clip(out, 0, 255).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=SRC)
    ap.add_argument("--dst", type=Path, default=DST_DEFAULT)
    ap.add_argument("--min-real", type=int, default=200,
                    help="How many real backgrounds to accept before "
                         "topping up with procedural.")
    ap.add_argument("--total", type=int, default=720,
                    help="Total backgrounds to produce.")
    args = ap.parse_args()

    args.dst.mkdir(parents=True, exist_ok=True)
    print(f"scanning {args.src}...")
    accepted = []
    for p in sorted(args.src.glob("bg_*.png")):
        bg = np.asarray(Image.open(p).convert("RGB"))
        if is_strict_water(bg):
            accepted.append(bg)
    print(f"strict-water pass: {len(accepted)} / (all in src)")

    if not accepted:
        raise SystemExit("no strict-water backgrounds — start with the "
                         "canonical mini-map water colour")

    # Write accepted real backgrounds
    n_real = min(len(accepted), args.total)
    for i, bg in enumerate(accepted[:n_real]):
        Image.fromarray(bg).save(args.dst / f"bg_{i:04d}.png")
    print(f"wrote {n_real} real water backgrounds")

    # Top up with procedural backgrounds using a fixed palette of
    # CLEAN blue water tones (not sampled from real bgs, which still
    # contain some tan/gray shore colors that leaked past the filter).
    # These tones span typical game mini-map water: dark deep-blue,
    # medium water blue, slightly greenish shallow water, etc.
    n_proc = args.total - n_real
    if n_proc > 0:
        # Palette of canonical mini-map water tones (RGB).
        water_palette = np.array([
            [ 42,  75, 120],   # deep blue
            [ 55,  90, 130],   # medium blue
            [ 60, 100, 135],   # slightly lighter blue
            [ 48,  95, 115],   # slightly greenish blue
            [ 65, 105, 145],   # brighter blue (sunlit water)
            [ 40,  70, 105],   # darker deep water
        ], dtype=np.float32)
        rng = np.random.default_rng(0)
        for i in range(n_proc):
            base = water_palette[rng.integers(0, len(water_palette))]
            jitter = rng.normal(0, 4, size=3)
            base_jittered = np.clip(base + jitter, 20, 200)
            bg = synthesize_water_bg(base_jittered)
            Image.fromarray(bg).save(args.dst / f"bg_{n_real + i:04d}.png")
        print(f"generated {n_proc} procedural water backgrounds "
              f"(canonical palette, not sampled from real bgs)")

    print(f"total in {args.dst}: {args.total}")


if __name__ == "__main__":
    main()
