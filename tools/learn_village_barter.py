"""Read a village's FULL barter list — every good and its materials — into the KB.

    python -m tools.learn_village_barter "Svear Village"
    python -m tools.learn_village_barter "Svear Village" --dry-run   # no KB write

No sailing, no gathering: this opens the world map from wherever the fleet is, finds the
village, and scroll-accumulates its Trade List.

Why a separate tool.  The mission's own check stops as soon as the ONE good it came for is
read, which is right for a mission and wrong for learning: it leaves the rest of the village
unknown.  It also has nowhere to report WHAT it saw per screen, so a bad read (Svear
2026-08-24: 14 scrolls, nothing parsed) gives no evidence to work from.  This prints every
screen's parse and saves the frames.

The list layout (user 2026-08-20):
    BARTER GOOD  = flush-left qty tile, NO location pin      e.g. 'Birch Tree' 648
    MATERIAL     = qty tile indented ~30px, WITH a pin       e.g. 'Iron' 91
Indented rows belong to the most recent flush-left good, and a good's materials can CONTINUE
across a scroll boundary — which is why the scroll must OVERLAP rather than jump.
"""

from __future__ import annotations

import re

import os
import sys
import time

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from memory.logger import setup_logging

setup_logging()

from loguru import logger

# Half a panel per step: ~2 rows of overlap, so no row can fall between two reads.
SCROLL_X, SCROLL_Y = 2080, 880
# A GOOD AND ITS MATERIALS MUST SHARE A SCREEN. The panel shows ~4 rows; a good with three
# materials is four rows, so a coarse step can leave the good's NAME on one screen and its
# materials on the next — and the materials then fall to whatever good was named last.
# Measured at Svear (2026-08-24): with -230, Näverslöjd's name was on screen 1 and its rows
# (Birch Tree 260, Iron 130) on screen 2, so they were recorded against Birch Tree, which is
# ALSO a real material of Näverslöjd — a wrong recipe that looks plausible.
# -150 measured best at Svear: coarse enough that each good's NAME appears on a screen with
# some of its materials, fine enough that no row is skipped. Going finer (-110) is WORSE — it
# produces more screens whose leading good is clipped, and a clipped good's materials get
# attributed to whatever good was named last.
SCROLL_DY = int(os.environ.get("VILLAGE_SCROLL_DY", "-150"))
MAX_SCREENS = 24
# Enough upward swipes to reach the top of any village's list from anywhere in it.
REWIND_SWIPES = 12
# Stop when this many consecutive screens add nothing new — one quiet screen can just be a
# slow render, two in a row means the list is done.
QUIET_SCREENS_TO_STOP = 2
# A screen we could not READ is not the end of the list. Look again before moving on — the
# panel animates, and a parser bug can blank a screen that is perfectly legible.
EMPTY_RELOOKS = 2
# The list said there is more below but the swipe moved nothing: retry before giving up.
MAX_STALLS = 2
# A screen sharing no row with the sequence means the scroll jumped past unseen rows. Back up
# by more than a screen's worth of overshoot and look again.
MAX_GAP_RECOVERIES = 3
BACKUP_SCROLL = 260
FRAME_DIR = "/tmp/village_learn"


# ── Only read COMPLETE list items ────────────────────────────────────────────
#
# A list row is a card: a thumbnail tile on the left, name and category to its right.
# Measured on the Svear trade list (2026-08-24):
#     GOOD      tile x1=1694, h=131   flush left, TALLER
#     MATERIAL  tile x1=1723, h=105   indented ~29px, shorter
#
# The rule (user, 2026-08-24): OCR a row ONLY when the whole item is in view. A row clipped
# by the viewport reads partially — Näverslöjd's row spans 869-977 against a list that ends
# at 966, so its name came back sometimes and not others, and when it did not, its materials
# (Birch Tree 260, Iron 130) were attributed to whatever good was named last.
#
# So: parse only rows fully inside the viewport, then scroll by exactly enough to bring the
# NEXT row to the top of it. Aligning the scroll to the list beats any fixed step — no row is
# ever half-read, and none is skipped.
_TILE_X_MIN, _TILE_X_MAX = 1650, 1800
_TILE_MIN_H, _TILE_MAX_H = 80, 170
_TILE_MAX_W = 200
# Materials are indented ~29px from the goods' left edge; anything within this is a GOOD.
_GOOD_INDENT_TOL = 15
_VIEWPORT_TOP_WORDS = ("trade list", "closeout")
_VIEWPORT_BOTTOM_WORDS = ("view by min", "exchange unit")

