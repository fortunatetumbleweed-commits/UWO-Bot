# vision/fingerprint_survey.py
#
# Phase 6 L1 — extract candidate state fingerprints from labelled frames.
#
# Reads data/labels.jsonl, groups by screen_type, runs OmniParser on a
# sample of frames per type, and reports region-keyed label distributions.
# The output is the data source for hand-authoring Fingerprint entries
# in vision/state_fingerprints.py.
#
# Usage:
#   python -m vision.fingerprint_survey                    # all states
#   python -m vision.fingerprint_survey main_menu          # single state
#   python -m vision.fingerprint_survey --sample 8         # use 8 frames per state
#   python -m vision.fingerprint_survey --json out.json    # save results
#
# Output (per state):
#   - per-frame element counts by element_type
#   - region-keyed label sets (which labels appear in which 9-grid cell
#     across cross-frame consistency)
#   - candidate signals (labels appearing in >= 80% of sampled frames)

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from PIL import Image
from loguru import logger

from vision.omniparser import DetectedElement, get_omniparser

LABELS_PATH    = Path("data/labels.jsonl")
SESSIONS_DIR   = Path("data/sessions")

# Nine-grid regions (normalised) — labels are placed in the grid cell
# their centre falls in.  This is a coarse partitioning, intended only
# for fingerprint discovery; actual fingerprints can use finer regions.
_GRID = {
    "TL": (0.00, 0.00, 0.33, 0.33),
    "TC": (0.33, 0.00, 0.67, 0.33),
    "TR": (0.67, 0.00, 1.00, 0.33),
    "ML": (0.00, 0.33, 0.33, 0.67),
    "MC": (0.33, 0.33, 0.67, 0.67),
    "MR": (0.67, 0.33, 1.00, 0.67),
    "BL": (0.00, 0.67, 0.33, 1.00),
    "BC": (0.33, 0.67, 0.67, 1.00),
    "BR": (0.67, 0.67, 1.00, 1.00),
}

# Threshold for "stable signal" — label must appear in this fraction of
# sampled frames within the same grid cell.
DEFAULT_STABILITY_THRESHOLD = 0.80


@dataclass
class FrameElements:
    session_id: str
    file:       str
    width:      int
    height:     int
    elements:   list[DetectedElement] = field(default_factory=list)


@dataclass
class StateSurvey:
    state_id:           str
    n_labelled_total:   int
    n_sampled:          int
    per_frame_counts:   list[dict] = field(default_factory=list)
    # Region cell → label → count (across sampled frames)
    region_labels:      dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    # Region cell → element_type → count (across sampled frames)
    region_types:       dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    # Aggregate label → count (across all regions)
    all_labels:         Counter = field(default_factory=Counter)


def _grid_cell(el: DetectedElement, w: int, h: int) -> str:
    cx_n = el.cx / w
    cy_n = el.cy / h
    for cell, (l, t, r, b) in _GRID.items():
        if l <= cx_n <= r and t <= cy_n <= b:
            return cell
    return "??"


def labelled_frames() -> Iterator[dict]:
    for line in LABELS_PATH.read_text().splitlines():
        if not line.strip():
            continue
        yield json.loads(line)


def _frame_path(rec: dict) -> Path:
    return SESSIONS_DIR / rec["session_id"] / "frames" / rec["file"]


def survey_state(
    state_id: str,
    sample_limit: int = 5,
    stability_threshold: float = DEFAULT_STABILITY_THRESHOLD,
) -> StateSurvey:
    """Run OmniParser on up to *sample_limit* labelled frames for the
    given screen_type and return a StateSurvey."""
    parser = get_omniparser()
    if not parser.yolo_available():
        raise RuntimeError("OmniParser YOLO unavailable")

    matching = [rec for rec in labelled_frames() if rec.get("screen_type") == state_id]
    survey = StateSurvey(state_id=state_id, n_labelled_total=len(matching), n_sampled=0)

    # Spread samples across sessions for diversity
    by_session: dict[str, list[dict]] = defaultdict(list)
    for rec in matching:
        by_session[rec["session_id"]].append(rec)
    sampled: list[dict] = []
    while len(sampled) < sample_limit and any(by_session.values()):
        for sid in list(by_session.keys()):
            if len(sampled) >= sample_limit:
                break
            if by_session[sid]:
                sampled.append(by_session[sid].pop(0))
            else:
                del by_session[sid]

    for rec in sampled:
        path = _frame_path(rec)
        if not path.exists():
            continue
        frame = Image.open(path)
        elements = parser.parse_fast(frame)
        survey.n_sampled += 1

        type_counts = Counter(e.element_type for e in elements)
        survey.per_frame_counts.append({
            "session": rec["session_id"],
            "file":    rec["file"],
            "size":    f"{frame.width}x{frame.height}",
            "total":   len(elements),
            "by_type": dict(type_counts),
        })

        # Track per-frame label observations to compute cross-frame
        # consistency later.  For each region, build a SET of labels
        # seen on this frame (de-duped) so a label appearing twice on
        # one frame doesn't get over-weighted.
        frame_region_labels: dict[str, set[str]] = defaultdict(set)
        for el in elements:
            cell = _grid_cell(el, frame.width, frame.height)
            label = (el.label or "").lower().strip()
            survey.region_types[cell][el.element_type] += 1
            if label:
                frame_region_labels[cell].add(label)
                survey.all_labels[label] += 1
        for cell, labels in frame_region_labels.items():
            for lbl in labels:
                survey.region_labels[cell][lbl] += 1

    return survey


