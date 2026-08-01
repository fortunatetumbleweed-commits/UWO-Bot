# vision/region_detectors/composite_buttons.py
#
# Composite-button region detector.  Slice 3.5.
#
# UWO uses a recurring "gold composite button" pattern extensively:
# a horizontal pill-shaped yellow button with three parts in left-to-
# right order:
#
#     [currency icon] [cost number] [action verb]
#
# Together they form ONE tappable clickable.  Examples:
#
#   Item Shop / Sell        — [coin] 0       Sell
#   Item Shop / Gear        — [coin] 43,200  Purchase
#   Cathedral / Pray cards  — [coin] 75      (or "Free")
#   Inn / Recruit Crew      — [coin] X gold  Recruit
#   Bank / Insurance        — [coin] X       Purchase
#   World Map / City Info   — [coin] X       Go to City
#   Shipyard / Repair       — [coin] X       Repair
#
# OmniParser does NOT package this as a single element.  Depending on
# the frame it may return:
#   (a) [button] '<verb>' with the icon+number fused into the bbox
#   (b) [icon] icon  +  [text] '<number>'  +  [text] '<verb>'  separately
#   (c) some mix
#
# This detector recognises the pattern from OmniParser elements alone:
# it walks left from each action-verb text/button element, collects
# any adjacent numeric and icon elements, and composes them into one
# CompositeButton record with a union bbox.  Currency-icon discrimination
# is NOT performed in this slice — see TODO below.
#
# TODO(future-slice): identify WHICH currency the icon represents
# (gold / blue_gem / red_gem / stamina).  Approach: sample the dominant
# hue inside the icon's bbox and map color to currency.  OmniParser does
# NOT provide semantic labels for currency icons (verified 2026-05-17):
# the icons either come back as label='icon' (fast path) or with
# hallucinated Florence-2 captions (slow path).  Pixel-color sampling
# is the cheapest reliable path; expect ~30 lines to add.

from __future__ import annotations

from typing import List, Optional, Tuple

from vision.omniparser import DetectedElement
from vision.scene_model import CompositeButton, CompositeButtonsRegion


# ── Constants ─────────────────────────────────────────────────────────────


# Known commit-action verbs.  These are the canonical action labels that
# appear on the rightmost side of a composite button.  Shared with the
# action_buttons detector — keeping the list here too keeps composite-
# button detection self-contained.
_COMMIT_ACTION_VERBS = frozenset({
    "purchase", "sell", "buy",
    "confirm", "ok", "yes", "accept",
    "recruit", "hire",
    "donate", "pray",
    "depart", "depart now", "set sail",
    "go to city", "go to port", "sail",
    "supply", "repair", "restore", "modify",
    "build", "assemble", "dismantle",
    "invest", "withdraw", "deposit",
    "cancel insurance",
})

# Maximum horizontal distance (in normalised frame width) to search left
# of the action verb for the cost number and icon.  Composite buttons
# are ~200-400 px wide on a 2400-wide frame; 300 px is generous enough
# to catch the cost+icon without sweeping in unrelated elements.
_LEFT_SEARCH_DISTANCE_NORM = 350.0 / 2400.0

# Vertical alignment tolerance — cost and icon must be within ±25 px Y
# of the action verb (on a 1080-tall frame) to count as part of the
# same composite button.
_VERTICAL_TOL_NORM = 25.0 / 1080.0

# Minimum cy for action-verb candidates.  The chromed-scene title bar
# (cy < ~110) often contains the same text as a commit verb (e.g. the
# title 'Sell' on the Item Shop / Sell sub-menu, the title 'Pray' on
# the Cathedral / Pray sub-menu).  Title-bar verbs are NOT composite
# buttons — they're chrome.  Filter them out by cy.
_VERB_MIN_CY_NORM = 110.0 / 1080.0


# ── Detector ──────────────────────────────────────────────────────────────


