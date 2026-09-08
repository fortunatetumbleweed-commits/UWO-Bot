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
from statistics import median
from typing import Optional

import numpy as np
from loguru import logger
from PIL import Image

from memory.market_kb import MarketGood, MarketSnapshot
from utils.digits import SEPARATORS as _SEP


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


# ── OmniParser-based reader (precision-first, no hardcoded grid) ───────────────
# The goods tiles are detected as `button` elements, so we don't assume a fixed
# 3×3 grid at absolute pixels. This adapts to the actual number of goods (6 vs 9)
# and is orientation / notch robust — the fixed-grid reader breaks when the whole
# UI shifts (the live D11 "1-2 garbage items" symptom). Slower than the fixed
# grid, but that's fine: building screens are off the nav hot-path and want
# PRECISION over speed. See memory: perception is mode-dependent (nav=fast,
# buildings/port=precise via OmniParser).

def _parse_tile_from_button(button, text_els, tab: str,
                            label: Optional[str] = None) -> Optional[MarketGood]:
    """Build a MarketGood from an OmniParser tile BUTTON + the text elements
    inside it. Name = the button label (clean); price/index/qty/etc. from the
    inner text via `_classify_token`, with y scaled to the `_TILE_H` reference so
    the zone thresholds hold regardless of the detected tile's actual height."""
    name = re.sub(r"\s+", " ", ((button.label if label is None else label) or "").strip())
    tile_h = max(1, button.y2 - button.y1)
    text_x0 = button.x1 + _IMG_ZONE_W

    index_pct = price = available_qty = profit = owned_qty = None
    sold_out = False
    category = ""
    trend = "unknown"
    # Text in the tile's upper zone, right of the thumbnail — the good's name sits on the TOP
    # line, the category on the one below. Collected by POSITION, not classification, because
    # some goods ARE category words ("Textiles" is both), so a name-only list would lose them.
    top_tokens: list[tuple[str, int, int]] = []

    for e in text_els:
        text = (e.label or "").strip()
        if not text:
            continue
        rel_y = int((e.cy - button.y1) * _TILE_H / tile_h)   # scale to _TILE_H frame
        # SELL-tile tokens, matched by SHAPE before the buy-oriented _classify_token (which drops
        # comma'd "price (profit)" as skip and mistakes the owned-qty badge for a price):
        m = re.match(r"^(\d[\d,.']*)\s*\(\s*([-+]?\d[\d,.']*)\s*\)$", text)
        if m:                                     # "price (profit/unit)" — sell price + profit
            pv = int(m.group(1).translate(_SEP))
            if price is None or pv > price:
                price, profit = pv, int(m.group(2).translate(_SEP))
            continue
        if tab != "purchase" and re.fullmatch(r"\d{1,4}", text) and 80 < rel_y < 150:
            owned_qty = int(text)                 # units of this good in cargo (upper-middle badge)
            continue
        kind = _classify_token(text, rel_y)
        if kind == "timer":
            sold_out = True
        elif kind == "index_pct":
            v = int(re.sub(r"[^\d]", "", text))
            if index_pct is None or abs(v - 100) < abs(index_pct - 100):
                index_pct = v
        elif kind == "price":
            t_num = re.sub(r"\([-+]?\d+\)\s*$", "", text).strip()
            digits = re.sub(r"^\D+|\D+$", "", re.sub(r"[,\.\+\-▼▲\s]", "", t_num))
            if not digits.isdigit():
                continue
            v = int(digits)
            if e.cx <= text_x0:
                if available_qty is None or v > available_qty:
                    available_qty = v
            elif price is None or v > price:
                price = v
        elif kind == "trend":
            trend = _TREND_MAP[text.lower()]
        elif kind == "category":
            category = text.title()
            if e.cx > text_x0:
                top_tokens.append((text, e.cx, e.cy))
        elif kind == "name" and e.cx > text_x0:
            top_tokens.append((text, e.cx, e.cy))

    # OmniParser labels a tile with its most prominent text, which on a SPECIALTY tile is the
    # yellow "Specialties" banner rather than the good. Live 2026-08-22 at Kolkata the Textiles
    # tile came back named "Specialties", so the hold read as 0 Textiles and the bot re-bought
    # 920 units it was already carrying for 235,520 ducats. A banner is never a good's name.
    if name.lower() in _BADGES:
        name = ""
    # Fall back to the tile's own text: the TOP line of the upper zone.
    if len(name) < 3 and top_tokens:
        top_tokens.sort(key=lambda t: (t[2], t[1]))
        y0 = top_tokens[0][2]
        name = " ".join(t[0] for t in top_tokens if abs(t[2] - y0) <= 28).strip()
    if len(name) < 2:
        return None

    from training.collector import apply_name_correction
    name = apply_name_correction(name)

    tile_cx = (button.x1 + button.x2) // 2
    tile_cy = (button.y1 + button.y2) // 2
    good = MarketGood(
        name=name, category=category, index_pct=index_pct, trend=trend,
        sold_out=sold_out, available_qty=available_qty,
        tap_x=tile_cx, tap_y=tile_cy,
    )
    if tab == "purchase":
        good.buy_price = None if sold_out else price
    else:
        good.sell_price = price
        good.profit_per_unit = profit
        good.is_loss = profit is not None and profit < 0
        good.owned_qty = owned_qty
    return good


