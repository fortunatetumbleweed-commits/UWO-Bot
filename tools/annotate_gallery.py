"""Regenerate annotated regression-gallery images.

For each (session, tick) in `CASES`, load the raw mini-map crop from
`data/sessions/<session>/tick_XXXX.png`, run the edge-anchored
centerline pipeline (find_openings + Dijkstra along `1/(DT+1)`), and
overlay:

  - Water mask (subtle blue tint)
  - Frame-edge opening candidates: yellow squares, labeled `<side><span>`
  - Ship position: lime circle at frame center
  - Picked tactical destination (from trace.jsonl `tactical_dest_px_offset`):
    magenta X
  - Centerline from ship → picked dest: white polyline
  - Header bar: tick number, commit_reason, commit bearing, n_openings

Output → `docs/tactical_regression_gallery/images/<session>_tXXXX.png`
(overwrites in place; images are force-added to git despite `*.png`
in .gitignore).

Usage:

  python -m tools.annotate_gallery

Add / remove / edit cases by editing `CASES` below.

Cases are also the source of truth for
`docs/tactical_regression_gallery/index.html` — if you change one,
update the other (a future improvement would generate the HTML too).
"""
from __future__ import annotations
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import distance_transform_edt, label as cc_label

from tools.edge_centerline_prototype import (
    build_mask,
    compute_centerline,
    find_openings,
    snap_to_water,
)

log = logging.getLogger(__name__)


# ── Cases ──────────────────────────────────────────────────────────────
# One entry per regression-gallery tick.  `mission_dest` is the
# ultimate destination the mission was pursuing at that tick — used to
# compute a live dynamic goal bearing so the annotation reflects the
# CURRENT picker's decision, not whatever was recorded in the trace
# (which used a fixed --commit-bearing).
#
# Extend as new voyages surface new interesting frames.
CASES: list[dict] = [
    {"session": "ai_nav_2026-07-24T16-22-17", "tick": 250,
     "mission_dest": (5.71, 32.12), "note": "P1 Khartoum bend (positive)"},
    {"session": "ai_nav_2026-07-24T16-22-17", "tick": 390,
     "mission_dest": (5.71, 32.12), "note": "P2 Y-fork hug dead-zone"},
    {"session": "ai_nav_2026-07-24T16-22-17", "tick": 475,
     "mission_dest": (5.71, 32.12), "note": "P3 past-Y-tip continuation (positive)"},
    {"session": "ai_nav_2026-07-24T16-22-17", "tick": 674,
     "mission_dest": (5.71, 32.12), "note": "P4 SEARCH tracker premature"},
    {"session": "ai_nav_2026-07-24T16-22-17", "tick": 825,
     "mission_dest": (5.71, 32.12), "note": "P5 closest approach (positive)"},
    {"session": "ai_nav_2026-07-24T16-22-17", "tick": 868,
     "mission_dest": (5.71, 32.12), "note": "P6 legit reclassif (positive)"},
    {"session": "ai_nav_2026-07-24T16-22-17", "tick": 871,
     "mission_dest": (5.71, 32.12), "note": "P7 wrong branch after refresh"},
    {"session": "ai_nav_2026-07-24T20-37-02", "tick": 118,
     "mission_dest": (5.71, 32.12), "note": "P8 frame_shift low-conf spike"},
    {"session": "ai_nav_2026-07-24T22-01-22", "tick": 395,
     "mission_dest": (5.71, 32.12), "note": "P9 hug-left uses body-frame not bank topology"},
    {"session": "ai_nav_2026-07-24T23-57-57", "tick": 139,
     "mission_dest": (5.71, 32.12), "note": "P10 bank trace walks wrong direction from projection"},
    {"session": "ai_nav_2026-07-25T11-01-09", "tick": 391,
     "mission_dest": (5.71, 32.12), "note": "P11 dead-end curl-back puts wp near ship"},
    {"session": "ai_nav_2026-07-25T11-01-09", "tick": 445,
     "mission_dest": (5.71, 32.12), "note": "P12 SEARCH tracker corner-sticky hides new bank exit"},
    {"session": "ai_nav_2026-07-25T11-01-09", "tick": 573,
     "mission_dest": (5.71, 32.12), "note": "P13 hairpin-bend flip: dest jumps top→bottom on approach"},
    {"session": "ai_nav_2026-07-25T13-09-55", "tick": 212,
     "mission_dest": (5.71, 32.12), "note": "P14 anchored edge flipped to left; should switch to right"},
    {"session": "ai_nav_2026-07-25T13-09-55", "tick": 373,
     "mission_dest": (5.71, 32.12), "note": "P15 bottom edge has 2 exits; picked wrong one, flipped to top"},
    {"session": "ai_nav_2026-07-25T15-54-02", "tick": 206,
     "mission_dest": (5.71, 32.12), "note": "P16 tracker axis-only distance gate admits far cross-edge midpoint"},
    {"session": "ai_nav_2026-07-25T15-54-02", "tick": 401,
     "mission_dest": (5.71, 32.12), "note": "P17 corner-split fires on far corner + post-bounce approach → flip"},
]