def stable_signals(
    survey: StateSurvey,
    stability_threshold: float = DEFAULT_STABILITY_THRESHOLD,
) -> dict[str, list[tuple[str, int]]]:
    """Extract labels that appear in >= stability_threshold fraction
    of sampled frames within each region cell."""
    if survey.n_sampled == 0:
        return {}
    min_count = max(1, int(survey.n_sampled * stability_threshold))
    stable: dict[str, list[tuple[str, int]]] = {}
    for cell, lbl_counts in survey.region_labels.items():
        passing = [(lbl, c) for lbl, c in lbl_counts.items() if c >= min_count]
        if passing:
            stable[cell] = sorted(passing, key=lambda x: -x[1])
    return stable


def report_survey(survey: StateSurvey, threshold: float = DEFAULT_STABILITY_THRESHOLD) -> str:
    lines = []
    lines.append("=" * 72)
    lines.append(
        f"State: {survey.state_id!r}  "
        f"(labelled total: {survey.n_labelled_total}, sampled: {survey.n_sampled})"
    )
    lines.append("=" * 72)

    if survey.n_sampled == 0:
        lines.append("  (no usable sampled frames)")
        return "\n".join(lines)

    # Per-frame summary
    lines.append("\nPer-frame element counts:")
    for f in survey.per_frame_counts:
        lines.append(
            f"  {f['session']}/{f['file']}  ({f['size']}): "
            f"total={f['total']}  by_type={f['by_type']}"
        )

    # Region element-type density (used to spot button-dense regions)
    lines.append("\nRegion element-type density (sum across sampled frames):")
    for cell in ["TL", "TC", "TR", "ML", "MC", "MR", "BL", "BC", "BR"]:
        types = survey.region_types.get(cell, Counter())
        if not types:
            continue
        lines.append(f"  {cell}: {dict(types)}")

    # Stable labels per region
    lines.append(
        f"\nStable labels per region (≥{int(threshold * 100)}% cross-frame "
        f"consistency, threshold={int(survey.n_sampled * threshold)}/{survey.n_sampled}):"
    )
    sigs = stable_signals(survey, threshold)
    if not sigs:
        lines.append("  (no labels reach the consistency threshold)")
    else:
        for cell in ["TL", "TC", "TR", "ML", "MC", "MR", "BL", "BC", "BR"]:
            if cell not in sigs:
                continue
            lines.append(f"  {cell}:")
            for lbl, c in sigs[cell][:15]:
                lines.append(f"    {c}/{survey.n_sampled}× {lbl!r}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("state", nargs="?",
                   help="Specific screen_type to survey (default: all)")
    p.add_argument("--sample", type=int, default=5,
                   help="Frames to sample per state (default 5)")
    p.add_argument("--threshold", type=float, default=DEFAULT_STABILITY_THRESHOLD,
                   help="Cross-frame consistency threshold (0.0-1.0)")
    p.add_argument("--json", type=str, default=None,
                   help="Save survey results as JSON")
    args = p.parse_args()

    # Discover all screen_types in labels
    all_types = sorted({
        rec.get("screen_type") for rec in labelled_frames()
        if rec.get("screen_type")
    })
    targets = [args.state] if args.state else all_types

    surveys = []
    for state in targets:
        try:
            s = survey_state(state, args.sample, args.threshold)
        except Exception as e:
            logger.error(f"survey failed for {state!r}: {e}")
            continue
        print(report_survey(s, args.threshold))
        surveys.append(s)

    if args.json:
        out = []
        for s in surveys:
            out.append({
                "state_id":         s.state_id,
                "n_labelled_total": s.n_labelled_total,
                "n_sampled":        s.n_sampled,
                "per_frame":        s.per_frame_counts,
                "stable_signals":   stable_signals(s, args.threshold),
                "region_types":     {k: dict(v) for k, v in s.region_types.items()},
            })
        Path(args.json).write_text(json.dumps(out, indent=2))
        print(f"\nSaved survey results to {args.json}")


if __name__ == "__main__":
    main()
