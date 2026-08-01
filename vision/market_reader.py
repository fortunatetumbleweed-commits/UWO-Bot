# vision/market_reader.py
# Market screen price reader — uses EasyOCR directly.
#
# The market screen shows trade goods in a 3-column tile grid.
# Each tile contains: item image, name, category, price index %, price, quantity.
#
# Strategy:
#   1. Crop the center content area (strip left menu + right cargo panel)
#   2. Run EasyOCR to get (text, bbox, conf) for every token
#   3. Map each token to a tile cell using the regular grid dimensions
#   4. Parse each tile: classify tokens as name / price / index_pct / sold_out / noise
#   5. Return a MarketGood per tile
#
# Usage:
#   python -m vision.market_reader          # test on current screen (market open)
#   python -m vision.market_reader --save   # test + save to market KB

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from loguru import logger
from PIL import Image

from memory.market_kb import MarketGood, MarketSnapshot


# ── Grid geometry (2400 × 1080 landscape) ─────────────────────────────────────
# Calibrated from pixel brightness scan of a real market screenshot.
# Left sidebar ends at ~460; right cargo panel starts at ~1810.
# Row separators (dark dips) measured at y≈330 and y≈605 → pitch≈275.
# Grid top at ~55 (just below "Trade Goods on Sale" header bar).

_GRID_LEFT    = 460    # x start of tile grid (left of leftmost tile)
_GRID_TOP     = 215    # y start of tile grid (below "Trade Goods on Sale" header)
_TILE_W       = 450    # tile pitch horizontally (content + separator)
_TILE_H       = 230    # tile pitch vertically — measured: Whisky name y=221,
                       # Wheat name y=451, pitch=230px
_TILE_COLS    = 3
_TILE_ROWS    = 3      # 3 full rows visible per page
_IMG_ZONE_W   = 160    # item thumbnail width inside tile — skip for name OCR
_BOTTOM_ZONE  = 80     # tile-relative y threshold: above → name/category zone;
                       # at/below → price/qty zone (qty at rel_y≈83, price at ≈169)

# ── Vocabulary ─────────────────────────────────────────────────────────────────

_CATEGORIES = frozenset([
    "liquor", "wares", "fabrics", "metals", "spices", "food", "foods",
    "dyes", "crafts", "artworks", "artwork", "craft", "metal",
    "livestock", "weapons", "firearms", "medicines", "medicine",
    "textile", "textiles", "sundries", "sundry", "ores", "ore",
    "perfumes", "perfume", "jewelry", "jewelries", "seasonings",
    "seasoning", "luxuries", "luxury", "fabric",
])

_BADGES = frozenset([
    "specialties", "specialty", "on sale", "onsale", "recommended",
    "favorites",
])

_SKIP_WORDS = frozenset([
    "purchase", "sell", "trade", "goods", "sale", "load", "bulk",
    "put", "in", "apply", "ratio", "supplies", "overall", "info",
    "tax", "neg", "nego", "chance", "success", "rate", "cargo",
    "all", "west", "east", "special", "improved", "language", "effect",
    "discount", "the", "of", "and",
    # extra UI noise
    "owned", "points", "on", "there", "are", "no", "items", "to",
    "by", "with", "for", "from", "at", "a", "an", "set",
])

_TREND_MAP: dict[str, str] = {
    "soaring":    "soaring",
    "plunging":   "plunging",
    "rising":     "rising",
    "falling":    "falling",
    "stable":     "stable",
    "trending":   "rising",
    "booming":    "soaring",
    "overflowing": "plunging",
}


# ── Tile parsing ───────────────────────────────────────────────────────────────

