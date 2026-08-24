# vision/region_detectors/left_menu.py
#
# Left-side vertical menu detector for chromed scenes.  Slice 2.
#
# Every chromed scene (building, sub_menu) has a vertical column of
# tappable menu items on the left side of the screen:
#
#   Inn:        Hire | Party | Recruit Crew | Employee | Manage Mate
#   Item Shop:  Gear | Tool | Black Market | Sell
#   Cathedral:  Pray | Donate
#   Harbor:     Supply | Repair | Recruit Crew | Departure
#   Bank:       Deposit/Withdrawal | Savings Account | Insurance
#
# This detector replaces the ad-hoc `_collect_menu_candidates` clustering
# in actions/explore_actions.py — it produces the same shape of result
# but reads directly from OmniParser elements (no dependency on the
# inventory/tagged-element pipeline).
#
# Output: LeftMenuRegion with a list of MenuItem entries — each item
# has its label, bbox, and a `locked` flag (set when an 'Unavailable'
# indicator sits in the same column).  Selected-state detection is
# best-effort: highlighted items appear as text-typed elements just
# like unselected ones (the highlight is a left-edge glow that
# OmniParser doesn't classify separately).

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from vision.omniparser import DetectedElement
from vision.scene_model import LeftMenuRegion


# ── Region constants (normalised) ─────────────────────────────────────────


# Outer zone for menu candidates.  Menu items sit at roughly:
#   cx ∈ [150, 280] on a 2400-wide frame  (~6-12% from left)
#   cy ∈ [110, 950] on a 1080-tall frame  (below title chrome, above
#                                          bottom Language Effect chrome)
# We use a slightly more generous zone here and rely on clustering to
# pick the actual menu column.
_ZONE_CX_NORM = 350.0 / 2400.0     # cx < ~14% width
_ZONE_CY_MIN_NORM = 110.0 / 1080.0   # cy > ~10% height (below title)
_ZONE_CY_MAX_NORM = 950.0 / 1080.0   # cy < ~88% height (above bottom chrome)

# Column-clustering tolerance: items in the same column have cx within
# ±80 px of the cluster mean (on a 2400-wide frame).
_CLUSTER_TOLERANCE_X_NORM = 80.0 / 2400.0

# Minimum cluster size to be considered a menu (e.g. Cathedral has 2 items,
# Inn has 5; an isolated label is probably not a menu).
_MIN_CLUSTER_SIZE = 2

# Menu items are narrow text rows, all in the same layout. The bottom-left
# reward / info widgets (e.g. the market's Trade Points / Trade Info / Language
# Effect boxes) are a DIFFERENT layout — full-panel-width filled boxes — and are
# NOT menu items. A cluster whose typical member is that wide is a reward/info
# group, not the menu, so it's deprioritised when picking the menu column.
# (Origin 2026-08-06: on the market trading view these boxes outnumbered the
# real Purchase/Sell menu and were wrongly returned as the menu.)
_REWARD_BOX_WIDTH_NORM = 260.0 / 2400.0

# Lock-indicator search tolerance.  A label is "locked" when an
# 'Unavailable' / 'Locked' element is within this Y radius AND within
# this X radius of the menu item's centre.
_LOCK_INDICATOR_Y_TOL_NORM = 50.0 / 1080.0
_LOCK_INDICATOR_X_TOL_NORM = 150.0 / 2400.0

# Strings on UI labels that indicate the adjacent menu item is locked.
_LOCK_KEYWORDS = (
    "unavailable", "locked", "requires", "not available",
)


# ── MenuItem ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MenuItem:
    """One vertical menu entry — label, position, optional locked flag."""
    label:    str
    bbox:     Tuple[int, int, int, int]
    cx:       int
    cy:       int
    is_locked:   bool = False
    is_selected: bool = False        # best-effort; often unreliable


# ── Detector ──────────────────────────────────────────────────────────────


