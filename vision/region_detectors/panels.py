# vision/region_detectors/panels.py
#
# Panels detector — overworld tabbed right panel + chromed right context
# panel + chromed center panel.  Slice 4.
#
# All three share dialog-like structure: title bar (or tab bar) at the
# top, content body below, optional X close icon at the title's right.
# They differ in:
#   - position on screen (right edge / right context / centre)
#   - whether the "title" is text or a tab-icon row
#   - whether they can have an X close icon (overworld tabbed: no;
#     chromed right context: yes when overlay; chromed center: no)
#
# This module factors the common detection logic into _detect_panel()
# and provides three position-specific wrappers that fill in the right
# region bounds and X-close-allowed flag.
#
# Scope: structural skeleton only.  We detect the panel's existence,
# title text (or tab list), bounds, X-overlay flag, and the list of
# body items when the content is list-shaped.  Item-level field
# extraction (prices, character details, quest descriptions, etc.) is
# per-flow concern, not a slice-4 concern.

from __future__ import annotations

from typing import List, Optional, Tuple

from vision.omniparser import DetectedElement
from vision.scene_model import Panel


# ── Region bounds for each panel type (normalised) ───────────────────────


# Overworld right panel: clings to the right edge of the screen.  Sits
# alongside the season/date strip + tab icon row at top, then the
# active-tab list below.
_OVERWORLD_PANEL_X_MIN_NORM = 2050.0 / 2400.0
_OVERWORLD_PANEL_Y_MIN_NORM = 50.0   / 1080.0
_OVERWORLD_PANEL_Y_MAX_NORM = 1020.0 / 1080.0

# Chromed right context panel: typically starts ~1700 px in.
_CHROMED_RIGHT_X_MIN_NORM = 1700.0 / 2400.0
_CHROMED_RIGHT_Y_MIN_NORM = 30.0   / 1080.0
_CHROMED_RIGHT_Y_MAX_NORM = 1020.0 / 1080.0

# Chromed center panel: between the left menu (cx < 350) and the right
# context panel (cx > 1700).
_CHROMED_CENTER_X_MIN_NORM = 400.0  / 2400.0
_CHROMED_CENTER_X_MAX_NORM = 1700.0 / 2400.0
_CHROMED_CENTER_Y_MIN_NORM = 100.0  / 1080.0
_CHROMED_CENTER_Y_MAX_NORM = 980.0  / 1080.0


# ── Heuristic thresholds ──────────────────────────────────────────────────


# Title bar zone — top fraction of the panel.  Title text typically sits
# within the first ~120 px of the panel's vertical span.
_TITLE_ZONE_HEIGHT_NORM = 120.0 / 1080.0

# Minimum title text height — distinguishes the panel title from body
# row labels.  Same threshold the top-left detector uses.
_TITLE_MIN_HEIGHT_NORM = 28.0 / 1080.0

# X-close icon search constraints.  Icon-typed element, small, at the
# top-right of the title row.
_X_ICON_MAX_WIDTH_NORM = 70.0 / 2400.0
_X_ICON_MAX_HEIGHT_NORM = 70.0 / 1080.0

# Tab icon size constraints for overworld tabbed panel.  Tabs are small
# icons in a horizontal row near the top of the right edge.
_TAB_ICON_MIN_WIDTH_NORM = 40.0 / 2400.0
_TAB_ICON_MAX_WIDTH_NORM = 100.0 / 2400.0
_TAB_ICON_MIN_HEIGHT_NORM = 30.0 / 1080.0
_TAB_ICON_MAX_HEIGHT_NORM = 80.0 / 1080.0


# ── Public API: position-specific wrappers ────────────────────────────────