def _tile_label(cell, elements) -> str:
    """The tile's good name, preferring a detection that is NOT the yellow banner.

    OmniParser often reports a specialty tile twice — once labelled with the good, once with
    the "Specialties" banner painted across it. Live 2026-08-22 at Kolkata the banner detection
    won the grid slot, the Textiles tile was read as 'Specialties', so the hold looked like
    0 Textiles and the bot re-bought the 920 units it was already carrying for 235,520 ducats.
    """
    label = (getattr(cell, "label", "") or "").strip()
    if label.lower() not in _BADGES:
        return label
    for e in elements:
        if e is cell or getattr(e, "element_type", "") != "button":
            continue
        other = (getattr(e, "label", "") or "").strip()
        if not other or other.lower() in _BADGES:
            continue
        ox = min(cell.x2, e.x2) - max(cell.x1, e.x1)
        oy = min(cell.y2, e.y2) - max(cell.y1, e.y1)
        area = (e.x2 - e.x1) * (e.y2 - e.y1)
        if ox > 0 and oy > 0 and area and (ox * oy) / area >= 0.6:
            return other           # same tile, seen twice — take the one naming the good
    return label


# A SOLD-OUT TILE IS GREY. The game desaturates the thumbnail art when a shelf is empty,
# and that is a STATE readable from one frame — unlike watching a tile go grey across a buy,
# which only catches a shelf emptying while you stand there (arrive already-empty, or leave
# the building and come back, and there is no transition left to see).
#
# Measured on the Bordeaux Purchase grid, 2026-08-24 (thumbnail art, saturation):
#     Raisin (SOLD OUT) 0.004    every active tile 0.233 - 0.622
# A ~60x gap, so the threshold is not delicate. Brightness agrees (18.5 vs 43.9-119.8) and is
# kept as a second condition so a legitimately dark-but-colourful tile is not condemned.
_SOLD_OUT_MAX_SAT = 0.10
# The CARD BODY's brightness below which the tile is greyed out. Live tiles measured
# 197.1-203.8 over twelve tiles on two frames; the two sold-out ones read 99.6 and 107.8.
# The boundary sits in ~90 points of empty space on either side.
_SOLD_OUT_MAX_CARD_BRIGHT = 150.0
_SOLD_OUT_MAX_BRIGHT = 35.0


# A CONDITION RIBBON sits in the tile's TOP-LEFT corner, carrying an icon for the condition
# (a guild badge, an hourglass). Measured on the Bordeaux grid 2026-08-24: Hungary Water's
# corner is 62% magenta, every other tile 0% — so this is not a delicate threshold either.
_RIBBON_MIN_MAGENTA = 0.15


# THE SEASON RIBBON sits in the tile's TOP-RIGHT corner, opposite the guild one, carrying a
# flower icon: RED when the good is scarce this season, GREEN when it is abundant, absent when
# it is ordinary. `memory/stock-status-is-a-colour` records the pairing — "top-right seasonal
# stock, top-left guild monopoly".
#
# Measured on the Madeira Purchase grid, frame 158 of
# `data/sessions/trace_barter_cmd_2026-09-06T21-45-01`, over a 60x52 band:
#
#     Sugar Cane   red 0.447  green 0.000     LOW
#     Shea Butter  red 0.000  green 0.469     ABUNDANT
#     four ordinary tiles      0.000/0.000
#
# A 0.45-against-0.00 gap, so this is not a delicate threshold either.
_SEASON_MIN_FRAC = 0.15


