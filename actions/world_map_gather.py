# actions/world_map_gather.py
#
# World map trade info gathering.
#
# Scrolls the world map port list, taps each port that is not yet in the KB,
# and reads the Trade tab (Market goods + Preferences modifiers) from the
# City Info panel on the right side of the screen.
#
# Usage:
#   python -m actions.world_map_gather              # scan all new ports
#   python -m actions.world_map_gather --max 20     # stop after 20 ports
#   python -m actions.world_map_gather --port London # single port (panel open)

from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from utils.fuzzy import token_sim


# ── Trade category names ───────────────────────────────────────────────────────
# Used to split the Preferences list into category-level vs per-good modifiers.
# Categories always appear at the top of the Preferences list.

TRADE_CATEGORIES: frozenset[str] = frozenset({
    "foods", "seasonings", "luxuries", "sundries", "medicines", "textiles",
    "wares", "fabrics", "livestock", "weapons", "firearms", "dyes",
    "crafts", "artworks", "metals", "ores", "spices", "perfumes",
    "liquors", "jewelries",
})

# ── Screen regions (2400×1080 landscape) ──────────────────────────────────────
# City Info panel — right portion of the world map screen.
# _PANEL_LEFT is set conservatively to exclude the world map toolbar tabs
# (Port/Explore/Route/Trade) which appear at x~990–1530, y~50.

_PANEL_LEFT   = 1560
_PANEL_RIGHT  = 2400
_PANEL_TOP    = 90      # below the toolbar row (y~50)
_PANEL_BOTTOM = 1080
_PANEL_CX     = (_PANEL_LEFT + _PANEL_RIGHT) // 2

# Content area — below both the main-tabs row (y~268) and sub-tabs row (y~336).
# _CONTENT_LEFT is slightly left of _PANEL_LEFT to capture the first character of
# goods names that start near the panel edge (e.g. "B" in "Bremen").
# _CONTENT_BOTTOM excludes the bottom ~60 px where world-map background bleeds in.
_CONTENT_LEFT   = 1540
_CONTENT_TOP    = 380    # safely below sub-tabs; tune if layout differs
_CONTENT_BOTTOM = 1020

# Port list (left side of world map)
_LIST_CROP       = (0, 150, 650, 1080)
_LIST_SCROLL_X   = 300
_LIST_SCROLL_Y1  = 430   # swipe from (scroll list down = finger moves up)
_LIST_SCROLL_Y2  = 230
_MAX_LIST_SCROLLS = 35

# UI chrome labels to ignore when parsing content
_PANEL_CHROME: frozenset[str] = frozenset({
    "base", "trade", "facility", "invest",
    "market", "cargo", "preferences", "preference",
    "city info", "city", "info",
})


# ── KB helpers ─────────────────────────────────────────────────────────────────

_KB_DIR = Path(__file__).parent.parent / "memory" / "knowledge" / "ports"

# OCR noise that looks like port names but never is
_INVALID_PORT_NAMES: frozenset[str] = frozenset({
    "unknown port", "unknown", "search", "world map",
    "port", "explore", "route", "trade", "city info",
})


def _port_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower().strip()).strip("_")


def _kb_path(port_name: str) -> Path:
    return _KB_DIR / f"{_port_slug(port_name)}__trade_info.json"


def _is_valid_port_name(text: str) -> bool:
    """
    Return True if *text* looks like a real port name.

    Rejects:
    - Fewer than 4 characters  ("Or", "Fri")
    - Does not start with a letter
    - Fewer than 80 % alphabetic/space/hyphen characters  ("24,2026", "Fri;")
    - Known OCR noise phrases
    """
    t = text.strip()
    if len(t) < 4 or not t[0].isalpha():
        return False
    alpha = sum(1 for c in t if c.isalpha() or c in " -'.")
    if alpha / len(t) < 0.80:
        return False
    if t.lower() in _INVALID_PORT_NAMES:
        return False
    return True


