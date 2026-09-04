# actions/village_check.py
# Remote village barter check — read a village's LIVE barter recipes from PORT, no sailing.
#
# Recipe model (user 2026-08-20): the MATERIALS for a barter good are invariant, but the
# QUANTITIES (need per material, obtain per round) refresh every ~6 hours (the countdown
# timer on the village screen).  So a barter plan must construct the live recipe BEFORE
# setting sail:
#
#   1. Open the world map from port (globe on the minimap — actions.sail_actions.open_world_map)
#      and find the village exactly as at sea (Explore tab; pan_to_village).
#   2. Tap the village → the right-side **Village Info** panel opens.
#   3. **Base tab**: amity grade + points, and 'Daily Barter Progress N/7' — barters
#      used / available today.
#   4. **Barter tab → Trade List**: the village's barter goods and their materials in ONE
#      interleaved list (NO popup on tap).  Layout rules (user):
#        - BARTER GOOD  = flush-left qty tile, NO location icon        (e.g. 'Camas' 953)
#        - MATERIAL     = qty tile indented ~30px, WITH a location-pin icon (e.g. 130 Avocado)
#      Scroll to read everything; a good's materials can CONTINUE across a scroll boundary —
#      indented rows always belong to the most recent flush-left good.
#
# Validated live 2026-08-20 on Apache Village (read entirely from a port):
#   Camas 953 ← 130 Avocado + 150 Cassava
#   Pulque 1,022 ← 150 Coral + 150 Silver
#   Wampum 635 ← 150 Platinum + 150 Coral + 150 Silver
#   Base tab: Amity Friendly 93,958/100,000, Daily Barter Progress 0/7.
#
# This module holds the PARSERS (pure, testable).  The full navigate-and-read driver
# (`read_village_barter_remote(village)`) is the next build step — see
# memory/project_remote_village_barter_check_2026-08-20.md.

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional

from PIL import Image

# Panel geometry — RELATIVE, never absolute.  The game re-bakes a camera-cutout safe-area
# offset per screen at each world-switch, so the whole panel slides horizontally between
# sessions: the columns calibrated 2026-08-20 sat ~110px right of where the SAME panel
# rendered on 2026-08-21 (tiles x1 1780-1900 → 1692-1722, names 1940-2120 → 1829-1834,
# pins 2270-2340 → 2184-2185).  Absolute bands rejected every row and the trade list read
# back empty.  So: find the tile COLUMN structurally, then locate everything else by its
# offset from that column.  The layout is stable even when its position is not.
_COLUMN_TOL = 100           # px spread of the quantity column (badge vs whole-tile bbox)
_ROW_TOL = 50               # px alignment of a row's TOP edges (name vs the category below)
_ROW_SPAN = 130             # px from a row's name down to its quantity badge
_NAME_GAP = 150             # px a name may sit right of its badge (overlays sit far off)
_NAME_OVERLAP = 60          # px a name may start LEFT of the badge's right edge (observed −2)
_INDENT_PX = 15             # x1 offset beyond the flush-left base that marks a material
_PANEL_FRAC = 0.60          # the info panel occupies the right ~40% of the frame


@dataclass
class VillageTrade:
    good: str
    obtain: int                     # units received per exchange round (volatile — 6h refresh)
    materials: dict = field(default_factory=dict)   # {material: need_qty} (qty volatile)


# Category pills sit in the same column as the row NAME ("Wares", "Jewelry", …), so the
# name reader has to know them by vocabulary — OmniParser reports some names as buttons
# and some categories as buttons, so the element TYPE cannot tell them apart.
# ONE CATEGORY VOCABULARY. This list used to be maintained separately from
# `vision.market_reader._CATEGORIES` and was missing entries the other had — "firearms"
# among them. A category that is not recognised becomes a candidate NAME, and at Svear
# Village the Matchlock Gun row was recorded as "Firearms" (its category chip) with the right
# quantity, 53. The mission then planned to gather a material that does not exist, from ports
# that sell firearms. Categories are the same concept in both readers, so they share a list.
_PANEL_CHROME_WORDS = frozenset({
    "trade list", "closeout", "barter", "explore", "base", "village info", "icon",
    "view by min. exchange unit", "market", "current location", "source",
})


def _category_words() -> frozenset:
    try:
        from vision.market_reader import _CATEGORIES
    except Exception:                                   # reader unavailable — degrade, don't die
        _CATEGORIES = frozenset()
    return frozenset(_CATEGORIES) | _PANEL_CHROME_WORDS | frozenset({
        # seen on village trade lists, kept here until the shared list carries them
        "sundries", "sundry goods", "wares", "medicine", "liquor",
    })


_CATEGORY_WORDS = _category_words()


def _cy(e) -> int:
    return (e.y1 + e.y2) // 2


def _labelled(elements) -> list:
    return [e for e in elements or []
            if getattr(e, "element_type", "") in ("text", "button")
            and (getattr(e, "label", "") or "").strip()]


