"""Goods cards on a market page, MEASURED from the frame.

DialogModel's sibling, and written for the same reason: a dialog has a stable visual grammar
that classical rules read reliably, and so does a market card. This does not ask OmniParser
where the cards are — it finds them.

WHY THIS EXISTS. Two money-losing defects on 2026-09-10, both an element's BOUNDARY taken
from OmniParser and never checked (see `docs/game_element_detection.md`):

  * A tile boxed SHORT. The Lisboa Almond card runs y 198-432; OmniParser boxed y 195-369,
    the `Specialties` bar and the price row falling outside. Tile-relative positions are
    scaled by dividing by the detected height, so a clipped box reads every token as LOWER
    than it is: the owned badge sits 119px down, which scales to 119 against the true 234 and
    lands in the (80,150) band, but to 157 against the clipped 174 and misses. `1,841` was
    read correctly and discarded, the ledger said `have: 0`, and the buy loop bought 2.1x.

  * A banner boxed ALONE. The same card's gold `Specialties` bar, boxed on its own, is
    508x46 — a WIDE strongly-gold pill, which is exactly the shape a commit button has. With
    the basket empty the real Sell button was greyed and undetectable, so it was the only
    gold thing on screen; it was returned as the Sell button and the tap, landing inside the
    tile, staged and sold all 1,841 Almond during a trim that meant to keep them.

The second is why `tile_containing` matters as much as the boxes: **a goods tile contains no
controls** (user, 2026-09-10: *"the yellow banner is not a yellow button ... there are no
buttons in the good tiles"*). Stated positionally it costs nothing to maintain, where a list
of banner words catches `Specialties` and misses the next one.

HOW. The card is a cream panel on dark wood, and the separation is enormous — which is the
point, after a session spent on a gold threshold with a 0.03 margin either side. Measured at
two ports, on different continents, from different sessions:

    card body      rgb(216, 206, 196)      IDENTICAL at Lisboa and San
    background     rgb(38, 36, 38) / (56, 54, 56)

Cards touch, so cream alone merges a row into one blob. They are separated by a narrow strip
of BACKGROUND, and background-dark is what tells a gap from a thumbnail — a thumbnail is an
image, not a hole:

    inside a card, over the card's full height    dark fraction  0.00 - 0.37
    between two cards                             dark fraction  1.00

KNOWN LIMIT. A card is separated from its neighbour by a column holding NO cream, so a band
inside a card that spanned it edge to edge would split it. Measured, none does: the
conditional card's dark furniture leaves a cream margin either side and its worst row still
reads 0.044. If a future card paints a full-width band, this splits it — and the symptom is
two half-height tiles where one belongs, which the row pitch of its neighbours makes obvious.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np
from loguru import logger
from PIL import Image

# The card body, measured identically at Lisboa and San Village: rgb(216, 206, 196).
#
# MATCHED BY ITS HUE, NOT ITS BRIGHTNESS, because the game DIMS a sold-out tile — measured
# rgb(109, 104, 98) at Ambon, which is the same colour at half the light (the ~1.98 dim
# factor `DialogModel` also measures). A fixed colour missed those cards entirely and the
# page came back one good short; `Ebony` simply was not there.
#
# Cream is WARM — b < g < r — and dimming scales all three channels together, so the ratios
# survive it where the values do not:
#
#     full card    216,206,196     g/r 0.954   b/r 0.907
#     dimmed card  109,104, 98     g/r 0.954   b/r 0.899
#     background    38, 36, 38     g/r 0.947   b/r 1.000   <- neutral, not warm
#     background    56, 54, 56     g/r 0.964   b/r 1.000
#
# So `b/r` alone separates card from ground with a gulf, and the brightness floor only keeps
# near-black noise out of the ratio.
_G_OVER_R, _B_OVER_R = 0.954, 0.903
_RATIO_TOL = 0.045
_MIN_CARD_LUM = 80


def _card_body(arr: np.ndarray) -> np.ndarray:
    """Mask of card-body pixels — the cream, at any brightness the game dims it to."""
    r = np.maximum(arr[:, :, 0], 1)
    g, b = arr[:, :, 1], arr[:, :, 2]
    return ((arr.mean(axis=2) >= _MIN_CARD_LUM)
            & (np.abs(g / r - _G_OVER_R) <= _RATIO_TOL)
            & (np.abs(b / r - _B_OVER_R) <= _RATIO_TOL))

# A GAP CONTAINS NO CARD. That is the whole test, and it is a statement rather than a tuned
# number — which is why this is the measure used instead of darkness. Measured on the Madeira
# purchase grid:
#
#     a true gap, between cards or between rows      cream fraction  EXACTLY 0.000
#     the worst row INSIDE a card (the conditional
#       Shea Butter card's furniture band)                           0.044
#     a thumbnail column                                             0.165 - 0.318
#
# Darkness cannot do this job. The inter-card gap is a ~4px SHADOW whose darkest pixels only
# reach 54 in some rows, while a card's own furniture reaches 88 — so any single luminance
# cut either splits a card in two or merges two cards into one. Both were observed.
_GAP_CREAM_FRAC = 0.01
# Smaller than any real card, in either axis. Only to drop speckle.
_MIN_W, _MIN_H = 120, 80
# AND NOT BIGGER THAN A CARD CAN BE. The market shows a THREE-COLUMN grid, so a card spans
# about a third of the content area — measured 431x234 on 2400x1080, i.e. 0.18 x 0.22.
#
# Without this a DIALOG is a goods card: cream, landscape, centred in the content area. The
# overflow card measures 1314x811 (0.55 x 0.75), and calling it a tile made this detector
# refuse its own `Receive` button — a safety rule turned into a new way to be stuck, which
# is exactly the failure mode the zone default already guards against for the left rail.
_MAX_W_FRAC, _MAX_H_FRAC = 0.35, 0.40
# The market content area, as fractions — the same box `read_market_page_omni` searches:
# right of the left rail, left of the cargo panel, below the header.
_ZONE = (0.17, 0.14, 0.78, 0.92)


@dataclass(frozen=True)
class GoodsTile:
    """One goods card, with the boundary the frame actually shows.

    `label` is left for the caller to fill from whatever read the text. This detector finds
    BOUNDARIES and says nothing about content — which is the division of labour that makes
    it worth having: OmniParser reads text well and boxes it badly, so take the box from the
    pixels and the words from the parse.
    """
    x1: int
    y1: int
    x2: int
    y2: int
    label: str = ""

    @property
    def w(self) -> int:
        return self.x2 - self.x1

    @property
    def h(self) -> int:
        return self.y2 - self.y1

    @property
    def cx(self) -> int:
        return (self.x1 + self.x2) // 2

    @property
    def cy(self) -> int:
        return (self.y1 + self.y2) // 2

    def contains(self, x: float, y: float) -> bool:
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2

    def rel_region(self, fx1: float, fy1: float, fx2: float, fy2: float) -> Tuple[int, int, int, int]:
        """A sub-box given as fractions of the card — the same shape `GridCell` offers.

        Safe here in a way it is not against a detected button box, because these edges are
        the card's own.
        """
        return (int(self.x1 + fx1 * self.w), int(self.y1 + fy1 * self.h),
                int(self.x1 + fx2 * self.w), int(self.y1 + fy2 * self.h))


def _runs(profile: np.ndarray, origin: int) -> List[Tuple[int, int]]:
    """Stretches along one axis that hold some card, given a cream fraction per row/column."""
    solid = profile > _GAP_CREAM_FRAC
    out: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for i, s in enumerate(solid):
        if s and start is None:
            start = i
        elif not s and start is not None:
            out.append((start + origin, i + origin))
            start = None
    if start is not None:
        out.append((start + origin, len(solid) + origin))
    return out


def _carve(cream: np.ndarray, box: Tuple[int, int, int, int],
           depth: int = 0) -> List[Tuple[int, int, int, int]]:
    """Cut a cream region into cards, alternating axes until neither splits.

    RECURSIVE, AND THE PROFILE IS TAKEN OVER THE CURRENT SUB-BOX ONLY, because the grid is
    RAGGED: a last row with one card leaves the rest of its band empty, and a profile taken
    across the whole page would read a card's own furniture band as a gap once two thirds of
    the width is already background. Measured on the Madeira grid, where the final row holds
    only Shea Butter — a full-width profile split that card in two.
    """
    x1, y1, x2, y2 = box
    if depth > 4 or (x2 - x1) < _MIN_W or (y2 - y1) < _MIN_H:
        return [box]
    sub = cream[y1:y2, x1:x2]
    cols = _runs(sub.mean(axis=0), x1)
    rows = _runs(sub.mean(axis=1), y1)
    if len(cols) > 1:
        return [b for cx1, cx2 in cols for b in _carve(cream, (cx1, y1, cx2, y2), depth + 1)]
    if len(rows) > 1:
        return [b for ry1, ry2 in rows for b in _carve(cream, (x1, ry1, x2, ry2), depth + 1)]
    if not cols or not rows:
        return []                                 # no card here at all
    # Trim to what the runs actually cover, so a box never carries dead margin.
    if cols[0] != (x1, x2) or rows[0] != (y1, y2):
        return [(cols[0][0], rows[0][0], cols[0][1], rows[0][1])]
    return [box]


def detect_goods_tiles(frame: Image.Image, *,
                       zone: Optional[Tuple[int, int, int, int]] = None) -> List[GoodsTile]:
    """Every goods card on this frame, left to right, top to bottom.

    `zone` restricts the search to an absolute (x0, y0, x1, y1) box; it defaults to the
    market content area, the SAME fractions `read_market_page_omni` already uses.

    The default is not decoration. The left rail carries cream landscape widgets of its own
    — Trade Points and Language Effect measure 294x131 — and those ARE controls, with their
    own `>` arrows. Calling one a goods tile would make `tile_containing` refuse to press
    it, turning this detector's safety rule into a new way to be stuck. The shape test keeps
    the tall right-hand panel out; the zone keeps the left rail out.
    """
    arr = np.asarray(frame.convert("RGB")).astype(int)
    H, W = arr.shape[:2]
    zx0, zy0, zx1, zy1 = zone or (_ZONE[0] * W, _ZONE[1] * H, _ZONE[2] * W, _ZONE[3] * H)

    # SEARCH THE WHOLE FRAME AND FILTER BY CENTRE — never crop to the zone. The zone's left
    # edge falls at 0.17W = 408 and the leftmost card starts at 358, so cropping SHORTENS the
    # very boundary this detector exists to measure. `read_market_page_omni` can crop because
    # it filters ELEMENTS, whose boxes survive the cut; pixels do not.
    cream = _card_body(arr)

    # NO MORPHOLOGY. `_carve` separates regions by itself — the right-hand panel is parted
    # from the goods by the same background column a card is parted from its neighbour — so
    # the closing and hole-filling this began with bought nothing and cost 300ms a frame,
    # which is far too much to spend on every commit-button check.
    out: List[GoodsTile] = []
    for bx1, by1, bx2, by2 in _carve(cream, (0, 0, W, H)):
        w, h = bx2 - bx1, by2 - by1
        if w < _MIN_W or h < _MIN_H:
            continue
        # A CARD IS LANDSCAPE. This is what keeps the tall cream side panels out without
        # guessing where they start — a property of the layout, not a tuned number.
        if w < h:
            continue
        if w > _MAX_W_FRAC * W or h > _MAX_H_FRAC * H:
            continue                              # a panel or a dialog, not a card
        tile = GoodsTile(x1=bx1, y1=by1, x2=bx2, y2=by2)
        if not (zx0 <= tile.cx <= zx1 and zy0 <= tile.cy <= zy1):
            continue                              # outside the market's content area
        out.append(tile)
    out.sort(key=lambda t: (t.y1, t.x1))
    return out


def tile_containing(tiles: Sequence[GoodsTile], x: float, y: float) -> Optional[GoodsTile]:
    """The card this point falls in, or None.

    A GOODS TILE CONTAINS NO CONTROLS. Anything gold inside one is a banner, a price bar or
    a highlight — a LABEL — and tapping it taps the tile. Callers looking for a button use
    this to refuse a candidate rather than to locate one.
    """
    for t in tiles:
        if t.contains(x, y):
            return t
    return None


@dataclass(frozen=True)
class GoodsGrid:
    """The cards as a grid, shaped like `grid_detector.GridModel` so it can stand in.

    `n_rows`/`n_cols` are what the scroll test asks for — "is this a full 3x3 page, or does
    the hold simply end here?" — so they are COUNTED from the cards rather than inferred.
    """
    cells: List[GoodsTile]
    n_rows: int
    n_cols: int

    def in_reading_order(self) -> List[GoodsTile]:
        return list(self.cells)


def measure_goods_grid(frame: Image.Image, *,
                       zone: Optional[Tuple[int, int, int, int]] = None) -> Optional[GoodsGrid]:
    """`detect_goods_tiles`, arranged into rows and columns. None when there are no cards.

    Rows are grouped on the card top, with a tolerance of a third of a card: cards in one row
    agree on `y1` to within a pixel or two (measured 198/198/198 and 439/439/439), and the
    next row is a full pitch away, so nothing marginal is being decided here.
    """
    tiles = detect_goods_tiles(frame, zone=zone)
    if not tiles:
        return None
    tol = max(8, min(t.h for t in tiles) // 3)
    rows: List[List[GoodsTile]] = []
    for t in tiles:                                # already sorted (y1, x1)
        if rows and abs(t.y1 - rows[-1][0].y1) <= tol:
            rows[-1].append(t)
        else:
            rows.append([t])
    # ROW-MAJOR. Cards in a row agree on `y1` only to within a pixel or two, so a global
    # sort on (y1, x1) can interleave them and the page comes back in a shuffled order.
    for r in rows:
        r.sort(key=lambda t: t.x1)
    return GoodsGrid(cells=[t for r in rows for t in r],
                     n_rows=len(rows), n_cols=max(len(r) for r in rows))