def tile_season(frame, cell) -> Optional[str]:
    """"low" | "abundant" | None, from the tile's top-right season ribbon.

    A SOLD-OUT TILE IS NOT A SCARCE SEASON, and the two want opposite things: a blue gem
    restocks an empty shelf and can do nothing about a bad season. They are easy to confuse
    because an empty tile is ALSO reddish — the Sold Out stamp and a red `0` badge — so the
    caller must not ask this of a tile it has already judged sold out. Measured on that same
    grid: Raisin, sold out and with no ribbon at all, reads red 0.138.
    """
    try:
        import numpy as np
        # CROP FIRST, THEN CONVERT. Converting the whole 2400x1080 frame to read a 60x52
        # corner allocates ~62MB per tile and nine tiles per grid — measured as a large
        # slowdown across the functional suite, which reads real grids.
        box = (max(0, cell.x2 - 64), cell.y1, max(0, cell.x2 - 4), cell.y1 + 52)
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        cor = np.asarray(frame.crop(box).convert("RGB")).astype(float)
        if cor.size == 0:
            return None
        R, G, B = cor[..., 0], cor[..., 1], cor[..., 2]
        red = float(((R > 110) & (R - G > 45) & (R - B > 45)).mean())
        green = float(((G > 90) & (G - R > 25) & (G - B > 15)).mean())
        if green >= _SEASON_MIN_FRAC and green > red:
            return "abundant"
        if red >= _SEASON_MIN_FRAC and red > green:
            return "low"
        return None
    except Exception as exc:
        logger.debug(f"[market] season-ribbon check skipped: {exc}")
        return None


def _tile_has_condition_ribbon(frame, cell) -> bool:
    """True when the tile carries a corner ribbon marking it as CONDITIONAL."""
    try:
        import numpy as np
        # Cropped first for the same reason as `tile_season` next door — this one has always
        # converted the whole frame to read a 40x40 corner.
        cor = np.asarray(frame.crop((cell.x1, cell.y1, cell.x1 + 40,
                                     cell.y1 + 40)).convert("RGB")).astype(float)
        if cor.size == 0:
            return False
        R, G, B = cor[..., 0], cor[..., 1], cor[..., 2]
        magenta = ((R > 120) & (B > 120) & (R - G > 40) & (B - G > 40)).mean()
        return float(magenta) >= _RIBBON_MIN_MAGENTA
    except Exception as exc:
        logger.debug(f"[market] condition-ribbon check skipped: {exc}")
        return False


def _tile_looks_sold_out(frame, cell) -> bool:
    """True when the tile's CARD is greyed — the single-frame sold-out signal.

    THE CARD, NOT THE ARTWORK (user, 2026-09-07: "the whole tile is greyed out, that should
    be the most prominent determining factor").

    This used to measure the thumbnail and ask for `sat <= 0.10 and brightness <= 35`. The
    artwork is the GOOD'S OWN PICTURE, so neither number means anything across goods: dark
    brown Ebony reads sat 0.28 / brightness 21 when SOLD OUT, while Rosewood reads 0.45 / 54
    while perfectly in stock. The test failed Ebony on saturation and called a sold-out shelf
    buyable.

    What it cost, live 2026-09-07 at Ambon (frame 131 of trace_barter_cmd_2026-09-07T23-05-09):
    the tile was greyed, stamped `Sold Out`, and its badge read 0, and the buy loop tapped it
    twice, found the cart still empty, concluded "the tile is not taking taps" and failed the
    mission. Claude's own consult on that frame said "Ebony is sold out" three times over.

    The CARD BODY is chrome with a fixed palette — cream when live, grey when dead — and it
    separates cleanly. Measured over two frames and twelve tiles:

        live tiles          197.1 .. 203.8      (spread 6.7, and that includes both GATED
                                                 tiles, which must not read as sold out)
        Palm Oil sold out    99.6
        Ebony sold out      107.8

    ~90 points of clear air either side of the boundary, against 33 points of overlap on the
    artwork. Saturation is dropped: the card body reads 0.09-0.14 whether live or dead, so it
    discriminates nothing here.

    Same rule as everywhere else in this file — judge the chrome, never the artwork
    (`memory/stock-status-is-a-colour`: "never scan the whole tile, the artwork is coloured").
    """
    try:
        import numpy as np
        w, h = cell.x2 - cell.x1, cell.y2 - cell.y1
        if w <= 0 or h <= 0:
            return False
        body = np.asarray(frame.convert("RGB")).astype(float)[
            cell.y1 + int(0.12 * h):cell.y1 + int(0.40 * h),
            cell.x1 + int(0.41 * w):cell.x1 + int(0.92 * w)]
        if body.size == 0:
            return False
        return float(body.mean()) <= _SOLD_OUT_MAX_CARD_BRIGHT
    except Exception as exc:
        logger.debug(f"[market] grey-tile check skipped: {exc}")
        return False