# Panel chrome and category chips — never row NAMES, so their absence from a
# parse is not a loss worth reporting.
try:
    from actions.village_check import _CATEGORY_WORDS as _CATEGORY_WORDS_SAFE
except Exception:
    _CATEGORY_WORDS_SAFE = frozenset()


def _label(e) -> str:
    return (getattr(e, "label", "") or "").strip().lower()


def _list_viewport(els):
    """(top, bottom) of the scrollable list area, from the chrome that bounds it."""
    tops = [e.y2 for e in els if any(w in _label(e) for w in _VIEWPORT_TOP_WORDS)]
    bots = [e.y1 for e in els if any(w in _label(e) for w in _VIEWPORT_BOTTOM_WORDS)]
    if not tops or not bots:
        return None
    return max(tops), min(bots)


# DO NOT TRY TO LAND A PRECISE STEP — the gesture will not honour it. Measured on Cheyenne
# 2026-08-25, the same request delivers wildly different distances and a longer duration does
# not reliably tame the fling:
#
#     asked 150 @300ms -> 384px (256%)      asked 300 @300ms -> 180px (60%)
#     asked 150 @800ms -> 199px (133%)      asked 300 @800ms -> 429px (143%)
#     asked 450 @300ms -> 333px (74%)       asked 450 @1200ms -> 95px (21%)
#
# What IS reliable: no delivery in any run came close to a full viewport (~585px), and small
# requests stayed furthest from it. Capping the request therefore buys the property that
# actually matters — every screen OVERLAPS the last, so a good and its materials appear
# together at least once and no row is scrolled past unseen. The cost is more screens, which
# is free: the sweep stops on the scrollbar, not on a screen count.
MAX_SCROLL_REQUEST = 150


def _safe_scroll(viewport, dy: int, why: str):
    """Scroll the trade list with BOTH ENDPOINTS INSIDE IT.

    The list ends at y~966 and the "View by Min. Exchange Unit" checkbox sits directly below
    it at y 967-1034. A rewind swipe from the fixed y=880 with dy=+150 releases at y~1030 —
    on the checkbox — and live on 2026-08-25 a sweep toggled it. That switch rewrites every
    quantity as a MINIMUM EXCHANGE UNIT: 187 Chicle becomes 1, 1057 Goldenseal becomes 3. The
    parser's OCR discards text under two characters, so the whole quantity column vanished
    and the Trade List read as unparseable — a view toggle, mistaken for a broken panel.

    Clamping to the measured viewport keeps the finger on the list and off the controls.
    """
    from actions import ui
    if not viewport:
        ui.scroll(SCROLL_X, SCROLL_Y, dy, why=why)
        return
    dy = max(-MAX_SCROLL_REQUEST, min(MAX_SCROLL_REQUEST, dy))
    top, bottom = viewport[0] + 25, viewport[1] - 25
    start = bottom if dy < 0 else top
    end = min(max(start + dy, top), bottom)
    if start == end:
        logger.debug(f"[learn] no room inside the list for a {dy}px scroll — skipping")
        return
    ui.scroll(SCROLL_X, start, end - start, why=why)


def _row_tiles(els):
    """The row thumbnail tiles, top→bottom — one per list item, good or material."""
    out = [e for e in els
           if _TILE_X_MIN < e.x1 < _TILE_X_MAX
           and _TILE_MIN_H <= (e.y2 - e.y1) <= _TILE_MAX_H
           and (e.x2 - e.x1) <= _TILE_MAX_W]
    return sorted(out, key=lambda e: e.y1)


def _is_good_tile(tile, tiles) -> bool:
    """True when this row is a GOOD, not a material.

    Goods are flush left and TALLER; materials are indented by a fixed margin and shorter
    (measured at Svear: good x1=1694 h=131, material x1=1723 h=105). The indent is what
    separates them — height varies more with artwork.
    """
    if not tiles:
        return False
    left = min(t.x1 for t in tiles)
    return (tile.x1 - left) <= _GOOD_INDENT_TOL


