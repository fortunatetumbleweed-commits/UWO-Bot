"""Render Florence-2's segmentation polygons overlaid on the
saved minimap frames so we can see exactly what it picked as
'land'.  Pair with the caption text it produced.

Usage
─────
  python -m tools.viz_florence2 \\
      data/sessions/ai_nav_live_palma \\
      --ticks 1,35,90,149,187
"""
from __future__ import annotations

import argparse
import sys
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from PIL import Image

from tools.probe_florence2 import run_task, _ensure_model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--ticks", required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--query", default="the solid whitish gray land area",
                   help="Referring-expression query for segmentation")
    args = ap.parse_args()

    _ensure_model("microsoft/Florence-2-base")

    ticks = [int(t) for t in args.ticks.split(",")]
    n_rows = len(ticks)

    fig = plt.figure(figsize=(13, 3.2 * n_rows))
    gs = gridspec.GridSpec(
        n_rows, 2, figure=fig,
        width_ratios=[1, 1.8], hspace=0.3, wspace=0.1,
    )

    for row, tick in enumerate(ticks):
        img_path = args.session_dir / f"tick_{tick:04d}.png"
        if not img_path.exists():
            continue
        img = Image.open(img_path).convert("RGB")

        # CAPTION
        cap_out, t_cap = run_task(img, "<MORE_DETAILED_CAPTION>")
        caption = cap_out.get("<MORE_DETAILED_CAPTION>", "(none)")

        # SEGMENTATION
        seg_out, t_seg = run_task(
            img, "<REFERRING_EXPRESSION_SEGMENTATION>",
            text_input=args.query,
        )
        seg = seg_out.get("<REFERRING_EXPRESSION_SEGMENTATION>", {})
        polys = seg.get("polygons", [])

        # ─── Image panel with seg overlay ───
        ax_img = fig.add_subplot(gs[row, 0])
        ax_img.imshow(np.asarray(img))
        for poly_list in polys:
            for pts in poly_list:
                if not pts:
                    continue
                xs = pts[0::2]; ys = pts[1::2]
                # Outline
                ax_img.plot(xs + xs[:1], ys + ys[:1],
                            color="#ff33aa", linewidth=2.0, alpha=0.95)
                # Fill (semi-transparent)
                ax_img.fill(xs, ys, color="#ff33aa", alpha=0.18)
        ax_img.set_xticks([]); ax_img.set_yticks([])
        ax_img.set_title(
            f"t{tick}  ({img.size[0]}×{img.size[1]})  "
            f"seg={t_seg:.1f}s  cap={t_cap:.1f}s\n"
            f"query: \"{args.query}\"",
            fontsize=9,
        )

        # ─── Caption text panel ───
        ax_txt = fig.add_subplot(gs[row, 1])
        ax_txt.axis("off")
        n_polys = len(polys)
        total_points = sum(
            len(pts) // 2
            for pl in polys for pts in pl
        )
        ax_txt.set_title(
            f"Florence-2 caption  "
            f"[seg: {n_polys} polygon(s), {total_points} pts]",
            fontsize=10,
        )
        wrapped = textwrap.fill(caption, width=70)
        ax_txt.text(0.0, 1.0, wrapped, fontsize=8.5, family="monospace",
                    va="top", ha="left", transform=ax_txt.transAxes)

    out = args.out or (args.session_dir / "florence2_viz.png")
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"→ {out}")


if __name__ == "__main__":
    main()