def _classify_token(text: str, tile_rel_y: int) -> str:
    """
    Classify a token into one of: index_pct | price | timer | trend |
    category | badge | skip | name.
    tile_rel_y is the token's y position relative to the top of its tile.
    """
    t = text.strip()
    if not t:
        return "skip"
    t_low = t.lower()

    # Sold-out restock timer  e.g. "00:21:04"
    if re.match(r'^\d{2}:\d{2}:\d{2}$', t):
        return "timer"

    # Price index %  e.g. "98%", "102 %"
    if re.match(r'^\d+\s*%$', t):
        return "index_pct"

    # Strip trailing gain/loss suffix "(+150)" or "(-93)" before number parse.
    # Sell tab shows: price(gain/loss) e.g. "7(-93)" or "1,234(+50)"
    t_num = re.sub(r'\([-+]?\d+\)\s*$', '', t).strip()

    # Pure number (with optional leading +/-/▼/▲ and internal commas/dots).
    # Also strip non-digit prefix/suffix — the sell tab prefixes prices with a
    # coin icon that OCR sometimes renders as a letter (e.g. "⊙3087", "G3087").
    cleaned = re.sub(r'[,\.\+\-▼▲\s]', '', t_num)
    num_only = re.sub(r'^\D+|\D+$', '', cleaned)   # strip leading/trailing non-digits
    if num_only.isdigit():
        v = int(num_only)
        # Must be in the price zone of the tile (bottom portion).
        # Threshold ≥ 5 to skip trivial noise; sell prices can be small integers.
        if v >= 5 and tile_rel_y >= _BOTTOM_ZONE:
            return "price"
        return "skip"

    # Trend words
    if t_low in _TREND_MAP:
        return "trend"

    # Known category label
    if t_low in _CATEGORIES:
        return "category"

    # Badge/banner
    if t_low in _BADGES:
        return "badge"

    # Generic skip words
    if t_low in _SKIP_WORDS:
        return "skip"

    # Very short tokens and mostly-non-alpha noise
    if len(t) < 2:
        return "skip"
    alpha_ratio = sum(c.isalpha() for c in t) / len(t)
    if alpha_ratio < 0.45:
        return "skip"

    # Sentence-level UI noise: ends with punctuation, or too many words
    if t.endswith(('.', '!', '?')):
        return "skip"
    words = t_low.split()
    if len(words) >= 4:
        return "skip"
    # Multi-word phrase where every word is a known skip word (or very short)
    if len(words) > 1 and all(w in _SKIP_WORDS or len(w) <= 2 for w in words):
        return "skip"

    return "name"


def _parse_tile(tokens: list[tuple[str, int, int]], tab: str,
                tile_cx: int, tile_cy: int) -> Optional[MarketGood]:
    """
    tokens: list of (text, abs_cx, abs_cy) for this tile.
    Returns a MarketGood or None if the tile looks empty / noise.
    """
    tile_top  = tile_cy - _TILE_H // 2
    tile_left = tile_cx - _TILE_W // 2
    text_x0   = tile_left + _IMG_ZONE_W   # x boundary: image zone | text zone

    index_pct:     Optional[int] = None
    price:         Optional[int] = None
    available_qty: Optional[int] = None
    trend:     str = "unknown"
    sold_out:  bool = False
    category:  str = ""
    name_tokens: list[tuple[str, int, int]] = []  # (text, abs_cx, abs_cy)

    for text, cx, cy in tokens:
        rel_y = cy - tile_top
        kind  = _classify_token(text, rel_y)

        if kind == "timer":
            sold_out = True
        elif kind == "index_pct":
            v = int(re.sub(r'[^\d]', '', text))
            if index_pct is None or abs(v - 100) < abs(index_pct - 100):
                index_pct = v   # keep value closest to 100 if multiple
        elif kind == "price":
            t_num = re.sub(r'\([-+]?\d+\)\s*$', '', text).strip()
            v = int(re.sub(r'[,\.\+\-▼▲\s]', '', t_num))
            if cx <= text_x0:
                # Number is inside the image zone → available quantity in stock
                if available_qty is None or v > available_qty:
                    available_qty = v
            else:
                # Number is in the text zone → actual price
                if price is None or v > price:
                    price = v
        elif kind == "trend":
            trend = _TREND_MAP[text.strip().lower()]
        elif kind == "category":
            category = text.strip().title()
        elif kind == "name":
            # Only accept name tokens to the right of the item image thumbnail
            if cx > text_x0:
                name_tokens.append((text.strip(), cx, cy))

    if not name_tokens:
        return None

    # Join name tokens: group by y proximity (≤ 28 px), join each line L→R
    name_tokens.sort(key=lambda t: (t[2], t[1]))
    lines: list[list[str]] = []
    line_ys: list[int] = []
    for text, cx, cy in name_tokens:
        placed = False
        for i, ly in enumerate(line_ys):
            if abs(cy - ly) <= 28:
                lines[i].append(text)
                line_ys[i] = (line_ys[i] + cy) // 2
                placed = True
                break
        if not placed:
            lines.append([text])
            line_ys.append(cy)

    name = " ".join(lines[0]) if lines else ""
    if len(name) < 3:
        return None

    # Apply learned OCR corrections (built up from Claude fallback responses)
    from training.collector import apply_name_correction
    name = apply_name_correction(name)

    good = MarketGood(
        name=name,
        category=category,
        index_pct=index_pct,
        trend=trend,
        sold_out=sold_out,
        available_qty=available_qty,
        tap_x=tile_cx,
        tap_y=tile_cy,
    )
    if tab == "purchase":
        good.buy_price = None if sold_out else price
    else:
        good.sell_price = price
    return good