def _complete_only(els, viewport):
    """Elements belonging to rows FULLY inside the viewport, plus the first clipped row."""
    if viewport is None:
        return els, None
    top, bottom = viewport
    tiles = _row_tiles(els)
    clipped_below = next((t for t in tiles if t.y2 > bottom), None)

    # EXCLUDE WHAT IS CLIPPED — do not whitelist by detected tile.
    # Keying on tiles looked equivalent and was not: OmniParser does not always return a
    # tile for every row, and a row without one was thrown away even though its name and
    # quantity were perfectly readable. On Cheyenne's FIRST view that silently dropped
    # Corn 162 from Goldenseal — parse_trade_list on the raw elements returned the full
    # recipe (Chicle 187, Corn 162, Gold Dust 187) while this filter cut it to two.
    # The rule is about VISIBILITY: keep every element that lies wholly inside the list
    # viewport, and drop the rows straddling its edges.
    keep = [e for e in els
            if not (e.x1 > 1650)                       # panel chrome & the map behind: leave as-is
            or (e.y1 >= top and e.y2 <= bottom)]
    return keep, clipped_below


def _recover_tile_qty(frame, tile):
    """The quantity badge of a row whose tile OmniParser left unlabelled, or None.

    A row is invisible to the parser without its quantity — the pairing is anchored on it —
    so ONE unread badge silently drops a real material. At Svear the Iron row came back as a
    bare 'icon' and Birch Tree was recorded as needing only Matchlock Gun and Candle
    (2026-08-24). The row itself was fully in view; only the badge failed.
    The badge sits in the bottom strip of the tile: cropping just that reads it at confidence
    1.0 where OCR of the whole tile returns nothing.
    """
    import re
    import numpy as np
    # TRY SEVERAL STRIPS. The badge's height within the tile varies with the artwork, and a
    # single crop is a coin flip: measured on two frames of the SAME row, 0.60 read it on one
    # and returned nothing on the other, where 0.75 read it at confidence 0.92.
    try:
        from actions.water_tap import _get_reader
        reader = _get_reader()
    except Exception as exc:
        logger.debug(f"[learn] badge recovery unavailable: {exc}")
        return None
    h = tile.y2 - tile.y1
    for frac in (0.75, 0.65, 0.55, 0.45):
        box = (tile.x1, tile.y1 + int(h * frac), tile.x2, tile.y2)
        try:
            raw = reader.readtext(np.asarray(frame.crop(box)), detail=1)
        except Exception:
            continue
        for _b, text, conf in raw:
            digits = re.sub(r"[^\d]", "", (text or ""))
            if conf >= 0.5 and digits.isdigit():
                return int(digits)
    return None