def sell_page_can_have_more_below(frame, elements=None) -> bool:
    """Could this goods grid continue past the bottom of the screen?

    A GRID THAT DOES NOT FILL THE PAGE HAS NOTHING UNDER IT (user, 2026-09-08). The grid
    fills row-major, so a page that is not full IS the end of the list, and scrolling it
    costs a swipe, a capture and a whole dispatcher tick to learn what the picture already
    said. Live 2026-09-08 at Jakarta the Sell page held two tiles — Ebony and Coral, seven
    empty cells — and the clear scrolled anyway before finishing.

    Asks the SHAPE, not the cell count. Measured over the 2026-09-08 run: a full purchase
    shelf detects 8 of its 9 tiles about as often as 9, so `len(cells) == 9` would call a
    full page partial and stop a clear early — the failure this guards against is exactly
    the one that ended a clear four pages deep on 2026-08-26 with the hold still aboard.
    The row and column COUNTS are stable at 3x3 across those same frames, and every Sell
    page in that run measured 1 row x 2 columns.

    Unreadable is not the same as absent: when no grid is detected at all, say True and let
    the caller scroll as it always has.
    """
    from vision.omniparser import parse_fast_cached
    from vision.grid_detector import detect_grid
    try:
        if elements is None:
            elements = parse_fast_cached(frame)
        W, H = frame.width, frame.height
        grid = detect_grid(elements, W, H, zone=(0.17 * W, 0.14 * H, 0.78 * W, 0.92 * H),
                           cell_types=("button",), min_cells=1, size_tol_h=0.45)
    except Exception as exc:                    # noqa: BLE001 — never end a clear on this
        logger.debug(f"[sell] could not measure the grid: {exc}")
        return True
    if grid is None:
        return True
    full = grid.n_rows >= _TILE_ROWS and grid.n_cols >= _TILE_COLS
    if not full:
        logger.info(f"[sell] the grid is {grid.n_rows}x{grid.n_cols}, not "
                    f"{_TILE_ROWS}x{_TILE_COLS} — it ends on this page, nothing to scroll to")
    return full


