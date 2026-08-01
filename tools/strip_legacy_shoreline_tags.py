"""Strip the legacy yolo:S1..yolo:S7 tags from latest sea rows.

After running tools/migrate_shoreline_to_binary.py the binary
shore:* tags are present, but the old yolo:S* tags are still on the
same row.  The labeler renders both, which is confusing now that the
schema only defines shore:*.

This script appends a clean row per affected (session, file) where
the legacy yolo:S* tags are removed.  History (older rows) is
preserved for audit; only the LATEST row's view changes.

Idempotent — re-running on already-clean rows is a no-op.

Usage:
    python tools/strip_legacy_shoreline_tags.py            # dry run
    python tools/strip_legacy_shoreline_tags.py --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
LABELS_PATH = _REPO / "data" / "labels.jsonl"

LEGACY_S_TAGS = {
    "yolo:S1_no_land",
    "yolo:S2_land_left",
    "yolo:S3_land_right",
    "yolo:S4_land_ahead",
    "yolo:S5_land_both",
    "yolo:S6_land_ahead_side",
    "yolo:S7_faint_land",
}


def _load_latest() -> dict:
    out: dict = {}
    with open(LABELS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            key = (r.get("session_id"), r.get("file"))
            if key[0] and key[1]:
                out[key] = r
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write stripped rows to labels.jsonl (else dry run)")
    args = ap.parse_args()

    if not LABELS_PATH.exists():
        print(f"labels.jsonl not found at {LABELS_PATH}", file=sys.stderr)
        return 1

    latest = _load_latest()
    by_old = Counter()
    fixes: list[dict] = []
    for (sid, fname), r in latest.items():
        if r.get("screen_type") != "sea":
            continue
        tags = list(r.get("tags") or [])
        legacy = LEGACY_S_TAGS & set(tags)
        if not legacy:
            continue
        for t in legacy:
            by_old[t] += 1
        new_tags = sorted(set(tags) - LEGACY_S_TAGS)
        new_row = dict(r)
        new_row["tags"] = new_tags
        new_row["labeled_at"] = datetime.now(timezone.utc).isoformat()
        new_row["labeled_by"] = "claude-shoreline-strip"
        fixes.append(new_row)

    print(f"sea frames whose latest row carries a legacy yolo:S* tag: "
          f"{len(fixes)}")
    print()
    print("breakdown by legacy tag:")
    for t in sorted(LEGACY_S_TAGS):
        print(f"  {t:30s} {by_old[t]}")
    print()

    if not args.apply:
        print("(dry run — re-run with --apply to write stripped rows)")
        return 0

    if not fixes:
        print("Nothing to strip.")
        return 0

    with open(LABELS_PATH, "a") as out:
        for row in fixes:
            out.write(json.dumps(row) + "\n")
    print(f"wrote {len(fixes)} stripped rows to {LABELS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