def _with_recovered_badges(frame, els, viewport):
    """`els` plus a synthetic numeric element for every complete row missing its badge."""
    import types
    if viewport is None:
        return els
    top, bottom = viewport
    out = list(els)
    for t in _row_tiles(els):
        if t.y1 < top or t.y2 > bottom:
            continue                                   # not fully in view — leave it
        if re.fullmatch(r"[\d,]+", _label(t) or ""):
            continue                                   # badge already read
        n = _recover_tile_qty(frame, t)
        if n is None:
            continue
        logger.info(f"[learn] recovered a missing badge: {n} at y={t.y1}-{t.y2}")
        out.append(types.SimpleNamespace(
            label=str(n), element_type="text",
            x1=t.x1, y1=t.y1, x2=t.x2, y2=t.y2,
            cx=(t.x1 + t.x2) // 2, cy=(t.y1 + t.y2) // 2))
    return out


def _panel_shows_trade_list(elements) -> bool:
    """The Trade List is up when the panel names it — association, not position."""
    labs = [(getattr(e, "label", "") or "").strip().lower() for e in elements]
    return any("trade list" in l for l in labs)


def learn(village: str, dry_run: bool = False) -> int:
    from capture.adb_capture import capture_screen
    from vision.omniparser import parse_fast_cached
    from actions import ui
    from actions.sail_actions import open_world_map, _try_village_search, _find_button
    from actions.village_check import (parse_trade_list, merge_trade_screens,
                                       screen_signature, _PANEL_X_MIN)

    os.makedirs(FRAME_DIR, exist_ok=True)

    # THE GAME REMEMBERS WHERE EACH LIST WAS LEFT, and reopening the village panel does not
    # clear it — only leaving the world map and coming back does (user, 2026-08-25). Without
    # this, Cheyenne's list opened on its FINAL row and every downward scroll was a no-op.
    from tools.village_scroll_report import _reset_list_position
    if not open_world_map():
        logger.error("[learn] could not open the world map")
        return 1
    from actions.sail_actions import _is_on_world_map
    _reset_list_position(capture_screen, ui, open_world_map, _is_on_world_map)
    if not _try_village_search(village):
        logger.error(f"[learn] could not find {village!r}")
        return 1
    ui.settle("screen", why="village selected → Village Info panel")

    frame = capture_screen()
    if _find_button(frame, "barter", x_min=_PANEL_X_MIN) is None:
        logger.error("[learn] Village Info panel did not open (no Barter tab)")
        return 1
    if not ui.tap_text(frame, "barter", x_min=_PANEL_X_MIN, dwell="dialog",
                       why="Village Info → Barter tab"):
        logger.error("[learn] could not tap the Barter tab")
        return 1

    # VERIFY THE LIST IS ACTUALLY UP BEFORE SCROLLING. Scrolling a panel that has not
    # rendered yet reads nothing and, worse, scrolls something else (Svear 2026-08-24:
    # 14 scrolls, every screen empty, no diagnosis).
    for attempt in range(4):
        frame = capture_screen()
        from actions.village_check import trade_list_elements
        els = list(trade_list_elements(frame))
        if _panel_shows_trade_list(els) and parse_trade_list(els):
            break
        logger.info(f"[learn] Trade List not readable yet — waiting ({attempt + 2}/4)")
        time.sleep(1.5)
    else:
        logger.error("[learn] the Barter tab never showed a readable Trade List")
        return 1

    passes = int(os.environ.get("VILLAGE_PASSES", "2"))
    by_good = {}
    for attempt in range(passes):
        logger.info(f"[learn] --- pass {attempt + 1}/{passes} ---")
        got = _one_pass(village, ui, capture_screen, parse_fast_cached,
                        parse_trade_list, merge_trade_screens, screen_signature)
        for t in got:
            prev = by_good.get(t.good)
            # UNION ACROSS PASSES. The read is non-deterministic — a badge that fails to OCR
            # on one pass reads fine on the next, and a good whose name row is clipped in one
            # pass is named in another (Näverslöjd, 2026-08-24). Passes only ADD: a material
            # seen once is real, and nothing here can invent one, because unnamed groups are
            # dropped before we get this far.
            if prev is None:
                by_good[t.good] = t
            else:
                merged_mats = dict(prev.materials)
                merged_mats.update(t.materials)
                prev.materials = merged_mats
                if t.obtain and not prev.obtain:
                    prev.obtain = t.obtain
    merged = sorted(by_good.values(), key=lambda t: t.good)
    _report_and_save(village, merged, dry_run)
    return 0 if merged else 1


def _one_pass(village, ui, capture_screen, parse_fast_cached,
              parse_trade_list, merge_trade_screens, screen_signature):
    """One rewind-and-scroll sweep of the Trade List. Returns the named goods it read."""
    import time
    from loguru import logger
    # ASK THE SCROLLBAR, NOT THE TILES. "Am I at the top?" used to be answered by finding a
    # GOOD tile near the viewport's top edge, but row thumbnails are detected at conf
    # 0.25-0.56 against a 0.30 cutoff, so roughly a third of them are missing on any given
    # frame and the test failed on lists that were already at the top. The thumb's position
    # in its track is unambiguous and always drawn.
    from tools.village_scroll_report import _scrollbar, bar_position
    for _ in range(REWIND_SWIPES):
        frame0 = capture_screen()
        from actions.village_check import trade_list_elements as _tl_els
        vp0 = _list_viewport(list(_tl_els(frame0)))
        at_top, _at_end = bar_position(_scrollbar(frame0, vp0), vp0)
        if at_top:
            logger.info("[learn] the trade list is at the top")
            break
        _safe_scroll(vp0, -SCROLL_DY, "rewind the trade list to the top")
        time.sleep(0.6)

    from vision.list_position import scrollbar_thumb, bar_position, content_shift
    screens, seen_sigs = [], set()
    rows, gaps, gap_recoveries, steps, unread = [], 0, 0, [], 0
    prev_frame, expect_movement, empty_looks, stalls = None, False, 0, 0
    for i in range(MAX_SCREENS):
        frame = capture_screen()
        frame.save(f"{FRAME_DIR}/{village.replace(' ', '_')}_{i:02d}.png")
        from actions.village_check import trade_list_elements
        els = list(trade_list_elements(frame))

        # COMPLETE ITEMS ONLY, then scroll the next one into full view.
        viewport = _list_viewport(els)
        els = _with_recovered_badges(frame, els, viewport)
        whole, clipped = _complete_only(els, viewport)
        if viewport and clipped is not None:
            logger.info(f"[learn] screen {i + 1}: a row is clipped at the fold "
                        f"(y={clipped.y1}-{clipped.y2}, list ends {viewport[1]}) — "
                        "it will be read after the next scroll")
        from actions.village_check import parse_trade_rows, rows_to_trades
        from actions.trade_list_stitch import align, alignable, dedupe_adjacent
        screen_rows = parse_trade_rows(whole if viewport else els)
        trades = rows_to_trades(screen_rows)
        names = [f"{t.good}({t.obtain})←{sorted(t.materials)}" for t in trades]
        logger.info(f"[learn] screen {i + 1}: {len(trades)} good(s) — {names}")

        # WHERE THE LIST IS, AND WHETHER IT MOVED — both read off the screen, neither
        # routed through the parse. The sweep used to end when two screens produced no new
        # PARSE output, which cannot tell "there is nothing more to read" from "I could not
        # read this screen": on 2026-08-25 a parser bug blanked two Cheyenne screens and the
        # sweep would have declared the end of the list with American Bison and Eagle
        # Feather still below the fold.
        at_top, at_end = bar_position(scrollbar_thumb(frame, viewport), viewport)
        moved = None
        if prev_frame is not None and expect_movement:
            dy, err, zero = content_shift(prev_frame, frame)
            moved = bool(dy != 0 and err < 0.6 * zero)
            logger.info(f"[learn] screen {i + 1}: the list moved {abs(dy)}px"
                        if moved else
                        f"[learn] screen {i + 1}: the list did NOT move"
                        + ("  (it is against its bottom stop)" if at_end else ""))
        prev_frame = frame

        # DO NOT CARRY A GOOD ACROSS THE FOLD. Tried twice now, wrong both times. The
        # guards (previous screen read, list provably moved, group at the TOP) establish that
        # the screens are adjacent — but adjacency does not make the previous screen's parse
        # RIGHT. Live 2026-08-25 pass 2 read Moccasin as a material, the carry attached that
        # group to American Bison, and the next screen's group went the same way: American
        # Bison was saved with SIX inputs including itself and Moccasin. A good that is also
        # a material (user: they exist, and carry a location pin whose popup names a VILLAGE
        # rather than a port) makes such a misparse entirely plausible, so the carry amplifies
        # a mistake into a confident wrong recipe. The fix belongs in the SCROLL: step in
        # overlapping screens so a good and its materials are visible together at least once.
        sig = screen_signature(trades)

        if not screen_rows:
            if empty_looks < EMPTY_RELOOKS:
                empty_looks += 1
                logger.warning(f"[learn] screen {i + 1} parsed EMPTY — looking again "
                               f"({empty_looks}/{EMPTY_RELOOKS}) rather than calling it the "
                               "end of the list")
                expect_movement = False       # nothing was scrolled, so expect no shift
                time.sleep(0.8)
                continue
            logger.warning(f"[learn] screen {i + 1} still unreadable after "
                           f"{EMPTY_RELOOKS} looks — scrolling past it")
            empty_looks = 0
        else:
            empty_looks = 0
            # STITCH THE SCREEN ONTO THE SEQUENCE. Recipes are no longer inferred per
            # screen and merged afterwards — a good and its materials routinely straddle the
            # fold, so no single screen holds the whole recipe. The rows are reassembled
            # first and the recipes read once from the finished sequence.
            sc = dedupe_adjacent(alignable(screen_rows))
            unread += sum(1 for r in screen_rows if not (r.name or "").strip())
            if not rows:
                rows = list(sc)
                steps.append({"screen": i + 1, "overlap": 0, "appended": len(sc),
                              "rows": [r.key for r in sc], "note": "first screen"})
            else:
                p = align(rows, sc)
                if p is None:
                    # NO PLACEMENT: the scroll jumped past rows nobody saw. Bridging would
                    # fabricate an order, so back up and look again — safe now that `align`
                    # recognises a backward re-read, which an earlier suffix-only match did
                    # not, walking the list to its top instead.
                    if gap_recoveries < MAX_GAP_RECOVERIES:
                        gap_recoveries += 1
                        logger.warning(f"[learn] screen {i + 1} does not fit the sequence — "
                                       f"backing up and re-reading "
                                       f"({gap_recoveries}/{MAX_GAP_RECOVERIES})")
                        _safe_scroll(viewport, BACKUP_SCROLL, "back up over a skipped stretch")
                        expect_movement = True
                        continue
                    logger.error(f"[learn] screen {i + 1} still does not fit after "
                                 f"{MAX_GAP_RECOVERIES} attempts — the sequence has a GAP")
                    gaps += 1
                    steps.append({"screen": i + 1, "overlap": 0, "appended": len(sc),
                                  "rows": [r.key for r in sc],
                                  "note": "GAP — no placement after retries; rows missing"})
                    rows.extend(sc)
                else:
                    gap_recoveries = 0
                    end = p + len(sc)
                    added = sc[len(rows) - p:] if end > len(rows) else []
                    kind = "forward" if end > len(rows) else "backward re-read"
                    if added:
                        logger.info(f"[learn] screen {i + 1}: aligns at row {p} ({kind}), "
                                    f"{len(added)} new — "
                                    + ", ".join(f"{r.name}({r.qty})" for r in added))
                    else:
                        logger.info(f"[learn] screen {i + 1}: aligns at row {p} ({kind}), "
                                    "nothing new")
                    steps.append({"screen": i + 1, "overlap": len(sc) - len(added),
                                  "appended": len(added), "rows": [r.key for r in sc],
                                  "added_rows": [r.key for r in added], "note": kind})
                    rows.extend(added)

        if moved is False:
            if at_end:
                logger.info(f"[learn] end of the list after {i + 1} screen(s) — the thumb is "
                            "against the bottom stop and the last scroll moved nothing")
                break
            stalls += 1
            if stalls >= MAX_STALLS:
                logger.warning(f"[learn] the list will not scroll and the scrollbar says it "
                               f"is not at the end — giving up after {stalls} stalled "
                               "attempt(s)")
                break
            logger.warning(f"[learn] the scroll moved nothing but there is more below — "
                           f"retrying ({stalls}/{MAX_STALLS})")
        else:
            stalls = 0

        # ALIGN THE SCROLL TO THE LIST. Bring the first clipped row to the top of the
        # viewport, so the next screen starts on a row boundary and that row is whole.
        # LAND THE NEXT SCREEN ON A GOOD, not on a loose material. Scrolling the first
        # CLIPPED row to the top puts a MATERIAL there whenever the clipped row is one — and
        # its good scrolls away above it, so the group arrives unnamed and gets dropped.
        # Cheyenne 2026-08-24: Moccasin was named at the bottom of one screen with its
        # materials still below the fold, and by the next screen they were orphaned.
        # Scrolling the LAST COMPLETE GOOD to the top re-reads that good WITH its materials,
        # at the cost of one repeated row.
        dy = SCROLL_DY
        if viewport:
            tiles_now = _row_tiles(els)
            whole_tiles = [t for t in tiles_now
                           if t.y1 >= viewport[0] and t.y2 <= viewport[1]]
            goods_now = [t for t in whole_tiles if _is_good_tile(t, tiles_now)]
            anchor_row = goods_now[-1] if goods_now else (whole_tiles[-1] if whole_tiles else None)
            if anchor_row is not None and anchor_row.y1 > viewport[0] + 20:
                dy = max(min(-(anchor_row.y1 - viewport[0]), -40), -700)
                logger.info(f"[learn] scrolling {dy}px to put the last complete good at the top")
            elif clipped is not None:
                dy = max(min(-(clipped.y1 - viewport[0]), -40), -700)
                logger.info(f"[learn] scrolling {dy}px to bring the clipped row into view")
        _safe_scroll(viewport, dy, f"trade list, screen {i + 1}")
        expect_movement = True      # the next frame SHOULD differ; if it does not, that is
                                    # the list against a stop, not a screen we mis-read

    from actions.village_check import rows_to_trades
    from actions.trade_list_stitch import _write_trace
    _write_trace(village, rows, steps + [{"summary": True, "screens": len(steps),
                                          "gaps": gaps, "unreadable_rows": unread}])
    if gaps:
        logger.warning(f"[learn] the row sequence has {gaps} gap(s) — this read is INCOMPLETE")
    if unread:
        logger.warning(f"[learn] {unread} row(s) were never readable on any screen — the "
                       "read may be missing a good or a material")
    # NEVER RETURN AN UNNAMED GROUP. `rows_to_trades` opens one for materials that appear
    # before any named good — real rows whose good is not in the sequence. Saving it wrote a
    # recipe under an EMPTY name into the knowledge base (live 2026-08-25).
    out = []
    for t in rows_to_trades(rows):
        if (t.good or "").strip():
            out.append(t)
        else:
            logger.warning(f"[learn] dropping an unnamed group {sorted(t.materials)} — "
                           "its good never appeared in the stitched sequence")
    return out


def _report_and_save(village, merged, dry_run):
    from loguru import logger
    if not merged:
        logger.error("[learn] nothing parsed — frames saved in " + FRAME_DIR)
        return

    print(f"\n=== {village}: {len(merged)} barter good(s) ===")
    for t in merged:
        mats = ", ".join(f"{m} x{q}" for m, q in sorted(t.materials.items()))
        print(f"  {t.good:<22} obtain {t.obtain:<6} ← {mats or '(no materials read)'}")

    if dry_run:
        print("\n(--dry-run: KB not written)")
        return

    # Written the same way the mission's own check writes them, so the two agree: MERGE
    # rather than replace (a material list is invariant — a read that misses one is a
    # partial READ, not a recipe change), and keep a per-village input list.
    from memory.barter_kb import (BarterRecipe, RecipeInput, Village, load_recipe,
                                  save_recipe, save_village, _slug)
    written = 0
    for t in merged:
        if not t.materials:
            logger.warning(f"[learn] skipping {t.good!r} — no materials read")
            continue
        recipe = load_recipe(t.good) or BarterRecipe(good=t.good)
        prior = {i.material.strip().lower(): i for i in recipe.inputs}
        seen, inputs = set(), []
        for material, need in t.materials.items():
            old = prior.get(material.strip().lower())
            inputs.append(RecipeInput(material=material, ratio=int(need),
                                      source_ports=list(old.source_ports if old else [])))
            seen.add(material.strip().lower())
        for key, old in prior.items():
            if key not in seen:
                logger.warning(f"[learn] {t.good}: keeping known material {old.material!r} "
                               "that this read did not return")
                inputs.append(old)
        recipe.inputs = inputs
        recipe.village_inputs = dict(recipe.village_inputs or {})
        recipe.village_inputs[_slug(village)] = [
            RecipeInput(material=m, ratio=int(q),
                        source_ports=list(prior[m.strip().lower()].source_ports
                                          if m.strip().lower() in prior else []))
            for m, q in t.materials.items()]
        if village not in recipe.villages:
            recipe.villages.append(village)
        save_recipe(recipe)
        written += 1
    save_village(Village(name=village))
    print(f"\nKB updated: {written} recipe(s) for {village}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print('Usage: python -m tools.learn_village_barter "<Village Name>" [--dry-run]')
        raise SystemExit(1)
    raise SystemExit(learn(args[0], dry_run="--dry-run" in sys.argv))