def read_market_page_omni(
    frame: Image.Image,
    tab: str = "purchase",
    port: str = "unknown",
    elements=None,
    claude_fallback: bool = True,
) -> list[MarketGood]:
    """Read one visible market page from OmniParser-detected tiles.

    No hardcoded grid: the goods tiles are `button` elements, so this adapts to
    the number of goods and is orientation/notch-robust. `elements` may be a
    pre-computed OmniParser list (cache hit); otherwise it is parsed here.
    """
    from vision.omniparser import parse_fast_cached
    if elements is None:
        try:
            elements = parse_fast_cached(frame)
        except Exception as exc:
            logger.debug(f"[market omni] OmniParser unavailable: {exc}")
            return []
    if not elements:
        return []

    from vision.grid_detector import detect_grid
    W, H = frame.width, frame.height
    # goods zone: right of the left sub-menu, left of the cargo panel, below header
    zone = (0.17 * W, 0.14 * H, 0.78 * W, 0.92 * H)
    # SELL pages list only the goods in CARGO — legitimately 1-3 tiles (a single row, or a lone
    # tile after a barter run), which the default min_cells=4 rejects → "no goods grid detected"
    # → sell_goods concluded "nothing to sell" while holding ~605 Box of Nutmeg (live 2026-08-20).
    # Purchase pages keep the stricter minimum (they always show a full grid).
    # A BAZAAR tile detects SHORT — same tile, clipped box, the banner having tripped
    # OmniParser (432x181 against 435x231 neighbours, Bremen 2026-08-24). Width still
    # identifies the column, so only the height tolerance is loosened. Without this the Box
    # of Nutmeg at 211% was filtered out of a live bazaar and the page read as "no Spices".
    grid = detect_grid(elements, W, H, zone=zone, cell_types=("button",),
                       min_cells=(1 if tab != "purchase" else 4), size_tol_h=0.45)
    if grid is None:
        logger.info(f"[{tab}] omni: no goods grid detected")
        return []

    text_els = [e for e in elements if getattr(e, "element_type", "") == "text"]
    # A SHORT-BOXED CELL STILL OWNS A FULL ROW OF TEXT. The price index sits at the bottom of
    # the tile, but a BAZAAR tile's box is clipped above it (y2=616 against a 211% at y=642), so the
    # `211%` on the Box of Nutmeg fell outside its own cell and the good came back with no
    # index — which reads as "cannot confirm the bazaar" and refuses the sale. Every cell is
    # measured against the row pitch so the text below a short box is still attributed to it.
    from dataclasses import replace as _replace
    _row_h = int(median([c.h for c in grid.cells])) if grid.cells else 0
    goods: list[MarketGood] = []
    owned_candidates: dict[str, list[int]] = {}
    for cell in grid.in_reading_order():
        if cell.h < _row_h:
            cell = _replace(cell, y2=cell.y1 + _row_h)
        cell_text = [e for e in text_els if cell.contains(e.cx, e.cy)]
        good = _parse_tile_from_button(cell, cell_text, tab, label=_tile_label(cell, elements))
        if not good:
            continue
        # Template-guided recovery: the index % sits at the bottom-left of EVERY
        # cell (congruent layout). OmniParser's tiny-text detection is flaky, so
        # when it's missing, re-read exactly that sub-region instead of guessing.
        if good.index_pct is None:
            good.index_pct = _recover_cell_index(frame, cell)
        # The greyed-out artwork is the sold-out state itself, independent of any timer text
        # (which is all `_classify_token` ever set this from) and of the quantity badge.
        if tab == "purchase" and _tile_has_condition_ribbon(frame, cell):
            # CONDITIONAL BEFORE SOLD-OUT: a gated good may also read as greyed, and calling
            # it "sold out" sends the buy loop off to spend a blue gem that cannot help.
            logger.info(f"[{tab}] {good.name!r} carries a condition ribbon — gated, not empty")
            good.conditional = True
        elif tab == "purchase" and not good.sold_out and _tile_looks_sold_out(frame, cell):
            logger.info(f"[{tab}] {good.name!r} tile is greyed — sold out")
            good.sold_out = True
        # THE SEASON, asked only of a tile we have NOT judged sold out. An empty tile is also
        # reddish (the stamp, the red `0`) and would read as a scarce season — a different
        # fact with the opposite remedy, since a gem restocks a shelf and cannot mend a season.
        if tab == "purchase" and not good.sold_out and not good.conditional:
            good.season = tile_season(frame, cell)
            if good.season:
                logger.info(f"[{tab}] {good.name!r} season ribbon: {good.season}")
        # SELL tab: the owned-count overlay (white, bottom-left of the icon) is too small for
        # the general OmniParser pass — it mangles multi-digit counts (1,444→444/14444), so
        # read it directly from that sub-region (threshold the white digits + targeted OCR).
        #
        # KEEP BOTH READINGS. This used to overwrite the OmniParser value outright, which
        # meant the specialist won even when it was the one that was wrong: live 2026-08-27
        # it read Candle's badge as 2148 (truly 148) because the melted-wax artwork beside
        # the digits thresholded into a leading '2', at confidence 0.6519 against
        # OmniParser's 0.9987 — and it discards its own confidence, so it could not know.
        # The two candidates are reconciled below against what the hold can contain.
        if tab != "purchase":
            q = _read_owned_qty(frame, cell)
            if q is not None:
                if good.owned_qty is not None and int(good.owned_qty) != int(q):
                    owned_candidates[good.name] = [int(q), int(good.owned_qty)]
                good.owned_qty = q
        goods.append(good)

    if tab != "purchase" and owned_candidates:
        _reconcile_owned_against_the_hold(frame, goods, owned_candidates)

    if claude_fallback:
        goods = _apply_claude_fallback(frame, goods, tab)

    # THE RECOVERY BELONGS TO THE READ, NOT TO ONE CALLER OF IT. This used to live only in
    # `read_market_all_pages`, so the ledger got the tile fallback and `sell_down_to` — which
    # calls THIS function directly — did not. On 2026-08-29 that split the two apart by eleven
    # seconds: the accumulator recovered `Candle 1182` from its tile, the trim asked the same
    # page, got None, and skipped Candle as unreadable. Both were individually right. Fill the
    # gaps HERE, while this frame's tiles are still where they were, and every caller gets the
    # combined read instead of each choosing.
    fill_missing_quantities(frame, goods)
    if tab != "purchase":
        fill_missing_prices(frame, goods)

    logger.info(
        f"[{tab}] omni grid {grid.n_rows}×{grid.n_cols}: {len(goods)} goods"
    )
    return goods