def _fuzzy_trade_path(port_name: str, threshold: float = 0.85) -> Optional[Path]:
    """
    Return the trade-info KB path for *port_name*, using fuzzy slug matching
    to handle OCR variants of the same port (e.g. "Isurabaya" → "surabaya").

    Returns the exact path (whether it exists or not) if no fuzzy match is
    found, so callers can use it for writing.
    """
    exact = _kb_path(port_name)
    if exact.exists():
        return exact
    if not _KB_DIR.exists():
        return exact   # nothing to compare against

    new_slug = _port_slug(port_name)
    best_path: Optional[Path] = None
    best_sim = 0.0
    for p in _KB_DIR.glob("*__trade_info.json"):
        existing_slug = p.stem[: -len("__trade_info")]
        sim = token_sim(existing_slug.replace("_", " "),
                        new_slug.replace("_", " "))
        if sim > best_sim and sim >= threshold:
            best_sim = sim
            best_path = p

    if best_path:
        logger.info(
            f"  Fuzzy KB match: {port_name!r} → {best_path.stem!r} (sim={best_sim:.2f})"
        )
        return best_path
    return exact


def has_trade_info(port_name: str) -> bool:
    return _fuzzy_trade_path(port_name).exists()


def save_trade_info(port_name: str, data: dict) -> None:
    path = _fuzzy_trade_path(port_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Preserve the canonical name from the first save; record this variant too
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            pass
    canonical = existing.get("port", port_name)
    variants: list[str] = existing.get("name_variants", [])
    if port_name != canonical and port_name not in variants:
        variants.append(port_name)
    record = {
        **data,
        "port": canonical,
        "name_variants": variants,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    logger.info(f"  Saved → {path.name}")


def load_trade_info(port_name: str) -> Optional[dict]:
    path = _fuzzy_trade_path(port_name)
    return json.loads(path.read_text()) if path.exists() else None


# ── Panel OCR + tap ────────────────────────────────────────────────────────────

def _ocr_panel() -> list[tuple[str, float, int, int]]:
    """
    OCR the City Info panel region.
    Returns [(text, conf, abs_x, abs_y), …] in full-frame coordinates.
    """
    from capture.adb_capture import capture_screen
    from actions.sail_actions import _ocr_frame

    frame = capture_screen()
    crop = frame.crop((_PANEL_LEFT, _PANEL_TOP, _PANEL_RIGHT, _PANEL_BOTTOM))
    return [
        (text, conf, cx + _PANEL_LEFT, cy + _PANEL_TOP)
        for text, conf, cx, cy in _ocr_frame(crop)
    ]


def _tap_in_panel(target: str, timeout: float = 8.0) -> bool:
    """
    Find `target` text in the City Info panel and tap it.

    Uses a human-scale delay (1.5–3 s) after each tap — panel tab navigation
    on the world map should not look like rapid automated clicking.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        best_pos: Optional[tuple[int, int]] = None
        best_sim = 0.0
        for text, conf, cx, cy in _ocr_panel():
            sim = token_sim(text, target)
            if sim > best_sim and sim >= 0.80:
                best_sim = sim
                best_pos = (cx, cy)
        if best_pos:
            logger.info(f"  Tapping '{target}' @ {best_pos} (sim={best_sim:.2f})")
            from actions.adb_actions import tap_fast
            # Offset from the detected centre — humans don't tap exactly on text
            jx = random.randint(-6, 6)
            jy = random.randint(-4, 4)
            tap_fast(best_pos[0] + jx, best_pos[1] + jy,
                     delay_ms=random.randint(1500, 3000))
            return True
        time.sleep(1.0)

    logger.warning(f"  '{target}' not found in panel after {timeout:.0f}s")
    return False


# ── Content reading ────────────────────────────────────────────────────────────

def _scroll_content_down() -> None:
    """
    Scroll panel content to reveal items lower in the list.

    The swipe must start WITHIN the scrollable list — if the touch-down lands
    below the last item (empty space), the container never captures the gesture
    and touch-up fires as a tap on whatever item the swipe passes over.

    Starting at _CONTENT_TOP + 180 (~y=560) puts the touch-down on roughly
    the 4th visible item (assuming ~45 px row height), which is safely inside
    any list with ≥ 3 rows.  The short swipe (~140 px) is slow enough that
    Android classifies it as a scroll, not a tap.
    """
    from actions.adb_actions import swipe_fast
    # Randomise start/end coordinates so the gesture is never identical
    x1 = _PANEL_CX + random.randint(-25, 25)
    x2 = x1 + random.randint(-8, 8)           # slight finger drift during swipe
    y1 = _CONTENT_TOP + random.randint(155, 205)
    y2 = _CONTENT_TOP + random.randint(25, 60)
    swipe_fast(x1, y1, x2, y2,
               duration_ms=random.randint(500, 750), settle_ms=1200)


def _ocr_rows(crop) -> list[str]:
    """
    OCR *crop* and return one string per logical row.

    OCR often splits a market row into separate bounding boxes for each column
    (e.g. "Hamburger", "101%", "1,296").  This function groups tokens that are
    within 10 px of each other vertically and joins them left-to-right, giving
    a single string per row: "Hamburger 101% 1,296".
    """
    from actions.sail_actions import _ocr_frame
    tokens = [
        (text.strip(), cx, cy)
        for text, conf, cx, cy in _ocr_frame(crop)
        if len(text.strip()) >= 2
    ]
    if not tokens:
        return []
    tokens.sort(key=lambda t: (t[2], t[1]))   # sort by y then x

    rows: list[list[tuple[str, int, int]]] = []
    cur: list[tuple[str, int, int]] = [tokens[0]]
    for tok in tokens[1:]:
        if abs(tok[2] - cur[0][2]) <= 10:
            cur.append(tok)
        else:
            rows.append(cur)
            cur = [tok]
    rows.append(cur)
    return [" ".join(t[0] for t in sorted(r, key=lambda t: t[1])) for r in rows]


def _collect_content(max_scrolls: int = 12) -> list[str]:
    """
    Collect all logical rows from the panel content area, scrolling until stable.

    Returns deduplicated rows in order first seen (top → bottom).  Each row is a
    single string reconstructed from OCR tokens on the same horizontal line.
    """
    from capture.adb_capture import capture_screen

    accumulated: list[str] = []
    seen_lower: set[str] = set()
    prev_row_set: frozenset = frozenset()

    for i in range(max_scrolls + 1):
        frame = capture_screen()
        crop  = frame.crop((_CONTENT_LEFT, _CONTENT_TOP, _PANEL_RIGHT, _CONTENT_BOTTOM))
        rows  = _ocr_rows(crop)

        cur_row_set = frozenset(r.lower() for r in rows)
        for row in rows:
            rl = row.lower()
            if rl not in seen_lower:
                seen_lower.add(rl)
                accumulated.append(row)

        if cur_row_set == prev_row_set and i > 0:
            logger.info(f"    Content stable after {i} scroll(s) — {len(accumulated)} rows")
            break
        prev_row_set = cur_row_set

        if i < max_scrolls:
            _scroll_content_down()

    return accumulated


# ── Parsers ────────────────────────────────────────────────────────────────────

def _parse_market_goods(lines: list[str]) -> list[str]:
    """
    Extract good names from Market sub-tab rows.

    Each row is a joined string from _collect_content, e.g.:
      "Hamburger 101% 1,296"   → "Hamburger"
      "Cowhide 288"            → "Cowhide"
      "Silk ???"               → "Silk"
      "Wool"                   → "Wool"

    Noise rows that are filtered out:
      "2350/4,066"             — cargo capacity (number/number)
      "101%"                   — price-only row (no good name)
      very long strings        — world-map background bleed
    """
    goods: list[str] = []
    for raw in lines:
        t = raw.strip()
        if t.lower() in _PANEL_CHROME or len(t) < 2:
            continue
        # Skip cargo capacity row  ("Cargo 2350/4,066" or bare "2350/4,066")
        if re.search(r'\d[\d,]*/[\d,]', t):
            continue
        # Skip pure numeric/percentage rows (price column without a name)
        if re.match(r'^[\d,\s%\.\?]+$', t):
            continue
        # Skip very long lines (coordinates / background map text)
        if len(t) > 50:
            continue
        # Strip trailing price index ("%") and everything after
        name = re.sub(r'\s+\d+\s*%.*$', '', t).strip()
        # Strip trailing "???" or bare numbers (e.g. "Cowhide 288")
        name = re.sub(r'\s+[\?\d][\d,\?]*$', '', name).strip()
        if len(name) >= 2 and not re.match(r'^[\d,\s%\.]+$', name):
            goods.append(name)
    return goods


def _parse_modifier(text: str) -> Optional[int]:
    """Parse '+15' from '+15%' or '-100%'. Returns None if not found."""
    m = re.search(r'([+-]?\d+)\s*%', text)
    return int(m.group(1)) if m else None


def _parse_preferences(lines: list[str]) -> dict:
    """
    Parse Preferences sub-tab into category modifiers and per-good modifiers.

    Lines look like:
        Metal  +15%          ← category (in TRADE_CATEGORIES)
        Liquor -100%         ← category
        Tomato +50%          ← individual good
        Coffee +30%          ← individual good

    Returns {"categories": [...], "goods": [...]} where each entry is
    {"name": str, "modifier": int}.
    """
    categories: list[dict] = []
    goods_list: list[dict] = []

    for raw in lines:
        t = raw.strip()
        if t.lower() in _PANEL_CHROME or len(t) < 3:
            continue

        modifier = _parse_modifier(t)
        if modifier is None:
            continue  # line has no modifier → noise or header

        name = re.sub(r'\s*[+-]?\d+\s*%.*$', '', t).strip()
        if len(name) < 2:
            continue

        entry = {"name": name, "modifier": modifier}
        if name.lower() in TRADE_CATEGORIES:
            categories.append(entry)
        else:
            goods_list.append(entry)

    return {"categories": categories, "goods": goods_list}


# ── Per-port gather ────────────────────────────────────────────────────────────

def gather_port_trade_info(port_name: str) -> Optional[dict]:
    """
    With the City Info panel already showing for port_name, navigate to
    Trade → Market and Trade → Preferences and read both sub-tabs.

    Always navigates to each tab/sub-tab explicitly — the panel remembers
    the last active tab, so we must not assume what's currently shown.

    Returns a dict ready for save_trade_info(), or None on failure.
    """
    logger.info(f"Gathering trade info for {port_name!r}…")

    # ── Market ────────────────────────────────────────────────────────────────
    if not _tap_in_panel("Trade"):
        logger.warning(f"  Trade tab not found — skipping {port_name!r}")
        return None

    if not _tap_in_panel("Market"):
        logger.warning(f"  Market sub-tab not found — skipping {port_name!r}")
        return None

    market_lines = _collect_content()
    market_goods = _parse_market_goods(market_lines)
    logger.info(f"  Market: {len(market_goods)} goods  {market_goods[:4]}")

    # ── Preferences ───────────────────────────────────────────────────────────
    if not _tap_in_panel("Trade"):   # re-tap Trade to ensure sub-tabs are visible
        logger.warning(f"  Trade tab lost after Market read — skipping Preferences")
        return {"market_goods": market_goods,
                "preferences": {"categories": [], "goods": []}}

    if not _tap_in_panel("Preferences"):
        logger.warning(f"  Preferences sub-tab not found — skipping {port_name!r}")
        return {"market_goods": market_goods,
                "preferences": {"categories": [], "goods": []}}

    pref_lines = _collect_content()
    preferences = _parse_preferences(pref_lines)
    logger.info(f"  Preferences: {len(preferences['categories'])} categories, "
                f"{len(preferences['goods'])} goods")

    return {
        "market_goods": market_goods,
        "preferences":  preferences,
    }


# ── Full world map scan ────────────────────────────────────────────────────────

def scan_world_map_trade_info(
    max_ports: Optional[int] = None,
    skip_existing: bool = True,
) -> list[str]:
    """
    Scroll the world map port list and gather Trade info for every port
    not yet in the KB.

    Assumes the world map is already open with the port list visible on the left.

    max_ports:     stop after gathering this many new ports (None = all).
    skip_existing: if True, skip ports that already have a trade_info KB entry.

    Returns list of port names successfully gathered in this run.
    """
    from capture.adb_capture import capture_screen
    from actions.sail_actions import _ocr_frame
    from actions.adb_actions import tap, swipe_fast

    gathered: list[str] = []
    prev_labels: frozenset = frozenset()

    logger.info("=" * 55)
    logger.info("World map trade info scan starting…")
    if max_ports:
        logger.info(f"  Will stop after {max_ports} new ports")
    logger.info("=" * 55)

    for scroll_i in range(_MAX_LIST_SCROLLS + 1):
        frame = capture_screen()
        crop = frame.crop(_LIST_CROP)

        # Collect all visible port entries on this page
        port_entries: list[tuple[str, int, int]] = []
        cur_label_set: set[str] = set()

        for text, conf, cx, cy in _ocr_frame(crop):
            t = text.strip()
            if cx < 50 or conf < 0.40:
                continue
            if not _is_valid_port_name(t):
                continue
            cur_label_set.add(t.lower())
            port_entries.append((t, cx + _LIST_CROP[0], cy + _LIST_CROP[1]))

        cur_labels = frozenset(cur_label_set)

        if cur_labels == prev_labels and scroll_i > 0:
            logger.info(f"Port list end reached after {scroll_i} scroll(s)")
            break

        prev_labels = cur_labels
        logger.info(f"Scroll {scroll_i}: {len(port_entries)} ports visible")

        # Process each visible port on this page
        for port_name, px, py in port_entries:
            if skip_existing and has_trade_info(port_name):
                logger.debug(f"  Skipping {port_name!r} (already in KB)")
                continue

            logger.info(f"Opening City Info for {port_name!r} @ ({px},{py})")
            tap(px, py)
            # tap() already includes a 5–10 s human delay; add a small extra
            # settle time for the panel to load before OCR starts.
            time.sleep(random.uniform(1.0, 2.5))

            data = gather_port_trade_info(port_name)
            if data:
                save_trade_info(port_name, data)
                gathered.append(port_name)
                logger.info(f"  [{len(gathered)}] ✓ {port_name!r}")
            else:
                logger.warning(f"  [!] Failed to gather {port_name!r}")

            if max_ports and len(gathered) >= max_ports:
                logger.info(f"Reached max_ports={max_ports} — stopping")
                return gathered

        # Scroll port list down to reveal next page
        if scroll_i < _MAX_LIST_SCROLLS:
            sx = _LIST_SCROLL_X + random.randint(-20, 20)
            swipe_fast(sx, _LIST_SCROLL_Y1 + random.randint(-15, 15),
                       sx + random.randint(-5, 5), _LIST_SCROLL_Y2 + random.randint(-15, 15),
                       duration_ms=random.randint(260, 380), settle_ms=800)

    logger.info(f"Scan complete — gathered {len(gathered)} ports")
    return gathered


# ── Standalone ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from memory.logger import setup_logging
    setup_logging()

    args = sys.argv[1:]
    max_p: Optional[int] = None

    if "--max" in args:
        idx = args.index("--max")
        max_p = int(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    if "--port" in args:
        idx = args.index("--port")
        port_name = " ".join(args[idx + 1:])
        data = gather_port_trade_info(port_name)
        if data:
            save_trade_info(port_name, data)
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print(f"Failed to gather trade info for {port_name!r}")
        sys.exit(0 if data else 1)

    gathered = scan_world_map_trade_info(max_ports=max_p)
    print(f"\nGathered {len(gathered)} port(s): {', '.join(gathered)}")
