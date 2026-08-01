"""Scrub the player-ship name 'Tumbler' from label notes that
mistakenly treated it as a port / settlement / NPC / location.

Auto-labeler runs from earlier today described frames with phrases
like "another ship 'Tumbler'", "settlement 'Tumbler'", "NPC fleet
'Tumbler'", etc.  'Tumbler' is in fact the PLAYER's own fleet name,
shown as a label above the centered ship sprite.  The misreading is
benign for tags (which are correct) but pollutes the notes.

This script:
  1. Reads data/labels.jsonl.
  2. For each row whose `notes` mention 'Tumbler', applies regex
     substitutions to remove the wrong references while preserving the
     surrounding scene description.
  3. Appends new rows with corrected notes (the labeler's "last entry
     wins" logic makes them effective).  Original rows are preserved
     so the change is auditable / reversible.

Usage:
    python tools/cleanup_tumbler_notes.py            # dry run — preview
    python tools/cleanup_tumbler_notes.py --apply    # write changes
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
LABELS_PATH = _REPO / "data" / "labels.jsonl"


# Patterns where 'Tumbler' is being mis-attributed to something other
# than the player's ship.  Each pattern → replacement.  Applied in order.
# Case-insensitive.  Quotes around the name are tolerated.
_T = r"['\"]?Tumbler['\"]?"

_PATTERNS = [
    # "another ship labeled 'Tumbler'" → "another ship"
    (re.compile(rf"\s*labeled\s+{_T}", re.IGNORECASE), ""),

    # "another (ship|vessel|fleet) 'Tumbler'" → "another (ship|vessel|fleet)"
    (re.compile(rf"\b(another |an? )?(NPC |enemy |nearby )?(fleet|vessel|ship|sailing ship)\s+\(?{_T}\)?",
                re.IGNORECASE),
     r"\1\2\3"),

    # "(coastal )?settlement 'Tumbler'" → "(coastal )?settlement"
    (re.compile(rf"\b((coastal|small|nearby) )?(settlement|location|village|town|port|city)\s+\(?{_T}\)?",
                re.IGNORECASE),
     r"\1\3"),

    # "(green )?ghost ship ... 'Tumbler'" → "(green )?ghost ship ..."
    (re.compile(rf"\bghost ship[^,.;]*{_T}", re.IGNORECASE), "ghost ship"),

    # bare "'Tumbler' visible" / "'Tumbler' ahead" — leftover after the
    # above subs strip the noun.  Drop the orphan reference.
    (re.compile(rf"\s*\(?{_T}\)?(\s+(visible|ahead|nearby|in (the )?distance))?",
                re.IGNORECASE),
     ""),
]

# Cleanup leftovers from the above substitutions.
_TIDY = [
    (re.compile(r"\s+,"),         ","),       # " ," → ","
    (re.compile(r"\(\s*\)"),      ""),        # empty parens
    (re.compile(r"\s+\."),        "."),       # " ." → "."
    (re.compile(r"\s{2,}"),       " "),       # double spaces
    (re.compile(r",\s*,"),        ","),       # ", ," → ","
    (re.compile(r"\s+;"),         ";"),       # " ;" → ";"
    (re.compile(r";\s*;"),        ";"),       # consecutive semicolons
]


_PRESERVE_TOKEN = "ZZ9KEEPPLAYERPHRASE9ZZ"

# Patterns where 'Tumbler' is being used correctly (referring to the
# player's own ship).  These get masked out before scrubbing so the
# scrub regexes don't touch them, then unmasked at the end.
_PRESERVE_PATTERNS = [
    re.compile(r"player'?s?\s+(own\s+)?(ship|vessel|fleet)\s+['\"]?Tumbler['\"]?",
               re.IGNORECASE),
    re.compile(r"['\"]?Tumbler['\"]?\s+\((player|own)[^)]*\)", re.IGNORECASE),
]


def _scrub(note: str) -> str | None:
    """Return cleaned note, or None if no change is needed.

    Preserves correct usages like 'player ship Tumbler' (the player's
    own fleet — accurate).  Only strips the misattributed references.
    """
    if not note or "tumbler" not in note.lower():
        return None

    # Mask correct usages so the scrub patterns don't touch them.
    masked = note
    preserved: list[str] = []
    for pat in _PRESERVE_PATTERNS:
        def _stash(m, _preserved=preserved):
            _preserved.append(m.group(0))
            return _PRESERVE_TOKEN

        masked = pat.sub(_stash, masked)

    cleaned = masked
    for pat, repl in _PATTERNS:
        cleaned = pat.sub(repl, cleaned)
    for pat, repl in _TIDY:
        cleaned = pat.sub(repl, cleaned).strip()

    # Restore preserved phrases.
    for original in preserved:
        cleaned = cleaned.replace(_PRESERVE_TOKEN, original, 1)

    # If after cleanup it's identical or empty, skip.
    if cleaned.strip() == note.strip():
        return None
    return cleaned


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually append corrected rows to labels.jsonl")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap the number of corrections (for debugging)")
    args = ap.parse_args()

    if not LABELS_PATH.exists():
        print(f"labels.jsonl not found at {LABELS_PATH}", file=sys.stderr)
        return 1

    # Load and keep ONE record per (session, file): last-entry wins.
    latest: dict[tuple, dict] = {}
    with open(LABELS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            latest[(r.get("session_id"), r.get("file"))] = r

    corrections: list[dict] = []
    skipped_no_change = 0
    skipped_preserve = 0
    for (sid, fname), r in latest.items():
        note = r.get("notes")
        if not note or "tumbler" not in note.lower():
            continue
        cleaned = _scrub(note)
        if cleaned is None:
            skipped_no_change += 1
            continue
        if args.limit and len(corrections) >= args.limit:
            break
        # Build the new corrected row.  Carry tags / screen_type / etc.
        # forward; flip labeled_by so it's clear this is a programmatic edit.
        new = dict(r)
        new["notes"] = cleaned
        new["labeled_at"] = datetime.now(timezone.utc).isoformat()
        new["labeled_by"] = "claude-cleanup-script"
        corrections.append({
            "old": note, "new": cleaned, "session": sid, "file": fname,
            "row": new,
        })

    print(f"found {len(corrections)} notes to correct "
          f"({skipped_no_change} mentions left as-is — already correct).")
    print()
    print("first 8 examples:")
    for c in corrections[:8]:
        print(f"  {c['session']}/{c['file']}")
        print(f"    OLD: {c['old']}")
        print(f"    NEW: {c['new']}")
        print()

    if not args.apply:
        print("(dry run — re-run with --apply to write changes)")
        return 0

    with open(LABELS_PATH, "a") as f:
        for c in corrections:
            f.write(json.dumps(c["row"]) + "\n")
    print(f"wrote {len(corrections)} corrected rows to {LABELS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
