"""General grid / list detector — structural regularity from OmniParser elements.

Recognises a repeating layout of same-size cells on a lattice: a grid
(N cols × M rows) or a list (1 col × M rows). No hardcoded coordinates — the
cell size and pitch are derived from the detected elements, so it's
orientation/notch robust and adapts to however many items are shown.

The key idea — how the bot "knows" it's a grid of identical items, the way a
human glances at one: **congruent bounding boxes + regular spacing ⇒ same-layout
cells.** Congruence then IMPLIES a shared internal template (every cell's fields
sit at the same RELATIVE position), which callers use to (a) read each cell
uniformly and (b) re-read a missing field from its known sub-region instead of
guessing — see `GridCell.rel_region`.

Consumers: the market goods grid, and the many other same-layout screens
(item shop, shipyard, mate/port lists, …). See discussion 2026-08-06 and
memory/feedback_perception_mode_dependent_omniparser.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import List, Optional, Tuple


@dataclass
class GridCell:
    row: int
    col: int
    x1: int
    y1: int
    x2: int
    y2: int
    label: str = ""      # the cell element's own label (e.g. a good/item name)

    @property
    def w(self) -> int: return self.x2 - self.x1
    @property
    def h(self) -> int: return self.y2 - self.y1
    @property
    def cx(self) -> int: return (self.x1 + self.x2) // 2
    @property
    def cy(self) -> int: return (self.y1 + self.y2) // 2

    def contains(self, px: float, py: float) -> bool:
        return self.x1 <= px <= self.x2 and self.y1 <= py <= self.y2

    def rel_region(self, rx0: float, ry0: float, rx1: float, ry1: float
                   ) -> Tuple[int, int, int, int]:
        """Absolute (x0,y0,x1,y1) of a sub-region given in cell-relative [0..1].

        Because congruent cells share a layout, a field's relative box is the
        same for every cell — so this locates e.g. the bottom-left index corner
        of ANY cell for a targeted re-read.
        """
        return (int(self.x1 + rx0 * self.w), int(self.y1 + ry0 * self.h),
                int(self.x1 + rx1 * self.w), int(self.y1 + ry1 * self.h))


@dataclass
class GridModel:
    cells: List[GridCell]
    n_rows: int
    n_cols: int
    cell_w: int
    cell_h: int

    @property
    def is_list(self) -> bool:
        return self.n_cols == 1

    def in_reading_order(self) -> List[GridCell]:
        return sorted(self.cells, key=lambda c: (c.row, c.col))


def _cluster_centers(values: List[float], tol: float) -> List[float]:
    """Group 1-D values whose gaps are ≤ tol; return the sorted cluster means."""
    if not values:
        return []
    vs = sorted(values)
    centers: List[float] = []
    group = [vs[0]]
    for v in vs[1:]:
        if v - group[-1] <= tol:
            group.append(v)
        else:
            centers.append(sum(group) / len(group))
            group = [v]
    centers.append(sum(group) / len(group))
    return centers


def _nearest(centers: List[float], v: float) -> int:
    return min(range(len(centers)), key=lambda i: abs(centers[i] - v))


def detect_grid(
    elements,
    frame_w: int,
    frame_h: int,
    zone: Optional[Tuple[float, float, float, float]] = None,
    cell_types: Tuple[str, ...] = ("button",),
    min_cells: int = 4,
    size_tol: float = 0.22,
    size_tol_h: Optional[float] = None,
) -> Optional[GridModel]:
    """Find a grid/list of congruent cells among OmniParser `elements`.

    `zone` is an absolute (x0,y0,x1,y1) content box to restrict to (default whole
    frame). Returns None when fewer than `min_cells` congruent cells arrange into
    a regular lattice.
    """
    zx0, zy0, zx1, zy1 = zone if zone else (0, 0, frame_w, frame_h)
    cands = []
    for e in elements or []:
        if getattr(e, "element_type", "") not in cell_types:
            continue
        cx = getattr(e, "cx", (e.x1 + e.x2) / 2)
        cy = getattr(e, "cy", (e.y1 + e.y2) / 2)
        if not (zx0 <= cx <= zx1 and zy0 <= cy <= zy1):
            continue
        w, h = e.x2 - e.x1, e.y2 - e.y1
        # not a tiny icon, not a whole panel
        if w < 0.06 * frame_w or h < 0.06 * frame_h:
            continue
        if w > 0.65 * frame_w or h > 0.65 * frame_h:
            continue
        cands.append(e)
    if len(cands) < min_cells:
        return None

    # congruence: keep the cells near the median size (the dominant repeat)
    med_w = median([e.x2 - e.x1 for e in cands])
    med_h = median([e.y2 - e.y1 for e in cands])
    # HEIGHT IS THE SOFT AXIS — because DETECTION is unreliable there, not the layout.
    # The tiles are all the same size; what varies is how much of one OmniParser encloses. A
    # market tile wearing a BAZAAR banner comes back 432x181 next to 435x231 neighbours
    # (Bremen 2026-08-24): the WIDTH is dead-on (dw=0.01) and the box is simply clipped
    # short, the banner having tripped the detector. A symmetric tolerance read that clipping
    # as "not part of the repeat" and dropped the one tile the voyage was for.
    # Width is the axis that actually identifies a column, so it stays strict.
    h_tol = size_tol if size_tol_h is None else size_tol_h
    congr = [
        e for e in cands
        if abs((e.x2 - e.x1) - med_w) <= size_tol * med_w
        and abs((e.y2 - e.y1) - med_h) <= h_tol * med_h
    ]
    if len(congr) < min_cells:
        return None

    # lattice: cluster the centers into columns and rows
    col_centers = _cluster_centers([(e.x1 + e.x2) / 2 for e in congr], tol=med_w * 0.5)
    row_centers = _cluster_centers([(e.y1 + e.y2) / 2 for e in congr], tol=med_h * 0.5)

    cells = [
        GridCell(
            row=_nearest(row_centers, (e.y1 + e.y2) / 2),
            col=_nearest(col_centers, (e.x1 + e.x2) / 2),
            x1=int(e.x1), y1=int(e.y1), x2=int(e.x2), y2=int(e.y2),
            label=(getattr(e, "label", "") or "").strip(),
        )
        for e in congr
    ]
    # ONE CELL PER SLOT. OmniParser sometimes returns two overlapping boxes for the same tile
    # — a full one and a clipped one (433x150 beside 438x240 for one Textiles tile, Bremen
    # 2026-08-24). A symmetric height tolerance discarded the clipped twin as a side effect;
    # now that height is deliberately loose so BAZAAR tiles survive, the twin reaches the
    # lattice and the good is read TWICE, which would stage and sell it twice. The box closest
    # to the median cell size is the one that encloses the whole tile.
    best: dict = {}
    for c in cells:
        fit = abs(c.w - med_w) / med_w + abs(c.h - med_h) / med_h
        key = (c.row, c.col)
        if key not in best or fit < best[key][0]:
            best[key] = (fit, c)
    cells = [c for _fit, c in best.values()]

    return GridModel(
        cells=cells, n_rows=len(row_centers), n_cols=len(col_centers),
        cell_w=int(med_w), cell_h=int(med_h),
    )