# ── Core reader ────────────────────────────────────────────────────────────────

def read_market_page_ocr(
    frame: Image.Image,
    tab: str = "purchase",
    port: str = "unknown",
) -> list[MarketGood]:
    """
    Read one visible page of the market using EasyOCR.
    Returns a list of MarketGood for the visible tile grid.
    """
    from vision.ocr import _get_reader

    # Crop to center content area — skip left sub-menu and right cargo panel
    content = frame.crop((_GRID_LEFT, _GRID_TOP,
                           _GRID_LEFT + _TILE_W * _TILE_COLS,
                           _GRID_TOP  + _TILE_H * _TILE_ROWS))

    raw = _get_reader().readtext(np.array(content), detail=1)

    # Assign every token to a tile cell
    tile_buckets: dict[tuple[int, int], list[tuple[str, int, int]]] = {}

    for (bbox, text, conf) in raw:
        if conf < 0.25 or not text.strip():
            continue
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        # Centre in crop-relative coords
        cx_rel = int(sum(xs) / 4)
        cy_rel = int(sum(ys) / 4)
        # Convert to full-frame coords
        cx = cx_rel + _GRID_LEFT
        cy = cy_rel + _GRID_TOP

        col = cx_rel // _TILE_W
        row = cy_rel // _TILE_H
        if 0 <= col < _TILE_COLS and 0 <= row < _TILE_ROWS:
            tile_buckets.setdefault((col, row), []).append((text, cx, cy))

    goods: list[MarketGood] = []
    for (col, row), tokens in sorted(tile_buckets.items()):
        tile_cx = _GRID_LEFT + col * _TILE_W + _TILE_W // 2
        tile_cy = _GRID_TOP  + row * _TILE_H + _TILE_H // 2
        good = _parse_tile(tokens, tab, tile_cx, tile_cy)
        if good:
            goods.append(good)
            logger.debug(
                f"  [{tab}] {good.name:<28} "
                f"{'SOLD OUT' if good.sold_out else str(good.buy_price or good.sell_price):>8}  "
                f"{str(good.index_pct)+'%' if good.index_pct else '?%':<5}  "
                f"qty={good.available_qty}  {good.trend}"
            )

    # ── Claude fallback for tiles that OCR failed to parse ─────────────────────
    # Any tile with name=None or price=None is re-tried via Claude Vision.
    # The result fixes the good in-place and saves the example as training data.
    goods = _apply_claude_fallback(frame, goods, tab)

    logger.info(f"[{tab}] page: {len(goods)} goods parsed")
    return goods


def _apply_claude_fallback(
    frame: Image.Image,
    goods: list[MarketGood],
    tab: str,
) -> list[MarketGood]:
    """
    For each good that OCR failed on (missing name or price), crop the tile
    and ask Claude.  Updates the good in-place.  No-op if Claude is unavailable.
    """
    from training.claude_fallback import read_market_tile

    for good in goods:
        price = good.buy_price if tab == "purchase" else good.sell_price
        need_name  = good.name is None
        need_price = price is None and not good.sold_out

        if not need_name and not need_price:
            continue

        trigger = "no_name" if need_name else "no_price"
        logger.info(
            f"  [{tab}] Claude fallback for tile @ "
            f"({good.tap_x}, {good.tap_y}): {trigger}"
        )

        # Crop the tile from the frame
        x0 = (good.tap_x or 0) - _TILE_W // 2
        y0 = (good.tap_y or 0) - _TILE_H // 2
        x1, y1 = x0 + _TILE_W, y0 + _TILE_H
        crop = frame.crop((
            max(0, x0), max(0, y0),
            min(frame.width, x1), min(frame.height, y1),
        ))

        result = read_market_tile(
            crop,
            tab=tab,
            local_name=good.name,
            trigger=trigger,
        )
        if result is None:
            continue

        if need_name and result.get("name"):
            good.name = result["name"]
        if good.name and result.get("category") and not good.category:
            good.category = result["category"]
        if need_price and result.get("price") is not None:
            if tab == "purchase":
                good.buy_price = result["price"]
            else:
                good.sell_price = result["price"]
        if result.get("index_pct") is not None and good.index_pct is None:
            good.index_pct = result["index_pct"]

        logger.info(
            f"  [{tab}] Claude fixed: name={good.name!r}  "
            f"price={good.buy_price or good.sell_price}  idx={good.index_pct}%"
        )

    return goods


# ── Alias used by market_actions.py ───────────────────────────────────────────

