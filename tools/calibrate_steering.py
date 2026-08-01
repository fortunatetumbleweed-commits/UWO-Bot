"""Steering calibration harness — measure the ship's real turn-per-hold curve.

Why: the reflex planner converts a heading error to a rudder hold via a fixed
`RATE_DPS=120°/sec` model, but voyage traces show the ship actually turns at
~20°/sec (RATE_DPS ~5× too high → chronic under-turn → land collisions).  The
effective rate is also expected to vary by ship / cruise speed.  This harness
measures the real curve deterministically so the gain can be calibrated
per ship before wiring the PID controller.

What it does (run with the ship CRUISING in OPEN water — e.g. the open
Mediterranean — where it can turn freely for ~2 min without hitting land):
  1. Recalibrate the wheel-arrow positions (same `_auto_calibrate_ui` as the
     live runner).
  2. Fire a sweep of rudder holds (default 300/500/800/1200 ms), alternating
     left/right each measurement so the heading oscillates around a centre and
     the ship stays in open water.
  3. For each hold: measure heading before and after via the SAME ensemble CNN
     the bot uses (median of a few captures to beat the ~±7° per-frame noise),
     record the actual turn + speed.
  4. Fit the effective turn rate (deg/sec, least-squares through the origin),
     per direction and overall, and save a per-ship JSON.

Usage:
    python -m tools.calibrate_steering --ship trader_8kt --reps 3
    python -m tools.calibrate_steering --ship scout_15kt --holds 300 600 1000 1500
    python -m tools.calibrate_steering --dry-run          # print the plan only
"""
from __future__ import annotations
import argparse
import json
import math
import random
import statistics
import time
from pathlib import Path
from typing import Optional


def _sdiff(a: float, b: float) -> float:
    """Signed smallest angle a-b in (-180, 180]."""
    return ((a - b + 540.0) % 360.0) - 180.0


def _circular_mean(bearings: list[float]) -> float:
    mx = sum(math.cos(math.radians(b)) for b in bearings)
    my = sum(math.sin(math.radians(b)) for b in bearings)
    return (math.degrees(math.atan2(my, mx)) + 360.0) % 360.0


def _measure_heading(source, heading_layer, prior, n: int):
    """Median heading over `n` captures while the ship holds course.

    Returns (bearing_deg, mean_confidence, speed_kt|None, new_prior).
    """
    from vision.sea_hud import read_speed
    bearings, confs, speeds = [], [], []
    for _ in range(n):
        frame = source.capture(0)
        h = heading_layer.estimate(frame, prior=prior)
        prior = h
        bearings.append(h.bearing_deg)
        confs.append(h.confidence)
        try:
            s = read_speed(frame.full_screen())
            if s is not None:
                speeds.append(s)
        except Exception:
            pass
        time.sleep(random.uniform(0.2, 0.4))
    return (_circular_mean(bearings), statistics.mean(confs),
            (statistics.median(speeds) if speeds else None), prior)