def detect_overworld_panel(
    elements:     List[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> Optional[Panel]:
    """Detect the tabbed right panel on overworld scenes.

    The overworld right panel has a row of tab icons at the top
    (buildings / characters / tasks / location) and a vertical list of
    items below corresponding to the active tab.  No title text; no X
    close icon (the panel is always shown).
    """
    region = (
        int(_OVERWORLD_PANEL_X_MIN_NORM * frame_width),
        int(_OVERWORLD_PANEL_Y_MIN_NORM * frame_height),
        frame_width,
        int(_OVERWORLD_PANEL_Y_MAX_NORM * frame_height),
    )
    in_region = _filter_in_region(elements, region)
    if not in_region:
        return None

    tabs = _find_tab_icons(in_region, frame_width, frame_height)
    items = _find_list_items(
        in_region, region, frame_width, frame_height,
        # Below tabs — tabs occupy roughly the top 200 px of the region.
        body_y_min=region[1] + int(200.0 / 1080.0 * frame_height),
    )

    return Panel(
        title=None,
        title_bbox=None,
        bbox=region,
        is_overlay=False,    # overworld panel is always-on, never overlay
        tabs=[t.get("label", "") for t in tabs],
        active_tab=None,     # active-tab detection deferred (per slice plan)
        items=items,
    )


def detect_right_panel(
    elements:     List[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> Optional[Panel]:
    """Detect the chromed right context panel.

    Examples: Item Shop / Cart, Inn / Hire, Inn / Party, Item Shop /
    Sell / Sales List, World Map / City Info, Bank / Insurance Info,
    Estate / Contract.

    May or may not have an X close icon — `is_overlay=True` indicates
    the panel is an optional overlay (City Info on World Map).
    Persistent panels (Cart, Hire) have is_overlay=False.
    """
    region = (
        int(_CHROMED_RIGHT_X_MIN_NORM * frame_width),
        int(_CHROMED_RIGHT_Y_MIN_NORM * frame_height),
        frame_width,
        int(_CHROMED_RIGHT_Y_MAX_NORM * frame_height),
    )
    return _detect_panel(
        elements, region, frame_width, frame_height, scan_x_close=True,
    )


def detect_center_panel(
    elements:     List[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> Optional[Panel]:
    """Detect the chromed center content panel.

    Center panels never have X close icons (you cannot dismiss them;
    they're the main content of the chromed sub-menu).  Content varies
    widely — sometimes a list, sometimes a grid, sometimes free-form
    dialogue.  Slice 4 detects the structural skeleton only; per-flow
    content extraction (item grid prices, NPC dialog parsing, …) is
    deferred to flow implementations.
    """
    region = (
        int(_CHROMED_CENTER_X_MIN_NORM * frame_width),
        int(_CHROMED_CENTER_Y_MIN_NORM * frame_height),
        int(_CHROMED_CENTER_X_MAX_NORM * frame_width),
        int(_CHROMED_CENTER_Y_MAX_NORM * frame_height),
    )
    return _detect_panel(
        elements, region, frame_width, frame_height, scan_x_close=False,
    )


# ── Shared implementation ────────────────────────────────────────────────


def _detect_panel(
    elements:     List[DetectedElement],
    region:       Tuple[int, int, int, int],
    frame_width:  int,
    frame_height: int,
    scan_x_close: bool,
) -> Optional[Panel]:
    """Detect a generic title-bar + body panel inside *region*.

    Returns None when nothing is detected; returns a Panel with whatever
    fields could be filled (title may be None for headerless panels).
    """
    in_region = _filter_in_region(elements, region)
    if not in_region:
        return None

    title_zone_h = int(_TITLE_ZONE_HEIGHT_NORM * frame_height)
    title_y_max = region[1] + title_zone_h

    title_candidates = [
        el for el in in_region
        if el.element_type in ("text", "button")
        and el.cy <= title_y_max
        and el.height >= _TITLE_MIN_HEIGHT_NORM * frame_height
        and (el.label or "").strip()
        and any(c.isalpha() for c in (el.label or ""))
    ]

    if not title_candidates:
        # Body-only panel without a title — return a Panel with what we
        # have so consumers can still inspect items.
        items = _find_list_items(in_region, region, frame_width, frame_height)
        if not items:
            return None
        return Panel(bbox=region, items=items)

    # Pick the title: largest-area title-zone text element, leftmost
    # ties broken first.
    title_candidates.sort(
        key=lambda e: (-e.width * e.height, e.cx, e.cy),
    )
    title_el = title_candidates[0]

    is_overlay = False
    if scan_x_close:
        is_overlay = _has_x_close_near_title(
            title_el, in_region, frame_width, frame_height,
        )

    items = _find_list_items(
        in_region, region, frame_width, frame_height,
        body_y_min=title_el.y2 + 10,    # 10 px margin below title
    )

    return Panel(
        title=title_el.label.strip(),
        title_bbox=(title_el.x1, title_el.y1, title_el.x2, title_el.y2),
        bbox=region,
        is_overlay=is_overlay,
        items=items,
    )


def _filter_in_region(
    elements: List[DetectedElement],
    region:   Tuple[int, int, int, int],
) -> List[DetectedElement]:
    """Return elements whose centre lies inside the region rectangle."""
    x1, y1, x2, y2 = region
    return [
        el for el in elements
        if x1 <= el.cx <= x2 and y1 <= el.cy <= y2
    ]


def _has_x_close_near_title(
    title:      DetectedElement,
    in_region:  List[DetectedElement],
    frame_w:    int,
    frame_h:    int,
) -> bool:
    """Detect an X close icon at the right end of the title row.

    Heuristic: small icon-typed element to the right of the title text,
    within the title's vertical band.  No label-content check — X icons
    typically come back as label='icon' or 'x' depending on OmniParser
    state.
    """
    max_w = _X_ICON_MAX_WIDTH_NORM * frame_w
    max_h = _X_ICON_MAX_HEIGHT_NORM * frame_h
    y_tol = 30.0 / 1080.0 * frame_h
    for el in in_region:
        if el.element_type != "icon":
            continue
        if el.width > max_w or el.height > max_h:
            continue
        if el.cx <= title.x2:
            continue          # must be right of the title
        if abs(el.cy - title.cy) > y_tol:
            continue          # must be in title's vertical band
        return True
    return False


def _find_tab_icons(
    in_region: List[DetectedElement],
    frame_w:   int,
    frame_h:   int,
) -> List[dict]:
    """Find tab icons at the top of the overworld right panel.

    Returns list of {label, bbox, cx, cy} per tab icon, sorted left-to-
    right.  Tab icons are small icon-typed elements clustered in a
    horizontal row near the top of the region.  Labels come back as
    "icon" generically (OmniParser doesn't identify tab kinds) — the
    label list is mostly informational.
    """
    w_min = _TAB_ICON_MIN_WIDTH_NORM * frame_w
    w_max = _TAB_ICON_MAX_WIDTH_NORM * frame_w
    h_min = _TAB_ICON_MIN_HEIGHT_NORM * frame_h
    h_max = _TAB_ICON_MAX_HEIGHT_NORM * frame_h

    tabs = []
    for el in in_region:
        if el.element_type != "icon":
            continue
        if not (w_min <= el.width <= w_max):
            continue
        if not (h_min <= el.height <= h_max):
            continue
        # Tab row sits near the top of the right edge — cy < 200 on
        # a 1080-tall frame.
        if el.cy > 200.0 / 1080.0 * frame_h:
            continue
        tabs.append({
            "label": (el.label or "").strip() or "icon",
            "bbox":  (el.x1, el.y1, el.x2, el.y2),
            "cx":    el.cx,
            "cy":    el.cy,
        })
    tabs.sort(key=lambda t: t["cx"])
    return tabs


def _find_list_items(
    in_region:    List[DetectedElement],
    region:       Tuple[int, int, int, int],
    frame_width:  int,
    frame_height: int,
    body_y_min:   Optional[int] = None,
) -> List[dict]:
    """Find list-style items in the panel body.

    A list item is a text/button-typed element with a meaningful label
    (≥2 chars, contains at least one letter, not a known chrome value).
    Items are clustered by cx (same column), and the largest cluster is
    returned in cy order.

    Returns a list of {label, bbox, cx, cy} dicts.
    """
    body_y_min = body_y_min or (region[1] + 50)
    candidates = [
        el for el in in_region
        if el.element_type in ("text", "button")
        and el.cy >= body_y_min
        and (el.label or "").strip()
        and len(el.label.strip()) >= 2
        and any(c.isalpha() for c in el.label)
        # Filter purely numeric labels and very long strings
        and len(el.label.strip()) <= 50
    ]
    if not candidates:
        return []

    # Cluster by cx.  Tolerance: ±80 px on a 2400-wide frame.
    tolerance_x = 80.0 / 2400.0 * frame_width
    candidates.sort(key=lambda e: e.cx)
    clusters: List[List[DetectedElement]] = []
    for el in candidates:
        for c in clusters:
            mean_cx = sum(x.cx for x in c) / len(c)
            if abs(el.cx - mean_cx) <= tolerance_x:
                c.append(el)
                break
        else:
            clusters.append([el])

    # Pick the largest cluster (most rows in a single column).  Ties
    # broken by leftmost mean cx.
    clusters.sort(key=lambda c: (-len(c), sum(x.cx for x in c) / len(c)))
    best = clusters[0]
    best.sort(key=lambda e: e.cy)

    return [
        {
            "label": el.label.strip(),
            "bbox":  (el.x1, el.y1, el.x2, el.y2),
            "cx":    el.cx,
            "cy":    el.cy,
        }
        for el in best
    ]
