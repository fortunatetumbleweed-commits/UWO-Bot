"""Standard voyage evaluation — one command, one verdict.

Procedure (the standard for any Nile-descent voyage):

  1. Load the canonical reference path from
     `data/reference/standard_nile.json` (clips the reference to the
     outbound leg only — comparing against a round trip causes phantom
     matches against the return leg; see the failure mode in the
     2026-06-06 evaluation session).
  2. Run `tools.compare_paths.compare` against the live trace, with
     the standard parameters (tol_km, lookahead).
  3. For each deviation segment, drill into the live trace ticks
     within `[off_tick, recover_tick or end]` and identify *major
     issue ticks* — the specific ticks where something diagnosable
     went wrong (rejection burst start, picker fire, commit reset,
     OCR delta jump).  Classify likely root cause.
  4. Emit headline verdict (PASS / WARN / FAIL) + per-segment table
     + ranked major-issue-tick list so the user can jump straight to
     them in `tick_viewer.py`.

Usage:
    python -m tools.evaluate_voyage <live_session_dir>

Outputs (in `<live_session_dir>/`):
    voyage_eval.md    — human-readable narrative + segment table + issue ticks
    voyage_eval.json  — same data, machine-readable
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple

from tools.compare_paths import (
    CompareResult,
    DeviationSegment,
    PathSample,
    _load_path,
    compare,
)


# ── Tunables ────────────────────────────────────────────────────────────────

# Reference-path config lives in version-controlled JSON so every
# evaluation uses identical parameters.
DEFAULT_REFERENCE_CONFIG = (
    Path(__file__).parent.parent / "data" / "reference" / "standard_nile.json"
)

# Lat/lon delta per tick above this threshold is flagged as an OCR
# jump in the segment analysis.  Matches the velocity-extrapolation
# threshold in `vision/sea_hud.py:_VELOCITY_RESIDUAL_DEG`.
OCR_JUMP_THRESHOLD_DEG = 0.3

# Headline verdict cutoffs.
PASS_MAX_OFF_PCT = 5.0
WARN_MAX_OFF_PCT = 20.0


# ── Per-segment issue analysis ──────────────────────────────────────────────

@dataclass
class IssueTick:
    """One diagnosable issue identified inside a deviation segment."""
    tick: int
    category: str        # rejection_burst_start | ocr_jump | picker_change
                         # | commit_reset | max_dist
    detail: str          # short human-readable hint
    severity: int        # 1 (low) … 3 (high) — for ranking


@dataclass
class SegmentAnalysis:
    """Per-segment narrative + major-issue-tick list."""
    segment: DeviationSegment
    n_rejections: int
    n_picker_changes: int
    n_commit_resets: int
    n_ocr_jumps: int
    issue_ticks: List[IssueTick] = field(default_factory=list)
    inferred_cause: str = "unknown"


def _load_trace_records(session_dir: Path) -> List[dict]:
    out: List[dict] = []
    p = session_dir / "trace.jsonl"
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _records_in_range(
    records: List[dict], first: int, last: int,
) -> List[dict]:
    return [r for r in records if first <= r.get("tick", -1) <= last]


def _committed_tick_of(rec: dict) -> Optional[int]:
    lyap = rec.get("lyapunov") or {}
    seg = lyap.get("segment_mem") or {}
    return seg.get("committed_tick")


def _picker_target_of(rec: dict) -> Optional[tuple]:
    pt = rec.get("picker_target")
    if pt is None:
        return None
    if isinstance(pt, list) and len(pt) == 2:
        return (float(pt[0]), float(pt[1]))
    return None


def _lat_lon_of(rec: dict) -> Optional[Tuple[float, float]]:
    lat = rec.get("lat")
    lon = rec.get("lon")
    if lat is None or lon is None:
        return None
    return (float(lat), float(lon))


def _euclid(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _analyze_segment(
    segment: DeviationSegment,
    records: List[dict],
) -> SegmentAnalysis:
    last = segment.recover_tick if segment.recover_tick is not None \
        else (records[-1].get("tick", segment.off_tick + 1)
              if records else segment.off_tick + 1)
    in_range = _records_in_range(records, segment.off_tick, last)

    # Rejection bursts — runs of consecutive heading_rejected=True.
    rejection_bursts: List[Tuple[int, int]] = []
    cur: Optional[List[int]] = None
    n_rejections = 0
    for r in in_range:
        if r.get("heading_rejected"):
            n_rejections += 1
            if cur is None:
                cur = [r["tick"], r["tick"]]
            else:
                cur[1] = r["tick"]
        else:
            if cur is not None:
                rejection_bursts.append((cur[0], cur[1]))
                cur = None
    if cur is not None:
        rejection_bursts.append((cur[0], cur[1]))

    # Picker target changes.
    picker_changes: List[int] = []
    last_pt = None
    for r in in_range:
        pt = _picker_target_of(r)
        if pt != last_pt and pt is not None:
            picker_changes.append(r["tick"])
        last_pt = pt

    # Commit resets — segment_mem.committed_tick value changes
    # (None → set, set → set with new value).
    commit_resets: List[int] = []
    last_ct: Any = "INIT"
    for r in in_range:
        ct = _committed_tick_of(r)
        if last_ct != "INIT" and ct != last_ct:
            commit_resets.append(r["tick"])
        last_ct = ct

    # OCR delta jumps — consecutive lat/lon changes exceeding threshold.
    ocr_jumps: List[Tuple[int, float]] = []
    prev_pos = None
    for r in in_range:
        pos = _lat_lon_of(r)
        if pos is None:
            continue
        if prev_pos is not None and pos != prev_pos:
            d = _euclid(pos, prev_pos)
            if d > OCR_JUMP_THRESHOLD_DEG:
                ocr_jumps.append((r["tick"], d))
        prev_pos = pos

    # Build the ranked issue-tick list.
    issues: List[IssueTick] = []
    if rejection_bursts:
        first, last_b = rejection_bursts[0]
        n = last_b - first + 1
        issues.append(IssueTick(
            tick=first, category="rejection_burst_start",
            detail=f"heading rejected {n} consecutive ticks ({first}–{last_b})",
            severity=3,
        ))
    for tick, d in ocr_jumps[:3]:
        issues.append(IssueTick(
            tick=tick, category="ocr_jump",
            detail=f"lat/lon delta {d:.2f}° from prev tick",
            severity=3,
        ))
    for tick in picker_changes[:2]:
        issues.append(IssueTick(
            tick=tick, category="picker_change",
            detail="Trémaux picker_target set or moved here",
            severity=2,
        ))
    for tick in commit_resets[:2]:
        issues.append(IssueTick(
            tick=tick, category="commit_reset",
            detail="§13.21 segment commit re-anchored",
            severity=2,
        ))
    # Always include the max-deviation tick.
    issues.append(IssueTick(
        tick=segment.max_dist_tick, category="max_dist",
        detail=f"farthest point in segment ({segment.max_dist_km:.1f} km off-path)",
        severity=1,
    ))

    # Inferred cause heuristic.
    if ocr_jumps and rejection_bursts:
        cause = "ocr_misread → heading_rejection_cascade"
    elif ocr_jumps:
        cause = "ocr_misread (lat/lon jump)"
    elif rejection_bursts:
        cause = "heading rejection cascade"
    elif picker_changes:
        cause = "picker fired (likely Trémaux reversal)"
    elif commit_resets:
        cause = "§13.21 commit reset (steering direction re-anchored)"
    else:
        cause = "small drift only — within tolerance is borderline"

    return SegmentAnalysis(
        segment=segment,
        n_rejections=n_rejections,
        n_picker_changes=len(picker_changes),
        n_commit_resets=len(commit_resets),
        n_ocr_jumps=len(ocr_jumps),
        issue_ticks=sorted(
            issues, key=lambda t: (-t.severity, t.tick),
        ),
        inferred_cause=cause,
    )


# ── Overall verdict ─────────────────────────────────────────────────────────

def _verdict(result: CompareResult) -> str:
    n_total = result.n_on_path_ticks + result.n_off_path_ticks
    if n_total == 0:
        return "UNKNOWN"
    pct_off = 100.0 * result.n_off_path_ticks / n_total
    unrecovered = sum(1 for s in result.segments if s.recover_tick is None)
    if unrecovered > 0:
        return "FAIL"
    if pct_off <= PASS_MAX_OFF_PCT:
        return "PASS"
    if pct_off <= WARN_MAX_OFF_PCT:
        return "WARN"
    return "FAIL"


# ── Report writers ──────────────────────────────────────────────────────────

def _render_md(
    live_name: str,
    ref_name: str,
    verdict: str,
    result: CompareResult,
    analyses: List[SegmentAnalysis],
) -> str:
    bar = "═" * 78
    lines = []
    lines.append(bar)
    lines.append(f"Voyage evaluation — {live_name}")
    lines.append(f"  vs reference: {ref_name}")
    lines.append(bar)
    lines.append("")

    n_total = result.n_on_path_ticks + result.n_off_path_ticks
    pct_off = (100.0 * result.n_off_path_ticks / n_total) if n_total else 0
    lines.append(f"VERDICT: {verdict}")
    lines.append(f"  on-path ticks   : {result.n_on_path_ticks} / {n_total}  "
                 f"({100.0 - pct_off:.1f}%)")
    lines.append(f"  deviation segs  : {len(result.segments)}  "
                 f"({sum(1 for s in result.segments if s.recover_tick is None)} unrecovered)")
    if result.segments:
        worst = max(result.segments, key=lambda s: s.max_dist_km)
        longest = max(result.segments, key=lambda s: s.duration_ticks)
        lines.append(f"  worst deviation : {worst.max_dist_km:.1f} km @ t{worst.max_dist_tick}")
        lines.append(f"  longest segment : {longest.duration_ticks} ticks  "
                     f"(t{longest.off_tick}–{longest.recover_tick or 'end'})")
    lines.append("")

    if not analyses:
        lines.append("No deviations detected — voyage tracked the reference cleanly.")
        return "\n".join(lines)

    lines.append("DEVIATION SEGMENTS — per-segment narrative")
    lines.append("")
    for i, a in enumerate(analyses, 1):
        s = a.segment
        rec = str(s.recover_tick) if s.recover_tick is not None else "—"
        lines.append(f"━━━ Segment #{i} — t{s.off_tick}–t{rec}  "
                     f"({s.duration_ticks} ticks, max {s.max_dist_km:.1f} km)")
        lines.append(f"  Inferred cause: {a.inferred_cause}")
        lines.append(f"  Inside-segment counts:")
        lines.append(f"    heading rejections: {a.n_rejections}")
        lines.append(f"    picker changes    : {a.n_picker_changes}")
        lines.append(f"    commit resets     : {a.n_commit_resets}")
        lines.append(f"    OCR delta jumps   : {a.n_ocr_jumps}")
        lines.append(f"  Major issue ticks (jump to these in tick_viewer):")
        for it in a.issue_ticks:
            sev = "!!!" if it.severity == 3 else "!! " if it.severity == 2 else "!  "
            lines.append(f"    {sev}  t{it.tick}: {it.category} — {it.detail}")
        lines.append("")

    lines.append(bar)
    return "\n".join(lines)


def _render_json(
    verdict: str, result: CompareResult, analyses: List[SegmentAnalysis],
) -> dict:
    return {
        "verdict": verdict,
        "n_on_path_ticks": result.n_on_path_ticks,
        "n_off_path_ticks": result.n_off_path_ticks,
        "n_segments": len(result.segments),
        "segments": [
            {
                "off_tick": a.segment.off_tick,
                "recover_tick": a.segment.recover_tick,
                "duration_ticks": a.segment.duration_ticks,
                "max_dist_km": a.segment.max_dist_km,
                "max_dist_tick": a.segment.max_dist_tick,
                "ref_tick_at_off": a.segment.ref_tick_at_off,
                "ref_tick_at_recovery": a.segment.ref_tick_at_recovery,
                "inferred_cause": a.inferred_cause,
                "n_rejections": a.n_rejections,
                "n_picker_changes": a.n_picker_changes,
                "n_commit_resets": a.n_commit_resets,
                "n_ocr_jumps": a.n_ocr_jumps,
                "issue_ticks": [
                    {
                        "tick": it.tick,
                        "category": it.category,
                        "detail": it.detail,
                        "severity": it.severity,
                    }
                    for it in a.issue_ticks
                ],
            }
            for a in analyses
        ],
    }


# ── Main ────────────────────────────────────────────────────────────────────

def evaluate(
    live_session: Path,
    reference_config: Path = DEFAULT_REFERENCE_CONFIG,
) -> Tuple[str, CompareResult, List[SegmentAnalysis]]:
    """Run the standard evaluation procedure.  Pure-ish: returns the
    structured results.  Caller decides where to print/write."""
    cfg = json.loads(reference_config.read_text())
    ref_session = (Path(__file__).parent.parent / "data" / "sessions"
                   / cfg["session"])
    if not ref_session.is_dir():
        raise SystemExit(
            f"reference session not found: {ref_session}\n"
            f"(referenced from {reference_config})"
        )

    live = _load_path(live_session / "trace.jsonl")
    ref = _load_path(ref_session / "trace.jsonl")
    ref = [s for s in ref if s.tick <= cfg["outbound_end_tick"]]
    if not live:
        raise SystemExit(f"no usable trace in {live_session}")

    result = compare(
        live, ref,
        tol_km=cfg["tolerance_km"],
        lookahead=cfg["lookahead"],
    )

    records = _load_trace_records(live_session)
    analyses = [_analyze_segment(seg, records) for seg in result.segments]
    verdict = _verdict(result)
    return verdict, result, analyses


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("live_session", type=Path,
                    help="data/sessions/explore_port_<ts>/ directory")
    ap.add_argument("--reference-config", type=Path,
                    default=DEFAULT_REFERENCE_CONFIG)
    args = ap.parse_args(argv)
    if not args.live_session.is_dir():
        raise SystemExit(f"not a directory: {args.live_session}")

    cfg = json.loads(args.reference_config.read_text())
    verdict, result, analyses = evaluate(
        args.live_session, args.reference_config,
    )
    md = _render_md(
        args.live_session.name, cfg["session"],
        verdict, result, analyses,
    )
    print(md)
    (args.live_session / "voyage_eval.md").write_text(md + "\n")
    (args.live_session / "voyage_eval.json").write_text(
        json.dumps(_render_json(verdict, result, analyses), indent=2) + "\n"
    )
    # Exit code reflects the verdict so this can gate CI / scripts.
    return {"PASS": 0, "WARN": 0, "FAIL": 1}.get(verdict, 2)


if __name__ == "__main__":
    sys.exit(main())
