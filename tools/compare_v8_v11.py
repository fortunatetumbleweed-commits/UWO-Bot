"""Side-by-side: V8 (perception_channel_graph_prototype) vs V11
(v11_brightness_channel_mask) water masks on the same minimap
frames.

Renders 3 columns per tick:
  - raw minimap
  - V8 water mask + bank contour
  - V11 water mask + bank contour

V8 = the 2026-06-08 channel-graph prototype with sprite/text
masking + K-means clustering + hole-fill.  More sophisticated
than V11's brightness threshold; possibly closer to the lost
V3b "perfect bank line" iteration.

Usage
─────
  python -m tools.compare_v8_v11 \\
      data/sessions/ai_nav_live_palma \\
      --ticks 1,35,90,149,187
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import matplotlib.pyplot as plt
import numpy as np
from PIL import Image


def v8_mask(img_path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Run V8 on a path, return (trimmed_rgb, water_mask, sprite_mask, secs)."""
    from tools.perception_channel_graph_prototype import water_mask_from_image
    t0 = time.time()
    rgb, mask, spr = water_mask_from_image(str(img_path))
    return rgb, mask, spr, time.time() - t0


def v11_mask(img_path: Path) -> tuple[np.ndarray, np.ndarray, float]:
    """Run V11 (the brightness-threshold mask) on a path, return
    (trimmed_rgb, water_mask, secs)."""
    from tools.centerline_extraction_prototype import channel_mask_from_rgb
    rgb_raw = np.asarray(Image.open(img_path).convert("RGB"))
    t0 = time.time()
    rgb, mask = channel_mask_from_rgb(rgb_raw)
    return rgb, mask, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--ticks", required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    ticks = [int(t) for t in args.ticks.split(",")]
    n_rows = len(ticks)

    fig, axes = plt.subplots(
        n_rows, 3, figsize=(15, 2.6 * n_rows), squeeze=False,
    )

    for row, tick in enumerate(ticks):
        path = args.session_dir / f"tick_{tick:04d}.png"
        if not path.exists():
            continue

        # ── raw ──
        rgb_raw = np.asarray(Image.open(path).convert("RGB"))
        axes[row, 0].imshow(rgb_raw)
        axes[row, 0].set_title(f"t{tick} — raw", fontsize=9)
        axes[row, 0].set_xticks([]); axes[row, 0].set_yticks([])

        # ── V8 ──
        try:
            rgb_v8, mask_v8, spr_v8, t_v8 = v8_mask(path)
            axes[row, 1].imshow(rgb_v8, alpha=0.7)
            ov = np.zeros((*mask_v8.shape, 4))
            ov[mask_v8] = [0.20, 0.55, 1.0, 0.32]
            axes[row, 1].imshow(ov)
            axes[row, 1].contour(
                mask_v8.astype(float), levels=[0.5],
                colors=["#ff33aa"], linewidths=1.6, alpha=0.95,
            )
            water_frac = float(mask_v8.mean())
            axes[row, 1].set_title(
                f"V8  [{t_v8:.2f}s]  water={water_frac:.1%}",
                fontsize=9,
            )
        except Exception as e:
            axes[row, 1].text(0.5, 0.5, f"V8 failed:\n{e}",
                              ha="center", va="center")
        axes[row, 1].set_xticks([]); axes[row, 1].set_yticks([])

        # ── V11 ──
        try:
            rgb_v11, mask_v11, t_v11 = v11_mask(path)
            axes[row, 2].imshow(rgb_v11, alpha=0.7)
            ov = np.zeros((*mask_v11.shape, 4))
            ov[mask_v11] = [0.20, 0.55, 1.0, 0.32]
            axes[row, 2].imshow(ov)
            axes[row, 2].contour(
                mask_v11.astype(float), levels=[0.5],
                colors=["#0044aa"], linewidths=1.6, alpha=0.95,
            )
            water_frac = float(mask_v11.mean())
            axes[row, 2].set_title(
                f"V11 [{t_v11:.2f}s]  water={water_frac:.1%}",
                fontsize=9,
            )
        except Exception as e:
            axes[row, 2].text(0.5, 0.5, f"V11 failed:\n{e}",
                              ha="center", va="center")
        axes[row, 2].set_xticks([]); axes[row, 2].set_yticks([])

    out = args.out or (args.session_dir / "v8_vs_v11.png")
    fig.tight_layout()
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"→ {out}")


if __name__ == "__main__":
    main()
