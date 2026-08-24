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
from dataclasses import dataclass, field
from typing import Optional

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
_CATEGORY_WORDS = frozenset({
    "spices", "wares", "jewelry", "fabrics", "food", "textile", "metal", "crafts",
    "sundries", "liquor", "luxuries", "artwork", "livestock", "medicine", "weapons",
    "trade list", "closeout", "barter", "explore", "base", "village info", "icon",
    "view by min. exchange unit", "market", "current location", "source",
})


def _cy(e) -> int:
    return (e.y1 + e.y2) // 2


def _labelled(elements) -> list:
    return [e for e in elements or []
            if getattr(e, "element_type", "") in ("text", "button")
            and (getattr(e, "label", "") or "").strip()]


def _pin_icons(elements) -> list:
    return [e for e in elements or [] if getattr(e, "element_type", "") == "icon"]


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
    left = max(q.x2 for q in qtys) - 20
    out = []
    for e in _labelled(elements):
        lab = (e.label or "").strip()
        if e.x1 < left or "." in lab or re.fullmatch(r"[\d,]+", lab):
            continue
        if lab.lower() in _CATEGORY_WORDS:
            continue
        out.append(e)
    return out


def _name_for_qty(qty, names):
    """The name belonging to a quantity badge: the nearest name that sits ABOVE the
    badge's centre AND immediately to its right.

    'Above' disambiguates vertically — the badge is in the icon's bottom-right corner, so
    its centre can fall nearer the NEXT row's name than its own. The horizontal bound
    stops a floating overlay claiming the row: observed gaps between a badge and its own
    name are under 20px, while the amity tooltip that once became a material sat 406px
    away (live 2026-08-21)."""
    centre = _cy(qty)
    cands = [n for n in names
             if n.y1 <= centre and centre - n.y1 <= _ROW_SPAN
             and -_NAME_OVERLAP <= n.x1 - qty.x2 <= _NAME_GAP]
    return max(cands, key=lambda n: n.y1) if cands else None


def parse_trade_list(elements) -> list[VillageTrade]:
    """Parse ONE screen of the Village Info → Barter → Trade List into VillageTrade rows.

    Rows whose qty tile is flush-left start a new barter GOOD; indented rows are MATERIALS
    of the most recent good.  Callers accumulate across scrolls via `merge_trade_screens`.
    """
    rows = _qty_elements(elements)
    names = _row_names(elements, rows)
    if not rows or not names:
        return []
    icons = _pin_icons(elements)

    base = min(e.x1 for e in rows)
    any_pin = any(_row_pin(t, icons) for t in rows)
    out: list[VillageTrade] = []
    cur: Optional[VillageTrade] = None
    for tile in rows:
        name_el = _name_for_qty(tile, names)
        name = (name_el.label or "").strip() if name_el is not None else None
        qty = int(tile.label.replace(",", ""))
        if not name:
            # A row we cannot name. If it carries NO pin it is a GOOD, and it must still
            # end the previous good's material list — otherwise the next good's materials
            # are swept into the previous one (live 2026-08-21: Glass Bead and Candle
            # became Box of Nutmeg materials, which put Tripoli and Amsterdam in the
            # gather route). An unnamed MATERIAL row is simply dropped.
            if _row_pin(tile, icons) is None and (not any_pin or True):
                cur = None
            continue
        # MATERIAL iff the row carries a location pin.  Prefer that over the indent: on a
        # continuation screen every row is a material, so the indent baseline is itself a
        # material and they would all read as goods.  Fall back to the indent only when no
        # pins were detected at all.
        pin = _row_pin(tile, icons) or (_row_pin(name_el, icons) if name_el else None)
        is_material = bool(pin) if any_pin else (tile.x1 - base > _INDENT_PX)
        if not is_material:
            cur = VillageTrade(good=name, obtain=qty)
            out.append(cur)
        else:
            if cur is None:
                # Continuation from the previous screen — caller attributes it via merge.
                cur = VillageTrade(good="", obtain=0)
                out.append(cur)
            cur.materials[name] = qty
    return out


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
    out = {}
    for tile in rows:
        name_el = _name_for_qty(tile, names)
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

_PANEL_X_MIN = 1700                     # Village Info panel occupies the right ~700px
_SCROLL_X, _SCROLL_Y, _SCROLL_DY = 2080, 880, -380   # in-panel scroll (memory: project_remote_village_…)
_MAX_SCROLLS = 8


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
        """Daily barter rounds still available (Base tab 'Daily Barter Progress N/7')."""
        if self.barters_used is None or self.barters_total is None:
            return None
        return max(0, self.barters_total - self.barters_used)

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
    from loguru import logger
    from actions import ui
    from actions.sail_actions import open_world_map, pan_to_village, _find_button, exit_to_overworld
    from actions.adb_actions import tap
    from capture.adb_capture import capture_screen
    from vision.omniparser import parse_fast_cached

    check = VillageCheck(village=village)

    if not open_world_map():
        check.reason = "could not open the world map"
        return check
    pos = pan_to_village(village)
    if not pos:
        check.reason = f"village {village!r} not found on the world map"
        exit_to_overworld(timeout=60.0)
        return check

    tap(*pos)                                   # position came from pan_to_village's own read
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
                f"{check.amity_points} rounds={check.barters_used}/{check.barters_total}")

    # ── Barter tab: scroll-accumulate the Trade List ─────────────────────────
    if not ui.tap_text(frame, "barter", x_min=_PANEL_X_MIN, dwell="dialog",
                       why="Village Info → Barter tab"):
        check.reason = "Barter tab not found after reading the Base tab"
        exit_to_overworld(timeout=60.0)
        return check

    known = _kb_sources()
    screens, last_sig = [], None
    for i in range(max_scrolls):
        frame = capture_screen()
        elements = parse_fast_cached(frame)
        trades = parse_trade_list(elements)
        sig = screen_signature(trades)
        if sig and sig == last_sig:
            logger.info(f"[village_check] trade list bottom reached after {i} scroll(s)")
            break
        last_sig = sig
        screens.append(trades)
        if learn_sources:
            wanted = _materials_needing_sources(screens, trades, good, known, check.sources)
            if wanted:
                check.sources.update(_learn_sources_on_screen(elements, wanted))
        # Stop as soon as the good we came for is fully read (user 2026-08-21: "we only
        # need to know what is needed for boxes of nutmeg"). Scrolling on is not just
        # wasted taps — it is how another good's rows got swept into this one when their
        # own name went undetected.
        if good and _target_complete(merge_trade_screens(screens), good, village):
            logger.info(f"[village_check] {good!r} fully read after {i + 1} screen(s) — "
                        "not scrolling the rest of the list")
            break
        ui.scroll(_SCROLL_X, _SCROLL_Y, _SCROLL_DY, why=f"trade list, screen {i + 1}")

    check.trades = merge_trade_screens(screens)
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
    known_inputs = known.inputs_for(village) if known is not None else None
    if known_inputs:
        have = {_norm_name(m) for m in target.materials}
        return all(_norm_name(i.material) in have for i in known_inputs)
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
        recipe.village_inputs[_slug(check.village)] = [
            RecipeInput(material=m, ratio=int(q),
                        source_ports=list(check.sources.get(m)
                                          or (prior[_norm_name(m)].source_ports
                                              if _norm_name(m) in prior else [])))
            for m, q in trade.materials.items()]
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
