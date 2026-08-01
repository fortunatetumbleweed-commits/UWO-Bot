"""Schema validation for data/labels.jsonl.

Origin: 2026-05-13 — extending the label schema with `parent_building`
on `sub_menu` entries so the layout-generation tooling can determine
which building's sub-menu a labelled frame belongs to.

Behaviour:

  * `LabelsFileTests` — basic schema sanity: file exists, entries
    parse, required top-level fields are present.
  * `SubMenuParentBuildingTests` — enforces that every
    `screen_type=sub_menu` entry has `parent_building`.

This last test **WILL FAIL** until the user finishes back-labelling
existing sub_menu entries with parent_building.  The failure message
includes a per-session count and the first 10 entries needing
attention.  Run `python tools/find_submenu_parents.py` for the
complete worklist with preceding-frame context.

Once back-labelling is done, this test pinning the rule prevents
future regressions — new sub_menu labels without parent_building
will fail CI before the bad data lands.
"""

from __future__ import annotations

import json
import unittest
from collections import Counter
from pathlib import Path

_LABELS = Path("data/labels.jsonl")
_REQUIRED_TOP_LEVEL_FIELDS = ("session_id", "file", "screen_type", "labeled_at")


def _load_labels() -> list[dict]:
    """Load labels.jsonl as the **currently-effective** label per
    (session_id, file) — the LAST entry wins.  Matches what
    `supervisor.labeler._load_labels` does internally, so the
    validator and the labeler agree on what counts as "the label".

    Why the dedup: the labeler is append-only.  Re-labelling a frame
    writes a NEW record without removing the old one — history is
    preserved.  Readers (including this validator) take the latest
    entry per frame.  Without this dedup, the test would penalise
    the user for old entries that have already been corrected.

    Returns a list of records, each with `_line` (1-based file line
    number) attached for diagnostic messages.
    """
    by_key: dict[tuple, dict] = {}
    if not _LABELS.exists():
        return []
    for i, line in enumerate(_LABELS.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError as e:
            raise AssertionError(f"data/labels.jsonl line {i} not valid JSON: {e}")
        d["_line"] = i
        key = (d.get("session_id"), d.get("file"))
        by_key[key] = d   # last write wins (matches labeler's read behaviour)
    return list(by_key.values())


def _parent_building_of(entry: dict) -> str | None:
    """Return the parent-building slug for a label entry.

    Two valid sources, in priority order:
      1. Explicit `parent_building` field.
      2. A `building:X` tag in the entry's `tags` list — same vocabulary
         used by `building_interior` entries.  Picking from the labeler's
         Parent Building tag group (see screen_tags.json) produces tags
         in this form.

    Returns None when neither is present."""
    p = entry.get("parent_building")
    if p:
        return p
    tags = entry.get("tags") or []
    for t in tags:
        if isinstance(t, str) and t.startswith("building:"):
            return t[len("building:"):]
    return None


class LabelsFileTests(unittest.TestCase):

    def test_file_exists(self):
        self.assertTrue(_LABELS.exists(), f"{_LABELS} should exist")

    def test_at_least_one_entry(self):
        self.assertGreater(len(_load_labels()), 0,
                           "expected at least one label entry")

    def test_every_entry_has_required_top_level_fields(self):
        missing = []
        for d in _load_labels():
            absent = [f for f in _REQUIRED_TOP_LEVEL_FIELDS if f not in d]
            if absent:
                missing.append((d["_line"], d.get("file"), absent))
        if missing:
            preview = "\n".join(
                f"  line {ln}: {f} missing {a}" for ln, f, a in missing[:10]
            )
            extra = (f"\n  ... ({len(missing) - 10} more)"
                     if len(missing) > 10 else "")
            self.fail(
                f"{len(missing)} entries missing required fields:\n"
                f"{preview}{extra}"
            )


class SubMenuParentBuildingTests(unittest.TestCase):
    """Every `sub_menu` entry must include `parent_building`.

    Currently failing while the user back-labels the existing corpus.
    Run `python tools/find_submenu_parents.py` for the worklist.
    """

    def test_every_sub_menu_has_parent_building(self):
        # Accept either an explicit `parent_building` field OR a
        # `building:X` tag (the labeler's Parent Building tag group
        # produces the latter).  See _parent_building_of().
        missing = [
            d for d in _load_labels()
            if d.get("screen_type") == "sub_menu"
            and not _parent_building_of(d)
        ]
        if not missing:
            return    # back-labelling complete

        by_session = Counter(d.get("session_id") for d in missing)
        per_session = "\n".join(
            f"  {sid}: {n}" for sid, n in sorted(by_session.items())
        )
        preview = "\n".join(
            f"  line {d['_line']}: {d.get('session_id')}/{d.get('file')}"
            + (f"  notes={d['notes']!r}" if d.get("notes") else "")
            for d in missing[:10]
        )
        extra = (f"\n  ... ({len(missing) - 10} more entries)"
                 if len(missing) > 10 else "")
        self.fail(
            f"{len(missing)} sub_menu entries missing parent_building.\n\n"
            f"By session:\n{per_session}\n\n"
            f"First 10:\n{preview}{extra}\n\n"
            "Run `python tools/find_submenu_parents.py` for the worklist "
            "with preceding-frame context."
        )


if __name__ == "__main__":
    unittest.main()
