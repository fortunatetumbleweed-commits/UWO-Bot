"""Extract clean ship sprites from gold-labeled ticks.

Reads the per-session `labels.jsonl` files written by
`tools/mine_heading_labels.py` and pulls the `gold` ticks (both
detectors agree within 5°).  For each, isolates the ship's
green-pixel sprite, crops a fixed window around the centroid, and
saves:

  - data/heading_sprites/sprite_<id>.png        — 80×80 RGB crop
  - data/heading_sprites/sprite_<id>_mask.png   — 80×80 binary mask
  - data/heading_sprites/manifest.jsonl         — per-sprite metadata

Quality filters applied:
  - ≥ 30 green pixels (visible ship)
  - largest connected component covers ≥ 70% of green pixels
    (compact sprite, not split by partial occlusion)
  - centroid ≥ HALF_CROP px from minimap edge (full crop fits)

These sprites are the rotation source for the synthesis pipeline:
take a sprite, rotate to an arbitrary angle, composite over a
sampled background, optionally apply occlusion → labeled training
example.

Usage
─────
  python -m tools.extract_ship_sprites
  python -m tools.extract_ship_sprites --max-per-session 200
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


CROP_SIZE = 80
HALF_CROP = CROP_SIZE // 2

MIN_GREEN_PIXELS = 30
MIN_LARGEST_COMPONENT_FRAC = 0.70

# When the ship isn't moving, every gold tick is essentially the same
# frame with the same label — mining 60 identical sprites biases the
# CNN toward that specific pose.  Keep at most this many sprites per
# run of consecutive stationary gold ticks.  A "stationary" tick is
# one where the miner's motion bearing was rejected as no_motion or
# low_speed (defined in tools/mine_heading_labels.py).
MAX_PER_STATIONARY_RUN = 2
_STATIONARY_REASONS = {"no_motion", "low_speed"}


def _green_mask(rgb: np.ndarray) -> np.ndarray:
    R, G, B = (rgb[..., c].astype(np.int16) for c in range(3))
    return (G > 140) & (G - R > 30) & (G - B > 30)


def _yellow_mask(rgb: np.ndarray) -> np.ndarray:
    """Detect the saturated yellow that the sonar fan + NPC sprites use.
    Important: the model must NOT learn yellow as a ship feature, so
    these pixels are stripped from training sprites.
    """
    R, G, B = (rgb[..., c].astype(np.int16) for c in range(3))
    return (R > 180) & (G > 150) & (B < 130) & (np.abs(R - G) < 40)


def _ship_only_mask(rgb: np.ndarray, green: np.ndarray,
                    keep_yellow: bool = False) -> np.ndarray:
    """Ship hull mask only — green pixels expanded by 2px to capture
    the dark sprite outline, then yellow explicitly stripped (unless
    keep_yellow=True, which is the v2-redux ablation mode that
    leaves the sonar fan in as a bow-direction feature).

    This is the alpha channel for the RGBA sprite.
    """
    from scipy.ndimage import binary_dilation
    ship_region = binary_dilation(green, iterations=2)
    if keep_yellow:
        # Include yellow blobs near the ship — the sonar fan extends
        # forward from the bow but doesn't overlap the green hull,
        # so a 15 px dilation is needed to reach it.  This is what
        # v2 saw during training.
        yellow = _yellow_mask(rgb)
        near_ship = binary_dilation(green, iterations=15)
        return ship_region | (yellow & near_ship)
    yellow = _yellow_mask(rgb)
    return ship_region & ~yellow


def _largest_component_frac(mask: np.ndarray) -> tuple[float, np.ndarray]:
    """Return (largest_component_fraction, largest_component_mask).

    Two-pass flood-fill via scipy if available, else simple BFS.
    """
    from scipy.ndimage import label as cc_label
    lab, n = cc_label(mask)
    if n == 0:
        return 0.0, mask
    sizes = np.bincount(lab.ravel())[1:]
    biggest = int(np.argmax(sizes)) + 1
    big_mask = (lab == biggest)
    return float(big_mask.sum()) / float(mask.sum()), big_mask


def extract_sprite(frame_path: Path, keep_yellow: bool = False
                   ) -> Optional[dict]:
    """Returns dict with crop, mask, centroid or None if filters reject."""
    img = Image.open(frame_path).convert("RGB")
    rgb = np.asarray(img)
    H, W, _ = rgb.shape

    mask = _green_mask(rgb)
    n_green = int(mask.sum())
    if n_green < MIN_GREEN_PIXELS:
        return None

    largest_frac, big_mask = _largest_component_frac(mask)
    if largest_frac < MIN_LARGEST_COMPONENT_FRAC:
        return None

    # Centroid of the largest component (ignore noise pixels).
    ys, xs = np.where(big_mask)
    cy, cx = float(ys.mean()), float(xs.mean())

    # Ensure full crop fits inside the minimap.
    if (cy < HALF_CROP or cy > H - HALF_CROP
            or cx < HALF_CROP or cx > W - HALF_CROP):
        return None

    y0, y1 = int(round(cy)) - HALF_CROP, int(round(cy)) + HALF_CROP
    x0, x1 = int(round(cx)) - HALF_CROP, int(round(cx)) + HALF_CROP
    crop_rgb = rgb[y0:y1, x0:x1]
    crop_mask = big_mask[y0:y1, x0:x1]

    # Ship-only alpha mask: green hull + small outline halo, with
    # yellow sonar/NPC pixels stripped.
    ship_only_full = _ship_only_mask(rgb, big_mask, keep_yellow=keep_yellow)
    ship_only_crop = ship_only_full[y0:y1, x0:x1]

    return {
        "crop_rgb": crop_rgb,
        "crop_mask": crop_mask,
        "ship_alpha": ship_only_crop,
        "centroid_y": cy,
        "centroid_x": cx,
        "n_green": n_green,
        "largest_component_frac": largest_frac,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels-root", default="data/heading_labels")
    ap.add_argument("--out-root", default="data/heading_sprites")
    ap.add_argument("--max-per-session", type=int, default=None,
                    help="Cap sprites mined per session (for balance)")
    ap.add_argument("--keep-yellow", action="store_true",
                    help="v2-redux ablation: keep the yellow sonar fan "
                         "in the sprite (default strips it to teach the "
                         "model not to use yellow as a bow signal)")
    ap.add_argument("--include-silver", action="store_true",
                    help="v4 expansion: include `silver` ticks (motion-"
                         "bearing-labelled, where the two detectors "
                         "disagreed but motion bearing decisively picked "
                         "one) in addition to gold.  Adds dead-end / "
                         "narrow-channel coverage that gold-only misses.")
    args = ap.parse_args()

    labels_root = Path(args.labels_root)
    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    manifest_path = out_root / "manifest.jsonl"
    counts = {
        "considered": 0, "rejected_low_green": 0,
        "rejected_split_component": 0, "rejected_edge_crop": 0,
        "rejected_stationary_dupe": 0,
        "saved": 0,
    }

    next_id = 0
    with manifest_path.open("w") as man_f:
        for sess_dir in sorted(labels_root.iterdir()):
            labels_path = sess_dir / "labels.jsonl"
            if not labels_path.exists():
                continue
            saved_this_session = 0
            stationary_run = 0    # count of gold-stationary ticks
                                  # taken from the current run
            with labels_path.open() as in_f:
                for line in in_f:
                    rec = json.loads(line)
                    accepted = {"gold"}
                    if args.include_silver:
                        accepted.add("silver")
                    if rec.get("agreement_class") not in accepted:
                        # non-gold tick breaks the stationary run
                        stationary_run = 0
                        continue
                    # Silver ticks must have a usable label
                    if rec.get("label_hdg") is None:
                        stationary_run = 0
                        continue
                    if (args.max_per_session is not None
                            and saved_this_session >= args.max_per_session):
                        break
                    # Stationary-run dedup: reject after N sprites from
                    # the same consecutive run of near-identical
                    # stationary poses.
                    is_stationary = (rec.get("mb_reject_reason")
                                     in _STATIONARY_REASONS)
                    if is_stationary:
                        if stationary_run >= MAX_PER_STATIONARY_RUN:
                            counts["rejected_stationary_dupe"] += 1
                            continue
                        stationary_run += 1
                    else:
                        stationary_run = 0
                    counts["considered"] += 1
                    frame_path = Path(rec["frame_path"])
                    if not frame_path.exists():
                        continue
                    spr = extract_sprite(frame_path,
                                         keep_yellow=args.keep_yellow)
                    if spr is None:
                        # Reason already filtered inside extract_sprite;
                        # bucket loosely for diagnostics.
                        img = Image.open(frame_path).convert("RGB")
                        mask = _green_mask(np.asarray(img))
                        if int(mask.sum()) < MIN_GREEN_PIXELS:
                            counts["rejected_low_green"] += 1
                        else:
                            frac, _ = _largest_component_frac(mask)
                            if frac < MIN_LARGEST_COMPONENT_FRAC:
                                counts["rejected_split_component"] += 1
                            else:
                                counts["rejected_edge_crop"] += 1
                        continue

                    sid = f"{next_id:06d}"
                    next_id += 1
                    # Save RGBA: ship hull pixels opaque, everything
                    # else (yellow sonar, water, shore, NPCs) transparent.
                    H, W = spr["crop_rgb"].shape[:2]
                    rgba = np.zeros((H, W, 4), dtype=np.uint8)
                    rgba[..., :3] = spr["crop_rgb"]
                    rgba[..., 3] = spr["ship_alpha"].astype(np.uint8) * 255
                    Image.fromarray(rgba, "RGBA").save(
                        out_root / f"sprite_{sid}.png")
                    Image.fromarray(
                        (spr["crop_mask"].astype(np.uint8) * 255)
                    ).save(out_root / f"sprite_{sid}_mask.png")
                    man_f.write(json.dumps({
                        "sprite_id": sid,
                        "source_session": sess_dir.name,
                        "source_tick": rec["tick"],
                        "heading_deg": rec["label_hdg"],
                        "n_green": spr["n_green"],
                        "largest_component_frac": round(
                            spr["largest_component_frac"], 3),
                        "centroid_yx": [round(spr["centroid_y"], 1),
                                        round(spr["centroid_x"], 1)],
                    }) + "\n")
                    counts["saved"] += 1
                    saved_this_session += 1

    print(f"\n=== EXTRACTION ===")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print(f"\nsprites at {out_root}/")
    print(f"manifest at {manifest_path}")


if __name__ == "__main__":
    main()