def detect_left_menu(
    elements:     List[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> Optional[LeftMenuRegion]:
    """Detect the vertical left menu in a chromed scene.

    Returns None when no candidates are present at all (likely an
    overworld scene or empty frame).  Returns a LeftMenuRegion with
    `items=[]` when candidates exist but none cluster into a real
    menu column (e.g. only chrome elements in the zone).
    """
    candidates = _collect_candidates(elements, frame_width, frame_height)
    if not candidates:
        return None

    cluster = _largest_left_column(candidates, frame_width)
    if not cluster or len(cluster) < _MIN_CLUSTER_SIZE:
        return LeftMenuRegion(items=[])

    cluster.sort(key=lambda e: e.cy)

    lock_indicators = _find_lock_indicators(elements, frame_width, frame_height)

    items: List[dict] = []
    for el in cluster:
        locked = _is_locked(el, lock_indicators, frame_width, frame_height)
        items.append({
            "label":       el.label.strip(),
            "bbox":        (el.x1, el.y1, el.x2, el.y2),
            "cx":          el.cx,
            "cy":          el.cy,
            "is_locked":   locked,
            "is_selected": False,
        })

    # Selected item = the one whose label matches the SCREEN TITLE. Game rule:
    # selecting a menu item highlights it (golden background) AND sets the
    # top-left title to its label (e.g. 'Purchase'). The title is a reliable
    # OCR signal, so we use it as the primary selection cue (the golden
    # highlight is the visual corroboration). Origin 2026-08-06.
    title = _find_screen_title(elements, frame_width, frame_height)
    if title:
        best_i, best_r = None, 0.0
        for i, it in enumerate(items):
            r = _title_ratio(it["label"], title)
            if r > best_r:
                best_i, best_r = i, r
        if best_i is not None and best_r >= 0.8:
            items[best_i]["is_selected"] = True

    return LeftMenuRegion(items=items)


def _norm_label(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def _title_ratio(label: str, title: str) -> float:
    """Similarity of a menu label to the screen title (1.0 = clear match)."""
    from difflib import SequenceMatcher
    a, b = _norm_label(label), _norm_label(title)
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _find_screen_title(
    elements: List[DetectedElement], frame_width: int, frame_height: int,
) -> str:
    """The screen title: the top-most text/button in the top-left corner, ABOVE
    the menu zone (e.g. the '‹ Purchase' header). Empty string if none."""
    cx_max = 0.30 * frame_width
    cy_max = _ZONE_CY_MIN_NORM * frame_height   # above the menu candidate zone
    cands = [
        e for e in elements
        if e.element_type in ("text", "button")
        and (e.label or "").strip()
        and any(c.isalpha() for c in e.label)
        and e.cx < cx_max and e.cy < cy_max
    ]
    if not cands:
        return ""
    cands.sort(key=lambda e: (e.cy, e.cx))   # top-most, then left-most
    return cands[0].label.strip()


# ── Implementation helpers ────────────────────────────────────────────────


def _collect_candidates(
    elements:     List[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> List[DetectedElement]:
    """Filter OmniParser elements down to plausible menu-item candidates.

    Filters:
      - text or button element type (menu items are clickable text).
      - label has ≥ 2 chars AND contains at least one letter (excludes
        bare numerics like '20' which are usually currency / counts).
      - label is ≤ 30 chars (menu labels are short).
      - cx within the left zone (cx < ~14% width).
      - cy below title chrome and above bottom Language-Effect strip.
    """
    cx_max = _ZONE_CX_NORM * frame_width
    cy_min = _ZONE_CY_MIN_NORM * frame_height
    cy_max = _ZONE_CY_MAX_NORM * frame_height

    out: List[DetectedElement] = []
    for el in elements:
        if el.element_type not in ("text", "button"):
            continue
        label = (el.label or "").strip()
        if not (2 <= len(label) <= 30):
            continue
        if not any(c.isalpha() for c in label):
            continue
        if el.cx > cx_max:
            continue
        if not (cy_min <= el.cy <= cy_max):
            continue
        # Reject label tokens that are clearly lock indicators — they
        # belong in lock_indicators, not items.
        if label.lower() in _LOCK_KEYWORDS:
            continue
        out.append(el)
    return out


def _largest_left_column(
    candidates: List[DetectedElement],
    frame_width: int,
) -> Optional[List[DetectedElement]]:
    """Cluster candidates by their LEFT EDGE and return the largest cluster.

    The largest cluster (most members) is the menu column.  Ties broken
    by mean left edge (leftmost wins).

    Clustering by CENTRE splits a real menu whenever OmniParser boxes its rows
    inconsistently, which it does routinely. Measured on the Market greeting page
    (2026-08-23):

        Purchase   x1=0   width=354   cx=177     <- full-row box
        Sell       x1=31  width=68    cx=65      <- tight box around the word

    Same menu, two conventions: the centres are 112px apart, past the 80px tolerance, so the
    two items landed in different clusters and the detector returned a single-item column —
    reporting NO MENU on a screen whose menu is a clean, evenly spaced column. Menu rows are
    LEFT-ALIGNED, so their left edges agree (0 vs 31) whatever the box width.
    """
    if not candidates:
        return None

    tolerance_x = _CLUSTER_TOLERANCE_X_NORM * frame_width
    candidates = sorted(candidates, key=lambda e: e.x1)

    clusters: List[List[DetectedElement]] = []
    for el in candidates:
        for c in clusters:
            mean_x1 = sum(x.x1 for x in c) / len(c)
            if abs(el.x1 - mean_x1) <= tolerance_x:
                c.append(el)
                break
        else:
            clusters.append([el])

    # Pick the MENU column: a cluster of narrow text rows, not the wide
    # reward/info boxes. Deprioritise reward-box clusters (typical member spans
    # the full panel), then prefer more members, then leftmost. This keeps the
    # real menu even when the reward widgets outnumber it.
    box_w = _REWARD_BOX_WIDTH_NORM * frame_width

    def _is_reward_cluster(c: List[DetectedElement]) -> bool:
        widths = sorted(e.x2 - e.x1 for e in c)
        return widths[len(widths) // 2] >= box_w   # median member is full-width

    clusters.sort(key=lambda c: (_is_reward_cluster(c), -len(c),
                                 sum(x.cx for x in c) / len(c)))
    return clusters[0]


def _find_lock_indicators(
    elements:     List[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> List[Tuple[int, int]]:
    """Return the (cx, cy) of every lock-indicator element on the frame.

    A lock indicator is a text element whose label contains one of
    `_LOCK_KEYWORDS`.  We scan the WHOLE frame (not just the menu zone)
    because the Unavailable badge may overlap the menu region slightly
    off-axis.
    """
    out: List[Tuple[int, int]] = []
    for el in elements:
        if el.element_type not in ("text", "button"):
            continue
        label = (el.label or "").lower()
        if any(kw in label for kw in _LOCK_KEYWORDS):
            out.append((el.cx, el.cy))
    return out


def _is_locked(
    item:        DetectedElement,
    indicators:  List[Tuple[int, int]],
    frame_width: int,
    frame_height: int,
) -> bool:
    """An item is locked if a lock indicator is in its same UI cluster.

    'Same cluster' means within ±50 px Y and ±150 px X (on a 2400×1080
    frame).  The X bound prevents right-panel Unavailable badges (e.g.
    Inn's empty Hire panel) from incorrectly locking left-strip items
    in the same Y band.
    """
    y_tol = _LOCK_INDICATOR_Y_TOL_NORM * frame_height
    x_tol = _LOCK_INDICATOR_X_TOL_NORM * frame_width
    for ix, iy in indicators:
        if abs(item.cy - iy) <= y_tol and abs(item.cx - ix) <= x_tol:
            return True
    return False