def _read_owned_qty(frame: Image.Image, cell) -> Optional[int]:
    """Units of this good in cargo, from the SELL tile's white count overlay at the icon's
    bottom-left.  The overlay is small and the general OmniParser text pass mis-OCRs multi-digit
    counts (1,444 read as 444 or 14444), so read it directly: crop that sub-region, KEEP only the
    bright digit pixels (threshold), upscale, OCR, and take the digits.  Bounded by cargo capacity
    (~4,108) to drop OCR garbage.  Returns None if unreadable."""
    import numpy as np
    from vision.ocr import _get_reader
    x0, y0, x1, y1 = cell.rel_region(0.0, 0.42, 0.37, 0.66)   # bottom-left of the icon square
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(frame.width, x1), min(frame.height, y1)
    if x1 <= x0 or y1 <= y0:
        return None
    a = np.asarray(frame.crop((x0, y0, x1, y1)).convert("L")).astype(np.int16)
    binimg = Image.fromarray((255 - (a > 170).astype(np.uint8) * 255).astype(np.uint8))
    up = binimg.resize((binimg.width * 5, binimg.height * 5), Image.LANCZOS)
    try:
        toks = _get_reader().readtext(np.array(up), detail=0)
    except Exception:
        return None
    # Take the LONGEST digit token, not the first: a BRIGHT icon (e.g. the white Textiles fabric)
    # leaves threshold blobs beside the count that OCR as short spurious digits — live 2026-08-20
    # the strip read ['2', '900'] and first-token returned 2.  The real count is the dominant
    # (longest) number; artifacts are 1-2 stray digits.
    cands = []
    for t in toks:
        d = re.sub(r"\D", "", t)
        if d and 0 < int(d) <= 4108:
            cands.append(d)
    if not cands:
        return None
    return int(max(cands, key=len))


def _recover_cell_index(frame: Image.Image, cell) -> Optional[int]:
    """Re-read the price index from a cell's bottom-left corner via targeted OCR.

    Used only when OmniParser dropped the % token for this cell. Because every
    cell is congruent, the index's relative box is known — no full re-parse.
    """
    from vision.ocr import _get_reader
    x0, y0, x1, y1 = cell.rel_region(0.0, 0.72, 0.36, 1.0)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(frame.width, x1), min(frame.height, y1)
    if x1 <= x0 or y1 <= y0:
        return None
    try:
        toks = _get_reader().readtext(np.array(frame.crop((x0, y0, x1, y1))), detail=0)
    except Exception:
        return None
    # Clean read: "99%"
    for t in toks:
        m = re.search(r'(\d{1,3})\s*%', t)
        if m:
            return int(m.group(1))
    # Garbled read: this corner holds ONLY the index badge, so any number here is
    # the index. OCR often turns "99%" -> "990" or "990/" (the % becomes 0/'/'):
    # a value > 250 ending in 0 is that misread — drop the trailing 0.
    for t in toks:
        digits = re.sub(r'\D', '', t)
        if not digits:
            continue
        v = int(digits)
        if 40 <= v <= 250:
            return v
        if v > 250 and digits.endswith('0'):
            v2 = int(digits[:-1])
            if 40 <= v2 <= 250:
                return v2
    return None


# ── Alias used by market_actions.py ───────────────────────────────────────────

def read_market_page_claude(
    frame: Image.Image,
    tab: str = "purchase",
    port: str = "unknown",
) -> list[MarketGood]:
    """Compatibility alias — now delegates to the OmniParser reader (detected
    tiles, orientation/notch-robust). Name kept for existing callers."""
    return read_market_page_omni(frame, tab=tab, port=port)


# ── Multi-page helpers (unchanged interface) ───────────────────────────────────


