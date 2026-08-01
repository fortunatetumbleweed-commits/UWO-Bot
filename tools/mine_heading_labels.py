"""Mine heading labels for the ship-direction CNN.

For each saved session, run BOTH heading detectors on every tick
without priors (so we get raw, independent estimates), then bucket
each tick by agreement:

  - gold:   |Δ| ≤ 5°    → high-confidence label (averaged)
  - silver: 5° < |Δ| ≤ 30° → motion-bearing tiebreaker if available
  - hard:   |Δ| > 30°  OR either detector reports zero confidence
            → informative training examples for the CNN, but no
              clean label until human review

Output (one file per session):
  data/heading_labels/<session_name>/labels.jsonl
with rows
  {tick, frame_path, hdg_template, conf_template,
   hdg_pca, conf_pca, hdg_trace, delta_tp, motion_bearing,
   agreement_class, label_hdg (when gold/silver)}

Also writes a `summary.json` per run with bucket counts.

Usage
─────
  python -m tools.mine_heading_labels                  # all ai_nav sessions
  python -m tools.mine_heading_labels --session SESS   # one session
  python -m tools.mine_heading_labels --limit 50       # first 50 ticks each
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from brain.ai_nav.layers.heading import PCAHeading, TemplateMatchHeading
from brain.ai_nav.vision_input import FileVisionSource


GOLD_DEG = 5.0
# Motion-bearing sanity check for gold: detectors can agree AND both
# be wrong (correlated error from shared occlusion bias).  Demote to
# `gold_unverified` when motion bearing disagrees by > this much.
# 15° accommodates legitimate heading-vs-motion lag without rejecting
# clean cases.  See discussion in
# docs/layered_training_with_bounce_and_thrash_rewards.md §5.3.
GOLD_MOTION_TOL = 15.0

# Motion-bearing tiebreaker: silver if motion_bearing is decisively
# closer to one detector than the other.  MB_PICK_THRESH = motion
# bearing must be within this many degrees of the winner; the loser
# must be at least MB_GAP_DEG further from motion bearing.
MB_PICK_THRESH = 30.0
MB_GAP_DEG = 30.0

# Minimum lat/lon displacement (degrees) to trust motion bearing.
# Below this, OCR noise / sub-pixel drift dominates direction.
MIN_MOTION_DEG = 0.005

# Maximum lat/lon displacement (degrees) between consecutive ticks
# before we treat motion as OCR-contaminated (typically caused by the
# "???"/village text overlapping the lat/lon HUD).  Set from p99 of
# the v4 empirical distribution — cruising motion rarely exceeds
# these bounds.  Above → drop the motion bearing.
MAX_MOTION_LAT_DEG = 0.15
MAX_MOTION_LON_DEG = 0.12

# Ships stationary in port yield motion bearings dominated by OCR
# noise.  Reject motion bearing below this speed; the detector-only
# gold/hard buckets still apply.
MIN_MOTION_SPEED_KT = 1.5

# Windowed-median outlier: if this tick's motion bearing differs from
# the local (±LOCAL_MB_WINDOW) circular median by more than this
# many degrees, the bearing is likely OCR-corrupted even though the
# per-tick delta looks in-range.  Catches the "silent pollution"
# class: prev/curr lat both happen to be near-plausible but neither
# matches the ship's real trajectory.
LOCAL_MB_WINDOW = 3
LOCAL_MB_OUTLIER_DEG = 90.0


def _ang_diff(a: float, b: float) -> float:
    """Smallest absolute angular difference, in degrees."""
    return abs(((a - b + 540.0) % 360.0) - 180.0)


def _ang_mean(a: float, b: float) -> float:
    """Circular mean of two angles."""
    sx = math.sin(math.radians(a)) + math.sin(math.radians(b))
    cx = math.cos(math.radians(a)) + math.cos(math.radians(b))
    return (math.degrees(math.atan2(sx, cx)) + 360.0) % 360.0


def _motion_bearing(prev_lat, prev_lon, lat, lon,
                    speed_kt: Optional[float] = None
                    ) -> tuple[Optional[float], Optional[str]]:
    """Bearing from (prev_lat, prev_lon) to (lat, lon), 0=N CW.

    Returns (bearing, reject_reason).  ``reject_reason`` is None when
    the bearing is trustworthy, or a short string explaining why the
    motion bearing was rejected.  Callers should treat a rejected
    bearing as unavailable (equivalent to no lat/lon data).
    """
    if None in (prev_lat, prev_lon, lat, lon):
        return None, "missing_latlon"
    dlat = lat - prev_lat
    dlon = lon - prev_lon
    if math.hypot(dlat, dlon) < MIN_MOTION_DEG:
        return None, "no_motion"
    if abs(dlat) > MAX_MOTION_LAT_DEG or abs(dlon) > MAX_MOTION_LON_DEG:
        return None, "impossible_delta"
    if speed_kt is not None and speed_kt < MIN_MOTION_SPEED_KT:
        return None, "low_speed"
    return (math.degrees(math.atan2(dlon, dlat)) + 360.0) % 360.0, None


def _circular_median(vals: list[float]) -> Optional[float]:
    """Circular mean via unit-vector averaging (approximation of the
    circular median, sufficient for outlier detection)."""
    if not vals: return None
    sx = sum(math.sin(math.radians(v)) for v in vals) / len(vals)
    cx = sum(math.cos(math.radians(v)) for v in vals) / len(vals)
    return (math.degrees(math.atan2(sx, cx)) + 360.0) % 360.0


def _classify(d_tp: Optional[float],
              conf_t: float, conf_p: float,
              motion_brg: Optional[float],
              hdg_t: float, hdg_p: float) -> tuple[str, Optional[float]]:
    """Return (agreement_class, label_hdg).

    Buckets:
      - gold              : both detectors agree AND motion bearing
                            confirms (or no motion bearing available)
      - gold_unverified   : both detectors agree, but motion bearing
                            disagrees → likely correlated 180° flip
                            from shared occlusion bias; do NOT train
      - silver            : detectors disagree; motion bearing picks
                            one decisively → train
      - silver_nobrg      : detectors disagree; no motion bearing →
                            no label
      - hard              : true ambiguity → no label
    """
    if conf_t == 0.0 or conf_p == 0.0 or d_tp is None:
        return "hard", None
    if d_tp <= GOLD_DEG:
        label = _ang_mean(hdg_t, hdg_p)
        # Sanity-check gold against motion bearing.  Detectors with
        # correlated errors (same occlusion bias) can agree on the
        # wrong answer.
        if motion_brg is None:
            return "gold", label
        if _ang_diff(label, motion_brg) <= GOLD_MOTION_TOL:
            return "gold", label
        return "gold_unverified", None
    if motion_brg is None:
        return "silver_nobrg", None
    d_t = _ang_diff(hdg_t, motion_brg)
    d_p = _ang_diff(hdg_p, motion_brg)
    if min(d_t, d_p) <= MB_PICK_THRESH and abs(d_t - d_p) >= MB_GAP_DEG:
        return ("silver", hdg_t) if d_t < d_p else ("silver", hdg_p)
    return "hard", None


def mine_session(session_dir: Path, out_dir: Path,
                 limit: Optional[int] = None) -> dict:
    trace_path = session_dir / "trace.jsonl"
    if not trace_path.exists():
        return {"skipped": True, "reason": "no trace.jsonl"}

    src = FileVisionSource(session_dir)
    pca = PCAHeading()
    tpl = TemplateMatchHeading()

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "labels.jsonl"

    counts = {"gold": 0, "gold_unverified": 0, "silver": 0,
              "silver_nobrg": 0, "hard": 0,
              "total": 0, "frame_missing": 0,
              "mb_reject_missing": 0, "mb_reject_no_motion": 0,
              "mb_reject_impossible_delta": 0, "mb_reject_low_speed": 0,
              "mb_reject_local_outlier": 0}

    # --------------- pass 1: build per-tick record list ---------------
    # Read trace + run detectors once per tick.  We buffer the whole
    # session so pass 2 can do the windowed-median outlier check that
    # a streaming pass can't.
    pending = []  # list of dicts with per-tick raw fields
    prev_lat = prev_lon = None
    with trace_path.open() as in_f:
        for line in in_f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            tick = rec.get("tick")
            if tick is None: continue
            if limit is not None and len(pending) >= limit: break

            try:
                frame = src.capture(tick)
            except FileNotFoundError:
                counts["frame_missing"] += 1
                continue
            try:
                h_tpl = tpl.estimate(frame, prior=None)
                h_pca = pca.estimate(frame, prior=None)
            except Exception:
                counts["frame_missing"] += 1
                continue

            lat = rec.get("lat"); lon = rec.get("lon")
            speed_kt = rec.get("speed_kt")
            mb, mb_reject = _motion_bearing(prev_lat, prev_lon, lat, lon,
                                            speed_kt=speed_kt)
            if mb_reject:
                counts[f"mb_reject_{mb_reject}"] = counts.get(
                    f"mb_reject_{mb_reject}", 0) + 1

            pending.append({
                "tick": tick, "lat": lat, "lon": lon,
                "speed_kt": speed_kt,
                "hdg_t": h_tpl.bearing_deg, "conf_t": h_tpl.confidence,
                "hdg_p": h_pca.bearing_deg, "conf_p": h_pca.confidence,
                "hdg_trace": rec.get("heading_deg"),
                "mb": mb, "mb_reject": mb_reject,
            })

            if lat is not None and lon is not None:
                prev_lat, prev_lon = lat, lon

    # --------------- pass 2: windowed-median outlier + classify -------
    # Index by tick for windowed lookups, and pre-collect all valid
    # motion bearings so we can flag outliers.
    by_tick = {r["tick"]: r for r in pending}
    ticks_sorted = sorted(by_tick)

    def _windowed_median_mb(t: int) -> Optional[float]:
        vals = []
        for k in range(t - LOCAL_MB_WINDOW, t + LOCAL_MB_WINDOW + 1):
            if k == t: continue
            r = by_tick.get(k)
            if r and r["mb"] is not None:
                vals.append(r["mb"])
        return _circular_median(vals)

    with out_path.open("w") as out_f:
        for r in pending:
            mb = r["mb"]
            mb_reject = r["mb_reject"]
            local_median = _windowed_median_mb(r["tick"])
            if mb is not None and local_median is not None:
                dev = _ang_diff(mb, local_median)
                if dev > LOCAL_MB_OUTLIER_DEG:
                    mb = None
                    mb_reject = "local_outlier"
                    counts["mb_reject_local_outlier"] += 1

            d_tp = (_ang_diff(r["hdg_t"], r["hdg_p"])
                    if r["conf_t"] > 0 and r["conf_p"] > 0 else None)
            agreement, label = _classify(d_tp, r["conf_t"], r["conf_p"],
                                         mb, r["hdg_t"], r["hdg_p"])
            counts[agreement] += 1
            counts["total"] += 1

            out_f.write(json.dumps({
                "tick": r["tick"],
                "frame_path": str((session_dir
                                   / f"tick_{r['tick']:04d}.png").resolve()),
                "hdg_template": round(r["hdg_t"], 2),
                "conf_template": round(r["conf_t"], 3),
                "hdg_pca": round(r["hdg_p"], 2),
                "conf_pca": round(r["conf_p"], 3),
                "hdg_trace": r["hdg_trace"],
                "delta_tp": round(d_tp, 2) if d_tp is not None else None,
                "motion_bearing": round(mb, 2) if mb is not None else None,
                "mb_reject_reason": mb_reject,
                "agreement_class": agreement,
                "label_hdg": round(label, 2) if label is not None else None,
            }) + "\n")

    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions-root", default="data/sessions")
    ap.add_argument("--out-root", default="data/heading_labels")
    ap.add_argument("--session", help="Mine only this session dir name")
    ap.add_argument("--prefix", default="ai_nav_",
                    help="Session-dir name prefix filter (default ai_nav_)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap ticks per session (for smoke testing)")
    args = ap.parse_args()

    sessions_root = Path(args.sessions_root)
    out_root = Path(args.out_root)

    if args.session:
        targets = [sessions_root / args.session]
    else:
        targets = sorted(p for p in sessions_root.iterdir()
                         if p.is_dir() and p.name.startswith(args.prefix))

    summary = {"sessions": {}, "totals": {"gold": 0, "gold_unverified": 0,
                                          "silver": 0,
                                          "silver_nobrg": 0, "hard": 0,
                                          "total": 0, "frame_missing": 0}}

    for sess in targets:
        if not sess.exists():
            print(f"[skip] {sess.name} — not found")
            continue
        print(f"[mine] {sess.name} ...", flush=True)
        out_dir = out_root / sess.name
        counts = mine_session(sess, out_dir, limit=args.limit)
        summary["sessions"][sess.name] = counts
        if counts.get("skipped"):
            print(f"  skipped: {counts.get('reason')}")
            continue
        for k in summary["totals"]:
            summary["totals"][k] += counts.get(k, 0)
        print(f"  {counts['total']} ticks → "
              f"gold {counts['gold']}  silver {counts['silver']}  "
              f"silver_nobrg {counts['silver_nobrg']}  "
              f"hard {counts['hard']}  "
              f"frame_missing {counts['frame_missing']}")

    (out_root).mkdir(parents=True, exist_ok=True)
    with (out_root / "summary.json").open("w") as f:
        json.dump(summary, f, indent=2)

    t = summary["totals"]
    print(f"\n=== TOTALS ===\n  ticks      {t['total']}\n"
          f"  gold       {t['gold']:>6}  "
          f"({100.0*t['gold']/max(t['total'],1):.1f}%)\n"
          f"  silver     {t['silver']:>6}  "
          f"({100.0*t['silver']/max(t['total'],1):.1f}%)\n"
          f"  silver_nobrg {t['silver_nobrg']:>4}\n"
          f"  hard       {t['hard']:>6}  "
          f"({100.0*t['hard']/max(t['total'],1):.1f}%)\n"
          f"  frames_missing {t['frame_missing']}")


if __name__ == "__main__":
    main()
