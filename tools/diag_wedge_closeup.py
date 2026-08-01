"""Close-up wedge view of ship-heading detection.

For each input mini-map crop, crop tightly around the ship region and
render a multi-panel close-up showing the raw color masks the
detector consumes.  Each row of the output is one frame; columns are:

  1. RGB (raw)
  2. ship_green mask (cleaned)
  3. yellow_any mask
  4. very_white + light_gray mask (text + sprite roofs live here)
  5. heading reading overlaid

Each panel is rendered at 12x scale so individual pixels are visible.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from vision.minimap_navigation_view import (
    _clean_ship_green,
    _color_masks,
    _ship_heading_detector as DET,
)
from tools.diag_ship_heading import (
    _run_baseline,
    _run_text_close,
    _run_pointy_tip,
    _run_yellow_centroid,
    _run_yellow_centroid_full,
    _run_yellow_centroid_touches,
    _run_pca_yellow_anchor,
)


SCALE = 12
CROP_HALF = 32       # px on each side of ship centroid


def _font(size=14):
    for p in ("/System/Library/Fonts/Menlo.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _mask_to_rgb(mask: np.ndarray, color: tuple[int, int, int]) -> np.ndarray:
    out = np.full((mask.shape[0], mask.shape[1], 3), 30, dtype=np.uint8)
    out[mask] = color
    return out


def _crop(arr: np.ndarray, cx: float, cy: float, half: int = CROP_HALF):
    h, w = arr.shape[:2]
    x0, y0 = max(0, int(cx) - half), max(0, int(cy) - half)
    x1, y1 = min(w, int(cx) + half), min(h, int(cy) + half)
    if arr.ndim == 3:
        return arr[y0:y1, x0:x1, :], (x0, y0)
    return arr[y0:y1, x0:x1], (x0, y0)


def _render_image(arr: np.ndarray, scale: int = SCALE) -> Image.Image:
    if arr.ndim == 2:
        arr = np.stack([arr.astype(np.uint8) * 255] * 3, axis=-1)
    img = Image.fromarray(arr)
    return img.resize((img.size[0] * scale, img.size[1] * scale),
                      Image.NEAREST)


def _annotate(img: Image.Image, caption: str,
              cands=None, origin=(0, 0), reading=None, ship_local=None,
              ) -> Image.Image:
    """Caption strip + candidate circles + heading arrow."""
    f = _font(13)
    strip = Image.new("RGB", (img.size[0], 26), (40, 40, 40))
    ImageDraw.Draw(strip).text((6, 4), caption, font=f, fill=(255, 255, 255))
    out = Image.new("RGB", (img.size[0], img.size[1] + 26), (0, 0, 0))
    out.paste(strip, (0, 0))
    out.paste(img, (0, 26))
    d = ImageDraw.Draw(out)
    ox, oy = origin
    if cands:
        for c in cands:
            wx = (c["wx"] - ox) * SCALE
            wy = (c["wy"] - oy) * SCALE + 26
            col = (0, 220, 0) if c["touches_green"] and c["on_axis"] else (200, 180, 0)
            d.ellipse((wx - 9, wy - 9, wx + 9, wy + 9), outline=col, width=2)
            d.text((wx + 11, wy - 8),
                   f"a={c['area']} d={c['dist']:.1f}"
                   f" t={int(c['touches_green'])} x={int(c['on_axis'])}",
                   font=_font(10), fill=col)
    if reading is not None and ship_local is not None:
        cx, cy = (ship_local[0] - ox) * SCALE, (ship_local[1] - oy) * SCALE + 26
        d.ellipse((cx - 4, cy - 4, cx + 4, cy + 4),
                  fill=(0, 200, 255), outline=(0, 0, 0))
        ang = math.radians(reading.bearing_deg)
        L = 80
        ex = cx + L * math.sin(ang)
        ey = cy - L * math.cos(ang)
        d.line((cx, cy, ex, ey), fill=(0, 255, 80), width=4)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frames", nargs="+", type=Path)
    ap.add_argument("--out", default="/tmp/wedge_closeup.png", type=Path)
    args = ap.parse_args()

    rows = []
    for fpath in args.frames:
        rgb = np.asarray(Image.open(fpath).convert("RGB"))
        color = _color_masks(rgb)
        ship_green_raw = _clean_ship_green(color["ship_green"])
        gys, gxs = np.where(ship_green_raw)
        if gxs.size == 0:
            gys, gxs = np.where(color["ship_green"])
        if gxs.size == 0:
            print(f"{fpath.name}: NO ship pixels at all; skipping")
            continue
        cx, cy = float(gxs.mean()), float(gys.mean())

        # Run methods on the FULL frame.
        r_base, dbg_base = _run_baseline(rgb)
        r_pt,   dbg_pt   = _run_yellow_centroid_touches(rgb)  # column 6
        r_yc,   dbg_yc   = _run_pca_yellow_anchor(rgb)        # column 7

        # Crops centered on baseline ship centroid for consistency.
        rgb_c, origin = _crop(rgb, cx, cy)
        green_base_c, _ = _crop(
            dbg_base.get("green", ship_green_raw).astype(np.uint8), cx, cy)
        green_tc_c, _ = _crop(
            dbg_pt.get("green", ship_green_raw).astype(np.uint8), cx, cy)
        yellow_c, _ = _crop(color["yellow_any"].astype(np.uint8), cx, cy)
        white_c, _ = _crop(((color["very_white"] | color["light_gray"])
                            ).astype(np.uint8), cx, cy)

        def _cap_reading(r, label):
            if r is None:
                return f"{label}: NO READING"
            return (f"{label}: brg={r.bearing_deg:.0f}° "
                    f"{r.strategy} conf={r.confidence:.2f}")

        cx_pt = dbg_pt.get("cx", cx); cy_pt = dbg_pt.get("cy", cy)
        cx_yc = dbg_yc.get("cx", cx); cy_yc = dbg_yc.get("cy", cy)

        # Render the result panels (one per method).
        rgb_arrow_base = _render_image(rgb_c)
        rgb_arrow_pt   = _render_image(rgb_c)
        rgb_arrow_yc   = _render_image(rgb_c)

        # For yellow_centroid: also draw a dot at the yellow centroid.
        yc_marker = dbg_yc.get("yellow_centroid")

        panel_a = _annotate(_render_image(rgb_c),
                             f"{fpath.name}  raw RGB")
        panel_white = _annotate(_render_image(_mask_to_rgb(
            white_c.astype(bool), (200, 200, 255))),
            "very_white + light_gray (text + sprites)")
        panel_g_tc = _annotate(_render_image(_mask_to_rgb(
            green_tc_c.astype(bool), (60, 230, 100))),
            f"text_close green ({int(green_tc_c.sum())} px)")

        # Dedicated yellow panel: yellow_any mask + centroid + green
        # centroid + connecting line so we can see exactly what
        # yellow_centroid is measuring.
        yellow_used = dbg_yc.get("yellow_used")
        if yellow_used is None:
            yellow_used = color["yellow_any"]
        yellow_c2, _ = _crop(yellow_used.astype(np.uint8), cx, cy)
        panel_yellow_img = _render_image(
            _mask_to_rgb(yellow_c2.astype(bool), (240, 220, 60)))
        cap_y = (f"yellow used  green_c=({cx_yc:.0f},{cy_yc:.0f})"
                 if yc_marker is None else
                 f"yellow used  green→yellow vector  brg={r_yc.bearing_deg:.0f}°")
        panel_yellow = _annotate(panel_yellow_img, cap_y)
        # Draw green centroid (cyan) + yellow centroid (magenta) +
        # connecting line on the yellow panel.
        d = ImageDraw.Draw(panel_yellow)
        gx_p = (cx_yc - origin[0]) * SCALE
        gy_p = (cy_yc - origin[1]) * SCALE + 26
        d.ellipse((gx_p - 8, gy_p - 8, gx_p + 8, gy_p + 8),
                   fill=(0, 220, 255), outline=(0, 0, 0), width=2)
        if yc_marker is not None:
            mx = (yc_marker[0] - origin[0]) * SCALE
            my = (yc_marker[1] - origin[1]) * SCALE + 26
            d.line((gx_p, gy_p, mx, my), fill=(255, 80, 255), width=4)
            d.ellipse((mx - 9, my - 9, mx + 9, my + 9),
                       fill=(255, 80, 255), outline=(0, 0, 0), width=2)

        panel_base = _annotate(rgb_arrow_base, _cap_reading(r_base, "baseline"),
                                reading=r_base, ship_local=(cx, cy),
                                origin=origin)
        panel_pt = _annotate(rgb_arrow_pt,
                              _cap_reading(r_pt, "yc_touches"),
                              reading=r_pt, ship_local=(cx_pt, cy_pt),
                              origin=origin)
        panel_yc = _annotate(rgb_arrow_yc,
                              _cap_reading(r_yc, "pca_yellow_anchor"),
                              reading=r_yc, ship_local=(cx_yc, cy_yc),
                              origin=origin)
        # Mark yellow centroid (large magenta) + green centroid (cyan)
        # on the RGB-overlay panel too.
        d = ImageDraw.Draw(panel_yc)
        gx_p = (cx_yc - origin[0]) * SCALE
        gy_p = (cy_yc - origin[1]) * SCALE + 26
        d.ellipse((gx_p - 7, gy_p - 7, gx_p + 7, gy_p + 7),
                   fill=(0, 220, 255), outline=(0, 0, 0), width=2)
        if yc_marker is not None:
            mx = (yc_marker[0] - origin[0]) * SCALE
            my = (yc_marker[1] - origin[1]) * SCALE + 26
            d.ellipse((mx - 9, my - 9, mx + 9, my + 9),
                       fill=(255, 80, 255), outline=(0, 0, 0), width=2)

        panels = [panel_a, panel_white, panel_g_tc, panel_yellow,
                  panel_base, panel_pt, panel_yc]

        # Console summary
        print(f"=== {fpath.name} ===")
        print(f"  baseline       : {_cap_reading(r_base, '')}")
        print(f"  pointy_tip     : {_cap_reading(r_pt,   '')}")
        print(f"  yellow_centroid: {_cap_reading(r_yc,   '')}")

        rows.append(panels)

    # Compose grid
    cell_w = max(p.size[0] for r in rows for p in r)
    cell_h = max(p.size[1] for r in rows for p in r)
    n_cols = max(len(r) for r in rows)
    grid = Image.new("RGB", (cell_w * n_cols, cell_h * len(rows)), (20, 20, 20))
    for r, row in enumerate(rows):
        for c, panel in enumerate(row):
            grid.paste(panel, (c * cell_w, r * cell_h))
    grid.save(args.out)
    print(f"\nWrote {args.out}  ({grid.size})")


if __name__ == "__main__":
    main()
