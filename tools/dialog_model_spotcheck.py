"""Phase-1 measurement: DialogModel fire-rate on labelled dialog frames.

Acceptance bar from docs/dialog_and_event_models.md:
  - DialogModel fires on ≥ 95% of `dialog_*` labelled frames.
  - Disagreement with the legacy interruptor pipeline < 10%.

Usage:
    python tools/dialog_model_spotcheck.py [--max-frames-per-label 20]

Outputs a summary table to stdout; full per-frame trace to
data/calibration/dialog_spotcheck_<ts>.jsonl.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PIL import Image                                            # noqa: E402

from vision.omniparser import get_omniparser                      # noqa: E402
from vision.region_detectors.dialog import detect_dialog          # noqa: E402
from vision.region_detectors.building_npc_overlay import (        # noqa: E402
    detect_building_npc_overlay,
)


DIALOG_LABEL_PREFIX = "dialog_"
# Other overlay-like labels we want to spot-check.
# - building_npc_overlay → expected to be caught by detect_building_npc_overlay
#   (a separate detector from DialogModel; see docs/dialog_and_event_models.md)
# - result_screen / announcement → not yet covered; EventPerkModel territory
ADDITIONAL_OVERLAY_LABELS = {"building_npc_overlay", "result_screen", "announcement"}


def _run_legacy_interruptor(frame, parser) -> bool:
    """Return True when the legacy `_detect_interruptors` pipeline
    would fire on this frame.

    Used only for agreement measurement (Phase 1 acceptance bar #2).
    Imports are inside the function so the spotcheck doesn't pay for
    loading the brain pipeline when --no-agreement is set.
    """
    try:
        from actions.sail_actions import _ocr_frame
        from brain.perceive       import _detect_interruptors
        tokens = _ocr_frame(frame, min_conf=0.3)
        active, _ = _detect_interruptors(frame, tokens)
        return len(active) > 0
    except Exception:
        return False


def collect_frames(max_per_label: int):
    """Group labelled frames by their LATEST screen_type.

    labels.jsonl is append-only — a frame relabelled over time has
    multiple entries.  We keep only the entry with the latest
    `labeled_at` per (session_id, file) so each frame is counted
    under its current label.
    """
    labels_path = REPO_ROOT / "data" / "labels.jsonl"
    by_frame: dict = {}
    for line in labels_path.read_text().splitlines():
        r = json.loads(line)
        key = (r["session_id"], r["file"])
        prev = by_frame.get(key)
        if prev is None or r.get("labeled_at", "") >= prev.get("labeled_at", ""):
            by_frame[key] = r

    out = defaultdict(list)
    for r in by_frame.values():
        st = r.get("screen_type", "")
        if not (st.startswith(DIALOG_LABEL_PREFIX) or st in ADDITIONAL_OVERLAY_LABELS):
            continue
        if len(out[st]) >= max_per_label:
            continue
        p = REPO_ROOT / "data" / "sessions" / r["session_id"] / "frames" / r["file"]
        if p.exists():
            out[st].append(p)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-frames-per-label", type=int, default=20)
    ap.add_argument("--measure-agreement", action="store_true",
                    help="Also run legacy _detect_interruptors and report agreement.")
    args = ap.parse_args()

    frames_by_label = collect_frames(args.max_frames_per_label)
    total = sum(len(v) for v in frames_by_label.values())
    print(f"Loaded {total} frames across {len(frames_by_label)} dialog labels.")

    out_dir = REPO_ROOT / "data" / "calibration"
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = out_dir / f"dialog_spotcheck_{time.strftime('%Y%m%dT%H%M%S')}.jsonl"

    parser = get_omniparser()
    parser.yolo_available()

    fired_by_label   = Counter()
    n_by_label       = Counter()
    kind_distribution = Counter()
    overall_fired    = 0
    agreement_buckets = Counter()  # (dialog_fired, legacy_fired) -> count

    with open(trace_path, "w") as trace_file:
        for label, paths in frames_by_label.items():
            for p in paths:
                img = Image.open(p)
                els = parser.parse_fast(img)
                d = detect_dialog(els, img.width, img.height)
                npc = detect_building_npc_overlay(els, img.width, img.height)
                # For building_npc_overlay labels, the NPC detector is the
                # responsible one.  For dialog_* labels, DialogModel.  Either
                # firing counts for that label's fire rate.
                if label == "building_npc_overlay":
                    fired = npc is not None
                    kind  = "building_npc_overlay" if fired else None
                    anchors = list(npc.anchors_fired) if npc else []
                else:
                    fired = d is not None
                    kind  = d.kind() if d else None
                    anchors = list(d.anchors_fired) if d else []

                n_by_label[label] += 1
                if fired:
                    fired_by_label[label] += 1
                    kind_distribution[kind] += 1
                    overall_fired += 1

                legacy_fired: Optional[bool] = None
                if args.measure_agreement:
                    legacy_fired = _run_legacy_interruptor(img, parser)
                    agreement_buckets[(fired, legacy_fired)] += 1

                trace_file.write(json.dumps({
                    "label":    label,
                    "session":  p.parent.parent.name,
                    "file":     p.name,
                    "fired":    fired,
                    "kind":     kind,
                    "anchors":  anchors,
                    "legacy_fired": legacy_fired,
                }) + "\n")
                legacy_marker = (
                    f"  legacy={'FIRE' if legacy_fired else 'miss'}"
                    if legacy_fired is not None else ""
                )
                print(f"  {label:22s} {'FIRE' if fired else 'miss':4s}  "
                      f"kind={kind!r}  anchors={anchors}{legacy_marker}  {p.name}",
                      flush=True)

    print()
    print("=" * 70)
    print(f"Fire rate per label:")
    for label in sorted(n_by_label):
        n = n_by_label[label]
        k = fired_by_label[label]
        pct = (100 * k / n) if n else 0
        print(f"  {label:22s} {k:3d}/{n:3d}  ({pct:5.1f}%)")

    print()
    print(f"Overall fire rate: {overall_fired}/{total} = {100 * overall_fired / max(total, 1):5.1f}%")
    print(f"DialogKind distribution: {dict(kind_distribution)}")
    if args.measure_agreement and total:
        agree = (agreement_buckets[(True, True)]
                 + agreement_buckets[(False, False)])
        disagree = (agreement_buckets[(True, False)]
                    + agreement_buckets[(False, True)])
        print()
        print(f"Agreement with legacy _detect_interruptors:")
        print(f"  both fired         {agreement_buckets[(True, True)]:3d}")
        print(f"  both missed        {agreement_buckets[(False, False)]:3d}")
        print(f"  dialog only fired  {agreement_buckets[(True, False)]:3d}  (new coverage)")
        print(f"  legacy only fired  {agreement_buckets[(False, True)]:3d}  (regression)")
        print(f"  overall agreement  {agree}/{total} = {100*agree/total:5.1f}%")
        print(f"  overall disagree   {disagree}/{total} = {100*disagree/total:5.1f}%")
    print(f"Trace: {trace_path}")


if __name__ == "__main__":
    main()