# The quantity badge sits in the lower-right of a tile's THUMBNAIL, which is left of the
# label. Offsets from the tile's tap point, measured on Barcelona sell pages 2026-08-27.
_TILE_QTY_BOX = (-215, -120, -40, 40)
# x4, because x2 was not enough for the smallest badge measured (`Lemon Oil 1`).
_TILE_QTY_SCALE = 4


def fill_missing_quantities(frame, goods, *, read_text_fn=None):
    """Re-read `owned_qty` for goods the page read left as None, from EACH GOOD'S OWN TILE.

    A whole-frame parse is silent about the smallest badges, and upscaling the WHOLE FRAME is
    not the answer: measured 2026-08-27, x2 recovered `Lemon Oil 1` while turning `Neroli 2`
    into `8`, and the resample filter changed both. A wrong number is worse than a missing one
    because nothing downstream can tell.

    A tight crop of one tile has the number ALONE in the image rather than one of forty in a
    busy 2400x1080 frame, and it reads `Lemon Oil 1`, `Iron 2,099` and `Gunpowder 2` correctly.
    Only the gaps pay for it.

    Mutates and returns `goods`. A tile that still will not read stays None — UNREADABLE IS
    NOT ZERO.
    """
    if read_text_fn is None:
        from vision.ocr import read_text as read_text_fn

    dx1, dy1, dx2, dy2 = _TILE_QTY_BOX
    for g in goods:
        if getattr(g, "owned_qty", None) is not None:
            continue
        x, y = getattr(g, "tap_x", None), getattr(g, "tap_y", None)
        if x is None or y is None:
            continue
        try:
            tile = frame.crop((max(0, x + dx1), max(0, y + dy1),
                               max(0, x + dx2), min(frame.height, y + dy2)))
            tile = tile.resize((tile.width * _TILE_QTY_SCALE, tile.height * _TILE_QTY_SCALE))
            digits = [t for t in (read_text_fn(tile) or "").replace(",", "").split()
                      if t.isdigit()]
        except Exception as exc:
            logger.debug(f"[market] tile re-read failed for {g.name!r}: {exc}")
            continue
        if digits:
            g.owned_qty = int(digits[-1])
            logger.info(f"[market] {g.name!r} owned was unreadable on the page; its tile "
                        f"reads {g.owned_qty}")
    return goods


# The tile's price BAR — the dark pill holding "price (profit)", with the index badge left
# out. Offsets from the tile's tap point, measured on both grid rows of
# trace_barter_cmd_2026-09-06T18-08-54 frame 209 (row 1 taps y=315, row 2 y=556).
_TILE_PRICE_BOX = (-125, 58, 235, 127)
# Tried in order, first CLEAN read wins. Noise is not monotonic in scale — the same bar read
# '13,322' at x4-rejected, '13, 4 322' at x6 and '13,322' at x8 — so one scale is a gamble
# and a short ladder is not.
_TILE_PRICE_SCALES = (8, 4, 6)

# STRICT, AND ANCHORED AT THE START. This is the whole safety of the re-read: a bar that OCR'd
# as '13 1 322 (10,330)' must be REJECTED, not read as 322 by a pattern that is happy to start
# matching in the middle. `^` plus a number that may hold only separators does that — '13'
# then a space is not followed by '(', so the match fails and the price stays unreadable.
#
# `fill_missing_quantities` states the reason: "A wrong number is worse than a missing one
# because nothing downstream can tell." Here it is worth 10x — 131,322 against 13,322.
_PRICE_PAIR = re.compile(r"^\s*(\d[\d,.']*)\s*\(\s*([-+]?\d[\d,.']*)\s*\)")


