"""Read the world map's **Trade Event Schedule** dialog.

Opened from the `Trade Event Schedule` button in the world map's bottom-left corner. Mayors
schedule market events; during a **Bazaar** the named goods category fetches a much higher
price at that city (user, 2026-08-23), so an event is a selling opportunity worth planning
around.

    Market Event | Trade Goods | Fixed-term              | Location
    [Bazaar]  Spices    2026/08/24 13:00 ~ 14:00   Bremen    Tax: 15%
    [Bazaar]  Jewelry   2026/08/24 15:00 ~ 16:00   Bergen    Tax:  5%
    [Bazaar]  Spices    2026/08/25 13:00 ~ 14:00   Cologne   Tax: 15%

**The times are KOREAN time, UTC+9** (user, 2026-08-23) — not the player's local time and not
the game's in-world clock. Everything here is returned timezone-aware so a caller can compare
against `datetime.now(timezone.utc)` without re-deriving the offset.

The list **SCROLLS** — the bottom row is usually clipped, and more events sit below it (user,
2026-08-23). A clipped row comes back with `start=None`, so a caller that needs a window can
filter on it; nothing here invents times for a row it could not read. Scroll-accumulation (as
`actions/village_check` does for the trade list) is the natural next step when the visible
window is not enough — often it is, since several bazaars are fully in view.

The dialog also carries a **Major Trend** tab; only Market Event is read today.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from loguru import logger

# The schedule is published in Korean time.
KST = timezone(timedelta(hours=9))

# Row geometry: each event occupies two OCR lines (goods + dates, then type + times + tax),
# so tokens are gathered in a band around the date line rather than line by line.
_ROW_BAND_PX = 60

_DATE_RE = re.compile(r"(20\d{2})[/\-.](\d{1,2})[/\-.](\d{1,2})")
# OCR renders 13:00 as "13.00" about as often as "13:00".
_TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")
_TAX_RE = re.compile(r"^(\d{1,3})\s*%$")

_EVENT_KINDS = ("bazaar", "festival", "fair", "boom", "shortage")

# The dialog extends a little past its outermost header: left of 'Market Event' for the event
# badge, right of 'Location' for the two per-row buttons. Beyond that is the world map behind.
_DIALOG_PAD_LEFT = 150
_DIALOG_PAD_RIGHT = 280


@dataclass(frozen=True)
class TradeEvent:
    """One scheduled market event."""

    kind: str                       # 'Bazaar', ...
    goods: str                      # the CATEGORY on offer, e.g. 'Spices'
    city: str
    start: Optional[datetime] = None      # timezone-aware (KST)
    end: Optional[datetime] = None
    tax_pct: Optional[int] = None
    # Where this row sits on screen. Carried so an action can find the row's buttons without
    # re-deriving it from the city TEXT, which is exactly the unreliable part: Edinburgh reads
    # as 'Fdinhuroh', and matching on that put the location pin in the dialog's title bar.
    row_cy: Optional[int] = None

    def starts_in(self, now: Optional[datetime] = None) -> Optional[timedelta]:
        """Time until the window opens; negative once it has started."""
        if self.start is None:
            return None
        return self.start - (now or datetime.now(timezone.utc))

    def is_live(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return bool(self.start and self.end and self.start <= now <= self.end)


def read_trade_events(frame, *, elements=None) -> List[TradeEvent]:
    """Parse the Trade Event Schedule dialog. Returns [] when it is not on screen.

    Read STRUCTURALLY, from OmniParser: the header row (`Market Event | Trade Goods |
    Fixed-term | Location`) gives stable column anchors, so every cell is assigned to a
    column instead of guessed at by position. The event kind is a tall button spanning both
    of a row's text lines, which makes it the natural row anchor.

    Raw OCR would do the text but not the layout, and this is a table — the same reason the
    market grid and the left menu are read by region rather than by scanning for words.
    """
    if elements is None:
        try:
            from vision.omniparser import parse_fast_cached
            elements = list(parse_fast_cached(frame))
        except Exception as exc:
            logger.debug(f"[trade_events] OmniParser unavailable: {exc}")
            return []
    if not elements:
        return []

    def _label(e):
        return (getattr(e, "label", "") or "").strip()

    if not any("trade event" in _label(e).lower() for e in elements):
        logger.debug("[trade_events] the Trade Event Schedule dialog is not on screen")
        return []

    columns = _header_columns(elements, _label)
    if not columns:
        logger.warning("[trade_events] no header row — cannot assign columns")
        return []

    # BOUND EVERYTHING TO THE DIALOG. `min(columns, key=...)` assigns an element to its
    # NEAREST column, so anything on the world map behind the dialog is assigned too — the
    # map label 'Gdansk' at cx=2270 landed in the Location column and became the city of the
    # Bremen row. The header row gives the dialog's own x-span; nothing outside it is a cell.
    x_lo = min(columns.values()) - _DIALOG_PAD_LEFT
    x_hi = max(columns.values()) + _DIALOG_PAD_RIGHT
    elements = [e for e in elements if x_lo <= e.cx <= x_hi]

    # ANCHOR ON THE GOODS COLUMN, not the event badge. The badge is the eye-catching cell,
    # but OmniParser detected only 4 of the 6 on this dialog (cy 349/460/571/679, missing the
    # last two), while the goods column came through complete (326/436/545/656/767/876).
    # Anchoring on the badge silently dropped two events — including a Spices bazaar.
    from vision.market_reader import _CATEGORIES
    goods_x = columns.get("goods")
    anchors = [e for e in elements
               if goods_x is not None
               and _label(e).lower() in _CATEGORIES
               and min(columns, key=lambda n: abs(columns[n] - e.cx)) == "goods"]
    if not anchors:
        logger.info("[trade_events] no event rows on screen")
        return []

    events = []
    for anchor in sorted(anchors, key=lambda e: e.cy):
        cells = {name: [] for name in columns}
        for e in elements:
            if abs(e.cy - anchor.cy) > _ROW_BAND_PX:
                continue
            lab = _label(e)
            if not lab or lab == "icon":
                continue
            col = min(columns, key=lambda n: abs(columns[n] - e.cx))
            cells[col].append((e.cx, lab))
        kind = next((t for _cx, t in cells.get("kind", [])
                     if t.lower() in _EVENT_KINDS), "")
        ev = _parse_cells(cells, kind)
        if ev is not None:
            events.append(replace(ev, row_cy=int(anchor.cy)))

    logger.info(f"[trade_events] read {len(events)} scheduled event(s): "
                + ", ".join(f"{e.kind} {e.goods}@{e.city}" for e in events))
    return events


def schedule_dialog_present(frame=None, *, elements=None) -> bool:
    """Is the Trade Event Schedule dialog on screen?

    THE HEADER ROW IS THE DIALOG. Nothing else on the world map carries three of
    `Market Event | Trade Goods | Fixed-term | Location`, and two permanent things carry its
    NAME without being it: the left rail's button that opens it (x=131), and a port panel's
    'Market Event Schedule' row (x=1959) — which sits in the panel that also holds Move, so
    a name-based test read every attempt to commit a course as this dialog (live 2026-08-29).

    This is the same gate `read_trade_events` uses to find its columns, so presence and
    reading cannot disagree.
    """
    if elements is None:
        try:
            from vision.omniparser import parse_fast_cached
            elements = list(parse_fast_cached(frame))
        except Exception as exc:
            logger.debug(f"[trade_events] OmniParser unavailable: {exc}")
            return False
    return bool(_header_columns(elements or [],
                                lambda e: (getattr(e, "label", "") or "").strip()))


def _header_columns(elements, label_of) -> dict:
    """{column name: x centre} from the dialog's header row."""
    wanted = {"market event": "kind", "trade goods": "goods",
              "fixed-term": "term", "location": "location"}
    out = {}
    for e in elements:
        key = label_of(e).strip().lower()
        if key in wanted:
            out[wanted[key]] = e.cx
    return out if len(out) >= 3 else {}


