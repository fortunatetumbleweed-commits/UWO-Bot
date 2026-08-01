"""Compare a live voyage's geographic path against a reference voyage.

Walks the live trace tick-by-tick and matches each sample to the
nearest reference sample within a forward-progressing window.
Distances under `--tol-km` count as ON_PATH; over count as OFF_PATH.
ON↔OFF transitions delimit *deviation segments* — each segment is
a stretch of live ticks where the bot left the reference path,
possibly returning to it (recovery) or not.

Design notes:
  - Forward-progressing reference index: the matcher's `ref_idx`
    only advances. After a live U-turn back along its own path,
    the matcher MUST find a *later* ref point to count as recovery
    — otherwise revisiting an already-walked ref segment would be
    miscoded as recovery.
  - Stationary-OCR ticks (live.lat/lon unchanged vs previous tick)
    are skipped — they're perception holds, not new path samples.
  - Haversine distance in km, not raw degree distance, so the
    tolerance band is uniform regardless of latitude.

Outputs (in `<live_session>/`):
  path_compare.md      Human-readable report (segment table + summary).
  path_compare.json    Machine-readable metric dump.
  path_compare.png     Overlay plot — both paths, deviations in red.

CLI:
  python -m tools.compare_paths <live_session_dir> \
      --reference <reference_session_dir> \
      [--tol-km 5.0] [--lookahead 50] [--no-plot]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


# ── Geometry ────────────────────────────────────────────────────────────────

_EARTH_R_KM = 6371.0088


def _haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Great-circle distance in km between (lat,lon) points in degrees."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * _EARTH_R_KM * math.asin(math.sqrt(h))


# ── Trace loading ───────────────────────────────────────────────────────────

@dataclass
class PathSample:
    tick: int
    lat: float
    lon: float


def _load_corrections(corrections_path: Path) -> dict:
    """Read a `corrected_latlon.jsonl` sidecar, return {tick: (lat, lon)}.
    Empty dict when the file doesn't exist.  See
    `tools/correct_reference_path.py` for the format."""
    if not corrections_path.exists():
        return {}
    out: dict = {}
    for line in corrections_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            out[int(d["tick"])] = (float(d["lat"]), float(d["lon"]))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            pass
    return out


def _load_path(
    trace_path: Path,
    dedupe_stationary: bool = True,
    apply_gate: bool = True,
    apply_corrections: bool = True,
) -> List[PathSample]:
    """Load (tick, lat, lon) samples from a `trace.jsonl`.

    Drops records without lat/lon and (by default) consecutive
    repeats — when the HUD OCR was stale, lat/lon stays unchanged
    for several ticks; those are perception holds, not path samples.

    `apply_corrections=True` merges a sibling `corrected_latlon.jsonl`
    sidecar (written by `tools/correct_reference_path.py`).  Listed
    ticks have their raw lat/lon replaced by the curated value BEFORE
    the gate runs.  The raw `trace.jsonl` is never modified.  This
    lets the reference voyage's known OCR glitches (e.g. t267
    (32.0, 9.0) in `reference_nile_full_20260606_141824`) be cleanly
    fixed without forking the data.

    `apply_gate=True` re-runs each candidate (possibly already
    corrected) through `vision.sea_hud.accept_or_defer` — same
    velocity-aware gate the live loop uses today — as a backstop for
    sessions without curated corrections.
    """
    from vision.sea_hud import accept_or_defer, _NO_PENDING
    corrections = (_load_corrections(trace_path.parent / "corrected_latlon.jsonl")
                   if apply_corrections else {})
    out: List[PathSample] = []
    last_dedup: Optional[Tuple[float, float]] = None
    gate_prev: Optional[Tuple[float, float]] = None
    gate_pending = _NO_PENDING
    gate_history: list = []
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
        # Sidecar correction takes precedence over raw OCR.
        if corrections and int(tick) in corrections:
            lat, lon = corrections[int(tick)]
        pt = (float(lat), float(lon))
        if apply_gate:
            accepted, gate_pending = accept_or_defer(
                candidate=pt, prev=gate_prev, pending=gate_pending,
                history=gate_history, current_tick=int(tick),
            )
            if accepted is None:
                continue
            gate_prev = accepted
            gate_history.append((accepted[0], accepted[1], int(tick)))
            if len(gate_history) > 4:
                del gate_history[0]
            pt = accepted
        if dedupe_stationary and last_dedup is not None and pt == last_dedup:
            continue
        out.append(PathSample(tick=int(tick), lat=pt[0], lon=pt[1]))
        last_dedup = pt
    # Single-point spike removal — drop any sample whose neighbours are
    # BOTH far away.  Catches stubborn OCR garbage that the gate's
    # confirm-required logic admitted because the bad value happened
    # twice in a row (e.g. t266/t267 reading (32.0, 9.0) in the Nile
    # reference voyage).  A real course change would produce
    # consecutive consistent samples, not an isolated jump-and-recover.
    if apply_gate and len(out) >= 3:
        SPIKE_DEG = 2.0
        cleaned = [out[0]]
        for i in range(1, len(out) - 1):
            p = out[i - 1]
            cur = out[i]
            n = out[i + 1]
            d_prev = ((cur.lat - p.lat) ** 2 + (cur.lon - p.lon) ** 2) ** 0.5
            d_next = ((cur.lat - n.lat) ** 2 + (cur.lon - n.lon) ** 2) ** 0.5
            d_pn = ((n.lat - p.lat) ** 2 + (n.lon - p.lon) ** 2) ** 0.5
            # Drop iff this point is far from BOTH neighbours AND the
            # neighbours are close to each other (i.e. it's an
            # isolated spike that the path "skips over").
            if (d_prev > SPIKE_DEG and d_next > SPIKE_DEG
                    and d_pn < SPIKE_DEG):
                continue
            cleaned.append(cur)
        cleaned.append(out[-1])
        out = cleaned
    return out


# ── Alignment ───────────────────────────────────────────────────────────────

@dataclass
class DeviationSegment:
    off_tick: int                  # first live tick that went OFF_PATH
    recover_tick: Optional[int]    # first live tick after recovery (None if never)
    duration_ticks: int            # off_tick → (recover_tick or end)
    max_dist_km: float
    max_dist_tick: int
    ref_tick_at_off: int           # ref point matched just before going off
    ref_tick_at_recovery: Optional[int]


@dataclass
class CompareResult:
    n_live_samples: int
    n_ref_samples: int
    tol_km: float
    lookahead: int
    n_on_path_ticks: int
    n_off_path_ticks: int
    segments: List[DeviationSegment] = field(default_factory=list)
    # Per-live-tick alignment for the plot.
    per_tick: List[dict] = field(default_factory=list)


def compare(
    live: List[PathSample],
    ref: List[PathSample],
    tol_km: float,
    lookahead: int,
) -> CompareResult:
    """Run the forward-progressing closest-point alignment."""
    res = CompareResult(
        n_live_samples=len(live),
        n_ref_samples=len(ref),
        tol_km=tol_km,
        lookahead=lookahead,
        n_on_path_ticks=0,
        n_off_path_ticks=0,
    )
    if not live or not ref:
        return res

    ref_idx = 0
    state = "ON_PATH"
    current_seg: Optional[DeviationSegment] = None
    last_matched_ref_tick = ref[0].tick

    for s in live:
        # First-crossing within tolerance scan. Walks ref forward
        # from ref_idx. Picks the FIRST ref point with d <= tol_km
        # (not the global nearest — the reference may revisit nearby
        # geography later in the voyage, e.g. a round-trip ref).
        # When no within-tol match exists in the window, records the
        # nearest forward point purely for max-distance diagnostics.
        window_end = min(ref_idx + lookahead, len(ref))
        found_global_i: Optional[int] = None
        found_d = float("inf")
        nearest_global_i = ref_idx
        nearest_d = float("inf")
        for i in range(ref_idx, window_end):
            d = _haversine_km((s.lat, s.lon), (ref[i].lat, ref[i].lon))
            if d < nearest_d:
                nearest_d = d
                nearest_global_i = i
            if d <= tol_km:
                found_global_i = i
                found_d = d
                break

        if found_global_i is not None:
            on_path = True
            used_global_i = found_global_i
            used_d = found_d
        else:
            on_path = False
            used_global_i = nearest_global_i
            used_d = nearest_d
        used_ref = ref[used_global_i]

        res.per_tick.append({
            "tick":         s.tick,
            "lat":          s.lat,
            "lon":          s.lon,
            "nearest_ref":  used_ref.tick,
            "dist_km":      used_d,
            "on_path":      on_path,
        })

        if on_path:
            res.n_on_path_ticks += 1
            ref_idx = used_global_i  # monotonic advance
            last_matched_ref_tick = used_ref.tick
            if state == "OFF_PATH" and current_seg is not None:
                current_seg.recover_tick = s.tick
                current_seg.ref_tick_at_recovery = used_ref.tick
                current_seg.duration_ticks = s.tick - current_seg.off_tick
                res.segments.append(current_seg)
                current_seg = None
                state = "ON_PATH"
        else:
            res.n_off_path_ticks += 1
            if state == "ON_PATH":
                state = "OFF_PATH"
                current_seg = DeviationSegment(
                    off_tick=s.tick,
                    recover_tick=None,
                    duration_ticks=0,
                    max_dist_km=used_d,
                    max_dist_tick=s.tick,
                    ref_tick_at_off=last_matched_ref_tick,
                    ref_tick_at_recovery=None,
                )
            else:
                if used_d > current_seg.max_dist_km:  # type: ignore[union-attr]
                    current_seg.max_dist_km = used_d  # type: ignore[union-attr]
                    current_seg.max_dist_tick = s.tick  # type: ignore[union-attr]

    # If we ended OFF_PATH, close the open segment without recovery.
    if current_seg is not None:
        current_seg.duration_ticks = live[-1].tick - current_seg.off_tick
        res.segments.append(current_seg)

    return res


# ── Reporting ───────────────────────────────────────────────────────────────

def render_report(
    live_name: str,
    ref_name: str,
    r: CompareResult,
) -> str:
    lines = []
    bar = "═" * 78
    lines.append(bar)
    lines.append(f"Path comparison — live: {live_name}")
    lines.append(f"                  ref:  {ref_name}")
    lines.append(bar)
    lines.append("")
    lines.append("PARAMETERS")
    lines.append(f"  tolerance     : {r.tol_km:.2f} km")
    lines.append(f"  lookahead     : {r.lookahead} ref samples")
    lines.append(f"  live samples  : {r.n_live_samples}")
    lines.append(f"  ref samples   : {r.n_ref_samples}")
    lines.append("")
    total = r.n_on_path_ticks + r.n_off_path_ticks
    pct_on = 100.0 * r.n_on_path_ticks / max(1, total)
    lines.append("SUMMARY")
    lines.append(f"  on-path  ticks: {r.n_on_path_ticks:>5}   ({pct_on:.1f}%)")
    lines.append(f"  off-path ticks: {r.n_off_path_ticks:>5}   "
                 f"({100.0 - pct_on:.1f}%)")
    lines.append(f"  deviation segments: {len(r.segments)}")
    if r.segments:
        recovered = sum(1 for s in r.segments if s.recover_tick is not None)
        lines.append(f"  recovered          : {recovered}")
        lines.append(f"  unrecovered        : {len(r.segments) - recovered}")
        longest = max(r.segments, key=lambda s: s.duration_ticks)
        farthest = max(r.segments, key=lambda s: s.max_dist_km)
        lines.append(f"  longest segment    : "
                     f"{longest.duration_ticks} ticks  "
                     f"(t{longest.off_tick}–{longest.recover_tick or 'end'})")
        lines.append(f"  farthest deviation : "
                     f"{farthest.max_dist_km:.1f} km  at t{farthest.max_dist_tick}")
    lines.append("")

    if r.segments:
        lines.append("DEVIATION SEGMENTS")
        lines.append("")
        lines.append(f"  {'#':>3}  {'off':>5}  {'rec':>6}  {'dur':>5}  "
                     f"{'max_km':>7}  {'max@t':>6}  "
                     f"{'ref_off':>7}  {'ref_rec':>7}  note")
        lines.append("  " + "─" * 76)
        for i, s in enumerate(r.segments, 1):
            rec = str(s.recover_tick) if s.recover_tick is not None else "—"
            ref_rec = (str(s.ref_tick_at_recovery)
                       if s.ref_tick_at_recovery is not None else "—")
            note = "" if s.recover_tick is not None else "did NOT recover"
            lines.append(
                f"  {i:>3}  "
                f"{s.off_tick:>5}  "
                f"{rec:>6}  "
                f"{s.duration_ticks:>5}  "
                f"{s.max_dist_km:>7.1f}  "
                f"{s.max_dist_tick:>6}  "
                f"{s.ref_tick_at_off:>7}  "
                f"{ref_rec:>7}  "
                f"{note}"
            )
        lines.append("")

    lines.append(bar)
    return "\n".join(lines)


# ── Plot ────────────────────────────────────────────────────────────────────

def _plot(
    live: List[PathSample],
    ref: List[PathSample],
    r: CompareResult,
    out_path: Path,
) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[compare_paths] matplotlib unavailable — skipping plot",
              file=sys.stderr)
        return

    # Split live samples into ON vs OFF segments for two-colour plot.
    on_x, on_y, off_x, off_y = [], [], [], []
    live_x = [entry["lon"] for entry in r.per_tick]
    live_y = [entry["lat"] for entry in r.per_tick]
    for entry in r.per_tick:
        # Note: x=lon, y=lat for natural geographic display.
        if entry["on_path"]:
            on_x.append(entry["lon"])
            on_y.append(entry["lat"])
        else:
            off_x.append(entry["lon"])
            off_y.append(entry["lat"])

    ref_x = [s.lon for s in ref]
    ref_y = [s.lat for s in ref]

    # Side-by-side panels: reference left, live/sim right.  Shared
    # lat/lon limits so the two paths are visually comparable at the
    # same scale — divergence between panels is real spatial drift,
    # not zoom-level confusion.
    fig, (ax_ref, ax_live) = plt.subplots(
        1, 2, figsize=(16, 12), sharex=True, sharey=True,
    )

    # Reference panel — full trajectory as a clean line + start/end.
    ax_ref.plot(ref_x, ref_y, color="#4a7", linewidth=1.4,
                label=f"reference ({len(ref)} samples)")
    if ref_x:
        ax_ref.scatter([ref_x[0]], [ref_y[0]], s=50, color="#0a0",
                       marker="o", edgecolors="black",
                       linewidths=0.8, zorder=5, label="start")
        ax_ref.scatter([ref_x[-1]], [ref_y[-1]], s=70, color="#a00",
                       marker="*", edgecolors="black",
                       linewidths=0.8, zorder=5, label="end")

    # Live/sim panel — on-path green, off-path red, joined by a
    # faint connecting line so the order of visitation is readable.
    ax_live.plot(live_x, live_y, color="#888", linewidth=0.5,
                 alpha=0.45, zorder=1)
    ax_live.scatter(on_x, on_y, s=5, color="#2a8",
                    label=f"on-path ({len(on_x)})", zorder=3)
    ax_live.scatter(off_x, off_y, s=6, color="#c33",
                    label=f"off-path ({len(off_x)})", zorder=2)
    if live_x:
        ax_live.scatter([live_x[0]], [live_y[0]], s=50, color="#0a0",
                        marker="o", edgecolors="black",
                        linewidths=0.8, zorder=5, label="start")
        ax_live.scatter([live_x[-1]], [live_y[-1]], s=70, color="#a00",
                        marker="*", edgecolors="black",
                        linewidths=0.8, zorder=5, label="end")

    # Annotate first 8 deviation-segment start ticks on the live panel.
    for s in r.segments[:8]:
        for entry in r.per_tick:
            if entry["tick"] == s.off_tick:
                ax_live.annotate(
                    f"t{s.off_tick}",
                    (entry["lon"], entry["lat"]),
                    fontsize=8, color="#c33",
                    xytext=(5, 5), textcoords="offset points",
                )
                break

    # Equalize per-axis to keep angles honest and to make the side-by-
    # side comparison fair.  Compute combined bounds from BOTH paths
    # so each panel uses the same window.
    all_x = ref_x + live_x
    all_y = ref_y + live_y
    if all_x and all_y:
        x_min, x_max = min(all_x), max(all_x)
        y_min, y_max = min(all_y), max(all_y)
        x_span = max(x_max - x_min, 1e-3)
        y_span = max(y_max - y_min, 1e-3)
        if x_span > y_span:
            pad = (x_span - y_span) / 2
            y_min -= pad
            y_max += pad
        elif y_span > x_span:
            pad = (y_span - x_span) / 2
            x_min -= pad
            x_max += pad
        span = max(x_max - x_min, 1e-3)
        margin = 0.05 * span
        ax_ref.set_xlim(x_min - margin, x_max + margin)
        ax_ref.set_ylim(y_min - margin, y_max + margin)

    for ax, title in (
        (ax_ref, f"REFERENCE — {len(ref)} samples"),
        (ax_live, f"LIVE / SIM — on={r.n_on_path_ticks}  "
                  f"off={r.n_off_path_ticks}  segments={len(r.segments)}"),
    ):
        ax.set_xlabel("longitude (°E)")
        # `sharex=True, sharey=True` above forbids adjustable=datalim,
        # so use adjustable=box.  We pre-equalised the shared limits to
        # the longer span above, so the box adjustment is a no-op.
        ax.set_aspect("equal", adjustable="box")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper right", fontsize=9)
        ax.set_title(title, fontsize=11)
    ax_ref.set_ylabel("latitude (°N)")

    fig.suptitle(
        f"Path comparison — tol={r.tol_km}km", fontsize=12,
    )
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)


# ── Main ────────────────────────────────────────────────────────────────────

def _result_to_dict(r: CompareResult) -> dict:
    return {
        "n_live_samples":   r.n_live_samples,
        "n_ref_samples":    r.n_ref_samples,
        "tol_km":           r.tol_km,
        "lookahead":        r.lookahead,
        "n_on_path_ticks":  r.n_on_path_ticks,
        "n_off_path_ticks": r.n_off_path_ticks,
        "segments": [
            {
                "off_tick":             s.off_tick,
                "recover_tick":         s.recover_tick,
                "duration_ticks":       s.duration_ticks,
                "max_dist_km":          s.max_dist_km,
                "max_dist_tick":        s.max_dist_tick,
                "ref_tick_at_off":      s.ref_tick_at_off,
                "ref_tick_at_recovery": s.ref_tick_at_recovery,
            }
            for s in r.segments
        ],
    }


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("live_session", type=Path)
    ap.add_argument("--reference", type=Path, required=True)
    ap.add_argument("--tol-km", type=float, default=5.0)
    ap.add_argument("--lookahead", type=int, default=1500,
                    help="How far forward in ref to search per live tick. "
                         "Default 1500 covers most full voyages.")
    ap.add_argument("--ref-end-tick", type=int, default=None,
                    help="Clip reference at this tick — useful when the "
                         "reference is a round trip and you only want to "
                         "compare against the outbound leg.")
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args(argv)

    if not args.live_session.is_dir():
        raise SystemExit(f"not a directory: {args.live_session}")
    if not args.reference.is_dir():
        raise SystemExit(f"not a directory: {args.reference}")

    live = _load_path(args.live_session / "trace.jsonl")
    ref = _load_path(args.reference / "trace.jsonl")
    if args.ref_end_tick is not None:
        ref = [s for s in ref if s.tick <= args.ref_end_tick]
    if not live:
        raise SystemExit(f"no usable trace in {args.live_session}")
    if not ref:
        raise SystemExit(f"no usable trace in {args.reference}")

    r = compare(live, ref, tol_km=args.tol_km, lookahead=args.lookahead)
    report = render_report(
        args.live_session.name, args.reference.name, r,
    )
    print(report)

    (args.live_session / "path_compare.md").write_text(report + "\n")
    (args.live_session / "path_compare.json").write_text(
        json.dumps(_result_to_dict(r), indent=2) + "\n",
    )
    if not args.no_plot:
        _plot(live, ref, r, args.live_session / "path_compare.png")

    return 0


if __name__ == "__main__":
    sys.exit(main())
