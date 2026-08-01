"""Enforce the mini-map land/port consistency rule.

Rule (user-confirmed 2026-05-26): if a sea frame is tagged with
`minimap:port_anchor` OR `minimap:village_building`, it MUST also be
tagged `minimap:land` — ports and villages physically sit on land.
`minimap:settlement_name_text` ALONE does not imply land (name text
can scroll into the radar over open water while the icon is still
off-edge).

This script scans `data/labels.jsonl`, finds the latest record per
(session_id, file), and appends a corrected row whenever the rule is
violated.  The labeler's last-entry-wins lookup makes the corrections
effective; original rows are preserved for audit.

Idempotent — re-runs only fix newly-introduced violations.

Usage:
    python tools/fix_minimap_land_consistency.py           # dry run
    python tools/fix_minimap_land_consistency.py --apply   # write fix rows
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
LABELS_PATH = _REPO / "data" / "labels.jsonl"

ANCHOR_OR_BUILDING = {"minimap:port_anchor", "minimap:village_building"}


def _load_latest() -> dict:
    out: dict = {}
    with open(LABELS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                out[(r["session_id"], r["file"])] = r
            except Exception:
                continue
    return out


def _violations(latest: dict) -> list[dict]:
    out = []
    for (sid, f), r in latest.items():
        if r.get("screen_type") != "sea":
            continue
        tags = set(r.get("tags", []))
        if (ANCHOR_OR_BUILDING & tags) and "minimap:land" not in tags:
            out.append(r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the fix rows to labels.jsonl (otherwise dry run)")
    args = ap.parse_args()

    if not LABELS_PATH.exists():
        print(f"labels.jsonl not found at {LABELS_PATH}", file=sys.stderr)
        return 1

    latest = _load_latest()
    vios = _violations(latest)

    print(f"Scanned {len(latest)} latest-label rows.")
    print(f"Found {len(vios)} sea frames with port_anchor or village_building "
          "but no minimap:land tag.")
    print()
    for r in vios:
        which = next(t for t in ANCHOR_OR_BUILDING if t in set(r.get("tags", [])))
        print(f"  {r['session_id']}/{r['file']}")
        print(f"    has {which} — adding minimap:land")
        note = (r.get("notes") or "")[:120]
        if note:
            print(f"    note: {note}")
        print()

    if not args.apply:
        print("(dry run — re-run with --apply to write changes)")
        return 0

    if not vios:
        print("Nothing to fix.")
        return 0

    with open(LABELS_PATH, "a") as out:
        for r in vios:
            new_tags = sorted(set(r.get("tags", [])) | {"minimap:land"})
            new_row = dict(r)
            new_row["tags"] = new_tags
            new_row["labeled_at"] = datetime.now(timezone.utc).isoformat()
            new_row["labeled_by"] = "claude-consistency-fix"
            out.write(json.dumps(new_row) + "\n")
    print(f"Wrote {len(vios)} corrected rows to {LABELS_PATH}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
