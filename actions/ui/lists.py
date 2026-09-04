# actions/ui/lists.py
#
# ONE PLACE THAT KNOWS HOW TO PAGE A SCROLLABLE LIST (user, 2026-09-03).
#
# The game has several: the port overworld's building list, the world map's port and village
# lists, and whatever `explore_port` sweeps. Three separate implementations of "swipe, re-read,
# stop when it stops moving" had grown, and — the usual shape — the live path was the one
# WITHOUT it:
#
#   * `sail_actions._scroll_list_for` + `_list_signature` — complete, used by the BUILDING
#     list. Rewinds to the top, pages down, stops on "signature unchanged".
#   * `explore_actions._signature` — a private second copy, same idea, for collecting.
#   * `world_map._on_list` — NONE. It swiped and returned the fixed string "scrolled the
#     list", so it could not tell a list that was paging from a list that was not moving.
#     Live 2026-09-03 it swiped five times at Faro and the stall guard stopped the mission for
#     reporting the same thing three ticks running. A fossil comment in `sail_actions`
#     ("falsely hitting 'end of list' after 1-2 scrolls") records that the port path once had
#     the detection and lost it.
#
# WHAT DIFFERS BETWEEN THE THREE IS WHAT THEY DO WITH EACH PAGE, NOT HOW TO PAGE. So the
# shared thing is a PAGER: it yields pages and knows when the list has stopped moving. Finding
# and collecting are thin callers on top.
#
# EVERY LIST IN THE GAME IS THIS LIST (user, 2026-09-03). The village list, and the fleet and
# task lists under the other tabs, are not wired up yet and need nothing new here: adding one
# means supplying two functions and no scrolling logic at all —
#
#     read_rows(frame) -> [(label, x, y), ...]     how to read THIS panel's rows
#     match(rows, target) -> (x, y) | None         what counts as a hit in it
#
# Everything else — which column to swipe in, when the list has stopped moving, rewinding
# before descending, the bounds — is the same for all of them and lives here once. Nothing in
# this module knows what kind of list it is paging; `fallback_x`, `y` and `reach_px` are the
# only geometry, and all three are per-call.
#
# Two lessons are baked in because both were paid for:
#
#   * SWIPE IN THE LIST'S OWN COLUMN. A region-centre swipe can miss the list entirely; the
#     rewind then never scrolls, the signature is unchanged, "at the top" is assumed, and a
#     clipped top entry never comes back (live 2026-08-19, Jakarta). The column is derived
#     from the rows themselves — the median entry x — not from a constant.
#   * REWIND BEFORE PAGING DOWN. A list left part-scrolled hides entries ABOVE the viewport,
#     and paging down would never find them.
from __future__ import annotations

from typing import Callable, Iterator, Optional, Sequence

from loguru import logger

# A page that repeats is the end of the list. Bounded anyway: a list that never settles is a
# fact to report, not something to grind at.
DEFAULT_MAX_PAGES = 6
DEFAULT_REWIND_PAGES = 4
# Half the swipe's travel — about one page. A DEFAULT, not a constant: the building list and
# the world map's rail are tall, but a fleet or task panel need not be, and a swipe longer than
# the panel overshoots whole pages while one shorter than a row never moves it. Per-call.
DEFAULT_REACH_PX = 150


def signature(rows: Sequence) -> tuple:
    """A stable scroll-position fingerprint: the labels plus the first row's y, bucketed.

    The bucket is what makes "the list shifted by 20px" read as movement while ignoring the
    couple of pixels a header wobbles by. Labels alone are not enough — a long list can show
    the same names either side of a small nudge — and y alone is not enough, because two
    different pages can happen to start at the same offset.
    """
    labels = tuple(str(r[0]) for r in rows)
    first_y = int(rows[0][2] // 30) if rows and len(rows[0]) > 2 else -1
    return (first_y, labels)


def _column_x(rows: Sequence, fallback: int) -> int:
    """The x of the list's own column — the median entry, never a region centre."""
    if not rows:
        return fallback
    xs = sorted(int(r[1]) for r in rows if len(r) > 1)
    return xs[len(xs) // 2] if xs else fallback


def pages(*, read_rows: Callable, capture: Callable, swipe: Callable,
          fallback_x: int, y: int,
          max_pages: int = DEFAULT_MAX_PAGES,
          rewind_pages: int = DEFAULT_REWIND_PAGES,
          reach_px: int = DEFAULT_REACH_PX,
          label: str = "list") -> Iterator[Sequence]:
    """Yield each page of a scrollable list, stopping when the list stops moving.

    `read_rows(frame)` -> [(label, x, y), ...]. `swipe(x1, y1, x2, y2)` performs one page.

    Rewinds to the top first (up-swipes until the page repeats), then pages down. Yields the
    rows of every page INCLUDING the first, so a caller that only wants what is already on
    screen can take the first item and stop.
    """
    rows = read_rows(capture())
    cx = _column_x(rows, fallback_x)

    # LOOK BEFORE MOVING. The page already on screen is yielded first, before any swipe, so a
    # caller finds what is in front of it without touching the list. Rewinding first would
    # scroll AWAY from a visible target — which is the San Village case: the entry was
    # highlighted in the open list and the bot never tapped it (user, 2026-09-03).
    yield rows

    def _one(direction: int, prev):
        """Swipe one page and return (rows, signature, moved)."""
        swipe(cx, y - reach_px * direction, cx, y + reach_px * direction)
        got = read_rows(capture())
        sig = signature(got)
        return got, sig, sig != prev

    # REWIND. A list left part-scrolled hides entries ABOVE the viewport, and paging down
    # would never reach them (live 2026-08-19, Jakarta). Nothing is yielded on the way up —
    # the pages passed through are yielded on the way back down.
    prev = signature(rows)
    for i in range(1, rewind_pages + 1):
        rows, prev, moved = _one(+1, prev)
        if not moved:
            logger.info(f"  [{label}] signature unchanged — at the top after {i} swipe(s)")
            break

    yield rows                        # the top page itself, which the descent swipes away from

    # PAGE DOWN.
    for i in range(1, max_pages + 1):
        rows, prev, moved = _one(-1, prev)
        if not moved:
            logger.info(f"  [{label}] signature unchanged — reached the bottom after "
                        f"{i} swipe(s)")
            return
        yield rows
    logger.info(f"  [{label}] stopped after {max_pages} pages without reaching the bottom "
                "— reporting rather than grinding")


def find_in_list(target: str, *, match: Callable, read_rows: Callable, capture: Callable,
                 swipe: Callable, fallback_x: int, y: int,
                 max_pages: int = DEFAULT_MAX_PAGES,
                 rewind_pages: int = DEFAULT_REWIND_PAGES,
                 reach_px: int = DEFAULT_REACH_PX,
                 label: str = "list") -> Optional[tuple]:
    """Page the list looking for `target`; the tap coordinate, or None.

    `match(rows, target)` -> (x, y) | None — the caller owns what counts as a match, because
    a building name and a fuzzy-OCR'd port name are not compared the same way.

    None means "paged the whole list and it is not there", which is a REFUSAL the caller must
    handle — not a licence to guess a coordinate.
    """
    for rows in pages(read_rows=read_rows, capture=capture, swipe=swipe,
                      fallback_x=fallback_x, y=y, max_pages=max_pages,
                      rewind_pages=rewind_pages, reach_px=reach_px, label=label):
        hit = match(rows, target)
        if hit:
            return hit
    logger.info(f"  [{label}] {target!r} is not in the list — paged to the end")
    return None
