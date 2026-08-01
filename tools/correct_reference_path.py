"""Interactive curator for reference-voyage lat/lon corrections.

The captured `trace.jsonl` is the raw OCR record — it's preserved
exactly as the bot saw it.  This tool writes a sidecar
`corrected_latlon.jsonl` containing ONLY the ticks whose lat/lon we
override after eyeballing the frame.  Both files coexist:

    trace.jsonl              # never modified by this tool
    corrected_latlon.jsonl   # one line per corrected tick

Each correction line:
    {"tick": 267, "lat": 19.5, "lon": 32.1,
     "raw_lat": 32.0, "raw_lon": 9.0,
     "source": "manual_from_frame",
     "note": "OCR axes garbled t266-t267"}

Sources:
    accepted_as_is      — raw value is correct, just confirming
    interpolated        — linear interpolation between good neighbours
    manual_from_frame   — read off the HUD by a human

Downstream tools (`tools/compare_paths`, `sim/region.py`) merge
corrections at load time so the analysis layer sees a clean path
while the raw data stays auditable.

Usage:
    python -m tools.correct_reference_path <session_dir>
    python -m tools.correct_reference_path <session_dir> --auto-interpolate
    python -m tools.correct_reference_path <session_dir> --review

Modes:
    (default)      Show each suspect tick, prompt for action.
    --auto-interpolate   Interpolate every suspect without prompting.
                         Fast path for known-good neighbours; review
                         the output with `--review` afterwards.
    --review       Print every recorded correction; no edits.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Threshold: a tick is "suspect" if it sits > SPIKE_DEG from the
# MEDIAN of its surrounding window.  0.5° = ~55 km — generous vs the
# ~0.05° per-tick of real motion (so we don't false-positive on
# normal jitter) but tight enough to catch one-digit OCR garbles
# like t223-t225 reading lon=30.0 when neighbours are lon=30.85.
# For finer issues (e.g. t219's 19.404 should be 19.04) use manual
# `--tick N --set "lat lon"` mode rather than tighter auto-detection.
SPIKE_DEG = 0.5


@dataclass
class RawSample:
    tick: int
    lat: float
    lon: float


def _load_raw_full(trace_path: Path) -> List[RawSample]:
    """Every tick with lat/lon — no dedup.  Used to expand a single
    detected-suspect tick into the full run of consecutive duplicate
    ticks (e.g. Nile reference t1226/t1227/t1228 all stuck at
    (19.19, 30.0))."""
    out: List[RawSample] = []
    for line in trace_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        lat = rec.get("lat")
        lon = rec.get("lon")
        tick = rec.get("tick")
        if lat is None or lon is None or tick is None:
            continue
        out.append(RawSample(tick=int(tick), lat=float(lat), lon=float(lon)))
    return out


def _load_raw(trace_path: Path) -> List[RawSample]:
    """All ticks with lat/lon, with consecutive duplicates collapsed.

    Collapsing duplicates is critical for spike detection.  When the
    HUD OCR locks on a bad value for several consecutive ticks (e.g.
    Nile reference t266-t268 all read `(32.0, 9.0)`), the per-tick
    spike check would see "this tick equals both neighbours" and
    miss the cluster.  After collapse, the same cluster appears as
    a single sample whose neighbours are the surrounding *good*
    samples — and the spike check works.  Writing corrections still
    needs to walk the full (un-deduped) stream — see
    `_expand_to_cluster`."""
    out: List[RawSample] = []
    last: Optional[Tuple[float, float]] = None
    for line in trace_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        lat = rec.get("lat")
        lon = rec.get("lon")
        tick = rec.get("tick")
        if lat is None or lon is None or tick is None:
            continue
        pt = (float(lat), float(lon))
        if last is not None and pt == last:
            continue
        out.append(RawSample(tick=int(tick), lat=pt[0], lon=pt[1]))
        last = pt
    return out


def _expand_to_cluster(
    suspect: RawSample,
    full_raw: List[RawSample],
) -> List[RawSample]:
    """Return the run of consecutive raw ticks (centered on `suspect`)
    that share the suspect's (lat, lon).  This is the *cluster* of
    stuck-OCR ticks the suspect represents in the deduped stream —
    every member of it needs its own correction.
    """
    idx = next((i for i, s in enumerate(full_raw) if s.tick == suspect.tick), -1)
    if idx < 0:
        return [suspect]
    val = (suspect.lat, suspect.lon)
    lo = idx
    while lo > 0 and (full_raw[lo - 1].lat, full_raw[lo - 1].lon) == val:
        lo -= 1
    hi = idx
    while hi + 1 < len(full_raw) and (full_raw[hi + 1].lat, full_raw[hi + 1].lon) == val:
        hi += 1
    return full_raw[lo:hi + 1]


def _load_corrections(path: Path) -> Dict[int, dict]:
    if not path.exists():
        return {}
    out: Dict[int, dict] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            out[int(d["tick"])] = d
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    return out


def _append_correction(path: Path, entry: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _detect_suspects(
    samples: List[RawSample],
    window: int = 3,
    jag_angle_threshold_deg: float = 90.0,
) -> List[Tuple[int, RawSample, RawSample, RawSample]]:
    """Two-pass outlier detection.

    Pass 1 — median-window:  flags samples > SPIKE_DEG from their
        local-window median.  Catches large garbles (lon=30.0 cluster,
        axes swap to (32.0, 9.0), etc.).

    Pass 2 — slope-jag:  flags samples where the trajectory bends by
        more than `jag_angle_threshold_deg` (vector angle between
        prev→cur and cur→next).  Catches subtle one-digit-extra
        misreads like t1178 reading 18.458 between 18.61 and 18.54 —
        too close to the median to flag in pass 1, but the resulting
        ~130° zigzag stands out.
    """
    out: List[Tuple[int, RawSample, RawSample, RawSample]] = []
    seen: set[int] = set()

    # Pass 1 — median-window.
    for i in range(len(samples)):
        c = samples[i]
        nbrs: List[RawSample] = []
        for j in range(max(0, i - window), min(len(samples), i + window + 1)):
            if j != i:
                nbrs.append(samples[j])
        if len(nbrs) < 2:
            continue
        lats = sorted(s.lat for s in nbrs)
        lons = sorted(s.lon for s in nbrs)
        med_lat = lats[len(lats) // 2]
        med_lon = lons[len(lons) // 2]
        if math.hypot(c.lat - med_lat, c.lon - med_lon) > SPIKE_DEG:
            prev_good = None
            for j in range(i - 1, -1, -1):
                if math.hypot(samples[j].lat - med_lat,
                              samples[j].lon - med_lon) <= SPIKE_DEG:
                    prev_good = samples[j]
                    break
            next_good = None
            for j in range(i + 1, len(samples)):
                if math.hypot(samples[j].lat - med_lat,
                              samples[j].lon - med_lon) <= SPIKE_DEG:
                    next_good = samples[j]
                    break
            if prev_good is not None and next_good is not None:
                out.append((i, prev_good, c, next_good))
                seen.add(c.tick)

    # Pass 2 — slope-jag.  Skip the first and last sample (no angle).
    cos_thresh = math.cos(math.radians(180.0 - jag_angle_threshold_deg))
    for i in range(1, len(samples) - 1):
        c = samples[i]
        if c.tick in seen:
            continue
        p = samples[i - 1]
        n = samples[i + 1]
        v1 = (c.lat - p.lat, c.lon - p.lon)
        v2 = (n.lat - c.lat, n.lon - c.lon)
        m1 = math.hypot(*v1)
        m2 = math.hypot(*v2)
        if m1 < 1e-4 or m2 < 1e-4:
            # Stationary on at least one side — no meaningful angle.
            continue
        cos_angle = (v1[0] * v2[0] + v1[1] * v2[1]) / (m1 * m2)
        # A "sharp jag" means the turn-angle is large, i.e. cos_angle
        # is very negative (vectors point opposite-ish directions).
        # Equivalently: the path-bend exceeds jag_angle_threshold_deg.
        if cos_angle < cos_thresh:
            out.append((i, p, c, n))
            seen.add(c.tick)
    return out


def _frame_path(session_dir: Path, tick: int) -> Optional[Path]:
    p = session_dir / f"tick_{tick:04d}.png"
    return p if p.exists() else None


def _show_frame(p: Path) -> None:
    try:
        subprocess.Popen(["open", str(p)],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"  [warn] couldn't open frame: {e}")


def _interpolate(p: RawSample, c: RawSample, n: RawSample) -> Tuple[float, float]:
    if n.tick > p.tick:
        ratio = (c.tick - p.tick) / (n.tick - p.tick)
    else:
        ratio = 0.5
    return (p.lat + (n.lat - p.lat) * ratio,
            p.lon + (n.lon - p.lon) * ratio)


def _interactive_loop(
    session_dir: Path,
    suspects: List[Tuple[int, RawSample, RawSample, RawSample]],
    existing: Dict[int, dict],
    corrections_path: Path,
) -> int:
    """Walk the user through each suspect.  Returns count of new corrections."""
    n_new = 0
    for idx, p, c, n in suspects:
        if c.tick in existing:
            print(f"\nt{c.tick} — already corrected "
                  f"({existing[c.tick].get('source', 'unknown')}). Skipping.")
            continue

        frame = _frame_path(session_dir, c.tick)
        if frame:
            _show_frame(frame)
        print()
        print(f"=== t{c.tick} ===")
        print(f"  Prev (t{p.tick}): lat={p.lat}  lon={p.lon}")
        print(f"  CUR  (t{c.tick}): lat={c.lat}  lon={c.lon}   ← SUSPECT")
        print(f"  Next (t{n.tick}): lat={n.lat}  lon={n.lon}")
        if frame:
            print(f"  Frame: {frame.name} (opened in Preview)")
        else:
            print(f"  Frame: (not captured)")
        ilat, ilon = _interpolate(p, c, n)
        print(f"  Interpolated: lat={ilat:.3f}  lon={ilon:.3f}")
        print()
        print("  y            accept raw as correct")
        print("  i            use interpolated value")
        print("  <lat> <lon>  manual override (e.g. 19.5 32.1)")
        print("  s            skip — decide later")
        print("  q            quit & save")
        choice = input("? ").strip()

        if choice == "q":
            print("[exit] saving and stopping.")
            return n_new
        if choice == "s":
            continue

        entry = {"tick": c.tick, "raw_lat": c.lat, "raw_lon": c.lon}
        if choice == "y":
            entry.update(lat=c.lat, lon=c.lon, source="accepted_as_is")
        elif choice == "i":
            entry.update(lat=ilat, lon=ilon, source="interpolated")
        else:
            parts = choice.split()
            if len(parts) != 2:
                print("  [skip] expected <lat> <lon>")
                continue
            try:
                lat = float(parts[0])
                lon = float(parts[1])
            except ValueError:
                print("  [skip] couldn't parse numbers")
                continue
            entry.update(lat=lat, lon=lon, source="manual_from_frame")
        _append_correction(corrections_path, entry)
        n_new += 1
        print(f"  saved: lat={entry['lat']:.3f}  lon={entry['lon']:.3f}  "
              f"({entry['source']})")
    return n_new


def _auto_interpolate(
    suspects: List[Tuple[int, RawSample, RawSample, RawSample]],
    existing: Dict[int, dict],
    corrections_path: Path,
    full_raw: List[RawSample],
) -> int:
    """For each suspect, expand to the *cluster* of consecutive ticks
    that share the suspect's raw value, and interpolate a correction
    for each cluster member.  Catches stuck-OCR runs like t1226/
    t1227/t1228 — without expansion, only t1226 would be corrected
    and the other two would still render as bad on the path."""
    n_new = 0
    # NOTE: we iterate even when the suspect's own tick is already
    # corrected — the *cluster* around it may have members that
    # haven't been written yet.  Per-member existence check below.
    for idx, p, c, n in suspects:
        cluster = _expand_to_cluster(c, full_raw)
        for member in cluster:
            if member.tick in existing:
                continue
            # Interpolate THIS member's position from the good
            # neighbours on either side of the cluster.
            ratio = ((member.tick - p.tick) / max(n.tick - p.tick, 1))
            ilat = p.lat + (n.lat - p.lat) * ratio
            ilon = p.lon + (n.lon - p.lon) * ratio
            entry = {
                "tick": member.tick,
                "lat": ilat, "lon": ilon,
                "raw_lat": member.lat, "raw_lon": member.lon,
                "source": "interpolated_cluster",
            }
            _append_correction(corrections_path, entry)
            existing[member.tick] = entry
            n_new += 1
            print(f"  t{member.tick}: {member.lat},{member.lon}  →  "
                  f"{ilat:.3f},{ilon:.3f}  (interpolated, cluster of t{c.tick})")
    return n_new


def _review(corrections_path: Path) -> None:
    corrections = _load_corrections(corrections_path)
    print(f"{len(corrections)} corrections in {corrections_path.name}:")
    for tick in sorted(corrections):
        c = corrections[tick]
        print(f"  t{tick}: raw=({c.get('raw_lat')},{c.get('raw_lon')})  "
              f"→ ({c.get('lat')},{c.get('lon')})  "
              f"[{c.get('source', '?')}]")


def _manual_set(
    session_dir: Path,
    samples: List[RawSample],
    existing: Dict[int, dict],
    corrections_path: Path,
    tick: int,
    lat: float, lon: float,
    note: Optional[str] = None,
) -> None:
    """Write one correction directly — used by `--tick N --set 'lat lon'`."""
    raw_lat = raw_lon = None
    for s in samples:
        if s.tick == tick:
            raw_lat, raw_lon = s.lat, s.lon
            break
    entry: dict = {
        "tick": tick, "lat": lat, "lon": lon,
        "source": "manual_from_frame",
    }
    if raw_lat is not None:
        entry["raw_lat"] = raw_lat
        entry["raw_lon"] = raw_lon
    if note:
        entry["note"] = note
    _append_correction(corrections_path, entry)
    if raw_lat is not None:
        print(f"  t{tick}: ({raw_lat},{raw_lon})  →  "
              f"({lat:.3f},{lon:.3f})  (manual)")
    else:
        print(f"  t{tick}: ({lat:.3f},{lon:.3f})  (manual; no raw to display)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("session_dir", type=Path,
                    help="reference voyage dir (must contain trace.jsonl)")
    ap.add_argument("--auto-interpolate", action="store_true",
                    help="apply linear interpolation to every suspect tick "
                         "without prompting")
    ap.add_argument("--review", action="store_true",
                    help="just print the existing corrections.jsonl, no edits")
    ap.add_argument("--tick", type=int,
                    help="manually correct THIS tick — use with --set to "
                         "supply the value, or omit --set to interactively "
                         "prompt for it.")
    ap.add_argument("--set", dest="set_value", default=None,
                    help='lat lon to write for --tick, e.g. --set "18.81 30.81"')
    ap.add_argument("--note", default=None,
                    help="optional note attached to the manual correction")
    ap.add_argument("--negate-lat-range",
                    metavar="START:END",
                    help="for every tick in [START, END] (inclusive), write "
                         "a correction with lat=-abs(lat).  Use when the "
                         "OCR dropped the minus sign across a south-of-"
                         "equator stretch (e.g. Nile reference t740:785).")
    args = ap.parse_args(argv)

    if not args.session_dir.is_dir():
        raise SystemExit(f"not a directory: {args.session_dir}")
    trace_path = args.session_dir / "trace.jsonl"
    if not trace_path.exists():
        raise SystemExit(f"missing: {trace_path}")
    corrections_path = args.session_dir / "corrected_latlon.jsonl"

    if args.review:
        _review(corrections_path)
        return 0

    samples = _load_raw(trace_path)
    existing = _load_corrections(corrections_path)

    # Bulk-negate mode.  Runs independently of suspect detection.
    if args.negate_lat_range:
        parts = args.negate_lat_range.split(":")
        if len(parts) != 2:
            raise SystemExit(f"--negate-lat-range expected START:END, "
                             f"got {args.negate_lat_range!r}")
        try:
            start, end = int(parts[0]), int(parts[1])
        except ValueError:
            raise SystemExit(f"--negate-lat-range bounds not parseable: "
                             f"{args.negate_lat_range!r}")
        full_raw = _load_raw_full(trace_path)
        n_new = 0
        for s in full_raw:
            if not (start <= s.tick <= end):
                continue
            # Source for the negated value: the most-recent correction
            # if any (e.g. an interpolated value from this same range)
            # else the raw lat from trace.jsonl.
            if s.tick in existing:
                base_lat = float(existing[s.tick]["lat"])
                base_lon = float(existing[s.tick]["lon"])
            else:
                base_lat = s.lat
                base_lon = s.lon
            if base_lat >= 0:
                neg_lat = -abs(base_lat)
                entry = {
                    "tick": s.tick,
                    "lat": neg_lat, "lon": base_lon,
                    "raw_lat": s.lat, "raw_lon": s.lon,
                    "source": "manual_from_frame",
                    "note": f"negated lat — OCR dropped minus sign in "
                            f"[{start},{end}]",
                }
                _append_correction(corrections_path, entry)
                existing[s.tick] = entry
                n_new += 1
                print(f"  t{s.tick}: lat {base_lat:.3f} → {neg_lat:.3f}")
        print(f"\nWrote {n_new} sign-flipped correction(s) to "
              f"{corrections_path}.")
        return 0

    # Manual single-tick mode.
    if args.tick is not None:
        if args.set_value is not None:
            parts = args.set_value.split()
            if len(parts) != 2:
                raise SystemExit(f"--set expected 'lat lon', got {args.set_value!r}")
            try:
                lat = float(parts[0]); lon = float(parts[1])
            except ValueError:
                raise SystemExit(f"--set values not parseable: {args.set_value!r}")
            _manual_set(args.session_dir, samples, existing, corrections_path,
                        args.tick, lat, lon, args.note)
            return 0
        # No --set: interactively prompt for this one tick.
        # Find the tick + surrounding good neighbours in the raw stream.
        target = next((s for s in samples if s.tick == args.tick), None)
        if target is None:
            raise SystemExit(f"tick {args.tick} not in trace.jsonl")
        idx = samples.index(target)
        prev = samples[idx - 1] if idx > 0 else target
        nxt = samples[idx + 1] if idx + 1 < len(samples) else target
        n_new = _interactive_loop(
            args.session_dir, [(idx, prev, target, nxt)],
            existing, corrections_path,
        )
        print(f"\nWrote {n_new} new correction(s) to {corrections_path}.")
        return 0

    suspects = _detect_suspects(samples)
    print(f"Detected {len(suspects)} suspect ticks "
          f"({len(existing)} corrections already on disk).")

    if args.auto_interpolate:
        full_raw = _load_raw_full(trace_path)
        n_new = _auto_interpolate(suspects, existing, corrections_path,
                                  full_raw)
    else:
        # Interactive: skip if every suspect's own tick is corrected.
        n_pending = sum(1 for _, _, c, _ in suspects if c.tick not in existing)
        if n_pending == 0:
            print("Nothing left to curate.")
            return 0
        n_new = _interactive_loop(args.session_dir, suspects, existing,
                                  corrections_path)
    print(f"\nWrote {n_new} new correction(s) to {corrections_path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