def _parse_cells(cells: dict, kind: str) -> Optional[TradeEvent]:
    """Build one event from its column cells."""
    from vision.market_reader import _CATEGORIES

    goods = next((t for _cx, t in cells.get("goods", []) if t.lower() in _CATEGORIES), "")
    if not goods:
        return None

    term = [t for _cx, t in sorted(cells.get("term", []))]
    dates = [m for t in term for m in [_DATE_RE.search(t)] if m]
    times = [m for t in term for m in [_TIME_RE.match(t)] if m]
    start = end = None
    if dates and times:
        start = _combine(dates[0], times[0])
        # The end date repeats when the window does not cross midnight.
        end = _combine(dates[1] if len(dates) > 1 else dates[0],
                       times[1] if len(times) > 1 else times[0])

    loc = cells.get("location", [])
    tax = next((int(m.group(1)) for _cx, t in loc for m in [_TAX_RE.match(t)] if m), None)
    city = _city_from([(t, cx) for cx, t in loc], goods, kind)
    if not city:
        return None
    return TradeEvent(kind=kind.title() or "Event", goods=goods.title(), city=city,
                      start=start, end=end, tax_pct=tax)


def _combine(date_m, time_m) -> datetime:
    return datetime(int(date_m.group(1)), int(date_m.group(2)), int(date_m.group(3)),
                    int(time_m.group(1)), int(time_m.group(2)), tzinfo=KST)


def _city_from(words, goods: str, kind: str) -> str:
    """The Location column, corrected against the port catalogue.

    OCR mangles city names on this dialog ('Fdinhuroh' for Edinburgh, measured), and the
    catalogue is what turns a mangled read into a place the fleet can sail to.
    """
    from vision.text_correction import correct_port_name

    skip = {goods.lower(), kind.lower(), "tax", "location", "market", "event", "trade",
            "goods", "fixed-term", "fixed", "term"}
    best, best_ratio = "", 0.0
    for t, _cx in words:
        s = t.strip()
        if len(s) < 3 or s.lower() in skip or _DATE_RE.search(s) or _TAX_RE.match(s):
            continue
        try:
            name, ratio = correct_port_name(s)
        except Exception:
            name, ratio = None, 0.0
        if name and ratio > best_ratio:
            best, best_ratio = name, ratio
    return best if best_ratio >= 0.6 else ""