def read_market_page_claude(
    frame: Image.Image,
    tab: str = "purchase",
    port: str = "unknown",
) -> list[MarketGood]:
    """Compatibility alias — delegates to OCR reader (no API key required)."""
    return read_market_page_ocr(frame, tab=tab, port=port)


# ── Multi-page helpers (unchanged interface) ───────────────────────────────────

def read_market_all_pages(
    capture_fn,
    tab: str = "purchase",
    port: str = "unknown",
    max_scrolls: int = 6,
) -> list[MarketGood]:
    from actions.adb_actions import swipe as adb_swipe
    from config.settings import MARKET_SCROLL_START, MARKET_SCROLL_END

    seen_names: set[str] = set()
    all_goods:  list[MarketGood] = []
    _FULL_PAGE_THRESHOLD = 5

    for scroll_n in range(max_scrolls + 1):
        frame     = capture_fn()
        page_goods = read_market_page_ocr(frame, tab=tab, port=port)

        new_goods = [g for g in page_goods if g.name not in seen_names]
        if not new_goods and scroll_n > 0:
            logger.debug(f"[{tab}] No new goods after scroll #{scroll_n} — done")
            break

        all_goods.extend(new_goods)
        for g in new_goods:
            seen_names.add(g.name)

        logger.debug(f"[{tab}] Page {scroll_n}: +{len(new_goods)} new (total {len(all_goods)})")

        if len(page_goods) < _FULL_PAGE_THRESHOLD:
            logger.debug(f"[{tab}] Partial page ({len(page_goods)} goods) — list end reached")
            break

        if scroll_n < max_scrolls:
            sx, sy_start = MARKET_SCROLL_START
            _,  sy_end   = MARKET_SCROLL_END
            adb_swipe(sx, sy_start, sx, sy_end, duration_ms=400)
            time.sleep(1.5)

    logger.info(f"[{tab}] Total: {len(all_goods)} goods")
    return all_goods


def read_both_tabs(
    capture_fn,
    tap_fn,
    port: str = "unknown",
) -> tuple[list[MarketGood], list[MarketGood]]:
    from config.settings import MARKET_COORDS

    logger.info("Market: reading Purchase tab…")
    tap_fn(*MARKET_COORDS["purchase"])
    time.sleep(1.5)
    purchase = read_market_all_pages(capture_fn, tab="purchase", port=port)

    logger.info("Market: reading Sell tab…")
    tap_fn(*MARKET_COORDS["sell"])
    time.sleep(1.5)
    sell = read_market_all_pages(capture_fn, tab="sell", port=port)

    return purchase, sell


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from memory.logger import setup_logging
    setup_logging()

    save = "--save" in sys.argv

    port = "unknown"
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = sys.argv[idx + 1]
    else:
        from capture.adb_capture import capture_screen as _cap
        from vision.ocr import read_port_name
        _f  = _cap()
        port = read_port_name(_f) or "unknown"

    logger.info(f"Port: {port!r}")

    from capture.adb_capture import capture_screen
    from actions.adb_actions import tap as adb_tap

    purchase, sell = read_both_tabs(capture_screen, adb_tap, port=port)

    logger.info(f"\n── Purchase ({len(purchase)} goods) ──")
    logger.info(f"  {'Name':<30} {'Price':>8}  {'Idx':<5}  {'Qty':>6}")
    for g in sorted(purchase, key=lambda x: x.name):
        idx   = f"{g.index_pct}%" if g.index_pct is not None else "?%"
        price = "SOLD OUT" if g.sold_out else str(g.buy_price)
        qty   = str(g.available_qty) if g.available_qty is not None else "?"
        logger.info(f"  {g.name:<30} {price:>8}  {idx:<5}  {qty:>6}")

    logger.info(f"\n── Sell ({len(sell)} goods) ──")
    logger.info(f"  {'Name':<30} {'Price':>8}  {'Idx':<5}  {'Qty':>6}")
    for g in sorted(sell, key=lambda x: x.name):
        idx   = f"{g.index_pct}%" if g.index_pct is not None else "?%"
        price = "SOLD OUT" if g.sold_out else str(g.sell_price)
        qty   = str(g.available_qty) if g.available_qty is not None else "?"
        logger.info(f"  {g.name:<30} {price:>8}  {idx:<5}  {qty:>6}")

    if save:
        from memory.market_kb import save_snapshot
        snap = MarketSnapshot(
            port=port,
            timestamp=datetime.now(timezone.utc).isoformat(),
            purchase_goods=purchase,
            sell_goods=sell,
        )
        save_snapshot(snap)
        logger.info(f"\nSnapshot saved → memory/knowledge/markets/{port}__market.json")