def _fit_rate(records: list[dict]) -> Optional[float]:
    """Least-squares turn = rate * hold_seconds through the origin."""
    hs = [r["hold_ms"] / 1000.0 for r in records]
    ts = [r["turn"] for r in records]
    den = sum(h * h for h in hs)
    if den == 0:
        return None
    return sum(h * t for h, t in zip(hs, ts)) / den


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ship", default="unknown",
                    help="ship label (used in the output filename)")
    ap.add_argument("--top-speed", type=float, default=None,
                    help="ship's rated top speed in kt (recorded for "
                         "cross-ship comparison)")
    ap.add_argument("--holds", type=int, nargs="+",
                    default=[300, 500, 800, 1200],
                    help="hold durations (ms) to sweep")
    ap.add_argument("--reps", type=int, default=3,
                    help="repetitions per hold duration")
    ap.add_argument("--captures", type=int, default=3,
                    help="heading captures to median per measurement")
    ap.add_argument("--settle-s", type=float, default=1.5,
                    help="seconds to let the turn settle before measuring")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-calibrate", action="store_true",
                    help="skip the wheel-arrow UI recalibration")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the measurement plan and exit (no ADB)")
    args = ap.parse_args()

    # Build the measurement plan: alternate L/R each step so heading stays
    # bounded and we sample both directions across every hold value.
    plan: list[tuple[int, str]] = []
    for hold in args.holds:
        for _ in range(args.reps):
            cmd = "hold_right" if len(plan) % 2 == 0 else "hold_left"
            plan.append((hold, cmd))

    if args.dry_run:
        print(f"Ship: {args.ship}  |  {len(plan)} measurements")
        for i, (hold, cmd) in enumerate(plan):
            print(f"  [{i:>2}] {cmd:>10}  {hold:>5}ms")
        return

    from brain.ai_nav import AdbVisionSource, AdbActionLayer
    from brain.ai_nav.layers.heading import EnsembleCNNReconstructHeading

    if not args.no_calibrate:
        print("→ recalibrating wheel arrows …")
        from tools.run_ai_nav_live import _auto_calibrate_ui
        _auto_calibrate_ui()

    source = AdbVisionSource()
    actions = AdbActionLayer()
    heading_layer = EnsembleCNNReconstructHeading()

    # Sea-view sanity: warn (don't hard-fail) if speed can't be read — the
    # harness only makes sense with the ship cruising in open water.
    b0, c0, sp0, prior = _measure_heading(source, heading_layer, None,
                                          args.captures)
    print(f"start: heading={b0:.0f}° conf={c0:.2f} speed={sp0} kt")
    if sp0 is None or sp0 < 2.0:
        print("  ⚠ ship does not appear to be cruising (speed unreadable or "
              "< 2 kt).  Start the ship sailing in open water, then re-run.")

    records: list[dict] = []
    for i, (hold, cmd) in enumerate(plan):
        h0, cf0, spd0, prior = _measure_heading(source, heading_layer, prior,
                                                args.captures)
        actions.fire(cmd, hold)                        # blocks for hold_ms
        time.sleep(random.uniform(args.settle_s, args.settle_s + 0.6))
        h1, cf1, spd1, prior = _measure_heading(source, heading_layer, prior,
                                                args.captures)
        sign = 1 if cmd == "hold_right" else -1
        turn = _sdiff(h1, h0) * sign                   # +ve = turned as commanded
        bounced = (spd0 is not None and spd1 is not None
                   and spd1 < 0.5 * spd0)
        rec = dict(i=i, hold_ms=hold, cmd=cmd,
                   h0=round(h0, 1), h1=round(h1, 1), turn=round(turn, 1),
                   conf0=round(cf0, 2), conf1=round(cf1, 2),
                   speed=spd0, bounced=bounced)
        records.append(rec)
        flag = "  ⚠BOUNCE" if bounced else ""
        print(f"  [{i:>2}] {cmd:>10} {hold:>5}ms  turn={turn:+6.1f}°  "
              f"h {h0:.0f}->{h1:.0f}  conf~{(cf0 + cf1) / 2:.2f}  "
              f"sp={spd0}{flag}")
        time.sleep(random.uniform(0.8, 1.6))           # jittered anti-cheat gap

    # ── Fit + report ─────────────────────────────────────────────────
    valid = [r for r in records
             if r["conf0"] > 0.5 and r["conf1"] > 0.5 and not r["bounced"]]
    rate_all = _fit_rate(valid)
    rate_r = _fit_rate([r for r in valid if r["cmd"] == "hold_right"])
    rate_l = _fit_rate([r for r in valid if r["cmd"] == "hold_left"])
    speeds = [r["speed"] for r in records if r["speed"]]

    print("\n── turn per hold (valid measurements) ──")
    for hold in args.holds:
        g = [r["turn"] for r in valid if r["hold_ms"] == hold]
        if g:
            eff = statistics.mean(g) / (hold / 1000.0)
            print(f"  {hold:>5}ms  n={len(g)}  turn median={statistics.median(g):>5.1f}°"
                  f"  eff_rate={eff:>5.1f}°/s")
    print(f"\nfitted effective RATE:  overall={rate_all:.1f}°/s  "
          f"right={rate_r if rate_r is None else round(rate_r,1)}  "
          f"left={rate_l if rate_l is None else round(rate_l,1)}  "
          f"(current RATE_DPS=120)")
    if speeds:
        print(f"mean cruise speed: {statistics.mean(speeds):.1f} kt")

    result = dict(
        ship=args.ship,
        top_speed_kt=args.top_speed,
        rate_dps=round(rate_all, 1) if rate_all is not None else None,
        rate_right=round(rate_r, 1) if rate_r is not None else None,
        rate_left=round(rate_l, 1) if rate_l is not None else None,
        mean_speed_kt=round(statistics.mean(speeds), 1) if speeds else None,
        n_valid=len(valid), n_total=len(records),
        holds_ms=args.holds, records=records,
    )
    out = args.out or Path(f"data/steering_cal/{args.ship}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(f"\nsaved: {out}")


if __name__ == "__main__":
    main()