def _pin_icons(elements) -> list:
    """The location pins — the icons in the row's RIGHTMOST column.

    A pin means MATERIAL, so anything mistaken for one turns a barter good into a material.
    Taking every icon did exactly that: each category chip carries a small green icon of its
    own at x~1980-2050, well right of the quantity badge, and `_row_pin` accepts anything
    right of the badge. Live at Cheyenne 2026-08-25 American Bison — a GOOD, and one that is
    also a material at this village — matched the chip icon at x=2021 and was filed as a
    material, losing its recipe.

    The pins line up in a column at the row's right edge, so keeping only the icons in that
    rightmost column separates them from the chip decorations without measuring any box.
    """
    icons = [e for e in elements or []
             if getattr(e, "element_type", "") == "icon"
             and ((e.x1 + e.x2) // 2) > _PANEL_X_MIN]
    if not icons:
        return []
    rightmost = max((e.x1 + e.x2) // 2 for e in icons)
    return [e for e in icons
            if rightmost - ((e.x1 + e.x2) // 2) <= _PIN_COLUMN_TOL]


def _row_pin(row_el, icons):
    """The location pin on a row — an icon to the RIGHT of the row whose centre falls in
    its vertical span. A pin means MATERIAL; a barter good has none."""
    for e in icons:
        cx = (e.x1 + e.x2) // 2
        if cx > row_el.x2 and row_el.y1 - 10 <= _cy(e) <= row_el.y2 + 60:
            return (cx, _cy(e))
    return None


def _qty_elements(elements, frame_w: int = 2400) -> list:
    """The quantity badges, top→bottom — found BEFORE the names, so the two don't define
    each other circularly.

    Accepts TEXT as well as BUTTON. Each row is a card: item icon on the left with the
    quantity as a badge in its bottom-right corner. OmniParser reports that either as the
    whole icon tile (a button) or as just the badge text, and requiring 'button' silently
    dropped whole rows — live 2026-08-21 it lost Coral from Box of Nutmeg, and the mission
    would have gathered two of three materials and found Exchange greyed at the village.

    The column is the largest x-aligned cluster of numerics inside the panel, so it
    follows the panel wherever the cutout offset puts it."""
    cands = [e for e in _labelled(elements)
             if re.fullmatch(r"[\d,]+", (e.label or "").strip())
             and e.x1 > frame_w * _PANEL_FRAC]
    if not cands:
        return []
    best: list = []
    for seed in cands:
        grp = [e for e in cands if abs(e.x1 - seed.x1) <= _COLUMN_TOL]
        if len(grp) > len(best):
            best = grp
    return sorted(best, key=lambda e: e.y1)


def _row_names(elements, qtys) -> list:
    """The row NAMES — the goods and materials themselves.

    Names are the most reliably detected part of a row, which is why they anchor the
    pairing. Scoped to the RIGHT of the quantity column, which excludes the panel's own
    chrome (Village Info, Trade List, the village title) without naming any of it.
    Excluded too: the category pill sharing the column, and the build stamp (has '.')."""
    if not qtys:
        return []
    # THE TYPICAL BADGE, NOT THE WIDEST ONE. OmniParser sometimes merges a quantity badge
    # with the category chip beside it into one very wide box — San Village 2026-08-25 had a
    # `285` badge come back 420px wide instead of ~106. Taking max(x2) let that single box
    # push this boundary to x=2172, which put every NAME on the screen to its left: Prunus
    # Padus, Rhino Horn, Senna and Anise were all discarded and the whole screen parsed as
    # nothing, with every row perfectly visible and perfectly OCR'd. A median ignores the
    # outlier, and the badge column is uniform enough that the median is the true edge.
    left = statistics.median([q.x2 for q in qtys]) - 20
    # REACHES the name area, rather than STARTS in it. OmniParser sometimes returns a whole
    # row card as one box and hangs the row's name on it, so the label's x1 is the row's left
    # edge (~1700) instead of the text's (~1830). Requiring the name to START right of the
    # badges then discarded every name on the screen — live at Cheyenne 2026-08-25, Moccasin,
    # American Bison, Wool and Bullet were all detected at 0.89-1.00 confidence and thrown
    # away, and the screen parsed as empty with the rows plainly visible.
    # The chrome this bound was protecting against — the panel title, the tab strip, the
    # village name — is ABOVE the first badge, so the badge column's own vertical span
    # excludes it without measuring any box's width.
    top = min(q.y1 for q in qtys) - _CHROME_MARGIN
    bottom = max(q.y2 for q in qtys) + _CHROME_MARGIN
    out = []
    for e in _labelled(elements):
        lab = (e.label or "").strip()
        if e.x2 < left or "." in lab or re.fullmatch(r"[\d,]+", lab):
            continue
        if e.y2 < top or e.y1 > bottom:
            continue                     # panel chrome above the list, controls below it
        if lab.lower() in _CATEGORY_WORDS:
            continue
        out.append(e)
    return out


def _name_for_qty(qty, names, col_right: Optional[float] = None):
    """The name belonging to a quantity badge: the nearest name that sits ABOVE the
    badge's centre AND immediately to its right.

    'Above' disambiguates vertically — the badge is in the icon's bottom-right corner, so
    its centre can fall nearer the NEXT row's name than its own. The horizontal bound
    stops a floating overlay claiming the row: observed gaps between a badge and its own
    name are under 20px, while the amity tooltip that once became a material sat 406px
    away (live 2026-08-21)."""
    # A MERGED BADGE MUST NOT OUTRUN ITS OWN NAME. When OmniParser fuses a badge with the
    # chip beside it the box can be 420px wide instead of ~106, and its right edge then sits
    # PAST the name it belongs to — so the row loses its own name and is dropped. Measuring
    # from the column's typical right edge instead keeps a fat box behaving like a normal
    # one; `min` means a narrow badge is unaffected.
    right = qty.x2 if col_right is None else min(qty.x2, col_right)
    centre = _cy(qty)
    # THE NAME MUST REACH THE BADGE, not begin beside it. When OmniParser returns a whole row
    # card as one box the name hangs off that box, whose x1 is the row's left edge — 128px
    # LEFT of the badge, far outside the old overlap allowance, so the row lost its own name
    # and the screen parsed empty (Cheyenne 2026-08-25: Moccasin, American Bison, Wool).
    # Requiring the name's box to EXTEND past the badge keeps the guarantee that mattered —
    # the amity tooltip that once became a material sat 406px clear to the right and still
    # fails `x1 - right <= _NAME_GAP`.
    cands = [n for n in names
             if n.y1 <= centre and centre - n.y1 <= _ROW_SPAN
             and n.x2 >= right and n.x1 - right <= _NAME_GAP]
    return max(cands, key=lambda n: n.y1) if cands else None


@dataclass
class TradeRow:
    """ONE row of the Trade List, before anything is grouped into a recipe.

    Grouping a screen into recipes on the spot is what makes the fold hurt: a good and its
    materials can straddle two screens and neither screen holds the whole recipe. Rows are
    the honest unit — they are what the panel actually shows — and `trade_list_stitch`
    reassembles them across screens before any recipe is inferred.
    """

    name: str
    qty: int
    is_material: bool
    y: int = 0

    @property
    def key(self) -> tuple:
        """Identity for overlap matching. Position deliberately excluded: the same row sits
        at a different y on every screen, which is the whole point of stitching."""
        return (self.name, self.qty, self.is_material)


def parse_trade_rows(elements) -> list[TradeRow]:
    """ONE screen of the Trade List as a flat, top-to-bottom list of rows.

    Adjacent duplicates are dropped here rather than left to the caller: OmniParser returns
    two boxes for one row often enough that a good otherwise appears twice — once empty, once
    with its materials — and the empty one then reads as a good with no recipe.
    """
    tiles = _qty_elements(elements)
    names = _row_names(elements, tiles)
    if not tiles or not names:
        return []
    icons = _pin_icons(elements)

    base = min(e.x1 for e in tiles)
    # Geometry is only meaningful across elements of the same KIND: OmniParser returns a row's
    # quantity either as the whole thumbnail (~105-140px tall) or as just the badge text
    # (~20-30px), and comparing one against the other measures the detector, not the row.
    tall = [e for e in tiles if (e.y2 - e.y1) >= _TILE_LIKE_MIN_H]
    tile_base = min((e.x1 for e in tall), default=None)
    # THE TALLEST ROW, not the median. A screen usually holds one good and several
    # materials, so the median IS a material's height and nothing ever measures as short —
    # with rows of 133/105/105 the median is 105 and a 105px material fails its own test.
    # The tallest row is a good's height whenever a good is present; when every row is a
    # material the tallest is a material too, no row measures short, and the pin decides
    # alone, which is exactly right for a continuation screen.
    tall_h = max((e.y2 - e.y1 for e in tall), default=None)
    _heights = [e.y2 - e.y1 for e in tall]
    has_spread = bool(_heights) and (max(_heights) - min(_heights)) >= _HEIGHT_SPREAD_MIN
    col_right = statistics.median([e.x2 for e in tiles])
    any_pin = any(_row_pin(t, icons) for t in tiles)
    out: list[TradeRow] = []
    for tile in tiles:
        name_el = _name_for_qty(tile, names, col_right)
        name = (name_el.label or "").strip() if name_el is not None else ""
        if not name:
            # A row we cannot name. It still BREAKS the run of materials — otherwise the
            # next good's materials are swept into the previous one (live 2026-08-21: Glass
            # Bead and Candle became Box of Nutmeg materials, routing the gather to Tripoli
            # and Amsterdam). An empty name marks the break; the stitcher honours it.
            out.append(TradeRow(name="", qty=0, is_material=False, y=tile.y1))
            continue
        # THE ROW CARRIES THREE SIGNALS and each fails differently, so no one of them decides
        # alone:
        #
        #   * a LOCATION PIN at the row's right edge — materials have one, goods do not
        #   * an INDENT — materials sit ~29px right of a good's flush-left edge
        #   * HEIGHT — a good's row is taller (~133px) than a material's (~105px)
        #
        # The pin leads, because it is the only signal that still works on a CONTINUATION
        # screen: when every row is a material the indent baseline is itself a material and
        # the height spread vanishes, so both would vote "good" for the whole screen and
        # outvote a correct pin. A plain majority is therefore worse than the pin alone.
        #
        # But the pin fails in the dangerous direction: a pin that goes undetected promotes a
        # material to a GOOD, and a good with a material's rows beneath it becomes a wrong
        # recipe. Live at Svear 2026-08-25 that turned Birch Tree and Iron into goods. So the
        # geometry is consulted exactly there — a row with no pin that is BOTH indented and
        # short is a material anyway. Both must agree; either one alone is too noisy.
        pin = _row_pin(tile, icons) or (_row_pin(name_el, icons) if name_el else None)
        if any_pin:
            is_material = bool(pin)
            # Requiring indent AND height together was a pixel too strict: at Svear a
            # material's box came back at x1=1709 against a 1695 baseline — 14px of indent
            # where the rule wanted 15 — so a row that was plainly short stayed a good.
            # EITHER signal is enough, but only while the screen shows a real spread of row
            # heights. That spread is what says goods and materials are both present; on a
            # continuation screen every row is a material, the spread collapses, geometry
            # falls silent, and the pin decides alone — which is correct there.
            if not pin and has_spread and (_looks_indented(tile, tile_base)
                                           or _looks_short(tile, tall_h)):
                is_material = True
        else:
            is_material = tile.x1 - base > _INDENT_PX
        out.append(TradeRow(name=name, qty=int(tile.label.replace(",", "")),
                            is_material=is_material, y=tile.y1))
    from actions.trade_list_stitch import dedupe_adjacent
    return dedupe_adjacent(out)


def rows_to_trades(rows) -> list[VillageTrade]:
    """A flat row sequence grouped into recipes: a good, then the materials beneath it."""
    out: list[VillageTrade] = []
    cur: Optional[VillageTrade] = None
    for r in rows:
        if not r.name:
            cur = None                      # an unreadable row ends the current recipe
            continue
        if not r.is_material:
            cur = VillageTrade(good=r.name, obtain=r.qty)
            out.append(cur)
        else:
            if cur is None:
                cur = VillageTrade(good="", obtain=0)   # materials before any named good
                out.append(cur)
            cur.materials[r.name] = r.qty
    return out


def parse_trade_list(elements) -> list[VillageTrade]:
    """Parse ONE screen of the Village Info -> Barter -> Trade List into VillageTrade rows.

    Kept for callers that read a single screen. A multi-screen read should go through
    `parse_trade_rows` + `trade_list_stitch.stitch_screens`, which does not have to guess
    what a recipe split across the fold belongs to.
    """
    return rows_to_trades(parse_trade_rows(elements))


def merge_trade_screens(screens: list[list[VillageTrade]]) -> list[VillageTrade]:
    """Accumulate scrolled Trade List screens.  A screen that STARTS with a continuation
    (good=='') appends its materials to the last good seen so far; duplicate goods merge."""
    order: list[str] = []
    acc: dict[str, VillageTrade] = {}
    last: Optional[str] = None
    for screen in screens:
        for t in screen:
            if t.good:
                if t.good not in acc:
                    acc[t.good] = VillageTrade(good=t.good, obtain=t.obtain)
                    order.append(t.good)
                acc[t.good].materials.update(t.materials)
                last = t.good
            elif last:                                    # continuation rows
                acc[last].materials.update(t.materials)
    return [acc[g] for g in order]


def parse_base_tab(elements, frame_w: int = 2400) -> dict:
    """Parse the Village Info **Base** tab: amity grade/points + Daily Barter Progress.

    Returns {amity_grade, amity_points:(cur,cap)|None, barters_used, barters_total} with
    None fields when unreadable."""
    out = {"amity_grade": None, "amity_points": None,
           "barters_used": None, "barters_total": None}
    labs = [((getattr(e, "label", "") or "").strip(), e) for e in elements or []]
    for lab, e in labs:
        m = re.fullmatch(r"([\d,]+)\s*/\s*([\d,]+)", lab)
        if m and getattr(e, "x1", 0) > frame_w * _PANEL_FRAC:
            a, b = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
            if b >= 10_000:                              # amity bar (e.g. 93,958/100,000)
                out["amity_points"] = (a, b)
            elif b <= 20:                                # Daily Barter Progress (e.g. 0/7)
                out["barters_used"], out["barters_total"] = a, b
        elif lab in ("Neutral", "Friendly", "Trusting", "Devoted", "Hostile", "Wary"):
            out["amity_grade"] = lab
    return out


def material_pins(elements) -> dict:
    """{material name: (cx, cy)} for every row that carries a location pin.

    The pin links a material to the ports that sell it (the 'Source' panel). Rows are
    anchored on their NAME, exactly as in `parse_trade_list`."""
    rows = _qty_elements(elements)
    names = _row_names(elements, rows)
    icons = _pin_icons(elements)
    if not rows:
        return {}
    col_right = statistics.median([e.x2 for e in rows])
    out = {}
    for tile in rows:
        name_el = _name_for_qty(tile, names, col_right)
        if name_el is None:
            continue
        pin = _row_pin(tile, icons) or _row_pin(name_el, icons)
        if pin:
            out[(name_el.label or "").strip()] = pin
    return out


def screen_signature(trades: list) -> tuple:
    """A stable fingerprint of one parsed Trade List screen.  The scroll loop stops
    when a swipe produces the SAME signature (the list has hit its bottom)."""
    return tuple((t.good, t.obtain, tuple(sorted(t.materials.items()))) for t in trades)


# ── Remote check driver ───────────────────────────────────────────────────────
# Reads a village's LIVE barter state from a PORT: world map → find village → tap →
# Village Info → Base tab + Barter tab (scroll-accumulated) → optional source pins →
# KB write-back of the invariants.  Never taps 'Move to Village' — this is a REMOTE
# read; the mission sails to the village later, after gathering.

# The day's barter allowance — a GAME RULE, so it is owned by the task layer and re-exported
# here for the callers that already import it from this module.
from brain.barter_quantity import MAX_DAILY_BARTER_ROUNDS

# A row's name can sit ~100px above its own badge (the badge is in the thumbnail's
# bottom-right corner), so the band around the badge column has to be at least that generous.
_CHROME_MARGIN = 100

# The location pins sit in a column at the row's right edge; the category chips' own icons
# sit well inside it. Only the DISTANCE BETWEEN the columns is fixed here — no absolute x
# bound, which is the mistake this whole session has been about: a first attempt capped the
# column at x<2260 and discarded pins that sit further right.
_PIN_COLUMN_TOL = 40

_PANEL_X_MIN = 1700                     # Village Info panel occupies the right ~700px
# SCROLL IN OVERLAPPING STEPS, NOT JUMPS. The panel shows ~4 rows and a row is ~116px tall
# (measured at Svear Village, 2026-08-24: row tops 405/548/642/753). A -380 scroll advanced
# 3.3 rows of the ~4.3 visible, leaving barely one row of overlap — so a row straddling the
# fold could be half-visible on one screen and gone from the next. Svear's Näverslöjd was
# exactly that: its name showed at the bottom of screen 1, its own row never appeared whole,
# and its materials (Lingonberry, Vodka) surfaced at the top of screen 2 where they were
# attributed to the previous good. The village has FOUR barter goods and the read saw two.
#
# Half a panel per step keeps ~2 rows of overlap, so every row is fully visible on at least
# one screen. The loop still stops early when a screen adds nothing new (screen_signature),
# so the extra steps cost nothing on short lists.
_SCROLL_X, _SCROLL_Y = 2080, 880
# The Trade List's icons — the row thumbnails and the location pins — come back far less
# confidently than the parser's global 0.30 floor assumes. Measured on the device: pins at
# 0.297 and 0.636, thumbnails at 0.127, 0.195, 0.246, 0.251, 0.42, 0.49, 0.56. The floor runs
# straight through that distribution, so whether a row is classified correctly is close to a
# coin flip per screen — Iron at Svear lost its pin by 0.003 and became a good.
# Lowered HERE ONLY. The same floor elsewhere guards navigation, market reading and dialog
# detection, and nothing has been measured about those.
_TRADE_LIST_ICON_CONF = 0.18
_PANEL_MARGIN = 15


def _panel_bounds(elements):
    """(x1, x2) of the Village Info panel, from its own landmarks.

    The panel is a strip on the right; everything left of it is the world map showing
    through. The map carries its own labels — port names, sea names — and they parse exactly
    like row names, so a village's row list can pick up "Pawnee" or "Atlantic Ocean" from the
    background. Bounding by the panel's own chrome keeps the reader inside the panel without
    hardcoding where the panel is.
    """
    spans = []
    for e in _labelled(elements):
        lab = (e.label or "").strip().lower()
        if lab in ("trade list", "closeout", "village info") or "exchange unit" in lab:
            spans.append((e.x1, e.x2))
    if not spans:
        return None
    return (min(a for a, _ in spans) - _PANEL_MARGIN,
            max(b for _, b in spans) + _PANEL_MARGIN)


def fill_missing_row_quantities(frame, elements) -> list:
    """Recover a row's quantity badge from ITS OWN TILE when the whole-frame parse lost it.

    The trade list draws each row as a thumbnail tile with the quantity as a badge, and
    OmniParser labels the whole tile with that text. On a full 2400x1080 frame the small
    badges are unreliable: live 2026-08-27 at Svear it read `689`, `88` and `88` but never
    proposed Matchlock Gun's `44` AS TEXT AT ALL — the tile came back as a bare `icon`. The
    row parser drops a material with no quantity, so a three-material recipe read as two,
    and `_target_complete` then certified its own partial read as complete and planned from
    it for five runs.

    Cropping that one tile and reading it at x4 returns `44` at confidence 1.0. This is the
    rule from CLAUDE.md applied here — downscaled/whole-frame reads answer WHICH SCREEN,
    never HOW MANY — and the combination it prescribes: the whole frame stays authoritative
    wherever it produced a value, and a region crop fills only the gaps.

    The tile is LOCATED from the rows that did read, never from a constant: their badge
    tiles give the column and the height, and the missing row's own name gives the y band.
    Returns `elements` plus one synthesised text element per recovered badge.
    """
    import re as _re

    import numpy as _np
    from loguru import logger

    qtys = _qty_elements(elements)
    named = _row_names(elements, qtys) if qtys else []
    if not qtys or not named:
        return elements                      # nothing to take the geometry from

    # The badge sits BELOW its name, in the tile column left of it. Both are measured from
    # the rows that DID read, so the crop follows the panel wherever the camera-cutout
    # offset puts it — never a baked coordinate.
    bx1 = min(int(e.x1) for e in qtys) - 40
    bx2 = max(int(e.x2) for e in qtys) + 10
    tops = sorted(int(e.y1) for e in named)
    pitch = int(statistics.median([b - a for a, b in zip(tops, tops[1:])])) if len(tops) > 1 else 120

    def _has_badge(name_el) -> bool:
        """A row owns the badge whose centre falls in ITS band — name top to the next row."""
        top = int(name_el.y1)
        return any(top <= (int(q.y1) + int(q.y2)) // 2 <= top + pitch for q in qtys)

    from vision.ocr import _get_reader
    recovered = list(elements)
    for name_el in named:
        if _has_badge(name_el):
            continue
        label = (name_el.label or "").strip()
        box = (max(0, bx1), int(name_el.y1),
               min(frame.width, bx2), min(frame.height, int(name_el.y1) + pitch))
        try:
            crop = frame.crop(box)
            up = crop.resize((crop.width * 4, crop.height * 4), Image.LANCZOS)
            toks = _get_reader().readtext(_np.array(up), detail=1)
        except Exception as exc:
            logger.warning(f"[village_check] could not re-read the badge for {label!r}: {exc}")
            continue
        best, best_conf = None, 0.0
        for _b, text, conf in toks:
            digits = _re.sub(r"\D", "", text)
            if digits and float(conf) > best_conf:
                best, best_conf = digits, float(conf)
        if best is None:
            logger.warning(f"[village_check] {label!r} has no quantity and its tile {box} "
                           "did not read either — the row stays incomplete rather than "
                           "being guessed")
            continue
        logger.info(f"[village_check] {label!r}: the whole-frame parse lost its badge; its "
                    f"own tile reads {best} (conf {best_conf:.2f}) at x4")
        recovered.append(SimpleNamespace(
            label=best, element_type="text", confidence=best_conf,
            x1=box[0], y1=box[1], x2=box[2], y2=box[3]))
    return recovered


def trade_list_elements(frame):
    """The elements of the Trade List panel: icons at a lower floor, nothing off-panel.

    Two departures from the ordinary parse, both measured rather than assumed — see
    `_TRADE_LIST_ICON_CONF` and `_panel_bounds`.
    """
    from vision.omniparser import parse_raw
    els = []
    for e in parse_raw(frame, yolo_conf=0.05):
        lab = (e.label or "").strip()
        if e.element_type == "text":
            if e.confidence >= 0.40 and len(lab) >= 2:
                els.append(e)
        elif e.confidence >= _TRADE_LIST_ICON_CONF:
            els.append(e)

    bounds = _panel_bounds(els)
    viewport = _list_viewport(els)
    if bounds is None:
        return els                       # no panel landmarks — do not silently discard
    lo, hi = bounds
    out = []
    for e in els:
        cx = (e.x1 + e.x2) // 2
        if not (lo <= cx <= hi):
            continue                     # the world map behind the panel
        if viewport and (e.y2 < viewport[0] - 120 or e.y1 > viewport[1] + 120):
            continue                     # panel chrome far above or below the list
        out.append(e)
    # A row whose badge the whole-frame parse missed is filled from its OWN tile here, so
    # every caller of this function gets the complete list — the row parser downstream
    # DROPS a material with no quantity, and a dropped material becomes a partial recipe.
    return fill_missing_row_quantities(frame, out)


def _list_viewport(elements):
    """(top, bottom) of the scrollable Trade List, from the panel's own landmarks.

    The list runs from the "Trade List" tab down to the "View by Min. Exchange Unit"
    checkbox. Both are always drawn, so the bounds come from the panel rather than from
    constants that drift with a layout change.
    """
    top = bottom = None
    for e in _labelled(elements):
        lab = (e.label or "").strip().lower()
        if lab in ("trade list", "closeout"):
            top = e.y2 if top is None else max(top, e.y2)
        elif "exchange unit" in lab:
            bottom = e.y1 if bottom is None else min(bottom, e.y1)
    return (top, bottom) if (top and bottom and bottom > top) else None


# A quantity element this tall is the row's thumbnail rather than the bare badge text.
_TILE_LIKE_MIN_H = 80
# A material's row runs shorter than a good's by roughly 30px; half of that is a safe margin.
_SHORT_ROW_MARGIN = 12
# Rows differing by at least this much mean the screen holds BOTH goods and materials, so
# height and indent carry information. Below it every row is the same kind and they do not.
_HEIGHT_SPREAD_MIN = 20


def _looks_indented(tile, tile_base) -> bool:
    """True when this row starts right of the flush-left edge, as a material does."""
    if tile_base is None or (tile.y2 - tile.y1) < _TILE_LIKE_MIN_H:
        return False
    return (tile.x1 - tile_base) > _INDENT_PX


def _looks_short(tile, tall_h) -> bool:
    """True when this row is shorter than the tallest row on screen, as a material is."""
    if tall_h is None or (tile.y2 - tile.y1) < _TILE_LIKE_MIN_H:
        return False
    return (tile.y2 - tile.y1) < tall_h - _SHORT_ROW_MARGIN


# Reduced from -230 on 2026-08-27. Half a panel still left `align()` returning None —
# "screen 2 does not fit the rows read so far" on every scroll of a four-screen read — and an
# alignment that fails does not merely lose rows, it MISATTRIBUTES them: the recovery appends
# the screen wholesale, so a material surfaces under the wrong good and the good's own row was
# read as one of its materials ("dropping 'Birch Tree' — not a known material"). Matchlock Gun
# was never returned across four screens because of it.
#
# ~1.5 rows a step leaves ~2.5 rows of overlap, which is what `align` needs to find its
# footing. The loop stops as soon as a screen adds nothing new, so the extra steps cost
# nothing on a short list — and an alignment that holds is worth several scrolls.
_SCROLL_DY = -170
_MAX_SCROLLS = 14


@dataclass
class VillageCheck:
    """One remote read of a village: the VOLATILE numbers a barter plan needs."""
    village: str
    ok: bool = False
    reason: str = ""
    amity_grade: Optional[str] = None
    amity_points: Optional[tuple] = None
    barters_used: Optional[int] = None
    barters_total: Optional[int] = None
    trades: list = field(default_factory=list)          # list[VillageTrade]
    sources: dict = field(default_factory=dict)         # {material: [source ports]}

    @property
    def rounds_remaining(self) -> Optional[int]:
        """Daily barter rounds still available — planned against the DAY'S CEILING.

        7 IS THE BEST HOPE, NOT A PROMISE. **Every amity grade opens ONE MORE barter**, and
        reaching Friendly is where the allowance reaches 7 (user, 2026-08-24). Amity grows as
        you barter — San Village climbed Neutral → Favorable → Trusting inside one session —
        so the Base tab's total is a snapshot at the grade you ARRIVE with, and it rises with
        each level gained while trading. Hutu showed 0/4 and San 0/5 on the same day.

        Prepare for the best and accept less (user, 2026-08-24): if amity does not reach
        Friendly before the available barters are spent, 7 rounds will not happen, and that
        is a normal outcome — not a failure. What planning on the ARRIVAL total does
        guarantee is coming up short: the gather is sized for fewer rounds than the day may
        open, and the fleet sails away leaving earned rounds unused.

        The real limits assert themselves at the panel every round — the day's rounds spent,
        or the materials run out. Over-planning costs hold space; under-planning wastes a
        voyage.
        """
        if self.barters_used is None or self.barters_total is None:
            return None
        ceiling = max(int(self.barters_total), MAX_DAILY_BARTER_ROUNDS)
        return max(0, ceiling - int(self.barters_used))

    def trade_for(self, good: str) -> Optional[VillageTrade]:
        """The village's trade row for `good` (case/spacing-insensitive)."""
        key = _norm_name(good)
        for t in self.trades:
            if _norm_name(t.good) == key:
                return t
        return None


def _norm_name(s: str) -> str:
    return " ".join((s or "").lower().split())


def read_village_barter_remote(village: str, *, good: Optional[str] = None,
                               learn_sources: bool = True,
                               max_scrolls: int = _MAX_SCROLLS,
                               write_back: bool = True) -> VillageCheck:
    """Read `village`'s live barter recipes + daily rounds REMOTELY, from a port.

    `good` narrows the (tap-costly) source-pin learning to one good's materials;
    None learns sources for every material still missing them in the KB.
    Returns a VillageCheck — `ok=False` with a structured `reason` on any miss, so
    the caller (mission / command driver) decides what to do (rule: escalate, don't
    absorb).  Leaves the bot back on port_overworld."""
    # DEPRECATED, AND LOUD ABOUT IT — same treatment as `open_world_map`, for the same reason.
    # It drives the whole world map itself, including a `for i in range(max_scrolls)` over the
    # trade list, from inside what is supposed to be a reader. Its one production caller was
    # the barter task runner, which now returns a `RemoteCheck` work order instead
    # (Guiding Principle #7): the dispatcher routes it to `WorldMapActivity`, which reads one
    # screen per tick and refuses to certify a short list.
    #
    # Left standing rather than deleted: it is the reference for what the activity must
    # reproduce, and its tests still describe behaviour the activity is held to. Any caller
    # appearing in a log is a path that went around the dispatcher.
    import inspect as _inspect

    _caller = "?"
    for _fr in _inspect.stack()[1:]:
        if _fr.filename != __file__:
            _caller = f"{_fr.filename.rsplit('/', 1)[-1]}:{_fr.lineno} in {_fr.function}()"
            break
    from loguru import logger

    logger.warning(f"[village_check] read_village_barter_remote DEPRECATED — called from "
                   f"{_caller}. Use the RemoteCheck goal + WorldMapActivity; report this "
                   f"caller.")
    from actions import ui
    from actions.sail_actions import open_world_map, pan_to_village, _find_button, exit_to_overworld
    from actions.adb_actions import tap
    from capture.adb_capture import capture_screen
    from vision.omniparser import parse_fast_cached

    check = VillageCheck(village=village)

    if not open_world_map():
        check.reason = "could not open the world map"
        return check
    # SEARCH THE VILLAGE LIST FIRST — panning matches the village's LABEL ON THE MAP, and a
    # label can be occluded: Svear Village sits beside a discovery marker that partly covers
    # its name, so 8 stride pans found nothing and the check failed outright (live
    # 2026-08-24). The Explore tab's village list names it regardless of what the map draws.
    # The sailing flow has searched since earlier that day; this REMOTE path still panned,
    # because the two are separate code paths.
    from actions.sail_actions import _try_village_search, _find_destination_button, _ocr_frame

    pos = None
    if _try_village_search(village):
        # The list tap already selects the village and opens its Info panel; the map has
        # centred on it, so there is nothing further to tap.
        ui.settle("screen", why="village selected from the list → Village Info panel")
        pos = "selected"
    if pos is None:
        pos = pan_to_village(village)
        if not pos:
            check.reason = f"village {village!r} not found on the world map"
            exit_to_overworld(timeout=60.0)
            return check
        tap(*pos)                               # position came from pan_to_village's own read
        ui.settle("screen", why="village selected → Village Info panel")
    frame = capture_screen()
    if _find_button(frame, "barter", x_min=_PANEL_X_MIN) is None:
        check.reason = "Village Info panel did not open (no Barter tab in the panel)"
        exit_to_overworld(timeout=60.0)
        return check

    # ── Base tab: amity + daily barter rounds ────────────────────────────────
    if ui.tap_text(frame, "base", x_min=_PANEL_X_MIN, why="Village Info → Base tab"):
        frame = capture_screen()
    base = parse_base_tab(parse_fast_cached(frame))
    check.amity_grade = base["amity_grade"]
    check.amity_points = base["amity_points"]
    check.barters_used = base["barters_used"]
    check.barters_total = base["barters_total"]
    logger.info(f"[village_check] {village}: amity={check.amity_grade} "
                f"{check.amity_points} barters {check.barters_used} USED of "
                f"{check.barters_total} — {check.rounds_remaining} still available today")

    # ── Barter tab: scroll-accumulate the Trade List ─────────────────────────
    if not ui.tap_text(frame, "barter", x_min=_PANEL_X_MIN, dwell="dialog",
                       why="Village Info → Barter tab"):
        check.reason = "Barter tab not found after reading the Base tab"
        exit_to_overworld(timeout=60.0)
        return check

    known = _kb_sources()
    # STITCH THE SCREENS, DO NOT MERGE PER-SCREEN RECIPES. A good and its materials routinely
    # straddle the fold, so no single screen holds the whole recipe and merging per-screen
    # groups attributes the leftovers to whichever good happened to be named — which wrote
    # `Juniper Berry <- Iron 130` and `Meteorite <- Vodka` at Svear (2026-08-24). Rows are
    # reassembled on their shared content first and the recipes read once from the finished
    # sequence, so nothing is ever attributed by position within a screen.
    from actions.trade_list_stitch import align, alignable, dedupe_adjacent
    from vision.list_position import scrollbar_thumb, bar_position, content_shift

    rows: list = []
    prev_frame = None
    expect_movement = False
    for i in range(max_scrolls):
        frame = capture_screen()
        # THE TRADE LIST GETS ITS OWN PARSE: a lower icon floor, because the row thumbnails
        # and location pins come back under the global 0.30 (a pin measured 0.297 and its row
        # became a good), and clipped to the panel, because the world map shows beside it and
        # its labels parse exactly like row names.
        elements = trade_list_elements(frame)
        screen_rows = dedupe_adjacent(alignable(parse_trade_rows(elements)))

        moved = None
        if prev_frame is not None and expect_movement:
            dy, err, zero = content_shift(prev_frame, frame)
            moved = bool(dy != 0 and err < 0.6 * zero)
        prev_frame = frame

        if screen_rows:
            if not rows:
                rows = list(screen_rows)
            else:
                p = align(rows, screen_rows)
                if p is None:
                    # The scroll jumped past rows nobody saw. Bridging would fabricate an
                    # order; record the break and keep going rather than inventing one.
                    logger.warning(f"[village_check] screen {i + 1} does not fit the rows "
                                   "read so far — the scroll overshot and rows are missing")
                    rows.extend(screen_rows)
                else:
                    end = p + len(screen_rows)
                    rows.extend(screen_rows[len(rows) - p:] if end > len(rows) else [])

        if learn_sources:
            partial = rows_to_trades(rows)
            wanted = _materials_needing_sources([partial], partial, good, known,
                                                check.sources)
            if wanted:
                check.sources.update(_learn_sources_on_screen(elements, wanted))

        # THE LIST DECIDES WHEN IT IS FINISHED, not the parse. A screen that produced nothing
        # new may be a screen we failed to READ; only the scrollbar and a measured absence of
        # movement mean there is nothing left.
        viewport = _list_viewport(elements)
        _at_top, at_end = bar_position(scrollbar_thumb(frame, viewport), viewport)
        if moved is False and at_end:
            logger.info(f"[village_check] trade list bottom reached after {i + 1} screen(s)")
            break

        # Stop as soon as the good we came for is fully read (user 2026-08-21: "we only
        # need to know what is needed for boxes of nutmeg"). This leaves the village record
        # PARTIAL — the goods further down the list are never seen — which is why
        # `learn_village_barter` exists for building the knowledge base.
        if good and _target_complete(rows_to_trades(rows), good, village):
            logger.info(f"[village_check] {good!r} fully read after {i + 1} screen(s) — "
                        "not scrolling the rest of the list (this village record is PARTIAL)")
            break
        ui.scroll(_SCROLL_X, _SCROLL_Y, _SCROLL_DY, why=f"trade list, screen {i + 1}")
        expect_movement = True

    check.trades = [t for t in rows_to_trades(rows) if (t.good or "").strip()]
    if not check.trades:
        check.reason = "Trade List parsed empty (no barter goods read)"
        exit_to_overworld(timeout=60.0)
        return check

    _reconcile_with_known(check.trades, good, village)
    missing = _missing_known_materials(check.trades, good, village)
    if missing:
        check.reason = (f"incomplete read: {missing} known for {good!r} but not seen on "
                        "the panel — materials are invariant, so this is a partial read, "
                        "not a recipe change")
        logger.error(f"[village_check] {check.reason}")
        if write_back:
            write_back_invariants(check)      # keep sources/villages learned this pass
        exit_to_overworld(timeout=60.0)
        return check

    check.ok = True
    logger.info(f"[village_check] {village}: " + "; ".join(
        f"{t.good} {t.obtain} ← {t.materials}" for t in check.trades))
    if write_back:
        write_back_invariants(check)
    exit_to_overworld(timeout=60.0)                    # leave on port_overworld
    return check


def _materials_needing_sources(screens, trades, good, known, learned) -> dict:
    """{material: None} for rows on THIS screen whose source ports are still unknown.

    Attributes continuation rows to the last good seen across screens (same rule as
    `merge_trade_screens`), so a `good` filter survives a scroll boundary."""
    last = ""
    for scr in screens[:-1]:
        for t in scr:
            if t.good:
                last = t.good
    target = _norm_name(good) if good else None
    wanted = {}
    for t in trades:
        name = t.good or last
        if t.good:
            last = t.good
        if target and _norm_name(name) != target:
            continue
        for m in t.materials:
            if not known.get(_norm_name(m)) and m not in learned:
                wanted[m] = None
    return wanted


def _learn_sources_on_screen(elements, wanted: dict) -> dict:
    """Tap the location pin of each wanted material → read its Source panel → Back.

    Returns {material: [ports]} for the ones read.  Best-effort: a material whose
    pin or Source panel doesn't read is simply left unlearned (the caller reports it
    as unsourced rather than the check failing)."""
    from loguru import logger
    from actions import ui
    from actions.sail_actions import _find_button
    from capture.adb_capture import capture_screen
    from actions.village_remote_reader import read_material_sources_frame

    pins = material_pins(elements)
    out = {}
    for material in wanted:
        pin = pins.get(material)
        if pin is None:
            continue
        ui.tap_at(*pin, dwell="dialog", why=f"source pin for {material}")
        frame = capture_screen()
        try:
            ports = read_material_sources_frame(frame)
        except Exception as exc:
            logger.warning(f"[village_check] source read for {material!r} failed: {exc}")
            ports = []
        if ports:
            out[material] = ports
            logger.info(f"[village_check] {material} ← sources {ports}")
        # Back ONLY if a Source panel really opened — a Back on the trade list would
        # close the Village Info panel and the next scroll would swipe the world map.
        if ports or _find_button(frame, "source", "market") is not None:
            ui.back(why=f"close the Source panel for {material}")
        else:
            logger.warning(f"[village_check] no Source panel opened for {material!r} "
                           "— not pressing Back (would close the village panel)")
    return out


def _kb_sources() -> dict:
    """{normalised material: [ports]} already known across every KB recipe."""
    from memory.barter_kb import all_recipes
    known = {}
    for r in all_recipes():
        for i in r.inputs:
            if i.source_ports:
                known[_norm_name(i.material)] = list(i.source_ports)
    return known


def _target_complete(trades, good: str, village: Optional[str]) -> bool:
    """True when `good`'s material list has been read in full, so scrolling can stop.

    Complete means either every material the KB knows for this village now has a
    quantity, or — for a good the KB has never seen — the good's row has been followed by
    ANOTHER good's row, which closes its list."""
    from memory.barter_kb import load_recipe
    target = None
    seen_after = False
    for t in trades:
        if _norm_name(t.good) == _norm_name(good):
            target = t
        elif target is not None and t.good:
            seen_after = True
    if target is None or not target.materials:
        return False
    known = load_recipe(good)
    if known is None:
        return seen_after

    # THE PER-VILLAGE LIST CANNOT CERTIFY ITSELF.
    #
    # `inputs_for(village)` is a refinement written FROM READS — so a read that scrolled
    # short writes a short list, and the next read consults that list, sees every material
    # in it, declares "fully read", stops scrolling, and writes the same short list again.
    # The truncation confirms itself and never recovers.
    #
    # Live 2026-08-26 at Svear: the recipe held {Iron, Candle, Matchlock Gun} and the village
    # list held {Iron, Candle}. Every check that day logged "'Birch Tree' fully read after 1
    # screen(s) — not scrolling", and then — in the SAME function, two lines later —
    # "keeping known material 'Matchlock Gun' that this read did not return". It had the
    # evidence its own verdict was wrong and did not use it.
    #
    # So completeness is judged against the UNION: materials do not vanish, and a village
    # that genuinely uses fewer simply never shows the extra one, costing a few scrolls to
    # the bottom. Scrolling too far is cheap; stopping too early cost the whole day.
    village_inputs = known.inputs_for(village) or []
    recipe_inputs = list(getattr(known, "inputs", None) or [])
    required = {_norm_name(i.material) for i in village_inputs}
    required |= {_norm_name(i.material) for i in recipe_inputs}
    if required:
        have = {_norm_name(m) for m in target.materials}
        missing = required - have
        if missing:
            from loguru import logger as _log
            _log.info(f"[village_check] {good!r} not complete yet — still missing "
                      f"{sorted(missing)}; scrolling on")
        return not missing
    return seen_after


def _reconcile_with_known(trades, good: Optional[str], village: Optional[str]) -> None:
    """Drop materials the KB says this village does not use for this good.

    The user's model, applied literally: **the material TYPES for a barter good do not
    change; the NUMBERS re-roll every ~6 hours.** So for a good+village already in the KB,
    the type set is authoritative and the live read supplies only quantities. A material
    the panel appears to show but the KB has never seen is misattribution — a later good's
    row swept in because its own name was not detected (live 2026-08-21: Glass Bead and
    Candle attached to Box of Nutmeg, and the plan set off to buy them in Tripoli and
    Amsterdam).

    Only applies where the KB HAS a list for this village; a genuinely new good or village
    is learned as read."""
    if not good:
        return
    from loguru import logger
    from memory.barter_kb import load_recipe
    known = load_recipe(good)
    if known is None:
        return
    known_inputs = known.inputs_for(village)
    if not known_inputs:
        return
    allowed = {_norm_name(i.material) for i in known_inputs}
    for t in trades:
        if _norm_name(t.good) != _norm_name(good):
            continue
        extra = [m for m in t.materials if _norm_name(m) not in allowed]
        for m in extra:
            logger.warning(f"[village_check] {good} at {village}: dropping {m!r} — not a "
                           "known material for this village, so it belongs to another "
                           "good's row (material types are invariant; only quantities move)")
            t.materials.pop(m, None)


def _missing_known_materials(trades, good: Optional[str], village: Optional[str] = None) -> list:
    """Materials the KB knows for `good` that this read did NOT return.

    Quantities re-roll every ~6h but the MATERIAL LIST does not, so a shortfall here
    means the panel was read incompletely (a row whose name missed, or a scroll that
    skipped one) — never that the recipe changed. Returning them lets the caller refuse
    to plan on a partial recipe rather than gather the wrong shopping list."""
    if not good:
        return []
    from memory.barter_kb import load_recipe
    known = load_recipe(good)
    if known is None:
        return []
    # Per-village, because the SAME good takes different materials at different villages
    # (Melanesian/Khmer use Textiles where Malay uses Dhaka Muslin). Comparing against the
    # union would flag a material this village never wanted.
    known_inputs = known.inputs_for(village)
    if not known_inputs:
        return []
    for t in trades:
        if _norm_name(t.good) == _norm_name(good):
            seen = {_norm_name(m) for m in t.materials}
            return sorted(i.material for i in known_inputs
                          if _norm_name(i.material) not in seen)
    return []


def write_back_invariants(check: VillageCheck) -> None:
    """Persist what the check learned that does NOT expire.

    INVARIANT (safe to keep): a good's material LIST, the villages offering it, and
    each material's source ports.  VOLATILE (refreshes every ~6h): the quantities —
    kept only as the last-seen nominal (`RecipeInput.ratio`, `output_per_round`) so a
    planner without a fresh check has a starting point.  Plans use the CHECK, not this."""
    from loguru import logger
    from memory.barter_kb import (BarterRecipe, RecipeInput, Village,
                                  load_recipe, save_recipe, save_village)
    for trade in check.trades:
        if not trade.materials:
            # A barter recipe with no inputs is not a recipe.  This drops rows where the
            # name came back as overlay text rather than a good — e.g. the amity-lock
            # tooltip ("…he Village Amity") sitting over a locked tile, read live
            # 2026-08-21 as a good called 'he Village'.
            logger.warning(f"[village_check] skipping {trade.good!r} — no materials read "
                           "(locked tile, tooltip, or a partial screen)")
            continue
        existing = load_recipe(trade.good)
        recipe = existing or BarterRecipe(good=trade.good)
        prior = {_norm_name(i.material): i for i in recipe.inputs}
        seen = set()
        inputs = []
        for material, need in trade.materials.items():
            old = prior.get(_norm_name(material))
            sources = check.sources.get(material) or (old.source_ports if old else []) or []
            inputs.append(RecipeInput(material=material, ratio=int(need),
                                      source_ports=list(sources)))
            seen.add(_norm_name(material))
        # MERGE, never replace.  A material list is INVARIANT — if this read did not see
        # one the KB already knows, that is a partial READ, not a recipe change.  The
        # replacing version silently deleted Coral from Box of Nutmeg when one live read
        # returned only Ebony + Textiles (2026-08-21); the mission would then have
        # gathered two of three materials and found Exchange greyed out at the village.
        for key, old in prior.items():
            if key not in seen:
                logger.warning(f"[village_check] {trade.good}: keeping known material "
                               f"{old.material!r} that this read did not return")
                inputs.append(old)
        # Per-village list: exactly what THIS village asked for, no merging across
        # villages (that would invent a recipe no village accepts).
        from memory.barter_kb import _slug
        recipe.village_inputs = dict(recipe.village_inputs or {})
        slug = _slug(check.village)
        per = [RecipeInput(material=m, ratio=int(q),
                           source_ports=list(check.sources.get(m)
                                             or (prior[_norm_name(m)].source_ports
                                                 if _norm_name(m) in prior else [])))
               for m, q in trade.materials.items()]

        # A PARTIAL READ MUST NOT SHRINK THIS VILLAGE'S OWN RECORD.
        #
        # The union list above already keeps a material this read did not return — and the
        # per-village list, written raw from `trade.materials`, did not. That is how Svear's
        # list came to hold {Iron, Candle} while the screen shows three, and the consequences
        # ran deep: `_target_complete` consulted it and certified a short read as finished,
        # and `plan_barter_task` consulted it and reported Matchlock Gun UNSOURCED even
        # though the KB held thirteen source ports for it (live 2026-08-27).
        #
        # Keeping a prior entry is NOT merging across villages — the thing the docstring
        # rightly forbids. It is this village's own earlier reading of itself, and a read
        # that admits it was incomplete has no standing to erase it.
        seen = {_norm_name(i.material) for i in per}
        for old_input in (recipe.village_inputs.get(slug) or []):
            if _norm_name(old_input.material) not in seen:
                logger.warning(f"[village_check] {check.village}: keeping {old_input.material!r} "
                               "in this village's material list — this read did not return it, "
                               "and a partial read must not shrink the record")
                per.append(old_input)
        recipe.village_inputs[slug] = per
        recipe.inputs = inputs
        if check.village not in recipe.villages:
            recipe.villages.append(check.village)
        if check.amity_grade:
            recipe.output_per_round[check.amity_grade] = int(trade.obtain)
        save_recipe(recipe)
    save_village(Village(name=check.village, amity=check.amity_grade,
                         amity_points=(check.amity_points or (None,))[0],
                         barter_rounds_total=check.barters_total,
                         barter_rounds_remaining=check.rounds_remaining,
                         eligible_goods=[t.good for t in check.trades]))
    logger.info(f"[village_check] KB updated: {len(check.trades)} recipe(s) + village "
                f"{check.village}")
