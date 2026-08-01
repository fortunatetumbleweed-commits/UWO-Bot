"""Migrate single-select S1..S7 shoreline tags to ship-relative binaries.

Until 2026-05-27 the shoreline schema was single-select S1..S7.  We
switched to three independent ship-relative binaries because:
  - the old "land left / land right" was screen-position, but rivers
    and frontal walls produce the same screen pattern with very
    different steering implications;
  - "land both sides" was being applied to both rivers AND frontal
    walls;
  - the steering decision actually wants the three bits directly.

New schema (per data/knowledge/screen_tags.json):
  shore:land_ahead       — bow path blocked by land
  shore:land_port        — land off the port (left) side of the ship
  shore:land_starboard   — land off the starboard (right) side
  shore:faint_distant    — faint distant land on the horizon (S7 carry-over)

Migration mapping (user-confirmed 2026-05-27):
  S1 no_land             → (none)
  S2 land_left           → port
  S3 land_right          → starboard
  S4 land_ahead          → ahead   (visually a wall — no side bits)
  S5 land_both           → port + starboard   (river/strait — clear ahead)
  S6 land_ahead_side     → ahead + (NEEDS REVIEW: pick port OR starboard)
  S7 faint_land          → faint_distant

Per the user's note on 2026-05-27:
  > "right now the labeling I have done are based on the lands position
  > to the ship, so land ahead frames can be safely mapped to wall ahead.
  > I will review and see what need to be adjusted, I can assume it
  > won't be too many"

So this script auto-migrates everything; S6 frames get land_ahead set
and a `notes` flag asking the user to pick the side.  The user reviews
the flagged frames in the labeler.

The migration is APPEND-ONLY — adds new rows with the binary tags +
labeled_by="claude-shoreline-migrate".  Original rows are preserved.
Re-running is idempotent: only adds rows whose binaries don't already
match what the legacy tag implies.

Usage:
    python tools/migrate_shoreline_to_binary.py            # dry run
    python tools/migrate_shoreline_to_binary.py --apply
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


# Mapping: old tag id -> set of new tag ids
OLD_TO_NEW: dict[str, set[str]] = {
    "yolo:S1_no_land":         set(),
    "yolo:S2_land_left":       {"shore:land_port"},
    "yolo:S3_land_right":      {"shore:land_starboard"},
    "yolo:S4_land_ahead":      {"shore:land_ahead"},
    "yolo:S5_land_both":       {"shore:land_port", "shore:land_starboard"},
    "yolo:S6_land_ahead_side": {"shore:land_ahead"},  # side needs human review
    "yolo:S7_faint_land":      {"shore:faint_distant"},
}

# Frames migrated from S6 get this note appended so the user knows to
# pick the side (port vs starboard) in the labeler.
S6_REVIEW_NOTE = (
    "shoreline migration: pick port or starboard side for S6 (ahead+side)"
)

NEW_TAGS = {"shore:land_ahead", "shore:land_port",
            "shore:land_starboard", "shore:faint_distant"}
LEGACY_S_TAGS = set(OLD_TO_NEW.keys())


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


def _migrate_row(r: dict) -> tuple[dict | None, str | None]:
    """Return (new_record, review_note) or (None, None) if no migration
    is needed for this row."""
    if r.get("screen_type") != "sea":
        return None, None
    tags = list(r.get("tags") or [])
    tagset = set(tags)
    s_tags = LEGACY_S_TAGS & tagset
    if not s_tags:
        return None, None     # no legacy S* — nothing to migrate
    # Compute the union of new tags implied by the legacy tags present.
    implied: set[str] = set()
    for s in s_tags:
        implied |= OLD_TO_NEW[s]
    # Tags already on the row in the new vocabulary
    existing_new = NEW_TAGS & tagset
    # If all implied bits already present, no migration needed.
    if implied.issubset(existing_new):
        return None, None
    # Build the new row.  Keep the legacy S* tags so labelled history
    # is preserved; just ADD the binary tags.
    merged = sorted(tagset | implied)
    new_row = dict(r)
    new_row["tags"] = merged
    new_row["labeled_at"] = datetime.now(timezone.utc).isoformat()
    new_row["labeled_by"] = "claude-shoreline-migrate"
    review_note: str | None = None
    if "yolo:S6_land_ahead_side" in s_tags:
        review_note = S6_REVIEW_NOTE
        note = (new_row.get("notes") or "").strip()
        new_row["notes"] = (note + " | " + review_note).strip(" |") if note else review_note
    return new_row, review_note


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write migration rows (otherwise dry run)")
    args = ap.parse_args()

    if not LABELS_PATH.exists():
        print(f"labels.jsonl not found at {LABELS_PATH}", file=sys.stderr)
        return 1

    latest = _load_latest()
    migrations: list[dict] = []
    reviews: list[tuple] = []
    by_old = Counter()
    for key, r in latest.items():
        new_row, review = _migrate_row(r)
        if new_row is None:
            continue
        migrations.append(new_row)
        for s in LEGACY_S_TAGS & set(r.get("tags") or []):
            by_old[s] += 1
        if review:
            reviews.append(key)

    print(f"latest sea-frame labels scanned: "
          f"{sum(1 for r in latest.values() if r.get('screen_type') == 'sea')}")
    print(f"frames needing migration: {len(migrations)}")
    print()
    print("breakdown by source legacy tag:")
    for s in [
        "yolo:S1_no_land",
        "yolo:S2_land_left",
        "yolo:S3_land_right",
        "yolo:S4_land_ahead",
        "yolo:S5_land_both",
        "yolo:S6_land_ahead_side",
        "yolo:S7_faint_land",
    ]:
        print(f"  {s:30s} {by_old[s]}")
    print()
    print(f"S6 frames flagged for human review: {len(reviews)}")

    if reviews:
        print("first 5:")
        for sid, fname in reviews[:5]:
            print(f"  {sid}/{fname}")
    print()

    if not args.apply:
        print("(dry run — re-run with --apply to write migration rows)")
        return 0

    if not migrations:
        print("Nothing to migrate.")
        return 0

    with open(LABELS_PATH, "a") as out:
        for row in migrations:
            out.write(json.dumps(row) + "\n")
    print(f"wrote {len(migrations)} migration rows to {LABELS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
