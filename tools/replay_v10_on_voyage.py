"""Replay V10 channel-extraction perception over an entire voyage.

Walks every frame in a session directory, runs the V10 channel-mask
pipeline on each, and reports per-tick stats + a stratified set of
visual composites so we can see V10's behaviour across an actual
recorded voyage (not just the 9-frame iteration set).

For each tick:
  - run `v10_channel_mask` on the 381×184 minimap crop,
  - record: channel_px, n_islands, time_ms, V10 success/fail flag,
  - also re-run the production `read_navigation_view` so we can
    side-by-side production water_mask vs V10 channel mask,
  - log to a JSONL stats file under the session directory.

A composite PNG is saved every COMPOSITE_STRIDE ticks (default 50)
showing 4 columns: original | production mask | V10 channel | ship-
overlay (production magenta + V10 yellow + ship dot from green sprite).

The replay does NOT simulate navigation — it's a pure perception
sweep.  Use the JSONL + composites to spot regions where V10 fails
(returns 0 channel pixels) or where V10 vs production banks disagree.

Usage
─────
  python -m tools.replay_v10_on_voyage data/sessions/reference_nile_full_20260606_141824
  python -m tools.replay_v10_on_voyage <session_dir> --outbound-only 800
  python -m tools.replay_v10_on_voyage <session_dir> --stride 100  # fewer composites
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

import matplotlib.pyplot as plt
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.perception_v10_channel_prototype import (  # noqa: E402
    v10_channel_mask, v11_brightness_channel_mask, bank_pixels,
)
from vision.minimap_navigation_view import (  # noqa: E402
    read_navigation_view, MINIMAP_CROP,
)


COMPOSITE_STRIDE = 50


def _production_water_mask(img_rgb_np):
    """Run the production minimap perception on a 381×184 crop by
    wrapping it into a 2400×1080 canvas."""
    h, w = img_rgb_np.shape[:2]
    if (w, h) != (381, 184):
        return None
    canvas = np.zeros((1080, 2400, 3), dtype=np.uint8)
    l, t, r, b = MINIMAP_CROP
    canvas[t:b, l:r] = img_rgb_np
    view = read_navigation_view(Image.fromarray(canvas))
    return view.water_mask


def _prod_bank_pixels(water_mask):
    if water_mask is None:
        return None
    bank = np.zeros_like(water_mask)
    bank[1:-1, 1:-1] = water_mask[1:-1, 1:-1] & (
        ~water_mask[:-2, 1:-1] | ~water_mask[2:, 1:-1]
        | ~water_mask[1:-1, :-2] | ~water_mask[1:-1, 2:]
    )
    ys, xs = np.where(bank)
    return xs, ys


def _ship_centroid(img):
    """Approximate ship dot — largest green CC, fall back to image
    centre."""
    from scipy.ndimage import label as cc_label
    R, G, B = (img[..., c].astype(np.int16) for c in range(3))
    green = (G > 140) & (G - R > 30) & (G - B > 30)
    if not green.any():
        return img.shape[1] // 2, img.shape[0] // 2
    lab, _ = cc_label(green)
    sizes = np.bincount(lab.ravel()); sizes[0] = 0
    largest = int(np.argmax(sizes))
    ys, xs = np.where(lab == largest)
    return int(xs.mean()), int(ys.mean())


def _save_composite(tick, img, prod_wm, chan, out_path):
    fig, axes = plt.subplots(1, 4, figsize=(18, 4.5))
    cx, cy = _ship_centroid(img)
    axes[0].imshow(img); axes[0].axis("off")
    axes[0].set_title(f"t{tick}  ORIGINAL", fontsize=10)
    if prod_wm is not None:
        axes[1].imshow(prod_wm.astype(np.uint8) * 255,
                       cmap="gray", vmin=0, vmax=255)
        axes[1].set_title(f"production water_mask  "
                          f"{int(prod_wm.sum())} px", fontsize=10)
    else:
        axes[1].imshow(np.zeros_like(img[:, :, 0]),
                       cmap="gray", vmin=0, vmax=255)
        axes[1].set_title("production water_mask = None", fontsize=10)
    axes[1].axis("off")
    binary_v10 = chan.astype(np.uint8) * 255
    axes[2].imshow(binary_v10, cmap="gray", vmin=0, vmax=255)
    axes[2].axis("off")
    axes[2].set_title(f"V10 channel  {int(chan.sum())} px", fontsize=10)
    axes[3].imshow(img, alpha=0.75)
    if prod_wm is not None:
        pbk = _prod_bank_pixels(prod_wm)
        if pbk is not None:
            axes[3].scatter(pbk[0], pbk[1], color="#ff66cc", s=1.5,
                            alpha=0.8, label="production")
    v10_bk = bank_pixels(chan)
    if v10_bk[0].size > 0:
        axes[3].scatter(v10_bk[0], v10_bk[1], color="#ffdd44", s=1.5,
                        alpha=0.95, label="V10")
    axes[3].scatter([cx], [cy], color="cyan", s=60, edgecolors="black",
                    linewidths=0.8, zorder=5)
    axes[3].legend(loc="lower right", fontsize=8)
    axes[3].axis("off")
    axes[3].set_title("banks side-by-side", fontsize=10)
    fig.suptitle(f"V10 vs production — tick {tick}",
                 fontsize=12, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--outbound-only", type=int, default=None,
                    help="stop after this tick (e.g. 800 for Nile outbound)")
    ap.add_argument("--stride", type=int, default=COMPOSITE_STRIDE,
                    help=f"save a composite every N ticks (default {COMPOSITE_STRIDE})")
    ap.add_argument("--variant", choices=("v10", "v11"), default="v10",
                    help="which channel-mask pipeline (v10=colour K-means, "
                         "v11=brightness threshold + V10 CC extraction)")
    args = ap.parse_args()

    session = args.session_dir.resolve()
    if not session.is_dir():
        raise SystemExit(f"not a directory: {session}")
    frames = sorted(session.glob("tick_*.png"))
    if not frames:
        raise SystemExit(f"no tick_*.png in {session}")
    if args.outbound_only:
        frames = [p for p in frames
                  if int(p.stem.split("_")[-1]) <= args.outbound_only]

    out_dir = session / f"{args.variant}_replay"
    out_dir.mkdir(exist_ok=True)
    composites_dir = out_dir / "composites"
    composites_dir.mkdir(exist_ok=True)
    stats_path = out_dir / "stats.jsonl"

    print(f"[replay] session       = {session}")
    print(f"[replay] frames        = {len(frames)}")
    print(f"[replay] composites    = every {args.stride} ticks → "
          f"{composites_dir.relative_to(session)}/")
    print(f"[replay] stats         = {stats_path.relative_to(session)}")
    print()

    # Aggregate counters.
    n_total = len(frames)
    n_v10_zero = 0
    n_v10_smaller_than_prod = 0
    n_prod_none = 0
    v10_times: list[float] = []
    prod_times: list[float] = []

    with stats_path.open("w") as stats_f:
        for i, frame_path in enumerate(frames):
            tick = int(frame_path.stem.split("_")[-1])
            img = np.asarray(Image.open(frame_path).convert("RGB"))

            t0 = time.time()
            if args.variant == "v10":
                chan, n_islands, _ = v10_channel_mask(img)
            else:
                chan, n_islands = v11_brightness_channel_mask(img)
            t_v10 = (time.time() - t0) * 1000.0
            v10_times.append(t_v10)

            t0 = time.time()
            prod_wm = _production_water_mask(img)
            t_prod = (time.time() - t0) * 1000.0
            prod_times.append(t_prod)

            v10_px = int(chan.sum())
            prod_px = int(prod_wm.sum()) if prod_wm is not None else 0
            n_v10_zero += (v10_px == 0)
            n_prod_none += (prod_wm is None)
            n_v10_smaller_than_prod += (v10_px < prod_px - 500)

            stats_f.write(json.dumps({
                "tick": tick,
                "v10_channel_px": v10_px,
                "v10_islands_removed": int(n_islands),
                "v10_ms": round(t_v10, 1),
                "prod_water_px": prod_px,
                "prod_ms": round(t_prod, 1),
                "delta_v10_minus_prod_px": v10_px - prod_px,
            }) + "\n")

            if tick % args.stride == 0 or tick == frames[0].stem:
                _save_composite(tick, img, prod_wm, chan,
                                composites_dir / f"tick_{tick:04d}.png")

            if (i + 1) % 100 == 0:
                print(f"[replay] {i + 1:>5}/{n_total}  "
                      f"v10_zero={n_v10_zero}  v10<prod={n_v10_smaller_than_prod}")

    v10_arr = np.array(v10_times)
    prod_arr = np.array(prod_times)
    print()
    print(f"[summary] {n_total} ticks replayed")
    print(f"[summary] V10 zero-channel: {n_v10_zero} "
          f"({100 * n_v10_zero / n_total:.1f}%)")
    print(f"[summary] V10 < prod (by ≥500 px): {n_v10_smaller_than_prod}")
    print(f"[summary] production None: {n_prod_none}")
    print(f"[summary] V10 ms:  mean={v10_arr.mean():.1f}  "
          f"p50={np.median(v10_arr):.1f}  p95={np.percentile(v10_arr, 95):.1f}  "
          f"max={v10_arr.max():.1f}")
    print(f"[summary] prod ms: mean={prod_arr.mean():.1f}  "
          f"p50={np.median(prod_arr):.1f}  p95={np.percentile(prod_arr, 95):.1f}  "
          f"max={prod_arr.max():.1f}")
    print()
    print(f"[done] composites saved under: {composites_dir}")
    print(f"[done] stats jsonl:            {stats_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
