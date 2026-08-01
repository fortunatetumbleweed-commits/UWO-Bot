# vision/region_detectors/action_buttons.py
#
# Bottom-row action buttons detector for chromed scenes.  Slice 3.
#
# Every chromed flow ends with the player tapping a positive action
# button to commit a transaction:
#
#   Market / Purchase    → "Purchase"
#   Market / Sell        → "Sell"
#   Item Shop / Gear     → "Purchase"
#   Item Shop / Sell     → "Sell"
#   Harbor / Supply      → "Supply" / "Confirm"
#   Harbor / Departure   → "Depart Now" / "Set Sail"
#   Inn / Recruit Crew   → "Recruit"
#   Cathedral / Donate   → "Donate"
#   Bank / Insurance     → "Cancel Insurance" / "Purchase"
#   World Map / Go to City → "Go to City"
#
# Action buttons live in a horizontal row at the bottom-right of the
# screen.  The POSITIVE (gold/yellow) action button is rightmost in
# the row.  Empirically OmniParser classifies these gold buttons with
# element_type="button" — that's the discriminator vs other text
# elements at similar y positions.
#
# Output: ActionButtonsRegion with a list of buttons sorted left-to-
# right.  The rightmost button is marked is_positive=True (it's the
# primary commit action).  Disabled-state detection is best-effort
# and not currently implemented (returns is_enabled=True for all
# detected buttons).

from __future__ import annotations

from typing import List, Optional, Tuple

from vision.omniparser import DetectedElement
from vision.scene_model import ActionButtonsRegion


# ── Region constants (normalised) ─────────────────────────────────────────


# Action-button row sits at the bottom of the screen but above the
# OS chrome (wifi/uid/server) and the Language Effect tooltip.  On a
# 1080-tall frame the row is roughly y ∈ [880, 1020].
_ROW_CY_MIN_NORM = 880.0 / 1080.0
_ROW_CY_MAX_NORM = 1020.0 / 1080.0

# Action buttons live in the right half of the screen — the primary
# commit action is far-right.  We use a generous left bound so we don't
# miss things like "Add All" / "Common 22/87" filter chips that
# accompany the primary button.
_ROW_CX_MIN_NORM = 1300.0 / 2400.0

# Minimum size for an action button.  Excludes tiny icons.
_MIN_BUTTON_WIDTH_NORM = 50.0 / 2400.0
_MIN_BUTTON_HEIGHT_NORM = 25.0 / 1080.0

# Known commit-action verbs.  Text-typed elements in the action row
# whose label matches one of these are accepted as action buttons —
# necessary because OmniParser sometimes splits the gold commit
# button into icon (background) + text (label) and the text doesn't
# always overlap the icon cleanly.
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


# ── Detector ──────────────────────────────────────────────────────────────


def detect_action_buttons(
    elements:     List[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> Optional[ActionButtonsRegion]:
    """Detect bottom-row action buttons in a chromed scene.

    Returns None when no button-typed candidates are present (likely
    an overworld scene or a chromed scene with no committable action).
    Returns an ActionButtonsRegion with `buttons=[]` when candidates
    exist but none satisfy size/zone constraints.
    """
    buttons = _collect_buttons(elements, frame_width, frame_height)
    if not buttons:
        return None

    # Sort left-to-right for stable downstream consumption.
    buttons.sort(key=lambda b: b.cx)

    items: List[dict] = []
    for i, b in enumerate(buttons):
        is_positive = (i == len(buttons) - 1)   # rightmost = primary commit
        items.append({
            "label":       b.label.strip(),
            "bbox":        (b.x1, b.y1, b.x2, b.y2),
            "cx":          b.cx,
            "cy":          b.cy,
            "is_positive": is_positive,
            "is_enabled":  True,        # disabled-state detection: future
        })
    return ActionButtonsRegion(buttons=items)


# ── Implementation helpers ────────────────────────────────────────────────


def _collect_buttons(
    elements:     List[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> List[DetectedElement]:
    """Filter OmniParser elements to plausible action-button candidates.

    Empirically OmniParser produces action buttons in TWO shapes:

      1. Bundled: element_type="button" with the label string inside.
         (Most chromed sub-menus' Purchase/Confirm/Recruit buttons.)

      2. Split: element_type="icon" for the gold button background +
         element_type="text" for the label inside.  Observed on the
         Item Shop / Sell sub-menu — the 'Sell' commit button comes
         back as icon @ (1987, 998) 297x52 + text 'Sell' @ (2239, 1001).

    To catch both shapes:
      - Accept any button-typed element in the action-row zone.
      - For text-typed elements in the zone, accept when an icon-typed
        element overlaps their position (indicating the text is a
        label inside a gold-button background).

    Other filters:
      - cy ∈ action-row zone.
      - cx in right half (action commit is always far-right).
      - Size above minimum thresholds for button-typed.
      - Label has at least one alphabetic character (excludes purely
        numeric labels like '0', '500', price values).
    """
    cy_min = _ROW_CY_MIN_NORM * frame_height
    cy_max = _ROW_CY_MAX_NORM * frame_height
    cx_min = _ROW_CX_MIN_NORM * frame_width
    w_min  = _MIN_BUTTON_WIDTH_NORM * frame_width
    h_min  = _MIN_BUTTON_HEIGHT_NORM * frame_height

    def _in_zone(el: DetectedElement) -> bool:
        return (
            cy_min <= el.cy <= cy_max
            and el.cx >= cx_min
        )

    def _has_alpha_label(el: DetectedElement) -> bool:
        label = (el.label or "").strip()
        return bool(label) and any(c.isalpha() for c in label)

    # Pass 1: native button-typed candidates.
    out: List[DetectedElement] = []
    for el in elements:
        if el.element_type != "button":
            continue
        if not _in_zone(el):
            continue
        if el.width < w_min or el.height < h_min:
            continue
        if not _has_alpha_label(el):
            continue
        out.append(el)

    # Pass 2: known-action-verb pattern.  OmniParser sometimes splits
    # the gold commit button into icon (background) + text (label)
    # where the text does NOT spatially overlap the icon (observed on
    # Item Shop / Sell: icon @ (1987,998) 297x52 sits LEFT of text
    # 'Sell' @ (2239,1001) 58x30 — adjacent, not overlapping).  We
    # accept text-typed elements in the zone when their label is a
    # known commit verb — content-based fallback for split buttons.
    for el in elements:
        if el.element_type != "text":
            continue
        if not _in_zone(el):
            continue
        if not _has_alpha_label(el):
            continue
        label = (el.label or "").strip().lower()
        if label in _COMMIT_ACTION_VERBS:
            out.append(el)

    return out
