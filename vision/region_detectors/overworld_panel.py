"""The overworld right panel — ONE element, two worlds, different contents.

The port overworld and the sea both carry it, in the same place, with the same parts in the
same order (user, 2026-09-11: *"it is a very distinct UI element shared by both sea and port
overworld, with different contents"*). Measured on two live frames of
`data/sessions/trace_barter_cmd_2026-09-11T13-47-33`, frame 0000 at London and frame 0588 in
the Atlantic:

                               PORT                         SEA
    tab strip   y[129,202]     Tasks · Buildings ·          Tasks · Ports ·
                               Players · location pin       fleets · ships
    minimap     y≈[200,397]    the town, GLOBE bottom-right the sea, lat/long bottom-right
    season row  y[397,439]     ☀ | Summer | Aug | 00:10     ☀ | Summer | Aug | Night
    the list    y 435 down     Harbor, Market, Shipyard…    Berber Village, Las Palmas…

The four tab boxes land within a few pixels of each other in both worlds — centres at x ≈
1916, 2016, 2113, 2211 — and the panel's own x-span is [1862, 2267] on both. At sea a column
of gauges (speed, crew, wind) sits to its LEFT at x[1778,1865]; that is the sea HUD, not part
of this.

WHY A CLASS RATHER THAN MORE CONSTANTS. This element already existed five times, and none of
the five could see the others:

  * `CHROME_RIGHT_PANEL_REGION = (2050, 100, 2400, 420)` — a presence COUNT, used to decide
    "this is an overworld". Both edges are wrong: the panel starts at 1862, not 2050, and
    ends at 2267, not the frame edge. It clips the tab strip and overruns into the scene.
  * `state_fingerprints_data.RIGHT_EDGE = (0.85, 0.05, 1.00, 0.45)` — the same box again,
    normalised, calibrated separately, and to different numbers.
  * `sail_actions._tab_strip_band()` — the strip alone, derived from the minimap crop.
  * `BUILDING_MENU_REGION = (1850, 380, 2400, 1080)` — the list alone.
  * `panels.detect_right_panel` — a DIFFERENT thing entirely (Cart, Hire, City Info on chromed
    screens) that happens to share the name.

Each was learned from a live wedge and written where it was learned, which is how they came to
disagree. Two of those wedges are this element being misread as something else: on 2026-09-01
ENTER_BUILDING at sea "cycled the minimap's four tab icons for minutes" — the same strip, a
different world, and code that only knew the port's; and the obstruction classifier has read
the port's panel as a POPUP, because a large bright rectangle on the right is what a popup
looks like if nobody has told you this one is furniture.

THE SEASON ROW IS THE ANCHOR, not a y constant. It is the one part that is identical in both
worlds and in every port, it separates the minimap from the list, and it is TEXT, so it is
read rather than located. Positions here are not stable — the game re-bakes its camera-cutout
offset per screen, and the same panel has rendered ~110px apart between two sessions — so
every box below is derived from what was found, never from a number written here.

WHAT THIS REPORTS IS STRUCTURE, NOT MEANING. Which tab is Buildings, and whether a row names a
building or a destination, is per-world and belongs to that world's context module
(`brain.port_context`, and the sea's when it has one). Same division as the market: this is
`goods_tiles`, not `market_reader`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

from loguru import logger

# The seasons, as the game draws them. The row reads "☀ | Summer | Aug | 00:10" at a port and
# "☀ | Summer | Aug | Night" at sea — same row, and the last cell is a clock or a day phase.
_SEASONS = ("spring", "summer", "autumn", "fall", "winter")

# NO SEARCH REGION. There was one — the right fifth of the frame — and it was the same
# mistake in miniature as the constants above (user, 2026-09-11: *"I hope to avoid the
# hardcoded bbox, especially for the x and y starting point"*). The season word is unique
# enough to be found in the whole parse, and everything else is measured from it, so this
# module now contains no absolute position at all.
#
# Every number below is a RATIO of something read off the frame, or a small tolerance. None
# of them says where anything is.

# A tab's side, as a fraction of the panel's own width. Measured 71-73 tall in a 405-wide
# panel, i.e. ~0.18; the bounds are wide because what they must exclude is far away — the
# minimap is 229 tall (0.57) and the list rows are 0.6+ of the width.
_TAB_MIN_SIDE_FRAC = 0.08
_TAB_MAX_SIDE_FRAC = 0.50

# A tab is roughly square. The list rows are several times wider than tall.
_TAB_MAX_ASPECT = 2.0

# A list ROW, as a fraction of the panel width. Rows measured 68-105 tall against a 40-tall
# sub-label ("Approx. 65.7km") that belongs to the row above it, so the cut sits between.
_MIN_ROW_H_FRAC = 0.11

# A row spans most of the panel; a sub-label does not.
_ROW_MIN_WIDTH_FRAC = 0.6

# How far a part may sit outside the measured span and still be attached to it, as a fraction
# of the panel width. Small, because what it must resolve is a seam: the sea's gauge strip is
# flush against the panel's left edge.
_SEAM_FRAC = 0.03


@dataclass(frozen=True)
class Tab:
    """One button of the strip. What it MEANS is the world's business, not this module's.

    NO BADGE FIELD. The little counts are real (9 tasks, 10 buildings, 6 ships) but
    `parse_fast` returns these boxes as bare `icon`, and the count it does surface lands on
    whichever box happened to swallow the digits — measured across two frames, one tab in
    eight carried its number. A field that is None almost always is worse than no field: it
    invites a caller to trust the one time it is set.
    """
    box: Tuple[int, int, int, int]

    @property
    def cx(self) -> int:
        return (self.box[0] + self.box[2]) // 2

    @property
    def cy(self) -> int:
        return (self.box[1] + self.box[3]) // 2


@dataclass(frozen=True)
class OverworldPanel:
    """The panel as found. Every box is measured; none is remembered.

    `gauges` is the vertical strip attached to the panel's top-left, and it is PART OF THIS
    PANEL (user, 2026-09-11: *"the speed square should also belong to this panel, it is a
    vertical icon strip that is attached to the top left of the panel"*). It appears at sea
    only. Read off frame 0588, top to bottom: **HW** in red (the tide state), a ship over
    **27.6** (speed), a windsock over **4 ◄** (wind strength and bearing), and waves over
    **1 ◄** (current). Empty at a port, which is how a caller tells the worlds apart without
    being told which world it is in.
    """
    box: Tuple[int, int, int, int]
    tabs: Tuple[Tab, ...]
    minimap: Optional[Tuple[int, int, int, int]]
    season_row: Tuple[int, int, int, int]
    list_region: Optional[Tuple[int, int, int, int]]
    rows: Tuple[Tuple[str, int, int], ...] = ()
    gauges: Tuple[Tuple[int, int, int, int], ...] = ()

    @property
    def at_sea(self) -> bool:
        """The gauge strip is drawn at sea and nowhere else — an OBSERVATION of this panel,
        never a substitute for the nav state, which perceive owns."""
        return bool(self.gauges)

    @property
    def tab_points(self) -> Tuple[Tuple[int, int], ...]:
        """Tap points, left to right — the shape `selected_tab_index` already takes."""
        return tuple((t.cx, t.cy) for t in self.tabs)


def detect_overworld_panel(frame, elements: Sequence[Any] = None) -> Optional[OverworldPanel]:
    """The right panel of a port overworld or the sea, or None if it is not there.

    `elements` is one tick's OmniParser parse; pass it so this costs nothing extra — every
    question here is answered from the same look (the per-frame sharing rule).

    None means NOT FOUND, which is a real answer: a building interior has no such panel, and
    at a port it can be missing simply because the scene has not finished drawing.
    """
    els = list(elements) if elements is not None else _parse(frame)
    if not els:
        return None

    season = _season_row(els)
    if season is None:
        # NO SEASON ROW, NO PANEL. Deliberately strict: without the anchor every other part
        # would have to be found by a position written down here, which is the thing this
        # module exists to stop. A caller that wants "is there anything on the right" should
        # ask the chrome detector, which is what that count is for.
        logger.debug("[overworld_panel] no season row — not an overworld")
        return None

    x0, x1 = _span(els, season)
    seam = max(4, int((x1 - x0) * _SEAM_FRAC))
    tabs = _tabs(els, x0, x1, above=season[1], seam=seam)
    top = min([t.box[1] for t in tabs], default=season[1])
    gauges = _gauges(els, x0, top=top, bottom=season[1], seam=seam)
    minimap = _minimap(x0, x1, tabs, season)
    # BELOW THE ROW'S MIDDLE, not below its bottom. The cells of the season row are not all
    # the same height, so its bottom edge is the tallest of them — and at London that put the
    # line at 439 while the first building row starts at 437, losing `Harbor` by two pixels.
    rows = _rows(els, x0, x1, below=(season[1] + season[3]) // 2, seam=seam)
    # CLAMPED TO THE FRAME. The last row's centre plus a row height runs a pixel past the
    # bottom at London, and a box that leaves the image is a box a crop will silently shrink.
    row_h = max(1, int((x1 - x0) * _MIN_ROW_H_FRAC))
    bottom = min(_height(frame, els), max((r[2] for r in rows), default=0) + row_h)
    list_region = (x0, season[3], x1, bottom) if rows else None

    # THE STRIP IS PART OF THE PANEL, so the panel's own box reaches around it.
    left = min([g[0] for g in gauges], default=x0)
    return OverworldPanel(box=(left, top, x1, list_region[3] if list_region else season[3]),
                          tabs=tuple(tabs), minimap=minimap, season_row=season,
                          list_region=list_region, rows=tuple(rows), gauges=tuple(gauges))


def _season_row(els) -> Optional[Tuple[int, int, int, int]]:
    """The ☀ | Season | Month | time row, as one box spanning its cells.

    Found by the SEASON WORD, which is one of five and is drawn the same everywhere. The month
    and the clock beside it vary; the season does not.
    """
    hit = next((e for e in els
                if (getattr(e, "label", "") or "").strip().lower() in _SEASONS), None)
    if hit is None:
        return None
    # Its neighbours COMPLETE the box, and they are the cells that sit INSIDE its band — not
    # everything that crosses it. The minimap is one tall box spanning y[134,425] at a port,
    # so "anything whose band contains this line" swallowed it and the row came back 300px
    # tall, which then put the tab search above the wrong line and returned the ACCOUNT BAR
    # at the top of the screen as the tab strip.
    pad = max(16, (hit.y2 - hit.y1) // 2)
    row = [e for e in els if e.y1 >= hit.y1 - pad and e.y2 <= hit.y2 + pad]
    return (min(e.x1 for e in row), min(e.y1 for e in row),
            max(e.x2 for e in row), max(e.y2 for e in row))


def _span(els, season) -> Tuple[int, int]:
    """The panel's left and right edges, from the WIDE boxes that share its column.

    The minimap and every list row run the full width of the panel, so their edges agree and
    the median of them is the panel's. Measured 1862..2267 in both worlds; taken from the
    frame rather than written down, because the camera-cutout offset moves it.
    """
    wide = [e for e in els
            if (e.x2 - e.x1) > (season[2] - season[0]) * 0.8
            and e.x2 > season[0] and e.x1 < season[2]]
    if not wide:
        return season[0], season[2]
    lefts = sorted(e.x1 for e in wide)
    rights = sorted(e.x2 for e in wide)
    return lefts[len(lefts) // 2], rights[len(rights) // 2]


def _tabs(els, x0: int, x1: int, *, above: int, seam: int) -> List[Tab]:
    """The strip: small, roughly square boxes in one horizontal row above the minimap.

    BY SHAPE AND ROW, not by count. Four is what both worlds show today, and a port that shows
    three (or five) must degrade to reading three, not to reading the wrong four — the
    calibrated-index bug at Jakarta selected PLAYERS because it assumed the layout.
    """
    # INSIDE THE PANEL, not merely centred near it. At sea a column of gauges — speed, wind,
    # crew — sits flush against the panel's left edge at x[1778,1865], and each is a small
    # roughly-square box, so by shape alone they are indistinguishable from tabs. Their
    # CONTAINMENT is what differs: a tab is within the span, a gauge straddles its edge.
    # A centre-plus-margin test separated them by two pixels, which is not a separation.
    cands = [e for e in els
             if e.y2 < above
             and e.x1 >= x0 - seam and e.x2 <= x1 + seam
             and _looks_like_a_tab(e, x1 - x0)]
    if not cands:
        return []
    # ONE ROW, the lowest one: the account bar at the very top of the screen is also a row of
    # small icons, and it is not this.
    lowest = max(cands, key=lambda e: e.y2)
    mid = (lowest.y1 + lowest.y2) // 2
    row = sorted((e for e in cands if e.y1 <= mid <= e.y2), key=lambda e: e.x1)
    return [Tab(box=(e.x1, e.y1, e.x2, e.y2)) for e in row]


def _looks_like_a_tab(e, span: int) -> bool:
    """Small and roughly square, SIZED AGAINST THE PANEL rather than in pixels.

    The panel's own width is the only scale this element has, and it is read off the frame —
    so a tab is a fraction of it, not a number of pixels. That survives a resolution change
    and a re-baked cutout offset alike.
    """
    w, h = e.x2 - e.x1, e.y2 - e.y1
    if h <= 0 or span <= 0:
        return False
    if not (span * _TAB_MIN_SIDE_FRAC <= h <= span * _TAB_MAX_SIDE_FRAC):
        return False
    return (w / h) <= _TAB_MAX_ASPECT


def _gauges(els, x0: int, *, top: int, bottom: int, seam: int) -> List[Tuple[int, int, int, int]]:
    """The sea's vertical strip, attached to the panel's top-left. Empty at a port.

    ATTACHED IS THE TEST, and it is a relation, not a place: each cell's RIGHT edge meets the
    panel's LEFT edge, and it sits in the band between the top of the tab strip and the season
    row. Four cells at sea — tide state, speed, wind, current — and nothing at a port.

    This strip is why the tab search checks containment rather than centres. The cells are the
    same shape and size as tabs and sit immediately beside them, so shape alone cannot tell
    them apart; which side of the seam they fall on can.
    """
    out = [e for e in els
           if abs(e.x2 - x0) <= seam and e.x1 < x0 - seam
           and e.y2 > top - seam and e.y1 < bottom]
    return [(e.x1, e.y1, e.x2, e.y2) for e in sorted(out, key=lambda e: e.y1)]


def _minimap(x0: int, x1: int, tabs, season) -> Optional[Tuple[int, int, int, int]]:
    """Everything between the strip and the season row. Derived, never calibrated.

    `MINIMAP_CROP` is calibrated elsewhere and is right about where the map is
    (`memory/minimap-crop-is-calibrated-not-constant`); this is the same rectangle arrived at
    from the panel's own parts, which is what makes it survive the cutout shift.
    """
    if not tabs:
        return None
    top = max(t.box[3] for t in tabs)
    return (x0, top, x1, season[1]) if season[1] > top else None


def _rows(els, x0: int, x1: int, *, below: int, seam: int) -> List[Tuple[str, int, int]]:
    """The list, as (label, x, y) — the shape `read_building_menu` already returns.

    A row is a LABELLED box below the season row, INSIDE the panel, and wide enough to span
    it. The sub-label under a destination ("Approx. 65.7km") is narrower and shorter, and
    belongs to the row above rather than being one.

    CONTAINMENT IS NOT OPTIONAL HERE EITHER. Dropping the old right-of-frame prescreen was
    right — it was an absolute position — but it left this scanning the whole frame, and at
    sea the destination label at the bottom CENTRE ("London", cx 1153) is a wide labelled box
    below the season line. It came back as a row of the list. The panel's own span, measured
    from the frame, is what excludes it.
    """
    out = []
    for e in sorted((e for e in els if e.y1 >= below), key=lambda e: e.y1):
        label = (getattr(e, "label", "") or "").strip()
        if not label or (e.y2 - e.y1) < (x1 - x0) * _MIN_ROW_H_FRAC:
            continue
        if (e.x2 - e.x1) < (x1 - x0) * _ROW_MIN_WIDTH_FRAC:
            continue
        # BY ITS CENTRE, NOT ITS EDGES. A row's box is routinely bigger than the row: at
        # London the last building comes back as x[1860,2351] because the parse merged
        # `Bureau` with the coordinate footer beneath it, 84px past the panel's right edge.
        # Judging that by an edge drops a building the bot can walk into
        # (`memory/a-box-bigger-than-its-thing`). The centre is inside either way, and it is
        # still enough to reject the sea's bottom-centre destination label at cx 1153.
        if not (x0 - seam <= _cx(e) <= x1 + seam):
            continue
        out.append((label, _cx(e), (e.y1 + e.y2) // 2))
    return out


def _cx(e) -> int:
    return (e.x1 + e.x2) // 2


def _height(frame, els) -> int:
    h = getattr(frame, "height", None)
    if isinstance(h, int) and h > 0:
        return h
    size = getattr(frame, "size", None)
    if isinstance(size, (tuple, list)) and len(size) > 1:
        return int(size[1])
    return max((e.y2 for e in els), default=1080)


def _width(frame, els) -> int:
    w = getattr(frame, "width", None)
    if isinstance(w, int) and w > 0:
        return w
    size = getattr(frame, "size", None)
    if isinstance(size, (tuple, list)) and size:
        return int(size[0])
    return max((e.x2 for e in els), default=2400)


def _parse(frame) -> list:
    try:
        from vision.omniparser import parse_fast_cached
        return list(parse_fast_cached(frame) or [])
    except Exception as exc:                        # noqa: BLE001 — a read, not a decision
        logger.debug(f"[overworld_panel] parse unavailable: {exc}")
        return []
