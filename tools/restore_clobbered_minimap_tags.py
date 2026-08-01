"""Restore mini-map tags lost when the sea-view pass overwrote them.

Earlier, `tools/auto_label_sea_capture.py` only skipped previously
labelled frames whose `labeled_by` was exactly "human" or "claude" —
not the auxiliary variants ("claude-minimap",
"claude-consistency-fix", …).  When the helper script reran pass 1
after the mini-map pass had already added `minimap:*` tags, the
sea-view labeller re-wrote the row without those tags.  Last-entry-
wins lookup therefore lost them.

This recovery script:
  1. Walks the full history in `data/labels.jsonl`.
  2. For each (session, file), finds the most recent row that DID
     have `minimap:*` tags.
  3. If the latest row no longer has those tags AND `screen_type ==
     "sea"`, appends a new merged row (latest tags ∪ recovered
     minimap tags) with `labeled_by="claude-restore-minimap"`.

Idempotent — re-running won't append duplicate fixes because once the
latest row contains the minimap tags again, the violation goes away.

Usage:
    python tools/restore_clobbered_minimap_tags.py            # dry run
    python tools/restore_clobbered_minimap_tags.py --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
LABELS_PATH = _REPO / "data" / "labels.jsonl"


def _walk_history() -> dict[tuple[str, str], list[dict]]:
    history: dict[tuple[str, str], list[dict]] = defaultdict(list)
    with open(LABELS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            sid = r.get("session_id")
            fname = r.get("file")
            if sid and fname:
                history[(sid, fname)].append(r)
    return history


def _find_clobbered(history: dict) -> list[tuple[tuple, list[str], dict]]:
    out = []
    for key, rows in history.items():
        latest = rows[-1]
        if latest.get("screen_type") != "sea":
            continue
        latest_mm = {t for t in latest.get("tags", []) if t.startswith("minimap:")}
        if latest_mm:
            continue
        # Walk backwards for the most recent row that DID have minimap tags.
        recovered: list[str] = []
        source_row: dict | None = None
        for r in reversed(rows[:-1]):
            mm = sorted(t for t in r.get("tags", []) if t.startswith("minimap:"))
            if mm:
                recovered = mm
                source_row = r
                break
        if recovered:
            out.append((key, recovered, source_row))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write recovery rows to labels.jsonl (else dry run)")
    args = ap.parse_args()

    if not LABELS_PATH.exists():
        print(f"labels.jsonl not found at {LABELS_PATH}", file=sys.stderr)
        return 1

    history = _walk_history()
    clobbered = _find_clobbered(history)
    print(f"scanned {len(history)} (session, file) pairs.")
    print(f"found {len(clobbered)} sea frames where minimap:* tags were "
          "present earlier but missing from the latest row.")
    print()

    if not clobbered:
        print("Nothing to restore.")
        return 0

    from collections import Counter
    by_session = Counter(k[0][0] for k in [(c[0],) for c in clobbered])
    by_session = Counter(c[0][0] for c in clobbered)
    print("top sessions affected:")
    for s, n in by_session.most_common(15):
        print(f"  {s}: {n}")
    print()

    # Sample one entry to show user
    sample_key, sample_tags, sample_src = clobbered[0]
    print(f"sample: {sample_key[0]}/{sample_key[1]}")
    print(f"  recovered tags: {sample_tags}")
    print(f"  source row: labeled_by={sample_src.get('labeled_by')!r} "
          f"labeled_at={sample_src.get('labeled_at')!r}")
    print()

    if not args.apply:
        print("(dry run — re-run with --apply to write recovery rows)")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    n_written = 0
    with open(LABELS_PATH, "a") as out:
        for key, recovered, src in clobbered:
            latest = history[key][-1]
            merged = sorted(set(latest.get("tags", [])) | set(recovered))
            row = dict(latest)
            row["tags"] = merged
            row["labeled_at"] = now
            row["labeled_by"] = "claude-restore-minimap"
            # Make the audit trail readable.
            note = row.get("notes") or ""
            tag_str = " ".join(recovered)
            stamp = (f"[restored minimap tags from {src.get('labeled_by')} "
                     f"row at {src.get('labeled_at')}: {tag_str}]")
            row["notes"] = (note + " " + stamp).strip() if note else stamp
            out.write(json.dumps(row) + "\n")
            n_written += 1
    print(f"wrote {n_written} recovery rows to {LABELS_PATH}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
