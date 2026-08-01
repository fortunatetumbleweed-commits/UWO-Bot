"""Remove no-op claude-minimap rows from data/labels.jsonl.

Pre-fix, `tools/label_minimap.py` processed every labeled frame in a
session — including non-sea frames (sub_menu, building_interior,
dialog_*, etc.) which have no mini-map.  Claude correctly returned no
tags for those, but the script still appended a new row with the
original tags + `labeled_by="claude-minimap"`.  Those rows are pure
noise:
  - they don't add any minimap:* tags
  - they overwrite the original labeled_by provenance (e.g. "human")
  - their screen_type is NOT "sea"

This script rewrites labels.jsonl with such rows removed.  A timestamped
backup is created first.

Usage:
    python tools/cleanup_bogus_minimap_rows.py            # dry run — preview
    python tools/cleanup_bogus_minimap_rows.py --apply    # write the cleaned file
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
LABELS_PATH = _REPO / "data" / "labels.jsonl"


def _is_bogus(rec: dict) -> bool:
    if rec.get("labeled_by") != "claude-minimap":
        return False
    if rec.get("screen_type") == "sea":
        return False
    # Has any minimap:* tag → was a real contribution, keep
    if any(t.startswith("minimap:") for t in rec.get("tags", [])):
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="rewrite labels.jsonl (otherwise dry run)")
    args = ap.parse_args()

    if not LABELS_PATH.exists():
        print(f"labels.jsonl not found at {LABELS_PATH}", file=sys.stderr)
        return 1

    keep, drop = [], []
    with open(LABELS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                keep.append(line)
                continue
            if _is_bogus(r):
                drop.append(r)
            else:
                keep.append(line)

    print(f"total rows: {len(keep) + len(drop)}")
    print(f"to drop:    {len(drop)} bogus claude-minimap rows on non-sea frames")
    print(f"to keep:    {len(keep)}")
    print()
    # Sample drops by screen_type
    from collections import Counter
    by_type = Counter(r.get("screen_type") for r in drop)
    if by_type:
        print("breakdown of dropped rows by screen_type:")
        for st, n in by_type.most_common():
            print(f"  {st}: {n}")
    print()

    if not args.apply:
        print("(dry run — re-run with --apply to rewrite)")
        return 0

    if not drop:
        print("Nothing to clean up.")
        return 0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = LABELS_PATH.with_suffix(f".jsonl.bak_before_minimap_cleanup_{ts}")
    shutil.copy(LABELS_PATH, backup)
    print(f"backup written: {backup}")

    with open(LABELS_PATH, "w") as f:
        for line in keep:
            f.write(line + "\n")
    print(f"wrote {len(keep)} rows back to {LABELS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
