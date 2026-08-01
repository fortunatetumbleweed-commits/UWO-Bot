# tools/phase6_spotcheck.py
#
# Spot-check the Phase 6 fingerprint registry against the labelled
# corpus.  Compares per-state classification accuracy to the baseline
# established when Phase 6 shipped (2026-05-03, commit b1f3687) and
# flags any state that's drifted significantly.
#
# Run any time after labelling work or perception-layer changes:
#   PYTHONPATH=. python tools/phase6_spotcheck.py
#
# Optional flags:
#   --per-state-limit N   Sample up to N labelled frames per state
#                         (default 10; use larger for fuller coverage,
#                         though OmniParser inference is ~0.5s/frame)
#   --json out.json       Save structured results
#   --strict              Exit 1 if any state drifts >10% from baseline
#                         (suitable for CI / pre-commit hooks)
#
# Output: per-state accuracy table + confusion matrix + delta vs baseline.

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional


# ── Phase 6 baseline (frozen at commit b1f3687, 2026-05-03) ─────────────────
#
# Each entry: state_id → expected accuracy as a fraction in [0.0, 1.0],
# measured on a sample of 10 labelled frames per state.  Any state
# dropping more than 10 percentage points below baseline is flagged
# as drift.
BASELINE_2026_05_03 = {
    "main_menu":      1.00,
    "world_map":      1.00,
    "port_overworld": 1.00,
    "sub_menu":       1.00,
    "sea":            1.00,
    "port_loading":   1.00,
    "building":       0.90,
    "port_map":       0.90,
    "loading":        0.10,   # known low — needs more sub-type labels
    # sailing_idle has no fingerprint; lives in the legacy Moondream path.
}

DRIFT_THRESHOLD = 0.10   # 10 percentage-point drop = drift


# ── Mapping from labelled screen_type → registry state_id ───────────────────

SCREEN_TYPE_TO_STATE = {
    "main_menu":         "main_menu",
    "world_map":         "world_map",
    "port_map":          "port_map",
    "building_interior": "building",
    "sub_menu":          "sub_menu",
    "port_overworld":    "port_overworld",
    "sea":               "sea",
    "port_loading":      "port_loading",
    "loading":           "loading",
}

LABELS_PATH = Path("data/labels.jsonl")
SESSIONS_DIR = Path("data/sessions")