def detect_composite_buttons(
    elements:     List[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> Optional[CompositeButtonsRegion]:
    """Detect all composite gold-button patterns on the frame.

    Returns None when no action verbs are present (likely an overworld
    scene or empty frame).  Returns CompositeButtonsRegion(buttons=[])
    when verbs exist but none compose into a valid composite.
    """
    verbs = _find_action_verbs(elements, frame_height)
    if not verbs:
        return None

    buttons: List[CompositeButton] = []
    for verb in verbs:
        composite = _build_composite_for_verb(
            verb, elements, frame_width, frame_height,
        )
        if composite is not None:
            buttons.append(composite)

    if not buttons:
        return CompositeButtonsRegion(buttons=[])

    # Sort by reading order (top-to-bottom then left-to-right) for
    # stable downstream consumption.
    buttons.sort(key=lambda b: (b.bbox[1], b.bbox[0]))

    # Mark the rightmost-bottommost composite as is_positive=True (the
    # primary commit action when multiple composites coexist on screen,
    # e.g. Cathedral with Pray + Donate cards).
    primary = max(buttons, key=lambda b: (b.bbox[3], b.bbox[2]))
    buttons = [
        CompositeButton(
            action_label=b.action_label,
            cost_value=b.cost_value,
            cost_currency=b.cost_currency,
            icon_present=b.icon_present,
            bbox=b.bbox,
            is_positive=(b is primary),
        )
        for b in buttons
    ]

    return CompositeButtonsRegion(buttons=buttons)


# ── Implementation helpers ────────────────────────────────────────────────


def _find_action_verbs(
    elements: List[DetectedElement], frame_height: int,
) -> List[DetectedElement]:
    """Find every text/button element whose label is a known commit verb
    AND that sits below the chrome title row.

    The title row often contains text matching a commit verb (e.g. the
    title 'Sell' on the Item Shop / Sell sub-menu).  Those are chrome,
    not composite buttons.  The cy filter excludes them.
    """
    cy_min = _VERB_MIN_CY_NORM * frame_height
    out: List[DetectedElement] = []
    for el in elements:
        if el.element_type not in ("text", "button"):
            continue
        if el.cy < cy_min:
            continue
        label = (el.label or "").strip().lower()
        if label in _COMMIT_ACTION_VERBS:
            out.append(el)
    return out


def _build_composite_for_verb(
    verb:         DetectedElement,
    elements:     List[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> Optional[CompositeButton]:
    """Compose a CompositeButton starting from an action verb element.

    Walks LEFT from the verb within the search distance, picking the
    nearest numeric text (cost) and the nearest icon-typed element
    (currency icon) that are roughly vertically aligned with the verb.
    Either or both may be absent — a plain action button without a cost
    (like "Confirm" / "Cancel") is still returned as a CompositeButton
    with cost_value=None and icon_present=False.
    """
    x_search_min = verb.x1 - _LEFT_SEARCH_DISTANCE_NORM * frame_width
    y_tol = _VERTICAL_TOL_NORM * frame_height

    cost: Optional[DetectedElement] = None
    icon: Optional[DetectedElement] = None

    for el in elements:
        if el is verb:
            continue
        # Must be left of the verb and within search distance
        if not (x_search_min <= el.cx < verb.x1):
            continue
        # Must be roughly aligned vertically with the verb
        if abs(el.cy - verb.cy) > y_tol:
            continue

        label = (el.label or "").strip()

        # Numeric text → candidate cost
        if (el.element_type == "text"
                and label
                and _looks_like_cost(label)):
            if cost is None or el.cx > cost.cx:
                cost = el

        # Icon (generic) → candidate currency icon
        if el.element_type == "icon":
            if icon is None or el.cx > icon.cx:
                icon = el

    bbox = _union_bbox(verb, cost, icon)
    cost_value = _parse_cost(cost.label) if cost is not None else None

    return CompositeButton(
        action_label=verb.label.strip(),
        cost_value=cost_value,
        cost_currency=None,           # TODO: pixel-color discrimination
        icon_present=icon is not None,
        bbox=bbox,
        is_positive=False,             # set by the caller after sort
    )


def _looks_like_cost(label: str) -> bool:
    """A cost label is digits and commas, optionally with thousands
    separators, OR the literal 'Free' / 'FREE'."""
    s = label.strip().replace(",", "")
    if s.lower() == "free":
        return True
    return s.isdigit() and len(s) <= 12


def _parse_cost(label: str) -> Optional[int]:
    """Parse a cost label into an integer.  'Free' → 0; numeric → int."""
    s = label.strip()
    if s.lower() == "free":
        return 0
    s = s.replace(",", "")
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return None
    return None


def _union_bbox(
    verb: DetectedElement,
    cost: Optional[DetectedElement],
    icon: Optional[DetectedElement],
) -> Tuple[int, int, int, int]:
    """Compute the union bounding box of the verb plus its cost and icon."""
    members = [verb] + [m for m in (cost, icon) if m is not None]
    x1 = min(m.x1 for m in members)
    y1 = min(m.y1 for m in members)
    x2 = max(m.x2 for m in members)
    y2 = max(m.y2 for m in members)
    return (x1, y1, x2, y2)
