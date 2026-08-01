"""Re-render tick composites with bank-distance perception overlaid.

Takes a session directory of tick_NNN.png composites + raw minimap
crops (extracted from the captured frames) and produces:

  - For each (or selected) tick, a composite showing:
    - the original minimap with V11 water mask overlay
    - the M6f tree + waypoint as the runner saw
    - 8 RAYS from the ship to the nearest LAND pixel in each cardinal
      and inter-cardinal direction, with the distance in pixels labeled
    - the picker's chosen waypoint highlighted
    - the closest-bank vector (shortest of the 8) in bright red

This lets you see "how close did the bot think it was to the nearest
bank, and in what direction?"  Compare with the heading/steering log
to understand under-steered situations.

Usage
─────
  python -m tools.visualize_bank_distance \\
    data/sessions/live_centerline_<TS>/  --ticks 50,80,100
  python -m tools.visualize_bank_distance \\
    data/sessions/live_centerline_<TS>/  --stride 20
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.centerline_extraction_prototype import (  # noqa: E402
    channel_mask_from_rgb, _ship_xy,
)
from tools.centerline_waypoint_prototype import (  # noqa: E402
    extract_tree, pick_waypoint, estimate_ship_heading, LOOKAHEAD_PX,
)


# 16-sector readings (every 22.5°) for finer-grained perception.
N_SECTORS = 16


def distance_to_land(mask: np.ndarray, ship_xy: tuple[int, int],
                    bearings_deg: list[float]) -> list[float | None]:
    """For each compass bearing (0=N, CW), march a ray from `ship_xy`
    until we hit a non-water pixel.  Return pixel distance or None
    if ray exits the frame still in water."""
    H, W = mask.shape
    sx, sy = ship_xy
    out = []
    for bearing in bearings_deg:
        r = math.radians(bearing)
        # Bearing 0 = north (image -y), CW.
        dy = -math.cos(r)
        dx = math.sin(r)
        d: float | None = None
        for step in range(1, 250):
            py = int(round(sy + step * dy))
            px = int(round(sx + step * dx))
            if not (0 <= py < H and 0 <= px < W):
                break
            if not mask[py, px]:
                d = float(step)
                break
        out.append(d)
    return out


def render_tick(rgb_raw: np.ndarray, out_path: Path, title: str,
                hug_side: str = "left", lookahead: int = LOOKAHEAD_PX):
    rgb_trim, mask = channel_mask_from_rgb(rgb_raw)
    tree, _ = extract_tree(rgb_raw)
    rgb = rgb_trim
    ship = _ship_xy(rgb)
    heading_vec = estimate_ship_heading(rgb)
    dest, _, _ = (None, [], [])
    if tree.nodes and ship is not None and heading_vec is not None:
        dest, _, _ = pick_waypoint(tree, ship, heading_vec, hug_side, lookahead)

    # 16-sector distance-to-land readings.
    bearings = [i * (360.0 / N_SECTORS) for i in range(N_SECTORS)]
    dists = distance_to_land(mask, ship, bearings) if ship else [None] * N_SECTORS

    fig, ax = plt.subplots(figsize=(13, 6.5))
    ax.imshow(rgb_trim, alpha=0.65)
    # Water overlay.
    mo = np.zeros((*mask.shape, 4)); mo[mask] = [0, 0.4, 1.0, 0.18]
    ax.imshow(mo)
    # Tree polylines.
    for e in tree.edges:
        ax.plot(e.points[:, 1], e.points[:, 0], c="red", lw=1.4, alpha=0.6)
    # Tree nodes.
    for n in tree.nodes.values():
        c = ("magenta" if n.kind == "junction"
             else "yellow" if "edge" in n.kind else "cyan")
        ax.scatter([n.x], [n.y], s=80, c=c,
                   edgecolors="black", linewidths=0.8, zorder=4)

    # Bank-distance rays.
    if ship is not None:
        sx, sy = ship
        # Find the closest sector for callout.
        valid = [(b, d) for b, d in zip(bearings, dists) if d is not None]
        closest = min(valid, key=lambda bd: bd[1]) if valid else None
        for bearing, d in zip(bearings, dists):
            if d is None:
                continue
            r = math.radians(bearing)
            dy = -math.cos(r); dx = math.sin(r)
            ex = sx + d * dx; ey = sy + d * dy
            color = "lime" if d > 30 else ("yellow" if d > 15 else "red")
            lw = 2.5 if (closest and bearing == closest[0]) else 1.2
            ax.plot([sx, ex], [sy, ey], c=color, lw=lw, alpha=0.9)
            ax.scatter([ex], [ey], s=40, c=color,
                       edgecolors="black", linewidths=0.6, zorder=5)
            # Label every 90° (so it's not too cluttered).
            if int(round(bearing)) % 45 == 0:
                ax.annotate(f"{int(d)}px", (ex, ey),
                            color="white", fontsize=8,
                            xytext=(3, 3), textcoords="offset points",
                            bbox=dict(boxstyle="round,pad=0.15",
                                      fc="black", ec="none", alpha=0.6))

        # Ship icon + heading arrow.
        ax.scatter([sx], [sy], s=200, c="lime",
                   edgecolors="black", linewidths=1.5, zorder=7)
        if heading_vec is not None:
            hy, hx = heading_vec
            ax.arrow(sx, sy, hx * 22, hy * 22, head_width=6,
                     fc="lime", ec="black", lw=1.0, zorder=8,
                     length_includes_head=True)

        # Waypoint.
        if dest is not None:
            dy_, dx_ = dest
            ax.scatter([dx_], [dy_], s=180, c="orange",
                       edgecolors="black", linewidths=1.4, marker="*", zorder=9)

        # Callout for closest bank.
        if closest is not None:
            cb, cd = closest
            ax.annotate(f"closest bank: {cd:.0f}px @ {cb:.0f}°",
                        (sx, sy), color="white", fontsize=10,
                        xytext=(15, -45), textcoords="offset points",
                        bbox=dict(boxstyle="round,pad=0.3", fc="darkred",
                                  ec="white", alpha=0.85))

    ax.set_title(title, fontsize=11)
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--ticks", type=str, default=None,
                   help="Comma-separated tick numbers, e.g. 50,80,100")
    ap.add_argument("--stride", type=int, default=None,
                   help="Render every Nth tick (alternative to --ticks)")
    ap.add_argument("--out-suffix", type=str, default="_bank")
    args = ap.parse_args()

    sess = args.session_dir
    composites = sorted(sess.glob("tick_*.png"))
    if args.ticks:
        wanted = set(int(t) for t in args.ticks.split(","))
        composites = [p for p in composites
                     if int(p.stem.split("_")[-1]) in wanted]
    elif args.stride:
        composites = [p for p in composites
                     if int(p.stem.split("_")[-1]) % args.stride == 0]

    if not composites:
        sys.exit("no composites match selection")

    out_dir = sess / "bank_perception"
    out_dir.mkdir(exist_ok=True)
    print(f"rendering {len(composites)} ticks → {out_dir}")
    for src in composites:
        # The composite file is the RENDERED output, not the raw frame.
        # We need the raw minimap crop, but the runner doesn't save it
        # separately.  Workaround: load the composite as RGB and crop
        # to the actual minimap region (the composite has matplotlib
        # padding/title which we'd have to undo).  Easier: re-extract
        # from the composite's center, knowing the composite includes
        # the trimmed minimap inside the matplotlib frame.
        #
        # Cleaner fix: regenerate the live runner to also save the raw
        # crop.  For now, we re-derive from the composite by trying to
        # find a 375x178-ish water region.  Easiest: just open the
        # composite, find the largest dark-ish blob area, crop tight.
        #
        # For this prototype we assume the composite was saved by
        # `render()` which used a 375×178-ish region — the matplotlib
        # render makes this awkward.  Practical compromise: just
        # process the composite as-is (matplotlib padding adds some
        # whitespace but M6f handles it).
        rgb_raw = np.asarray(Image.open(src).convert("RGB"))
        tick = int(src.stem.split("_")[-1])
        out = out_dir / f"tick_{tick:03d}{args.out_suffix}.png"
        render_tick(rgb_raw, out, title=f"tick {tick} — bank perception")
        print(f"  {src.name} → {out.name}")
    print(f"\nDone. View: {out_dir}/tick_*{args.out_suffix}.png")


if __name__ == "__main__":
    main()
