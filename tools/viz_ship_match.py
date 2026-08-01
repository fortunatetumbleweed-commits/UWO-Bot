"""Visualize per-tick ship template matching.

For each tick row:
  col 0: raw minimap, ship region cropped + zoomed
  col 1: ship region overlaid with template rotated to MATCHER's best
  col 2: ship region overlaid with template rotated to PCA's heading
  col 3: ship region overlaid with template rotated to TRUTH heading
         (from the clean prior tick)
  col 4: rotation-vs-IoU plot (which angles the matcher scored high on)

Truth is taken as the recorded heading of a known-clean nearby tick.
Each row's title shows the verdicts + IoU.

Usage
─────
  python -m tools.viz_ship_match \\
      data/reference/ship_template_raw.png \\
      data/sessions/ai_nav_live_palma \\
      --cases 35:34,85:84,89:88,91:90
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.match_ship_template import (
    green_mask, match_heading, template_green_mask, rotate_template_mask,
)


def crop_around_ship(rgb: np.ndarray, half: int = 35) -> tuple[np.ndarray, tuple[int, int]]:
    """Crop a square window at the minimap's geometric center.
    The ship is always at the center — no centroid search needed
    (centroid search on the whole frame gets polluted by stray green
    pixels from text overlays + sprite anti-aliasing edges)."""
    H, W = rgb.shape[:2]
    cy, cx = H // 2, W // 2
    y1 = max(0, cy - half); y2 = min(H, cy + half)
    x1 = max(0, cx - half); x2 = min(W, cx + half)
    return rgb[y1:y2, x1:x2], (cy, cx)


def overlay_rotated_template(crop_rgb: np.ndarray,
                              template_mask: np.ndarray,
                              compass_deg: float,
                              color: tuple[int, int, int]) -> np.ndarray:
    """Draw the template's rotated outline on top of the cropped frame."""
    H, W = crop_rgb.shape[:2]
    rot = rotate_template_mask(template_mask, compass_deg)
    th, tw = rot.shape
    cy, cx = H // 2, W // 2
    y1 = cy - th // 2; y2 = y1 + th
    x1 = cx - tw // 2; x2 = x1 + tw
    # Clip
    sy1 = max(0, -y1); sy2 = th - max(0, y2 - H)
    sx1 = max(0, -x1); sx2 = tw - max(0, x2 - W)
    y1 = max(0, y1); y2 = min(H, y2)
    x1 = max(0, x1); x2 = min(W, x2)

    out = crop_rgb.copy()
    sub_rot = rot[sy1:sy2, sx1:sx2]
    # Draw colored fill at 45% opacity
    overlay_color = np.array(color, dtype=np.float32)
    alpha = 0.55
    sub_out = out[y1:y2, x1:x2].astype(np.float32)
    sub_out[sub_rot] = sub_out[sub_rot] * (1 - alpha) + overlay_color * alpha
    out[y1:y2, x1:x2] = sub_out.clip(0, 255).astype(np.uint8)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("template", type=Path)
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--cases", required=True,
                   help="Comma-separated tick:clean_prior pairs, "
                        "e.g. 35:34,85:84,89:88,91:90")
    ap.add_argument("--step", type=float, default=2.0)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    template_img = Image.open(args.template).convert("RGBA")
    template_mask = template_green_mask(template_img)

    # trace
    trace_path = args.session_dir / "trace.jsonl"
    by_tick = {}
    if trace_path.exists():
        for line in trace_path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                by_tick[r["tick"]] = r

    cases = []
    for token in args.cases.split(","):
        t, prior = token.split(":")
        cases.append((int(t), int(prior)))

    n_rows = len(cases) + 1   # +1 for the template-display row
    fig = plt.figure(figsize=(18, 2.6 * n_rows))
    gs = gridspec.GridSpec(
        n_rows, 5, figure=fig,
        width_ratios=[1.5, 1.5, 1.5, 1.5, 2.0],
        hspace=0.4, wspace=0.18,
    )

    # ── Row 0: show the canonical template by itself ──
    ax_t = fig.add_subplot(gs[0, 0:4])
    ax_t.imshow(np.asarray(template_img))
    ax_t.set_xticks([]); ax_t.set_yticks([])
    ax_t.set_title(
        f"Canonical template (bow → north).  Matcher rotates by "
        f"compass heading directly at match time.  "
        f"Green pixels: {int(template_mask.sum())}",
        fontsize=10,
    )
    ax_legend = fig.add_subplot(gs[0, 4])
    ax_legend.axis("off")
    ax_legend.text(
        0.0, 0.95,
        "Legend (per case row):\n"
        " col 0:  raw ship region\n"
        " col 1:  MATCHER best (cyan)\n"
        " col 2:  PCA-reported (red)\n"
        " col 3:  TRUTH (green, from prior tick's clean heading)\n"
        " col 4:  IoU vs rotation\n\n"
        "Where MATCHER and PCA disagree by ~180°, PCA flipped;\n"
        "MATCHER caught the flip if cyan ≈ green.",
        fontsize=9, family="monospace", va="top",
    )

    # ── Case rows ──
    for i, (tick, prior_tick) in enumerate(cases, start=1):
        path = args.session_dir / f"tick_{tick:04d}.png"
        if not path.exists():
            continue
        rgb = np.asarray(Image.open(path).convert("RGB"))
        crop, _ = crop_around_ship(rgb, half=35)

        # Matcher
        matched, iou, scores = match_heading(rgb, template_mask, step=args.step)
        # PCA-reported (from trace)
        pca_hdg = by_tick.get(tick, {}).get("heading_deg")
        # Truth = recorded heading of the clean prior tick
        truth = by_tick.get(prior_tick, {}).get("heading_deg")

        def make_title():
            parts = [f"t{tick}"]
            parts.append(f"matcher={matched:.0f}° (IoU={iou:.2f})")
            if pca_hdg is not None:
                parts.append(f"pca={pca_hdg:.0f}°")
            if truth is not None:
                parts.append(f"truth≈{truth:.0f}° (t{prior_tick})")
            return "   ".join(parts)

        # col 0: raw
        ax0 = fig.add_subplot(gs[i, 0])
        ax0.imshow(crop)
        ax0.set_xticks([]); ax0.set_yticks([])
        ax0.set_title(make_title(), fontsize=9, loc="left")

        # col 1: matcher
        ax1 = fig.add_subplot(gs[i, 1])
        ax1.imshow(overlay_rotated_template(crop, template_mask, matched, (0, 200, 255)))
        ax1.set_xticks([]); ax1.set_yticks([])
        ax1.set_title(f"matcher → {matched:.0f}°", fontsize=9, color="#0088aa")

        # col 2: pca
        ax2 = fig.add_subplot(gs[i, 2])
        if pca_hdg is not None:
            ax2.imshow(overlay_rotated_template(crop, template_mask, pca_hdg, (255, 60, 60)))
            ax2.set_title(f"pca → {pca_hdg:.0f}°", fontsize=9, color="#cc0000")
        else:
            ax2.imshow(crop); ax2.set_title("pca → n/a", fontsize=9)
        ax2.set_xticks([]); ax2.set_yticks([])

        # col 3: truth
        ax3 = fig.add_subplot(gs[i, 3])
        if truth is not None:
            ax3.imshow(overlay_rotated_template(crop, template_mask, truth, (40, 220, 40)))
            ax3.set_title(f"truth → {truth:.0f}°", fontsize=9, color="#008800")
        else:
            ax3.imshow(crop); ax3.set_title("truth → n/a", fontsize=9)
        ax3.set_xticks([]); ax3.set_yticks([])

        # col 4: IoU vs rotation
        ax4 = fig.add_subplot(gs[i, 4])
        angles = sorted(scores.keys())
        ious = [scores[a] for a in angles]
        ax4.plot(angles, ious, color="#0088aa", linewidth=1.4)
        ax4.axvline(matched, color="#0088aa", linestyle=":",
                    label=f"matcher {matched:.0f}°")
        if pca_hdg is not None:
            ax4.axvline(pca_hdg, color="#cc0000", linestyle=":",
                        label=f"pca {pca_hdg:.0f}°")
        if truth is not None:
            ax4.axvline(truth, color="#008800", linestyle=":",
                        label=f"truth {truth:.0f}°")
        ax4.set_xlim(0, 360)
        ax4.set_xticks([0, 90, 180, 270, 360])
        ax4.set_xlabel("rotation (°)", fontsize=8)
        ax4.set_ylabel("IoU", fontsize=8)
        ax4.tick_params(labelsize=7)
        ax4.legend(loc="upper right", fontsize=7, framealpha=0.85)
        ax4.grid(alpha=0.3)

    out = args.out or (args.session_dir / "ship_match_viz.png")
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"→ {out}")


if __name__ == "__main__":
    main()
