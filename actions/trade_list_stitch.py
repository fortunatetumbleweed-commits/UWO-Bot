"""Assemble overlapping screens of the Trade List into ONE row sequence.

Reading a village means scrolling a list that is taller than the panel, and every earlier
approach grouped each screen into recipes on the spot and merged the recipes afterwards. That
cannot work at the fold: a good and its materials routinely straddle two screens, so neither
screen holds the whole recipe. The attempts to paper over it all failed the same way — the
last one carried a good's NAME onto the next screen's leading materials, and when a screen
misparsed it produced `American Bison <- Horse, Hand Cannon, Bullet, Moccasin, American
Bison, Wool`: six inputs, including itself.

The list itself is not ambiguous; only the windows onto it are. So stitch the windows first,
exactly as a panorama is assembled, and infer recipes once from the finished sequence:

    screen 1  A B C D E
    screen 2      C D E F G        -> A B C D E F G
    screen 3            E F G H

Overlap is found by matching ROWS (name, quantity, good-or-material), never by position — the
same row sits at a different y on every screen. Nothing is ever guessed: if two screens share
no rows the sequence is broken, and that is reported rather than bridged, because a bridge
across a gap is precisely how a confident wrong recipe gets built.

Goods legitimately appear as materials — Moccasin is made from American Bison, and Pulque and
Guarana are goods at their own villages (user, 2026-08-25) — so a name alone never identifies
a row. The row's own pin does.
"""

from __future__ import annotations

import json
import os
from typing import List, Optional

from loguru import logger

TRACE_DIR = "/tmp/village_stitch"


def alignable(rows: List) -> List:
    """A screen's rows with the UNREADABLE ones removed, ready for matching.

    `parse_trade_rows` emits a placeholder for a row whose name could not be read. Those
    placeholders are not content — they are a failure to read — and they break alignment
    outright: Cheyenne screen 8 began with one, so its `Bullet` row could not be matched
    against the `Bullet` already in the sequence and the screen was called a gap, losing
    Moccasin and Eagle Feather from the read.

    Dropping them loses a row; KEEPING them in the sequence would be worse than that, because
    a row nobody could name still separates one recipe from the next only by accident. What
    matters is that dropping never invents anything: an unread row means an incomplete read,
    which the caller is told about, not a wrong one.
    """
    return [r for r in rows if (r.name or "").strip()]


def dedupe_adjacent(rows: List) -> List:
    """Drop a row that repeats the one directly above it.

    OmniParser occasionally returns two boxes for one row, which becomes the same row twice
    in sequence — Cheyenne screen 2 held `American Bison 769` twice. Two genuinely identical
    adjacent rows do not occur in this list: a repeated good would carry its own materials
    between the two.
    """
    out: List = []
    for r in rows:
        if out and out[-1].key == r.key:
            logger.debug(f"[stitch] dropping a duplicate detection of {r.name}({r.qty})")
            continue
        out.append(r)
    return out


def align(acc: List, new: List) -> Optional[int]:
    """Where `new` sits in `acc`: the index at which it starts, or None if it does not fit.

    Two placements matter and suffix-only matching saw just the first:

      * FORWARD — `new` starts inside `acc` and runs past its end; the tail is new content.
      * BACKWARD — `new` lies entirely within `acc`; the view has moved back over rows
        already read, which happens the moment anything scrolls up.

    Missing the backward case is not cosmetic. A gap recovery that backed up and re-read
    produced screens matching the MIDDLE of the sequence; suffix-only matching called each a
    fresh gap, backed up again, and walked the list to its top — arriving there with three
    reported gaps and the read declared INCOMPLETE (live 2026-08-25).
    """
    if not acc or not new:
        return None
    for p in range(len(acc)):                       # forward: longest tail-overlap first
        span = len(acc) - p
        if [r.key for r in acc[p:]] == [r.key for r in new[:span]]:
            return p
    for p in range(len(acc) - len(new), -1, -1):    # backward: fully contained
        if [r.key for r in acc[p:p + len(new)]] == [r.key for r in new]:
            return p
    return None


def _overlap(acc: List, new: List) -> int:
    """How many trailing rows of `acc` are the leading rows of `new`. 0 = no overlap.

    The LONGEST match wins: a village can repeat a row (150 Bullet and 150 Hand Cannon are
    different rows, but two `Wool 300` rows would not be), and the longest common run is the
    one consistent with a list that only ever scrolls one way.
    """
    for k in range(min(len(acc), len(new)), 0, -1):
        if [r.key for r in acc[-k:]] == [r.key for r in new[:k]]:
            return k
    return 0


def stitch_screens(screens: List[List], *, village: str = "", trace: bool = True) -> tuple:
    """(rows, steps) — the stitched row sequence and a record of how it was built.

    `steps` is kept whether or not it is written to disk: every decision this makes is a
    place a wrong recipe could come from, so each one records what the screen held, how much
    of it was already known, and what was added.
    """
    rows: List = []
    steps: List[dict] = []
    for i, screen in enumerate(screens):
        if not screen:
            steps.append({"screen": i + 1, "rows": [], "note": "empty screen — skipped"})
            continue
        if not rows:
            rows = list(screen)
            steps.append({"screen": i + 1, "overlap": 0, "appended": len(screen),
                          "rows": [r.key for r in screen], "note": "first screen"})
            continue

        k = _overlap(rows, screen)
        added = screen[k:]
        note = ""
        if k == 0:
            # NO SHARED ROW. Either the scroll jumped more than a screen or a screen was
            # misread. Appending regardless would silently fabricate an order; the sequence
            # is marked broken instead so the caller can re-read rather than trust it.
            note = ("NO OVERLAP — the sequence is broken here; rows between these screens "
                    "may have been skipped")
            logger.warning(f"[stitch] screen {i + 1} shares no row with what came before — "
                           "not bridging the gap")
        elif not added:
            note = "no new rows — this screen is entirely contained in what came before"
        rows.extend(added)
        steps.append({"screen": i + 1, "overlap": k, "appended": len(added),
                      "rows": [r.key for r in screen],
                      "added_rows": [r.key for r in added], "note": note})

    if trace:
        _write_trace(village, rows, steps)
    return rows, steps


def _write_trace(village: str, rows: List, steps: List[dict]) -> Optional[str]:
    """Save the assembly so a wrong recipe can be traced back to the screen that caused it."""
    try:
        os.makedirs(TRACE_DIR, exist_ok=True)
        slug = (village or "village").lower().replace(" ", "_")
        path = f"{TRACE_DIR}/{slug}.json"
        with open(path, "w") as fh:
            json.dump({"village": village,
                       "steps": steps,
                       "stitched": [{"name": r.name, "qty": r.qty,
                                     "is_material": r.is_material} for r in rows]},
                      fh, indent=2, ensure_ascii=False)
        logger.info(f"[stitch] assembly trace written to {path}")
        return path
    except Exception as exc:                 # a trace is a diagnostic, never a hard failure
        logger.debug(f"[stitch] could not write the trace: {exc}")
        return None
