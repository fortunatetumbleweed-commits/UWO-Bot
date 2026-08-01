"""Run the trained heading CNN on real session ticks.

For each tick in a session:
  1. Load the saved minimap frame (400×190).
  2. Extract an 80×80 crop centered on the ship — same sprite-finder
     as `tools.extract_ship_sprites` but with relaxed filters (we
     WANT occluded / split-component ships; that's the whole point
     of this eval).  Falls back to the minimap geometric center
     when the green-pixel centroid is unusable.
  3. Run the CNN, decode heading + confidence.
  4. Print prediction alongside the live pipeline's heading and the
     two raw detector outputs from the mining pass.

Usage
─────
  python -m tools.eval_heading_cnn_on_ticks \\
      --session ai_nav_2026-06-30T07-59-21 \\
      --ticks 128,129,130,131,132,137,138,139,140,141
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


CROP_SIZE = 80
HALF_CROP = CROP_SIZE // 2


def _green_mask(rgb: np.ndarray) -> np.ndarray:
    R, G, B = (rgb[..., c].astype(np.int16) for c in range(3))
    return (G > 140) & (G - R > 30) & (G - B > 30)


def _yellow_mask(rgb: np.ndarray) -> np.ndarray:
    R, G, B = (rgb[..., c].astype(np.int16) for c in range(3))
    return (R > 180) & (G > 150) & (B < 130) & (np.abs(R - G) < 40)


def _ship_only_mask(rgb: np.ndarray, green: np.ndarray) -> np.ndarray:
    """Match training-side ship_only mask exactly: dilate green 2px,
    then strip yellow."""
    from scipy.ndimage import binary_dilation
    yellow = _yellow_mask(rgb)
    ship_region = binary_dilation(green, iterations=2)
    return ship_region & ~yellow


def _mask_to_ship_composite(crop: np.ndarray,
                             background: np.ndarray) -> np.ndarray:
    """Inference-side preprocessing: mask the crop to ship-only,
    then composite over a reference background.  Matches the
    training-time synthesis distribution exactly.
    """
    green = _green_mask(crop)
    ship = _ship_only_mask(crop, green)
    out = background.copy()
    out[ship] = crop[ship]
    return out


def _find_centroid(rgb: np.ndarray) -> tuple[float, float, str]:
    """Best-effort ship-icon location.

    Returns (cy, cx, source) where source ∈
      {'largest_cc', 'all_green_centroid', 'minimap_center'}.
    """
    H, W, _ = rgb.shape
    mask = _green_mask(rgb)
    n_green = int(mask.sum())
    if n_green >= 10:
        lab, n = cc_label(mask)
        if n >= 1:
            sizes = np.bincount(lab.ravel())[1:]
            biggest = int(np.argmax(sizes)) + 1
            big = (lab == biggest)
            ys, xs = np.where(big)
            return float(ys.mean()), float(xs.mean()), "largest_cc"
        ys, xs = np.where(mask)
        return float(ys.mean()), float(xs.mean()), "all_green_centroid"
    return H / 2.0, W / 2.0, "minimap_center"


def _crop_around(rgb: np.ndarray, cy: float, cx: float) -> np.ndarray:
    H, W, _ = rgb.shape
    cy_i = int(round(max(HALF_CROP, min(H - HALF_CROP, cy))))
    cx_i = int(round(max(HALF_CROP, min(W - HALF_CROP, cx))))
    return rgb[cy_i - HALF_CROP:cy_i + HALF_CROP,
               cx_i - HALF_CROP:cx_i + HALF_CROP]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--ticks", required=True,
                    help="comma-separated tick numbers")
    ap.add_argument("--ckpt", default="data/heading_cnn/best.pt")
    ap.add_argument("--reference-bg",
                    default="data/heading_backgrounds/bg_000000.png",
                    help="background to composite ship over before CNN")
    args = ap.parse_args()
    reference_bg = (np.asarray(Image.open(args.reference_bg).convert("RGB"))
                    if Path(args.reference_bg).exists() else None)

    ticks = [int(x) for x in args.ticks.split(",")]
    sess_dir = REPO / "data" / "sessions" / args.session

    device = torch.device("cpu")
    model = HeadingCNN().to(device).eval()
    ckpt = torch.load(args.ckpt, map_location=device, weights_only=False)
    model.load_state_dict(ckpt["model"])

    # Pull live + labelers per tick for comparison.
    trace = {}
    tp = sess_dir / "trace.jsonl"
    if tp.exists():
        with tp.open() as f:
            for line in f:
                try: r = json.loads(line)
                except: continue
                if r.get("tick") in set(ticks):
                    trace[r["tick"]] = r
    labels = {}
    lp = REPO / "data" / "heading_labels" / args.session / "labels.jsonl"
    if lp.exists():
        for line in lp.open():
            r = json.loads(line)
            if r.get("tick") in set(ticks):
                labels[r["tick"]] = r

    print(f"{'tick':>4} {'cnn_hdg':>8} {'conf':>5} {'live':>5} {'tpl':>5} "
          f"{'pca':>5} {'mb':>5} {'cls':>15} {'crop_src':>22}")
    print("-" * 90)

    for t in ticks:
        fp = sess_dir / f"tick_{t:04d}.png"
        if not fp.exists():
            print(f"{t:>4}  (frame missing)")
            continue
        rgb = np.asarray(Image.open(fp).convert("RGB"))
        cy, cx, src = _find_centroid(rgb)
        crop = _crop_around(rgb, cy, cx)
        # Inference preprocessing: ship-only mask + composite over
        # reference background, matching training distribution.
        if reference_bg is not None:
            crop = _mask_to_ship_composite(crop, reference_bg)
        arr = crop.astype(np.float32) / 255.0
        tensor = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)
        with torch.no_grad():
            sin_cos, conf = model(tensor)
            pred_deg = decode_heading(sin_cos).item()
        conf_v = conf.item()

        tr = trace.get(t, {})
        lb = labels.get(t, {})
        live = tr.get("heading_deg")
        live_s = f"{live:.0f}" if isinstance(live, (int, float)) else "?"
        tpl = lb.get("hdg_template")
        pca = lb.get("hdg_pca")
        mb = lb.get("motion_bearing")
        cls = lb.get("agreement_class", "?")
        tpl_s = f"{tpl:.0f}" if tpl is not None else "?"
        pca_s = f"{pca:.0f}" if pca is not None else "?"
        mb_s = f"{mb:.0f}" if mb is not None else "?"

        print(f"{t:>4} {pred_deg:>7.1f}° {conf_v:>5.2f} "
              f"{live_s:>5} {tpl_s:>5} {pca_s:>5} {mb_s:>5} "
              f"{cls:>15} {src:>22}")


if __name__ == "__main__":
    main()
