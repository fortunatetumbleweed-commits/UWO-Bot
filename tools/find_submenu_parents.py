#!/usr/bin/env python
"""Worklist generator for back-labelling sub_menu entries with
`parent_building`.

For each `screen_type=sub_menu` entry in `data/labels.jsonl` that
lacks `parent_building`, print the immediately-preceding frames in
the same session.  Those frames typically show the building the bot
entered before navigating to the sub-menu — you can read off the
parent and update the label.

Usage:
    python tools/find_submenu_parents.py             # human-readable
    python tools/find_submenu_parents.py --json      # JSON dump
    python tools/find_submenu_parents.py --context 5 # more neighbours

Origin: 2026-05-13.  Sub-menu entries in the existing labelled corpus
don't carry parent_building; the layout-generation tooling needs it.
This tool gives a per-entry worklist so the back-labelling pass can
proceed without re-watching session recordings.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

_LABELS = Path("data/labels.jsonl")


def _load_grouped_by_session() -> dict[str, list[dict]]:
    """Dedup by (session_id, file) keeping the LAST entry — matches
    the labeler's append-on-relabel + last-wins-on-read behaviour."""
    by_key: dict[tuple, dict] = {}
    if not _LABELS.exists():
        return defaultdict(list)
    for line in _LABELS.read_text().splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = d.get("session_id")
        if not sid:
            continue
        by_key[(sid, d.get("file"))] = d   # last write wins

    by_session: dict[str, list[dict]] = defaultdict(list)
    for d in by_key.values():
        by_session[d["session_id"]].append(d)
    # Sort each session by file name — frame indices encode order:
    # '0010_2105411488.png' comes after '0009_*.png'.
    for entries in by_session.values():
        entries.sort(key=lambda d: d.get("file", ""))
    return by_session


def _parent_building_of(entry: dict) -> str | None:
    """Two valid sources for the parent building, in priority order:
      1. Explicit `parent_building` field.
      2. A `building:X` tag in the entry's `tags` list (the labeler's
         Parent Building tag group produces this form).
    Returns None when neither is present."""
    p = entry.get("parent_building")
    if p:
        return p
    for t in entry.get("tags") or []:
        if isinstance(t, str) and t.startswith("building:"):
            return t[len("building:"):]
    return None


def _missing_entries(
    by_session: dict[str, list[dict]],
    context: int,
) -> list[tuple[dict, list[dict]]]:
    """Returns (sub_menu_entry, preceding_entries) for each sub_menu
    that lacks parent_building (either as a field OR as a building:X
    tag).  Preceding entries are within the same session, immediately
    before in file-name order, up to *context* items."""
    out: list[tuple[dict, list[dict]]] = []
    for sid in sorted(by_session):
        entries = by_session[sid]
        for i, e in enumerate(entries):
            if e.get("screen_type") != "sub_menu":
                continue
            if _parent_building_of(e):
                continue
            prev = entries[max(0, i - context):i]
            out.append((e, prev))
    return out


def _format_entry(d: dict, prefix: str = "  ") -> str:
    parts = [f"file={d.get('file', '?')}"]
    st = d.get("screen_type")
    if st:
        parts.append(f"screen_type={st!r}")
    if d.get("notes"):
        parts.append(f"notes={d['notes']!r}")
    if d.get("parent_building"):
        parts.append(f"parent_building={d['parent_building']!r}")
    if d.get("tags"):
        parts.append(f"tags={d['tags']}")
    return prefix + "  ".join(parts)


def _human_output(missing: list[tuple[dict, list[dict]]]) -> None:
    if not missing:
        print("All sub_menu entries already have parent_building.  ")
        print("Run `pytest tests/test_label_schema.py` to confirm.")
        return

    print(f"Sub-menu entries needing parent_building back-labelling: "
          f"{len(missing)}")
    print()

    by_session_count: dict[str, int] = defaultdict(int)
    for e, _ in missing:
        by_session_count[e["session_id"]] += 1
    print("By session:")
    for sid in sorted(by_session_count):
        print(f"  {sid}: {by_session_count[sid]}")
    print()
    print("=" * 78)
    print()

    for i, (e, prev) in enumerate(missing, 1):
        print(f"[{i}/{len(missing)}] {e['session_id']}/{e['file']}")
        if e.get("notes"):
            print(f"    notes: {e['notes']!r}")
        if e.get("tags"):
            print(f"    tags:  {e['tags']}")
        if not prev:
            print("    (no preceding frames in this session)")
        else:
            print("    Preceding frames (most recent first):")
            for p in reversed(prev):
                print(_format_entry(p, prefix="      ← "))
        print()

    print()
    print(f"Total: {len(missing)} entries to back-label.")
    print("After back-labelling, run: pytest tests/test_label_schema.py")


def _json_output(missing: list[tuple[dict, list[dict]]]) -> None:
    payload = [
        {
            "session_id":       e["session_id"],
            "file":             e["file"],
            "notes":            e.get("notes"),
            "tags":             e.get("tags"),
            "preceding_frames": [
                {
                    "file":              p["file"],
                    "screen_type":       p.get("screen_type"),
                    "notes":             p.get("notes"),
                    "tags":              p.get("tags"),
                    "parent_building":   p.get("parent_building"),
                }
                for p in prev
            ],
        }
        for e, prev in missing
    ]
    print(json.dumps(payload, indent=2))


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    context = 3
    for i, a in enumerate(argv):
        if a == "--context" and i + 1 < len(argv):
            try:
                context = max(1, int(argv[i + 1]))
            except ValueError:
                pass

    if not _LABELS.exists():
        print(f"No labels file at {_LABELS}", file=sys.stderr)
        return 1

    by_session = _load_grouped_by_session()
    missing = _missing_entries(by_session, context=context)

    if as_json:
        _json_output(missing)
    else:
        _human_output(missing)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