def fill_missing_prices(frame, goods, *, read_text_fn=None):
    """Re-read `price`/`profit_per_unit` from EACH GOOD'S OWN TILE, for goods the page missed.

    The same remedy `fill_missing_quantities` applies to badges, for the price bar — and the
    same reason. CLAUDE.md: "DOWNSCALED IMAGES ARE FOR COARSE JUDGMENTS ONLY — never for
    CONTENT... the `44` on Matchlock Gun's thumbnail was NEVER PROPOSED AS TEXT... The same
    detector on a 750x520 CROP read the `44` without trouble."

    Live 2026-09-06 at Lisboa, and it cost the mission its cargo. The Birch Tree tile reads
    `99%  13,322 (10,330)`; the whole-frame parse proposed ONE token for that row — '990',
    which is the 99% badge with its '%' read as '0' — and no price at all. `select_sellable`
    then skipped the good ("we hold 3668 but its price is unreadable, and this pass sells on
    profit") and the mission reported DONE holding 3,668 units it had sailed there to sell.
    The three neighbouring tiles, whose prices are four digits, parsed perfectly from the same
    frame.

    Mutates and returns `goods`. A bar that will not read CLEANLY stays None — unreadable is
    not zero, and a guessed price is worse than none.
    """
    if read_text_fn is None:
        from vision.ocr import read_text as read_text_fn

    dx1, dy1, dx2, dy2 = _TILE_PRICE_BOX
    for g in goods:
        if getattr(g, "profit_per_unit", None) is not None:
            continue
        if not getattr(g, "owned_qty", None):
            continue                          # not aboard — no sale to price
        x, y = getattr(g, "tap_x", None), getattr(g, "tap_y", None)
        if x is None or y is None:
            continue
        for scale in _TILE_PRICE_SCALES:
            try:
                bar = frame.crop((max(0, x + dx1), max(0, y + dy1),
                                  max(0, x + dx2), min(frame.height, y + dy2)))
                bar = bar.resize((bar.width * scale, bar.height * scale))
                m = _PRICE_PAIR.match(read_text_fn(bar) or "")
            except Exception as exc:
                logger.debug(f"[market] price re-read failed for {g.name!r}: {exc}")
                break
            if m is None:
                continue
            g.price = int(m.group(1).translate(_SEP))
            g.profit_per_unit = int(m.group(2).translate(_SEP))
            logger.info(f"[market] {g.name!r} price was unreadable on the page; its tile "
                        f"reads {g.price:,} ({g.profit_per_unit:,}) at x{scale}")
            break
    return goods


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
        page_goods = read_market_page_omni(frame, tab=tab, port=port)  # fills its own gaps

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
    # The Sell item is FOUND in the left menu, never tapped at a remembered point — see
    # `actions.buy_materials._sell_menu_item`. Reading the Purchase grid while labelling it
    # "sell" reports the SHOP'S stock as the fleet's hold, which is the one mistake this
    # function must not make.
    from actions.buy_materials import ensure_sell_tab
    if not ensure_sell_tab(capture_fn, tap_fn, 1.5):
        logger.warning("Market: could not reach the Sell tab — returning no hold rather "
                       "than the shop's stock")
        return purchase, []
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


def _reconcile_owned_against_the_hold(frame, goods, candidates) -> None:
    """Settle disagreeing owned-quantity readings by what the ship can carry.

    The Cargo bar is on this same frame, so the check is free. It can only VETO the
    impossible — where the arithmetic does not bind, the specialist's reading stands, and
    the good is logged as unsettled rather than silently believed."""
    from actions.market_actions import _read_cargo_capacity
    from vision.owned_constraints import reconcile_owned

    try:
        used, capacity = _read_cargo_capacity(frame)
    except Exception as exc:                       # a check must never break the read
        logger.warning(f"[sell] cargo bar unreadable ({exc}) — owned readings left as-is")
        return
    if not used or not capacity:
        logger.info("[sell] no cargo bar on this frame — cannot check owned readings "
                    f"against the hold; disagreements stand: {candidates}")
        return

    # Every good on the page, so the SUM is checked, not just the disputed one: 2,148 is
    # under the 3,040 held and passes any per-good bound. Only Iron+Candle=4,247 is
    # impossible.
    by_name = {g.name: g for g in goods}
    full = {g.name: [int(g.owned_qty)] for g in goods
            if getattr(g, "owned_qty", None) is not None}
    full.update({n: v for n, v in candidates.items()})

    verdict = reconcile_owned(full, used, capacity)
    for name, value in verdict.values.items():
        good = by_name.get(name)
        if good is None or getattr(good, "owned_qty", None) is None:
            continue
        if int(good.owned_qty) != int(value):
            logger.warning(f"[sell] {name}: readers disagreed {candidates.get(name)} — "
                           f"the hold ({used}/{capacity}) admits {value}. {verdict.reason}")
            good.owned_qty = int(value)
    if verdict.unresolved:
        logger.warning(f"[sell] owned quantity NOT settled by the hold for "
                       f"{list(verdict.unresolved)} — {verdict.reason}")