REPO_ROOT   = Path(__file__).resolve().parent.parent
SESSION_DIR = REPO_ROOT / "data" / "sessions"
GALLERY_DIR = REPO_ROOT / "docs" / "tactical_regression_gallery" / "images"

# Render scale for readability.  2× keeps file size modest and text
# legible at typical browser zoom.
SCALE = 2


# ── Rendering ──────────────────────────────────────────────────────────


def _restrict_to_ship_cc(mask: np.ndarray) -> np.ndarray:
    """Return the ship-CC-only mask (matches production tactical)."""
    H, W = mask.shape
    sr, sc = H // 2, W // 2
    lbl, _ = cc_label(mask)
    scc = lbl[sr, sc] if mask[sr, sc] else 0
    if scc == 0:
        sizes = np.bincount(lbl.ravel())
        sizes[0] = 0
        scc = int(np.argmax(sizes)) if sizes.max() > 0 else 0
    return (lbl == scc) if scc else mask


def _load_trace(session: str) -> dict[int, dict]:
    trace_path = SESSION_DIR / session / "trace.jsonl"
    if not trace_path.exists():
        return {}
    out: dict[int, dict] = {}
    for line in trace_path.open():
        d = json.loads(line)
        t = d.get("tick")
        if t is not None:
            out[t] = d
    return out


