"""Stack V11 water mask + shore_pts polyline on the raw minimap so we
can see whether the shore picker's failures live in the mask or in the
contour-selection layer.

For each chosen tick, dumps a single PNG with:
  - raw minimap RGB at 60% alpha (so the true waterline is visible),
  - V11 water mask as translucent blue (where V11 says "water"),
  - V11 mask edge as dark blue (the literal contour the picker walks),
  - shore_pts polyline in pink (from trace.jsonl),
  - path_pts polyline in cyan (from trace.jsonl).

With ``--close-px N`` set, the V11 mask is morphologically closed
(``binary_closing`` with ``disk(N)``) before contour extraction and
the shore picker is RE-RUN on the closed mask.  The diagnostic then
shows the closed mask in blue and the new shore/path polylines, so we
can see whether plugging the village-icon / text holes recovers a
sensible shore.

Read off two things:
  - Is the pink polyline ON the blue mask edge?  (should be, by
    find_contours construction).
  - Is the blue mask edge ON the visible waterline?  If it sits inland
    by ~10 px, V11's brightness threshold is the offender.

Usage
─────
  # Original (recorded) shore on raw V11 mask:
  python -m tools.diagnose_shore_mask \\
    data/sessions/live_centerline_2026-06-15T15-57-02 \\
    --ticks 60,85,90,95

  # Re-run the shore picker on a morphologically-closed mask:
  python -m tools.diagnose_shore_mask \\
    data/sessions/live_centerline_2026-06-15T15-57-02 \\
    --ticks 60,85,90,95  --close-px 18
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.centerline_extraction_prototype import channel_mask_from_rgb  # noqa: E402
from tools.shore_waypoint_picker import extract_shore_path  # noqa: E402


def _load_trace(trace_path: Path) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for line in trace_path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        out[r["tick"]] = r
    return out


def render(sess: Path, tick: int, rec: dict, out_dir: Path,
           close_px: int, side: str, walk_px: int, safe_px: int) -> Path:
    crop_name = rec.get("crop") or f"tick_{tick:04d}.png"
    crop_path = sess / crop_name
    rgb_raw = np.asarray(Image.open(crop_path).convert("RGB"))

    img_trim, mask = channel_mask_from_rgb(rgb_raw)

    # Morphological close + re-run picker.
    if close_px > 0:
        from skimage.morphology import binary_closing, disk
        mask_closed = binary_closing(mask, disk(close_px))
        H_mm, W_mm = mask_closed.shape
        ship_center = (W_mm // 2, H_mm // 2)
        heading = rec.get("heading_deg") or rec.get("commanded_deg_this") or 180.0
        shore_pts, path_pts = extract_shore_path(
            mask_closed, ship_center, float(heading),
            side=side, walk_distance_px=walk_px, safe_distance_px=safe_px,
        )
        display_mask = mask_closed
        wp = list(path_pts[-1]) if path_pts else None
        title_suffix = f"  CLOSED disk({close_px})"
    else:
        display_mask = mask
        shore_pts = rec.get("shore_pts") or []
        path_pts = rec.get("path_pts") or []
        wp = rec.get("waypoint_px")
        title_suffix = ""

    fig, ax = plt.subplots(figsize=(10, 5.5))
    ax.imshow(img_trim, alpha=0.65)

    # Water mask (raw or closed) as translucent blue.
    overlay = np.zeros((*display_mask.shape, 4))
    overlay[display_mask] = [0.20, 0.55, 1.0, 0.32]
    ax.imshow(overlay)

    # Mask edge — the literal iso-line the picker walks.
    ax.contour(display_mask.astype(float), levels=[0.5],
               colors=["#0044aa"], linewidths=1.0, alpha=0.9)

    if shore_pts:
        sx = [p[1] for p in shore_pts]
        sy = [p[0] for p in shore_pts]
        ax.plot(sx, sy, color="#ff33aa", linewidth=2.5,
                alpha=0.95, label=f"shore_pts ({len(shore_pts)})")

    if path_pts:
        px = [p[1] for p in path_pts]
        py = [p[0] for p in path_pts]
        ax.plot(px, py, color="#00ffff", linewidth=1.8, alpha=0.9,
                linestyle=(0, (3, 2)),
                label=f"path_pts ({len(path_pts)})")

    if wp:
        ax.scatter([wp[1]], [wp[0]], s=220, c="#ff9900",
                   edgecolors="black", linewidths=1.4, marker="*",
                   zorder=8, label="waypoint")

    H, W = display_mask.shape
    cx, cy = W // 2, H // 2
    ax.scatter([cx], [cy], s=140, c="lime",
               edgecolors="black", linewidths=1.2, zorder=9, label="ship")

    # Heading + port/starboard arrows so we can read sides unambiguously.
    import math
    heading = rec.get("heading_deg") or rec.get("commanded_deg_this") or 180.0
    hr = math.radians(heading)
    hy, hx = -math.cos(hr), math.sin(hr)
    ax.annotate("", xy=(cx + hx*55, cy + hy*55), xytext=(cx, cy),
                arrowprops=dict(arrowstyle="-|>", color="lime",
                                lw=2.2, mutation_scale=14),
                zorder=10)
    ax.text(cx + hx*60, cy + hy*60, f" hdg {heading:.0f}°",
            color="lime", fontsize=8, fontweight="bold", zorder=10)
    # Port (heading - 90°)
    pr = math.radians((heading - 90) % 360)
    py_, px_ = -math.cos(pr), math.sin(pr)
    ax.annotate("", xy=(cx + px_*45, cy + py_*45), xytext=(cx, cy),
                arrowprops=dict(arrowstyle="-|>", color="#ff33aa",
                                lw=1.8, mutation_scale=12),
                zorder=10)
    ax.text(cx + px_*50, cy + py_*50, " PORT",
            color="#ff33aa", fontsize=8, fontweight="bold", zorder=10)
    # Starboard (heading + 90°)
    sr = math.radians((heading + 90) % 360)
    spy, spx = -math.cos(sr), math.sin(sr)
    ax.annotate("", xy=(cx + spx*45, cy + spy*45), xytext=(cx, cy),
                arrowprops=dict(arrowstyle="-|>", color="#ffaa00",
                                lw=1.8, mutation_scale=12),
                zorder=10)
    ax.text(cx + spx*50, cy + spy*50, " STBD",
            color="#ffaa00", fontsize=8, fontweight="bold", zorder=10)

    lat = rec.get("lat"); lon = rec.get("lon")
    hdg = rec.get("heading_deg")
    title = (f"tick {tick}  sim=({lat:.3f},{lon:.3f})  hdg={hdg:.0f}°{title_suffix}"
             if lat is not None and hdg is not None
             else f"tick {tick}{title_suffix}")
    ax.set_title(title, fontsize=11)
    ax.legend(loc="lower right", fontsize=8, framealpha=0.85)
    ax.axis("off")
    suffix = f"_close{close_px:02d}" if close_px > 0 else ""
    out_path = out_dir / f"diag_t{tick:04d}{suffix}.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--ticks", required=True,
                    help="Comma-separated tick numbers, e.g. 60,85,90,95")
    ap.add_argument("--close-px", type=int, default=0,
                    help="Apply binary_closing(disk(N)) to V11 mask before "
                         "extraction and re-run the shore picker.")
    ap.add_argument("--side", choices=("port", "starboard"), default="port")
    ap.add_argument("--walk", type=int, default=80)
    ap.add_argument("--safe", type=int, default=25)
    args = ap.parse_args()

    sess = args.session_dir
    trace = _load_trace(sess / "trace.jsonl")
    out_dir = sess / "shore_mask_diag"
    out_dir.mkdir(exist_ok=True)
    for t in (int(x) for x in args.ticks.split(",")):
        rec = trace.get(t)
        if rec is None:
            print(f"  t{t}: no trace record — skipping")
            continue
        p = render(sess, t, rec, out_dir, args.close_px,
                   args.side, args.walk, args.safe)
        print(f"  t{t} → {p}")
    print(f"\nDone. View: {out_dir}/diag_t*.png")


if __name__ == "__main__":
    main()