def labelled_frames():
    if not LABELS_PATH.exists():
        return
    for line in LABELS_PATH.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("screen_type") in SCREEN_TYPE_TO_STATE:
            yield rec


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--per-state-limit", type=int, default=10,
                   help="Frames sampled per state (default 10)")
    p.add_argument("--json", type=str, default=None,
                   help="Save structured results to this path")
    p.add_argument("--strict", action="store_true",
                   help="Exit 1 if any state drifts >10pp below baseline")
    args = p.parse_args()

    # Lazy imports — keep top of file fast for --help.
    from PIL import Image

    # Importing _data registers all fingerprints into FINGERPRINT_REGISTRY.
    import vision.state_fingerprints_data  # noqa: F401
    from vision.omniparser import get_omniparser
    from vision.state_fingerprints import classify_via_registry

    parser = get_omniparser()
    if not parser.yolo_available():
        print("ERROR: OmniParser YOLO unavailable.  "
              "Run `python -m vision.omniparser --download` first.")
        return 2

    if not LABELS_PATH.exists():
        print(f"ERROR: {LABELS_PATH} not found.")
        return 2

    # Per-state stats
    by_state_total: Counter = Counter()
    by_state_correct: Counter = Counter()
    confusion: Counter = Counter()
    samples_taken: dict[str, int] = defaultdict(int)
    no_match_examples: dict[str, list[str]] = defaultdict(list)

    started_at = datetime.now()
    print(f"phase6_spotcheck — running at {started_at.isoformat(timespec='seconds')}")
    print(f"baseline: 2026-05-03 (Phase 6 commit b1f3687)")
    print(f"per-state sample limit: {args.per_state_limit}")
    print("-" * 72)

    for rec in labelled_frames():
        labelled_state = SCREEN_TYPE_TO_STATE[rec["screen_type"]]
        if samples_taken[labelled_state] >= args.per_state_limit:
            continue
        path = SESSIONS_DIR / rec["session_id"] / "frames" / rec["file"]
        if not path.exists():
            continue
        frame = Image.open(path)
        elements = parser.parse_fast(frame)
        result = classify_via_registry(elements, frame.width, frame.height)

        samples_taken[labelled_state] += 1
        by_state_total[labelled_state] += 1

        classified = result.state if result is not None else "<no_match>"
        if classified == labelled_state:
            by_state_correct[labelled_state] += 1
        elif classified == "<no_match>":
            if len(no_match_examples[labelled_state]) < 3:
                no_match_examples[labelled_state].append(
                    f"{rec['session_id']}/{rec['file']}"
                )
        confusion[(labelled_state, classified)] += 1

    elapsed = (datetime.now() - started_at).total_seconds()

    # Per-state accuracy table with delta vs baseline
    print(f"\nPer-state accuracy (elapsed {elapsed:.1f}s):")
    print(f"  {'state':<20s}  {'sampled':>8s}  {'correct':>8s}  "
          f"{'%':>6s}  {'baseline':>9s}  {'Δ':>7s}  flag")

    drift_states = []
    results_for_json = {}
    for state in sorted(SCREEN_TYPE_TO_STATE.values()):
        total = by_state_total[state]
        correct = by_state_correct[state]
        baseline = BASELINE_2026_05_03.get(state)
        if total == 0:
            print(f"  {state:<20s}  {'(no sample)':>8s}")
            continue
        pct = correct / total
        if baseline is None:
            delta_str = "n/a"
            flag = ""
        else:
            delta = pct - baseline
            delta_str = f"{delta * 100:+.0f}pp"
            if delta < -DRIFT_THRESHOLD:
                flag = "DRIFT"
                drift_states.append((state, pct, baseline))
            elif delta > DRIFT_THRESHOLD:
                flag = "improved"
            else:
                flag = ""
        print(f"  {state:<20s}  {total:>8d}  {correct:>8d}  "
              f"{pct*100:>5.0f}%  {(baseline or 0)*100:>8.0f}%  "
              f"{delta_str:>7s}  {flag}")
        results_for_json[state] = {
            "sampled":  total,
            "correct":  correct,
            "accuracy": pct,
            "baseline": baseline,
            "delta":    pct - baseline if baseline is not None else None,
        }

    # Confusion matrix entries (off-diagonal only)
    print("\nConfusion matrix (labelled → classified, off-diagonal):")
    off_diag = [(k, v) for k, v in confusion.items() if k[0] != k[1]]
    if not off_diag:
        print("  (none — no misclassifications)")
    else:
        for (lab, cls), c in sorted(off_diag, key=lambda x: -x[1]):
            print(f"  {c:3d}× {lab!r} → {cls!r}")

    # No-match examples for failure diagnosis
    if any(no_match_examples.values()):
        print("\nFrames returning <no_match> (first 3 per state):")
        for state, examples in no_match_examples.items():
            print(f"  {state}:")
            for ex in examples:
                print(f"    {ex}")

    # Drift summary
    print("\nDrift summary:")
    if not drift_states:
        print(f"  ✓ All states within {int(DRIFT_THRESHOLD*100)}pp of baseline")
    else:
        print(f"  {len(drift_states)} state(s) drifted >{int(DRIFT_THRESHOLD*100)}pp below baseline:")
        for state, pct, baseline in drift_states:
            print(f"    {state}: {pct*100:.0f}% (baseline {baseline*100:.0f}%)")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "timestamp": started_at.isoformat(),
            "elapsed_secs": elapsed,
            "per_state_limit": args.per_state_limit,
            "states": results_for_json,
            "confusion": [
                {"labelled": k[0], "classified": k[1], "count": v}
                for k, v in confusion.items()
            ],
            "drift": [
                {"state": s, "accuracy": p, "baseline": b}
                for s, p, b in drift_states
            ],
        }, indent=2))
        print(f"\nResults saved to {args.json}")

    if args.strict and drift_states:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
