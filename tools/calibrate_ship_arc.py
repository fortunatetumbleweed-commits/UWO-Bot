"""Calibrate the ship arc model — linear acceleration + turn-rate-vs-speed.

The hug-shore policy assumes a constant rotation rate (~120°/sec) and
ignores forward distance.  Both assumptions break in tight quarters:
- The ship slows during turns, so accumulated rotation per tick varies.
- The ship cannot rotate without translating — every turn is an arc.

A "stop-then-arc" pattern (Phase 6 of the steering plan) exploits the
fact that arc radius shrinks toward zero as forward speed shrinks, so
restarting with rudder pre-applied gives a tight turn.  To use it we
need calibrated profiles for:

  (1) Linear acceleration from rest:
          v(t) = v_max × (1 − exp(−t / τ_v))
      Yields τ_v (time constant) and v_max (top speed in knots).

  (2) Turn rate as a function of forward speed (degrees per second
      observed while a steering arrow is held).  Modelled as
          dθ/dt = a + b × v
      where typically b < 0 (faster ships turn at a *lower* angular
      rate per unit time — wider arcs).

Usage:
    # 1. Drive the calibration maneuvers and log every tick:
    python tools/calibrate_ship_arc.py --run --duration-s 60 \\
        --out data/calibration/arc_$(date +%Y%m%d_%H%M%S).jsonl

    # 2. Fit the curves and print constants ready to paste into
    #    brain/goals/hug_shore.py:
    python tools/calibrate_ship_arc.py --fit data/calibration/arc_*.jsonl

The maneuver sequence in --run mode:

    A.  sail_stop, wait until speed_kt == 0
    B.  sail_start, log speed for ACCEL_LOG_S seconds (no rudder)
        → linear-acceleration profile
    C.  Hold rudder right for TURN_HOLD_S seconds (at top speed)
        → turn rate at v_max
    D.  sail_stop, wait, sail_start WITH rudder already held
        → turn rate during low-speed acceleration

C and D give us two (speed, turn_rate) points to fit dθ/dt = a + b×v.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ── Maneuver timing ────────────────────────────────────────────────────
ACCEL_LOG_S       = 12.0    # log accel for this long after sail_start
STOP_WAIT_S       = 15.0    # wait at sail_stop before declaring "stopped"
HOLD_SEGMENT_MS   = 3500    # single hold; `_do_hold` rejects >4000 ms
HOLD_INTER_GAP_S  = 0.4     # gap between chained holds — anti-cheat safe
TURN_SEGMENTS_PER_PHASE = 3   # chained holds → ~3 × 3.5s = 10.5s of turn
SAMPLE_INTERVAL_S = 0.1     # mini-map only sampling cadence
HEAVY_HUD_AT_BOUNDARIES_ONLY = True


@dataclass
class Sample:
    t_s: float           # seconds since maneuver start
    phase: str           # "stopped" | "accel" | "turn_at_speed" | "start_w_rudder"
    speed_kt: Optional[float]
    heading_deg: Optional[float]
    lat: Optional[float]
    lon: Optional[float]


# ── Run mode ───────────────────────────────────────────────────────────

def run_calibration(out_path: Path, total_duration_s: float,
                    save_crops: bool = True) -> None:
    """Drive the calibration maneuvers via ADB primitives and log
    one Sample row per SAMPLE_INTERVAL_S to `out_path` (JSONL).

    When `save_crops=True`, also dump:
      - the full frame
      - the speed crop (what `read_speed` is OCR'ing)
      - the mini-map crop (what `read_navigation_view` ingests)
    to `<out_path-stem>/sample_<phase>_<idx>.png`.  Lets you confirm
    after the run that the readers were looking at the right pixels.
    """
    from capture.adb_capture import capture_screen
    from actions.sea_actions import sail_start, sail_stop, hold_right
    from vision.sea_hud import read_speed, read_latlon, SPEED_CROP, LATLON_CROP
    from vision.minimap_navigation_view import read_navigation_view, MINIMAP_CROP

    out_path.parent.mkdir(parents=True, exist_ok=True)
    crops_dir = out_path.with_suffix("") / "crops" if save_crops else None
    if crops_dir is not None:
        crops_dir.mkdir(parents=True, exist_ok=True)
    fp = open(out_path, "w")
    sample_idx = [0]

    def sample(phase: str, t_origin: float, with_hud: bool = False) -> Sample:
        """Capture one sample.  `with_hud=True` runs read_speed +
        read_latlon (both EasyOCR-based now — fast, ~100 ms each).
        Default (`with_hud=True`) skips HUD and only logs heading
        from the mini-map (≈ 1.5 s capture)."""
        frame = capture_screen()
        nav = read_navigation_view(frame)
        spd = ll = None
        if with_hud:
            try: spd = read_speed(frame)
            except Exception: pass
            try: ll = read_latlon(frame)
            except Exception: pass
        s = Sample(
            t_s=time.monotonic() - t_origin,
            phase=phase,
            speed_kt=spd,
            heading_deg=getattr(nav, "ship_heading_deg", None),
            lat=ll[0] if ll else None,
            lon=ll[1] if ll else None,
        )
        fp.write(json.dumps(asdict(s)) + "\n")
        fp.flush()
        # Save inspection crops
        if crops_dir is not None:
            idx = sample_idx[0]
            sample_idx[0] += 1
            stem = f"{idx:03d}_{phase}"
            try:
                frame.save(crops_dir / f"{stem}_full.png")
                frame.crop(SPEED_CROP).save(crops_dir / f"{stem}_speed.png")
                frame.crop(LATLON_CROP).save(crops_dir / f"{stem}_latlon.png")
                frame.crop(MINIMAP_CROP).save(crops_dir / f"{stem}_minimap.png")
            except Exception as e:
                print(f"[calibrate] crop save failed: {e}")
        hud_tag = " [HUD]" if with_hud else ""
        print(f"[calibrate] {phase:18s} t={s.t_s:5.1f}s  "
              f"v={spd}  hdg={s.heading_deg}  ll=({s.lat},{s.lon}){hud_tag}")
        return s

    print("[calibrate] === A. sail_stop and wait for speed=0 ===")
    sail_stop()
    print(f"[calibrate] sleeping {STOP_WAIT_S}s for ship to fully stop…")
    time.sleep(STOP_WAIT_S)
    sample("stopped_boundary", time.monotonic(), with_hud=True)

    print("[calibrate] === B. sail_start, log linear accel ===")
    sail_start()
    # HUD reads are now cheap (~100 ms) — enable on every sample so we
    # see speed track from 0 → v_max through the acceleration curve.
    t0 = time.monotonic()
    while time.monotonic() - t0 < ACCEL_LOG_S:
        sample("accel", t0, with_hud=True)
        time.sleep(SAMPLE_INTERVAL_S)

    print("[calibrate] === C. chained holds rudder right at top speed ===")
    # `_do_hold` rejects > 4000 ms — chain several segments instead.
    t_phase_start = time.monotonic()
    sample("turn_pre", t_phase_start, with_hud=True)
    for seg in range(TURN_SEGMENTS_PER_PHASE):
        hold_right(duration_ms=HOLD_SEGMENT_MS)
        sample(f"turn_seg{seg}", t_phase_start, with_hud=True)
        if seg < TURN_SEGMENTS_PER_PHASE - 1:
            time.sleep(HOLD_INTER_GAP_S)
    sample("turn_post", t_phase_start, with_hud=True)
    total = time.monotonic() - t_phase_start
    print(f"[calibrate] turn-at-speed phase total {total:.1f}s")

    print("[calibrate] === D. sail_stop, then sail_start WITH chained rudder ===")
    sail_stop()
    print(f"[calibrate] sleeping {STOP_WAIT_S}s for ship to fully stop…")
    time.sleep(STOP_WAIT_S)
    sample("stopped_boundary_2", time.monotonic(), with_hud=True)

    t_d_start = time.monotonic()
    sail_start()
    sample("start_w_rudder_pre", t_d_start, with_hud=True)
    for seg in range(TURN_SEGMENTS_PER_PHASE):
        hold_right(duration_ms=HOLD_SEGMENT_MS)
        sample(f"start_w_rudder_seg{seg}", t_d_start, with_hud=True)
        if seg < TURN_SEGMENTS_PER_PHASE - 1:
            time.sleep(HOLD_INTER_GAP_S)
    sample("start_w_rudder_post", t_d_start, with_hud=True)
    total = time.monotonic() - t_d_start
    print(f"[calibrate] start-w-rudder phase total {total:.1f}s")
    sample("end_hud", t_d_start, with_hud=True)

    print("[calibrate] === stopping ship to finish ===")
    sail_stop()
    fp.close()
    print(f"[calibrate] wrote {out_path}")


# ── Fit mode ───────────────────────────────────────────────────────────

def _fit_exponential(ts, vs):
    """Fit v(t) = v_max × (1 − exp(−t/τ)).

    Returns (v_max, τ).  Uses a coarse grid search — good enough for a
    single-curve fit, no scipy needed.
    """
    if not ts:
        return None, None
    v_max_guess = max(vs)
    best = (None, None, float("inf"))
    for v_max in [v_max_guess * f for f in (0.9, 0.95, 1.0, 1.05, 1.1)]:
        for tau in [0.5, 0.7, 1.0, 1.3, 1.5, 2.0, 2.5, 3.0]:
            sse = 0.0
            for t, v in zip(ts, vs):
                pred = v_max * (1 - math.exp(-t / tau))
                sse += (pred - v) ** 2
            if sse < best[2]:
                best = (v_max, tau, sse)
    return best[0], best[1]


def fit_from_log(log_path: Path) -> None:
    """Read a calibration JSONL and print fitted constants."""
    samples = []
    for line in open(log_path):
        line = line.strip()
        if not line:
            continue
        samples.append(json.loads(line))
    print(f"[calibrate] {len(samples)} samples from {log_path}")

    # ── 1. Linear acceleration ───────────────────────────────
    accel = [(s["t_s"], s["speed_kt"]) for s in samples
             if s["phase"] == "accel" and s["speed_kt"] is not None]
    if not accel:
        print("[calibrate] no 'accel' samples — skipping linear fit")
        v_max = tau_v = None
    else:
        ts, vs = zip(*accel)
        v_max, tau_v = _fit_exponential(list(ts), list(vs))
        print(f"\n=== Linear acceleration ===")
        print(f"  fit: v(t) = {v_max:.1f}kt × (1 − exp(−t / {tau_v:.2f}s))")
        for t, v in accel:
            pred = v_max * (1 - math.exp(-t / tau_v))
            print(f"    t={t:5.1f}s  v={v:5.1f}kt  pred={pred:5.1f}kt"
                  f"  resid={v-pred:+.1f}")

    # ── 2. Turn rate vs speed ────────────────────────────────
    # turn_at_speed segment: ship rotating at v_max with rudder held.
    # start_w_rudder segment: ship accelerating from 0 with rudder held.
    print(f"\n=== Turn rate ===")
    for label, phase in [("at top speed", "turn_at_speed"),
                         ("during accel from rest", "start_w_rudder")]:
        seg = [(s["t_s"], s["speed_kt"], s["heading_deg"])
               for s in samples if s["phase"] == phase
               and s["heading_deg"] is not None]
        if len(seg) < 2:
            print(f"  {label}: insufficient samples")
            continue
        # Compute pairwise turn rates.
        rates = []
        for i in range(1, len(seg)):
            t0, v0, h0 = seg[i - 1]
            t1, v1, h1 = seg[i]
            dt = t1 - t0
            if dt < 0.1:
                continue
            dh = ((h1 - h0 + 540) % 360) - 180
            v_mid = ((v0 or 0) + (v1 or 0)) / 2
            rates.append((v_mid, dh / dt))
        if rates:
            vs_r, dthdt = zip(*rates)
            avg_v = sum(vs_r) / len(vs_r)
            avg_rate = sum(dthdt) / len(dthdt)
            print(f"  {label}: mean v={avg_v:.1f}kt  mean turn={avg_rate:+.1f}°/s "
                  f"({len(rates)} samples)")

    # ── Pasteable constants ─────────────────────────────────
    if v_max is not None:
        accel_3s = v_max * 3 - v_max * tau_v * (1 - math.exp(-3 / tau_v))
        print(f"\n=== Constants for brain/goals/hug_shore.py ===")
        print(f"# Calibrated {time.strftime('%Y-%m-%d')} from "
              f"{log_path.name}")
        print(f"SHIP_V_MAX_KT   = {v_max:.1f}")
        print(f"SHIP_ACCEL_TAU  = {tau_v:.2f}     # v(t) = v_max × (1 − exp(−t/τ))")
        print(f"SHIP_DIST_3S_KT_S = {accel_3s:.2f}   # forward kt·s after 3s from rest")


def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", action="store_true",
                      help="drive the calibration maneuvers and log to --out")
    mode.add_argument("--fit", type=Path,
                      help="fit curves from an existing JSONL log")
    ap.add_argument("--out", type=Path, default=None,
                    help="output JSONL path (required with --run)")
    ap.add_argument("--duration-s", type=float, default=60.0,
                    help="hint for total run time (not strictly enforced)")
    ap.add_argument("--save-crops", action="store_true", default=True,
                    help="save full-frame + speed/latlon/minimap crops "
                         "per sample for offline inspection (default on)")
    ap.add_argument("--no-save-crops", dest="save_crops",
                    action="store_false")
    args = ap.parse_args()

    if args.run:
        if args.out is None:
            raise SystemExit("--out is required with --run")
        run_calibration(args.out, args.duration_s, save_crops=args.save_crops)
    else:
        fit_from_log(args.fit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