def _bearing_ship_to(lat1: float, lon1: float,
                     lat2: float, lon2: float) -> float:
    import math
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlam))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _opening_bearing(o: dict, sr: int, sc: int) -> float:
    import math
    dy = o["y"] - sr
    dx = o["x"] - sc
    return (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0


def _pick_opening(openings: list[dict], approach_bearing: float,
                  sr: int, sc: int, hug_side: str = "port") -> dict:
    """Reimplements the tactical wall-follower picker — keep in sync
    with LookaheadTactical._pick_new_dest.

    Rule: bucket openings by body-frame bearing (LEFT/STRAIGHT/RIGHT/
    REVERSE); for port hug, priority LEFT > STRAIGHT > RIGHT > REVERSE.
    Within a bucket, prefer the opening closest to the bucket's ideal
    angle (-90°/0°/+90°); width tie-breaks.  This approximates
    "follow the left bank forward, first opening reached."
    """
    def _body_frame(brg):
        return ((brg - approach_bearing + 540.0) % 360.0) - 180.0

    def _bucket(body):
        ab = abs(body)
        if ab <= 45.0:  return "STRAIGHT"
        if ab <= 135.0: return "LEFT" if body < 0 else "RIGHT"
        return "REVERSE"

    IDEAL = {"LEFT": -90.0, "STRAIGHT": 0.0, "RIGHT": +90.0, "REVERSE": 180.0}
    if hug_side == "port":
        ORDER = {"LEFT": 0, "STRAIGHT": 1, "RIGHT": 2, "REVERSE": 3}
    else:
        ORDER = {"RIGHT": 0, "STRAIGHT": 1, "LEFT": 2, "REVERSE": 3}

    def _key(o):
        body = _body_frame(_opening_bearing(o, sr, sc))
        bucket = _bucket(body)
        dev = 180.0 - abs(body) if bucket == "REVERSE" \
              else abs(body - IDEAL[bucket])
        return (ORDER[bucket], dev, -o["span"])

    return sorted(openings, key=_key)[0]


def annotate_tick(session: str, tick: int,
                  mission_dest: Optional[tuple[float, float]] = None,
                  hug_side: str = "port") -> Optional[Path]:
    """Annotate one case.  Runs the CURRENT tactical picker (dynamic
    goal bearing from ship → mission_dest) rather than reading the
    historical trace's dest — so the annotation reflects post-fix
    behaviour, not the frozen decisions of the original voyage.
    Returns output path or None on missing input."""
    raw_path = SESSION_DIR / session / f"tick_{tick:04d}.png"
    if not raw_path.exists():
        log.warning("missing raw tick: %s", raw_path)
        return None

    img = np.asarray(Image.open(raw_path).convert("RGB"))
    H, W, _ = img.shape
    sr, sc = H // 2, W // 2

    # Run the production mask pipeline + restrict to ship's CC.
    mask_full, _ = build_mask(img)
    mask = _restrict_to_ship_cc(mask_full)
    dt = distance_transform_edt(mask)
    openings = find_openings(mask)

    # Look up the tick's state from the session trace (ship lat/lon,
    # heading, historical dest for the "OLD" reference marker).
    trace = _load_trace(session)
    d = trace.get(tick, {})
    ship_hdg = d.get("heading_deg") or 180.0
    old_tact_off = d.get("tactical_dest_px_offset")

    # Approach direction — prefer per-tick vision signal
    # (shift_motion_bearing) then motion → heading → last commit.
    #
    # Post-fix simulation (pipeline.py d1d08e1 tightened the ingest
    # gate for frame_shift): if the recorded shift_motion diverges
    # from motion/heading by more than 90°, treat it as a spike and
    # skip it (approximating the effect of the tightened gate).
    shift_mot = d.get("shift_motion_bearing_deg")
    motion    = d.get("motion_bearing_deg")
    heading   = d.get("heading_deg")

    def _ang_diff(a, b):
        return abs(((a - b + 540.0) % 360.0) - 180.0)

    if shift_mot is not None:
        # Reference bearings for spike detection — the stable
        # perception signals that don't share a failure mode with
        # frame_shift (motion is from OCR lat/lon, heading is CNN).
        ref = motion if motion is not None else heading
        if ref is not None and _ang_diff(shift_mot, ref) > 90.0:
            shift_mot = None   # discard the spike

    approach_bearing = (
        shift_mot
        or motion
        or heading
        or d.get("commit_deg")
        or 180.0
    )

    # Compute the CURRENT picker's choice with the "first reached" rule.
    picked_yx: Optional[tuple[int, int]] = None
    polyline: Optional[list[tuple[int, int]]] = None
    if openings:
        picked_opening = _pick_opening(
            openings, approach_bearing, sr, sc, hug_side=hug_side,
        )
        picked_yx = (int(picked_opening["y"]), int(picked_opening["x"]))
        ship_snap = snap_to_water(mask, sr, sc)
        pick_snap = snap_to_water(mask, *picked_yx)
        polyline = compute_centerline(mask, dt, ship_snap, pick_snap)

    # Render — 2× upscale so labels and lines are legible.
    overlay = img.astype(np.int16).copy()
    overlay[mask] = (overlay[mask] * 0.55
                     + np.array([0, 60, 160]) * 0.45).astype(np.int16)
    im = Image.fromarray(np.clip(overlay, 0, 255).astype(np.uint8))
    im = im.resize((W * SCALE, H * SCALE), Image.NEAREST)
    draw = ImageDraw.Draw(im)

    # Centerline (white polyline)
    if polyline:
        for i in range(len(polyline) - 1):
            y0, x0 = polyline[i]
            y1, x1 = polyline[i + 1]
            draw.line(
                [x0 * SCALE, y0 * SCALE, x1 * SCALE, y1 * SCALE],
                fill=(255, 255, 255), width=2,
            )

    # Opening candidates (yellow squares labeled <side><span>)
    for o in openings:
        py, px = o["y"] * SCALE, o["x"] * SCALE
        draw.rectangle([px - 7, py - 7, px + 7, py + 7],
                       outline=(255, 220, 0), width=2)
        draw.text((px + 9, py - 9),
                  f"{o['side'][0]}{o['span']}",
                  fill=(255, 220, 0))

    # Ship (lime circle)
    sy, sx = sr * SCALE, sc * SCALE
    draw.ellipse([sx - 8, sy - 8, sx + 8, sy + 8],
                 outline=(0, 255, 0), width=2)

    # OLD picker's choice (from trace) — grey X for reference
    if old_tact_off is not None:
        oy, ox = (sr + int(old_tact_off[0])) * SCALE, \
                 (sc + int(old_tact_off[1])) * SCALE
        draw.line([ox - 8, oy - 8, ox + 8, oy + 8],
                  fill=(140, 140, 140), width=2)
        draw.line([ox - 8, oy + 8, ox + 8, oy - 8],
                  fill=(140, 140, 140), width=2)

    # NEW picker's choice (current post-fix picker) — magenta X
    if picked_yx:
        py, px = int(picked_yx[0]) * SCALE, int(picked_yx[1]) * SCALE
        draw.line([px - 10, py - 10, px + 10, py + 10],
                  fill=(255, 0, 255), width=3)
        draw.line([px - 10, py + 10, px + 10, py - 10],
                  fill=(255, 0, 255), width=3)

    # Header bar
    draw.rectangle([0, 0, W * SCALE, 18], fill=(30, 30, 30))
    header = (f"t={tick}  n_openings={len(openings)}  "
              f"approach={approach_bearing:.0f}°  "
              f"grey=OLD pick  magenta=NEW pick (first reached)")
    draw.text((5, 3), header, fill=(240, 240, 240))

    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GALLERY_DIR / f"{session}_t{tick:04d}.png"
    im.save(out_path)
    return out_path


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    ok = missing = 0
    for case in CASES:
        session = case["session"]
        tick = case["tick"]
        try:
            out = annotate_tick(session, tick,
                                mission_dest=case.get("mission_dest"),
                                hug_side=case.get("hug_side", "port"))
        except Exception as e:
            log.error("failed to annotate %s t%d: %s", session, tick, e)
            missing += 1
            continue
        if out is None:
            missing += 1
            continue
        log.info(
            "  ok  %s t%d [%s] → %s",
            session, tick, case.get("note", ""),
            out.relative_to(REPO_ROOT),
        )
        ok += 1
    log.info("annotated %d/%d cases (%d missing / failed)",
             ok, ok + missing, missing)
    return 0 if missing == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
