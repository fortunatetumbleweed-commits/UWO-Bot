"""Annotate hug-shore debug crops with sector boundaries, virtual wall
position, and chosen direction — for offline analysis of live runs.

Usage:
    python tools/annotate_hug_shore_trace.py data/sessions/hug_debug_<TS>/

Reads trace.jsonl + tick_NNNN.png crops, writes tick_NNNN_annotated.png
side-by-side.  Each annotation shows:

  * the crop, 4× upscaled for readability
  * 8 radial sector boundaries rotated by the ship heading
  * the virtual-wall arc on the opposite-hug side at wall_distance ×
    view_radius (red if wall cost is active that tick, gray if
    suppressed)
  * per-sector cost text labels (current, no-ideal, heading-smooth)
  * an arrow indicating the chosen sector

The "virtual wall" overlay is the imaginary boundary the cost function
treats as a wall on the opposite-hug side — useful for sanity-checking
whether `wall_distance` is set sensibly relative to where actual shore
sits in the mini-map.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# Crop is 381x184; in the new centered crop the ship sits near the
# geometric centre.  view_radius is roughly half the crop height —
# matches the mini-map disc radius.
SHIP_XY        = (190, 92)
VIEW_RADIUS_PX = 92
UPSCALE        = 4
SECTOR_DEGS    = 45.0    # 8 sectors


def _compass_to_pil_angle(compass_deg: float) -> float:
    """PIL.ImageDraw uses east=0° increasing clockwise.  Our compass is
    north=0° increasing clockwise.  Conversion: subtract 90."""
    return (compass_deg - 90.0) % 360.0


def _rotate_point(cx: int, cy: int, dist: float, compass_deg: float) -> tuple[int, int]:
    """Return (x, y) at `dist` pixels from (cx, cy) in the compass-`deg`
    direction.  +y is down in image coords."""
    rad = math.radians(compass_deg)
    x = cx + dist * math.sin(rad)
    y = cy - dist * math.cos(rad)
    return int(x), int(y)


def annotate_tick(crop_path: Path, record: dict, out_path: Path) -> None:
    img = Image.open(crop_path).convert("RGBA")
    # Upscale for legibility
    big = img.resize((img.size[0] * UPSCALE, img.size[1] * UPSCALE), Image.NEAREST)
    overlay = Image.new("RGBA", big.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")

    cx = SHIP_XY[0] * UPSCALE
    cy = SHIP_XY[1] * UPSCALE
    R  = VIEW_RADIUS_PX * UPSCALE
    heading = record.get("heading_deg") or 0.0
    side = record.get("side") or "starboard"
    wall_d = record.get("wall_distance") or 0.20
    note = record.get("note") or ""

    # ── outer disc + ship-centre dot ────────────────────────────────────────
    draw.ellipse((cx - R, cy - R, cx + R, cy + R), outline=(255, 255, 255, 255), width=3)
    draw.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), fill=(0, 255, 0, 255))

    # ── 8 sector boundaries (radials) rotated by ship heading ───────────────
    # Sector i is ship-relative angle i*45°; bin edges at ±22.5°.
    for i in range(8):
        rel_edge = i * SECTOR_DEGS - SECTOR_DEGS / 2  # ship-relative
        compass = (heading + rel_edge) % 360
        x2, y2 = _rotate_point(cx, cy, R, compass)
        draw.line((cx, cy, x2, y2), fill=(180, 180, 180, 255), width=2)

    # ── heading arrow (current ship orientation) ────────────────────────────
    bx, by = _rotate_point(cx, cy, R * 0.45, heading)
    draw.line((cx, cy, bx, by), fill=(0, 255, 0, 220), width=4)

    # ── virtual wall: pie slice on opposite-hug side ────────────────────────
    # For stbd hug: wall covers sectors 6 (port-beam, -90°) and 7
    # (bow-port, -45°), i.e. ship-relative arc -112.5° to -22.5°.
    if side == "starboard":
        rel_start, rel_end = -112.5, -22.5
    else:
        rel_start, rel_end = 22.5, 112.5
    wall_r = int(wall_d * R)
    wall_active = ("wd=" in note) and ("*" not in note.split("wd=")[1].split()[0])
    bbox = (cx - wall_r, cy - wall_r, cx + wall_r, cy + wall_r)
    pil_start = _compass_to_pil_angle(heading + rel_start)
    pil_end   = _compass_to_pil_angle(heading + rel_end)
    if pil_end < pil_start:
        pil_end += 360

    # Filled pie slice — the "wall zone" where the cost function adds
    # virtual-wall cost on the opposite-hug side, out to wall_distance.
    fill_col   = (255, 60, 60, 80)  if wall_active else (130, 130, 130, 60)
    edge_col   = (255, 40, 40, 255) if wall_active else (120, 120, 120, 220)
    draw.pieslice(bbox, pil_start, pil_end, fill=fill_col, outline=edge_col, width=4)

    # Full reference ring at wall_distance — shows the "ideal corridor width"
    # the policy wants to maintain (visible all the way around).
    draw.ellipse(bbox, outline=(255, 200, 50, 200), width=2)

    # Label on the opposite-hug side, outside the wall radius.
    mid_compass = (heading + (rel_start + rel_end) / 2) % 360
    lx, ly = _rotate_point(cx, cy, wall_r + 30, mid_compass)
    status = "ACTIVE" if wall_active else "suppressed"
    draw.text((lx - 55, ly - 6),
              f"WALL d={wall_d:.2f} {status}",
              fill=(255, 80, 80, 255) if wall_active else (180, 180, 180, 255))

    # ── per-sector cost labels for the 5 candidates ─────────────────────────
    CANDIDATES = (6, 7, 0, 1, 2)
    nav = record.get("nav") or {}
    alt = record.get("alt_costs") or {}
    chosen = None
    try:
        # Recover chosen sector index from the "VFH+ best=N" prefix in note.
        if "best=" in note:
            chosen = int(note.split("best=")[1].split()[0])
    except Exception:
        chosen = None

    # Parse current costs from "[6=0.52 7=0.43 0=0.30 1=0.31 2=0.21]" in note.
    cur_costs: dict[int, float] = {}
    if "[" in note and "]" in note:
        body = note.split("[")[-1].split("]")[0]
        for tok in body.split():
            if "=" in tok:
                k, v = tok.split("=")
                try:
                    cur_costs[int(k)] = float(v)
                except ValueError:
                    pass

    for i in CANDIDATES:
        rel = i * SECTOR_DEGS
        if rel > 180:
            rel -= 360
        compass = (heading + rel) % 360
        lx, ly = _rotate_point(cx, cy, R * 0.85, compass)
        cc = cur_costs.get(i)
        nc = (alt.get("no_ideal") or {}).get(str(i))
        hc = (alt.get("heading_smooth") or {}).get(str(i))
        text_lines = []
        text_lines.append(f"s{i}")
        if cc is not None:    text_lines.append(f"cur={cc:.2f}")
        if nc is not None:    text_lines.append(f"noid={nc:.2f}")
        if hc is not None:    text_lines.append(f"hs={hc:.2f}")
        text = "\n".join(text_lines)
        fill = (255, 255, 0, 255) if i == chosen else (220, 220, 220, 230)
        draw.text((lx - 35, ly - 28), text, fill=fill)

    # ── header text ─────────────────────────────────────────────────────────
    header = (
        f"tick {record.get('tick')}  hdg={heading:.0f}°  phase={record.get('phase')}  "
        f"side={side}  wall_d={wall_d:.2f}\n"
        f"action: {record.get('action')}\n"
        f"alt_best (no_ideal / heading_smooth): "
        f"{(record.get('alt_best') or {}).get('no_ideal')} / "
        f"{(record.get('alt_best') or {}).get('heading_smooth')}"
    )
    draw.text((10, 10), header, fill=(255, 255, 255, 255))

    composed = Image.alpha_composite(big, overlay).convert("RGB")
    composed.save(out_path)


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python tools/annotate_hug_shore_trace.py <session_dir>",
              file=sys.stderr)
        sys.exit(2)
    session = Path(sys.argv[1])
    trace = session / "trace.jsonl"
    if not trace.exists():
        print(f"no trace.jsonl in {session}", file=sys.stderr)
        sys.exit(2)
    records = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
    out_dir = session / "annotated"
    out_dir.mkdir(exist_ok=True)
    n = 0
    for r in records:
        crop_name = r.get("crop")
        if not crop_name:
            continue
        crop_path = session / crop_name
        if not crop_path.exists():
            continue
        out_path = out_dir / crop_name.replace(".png", "_ann.png")
        try:
            annotate_tick(crop_path, r, out_path)
            n += 1
        except Exception as e:
            print(f"  tick {r.get('tick')}: skip ({e})", file=sys.stderr)
    print(f"wrote {n} annotated images to {out_dir}/")


if __name__ == "__main__":
    main()
