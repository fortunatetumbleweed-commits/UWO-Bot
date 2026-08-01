"""Interactive tick browser for hug_shore voyage sessions.

Shows the saved mini-map crop for each tick alongside the matching
trace.jsonl record (position, heading, sectors, avoider diagnostics,
selector regime, etc.) so you can scroll through a voyage and see
what the bot saw + decided at each step.

Built on matplotlib (no tkinter dependency), so it works with any
Python build that has matplotlib.

Visual overlays on the mini-map:
  cyan arrow    = PICKED heading (heading_deg — the model's read OR the
                  motion bearing after correction; tagged with its source)
  yellow arrow  = heading model's RAW read (CNN raw for old traces; the
                  U-Net re-run live for current traces) — compare vs cyan
  magenta arrow = PCA heading (re-run live)
  green arrow   = ship → destination point (the wp's actual lat/lon,
                  projected to the minimap via MINIMAP_PIX_PER_DEG_*).
                  Dot on wp when it sits inside the minimap crop; an
                  arrow-tip marker (`<` / `>`) at the clipped edge
                  when the wp is off-screen.  Stale wp = stationary
                  dot the ship icon drifts past.
  yellow dashed = M-line direction (bearing to mission endpoint, used
                  by the progress monitor; only drawn when endpoint set)
  orange arrow  = commanded turn direction (bow + cmd°), if not "hold"

The text panel above the world-view shows, when capture-mode key
recordings exist for the session, the line `your input: 'a':1023ms
(total 1023ms)`, listing all physical key holds that overlap the
current tick's wall_iso window.  `↳` prefix means the hold started
in an earlier tick and is still continuing into this one.
The mini-map crop is rendered north-up, so compass bearings map
directly to direction on the image.

Below the mini-map: world-view pane in honest lat/lon coordinates.
  red triangle  = voyage start
  cyan polyline = trajectory so far
  cyan dot      = current position
  green dot     = waypoint at its actual (lat, lon) — world-anchored.
                  Frozen wp is visible: dot stays put while ship moves
                  past it.  Compare with the minimap green dot above,
                  which is bearing-only.
  green dotted  = link from current position to waypoint
  red square    = branch-end mark (user-annotated turn-around point /
                  Trémaux leaf).  Press `m` on a tick to record; `M`
                  to remove the most recent.  Saved to ends.jsonl in
                  the session dir.
  green/yellow/
  red dot       = per-tick verdict on the ground-truth correct action
                  for this tick (NOT "what happened" — what SHOULD
                  happen).  Use the human input + bot's intent shown
                  in the text panel to decide.
                    1 = ideal       (green)  — correct action
                    2 = acceptable  (yellow) — workable, not best
                    3 = wrong       (red)    — neither human nor bot
                                              should do this here
                    0 = clear
                  Stored in labels.jsonl.  Replay diff will check the
                  bot's recomputed action against this ground truth.
  yellow star   = endpoint (only when set)
  yellow dashed = M-line from start to endpoint
  red x         = OCR spike (sample whose per-tick displacement is
                  implausible; excluded from the trajectory line)
Auto-scales to fit trajectory + endpoint + margin (spikes excluded
from bounds so a bad lon=61 read doesn't blow out the axis range).
Equal-aspect.

Usage:
  python -m tools.tick_viewer data/sessions/hug_debug_20260601_114709
  python -m tools.tick_viewer <session_dir> --start-tick 320

Keys:
  ←  /  →     previous / next tick
  Space       toggle auto-play
  +  /  -     auto-play speed (faster / slower)
  Home / End  first / last tick
  r           jump to next §13.28 heading-rejection tick (and print
              one-line summary to stdout)
  R           print FULL rejection summary table to stdout
              (count, ticks, raw vs baseline, strategy, reason)
  b           toggle production bank overlay (magenta)
  v           toggle V10-prototype bank overlay (yellow)
  w           toggle walked-path overlay (orange) — recomputes the
              V11 → skeleton → walker on the current frame
  s           save current view as PNG (to /tmp by default)
  o           open another session folder
  q           quit
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Optional

import matplotlib

# Use an interactive backend that supports key events on macOS.
try:
    matplotlib.use("MacOSX")
except (ImportError, ValueError):
    pass

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.widgets import Button  # noqa: E402
from matplotlib.animation import FuncAnimation  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

# Bank-line overlay uses the bot's production minimap perception so
# the viewer sees exactly what the bot sees.  Import lazily — only
# needed when the overlay is on (defaults on, toggle with `b`).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vision.minimap_navigation_view import (  # noqa: E402
    read_navigation_view as _read_navigation_view,
    MINIMAP_CROP as _MINIMAP_CROP,
)


DEFAULT_AUTOPLAY_MS = 500

# Mini-map disc center (approximate ship origin when the green
# centroid is unknown).  Matches DISC_FALLBACK_CENTER in
# vision/minimap_navigation_view.py.  These crops are 381×184.
SHIP_DEFAULT_CENTER = (190, 92)
WAYPOINT_ARROW_LEN_PX = 60
CMD_ARROW_LEN_PX = 40
HEADING_ARROW_LEN_PX = 50

# Mini-map degrees-per-pixel scale.  Calibrated empirically from
# session explore_port_20260604_180018 by phase-correlating tick_0001
# minimap against tick_0006 minimap (pixel shift = (-42, +13)) against
# the HUD Δlat/lon read off the on-screen text (Δlat = +0.110°, Δlon =
# +0.400° between t1 and t6).  At lat 30°:
#   pix/deg lat = 13 / 0.110 = 118.2
#   pix/deg lon = 42 / 0.400 = 105.0
# Averaged across t1→t5 and t1→t6 pairs and rounded to the values
# below.  A future calibration tool should re-derive these for the
# game's current minimap zoom; the present values are consistent
# across the eight pairs I tested but are pinned to one voyage.
MINIMAP_PIX_PER_DEG_LAT = 117.0
MINIMAP_PIX_PER_DEG_LON = 105.0


# ── V10 prototype bank line (alternative to production perception) ──
#
# The "V10" channel-extraction pipeline iterated during 2026-06-08/09
# in /tmp/.  It uses K-means + colour rules + morphology + connected-
# component channel extraction, and does NOT punch the
# ship/sonar holes that the production pipeline punches.  The user
# wants this drawn alongside the production bank as a diagnostic
# comparison — particularly useful when the production skeleton tangent
# misbehaves and the question is whether V10's mask topology would have
# been better.  Toggle with `v`.
def _v10_water_mask(img_rgb_np) -> Optional[np.ndarray]:
    """Run the V10 channel-extraction pipeline on a 381×184 minimap
    crop and return the binary water mask.  Returns None on failure or
    if cv2 / scipy / skimage isn't available."""
    try:
        import cv2
        from scipy.ndimage import (
            binary_fill_holes, binary_dilation, binary_erosion,
            label as cc_label,
        )
        from skimage.morphology import binary_opening, disk
    except ImportError:
        return None
    try:
        img = img_rgb_np
        R, G, B = (img[..., c].astype(np.int16) for c in range(3))
        # Confident sprite mask (yellow / red / green / white) — used
        # only to exclude from K-means fit and for the rescue step.
        yellow = (R > 180) & (G > 150) & (B < 130)
        green_  = (G > 140) & (G - R > 30) & (G - B > 30)
        white  = (R > 235) & (G > 235) & (B > 235)
        red    = (R > 180) & (R - G > 40) & (R - B > 40)
        conf = yellow | green_ | white | red
        conf_dil = binary_dilation(conf, iterations=2)
        # Text-like (light-gray uniform pixels in lower 2/3) and a
        # thin-filter pass for cv2.inpaint.
        lum = (R + G + B) // 3
        uniform = ((np.abs(R - G) < 18) & (np.abs(G - B) < 18)
                   & (np.abs(R - B) < 18))
        text = uniform & (lum > 195) & (lum < 240)
        H = img.shape[0]
        band = np.zeros_like(text); band[H // 3:, :] = True
        txt_full = text & band
        txt_thin = txt_full & ~binary_erosion(txt_full, iterations=2)
        # Step 1: inpaint text-only (sprites stay as a barrier so
        # water colour doesn't bleed through them into adjacent text).
        if txt_thin.any():
            bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            bgr = cv2.inpaint(bgr, txt_thin.astype(np.uint8), 3,
                              cv2.INPAINT_TELEA)
            cleaned = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        else:
            cleaned = img.copy()
        # Step 2: K-means K=5 on bilateral-filtered cleaned, fit
        # excluding original sprite + text masks.
        exclude = conf_dil | txt_full
        bgr = cv2.cvtColor(cleaned, cv2.COLOR_RGB2BGR)
        blur = cv2.cvtColor(cv2.bilateralFilter(bgr, 7, 40, 10),
                            cv2.COLOR_BGR2RGB)
        Z = blur[~exclude].reshape(-1, 3).astype(np.float32)
        if Z.shape[0] < 5:
            return None
        crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
                25, 0.5)
        _, _, centers = cv2.kmeans(Z, 5, None, crit, 3,
                                   cv2.KMEANS_PP_CENTERS)
        all_p = blur.reshape(-1, 3).astype(np.float32)
        d = np.linalg.norm(all_p[:, None, :] - centers[None, :, :],
                           axis=-1)
        labels_arr = np.argmin(d, axis=1).reshape(img.shape[0],
                                                  img.shape[1])
        cu8 = centers.astype(np.uint8)
        # is_water_center: dark + blue-dominant
        wc = [j for j, c in enumerate(cu8)
              if (int(c[0]) + int(c[1]) + int(c[2])) // 3 < 140
              and int(c[2]) > int(c[0]) + 10
              and int(c[2]) >= int(c[1]) - 5]
        water = np.isin(labels_arr, wc)
        # Step 3: rescue confident sprites that landed in water.
        conf_in_water = conf & binary_dilation(water, iterations=3)
        water = water | conf_in_water
        m = binary_fill_holes(water)
        m = binary_dilation(m, iterations=2)
        # Padded opening to avoid edge erosion.
        P = 6
        mp = np.pad(m, P, mode="edge")
        mp = binary_opening(mp, disk(5))
        m_v9 = mp[P:-P, P:-P]
        # Step 4: channel extraction — engulf small not-water CCs
        # (< 400 px), then keep only the largest water CC.
        nw_labels, n_nw = cc_label(~m_v9)
        nw_sizes = np.bincount(nw_labels.ravel())
        nw_sizes[0] = 0
        largest_nw = int(np.argmax(nw_sizes))
        out = m_v9.copy()
        for lab in range(1, n_nw + 1):
            if lab == largest_nw:
                continue
            if nw_sizes[lab] < 400:
                out[nw_labels == lab] = True
        w_labels, n_w = cc_label(out)
        w_sizes = np.bincount(w_labels.ravel())
        w_sizes[0] = 0
        if n_w == 0:
            return out
        return (w_labels == int(np.argmax(w_sizes)))
    except Exception as e:  # noqa: BLE001
        print(f"[viewer] V10 bank computation failed: {e}")
        return None


def _v10_bank_pixels_from_minimap_crop(img_rgb_np) -> Optional[tuple]:
    """V10-pipeline bank line for the same 381×184 minimap crop.
    Returns (xs, ys) of boundary pixels in crop coordinates, or None
    on failure.  Parallel to the production helper above."""
    h, w = img_rgb_np.shape[:2]
    if (w, h) != (381, 184):
        return None
    wm = _v10_water_mask(img_rgb_np)
    if wm is None:
        return None
    bank = np.zeros_like(wm)
    bank[1:-1, 1:-1] = wm[1:-1, 1:-1] & (
        ~wm[:-2, 1:-1] | ~wm[2:, 1:-1] |
        ~wm[1:-1, :-2] | ~wm[1:-1, 2:]
    )
    ys, xs = np.where(bank)
    return xs, ys


def _bank_pixels_from_minimap_crop(img_rgb_np) -> Optional[tuple]:
    """Compute the bank line the production bot sees for this minimap.

    Wraps the 381×184 minimap crop in a 2400×1080 canvas so
    `read_navigation_view`'s `MINIMAP_CROP` extracts the correct region,
    then returns the (xs, ys) of `water_mask`'s boundary pixels in the
    minimap-crop coordinate frame.  Identical pipeline to what
    `HugShoreGoal` consumes at runtime, so what's drawn here is exactly
    what perception believes.

    Returns None on any failure (e.g. perception couldn't compute a
    water_mask — happens at full-frame anomalies, port frames, etc.).
    """
    try:
        h, w = img_rgb_np.shape[:2]
        if (w, h) != (381, 184):
            return None
        canvas = np.zeros((1080, 2400, 3), dtype=np.uint8)
        l, t, r, b = _MINIMAP_CROP
        canvas[t:b, l:r] = img_rgb_np
        view = _read_navigation_view(Image.fromarray(canvas))
        wm = view.water_mask
        if wm is None:
            return None
        # Bank = water/land boundary.  Use a 4-neighbour erosion:
        # a water pixel is on the bank iff any 4-neighbour is land.
        bank = np.zeros_like(wm)
        bank[1:-1, 1:-1] = wm[1:-1, 1:-1] & (
            ~wm[:-2, 1:-1] | ~wm[2:, 1:-1] |
            ~wm[1:-1, :-2] | ~wm[1:-1, 2:]
        )
        ys, xs = np.where(bank)
        return xs, ys
    except Exception as e:  # noqa: BLE001
        print(f"[viewer] bank overlay failed: {e}")
        return None


