"""Unattended sea-view capture loop.

Run from project root.  Single Bash entry point (matches Bash(python:*)),
no further prompts needed during the run.

Usage:
    python tools/sail_capture.py --session calais_south_v2 \
        --duration 600 --interval-min 4 --interval-max 7

Behaviour:
  1. Verifies the bot is on the sea view.  Aborts otherwise.
  2. If the ship is not under sail, taps the rudder once to start sailing.
  3. Loops:  capture → save → write metadata.json → sleep jittered interval.
  4. After --enter-after seconds elapsed (default 420 = 7 min), each tick
     runs OmniParser and looks for an "Enter New City" / "Enter New
     Village" label.  If found, taps it once, waits, and exits.
  5. Hard stop at --duration seconds (default 600 = 10 min) regardless.

Stops on SIGINT/SIGTERM.  Writes metadata.json continuously so the
supervisor labeller can see the session the moment the script ends.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capture.adb_capture import capture_screen           # noqa: E402
from actions.adb_actions import tap                       # noqa: E402
from actions.sea_actions import sail_start, turn_left, turn_right, is_ship_moving  # noqa: E402
import math                                                # noqa: E402
from brain.perceive import perceive                       # noqa: E402


_stop = False
def _on_sig(*_):
    global _stop
    _stop = True
signal.signal(signal.SIGINT, _on_sig)
signal.signal(signal.SIGTERM, _on_sig)


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _write_metadata(session_id: str, session_dir: Path,
                    started_at: str, frames: list[dict]) -> None:
    meta = {
        "session_id": session_id,
        "started_at": started_at,
        "mode":       "manual",
        "frames":     frames,
        "ended_at":   frames[-1]["timestamp"] if frames else started_at,
        "total_saved": len(frames),
    }
    (session_dir / "metadata.json").write_text(json.dumps(meta, indent=2))


def _find_enter_label(img) -> tuple[int, int] | None:
    """Return (cx, cy) of an 'Enter New City/Village' label, or None."""
    from vision.omniparser import parse_fast_cached
    elems = parse_fast_cached(img)
    for e in elems:
        label = (e.label or "").strip().lower()
        if not label:
            continue
        if "enter new city" in label or "enter new village" in label:
            return (e.cx, e.cy)
    return None


def _read_speed(img) -> float | None:
    """Extract current sailing speed from the top-right vertical stats column.

    The HUD has a small vertical list to the LEFT of the mini map; the
    second entry is the ship's speed in knots.  Top speed is ~27 kn,
    so anything in 0..30 is plausible.

    Uses OmniParser text elements (already running in the loop) and
    picks the second numeric-looking item in the zone.  Returns None
    if it can't find a sensible reading.
    """
    from vision.omniparser import parse_fast_cached
    elems = parse_fast_cached(img)
    candidates = []
    for e in elems:
        # OmniParser classifies the numeric stat values as 'button',
        # not 'text' — see _read_latlon comment.  Accept both.
        if e.element_type not in ("text", "button"):
            continue
        if not (1900 <= e.cx <= 2400 and 80 <= e.cy <= 420):
            continue
        text = (e.label or "").strip().replace(",", "")
        try:
            v = float(text)
        except ValueError:
            continue
        if 0 <= v <= 50:
            candidates.append((e.cy, v))
    if not candidates:
        return None
    candidates.sort()
    # second-from-top in the column (first is usually supply icon count;
    # second is the speed value).  Fall back to first if only one found.
    return candidates[1][1] if len(candidates) >= 2 else candidates[0][1]


_LATLON_RE = re.compile(r"^\s*(-?\d{1,3}\.\d{1,2})\s*,\s*(-?\d{1,3}\.\d{1,2})\s*$")


def _heading_deg(p0: tuple[float, float], p1: tuple[float, float]) -> float | None:
    """Compass heading (0=N, 90=E, 180=S, 270=W) from p0 → p1, in degrees.

    p0, p1 are (lat, lon).  Returns None if the movement is below the
    noise floor (< ~1km, where OCR jitter can dominate).
    """
    dlat = p1[0] - p0[0]
    dlon = p1[1] - p0[1]
    if abs(dlat) + abs(dlon) < 0.01:
        return None
    deg = math.degrees(math.atan2(dlon, dlat))
    return (deg + 360.0) % 360.0


def _heading_error(current_h: float, target_h: float) -> float:
    """Signed shortest turn from current to target, in (-180, 180]
    degrees.  Positive = turn right (clockwise); negative = turn left.
    """
    return ((target_h - current_h + 540.0) % 360.0) - 180.0


# Mini-map crop covering the lat/lon line beneath it.  Generous so the
# line is captured whether it sits at cy=306 or cy=362.
_MINIMAP_LATLON_CROP = (2050, 250, 2400, 410)   # (l, t, r, b) in 2400×1080


def _lenient_parse_minimap_latlon(text: str) -> tuple[float, float] | None:
    """Parse 'NN.NN,±NN.NN' from noisy mini-map OCR output.

    The mini-map readout is small white text often overlapped by:
      - the player ship icon (adds a stray leading digit, e.g.
        '50.93,1.20' → OCR reads '750,93,1.20')
      - the season tab bar ('Summer Jul Night' joined into the same string)
      - decimal-comma vs decimal-period ambiguity

    Strategy: find every 'd+[.,]d+' run, convert commas to periods, then
    try each consecutive (lat, lon) pair.  When the first value is out of
    range (|lat| > 90), strip 100s digits one at a time to recover the
    real value.
    """
    import re as _re
    numbers = _re.findall(r"-?\d+[.,]\d+", text or "")
    floats: list[float] = []
    for n in numbers:
        try:
            floats.append(float(n.replace(",", ".")))
        except ValueError:
            continue
    for i in range(len(floats) - 1):
        lat_cand = floats[i]
        lon_cand = floats[i + 1]
        if not (-180.0 <= lon_cand <= 180.0):
            continue
        # Try the raw lat, then progressively drop leading 100s digits
        # to handle the icon-mast '7' prefix.
        attempts = [lat_cand]
        v = lat_cand
        while abs(v) > 99.99:
            v = v - (int(v / 100)) * 100
            attempts.append(v)
        for lat in attempts:
            if -90.0 <= lat <= 90.0:
                return (lat, lon_cand)
    return None


def _read_latlon_via_ocr(img) -> tuple[float, float] | None:
    """Fallback: dedicated EasyOCR pass on the mini-map crop.

    Lat/lon text on the mini-map is small white-on-grey and OmniParser's
    element detector often misses it.  We try the strict parser first
    (handles clean text), then the lenient one (recovers icon-mangled
    cases like '750,93,1.20' → '50.93,1.20').
    """
    try:
        import numpy as np
        from actions.water_tap import _get_reader, _parse_latlon_from_text
    except Exception:
        return None
    # No contrast boost — earlier experiments showed autocontrast washed
    # out the bright text against the bright land mass.  Raw crop works
    # better; the lenient parser handles the OCR noise downstream.
    crop = img.crop(_MINIMAP_LATLON_CROP)
    arr = np.asarray(crop)
    try:
        raw = _get_reader().readtext(arr, detail=1)
    except Exception:
        return None
    tokens = [text for _, text, conf in raw if conf >= 0.10]
    joined = " ".join(tokens)
    return _parse_latlon_from_text(joined) or _lenient_parse_minimap_latlon(joined)


def _read_latlon(img) -> tuple[float, float] | None:
    """Read the ship's lat/lon from under the mini-map.

    Two-stage:
      1. Fast OmniParser pass — uses the already-cached element list and
         picks up the readout when it survives the detector.
      2. EasyOCR fallback on a tight crop with contrast boost — handles
         the low-contrast frames where OmniParser missed the line.
    """
    from vision.omniparser import parse_fast_cached
    elems = parse_fast_cached(img)
    for e in elems:
        if e.element_type not in ("text", "button"):
            continue
        if not (1800 <= e.cx <= 2400 and 100 <= e.cy <= 600):
            continue
        m = _LATLON_RE.match((e.label or "").strip())
        if not m:
            continue
        try:
            return float(m.group(1)), float(m.group(2))
        except ValueError:
            continue
    # OmniParser missed — try dedicated OCR with contrast boost.
    return _read_latlon_via_ocr(img)


_NON_PORT_WORDS = {
    # season / time-of-day tabs at the top of the right panel
    "summer", "winter", "spring", "autumn", "fall",
    "jan", "feb", "mar", "apr", "may", "jun", "jul",
    "aug", "sep", "oct", "nov", "dec",
    "day", "night", "daytime", "morning", "evening", "dusk", "dawn",
    # right-panel headers / buttons that aren't ports
    "use repel", "repel support owned", "repel support used",
    "pass", "approx",
    # tabs
    "tasks", "ports", "fleets", "ship status",
}


def _nearest_port_distance(img) -> tuple[str | None, float | None]:
    """Return (port_name, distance_km) of the closest entry in the
    right-panel destinations list, or (None, None) on failure.

    Uses vision.destinations_panel.read_destinations and then drops
    rows whose 'name' is actually a tab label, season/time word, or
    starts with 'Approx' (OCR misparse).
    """
    try:
        from vision.destinations_panel import read_destinations
        rows = read_destinations(img)
    except Exception:
        return None, None

    def _ok(name: str, dist: float | None) -> bool:
        if dist is None:
            return False
        n = (name or "").strip()
        if not n or n == "???":
            return False
        nl = n.lower()
        if nl in _NON_PORT_WORDS:
            return False
        if nl.startswith("approx"):
            return False
        if any(part in nl for part in ("|", "ship status")):
            return False
        # require at least 3 letters — filters '0.0', 'kn', symbols
        if sum(c.isalpha() for c in n) < 3:
            return False
        return True

    valid = [r for r in rows if _ok(r.name, r.distance_km)]
    if not valid:
        return None, None
    nearest = min(valid, key=lambda r: r.distance_km)
    return nearest.name, nearest.distance_km


def _land_visible_on_left(img) -> bool:
    """Cheap heuristic: is there a meaningful coast on the LEFT half?

    Crops the upper-left chunk of the sea view (avoiding the top HUD and
    the ship in the centre), converts to HSV and counts pixels that
    look like land — coloured (S > 0.18) AND not in the deep-blue water
    range (hue < 180° or > 240°).  If more than ~5% of the cropped area
    is land-coloured, return True.

    This is intentionally coarse — it only needs to answer "should the
    ship keep turning left to find shore, or stay the course?".  Land
    in mist, far horizons, and night scenes will all sometimes mislabel;
    the steering policy treats it as a soft signal and only nudges with
    a single tap per decision cycle.
    """
    from PIL import Image
    import numpy as np

    w, h = img.size
    # upper-left region: x in [0.02..0.45], y in [0.10..0.55]
    crop = img.crop((
        int(w * 0.02), int(h * 0.10),
        int(w * 0.45), int(h * 0.55),
    )).convert("RGB")
    arr = np.asarray(crop).astype(np.float32) / 255.0
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mx, mn = arr.max(axis=-1), arr.min(axis=-1)
    v = mx
    s = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)

    # hue in 0..360
    rc = (mx - r) / np.maximum(mx - mn, 1e-6)
    gc = (mx - g) / np.maximum(mx - mn, 1e-6)
    bc = (mx - b) / np.maximum(mx - mn, 1e-6)
    h_arr = np.zeros_like(r)
    h_arr = np.where(r == mx, bc - gc, h_arr)
    h_arr = np.where(g == mx, 2.0 + rc - bc, h_arr)
    h_arr = np.where(b == mx, 4.0 + gc - rc, h_arr)
    h_arr = (h_arr * 60.0) % 360.0

    # "land-like" = saturated AND outside the deep-water blue band
    saturated = s > 0.18
    water_like = (h_arr >= 180.0) & (h_arr <= 240.0)
    bright = v > 0.18
    land = saturated & (~water_like) & bright
    ratio = float(land.mean())
    return ratio > 0.05


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True,
                    help="session id suffix (full id = 'YYYY-MM-DD_<session>')")
    ap.add_argument("--duration",      type=int, default=600,
                    help="hard stop after N seconds (default 600)")
    ap.add_argument("--enter-after",   type=int, default=420,
                    help="only attempt to enter a city after N seconds (default 420)")
    ap.add_argument("--interval-min",  type=float, default=4.0)
    ap.add_argument("--interval-max",  type=float, default=7.0)
    ap.add_argument("--no-start-sail", action="store_true",
                    help="skip the sail_start() bring-up step")
    ap.add_argument("--steer-policy",
                    choices=["none", "hug-coast-left", "coast-trend", "latlon-south"],
                    default="none",
                    help="post-startup steering.  'none' = pure capture; "
                         "'hug-coast-left' = tap left when no land visible "
                         "on the upper-left of the frame; "
                         "'coast-trend' = read nearest-port distance from the "
                         "right panel and tap left when the distance is "
                         "increasing tick-over-tick (going wrong way); "
                         "'latlon-south' = read ship lat/lon under the mini-map "
                         "and tap left when latitude is not decreasing (= not "
                         "heading south).")
    ap.add_argument("--steer-every", type=int, default=2,
                    help="evaluate steering every N captures (default 2, "
                         "≈ every 8-14s with default capture interval).")
    ap.add_argument("--steer-taps",  type=int, default=3,
                    help="taps per steering decision (default 3 — a single "
                         "tap is too gentle at 27 kn top speed).")
    args = ap.parse_args()

    date = datetime.now().strftime("%Y-%m-%d")
    session_id = f"{date}_{args.session}"
    session_dir = REPO / "data" / "sessions" / session_id
    frames_dir  = session_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. verify we're at sea ─────────────────────────────────────────
    state = perceive()
    print(f"[{_ts()}] start state: {state.state!r}  detail={state.detail[:80]!r}")
    if state.state not in ("sea", "sea_cinematic"):
        print(f"[{_ts()}] not at sea — abort")
        return 2

    # ── 2. ensure ship is sailing ──────────────────────────────────────
    if not args.no_start_sail:
        moving = is_ship_moving()
        print(f"[{_ts()}] is_ship_moving={moving!r}")
        if moving is not True:
            print(f"[{_ts()}] tapping rudder to start sailing")
            sail_start()
            time.sleep(2.0)
            moving = is_ship_moving()
            print(f"[{_ts()}] after sail_start: is_ship_moving={moving!r}")

    # ── 3. capture loop ────────────────────────────────────────────────
    start_t = time.monotonic()
    started_at = datetime.now().isoformat(timespec="microseconds")
    frames: list[dict] = []
    frame_idx = 0
    next_capture = time.monotonic()
    entered = False
    _coast_trend_prev_dist: float | None = None
    # latlon-south state.  We need the two most-recent positions to
    # compute the current heading, plus a running estimate of how many
    # degrees of heading-change a single L/R tap produces (learned
    # over the run by comparing heading before vs. after taps).
    _ll_history: list[tuple[float, float]] = []
    _last_tap_count: int = 0
    _last_tap_dir: str | None = None  # 'left' / 'right' / None
    _last_heading_before_tap: float | None = None
    _deg_per_tap_est: float = 5.0   # initial guess; gets refined

    while not _stop:
        elapsed = time.monotonic() - start_t
        if elapsed >= args.duration:
            print(f"[{_ts()}] duration reached ({elapsed:.0f}s) — stopping")
            break
        if time.monotonic() < next_capture:
            time.sleep(min(0.5, next_capture - time.monotonic()))
            continue

        # capture
        try:
            img = capture_screen()
        except Exception as e:
            print(f"[{_ts()}] capture failed: {e!r}")
            next_capture = time.monotonic() + 2.0
            continue

        ts_short = datetime.now().strftime("%H%M%S")
        fname = f"{frame_idx:04d}_{ts_short}.png"
        try:
            img.save(frames_dir / fname)
        except Exception as e:
            print(f"[{_ts()}] save failed: {e!r}")
            next_capture = time.monotonic() + 2.0
            continue

        frames.append({
            "file":      fname,
            "timestamp": datetime.now().isoformat(timespec="microseconds"),
        })
        frame_idx += 1
        # write metadata each tick so labeler can see in-progress
        _write_metadata(session_id, session_dir, started_at, frames)

        # per-tick speed log (best-effort, never fatal)
        try:
            spd = _read_speed(img)
            if spd is not None:
                print(f"[{_ts()}] speed: {spd:.1f} kn")
        except Exception:
            pass

        # steering policy — runs only AFTER frame is captured & saved
        if args.steer_policy != "none" \
                and frame_idx > 0 \
                and frame_idx % args.steer_every == 0:
            try:
                if args.steer_policy == "hug-coast-left":
                    land_left = _land_visible_on_left(img)
                    if not land_left:
                        print(f"[{_ts()}] no land on left — turn_left × {args.steer_taps}")
                        turn_left(taps=args.steer_taps)
                    else:
                        print(f"[{_ts()}] land visible on left — stay course")

                elif args.steer_policy == "latlon-south":
                    ll = _read_latlon(img)
                    if ll is None:
                        print(f"[{_ts()}] latlon-south: lat/lon unreadable — skip")
                    else:
                        _ll_history.append(ll)
                        if len(_ll_history) > 6:
                            _ll_history = _ll_history[-6:]

                        if len(_ll_history) < 2:
                            print(f"[{_ts()}] latlon-south: pos=({ll[0]:.2f},{ll[1]:.2f}) (need 2 samples to compute heading)")
                        else:
                            cur_h = _heading_deg(_ll_history[-2], _ll_history[-1])
                            if cur_h is None:
                                print(f"[{_ts()}] latlon-south: pos=({ll[0]:.2f},{ll[1]:.2f}) — movement too small to read heading")
                            else:
                                # Calibrate degrees-per-tap if our previous decision applied taps.
                                if _last_tap_count > 0 and _last_heading_before_tap is not None:
                                    obs_change = _heading_error(_last_heading_before_tap, cur_h)
                                    expected_sign = -1 if _last_tap_dir == "left" else 1
                                    # Only update if the observed turn has the correct sign
                                    # (otherwise drift dominated and the sample is unreliable).
                                    if (obs_change * expected_sign) > 0:
                                        new_est = abs(obs_change) / _last_tap_count
                                        # exponential moving average
                                        _deg_per_tap_est = 0.7 * _deg_per_tap_est + 0.3 * new_est
                                        print(f"[{_ts()}] latlon-south: calibration — {_last_tap_count} {_last_tap_dir}-taps → {obs_change:+.1f}° (est now {_deg_per_tap_est:.1f}°/tap)")
                                _last_tap_count = 0
                                _last_tap_dir = None
                                _last_heading_before_tap = None

                                target_h = 180.0    # south
                                err = _heading_error(cur_h, target_h)
                                # 10° dead-zone keeps us from oscillating
                                if abs(err) < 10.0:
                                    print(f"[{_ts()}] latlon-south: pos=({ll[0]:.2f},{ll[1]:.2f}) heading={cur_h:.0f}° err={err:+.0f}° — on course")
                                else:
                                    n = max(1, min(6, int(round(abs(err) / max(_deg_per_tap_est, 1.0)))))
                                    if err < 0:
                                        print(f"[{_ts()}] latlon-south: pos=({ll[0]:.2f},{ll[1]:.2f}) heading={cur_h:.0f}° err={err:+.0f}° — turn_left × {n} (est {_deg_per_tap_est:.1f}°/tap)")
                                        turn_left(taps=n)
                                        _last_tap_dir = "left"
                                    else:
                                        print(f"[{_ts()}] latlon-south: pos=({ll[0]:.2f},{ll[1]:.2f}) heading={cur_h:.0f}° err={err:+.0f}° — turn_right × {n} (est {_deg_per_tap_est:.1f}°/tap)")
                                        turn_right(taps=n)
                                        _last_tap_dir = "right"
                                    _last_tap_count = n
                                    _last_heading_before_tap = cur_h

                elif args.steer_policy == "coast-trend":
                    name, dist = _nearest_port_distance(img)
                    if name is None:
                        print(f"[{_ts()}] coast-trend: panel unreadable — skip")
                    else:
                        prev = _coast_trend_prev_dist
                        if prev is None:
                            print(f"[{_ts()}] coast-trend: nearest={name} {dist:.1f}km (baseline)")
                        else:
                            delta = dist - prev
                            sign = "+" if delta >= 0 else ""
                            if delta > 5.0:        # ≥5 km farther → going wrong way
                                print(f"[{_ts()}] coast-trend: nearest={name} {dist:.1f}km ({sign}{delta:.1f}) — turn_left × {args.steer_taps}")
                                turn_left(taps=args.steer_taps)
                            else:
                                print(f"[{_ts()}] coast-trend: nearest={name} {dist:.1f}km ({sign}{delta:.1f}) — stay course")
                        _coast_trend_prev_dist = dist  # update for next decision
            except Exception as e:
                print(f"[{_ts()}] steering check failed: {e!r}")

        # post-7min: look for "Enter New City/Village" label
        if elapsed >= args.enter_after:
            pos = _find_enter_label(img)
            if pos is not None:
                cx, cy = pos
                print(f"[{_ts()}] Enter label found at ({cx},{cy}) — tapping")
                tap(cx, cy)
                time.sleep(3.0)
                entered = True
                # capture the post-tap frame too so the loading screen is on record
                try:
                    ts2 = datetime.now().strftime("%H%M%S")
                    fname2 = f"{frame_idx:04d}_{ts2}_AFTER_ENTER.png"
                    capture_screen().save(frames_dir / fname2)
                    frames.append({
                        "file":      fname2,
                        "timestamp": datetime.now().isoformat(timespec="microseconds"),
                    })
                    frame_idx += 1
                    _write_metadata(session_id, session_dir, started_at, frames)
                except Exception as e:
                    print(f"[{_ts()}] post-enter capture failed: {e!r}")
                break

        delay = random.uniform(args.interval_min, args.interval_max)
        next_capture = time.monotonic() + delay
        print(f"[{_ts()}] {frame_idx:3d}  {fname}  (+{delay:.1f}s)  elapsed={elapsed:5.0f}s")

    _write_metadata(session_id, session_dir, started_at, frames)
    final_elapsed = time.monotonic() - start_t
    print(f"[{_ts()}] done.  frames={len(frames)}  elapsed={final_elapsed:.0f}s  entered={entered}")
    print(f"[{_ts()}] session dir: {session_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
