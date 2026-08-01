"""Draw a vertical list of failure-case ticks with all readings.

For each tick in the failure list:
  - Left: 3x-scaled mini-map with 5 heading arrows overlaid
      GREEN   = primary CNN raw (v6 or v5 depending on voyage)
      MAGENTA = shadow CNN raw   (v5 or v6)
      CYAN    = PCA live
      ORANGE  = motion bearing (1-tick prev→curr)
      WHITE   = final "picked" heading used for steering
  - Right: multi-line text with tick number, lat/lon, speed, all
      readings, confidences, and source tags.

Two output PNGs — one for the v6-primary voyage 4, one for the
v5-primary voyage 3.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from brain.ai_nav.layers.heading import PCAHeading


COLORS = {
    "v6":     (85, 255, 85),      # green
    "v5":     (255, 85, 255),     # magenta
    "pca":    (85, 255, 255),     # cyan
    "motion": (255, 165, 85),     # orange
    "picked": (255, 255, 255),    # white
}

ROW_H = 200         # per-row height (px)
MAP_W = 360         # 120 * 3 scale
MAP_H = 180         # 60 * 3 scale — actual minimap is 400x190, cropped
TEXT_W = 480        # right-hand text column width
MARGIN = 8
LEGEND_H = 40


def _load_font(size: int):
    try:
        return ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", size)
    except (OSError, IOError):
        return ImageFont.load_default()


def _compass_arrows(img: Image.Image, arrows: dict[str, float | None]
                    ) -> Image.Image:
    """Overlay compass ring + labeled arrows.  `arrows` maps
    name → bearing_deg (None to skip)."""
    out = img.convert("RGB").copy()
    draw = ImageDraw.Draw(out, "RGBA")
    W, H = out.size
    cx, cy = W // 2, H // 2
    r = min(W, H) // 2 - 8

    # Compass ring + N/E/S/W ticks
    draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                 outline=(255, 255, 255, 100), width=1)
    font_small = _load_font(9)
    for deg, txt in [(0, "N"), (90, "E"), (180, "S"), (270, "W")]:
        th = math.radians(deg)
        dx, dy = math.sin(th), -math.cos(th)
        draw.text((cx + (r - 12) * dx - 4, cy + (r - 12) * dy - 5),
                  txt, fill=(255, 255, 0, 200), font=font_small)

    for name, deg in arrows.items():
        if deg is None: continue
        color = COLORS[name] + (255,)
        th = math.radians(deg)
        dx, dy = math.sin(th), -math.cos(th)
        # Slight per-arrow offset so overlapping arrows don't hide each other
        offs = {"v6": (0, 0), "v5": (2, 0), "pca": (0, 2),
                "motion": (-2, 0), "picked": (0, -2)}
        ox, oy = offs.get(name, (0, 0))
        x1, y1 = cx + ox, cy + oy
        L = r * 0.85
        x2, y2 = x1 + L * dx, y1 + L * dy
        draw.line([(x1, y1), (x2, y2)], fill=color, width=3)
        # Arrowhead
        for sign in (-1, 1):
            ah = th + sign * math.radians(25) + math.pi
            ax, ay = x2 + 10 * math.sin(ah), y2 - 10 * math.cos(ah)
            draw.line([(x2, y2), (int(ax), int(ay))], fill=color, width=3)
    return out


def _motion_bearing(recs_by_tick: dict, tick: int
                    ) -> float | None:
    prev, curr = recs_by_tick.get(tick - 1), recs_by_tick.get(tick)
    if not (prev and curr): return None
    la, lo = prev.get('lat'), prev.get('lon')
    lb, lob = curr.get('lat'), curr.get('lon')
    if None in (la, lo, lb, lob): return None
    dlat, dlon = lb - la, lob - lo
    if abs(dlat) < 1e-5 and abs(dlon) < 1e-5: return None
    if abs(dlat) > 0.15 or abs(dlon) > 0.12: return None
    return (math.degrees(math.atan2(dlon, dlat)) + 360) % 360


def build_failure_image(session_dir: Path, tick_list: list[int],
                        title: str, primary_label: str,
                        shadow_label: str, out_path: Path,
                        pca: PCAHeading) -> None:
    by_tick = {}
    for line in (session_dir / "trace.jsonl").open():
        r = json.loads(line)
        if r.get("tick") is not None:
            by_tick[r["tick"]] = r

    # Load valid ticks + compute PCA live
    class _F:
        def __init__(self, mm): self._mm = mm
        def minimap(self):     return self._mm
        def full_screen(self): return self._mm

    rows = []
    for t in tick_list:
        r = by_tick.get(t)
        if r is None: continue
        fp = session_dir / f"tick_{t:04d}.png"
        if not fp.exists(): continue
        img = Image.open(fp).convert("RGB")
        # Live PCA
        pca_deg = None
        try:
            h = pca.estimate(_F(img), prior=None)
            if h.confidence > 0: pca_deg = h.bearing_deg
        except Exception:
            pass
        motion_deg = _motion_bearing(by_tick, t)
        rows.append(dict(
            tick=t, img=img, rec=r,
            primary=r.get('cnn_raw_heading_deg'),
            shadow=r.get('shadow_cnn_heading_deg'),
            pca=pca_deg, motion=motion_deg,
            picked=r.get('heading_deg'),
        ))

    # Canvas
    W = MAP_W + MARGIN * 3 + TEXT_W
    H = LEGEND_H + len(rows) * ROW_H + MARGIN * 2
    canvas = Image.new("RGB", (W, H), (25, 25, 30))
    draw = ImageDraw.Draw(canvas)
    font_title = _load_font(14)
    font_hdr = _load_font(12)
    font_txt = _load_font(11)

    # Title + legend
    draw.text((MARGIN, MARGIN), title, fill=(255, 255, 255), font=font_title)
    lx = MARGIN
    ly = MARGIN + 18
    entries = [
        (primary_label + " (primary)", COLORS["v6"]),
        (shadow_label  + " (shadow)",  COLORS["v5"]),
        ("PCA",     COLORS["pca"]),
        ("motion",  COLORS["motion"]),
        ("picked",  COLORS["picked"]),
    ]
    for name, col in entries:
        draw.line([(lx, ly + 5), (lx + 20, ly + 5)], fill=col, width=3)
        draw.text((lx + 24, ly), name, fill=col, font=font_hdr)
        lx += 24 + int(font_hdr.getlength(name)) + 20

    # Rows
    y0 = LEGEND_H
    for i, row in enumerate(rows):
        y = y0 + i * ROW_H + MARGIN
        # Scaled mini-map with arrows
        big = row['img'].resize((MAP_W, MAP_H), Image.NEAREST)
        arrows = {
            "v6":     row['primary'],
            "v5":     row['shadow'],
            "pca":    row['pca'],
            "motion": row['motion'],
            "picked": row['picked'],
        }
        overlaid = _compass_arrows(big, arrows)
        canvas.paste(overlaid, (MARGIN, y))
        # Right-hand text
        tx = MARGIN + MAP_W + MARGIN
        r = row['rec']
        lines = []
        lines.append(f"tick {row['tick']}  lat={r.get('lat', '?'):>6.2f}  "
                     f"lon={r.get('lon', '?'):>6.2f}  speed={r.get('speed_kt','?')} kt")
        def _fmt(v):  return f"{v:6.1f}°" if v is not None else "   n/a"
        # Primary + shadow with confidences
        pc = r.get('heading_conf');    sc = r.get('shadow_cnn_confidence')
        lines.append(f"{primary_label:>8} (primary, conf {pc:.2f}): "
                     f"{_fmt(row['primary'])}"
                     if pc is not None else
                     f"{primary_label:>8} (primary): {_fmt(row['primary'])}")
        lines.append(f"{shadow_label:>8} (shadow, conf {sc:.2f}):  "
                     f"{_fmt(row['shadow'])}"
                     if sc is not None else
                     f"{shadow_label:>8} (shadow):  {_fmt(row['shadow'])}")
        lines.append(f"    PCA               : {_fmt(row['pca'])}")
        lines.append(f"    motion  (prev→curr): {_fmt(row['motion'])}")
        lines.append(f"    picked (final out): {_fmt(row['picked'])}")
        src = r.get('heading_source', '') or ''
        tags = [t for t in src.split("+")
                if t and t != "ship_only_cnn"
                and not t.startswith("cnn_shadow_disagree")]
        lines.append(f"    corrections fired : "
                     f"{', '.join(tags) if tags else 'none'}")
        # Compute error vs motion when available
        def _ang(a, b):
            if a is None or b is None: return None
            return abs(((a-b+540)%360)-180)
        if row['motion'] is not None:
            errs = []
            for k, val in [("v6", row['primary']), ("v5", row['shadow']),
                           ("pca", row['pca']), ("picked", row['picked'])]:
                e = _ang(val, row['motion'])
                if e is not None: errs.append(f"{k}={e:.0f}°")
            lines.append(f"    errs vs motion    : {', '.join(errs)}")

        for j, line in enumerate(lines):
            draw.text((tx, y + j * 15), line, fill=(230, 230, 235),
                      font=font_txt)

        # Separator
        draw.line([(MARGIN, y + ROW_H - MARGIN),
                   (W - MARGIN, y + ROW_H - MARGIN)],
                  fill=(60, 60, 70), width=1)

    canvas.save(out_path)
    print(f"wrote {out_path}  ({len(rows)} rows)")


# Voyage 4 — v6 primary, v5 shadow.
# Failure ticks from the "genuinely suspicious, excluding lake" filter.
VOYAGE_4_TICKS = [5, 107, 108, 304, 309, 361, 362, 443, 445, 446]

# Voyage 3 — v5 primary, v6 shadow.  The union of biggest-flip disagreements
# (Δ >= 90°) that we discussed earlier — a curated set showcasing v5's
# failures where v6 was often right.
VOYAGE_3_TICKS = [107, 293, 294, 296, 338, 348, 351, 354, 355, 365, 375, 388,
                  499, 528, 544, 548, 566, 584]


def main():
    pca = PCAHeading()

    build_failure_image(
        session_dir=REPO / "data/sessions/ai_nav_2026-07-01T23-51-38",
        tick_list=VOYAGE_4_TICKS,
        title="Voyage 4 — v6 primary, v5 shadow — suspicious ticks (lat >= 5°)",
        primary_label="v6",
        shadow_label="v5",
        out_path=REPO / "data/sessions/ai_nav_2026-07-01T23-51-38/"
                        "failure_cases_v6_primary.png",
        pca=pca,
    )

    build_failure_image(
        session_dir=REPO / "data/sessions/ai_nav_2026-07-01T23-06-57",
        tick_list=VOYAGE_3_TICKS,
        title="Voyage 3 — v5 primary, v6 shadow — v5 failures (curated)",
        primary_label="v5",
        shadow_label="v6",
        out_path=REPO / "data/sessions/ai_nav_2026-07-01T23-06-57/"
                        "failure_cases_v5_primary.png",
        pca=pca,
    )


if __name__ == "__main__":
    main()