def _walked_path_from_minimap_crop(img_rgb_np, commit_deg=None
                                   ) -> Optional[list[tuple[int, int]]]:
    """Run V11 → skeleton → walker on the current mini-map crop.
    Returns the walked trace as a list of (y, x) points in the
    ORIGINAL crop's coordinate frame (i.e., row 0 = top of raw crop,
    accounting for the TRIM_TOP offset used by channel_mask_from_rgb).
    Returns None on failure.  Initial-bearing tie-break uses
    `commit_deg` if provided, else 180°."""
    try:
        from tools.centerline_extraction_prototype import (
            channel_mask_from_rgb, TRIM_TOP,
        )
        from tools.centerline_waypoint_prototype import (
            extract_tree_from_mask, pick_waypoint,
        )
        trimmed, mask = channel_mask_from_rgb(img_rgb_np)
        H, W = mask.shape
        ship_xy = (W // 2, H // 2)
        tree = extract_tree_from_mask(mask)
        if not tree.edges:
            return None
        bearing = 180.0 if commit_deg is None else float(commit_deg)
        r = math.radians(bearing)
        heading_vec = (-math.cos(r), math.sin(r))
        _, trace, _ = pick_waypoint(tree, ship_xy, heading_vec,
                                     hug_side="left",
                                     lookahead=max(H, W) * 2)
        if not trace:
            return None
        # Offset back to raw-crop coordinates (TRIM_TOP was stripped
        # from the top; TRIM_LEFT is 0).
        return [(int(y) + TRIM_TOP, int(x)) for (y, x) in trace]
    except Exception as e:  # noqa: BLE001
        print(f"[viewer] walked-path overlay failed: {e}")
        return None


def _ship_centroid(img_rgb) -> tuple[float, float]:
    """Locate the ship icon's centroid in the mini-map crop.

    Mirrors the production pipeline: HSV-thresholded `ship_green`,
    then `_clean_ship_green` filtering to the largest connected
    component.  Without the CC filter the centroid drifts on every
    green speck in the frame — text glow, NPC markers, coastline
    vegetation can all pass the broad HSV gate.  Observed at t4 of
    hug_debug_20260601_144548 (Cairo start): raw 974 px → centroid
    (202, 132); largest CC only 410 px → centroid (186, 92) on the
    actual ship.  Falls back to SHIP_DEFAULT_CENTER on any error."""
    try:
        import numpy as np
        from PIL import Image as _PILImage
        if not isinstance(img_rgb, np.ndarray):
            img_rgb = np.asarray(img_rgb)
        hsv = np.array(_PILImage.fromarray(img_rgb).convert("HSV"))
        H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        green = (H > 60) & (H < 130) & (S > 100) & (V > 100)
        if not green.any():
            return SHIP_DEFAULT_CENTER
        try:
            from scipy.ndimage import label
            labels, _ = label(green)
            sizes = np.bincount(labels.ravel())
            sizes[0] = 0   # ignore background
            green = (labels == int(np.argmax(sizes)))
        except ImportError:
            pass   # no scipy: fall back to raw mask
        ys, xs = np.where(green)
        if xs.size == 0:
            return SHIP_DEFAULT_CENTER
        return float(xs.mean()), float(ys.mean())
    except Exception:
        return SHIP_DEFAULT_CENTER


def _compass_to_xy(bearing_deg: float, length: float
                    ) -> tuple[float, float]:
    """Convert a compass bearing + length to (dx, dy) in image-pixel
    coords.  Image y-axis points DOWN so north (0°) maps to dy < 0."""
    rad = math.radians(bearing_deg)
    return length * math.sin(rad), -length * math.cos(rad)


def _load_trace(session_dir: Path) -> dict[int, dict]:
    out: dict[int, dict] = {}
    trace_path = session_dir / "trace.jsonl"
    if not trace_path.exists():
        return out
    with open(trace_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = rec.get("tick")
            if t is not None:
                out[int(t)] = rec
    return out


def _load_corrections(session_dir: Path) -> dict[int, dict]:
    """Read the sibling `corrected_latlon.jsonl` sidecar.  Same format
    as `tools/correct_reference_path.py`.  Returns {} when absent."""
    out: dict[int, dict] = {}
    path = session_dir / "corrected_latlon.jsonl"
    if not path.exists():
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                out[int(d["tick"])] = d
            except (json.JSONDecodeError, KeyError, ValueError):
                pass
    return out


def _format_record(rec: Optional[dict], correction: Optional[dict] = None) -> str:
    if rec is None:
        return "(no trace record for this tick)"

    # §13.20: 16 sectors of 22.5°.  In-between sectors get a numeric
    # label so the row is readable; the canonical eight (matching the
    # old 8-sector grid's bearings) keep their cardinal names.
    NAMES_8 = ["A", "bS", "S", "aS", "B", "aP", "P", "bP"]
    REL_8 = [0, 45, 90, 135, 180, -135, -90, -45]
    NAMES = []
    REL = []
    for j, (n, r) in enumerate(zip(NAMES_8, REL_8)):
        NAMES.append(n)
        REL.append(r)
        # In-between sector at +22.5° from this cardinal one.
        in_between_rel = ((r + 22.5 + 180) % 360) - 180
        NAMES.append(f"{j}.5")
        REL.append(int(round(in_between_rel)))

    lines: list[str] = []
    lines.append(f"tick      {rec.get('tick')}")
    lat = rec.get("lat")
    lon = rec.get("lon")
    if correction is not None:
        # Show corrected first (this is the value used downstream by
        # path-comparison + the simulator), with raw + source on the
        # next line so the OCR value is still visible.
        clat = correction.get("lat")
        clon = correction.get("lon")
        src = correction.get("source", "?")
        lines.append(f"pos       ({clat:.3f}, {clon:.3f})  ← CORRECTED [{src}]")
        if lat is not None:
            lines.append(f"  raw     ({lat:.3f}, {lon:.3f})")
        note = correction.get("note")
        if note:
            lines.append(f"  note    {note}")
    elif lat is not None:
        lines.append(f"pos       ({lat:.3f}, {lon:.3f})")
    else:
        lines.append("pos       --")
    hdg = rec.get("heading_deg")
    raw = rec.get("heading_deg_raw")
    # `heading_source` is the current field (e.g. "ship_parts_unet(parts=...)");
    # `heading_strategy` is the legacy name.  Likewise `heading_conf` replaced
    # `heading_confidence`.  Fall back so old traces still render.
    strat = rec.get("heading_source") or rec.get("heading_strategy", "?")
    conf = rec.get("heading_conf", rec.get("heading_confidence"))
    rej = rec.get("heading_rejected")
    # §13.28 — make rejection ticks impossible to miss in the viewer.
    if rej:
        lines.append("⚓ HEADING REJECTED — SHIP ANCHORED (blind) ⚓")
    if hdg is not None:
        rej_str = "  REJECTED" if rej else ""
        conf_str = f"{conf:.2f}" if conf is not None else "-"
        raw_str = f"{raw:5.1f}°" if raw is not None else "  -  "
        lines.append(f"heading   {hdg:5.1f}°  raw={raw_str}  "
                     f"strat={strat}  conf={conf_str}{rej_str}")

    # CNN raw + shadow — primary CNN is the one that actually drove
    # steering (recorded during voyage); shadow is a second model
    # that ran in parallel for A/B (also recorded).  Both come
    # straight from the trace, no re-inference.
    cnn_raw = rec.get("cnn_raw_heading_deg")
    shadow  = rec.get("shadow_cnn_heading_deg")
    shadow_c = rec.get("shadow_cnn_confidence")
    hcnf    = rec.get("heading_conf")
    if cnn_raw is not None or shadow is not None:
        cnn_str = (f"{cnn_raw:5.1f}° (c={hcnf:.2f})"
                   if cnn_raw is not None and hcnf is not None
                   else f"{cnn_raw:5.1f}°" if cnn_raw is not None
                   else "     -")
        shd_str = (f"{shadow:5.1f}° (c={shadow_c:.2f})"
                   if shadow is not None and shadow_c is not None
                   else f"{shadow:5.1f}°" if shadow is not None
                   else "     -")
        lines.append(f"cnn raw   primary={cnn_str}")
        lines.append(f"          shadow ={shd_str}")
        if cnn_raw is not None and shadow is not None:
            diff = abs(((cnn_raw - shadow + 540.0) % 360.0) - 180.0)
            marker = "  ← DISAGREE" if diff > 30 else ""
            lines.append(f"          |Δ|    ={diff:5.1f}°{marker}")
    delta = rec.get("heading_delta_deg")
    if delta is not None:
        lines.append(f"Δhdg      {delta:+6.1f}°")
    speed = rec.get("speed_kt")
    if speed is not None:
        lines.append(f"speed     {speed:.1f} kt")
    shift = rec.get("frame_shift_px")
    if shift is not None:
        try:
            dy, dx, conf = shift
            flag = " ⚠ LOW-CONF" if conf < 0.3 else ""
            lines.append(f"frame_δ   ({int(dy):+d}, {int(dx):+d}) "
                         f"conf={conf:.2f}{flag}")
        except (TypeError, ValueError):
            pass
    lines.append(f"phase     {rec.get('phase')}")
    cmd = rec.get("commanded_deg_this")
    if cmd is not None:
        lines.append(f"cmd       {cmd:+.0f}°")
    action = rec.get("action")
    if action:
        lines.append(f"action    {action}")
    note = rec.get("note")
    if note:
        lines.append(f"note      {note}")
    lines.append("")

    secs = (rec.get("nav") or {}).get("sectors") or []
    if secs:
        lines.append("sectors (frac / dist):")
        for i, s in enumerate(secs):
            obs = s.get("obs")
            frac = s.get("frac")
            dist = s.get("dist")
            if obs and frac is not None:
                d_str = f"{dist:.2f}" if dist is not None else "--"
                lines.append(f"  sec {i} {NAMES[i]:>2} (rel {REL[i]:+4d}°) "
                             f"frac={frac:.2f}  dist={d_str}")
            else:
                lines.append(f"  sec {i} {NAMES[i]:>2} (rel {REL[i]:+4d}°) "
                             f"--")
        lines.append("")

    ly = rec.get("lyapunov") or {}
    if ly:
        lines.append("destination generator:")
        wp = ly.get("pp_waypoint")
        if wp:
            lines.append(f"  destination     = ({wp[0]:.3f}, {wp[1]:.3f})")
        hug = ly.get("pp_waypoint_hug")
        if hug:
            lines.append(f"  hug (diag)      = ({hug[0]:.3f}, {hug[1]:.3f})")
        los = ly.get("pp_waypoint_dest_los")
        if los:
            lines.append(f"  los (diag)      = ({los[0]:.3f}, {los[1]:.3f})")
        wp_b = ly.get("pp_waypoint_bearing")
        if wp_b is not None:
            lines.append(f"  desired heading = {wp_b:.1f}°")
        # Endpoint of the voyage + bearing to it (the M-line direction).
        ep_lat = rec.get("endpoint_lat")
        ep_lon = rec.get("endpoint_lon")
        if ep_lat is not None and ep_lon is not None:
            lines.append(f"  endpoint        = ({ep_lat:.3f}, {ep_lon:.3f})")
            lat = rec.get("lat"); lon = rec.get("lon")
            if lat is not None and lon is not None:
                brg = (math.degrees(math.atan2(ep_lon - lon, ep_lat - lat))
                       + 360.0) % 360.0
                d = math.hypot(ep_lat - lat, ep_lon - lon)
                lines.append(f"  M-line bearing  = {brg:.1f}°  (d = {d:.2f})")
        # CoverageTracker verdict — observer-only, no behavior effect
        # (yet — FrontierPicker will read STUCK in a later phase).
        coverage_verdict = rec.get("coverage_verdict")
        if coverage_verdict:
            visited = rec.get("visited_cells")
            frontier = rec.get("frontier_cells")
            v_s = f"{visited}" if visited is not None else "—"
            f_s = f"{frontier}" if frontier is not None else "—"
            lines.append(f"  coverage verdict= {coverage_verdict}  "
                          f"(visited={v_s}, frontiers={f_s})")
        lines.append("")

        lines.append("avoider:")
        free = ly.get("pp_free_sectors")
        if free is not None:
            lines.append(f"  free       = {free}")
        diag = ly.get("pp_avoider_diag") or {}
        if diag:
            cb = diag.get("cost_breakdown") or {}
            if cb:
                ordered = sorted(cb.items(),
                                 key=lambda kv: (kv[1], int(kv[0])))
                lines.append("  cost_breakdown (low cost → chosen):")
                for sec_id, cost in ordered:
                    lines.append(f"    sec {sec_id}: {cost:.1f}")
            w = diag.get("weights")
            if w:
                lines.append(f"  weights μ  = {w}")
            sd = diag.get("safety_dist")
            if sd is not None:
                lines.append(f"  safety_dist= {sd:.3f}")
            regime = diag.get("regime")
            if regime is not None:
                dense = diag.get("dense_count")
                tir = diag.get("ticks_in_regime")
                lines.append(
                    f"  selector   = regime={regime}"
                    f"  dense={dense}"
                    f"  ticks_in_regime={tir}")
        pick_target = ly.get("pp_picked_for_target")
        if pick_target is not None:
            lines.append(f"  picked_for_target = {pick_target}")
        driver = ly.get("driver")
        if driver:
            lines.append(f"  driver     = {driver}")

    return "\n".join(lines)


class TickViewer:
    def _get_perception_models(self):
        """Lazily load CNN + PCA heading detectors, cache the pair.

        Returns (cnn, pca), or (None, None) on load failure.  On
        failure, `_perception_cache` is set to a sentinel so we don't
        retry each tick.

        IMPORTANT: The CNN checkpoint loaded here is determined by
        `self.cnn_ckpt` — either the --ckpt CLI arg, or (fallback)
        `data/heading_cnn/best.pt`.  This matters because best.pt
        may have been swapped since the session was recorded, so
        overlaid CNN arrows would show the CURRENT checkpoint's
        re-inference, NOT the CNN that actually drove the voyage.
        Pass --ckpt to view the trace with the model that actually
        ran (see the raw values in the trace itself).
        """
        if self._perception_cache is False:
            return None, None
        if self._perception_cache is not None:
            return self._perception_cache
        try:
            from brain.ai_nav.layers.heading import (
                CNNHeading as _CNN, PCAHeading as _PCA)
            kwargs = {"enable_pca_tiebreak": False}
            if self.cnn_ckpt is not None:
                kwargs["ckpt_path"] = str(self.cnn_ckpt)
            self._perception_cache = (_CNN(**kwargs), _PCA())
        except Exception:
            self._perception_cache = False
            return None, None
        return self._perception_cache

    def _get_unet(self):
        """Lazily load + cache the region-parts U-Net heading estimator
        (`ShipPartsHeading`) — the production model, re-run live for the
        yellow 'model raw' arrow.  Returns None on load failure."""
        if self._unet_cache is False:
            return None
        if self._unet_cache is not None:
            return self._unet_cache
        try:
            from brain.ai_nav.layers.heading import ShipPartsHeading
            self._unet_cache = ShipPartsHeading()
        except Exception as e:
            print(f"[viewer] U-Net heading load failed: {e}")
            self._unet_cache = False
            return None
        return self._unet_cache

    def __init__(self, session_dir: Path, start_tick: int = 1,
                 review_mode: bool = False,
                 cnn_ckpt: Optional[Path] = None):
        self.session_dir = session_dir
        # Which CNN checkpoint to use for arrow overlays.  None = read
        # from data/heading_cnn/best.pt (WARNING: may not match what
        # actually ran during this session).  Pass explicitly to see
        # the CNN the voyage used.
        self.cnn_ckpt = cnn_ckpt
        self.records = _load_trace(session_dir)
        self.corrections = _load_corrections(session_dir)
        self.frame_paths: dict[int, Path] = {}
        for p in sorted(session_dir.glob("tick_*.png")):
            try:
                t = int(p.stem.split("_")[-1])
                self.frame_paths[t] = p
            except ValueError:
                pass
        all_ticks = sorted(
            set(self.records.keys()) | set(self.frame_paths.keys()))
        if not all_ticks:
            raise SystemExit(f"no ticks found in {session_dir}")

        # Per-tick quality flags from build_rl_dataset.py sidecar.
        # Optional — only present after the dataset builder has run.
        # Drives the review mode below and the info-panel quality
        # section.  Schema documented in build_rl_dataset.py.
        self.quality_path = session_dir / "quality.jsonl"
        self.quality: dict[int, dict] = {}
        if self.quality_path.exists():
            for line in self.quality_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    self.quality[int(rec["tick"])] = rec
                except Exception:
                    pass

        # Review mode — filter the navigation list to only the flagged
        # ticks (those the auto-quality rule rejected, valid=False).
        # Lets the human walk through the dataset builder's complaints
        # and either accept ("include") or reject ("exclude") each one,
        # overriding the auto rule.  Override persists to labels.jsonl
        # as `training_include: true|false|null`.
        self.review_mode = review_mode and bool(self.quality)
        if self.review_mode:
            flagged = sorted(t for t, q in self.quality.items()
                             if not q.get("valid", True))
            if flagged:
                self.ticks = flagged
                print(f"[review] {len(flagged)} flagged ticks; "
                      f"use →/← to walk through, i=include, x=exclude, "
                      f"./0 to clear override")
            else:
                self.ticks = all_ticks
                print("[review] no flagged ticks in this session")
        else:
            self.ticks = all_ticks
        if start_tick in self.ticks:
            self.idx = self.ticks.index(start_tick)
        else:
            self.idx = 0

        self.autoplay = False
        self.autoplay_ms = DEFAULT_AUTOPLAY_MS
        self._anim = None
        self._overlay_artists: list = []
        # Perception model cache — CNN + PCA loaded lazily on first
        # frame render, then reused so each tick doesn't pay the
        # (~1s CNN, ~10ms PCA) model-init cost.  Set to `False`
        # sentinel after a load failure so we don't retry per tick.
        self._perception_cache = None
        # Region-parts U-Net heading estimator, loaded lazily + cached for
        # the live yellow "model raw" arrow (replaces the old CNN arrow; the
        # CNN is no longer used).  `False` sentinel after a load failure.
        self._unet_cache = None
        # Bank-line overlay (toggle with `b`).  Defaults ON so we
        # always see what perception thinks; useful for diagnosing
        # whether navigation issues are upstream (wrong banks) or
        # downstream (right banks, wrong waypoint).
        self._show_bank: bool = True
        # V10 prototype bank-line overlay (toggle with `v`).  Defaults
        # OFF — turn on when you want a side-by-side with production.
        # Drawn in yellow so it's distinguishable from production's
        # magenta bank.  See _v10_water_mask above for the pipeline.
        self._show_v10_bank: bool = False
        # Walked-path overlay (toggle with `w`).  Runs the current
        # V11 → extract_tree → pick_waypoint pipeline on the tick's
        # frame and draws the skeleton walker's trace in orange.
        # Off by default — this is a per-tick recompute (~10ms).
        self._show_walked_path: bool = False

        # Jump-to-tick mode: press `g`, type digits, Enter to jump.
        # `Esc` or `Backspace` clears.  Avoids blocking the matplotlib
        # event loop by capturing one digit at a time into a buffer.
        self._jump_mode: bool = False
        self._jump_buffer: str = ""

        # Branch-end annotations.  Load from session dir; the user
        # presses `m` on a tick where the captured voyage reached a
        # river/branch endpoint (turn-around point).  These ground-
        # truth ends drive the future Trémaux replay diff.
        self.ends_path = session_dir / "ends.jsonl"
        self.ends: list[dict] = []
        if self.ends_path.exists():
            for line in self.ends_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self.ends.append(json.loads(line))
                except Exception:
                    pass

        # Per-tick action verdicts.  `1`/`2`/`3` → ideal/acceptable/
        # wrong; `0` clears.  Keyed by tick; one verdict per tick.
        # Drives future replay diff: bot's recomputed wp at each tick
        # should land in `ideal` or `acceptable`, never `wrong`.
        self.labels_path = session_dir / "labels.jsonl"
        self.labels: dict[int, dict] = {}
        if self.labels_path.exists():
            for line in self.labels_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    self.labels[int(rec["tick"])] = rec
                except Exception:
                    pass

        # Raw keystrokes (capture mode only).  Loaded from keystrokes.
        # jsonl.  pynput emits OS auto-repeat presses while a key is
        # held, so we reconstruct physical "holds" (one entry per
        # press→release cycle, with duration_ms) at load time and
        # join each hold to the tick(s) it overlaps for display.
        self.keystrokes: list[dict] = []
        self.holds: list[dict] = []
        ks_path = session_dir / "keystrokes.jsonl"
        if ks_path.exists():
            from datetime import datetime as _dt
            for line in ks_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self.keystrokes.append(json.loads(line))
                except Exception:
                    pass
            # Collapse auto-repeats into physical holds.
            pending: dict = {}  # key -> (first_ts, n_press)
            for ev in self.keystrokes:
                try:
                    ts = _dt.fromisoformat(ev["wall_iso"])
                except Exception:
                    continue
                k = ev.get("key")
                if ev.get("event") == "press":
                    if k in pending:
                        first, n = pending[k]
                        pending[k] = (first, n + 1)
                    else:
                        pending[k] = (ts, 1)
                elif ev.get("event") == "release":
                    if k in pending:
                        first, n = pending.pop(k)
                        self.holds.append({
                            "key": k,
                            "press_iso": first.isoformat(),
                            "release_iso": ts.isoformat(),
                            "duration_ms": int(
                                (ts - first).total_seconds() * 1000),
                            "n_repeats": n,
                        })
            # If something is still pending (no release seen — voyage
            # ended mid-hold), close it at the last keystroke time.
            if self.keystrokes and pending:
                last = _dt.fromisoformat(self.keystrokes[-1]["wall_iso"])
                for k, (first, n) in pending.items():
                    self.holds.append({
                        "key": k,
                        "press_iso": first.isoformat(),
                        "release_iso": last.isoformat(),
                        "duration_ms": int(
                            (last - first).total_seconds() * 1000),
                        "n_repeats": n,
                        "open_ended": True,
                    })

        self._build_ui()
        self._show_current()

    def _build_ui(self):
        # Wide figure: image on left, text panel on right.
        self.fig = plt.figure(
            f"Tick Viewer — {self.session_dir.name}",
            figsize=(14, 8),
        )
        self.fig.patch.set_facecolor("#202020")

        # Image axis (left ~40% of width, top half).  Mini-map crop
        # of the current tick.
        self.ax_img = self.fig.add_axes([0.02, 0.50, 0.42, 0.46])
        self.ax_img.set_facecolor("#000")
        self.ax_img.set_xticks([])
        self.ax_img.set_yticks([])
        for spine in self.ax_img.spines.values():
            spine.set_color("#444")
        self._img_artist = self.ax_img.imshow(
            np.zeros((184, 381, 3), dtype=np.uint8),
            interpolation="nearest",
        )

        # World-view axis (left ~40% width, bottom half).  Honest
        # world coordinates: the full voyage trajectory + endpoint +
        # M-line, auto-scaled to fit.  Complements the mini-map's
        # local view.
        self.ax_world = self.fig.add_axes([0.02, 0.06, 0.42, 0.38])
        self.ax_world.set_facecolor("#101010")
        self.ax_world.tick_params(colors="#888", labelsize=8)
        for spine in self.ax_world.spines.values():
            spine.set_color("#444")
        self.ax_world.set_xlabel("lon", color="#aaa", fontsize=8)
        self.ax_world.set_ylabel("lat", color="#aaa", fontsize=8)
        self._world_artists: list = []

        # Text axis (right ~55% of width).  Use a dedicated axes for
        # the text so we can clear+redraw cheaply.
        self.ax_txt = self.fig.add_axes([0.46, 0.06, 0.52, 0.90])
        self.ax_txt.set_facecolor("#101010")
        self.ax_txt.set_xticks([])
        self.ax_txt.set_yticks([])
        for spine in self.ax_txt.spines.values():
            spine.set_color("#444")
        self._txt_artist = self.ax_txt.text(
            0.01, 0.99, "",
            transform=self.ax_txt.transAxes,
            ha="left", va="top",
            family="monospace", fontsize=10,
            color="#dddddd",
        )

        # Status bar (figure suptitle area).
        self._status_artist = self.fig.text(
            0.5, 0.01, "",
            ha="center", va="bottom",
            family="monospace", fontsize=11,
            color="#cccccc",
        )

        # Key bindings.
        self.fig.canvas.mpl_connect("key_press_event", self._on_key)

    def _on_key(self, event):
        k = event.key
        # Jump-to-tick capture mode — swallow all keys here.
        if self._jump_mode:
            if k in ("enter", "return"):
                self._execute_jump()
            elif k == "escape":
                self._cancel_jump()
            elif k == "backspace":
                self._jump_buffer = self._jump_buffer[:-1]
                self._render_jump_prompt()
            elif k and len(k) == 1 and k.isdigit():
                self._jump_buffer += k
                self._render_jump_prompt()
            return
        if k == "right":
            self._step(+1)
        elif k == "left":
            self._step(-1)
        elif k == "home":
            self._go(0)
        elif k == "end":
            self._go(len(self.ticks) - 1)
        elif k == "g":
            self._enter_jump_mode()
        elif k == "r":
            self._jump_to_next_rejection()
        elif k == "R":
            self._print_rejection_summary()
        elif k == " ":
            self._toggle_autoplay()
        elif k in ("+", "="):
            self.autoplay_ms = max(50, int(self.autoplay_ms * 0.5))
            if self._anim is not None:
                self._anim.event_source.interval = self.autoplay_ms
            self._update_status()
        elif k == "-":
            self.autoplay_ms = min(5000, int(self.autoplay_ms * 2.0))
            if self._anim is not None:
                self._anim.event_source.interval = self.autoplay_ms
            self._update_status()
        elif k == "s":
            self._save_view()
        elif k == "o":
            self._open_folder_dialog()
        elif k == "m":
            self._mark_end()
        elif k == "M":
            self._unmark_last_end()
        elif k == "1":
            self._label_tick("ideal")
        elif k == "2":
            self._label_tick("acceptable")
        elif k == "3":
            self._label_tick("wrong")
        elif k == "0":
            self._label_tick(None)
        # Corrected-action keys.  Digits 4-6 = LEFT short/med/long;
        # digits 7-9 = RIGHT short/med/long; `h` = HOLD.  Pressing any
        # of these promotes verdict to "wrong" if no verdict was set
        # (correction implies original action was wrong).
        elif k == "h":      self._correct_action("HOLD")
        elif k == "4":      self._correct_action("L_SHORT")
        elif k == "5":      self._correct_action("L_MED")
        elif k == "6":      self._correct_action("L_LONG")
        elif k == "7":      self._correct_action("R_SHORT")
        elif k == "8":      self._correct_action("R_MED")
        elif k == "9":      self._correct_action("R_LONG")
        # State verdict (independent of action).  Bracket keys form a
        # visual scale on the keyboard: [ = good situation, ] = bad
        # situation, \ clears the state verdict.
        elif k == "[":      self._label_state("good")
        elif k == "]":      self._label_state("bad")
        elif k == "\\":     self._label_state(None)
        # Training-include override (for review-mode triage of
        # quality-flagged ticks).  `i` = include in training despite the
        # auto rule saying skip; `x` = exclude despite the auto rule
        # saying keep; `.` = clear the human override (revert to auto).
        elif k == "i":      self._set_training_include(True)
        elif k == "x":      self._set_training_include(False)
        elif k == ".":      self._set_training_include(None)
        # Heading correction — take the motion bearing from quality.jsonl
        # as the ground-truth heading.  Useful for ticks where the
        # template-match heading is wrong but the lat/lon motion is
        # clean (the classic "facing bank, antipode flipped" case).
        # Capital H is one keystroke (shift+h), no conflict with `h`
        # which sets corrected_action=HOLD.
        elif k == "H":      self._adopt_motion_bearing()
        # Adopt antipode: corrected_heading = (template heading + 180°).
        # The right correction when the matcher's tiebreaker picked the
        # wrong side of the 180° symmetry — which is most "facing bank"
        # cases, where motion ≠ heading (ship pinned at shore, hull
        # sliding along it).  Use this when motion_brg disagrees with
        # both the heading AND the antipode by a lot.
        elif k == "A":      self._adopt_antipode()
        elif k == "b":
            self._show_bank = not self._show_bank
            self._show_current()
        elif k == "v":
            self._show_v10_bank = not self._show_v10_bank
            self._show_current()
        elif k == "w":
            self._show_walked_path = not self._show_walked_path
            self._show_current()
        elif k in ("q", "escape"):
            plt.close(self.fig)

    # ── Jump-to-tick (`g <digits> Enter`) ──

    def _enter_jump_mode(self):
        self._jump_mode = True
        self._jump_buffer = ""
        self._render_jump_prompt()

    def _cancel_jump(self):
        self._jump_mode = False
        self._jump_buffer = ""
        print("[viewer] jump cancelled")

    def _render_jump_prompt(self):
        # Print to stdout — keeps the matplotlib figure responsive.
        # `\r` so successive digits overwrite on the same line.
        print(f"\r[viewer] jump to tick: {self._jump_buffer}_   "
              "(Enter=go, Esc=cancel, Backspace=delete)",
              end="", flush=True)

    def _execute_jump(self):
        buf = self._jump_buffer
        self._jump_mode = False
        self._jump_buffer = ""
        print()  # newline after the \r prompt
        if not buf:
            print("[viewer] jump cancelled (empty)")
            return
        try:
            target = int(buf)
        except ValueError:
            print(f"[viewer] jump: invalid tick '{buf}'")
            return
        if target in self.ticks:
            self._go(self.ticks.index(target))
            print(f"[viewer] → t{target}")
            return
        # Snap to the nearest tick on either side — the captured
        # tick set may have gaps (e.g. when a tick failed to write).
        nearest = min(self.ticks, key=lambda t: abs(t - target))
        self._go(self.ticks.index(nearest))
        print(f"[viewer] → t{nearest}  "
              f"(snapped from requested t{target})")

    def _label_tick(self, verdict: Optional[str]):
        """Set or clear the action verdict on the current tick.
        Preserves existing corrected_action + state_verdict."""
        tick = self.ticks[self.idx]
        rec = self.records.get(tick) or {}
        existing = self.labels.get(tick, {})
        if verdict is None:
            # Clear only the action verdict + corrected_action.
            # state_verdict is independent and survives.
            for k in ("verdict", "corrected_action"):
                existing.pop(k, None)
            if existing.get("state_verdict"):
                self.labels[tick] = existing
                print(f"[viewer] cleared action on t{tick} "
                      f"(state={existing['state_verdict']} preserved)")
            else:
                if tick in self.labels:
                    del self.labels[tick]
                print(f"[viewer] cleared label on t{tick}")
        else:
            self.labels[tick] = {
                "tick": tick,
                "verdict": verdict,                      # = action_verdict
                "wall_iso": rec.get("wall_iso"),
                **({"corrected_action": existing["corrected_action"]}
                   if "corrected_action" in existing else {}),
                **({"state_verdict": existing["state_verdict"]}
                   if "state_verdict" in existing else {}),
            }
            print(f"[viewer] labeled t{tick} action={verdict}  "
                  f"({len(self.labels)} labels total)")
        self._save_labels()
        self._show_current()

    def _label_state(self, state_verdict: Optional[str]):
        """Set or clear the STATE verdict on the current tick.
        Independent of action verdict — the ship can be in a bad
        situation while the chosen action is the correct escape."""
        tick = self.ticks[self.idx]
        rec = self.records.get(tick) or {}
        existing = self.labels.get(tick, {})
        if state_verdict is None:
            if "state_verdict" in existing:
                del existing["state_verdict"]
                if existing.get("verdict") or "corrected_action" in existing:
                    self.labels[tick] = existing
                else:
                    del self.labels[tick]
                print(f"[viewer] cleared state on t{tick}")
        else:
            self.labels[tick] = {
                **existing,
                "tick": tick,
                "wall_iso": rec.get("wall_iso"),
                "state_verdict": state_verdict,
            }
            print(f"[viewer] t{tick} state={state_verdict}")
        self._save_labels()
        self._show_current()

    def _adopt_antipode(self):
        """Take the antipode (heading + 180°) as the corrected heading.

        The right correction for the canonical antipode-flip case: the
        template matcher's tiebreaker picked the wrong side of the 180°
        symmetry.  Use this when motion bearing also disagrees with the
        heading (i.e. ship isn't sailing where it points — collision /
        pinned at bank), since in those cases motion isn't a reliable
        heading proxy and the antipode of the template match is.
        """
        tick = self.ticks[self.idx]
        q = self.quality.get(tick) or {}
        hdg = q.get("heading_deg")
        if hdg is None:
            print(f"[viewer] t{tick}: no heading_deg in quality.jsonl "
                  f"(rebuild dataset?)")
            return
        anti = (float(hdg) + 180.0) % 360.0
        rec = self.records.get(tick) or {}
        existing = self.labels.get(tick, {})
        self.labels[tick] = {
            **existing,
            "tick": tick,
            "wall_iso": rec.get("wall_iso"),
            "corrected_heading": round(anti, 1),
        }
        print(f"[viewer] t{tick}: corrected_heading = {anti:.1f}° "
              f"(antipode of {hdg:.1f}°)")
        self._save_labels()
        self._show_current()

    def _adopt_motion_bearing(self):
        """Take the lat/lon-derived motion bearing as the corrected
        heading for the current tick.  Persists `corrected_heading`
        (float, degrees) to labels.jsonl.  BCDataset reads this and
        overrides heading_sin/cos at training time.

        Useful when the template-match heading is wrong but lat/lon
        motion is clean (the antipode-flip + ship-side-bias cases).
        Keeps the tick in training with a correct heading instead of
        either training on wrong data or dropping the tick entirely.

        Combine with `]` (state=bad) for cases like "ship facing bank"
        — model trains to recognize and avoid those trajectories via
        the hindsight propagation.
        """
        tick = self.ticks[self.idx]
        q = self.quality.get(tick) or {}
        mot_brg = q.get("motion_bearing_deg")
        if mot_brg is None:
            print(f"[viewer] t{tick}: no motion_bearing available "
                  f"(motion_mag={q.get('motion_mag', 0):.3f} out of "
                  f"trust range; rebuild dataset if quality.jsonl is stale)")
            return
        rec = self.records.get(tick) or {}
        existing = self.labels.get(tick, {})
        old_hdg = q.get("heading_deg")
        self.labels[tick] = {
            **existing,
            "tick": tick,
            "wall_iso": rec.get("wall_iso"),
            "corrected_heading": round(float(mot_brg), 1),
        }
        old_str = f"{old_hdg:.1f}°" if old_hdg is not None else "?"
        print(f"[viewer] t{tick}: corrected_heading = {mot_brg:.1f}° "
              f"(was {old_str}, motion-bearing override)")
        self._save_labels()
        self._show_current()

    def _set_training_include(self, include: Optional[bool]):
        """Override the auto valid-mask decision for the current tick.

        Persists `training_include: true|false|null` to labels.jsonl.
        The BC DataLoader checks this column and uses the human verdict
        when present; otherwise falls back to the auto `valid` rule.

        include=True  → force-include even if auto rule said skip.
        include=False → force-exclude even if auto rule said keep.
        include=None  → clear the override; auto rule wins.
        """
        tick = self.ticks[self.idx]
        rec = self.records.get(tick) or {}
        existing = self.labels.get(tick, {})
        q = self.quality.get(tick, {})
        auto = q.get("valid", True)
        if include is None:
            if "training_include" in existing:
                del existing["training_include"]
                has_other = (existing.get("verdict")
                             or "corrected_action" in existing
                             or "state_verdict" in existing)
                if has_other:
                    self.labels[tick] = existing
                else:
                    self.labels.pop(tick, None)
                print(f"[viewer] cleared training_include on t{tick} "
                      f"(auto rule says {'keep' if auto else 'skip'})")
            else:
                print(f"[viewer] no override to clear on t{tick}")
        else:
            self.labels[tick] = {
                **existing,
                "tick": tick,
                "wall_iso": rec.get("wall_iso"),
                "training_include": include,
            }
            verb = "INCLUDE" if include else "EXCLUDE"
            mismatch = " (overrides auto)" if include != auto else " (matches auto)"
            print(f"[viewer] t{tick}: {verb}{mismatch}")
        self._save_labels()
        self._show_current()

    def _correct_action(self, action_name: str):
        """Annotate the current tick with the action the user thinks
        should have been taken.  Promotes verdict to 'wrong' if not
        already set (a correction implies the original was wrong).
        Saved alongside verdict in labels.jsonl.

        Action names mirror tools/build_rl_dataset.ACTION_NAMES:
          HOLD, L_SHORT, L_MED, L_LONG, R_SHORT, R_MED, R_LONG.
        """
        tick = self.ticks[self.idx]
        rec = self.records.get(tick) or {}
        existing = self.labels.get(tick, {})
        verdict = existing.get("verdict") or "wrong"
        self.labels[tick] = {
            "tick": tick,
            "verdict": verdict,
            "corrected_action": action_name,
            "wall_iso": rec.get("wall_iso"),
        }
        print(f"[viewer] t{tick}: verdict={verdict}, "
              f"corrected_action={action_name}")
        self._save_labels()
        self._show_current()

    def _save_labels(self):
        with self.labels_path.open("w") as fp:
            for tick in sorted(self.labels.keys()):
                fp.write(json.dumps(self.labels[tick]) + "\n")

    def _format_keys_for_tick(self, tick: int, rec) -> str:
        """One line summarising what the user pressed during this
        tick's [wall_iso, next_tick.wall_iso) window.  Shows holds
        that overlap the window — each as `<key>:<overlap_ms>ms`
        (the visible portion within the tick).  Empty windows show
        `your input: ─`.
        """
        if rec is None or not self.holds:
            return "your input: ─"
        from datetime import datetime as _dt, timedelta as _td
        try:
            t0 = _dt.fromisoformat(rec.get("wall_iso") or "")
        except Exception:
            return "your input: ─"
        # Window end = next tick's wall_iso, or +1.5s as fallback.
        next_idx = self.idx + 1
        if next_idx < len(self.ticks):
            next_rec = self.records.get(self.ticks[next_idx])
            t1_str = next_rec.get("wall_iso") if next_rec else None
            try:
                t1 = _dt.fromisoformat(t1_str) if t1_str else t0 + _td(seconds=1.5)
            except Exception:
                t1 = t0 + _td(seconds=1.5)
        else:
            t1 = t0 + _td(seconds=1.5)
        parts = []
        for h in self.holds:
            try:
                p = _dt.fromisoformat(h["press_iso"])
                r = _dt.fromisoformat(h["release_iso"])
            except Exception:
                continue
            if r < t0 or p >= t1:
                continue
            # Compute the overlap within this tick window.
            overlap_ms = int(
                ((min(r, t1) - max(p, t0)).total_seconds()) * 1000)
            if overlap_ms <= 0:
                continue
            # Distinguish a hold that started in this tick from a
            # hold that's continuing from an earlier tick.
            mark = "" if p >= t0 else "↳"
            tot = h["duration_ms"]
            parts.append(f"{mark}{h['key']!r}:{overlap_ms}ms (total {tot}ms)")
        if not parts:
            return "your input: ─"
        return "your input: " + "  ".join(parts)

    def _mark_end(self):
        """Annotate the current tick as a branch-end (turn-around
        point) — appends {tick, lat, lon, wall_iso} to ends.jsonl.
        """
        tick = self.ticks[self.idx]
        rec = self.records.get(tick)
        if rec is None:
            print(f"[viewer] cannot mark t{tick}: no trace record")
            return
        lat, lon = rec.get("lat"), rec.get("lon")
        if lat is None or lon is None:
            print(f"[viewer] cannot mark t{tick}: no lat/lon")
            return
        # De-dupe if a mark already exists for this exact tick.
        self.ends = [e for e in self.ends if e.get("tick") != tick]
        entry = {
            "tick": tick,
            "lat": float(lat),
            "lon": float(lon),
            "wall_iso": rec.get("wall_iso"),
        }
        self.ends.append(entry)
        self._save_ends()
        print(f"[viewer] marked end at t{tick} ({lat:.3f}, {lon:.3f}) "
              f"→ {self.ends_path}  ({len(self.ends)} ends total)")
        self._show_current()

    def _unmark_last_end(self):
        if not self.ends:
            print("[viewer] no ends to unmark")
            return
        last = self.ends.pop()
        self._save_ends()
        print(f"[viewer] unmarked end at t{last.get('tick')}  "
              f"({len(self.ends)} remaining)")
        self._show_current()

    def _save_ends(self):
        with self.ends_path.open("w") as fp:
            for e in self.ends:
                fp.write(json.dumps(e) + "\n")

    def _step(self, delta: int):
        self.idx = max(0, min(len(self.ticks) - 1, self.idx + delta))
        self._show_current()

    def _go(self, idx: int):
        self.idx = max(0, min(len(self.ticks) - 1, idx))
        self._show_current()

    # ── §13.28 rejection navigation + summary ──

    def _rejection_records(self) -> list[dict]:
        """All ticks that were heading-rejected, in tick order."""
        return [
            {"tick": t, **r}
            for t, r in sorted(self.records.items())
            if r.get("heading_rejected")
        ]

    def _jump_to_next_rejection(self):
        """Advance to the next rejection tick after the current one.
        Wraps to the first rejection if past the last one."""
        rejs = self._rejection_records()
        if not rejs:
            print("[viewer] no rejection ticks in this voyage")
            return
        cur_tick = self.ticks[self.idx]
        nxt = next((r for r in rejs if r["tick"] > cur_tick), rejs[0])
        if nxt["tick"] in self.ticks:
            self._go(self.ticks.index(nxt["tick"]))
            print(
                f"[viewer] → t{nxt['tick']} REJECTED  "
                f"raw={nxt.get('heading_deg_raw'):.0f}°  "
                f"strat={nxt.get('heading_strategy')}  "
                f"conf={nxt.get('heading_confidence')}  "
                f"prev_cmd={nxt.get('commanded_deg_last'):+.0f}°"
            )

    def _print_rejection_summary(self):
        """Print full table of rejection ticks to stdout."""
        rejs = self._rejection_records()
        total = len(self.records)
        n_rej = len(rejs)
        pct = 100.0 * n_rej / total if total else 0.0
        # Group consecutive rejections into bursts (stop_for_rejection
        # cycles).  Each burst likely corresponds to one sail_stop /
        # sail_start pair in the live runner.
        bursts: list[list[dict]] = []
        for r in rejs:
            if bursts and r["tick"] == bursts[-1][-1]["tick"] + 1:
                bursts[-1].append(r)
            else:
                bursts.append([r])
        print()
        print("=" * 70)
        print(f"§13.28 rejection summary — {self.session_dir.name}")
        print("=" * 70)
        print(f"  total ticks recorded : {total}")
        print(f"  rejected ticks       : {n_rej}  ({pct:.1f}%)")
        print(f"  rejection bursts     : {len(bursts)}  "
              f"(≈ sail_stop / sail_start cycles)")
        print()
        if not rejs:
            print("  (no rejections)")
            return
        print(f"  {'burst':>5} {'first→last':>11} {'len':>3} "
              f"{'raw°':>5} {'prev°':>5} {'Δ°':>4} {'strat':>17} "
              f"{'conf':>4} {'prev_cmd°':>9}")
        print(f"  {'-'*5:>5} {'-'*11:>11} {'-'*3:>3} {'-'*5:>5} "
              f"{'-'*5:>5} {'-'*4:>4} {'-'*17:>17} {'-'*4:>4} "
              f"{'-'*9:>9}")
        for i, burst in enumerate(bursts, 1):
            first = burst[0]
            # use the first tick's signals as representative of the burst
            raw    = first.get("heading_deg_raw")
            prev   = first.get("heading_deg")
            strat  = first.get("heading_strategy") or "?"
            conf   = first.get("heading_confidence")
            cmd    = first.get("commanded_deg_last")
            delta  = first.get("heading_delta_deg")
            span = (f"t{burst[0]['tick']:4d}→t{burst[-1]['tick']:4d}"
                    if len(burst) > 1 else f"t{burst[0]['tick']:4d}     ")
            raw_s   = f"{raw:5.0f}"  if raw  is not None else "  -- "
            prev_s  = f"{prev:5.0f}" if prev is not None else "  -- "
            delta_s = f"{delta:+4.0f}" if delta is not None else " -- "
            conf_s  = f"{conf:.2f}"  if conf is not None else "  --"
            cmd_s   = f"{cmd:+9.0f}" if cmd  is not None else "       --"
            print(f"  {i:>5} {span:>11} {len(burst):>3} "
                  f"{raw_s:>5} {prev_s:>5} {delta_s:>4} {strat:>17} "
                  f"{conf_s:>4} {cmd_s:>9}")
        print()
        print("  Reason (one and only one rejection rule today):")
        print("    |raw − prev| exceeds plausible_max =")
        print("      max(|prev_commanded|+30°, 30°) × conf_relax")
        print("      where conf_relax = 1.0 + (1−conf)·1.25")
        print("  (see brain/goals/hug_shore.py:run_hug_shore_loop)")
        print("=" * 70)

    def _toggle_autoplay(self):
        self.autoplay = not self.autoplay
        if self.autoplay:
            self._anim = FuncAnimation(
                self.fig,
                lambda frame: self._autoplay_step(),
                interval=self.autoplay_ms,
                cache_frame_data=False,
            )
        else:
            if self._anim is not None:
                self._anim.event_source.stop()
                self._anim = None
        self._update_status()

    def _autoplay_step(self):
        if not self.autoplay:
            return
        if self.idx >= len(self.ticks) - 1:
            self.autoplay = False
            if self._anim is not None:
                self._anim.event_source.stop()
                self._anim = None
            self._update_status()
            return
        self.idx += 1
        self._show_current()

    def _show_current(self):
        tick = self.ticks[self.idx]
        path = self.frame_paths.get(tick)
        if path is not None and path.exists():
            img = np.asarray(Image.open(path).convert("RGB"))
            self._img_artist.set_data(img)
            self.ax_img.set_xlim(0, img.shape[1])
            self.ax_img.set_ylim(img.shape[0], 0)
        else:
            img = np.zeros((184, 381, 3), dtype=np.uint8)
            self._img_artist.set_data(img)
            self.ax_img.set_xlim(0, 381)
            self.ax_img.set_ylim(184, 0)
        rec = self.records.get(tick)
        # §13.28 — show a red title when the tick was a rejection-stop so
        # the rejected ticks are obvious while scrolling.
        rej = rec is not None and rec.get("heading_rejected")
        title_color = "#ff6666" if rej else "#dddddd"
        title_suffix = "  ⚓ REJECTED — anchored" if rej else ""
        if path is None or not path.exists():
            title_suffix += "  (no frame)"
        note = (rec or {}).get("note")
        note_line = f"\n{note}" if note else ""
        self.ax_img.set_title(
            f"tick {tick}{title_suffix}{note_line}",
            color=title_color, fontsize=11,
        )
        # Verdict panel — disambiguates what the label means.
        label = self.labels.get(tick)
        verdict_raw = (label or {}).get("verdict")
        verdict_str = verdict_raw.upper() if verdict_raw else "(none)"
        state_raw = (label or {}).get("state_verdict")
        state_str = state_raw.upper() if state_raw else "(none)"
        corrected = label.get("corrected_action") if label else None
        corrected_str = corrected if corrected else "(none)"
        bot_action = (rec or {}).get("action", "—")
        # Strip the parenthetical "(~Xdeg@Y/s)" so the line stays short.
        bot_action_short = bot_action.split("(")[0].strip()
        keys_str = self._format_keys_for_tick(tick, rec).removeprefix(
            "your input: ")
        # Quality / training-include block — only shown when the
        # dataset builder dropped a quality.jsonl sidecar.
        q = self.quality.get(tick)
        if q:
            auto_valid = q.get("valid", True)
            human_override = (label or {}).get("training_include")
            if human_override is None:
                effective = "KEEP" if auto_valid else "SKIP"
                override_str = "(auto)"
            else:
                effective = "KEEP" if human_override else "SKIP"
                override_str = "INCLUDE" if human_override else "EXCLUDE"
                if human_override != auto_valid:
                    override_str += " (overrides auto)"
            # Heading vs motion-bearing line — shows the user whether
            # heading and motion agree, and offers the motion bearing
            # as a candidate corrected heading.
            hdg_deg = q.get("heading_deg")
            mot_brg = q.get("motion_bearing_deg")
            corrected_hdg = (label or {}).get("corrected_heading")
            if hdg_deg is not None:
                anti_deg = (hdg_deg + 180.0) % 360.0
                if mot_brg is not None:
                    diff = abs(((hdg_deg - mot_brg + 540.0) % 360.0) - 180.0)
                    hdg_line = (f"   heading   : {hdg_deg:>6.1f}°   "
                                f"antipode: {anti_deg:>6.1f}°   "
                                f"motion_brg: {mot_brg:>6.1f}°  "
                                f"(Δ from heading={diff:>5.1f}°)")
                else:
                    hdg_line = (f"   heading   : {hdg_deg:>6.1f}°   "
                                f"antipode: {anti_deg:>6.1f}°   "
                                f"motion_brg: (mag={q.get('motion_mag',0):.3f} "
                                f"out of trust range)")
            else:
                hdg_line = "   heading   : (none)"
            if corrected_hdg is not None:
                hdg_line += f"   corrected→{corrected_hdg:.1f}°"
            quality_block = (
                "─── data quality (auto rule + human override) ─────────────────\n"
                f"   roi       : {q.get('roi',''):<14}  "
                f"topology: {q.get('topology','') or '(none)'}\n"
                f"   heading_q : {q.get('heading_quality',''):<14}  "
                f"src: {q.get('heading_source','')}\n"
                f"   latlon_q  : {q.get('latlon_quality',''):<14}  "
                f"auto_valid: {auto_valid}\n"
                + hdg_line + "\n"
                f"   training  : {effective:<14}  override: {override_str}\n"
                "   training keys: i=include  x=exclude  .=clear override\n"
                "   heading keys: A=use antipode (heading+180°)  "
                "H=use motion_bearing\n"
            )
        else:
            quality_block = ""
        header = (
            "─── verdict (action + state, independent dimensions) ─────────\n"
            f"   action    : {verdict_str:>12}    corrected: {corrected_str}\n"
            f"   state     : {state_str:>12}\n"
            "   action keys: 1=ideal  2=acceptable  3=wrong  0=clear\n"
            "   state keys : [=good  ]=bad  \\=clear\n"
            "   correct keys: h=HOLD  4/5/6=L_SHORT/MED/LONG  "
            "7/8/9=R_SHORT/MED/LONG\n"
            "   nav: g=jump-to-tick  ←/→=step  space=play\n"
            + quality_block +
            "─── context ──────────────────────────────────────────────────\n"
            f"   your input: {keys_str}\n"
            f"   bot wanted: {bot_action_short}\n"
            "─── trace ────────────────────────────────────────────────────\n"
        )
        correction = self.corrections.get(tick) if self.corrections else None
        self._txt_artist.set_text(header + _format_record(rec, correction))
        self._draw_overlays(rec, img)
        self._draw_world_view(tick, rec)
        self._update_status()
        self.fig.canvas.draw_idle()

    def _draw_overlays(self, rec: Optional[dict], img):
        """Draw the waypoint arrow + commanded-turn arrow on top of
        the mini-map.  Mini-map is north-up so compass bearing maps
        directly to direction on the image."""
        # Clear previous overlays.
        for art in self._overlay_artists:
            try:
                art.remove()
            except Exception:
                pass
        self._overlay_artists = []
        # Frame-shift low-confidence badge (top-right corner of minimap).
        # Warns of registration failure — UI overlay, camera change,
        # popup, or first tick with no prev mask.  See
        # docs/pixel_continuity_design.md §3.
        shift = rec.get("frame_shift_px") if rec else None
        if shift is not None:
            try:
                _, _, conf = shift
                if conf < 0.3:
                    warn = self.ax_img.text(
                        0.98, 0.02, f"shift low-conf {conf:.2f}",
                        transform=self.ax_img.transAxes,
                        ha="right", va="bottom",
                        color="white",
                        bbox=dict(facecolor="#cc2222", alpha=0.85,
                                  edgecolor="none", pad=2),
                        fontsize=8, zorder=10,
                    )
                    self._overlay_artists.append(warn)
            except (TypeError, ValueError):
                pass
        # Bank line — what perception currently believes about land/
        # water boundary.  Drawn FIRST so the ship/waypoint markers
        # render on top of it.  Use a faint magenta so it doesn't
        # compete visually with the cyan/green/orange action arrows.
        # Toggle with `b`.
        if self._show_bank and img is not None:
            bank = _bank_pixels_from_minimap_crop(img)
            if bank is not None:
                bxs, bys = bank
                bank_artist = self.ax_img.scatter(
                    bxs, bys, s=1.2, c="#ff66cc",
                    alpha=0.85, zorder=3,
                    label="production bank",
                )
                self._overlay_artists.append(bank_artist)
        # V10 prototype bank — drawn in yellow for clear contrast with
        # production's magenta.  See `_v10_water_mask` for the pipeline.
        if self._show_v10_bank and img is not None:
            v10_bank = _v10_bank_pixels_from_minimap_crop(img)
            if v10_bank is not None:
                bxs, bys = v10_bank
                v10_artist = self.ax_img.scatter(
                    bxs, bys, s=1.2, c="#ffdd44",
                    alpha=0.85, zorder=2,
                    label="V10 bank",
                )
                self._overlay_artists.append(v10_artist)
        # Walked-path overlay (toggle `w`) — recompute the skeleton
        # walker for the current tick's image and draw its trace in
        # orange.  Uses commit_deg from the trace record if available.
        if self._show_walked_path and img is not None:
            cd = (rec or {}).get("commit_deg")
            wp_trace = _walked_path_from_minimap_crop(img, commit_deg=cd)
            if wp_trace:
                pys = [p[0] for p in wp_trace]
                pxs = [p[1] for p in wp_trace]
                wp_line = self.ax_img.plot(
                    pxs, pys, color="#ff9900", linewidth=2.5,
                    alpha=0.9, zorder=4,
                )[0]
                self._overlay_artists.append(wp_line)
        if rec is None:
            return
        cx, cy = _ship_centroid(img)
        # Ship dot.
        dot = self.ax_img.scatter(
            [cx], [cy], s=40, c="#00d2ff", edgecolors="black",
            linewidths=0.8, zorder=5,
        )
        self._overlay_artists.append(dot)

        # Shore-picker overlays (run_centerline_live shore mode).
        # shore_pts = walked bank pixels, drawn as a pink polyline.
        # path_pts  = same points offset into water (the bot's path),
        #             drawn as a cyan polyline.
        # waypoint_px = chosen destination (last path_pts point), drawn
        #               as an orange star.  All in image-space [y, x].
        shore_pts = rec.get("shore_pts")
        if shore_pts:
            sx = [p[1] for p in shore_pts]
            sy = [p[0] for p in shore_pts]
            shore_line = self.ax_img.plot(
                sx, sy, color="#ff33aa", linewidth=2.0, alpha=0.9,
                zorder=4,
            )[0]
            self._overlay_artists.append(shore_line)
        path_pts = rec.get("path_pts")
        if path_pts:
            px = [p[1] for p in path_pts]
            py = [p[0] for p in path_pts]
            path_line = self.ax_img.plot(
                px, py, color="#00ffff", linewidth=1.5, alpha=0.85,
                linestyle=(0, (3, 2)), zorder=4,
            )[0]
            self._overlay_artists.append(path_line)
        wp_px_yx = rec.get("waypoint_px")
        if wp_px_yx is not None:
            wp_star = self.ax_img.scatter(
                [wp_px_yx[1]], [wp_px_yx[0]], s=180, c="#ff9900",
                edgecolors="black", linewidths=1.2, marker="*",
                zorder=7,
            )
            self._overlay_artists.append(wp_star)

        # Tactical waypoint (lat, lon) — convert to current-frame px
        # using ship's own lat/lon and a rough scale.  Draw as a
        # lime green square so it's distinct from the orange reflex star.
        tact_ll = rec.get("tactical_dest_latlon")
        if tact_ll is not None:
            ship_lat = rec.get("lat"); ship_lon = rec.get("lon")
            if ship_lat is not None and ship_lon is not None:
                PX_PER_DEG = 100.0                   # matches tactical + sim canvas
                dlat = tact_ll[0] - ship_lat
                dlon = tact_ll[1] - ship_lon
                # Assume 193x405 minimap, ship at (H//2, W//2) in the crop's coord frame
                H, W = 193, 405
                cy = H // 2; cx = W // 2
                py = cy + int(round(-dlat * PX_PER_DEG))
                px = cx + int(round(dlon * PX_PER_DEG))
                tact_marker = self.ax_img.scatter(
                    [px], [py], s=220, c="#00ff88",
                    edgecolors="black", linewidths=1.5, marker="s",
                    zorder=8,
                )
                self._overlay_artists.append(tact_marker)

        ly = rec.get("lyapunov") or {}

        # 1. Current heading — cyan arrow.  Where the bow is pointing
        # right now; useful as a visual baseline against which to read
        # the waypoint (green) and commanded-turn (orange) arrows.
        hdg_now = rec.get("heading_deg")
        if hdg_now is not None:
            dx, dy = _compass_to_xy(hdg_now, HEADING_ARROW_LEN_PX)
            arrow = self.ax_img.annotate(
                "", xy=(cx + dx, cy + dy), xytext=(cx, cy),
                arrowprops=dict(arrowstyle="-|>", color="#33ddff",
                                lw=2.5, mutation_scale=15),
                annotation_clip=False,
            )
            self._overlay_artists.append(arrow)
            # Tag the arrow with the heading source's short name (e.g. "unet",
            # "ensemble") so it's clear which model produced this direction.
            _src = (rec.get("heading_source") or "").split("(")[0]
            _src = _src.replace("ship_parts_", "")   # "ship_parts_unet" -> "unet"
            _hlbl = f"  hdg {hdg_now:.0f}°" + (f" [{_src}]" if _src else "")
            lbl = self.ax_img.text(
                cx + dx, cy + dy, _hlbl,
                color="#33ddff", fontsize=9, fontweight="bold",
                ha="left", va="center", zorder=6,
            )
            self._overlay_artists.append(lbl)

        # 1b. Model RAW read (yellow) + PCA (magenta).  The yellow arrow is
        # the heading model's OWN direct read — distinct from the picked
        # heading_deg (cyan), which may come from the model OR the motion
        # bearing after correction.  For old traces we read the CNN's raw
        # from `cnn_raw_heading_deg` (the CNN that actually drove the voyage);
        # for current traces (U-Net, no CNN) we re-run the U-Net LIVE here,
        # same as PCA — it's deterministic on these pixels.
        def _yellow_arrow(bearing, label):
            dx, dy = _compass_to_xy(bearing, HEADING_ARROW_LEN_PX * 0.9)
            ox, oy = 3, 3
            arr = self.ax_img.annotate(
                "", xy=(cx + ox + dx, cy + oy + dy),
                xytext=(cx + ox, cy + oy),
                arrowprops=dict(arrowstyle="-|>", color="#ffcc33",
                                lw=1.8, mutation_scale=12, alpha=0.9),
                annotation_clip=False,
            )
            self._overlay_artists.append(arr)
            lb = self.ax_img.text(
                cx + ox + dx * 1.15, cy + oy + dy * 1.15, label,
                color="#ffcc33", fontsize=8, ha="left", va="center", zorder=6,
            )
            self._overlay_artists.append(lb)

        cnn_raw = rec.get("cnn_raw_heading_deg")
        if cnn_raw is not None:
            _yellow_arrow(cnn_raw, f"cnn{cnn_raw:.0f}°")
        else:
            # No CNN in this trace — re-run the U-Net live for the raw read.
            unet = self._get_unet()
            if unet is not None:
                from PIL import Image as _PIL_Image_u

                class _Fu:
                    def __init__(self, mm): self._mm = mm
                    def minimap(self):     return self._mm
                    def full_screen(self): return self._mm
                uh = unet.estimate(_Fu(_PIL_Image_u.fromarray(img)), prior=None)
                if uh.confidence > 0.0:
                    _yellow_arrow(uh.bearing_deg, f"unet{uh.bearing_deg:.0f}°")
        # PCA — live inference (no trace field for its raw output)
        try:
            _, pca = self._get_perception_models()
            if pca is not None:
                from PIL import Image as _PIL_Image
                class _F:
                    def __init__(self, mm): self._mm = mm
                    def minimap(self):     return self._mm
                    def full_screen(self): return self._mm
                pil_img = _PIL_Image.fromarray(img)
                pca_h = pca.estimate(_F(pil_img), prior=None)
                if pca_h.confidence > 0.0:
                    dx, dy = _compass_to_xy(pca_h.bearing_deg,
                                            HEADING_ARROW_LEN_PX * 0.9)
                    ox, oy = -3, 3
                    arr = self.ax_img.annotate(
                        "", xy=(cx + ox + dx, cy + oy + dy),
                        xytext=(cx + ox, cy + oy),
                        arrowprops=dict(arrowstyle="-|>", color="#ff55cc",
                                        lw=1.8, mutation_scale=12,
                                        alpha=0.9),
                        annotation_clip=False,
                    )
                    self._overlay_artists.append(arr)
                    lb = self.ax_img.text(
                        cx + ox + dx * 1.15, cy + oy + dy * 1.15,
                        f"pca{pca_h.bearing_deg:.0f}°",
                        color="#ff55cc", fontsize=8,
                        ha="left", va="center", zorder=6,
                    )
                    self._overlay_artists.append(lb)
        except Exception:
            pass

        # 2. Destination point — green arrow + dot at the wp's actual
        # (lat, lon) position projected onto the minimap.  Earlier this
        # was a bearing-only dot at a fixed pixel radius from the ship,
        # which silently looked the same when the wp was frozen but the
        # bot's position belief moved.  Now the dot is world-anchored:
        # if wp is stale (cached for 5 ticks), the dot stays put and
        # the ship icon visibly moves past it.
        #
        # Conversion uses MINIMAP_PIX_PER_DEG_LAT/LON (calibrated, see
        # constants above) anchored at (cx, cy) = current ship pixel.
        # The wp may project outside the visible minimap crop when the
        # bot moves far from a stale wp; we clip the dot's screen
        # position to the crop bounds and draw an "off-screen" marker
        # at the clipped edge so the direction stays informative.
        wp = ly.get("pp_waypoint")
        wp_b = ly.get("pp_waypoint_bearing")
        cur_lat = rec.get("lat"); cur_lon = rec.get("lon")
        if (wp is not None and cur_lat is not None and cur_lon is not None):
            wp_lat, wp_lon = float(wp[0]), float(wp[1])
            d_lon = wp_lon - float(cur_lon)
            d_lat = wp_lat - float(cur_lat)
            wp_dx = d_lon * MINIMAP_PIX_PER_DEG_LON
            # Image y grows DOWN; latitude north is UP; hence the sign flip.
            wp_dy = -d_lat * MINIMAP_PIX_PER_DEG_LAT
            wp_px, wp_py = cx + wp_dx, cy + wp_dy
            # img is a numpy array (H, W[, 3]).
            crop_h, crop_w = img.shape[:2]
            on_screen = (0 <= wp_px <= crop_w and 0 <= wp_py <= crop_h)
            # Arrow ship → wp, even if wp is off-screen (clamped tip).
            tip_px = max(0, min(crop_w, wp_px))
            tip_py = max(0, min(crop_h, wp_py))
            arrow = self.ax_img.annotate(
                "", xy=(tip_px, tip_py), xytext=(cx, cy),
                arrowprops=dict(arrowstyle="-|>", color="#33ff66",
                                lw=2.5, mutation_scale=15),
                annotation_clip=False,
            )
            self._overlay_artists.append(arrow)
            # Dot ONLY when wp is actually visible on the minimap.
            if on_screen:
                dest_dot = self.ax_img.scatter(
                    [wp_px], [wp_py], s=70, c="#33ff66",
                    edgecolors="black", linewidths=0.8, zorder=6,
                    marker="o",
                )
                self._overlay_artists.append(dest_dot)
                lbl_text = f"  wp ({wp_lat:.3f}, {wp_lon:.3f})"
            else:
                # Off-screen — explicit marker at the clipped edge.
                edge_dot = self.ax_img.scatter(
                    [tip_px], [tip_py], s=70, c="#33ff66",
                    edgecolors="black", linewidths=0.8, zorder=6,
                    marker=">" if wp_dx >= 0 else "<",
                )
                self._overlay_artists.append(edge_dot)
                dist_deg = math.hypot(d_lat, d_lon)
                lbl_text = f"  wp off-map ({dist_deg:.2f}°)"
            lbl = self.ax_img.text(
                tip_px, tip_py, lbl_text,
                color="#33ff66", fontsize=8, fontweight="bold",
                ha="left", va="center", zorder=6,
            )
            self._overlay_artists.append(lbl)

        # 2b. M-line direction — yellow dashed line from ship toward
        # the endpoint.  The M-line is the abstract progress reference
        # (start → endpoint straight line); we render its direction
        # from the current position as a dashed line going to the
        # mini-map edge.  Only drawn when the endpoint is set.
        ep_lat = rec.get("endpoint_lat")
        ep_lon = rec.get("endpoint_lon")
        cur_lat = rec.get("lat")
        cur_lon = rec.get("lon")
        if (ep_lat is not None and ep_lon is not None
                and cur_lat is not None and cur_lon is not None):
            mline_brg = (math.degrees(math.atan2(
                ep_lon - cur_lon, ep_lat - cur_lat,
            )) + 360.0) % 360.0
            # Long enough to reach the image edge regardless of ship
            # position; matplotlib clips automatically.
            mdx, mdy = _compass_to_xy(mline_brg, 600)
            mline = self.ax_img.plot(
                [cx, cx + mdx], [cy, cy + mdy],
                color="#ffe066", linewidth=1.2, linestyle=(0, (4, 3)),
                zorder=4,
            )[0]
            self._overlay_artists.append(mline)
            # Small label near the ship end of the line.
            mlbl_dx, mlbl_dy = _compass_to_xy(mline_brg, 80)
            mlbl = self.ax_img.text(
                cx + mlbl_dx, cy + mlbl_dy,
                f" M-line {mline_brg:.0f}°",
                color="#ffe066", fontsize=8, fontweight="bold",
                ha="left", va="center", zorder=4,
            )
            self._overlay_artists.append(mlbl)

        # 3. Commanded turn — orange arrow.  Only draw if cmd is non-
        # trivial (otherwise the bot is holding course; no turn arrow).
        # §13.28 — suppress the cmd arrow when heading was rejected; the
        # ship is anchored and no command was actually issued this tick.
        cmd = rec.get("commanded_deg_this")
        hdg = rec.get("heading_deg")
        rej = rec.get("heading_rejected")
        if (cmd is not None and hdg is not None and abs(cmd) >= 2
                and not rej):
            new_bearing = (hdg + cmd) % 360
            dx, dy = _compass_to_xy(new_bearing, CMD_ARROW_LEN_PX)
            arrow = self.ax_img.annotate(
                "", xy=(cx + dx, cy + dy), xytext=(cx, cy),
                arrowprops=dict(arrowstyle="-|>", color="#ffaa33",
                                lw=2.5, mutation_scale=15),
                annotation_clip=False,
            )
            self._overlay_artists.append(arrow)
            sign = "+" if cmd > 0 else ""
            lbl = self.ax_img.text(
                cx + dx, cy + dy, f"  cmd {sign}{cmd:.0f}°",
                color="#ffaa33", fontsize=9, fontweight="bold",
                ha="left", va="center", zorder=6,
            )
            self._overlay_artists.append(lbl)

    def _draw_world_view(self, current_tick: int, rec: Optional[dict]):
        """Render the voyage trajectory in world (lat/lon) coordinates.

        Shows everything up to and including `current_tick`:
        - Cyan polyline: the trajectory.
        - Bright dot: current position.
        - Red marker: voyage start.
        - Yellow marker + dashed M-line: endpoint, when set.
        Auto-scales bounds to fit trajectory + endpoint + margin.
        """
        # Clear previous artists from this axis.
        for art in self._world_artists:
            try:
                art.remove()
            except Exception:
                pass
        self._world_artists = []

        # Walk records in tick order up to (and including) current,
        # collecting valid (lat, lon) samples.  Spike-filter: a
        # sample is treated as a bad OCR read when its displacement
        # from the previous accepted sample exceeds a plausible
        # per-tick max.  Bad samples are kept in a separate list so
        # they can be drawn as red markers — visible, but not
        # polluting the trajectory line.
        # Empirically calibrated against an observed Nile voyage:
        # legitimate cruise + cache-then-refresh jumps stay below 1.0
        # (typically ~0.3); known OCR spikes (lon flipping from 30 to
        # 61, for example) clear 14.0.  A 2.0 threshold sits cleanly
        # in the gap.
        MAX_DISP_PER_TICK = 2.0    # lat/lon delta
        # A single jump > MAX is a bad OCR read ONLY if it's isolated.  When
        # the HUD lat/lon freezes for a while then jumps to catch up (or the
        # ship genuinely moved far), the "spike" PERSISTS — subsequent points
        # cluster near the new value, not the stale reference.  So after this
        # many consecutive over-threshold ticks, adopt the new position and
        # reset the reference; otherwise the trajectory caps forever at the
        # pre-jump point (north-of-start positions were dropped — the
        # frozen-then-jumped HUD read capped the plot at the freeze latitude).
        SPIKE_RECOVERY_TICKS = 3
        consec_spikes = 0
        # Corrected trajectory (the "truth" path used by downstream
        # path-comparison + the simulator).  When this tick has no
        # correction, the corrected point IS the raw point.
        lats: list[float] = []
        lons: list[float] = []
        # Raw trajectory — drawn faintly underneath so the corrections
        # are visible by their offset from the raw line.  Only diverges
        # at ticks listed in `self.corrections`.
        raw_lats: list[float] = []
        raw_lons: list[float] = []
        spike_lats: list[float] = []
        spike_lons: list[float] = []
        for t in sorted(self.records.keys()):
            if t > current_tick:
                break
            r = self.records[t]
            lat = r.get("lat"); lon = r.get("lon")
            if lat is None or lon is None:
                continue
            lat = float(lat); lon = float(lon)
            # Pick the corrected value when present, else raw.
            corr = self.corrections.get(t) if self.corrections else None
            c_lat = float(corr["lat"]) if corr else lat
            c_lon = float(corr["lon"]) if corr else lon
            if lats:
                import math as _m
                disp = _m.hypot(c_lat - lats[-1], c_lon - lons[-1])
                if disp > MAX_DISP_PER_TICK:
                    consec_spikes += 1
                    if consec_spikes < SPIKE_RECOVERY_TICKS:
                        # isolated over-threshold read → treat as a spike
                        spike_lats.append(c_lat)
                        spike_lons.append(c_lon)
                        continue
                    # persisted → real move; adopt it and reset the reference
                consec_spikes = 0
            lats.append(c_lat)
            lons.append(c_lon)
            raw_lats.append(lat)
            raw_lons.append(lon)

        if not lats:
            # Nothing to plot yet.  Leave the axis blank.
            self.ax_world.set_xlim(-1, 1)
            self.ax_world.set_ylim(-1, 1)
            return

        ep_lat = rec.get("endpoint_lat") if rec else None
        ep_lon = rec.get("endpoint_lon") if rec else None
        has_endpoint = ep_lat is not None and ep_lon is not None

        # Auto-scale bounds: trajectory + endpoint + waypoint + ends +
        # 5% margin per axis.  Including the wp and the user-marked
        # branch-ends ensures they stay inside the visible axis range
        # even when far from the current trajectory.
        ly_for_bounds = rec.get("lyapunov") or {} if rec else {}
        wp_for_bounds = ly_for_bounds.get("pp_waypoint")
        wp_extra = ([float(wp_for_bounds[1])], [float(wp_for_bounds[0])]) \
            if wp_for_bounds is not None else ([], [])
        ends_lons = [e["lon"] for e in self.ends]
        ends_lats = [e["lat"] for e in self.ends]
        all_lons = (list(lons) + ([ep_lon] if has_endpoint else [])
                    + wp_extra[0] + ends_lons)
        all_lats = (list(lats) + ([ep_lat] if has_endpoint else [])
                    + wp_extra[1] + ends_lats)
        lon_min, lon_max = min(all_lons), max(all_lons)
        lat_min, lat_max = min(all_lats), max(all_lats)
        # Equal aspect by inflating the smaller span — keeps angles
        # honest on the plot.  We pre-equalize the spans HERE rather
        # than letting `set_aspect("equal", adjustable="datalim")` do
        # it, because that path emits a spammy
        # `Ignoring fixed x limits to fulfill fixed data aspect` warning
        # on every redraw (once per tick navigation).  Pre-equalized
        # limits already satisfy equal aspect → matplotlib has nothing
        # to adjust → no warning.
        lon_span = max(lon_max - lon_min, 1e-3)
        lat_span = max(lat_max - lat_min, 1e-3)
        if lon_span > lat_span:
            pad = (lon_span - lat_span) / 2
            lat_min -= pad
            lat_max += pad
        elif lat_span > lon_span:
            pad = (lat_span - lon_span) / 2
            lon_min -= pad
            lon_max += pad
        span = max(lon_max - lon_min, 1e-3)  # equal now
        margin = 0.05 * span
        lon_min -= margin; lon_max += margin
        lat_min -= margin; lat_max += margin
        self.ax_world.set_xlim(lon_min, lon_max)
        self.ax_world.set_ylim(lat_min, lat_max)
        # `adjustable="box"` (instead of `datalim`) so matplotlib never
        # overrides our explicit xlim/ylim — eliminates the "Ignoring
        # fixed x limits to fulfill fixed data aspect" warning that
        # used to fire on every redraw.  We pre-equalised the spans
        # above, so the box adjustment is a no-op.
        self.ax_world.set_aspect("equal", adjustable="box")

        # M-line: dashed yellow from voyage start to endpoint (when set).
        if has_endpoint:
            mline = self.ax_world.plot(
                [lons[0], ep_lon], [lats[0], ep_lat],
                color="#ffe066", linewidth=0.8,
                linestyle=(0, (4, 3)), zorder=2,
            )[0]
            self._world_artists.append(mline)

        # Raw trajectory polyline — drawn UNDER the corrected line in a
        # faded gray so the corrected line stands out where they
        # diverge.  Only visibly different at corrected ticks.
        if self.corrections and len(raw_lons) >= 2:
            raw_traj = self.ax_world.plot(
                raw_lons, raw_lats,
                color="#886666", linewidth=0.8, alpha=0.55,
                linestyle="--", zorder=2,
                label="raw OCR",
            )[0]
            self._world_artists.append(raw_traj)

        # Corrected (canonical) trajectory polyline — bright cyan.
        if len(lons) >= 2:
            traj = self.ax_world.plot(
                lons, lats, color="#33ddff", linewidth=1.2, zorder=3,
                label="corrected" if self.corrections else None,
            )[0]
            self._world_artists.append(traj)

        # OCR spikes — drawn as small red x's so the rejected reads
        # are visible at their reported coords without contaminating
        # the trajectory line.
        if spike_lats:
            spike_dots = self.ax_world.scatter(
                spike_lons, spike_lats, s=24, c="#ff4040",
                marker="x", linewidths=1.0, zorder=4,
            )
            self._world_artists.append(spike_dots)

        # Start marker (red triangle).
        start_dot = self.ax_world.scatter(
            [lons[0]], [lats[0]], s=60, c="#ff5050",
            marker="^", edgecolors="black", linewidths=0.6, zorder=5,
        )
        self._world_artists.append(start_dot)

        # Current position (bright cyan dot, larger than the trajectory line).
        cur_dot = self.ax_world.scatter(
            [lons[-1]], [lats[-1]], s=80, c="#00ffff",
            edgecolors="black", linewidths=0.8, zorder=6,
        )
        self._world_artists.append(cur_dot)

        # Waypoint as a world-anchored location — green dot at the wp's
        # actual (lat, lon).  Complements the bearing-only green dot on
        # the minimap above: when the wp is frozen (e.g. cache held
        # because shore extraction failed for N ticks), the world-view
        # dot stays put while the cyan ship dot moves past it — the
        # stale-wp pattern is visible at a glance.
        ly = rec.get("lyapunov") or {} if rec else {}
        wp = ly.get("pp_waypoint")
        if wp is not None:
            wp_lat, wp_lon = float(wp[0]), float(wp[1])
            wp_dot = self.ax_world.scatter(
                [wp_lon], [wp_lat], s=70, c="#33ff66",
                edgecolors="black", linewidths=0.8, zorder=6,
                marker="o",
            )
            self._world_artists.append(wp_dot)
            # Short link from current pos to wp so the relative
            # direction is obvious even at high zoom.
            link = self.ax_world.plot(
                [lons[-1], wp_lon], [lats[-1], wp_lat],
                color="#33ff66", linewidth=0.6, linestyle=":",
                alpha=0.7, zorder=4,
            )[0]
            self._world_artists.append(link)

        # Per-tick verdict markers (1/2/3 hotkeys → ideal/acceptable/
        # wrong).  Drawn as colored circles ON TOP of the cyan
        # trajectory line so labeled ticks stand out.  These are the
        # ground-truth labels the replay diff will check the bot's
        # recomputed action against.
        if self.labels:
            label_colors = {
                "ideal":      "#33ff66",  # green
                "acceptable": "#ffdd33",  # yellow
                "wrong":      "#ff3344",  # red
            }
            for verdict, color in label_colors.items():
                lats_v: list[float] = []
                lons_v: list[float] = []
                for tick, lbl in self.labels.items():
                    if lbl.get("verdict") != verdict:
                        continue
                    r = self.records.get(tick) or {}
                    lat, lon = r.get("lat"), r.get("lon")
                    if lat is None or lon is None:
                        continue
                    lats_v.append(float(lat))
                    lons_v.append(float(lon))
                if lats_v:
                    dots = self.ax_world.scatter(
                        lons_v, lats_v, s=40, c=color, marker="o",
                        edgecolors="black", linewidths=0.5, zorder=5,
                    )
                    self._world_artists.append(dots)
            # State-verdict markers — diamonds (independent dimension
            # from action verdicts above).  Same color scheme: green=good,
            # red=bad.  Slightly larger so they show up under the action
            # dot when both are set on the same tick.
            state_colors = {"good": "#33ff66", "bad": "#ff3344"}
            for state_v, color in state_colors.items():
                lats_v: list[float] = []
                lons_v: list[float] = []
                for tick, lbl in self.labels.items():
                    if lbl.get("state_verdict") != state_v:
                        continue
                    r = self.records.get(tick) or {}
                    lat, lon = r.get("lat"), r.get("lon")
                    if lat is None or lon is None:
                        continue
                    lats_v.append(float(lat))
                    lons_v.append(float(lon))
                if lats_v:
                    dots = self.ax_world.scatter(
                        lons_v, lats_v, s=70, c=color, marker="D",
                        edgecolors="black", linewidths=0.5, zorder=4,
                    )
                    self._world_artists.append(dots)

        # Branch-end marks (red squares).  These are the user-
        # annotated turn-around points / Trémaux leaf nodes captured
        # in ends.jsonl via the `m` hotkey.  Each mark labelled with
        # its tick number so cross-referencing with the trace is easy.
        if self.ends:
            end_lats = [e["lat"] for e in self.ends]
            end_lons = [e["lon"] for e in self.ends]
            end_dots = self.ax_world.scatter(
                end_lons, end_lats, s=90, c="#ff5050",
                marker="s", edgecolors="black", linewidths=0.8, zorder=7,
            )
            self._world_artists.append(end_dots)
            for e in self.ends:
                lbl = self.ax_world.annotate(
                    f"  end t{e['tick']}",
                    (e["lon"], e["lat"]),
                    color="#ffaaaa", fontsize=8, fontweight="bold",
                    ha="left", va="center", zorder=7,
                )
                self._world_artists.append(lbl)

        # Endpoint marker (yellow star).
        if has_endpoint:
            ep_dot = self.ax_world.scatter(
                [ep_lon], [ep_lat], s=120, c="#ffe066",
                marker="*", edgecolors="black", linewidths=0.6, zorder=5,
            )
            self._world_artists.append(ep_dot)

        # Title summarises mode + scalar progress info when endpoint set.
        if has_endpoint:
            import math as _math
            d = _math.hypot(ep_lat - lats[-1], ep_lon - lons[-1])
            title = f"world view  •  endpoint d = {d:.2f}"
        else:
            title = "world view  •  exploration mode (no endpoint)"
        self.ax_world.set_title(title, color="#cccccc", fontsize=9)

    def _save_view(self):
        tick = self.ticks[self.idx]
        out = Path("/tmp") / (
            f"tick_viewer_{self.session_dir.name}_t{tick:04d}.png")
        try:
            self.fig.savefig(
                str(out),
                facecolor=self.fig.get_facecolor(),
                dpi=120,
            )
            print(f"[tick_viewer] saved {out}")
        except Exception as e:
            print(f"[tick_viewer] save failed: {e}")

    def _open_folder_dialog(self):
        """Pop a native folder picker and load that session.  Uses
        macOS osascript when available; falls back to a stdin prompt
        on other platforms."""
        chosen: Optional[Path] = None
        if sys.platform == "darwin":
            try:
                # Default starting location: the parent of the current
                # session so the next pick is one click away.
                default = str(self.session_dir.parent)
                script = (
                    f'POSIX path of (choose folder with prompt '
                    f'"Select hug_debug session folder" '
                    f'default location POSIX file "{default}")'
                )
                result = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode == 0:
                    chosen = Path(result.stdout.strip())
            except Exception as e:
                print(f"[tick_viewer] folder dialog failed: {e}")
        else:
            try:
                path = input(
                    "Enter session directory path (or blank to cancel): "
                ).strip()
                if path:
                    chosen = Path(path)
            except Exception:
                return
        if chosen is None or not chosen.is_dir():
            return
        self._load_session(chosen)

    def _load_session(self, session_dir: Path):
        self.session_dir = session_dir
        self.records = _load_trace(session_dir)
        self.corrections = _load_corrections(session_dir)
        self.frame_paths = {}
        for p in sorted(session_dir.glob("tick_*.png")):
            try:
                t = int(p.stem.split("_")[-1])
                self.frame_paths[t] = p
            except ValueError:
                pass
        all_ticks = sorted(
            set(self.records.keys()) | set(self.frame_paths.keys()))
        if not all_ticks:
            print(f"[tick_viewer] no ticks found in {session_dir}")
            return
        self.ticks = all_ticks
        self.idx = 0
        # Clear any stale overlays before redraw.
        for art in self._overlay_artists:
            try:
                art.remove()
            except Exception:
                pass
        self._overlay_artists = []
        self.fig.canvas.manager.set_window_title(
            f"Tick Viewer — {session_dir.name}")
        self._show_current()

    def _update_status(self):
        n = len(self.ticks)
        tick = self.ticks[self.idx]
        ap = (f"AUTO @ {self.autoplay_ms}ms"
              if self.autoplay else "paused")
        bank = "bank ON" if self._show_bank else "bank OFF"
        v10 = "V10 ON" if self._show_v10_bank else "V10 OFF"
        self._status_artist.set_text(
            f"tick {tick}  ({self.idx + 1}/{n})  [{ap}]  "
            f"[{bank} magenta]  [{v10} yellow]   "
            f"CNN: from trace   "
            f"←/→ step  space play  +/- speed  s save  o open  "
            f"b bank  v V10  Home/End jump  q quit"
        )
        self.fig.canvas.draw_idle()

    def run(self):
        plt.show()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", type=Path,
                    help="hug_debug_YYYYMMDD_HHMMSS directory")
    ap.add_argument("--start-tick", type=int, default=1,
                    help="tick to display first (default 1)")
    ap.add_argument("--compare", type=Path, default=None,
                    help="Open a second viewer window for this session "
                         "side-by-side. Each window navigates "
                         "independently — click into one to give it "
                         "keyboard focus, then arrows / g / etc. "
                         "control that viewer.  Each window writes its "
                         "own labels.jsonl / ends.jsonl to its own dir.")
    ap.add_argument("--compare-start-tick", type=int, default=None,
                    help="tick to display first in the --compare viewer; "
                         "defaults to --start-tick.")
    ap.add_argument("--review-mode", action="store_true",
                    help="Walk through only the ticks flagged as bad by "
                         "the dataset builder (valid=False in quality.jsonl). "
                         "Lets you override per-tick: i=include, x=exclude, "
                         "./0=clear override.  Overrides persist to "
                         "labels.jsonl as training_include and the BC "
                         "trainer honors them over the auto rule.")
    ap.add_argument("--ckpt", type=Path, default=None,
                    help="CNN checkpoint override (only affects PCA-adjacent "
                         "internals).  The primary CNN arrow is now read "
                         "from the trace's cnn_raw_heading_deg field — "
                         "the actual model that drove the voyage — so this "
                         "flag is rarely needed.")
    args = ap.parse_args()
    if not args.session_dir.is_dir():
        raise SystemExit(f"not a directory: {args.session_dir}")
    # Startup banner — clarifies the data source for CNN overlay.
    print()
    print("┌──────────────────────────────────────────────────────────────────┐")
    print("│  tick_viewer overlays:                                           │")
    print("│    yellow arrow  = PRIMARY CNN (from trace, ran during voyage)   │")
    print("│    magenta arrow = PCA (re-run live on the frame)                │")
    print("│    right panel   = primary + shadow CNN raw values from trace    │")
    print("└──────────────────────────────────────────────────────────────────┘")
    print()
    primary = TickViewer(args.session_dir, start_tick=args.start_tick,
                         review_mode=args.review_mode,
                         cnn_ckpt=args.ckpt)
    if args.compare is not None:
        if not args.compare.is_dir():
            raise SystemExit(f"not a directory: {args.compare}")
        compare_tick = (args.compare_start_tick
                        if args.compare_start_tick is not None
                        else args.start_tick)
        secondary = TickViewer(args.compare, start_tick=compare_tick,
                               review_mode=args.review_mode,
                               cnn_ckpt=args.ckpt)
        # Best-effort: nudge windows apart so they don't fully overlap.
        # Backend-specific; failures are silent.
        try:
            mp = primary.fig.canvas.manager
            ms = secondary.fig.canvas.manager
            for mngr, x in ((mp, 40), (ms, 760)):
                # Order of attempts handles Tk, Qt, osx backends.
                try:
                    mngr.window.wm_geometry(f"+{x}+40")
                except Exception:
                    try:
                        mngr.window.move(x, 40)
                    except Exception:
                        pass
        except Exception:
            pass
    plt.show()


if __name__ == "__main__":
    main()
