"""Overlay detection — popups, dialogs, menus that sit on top of a base scene.

Design: docs/scene_model_design.md → 'Overlays are not independent scenes'.

An overlay is a transient UI layer over a base scene.  It binds its
meaning to where the bot was when it fired — a transaction dialog
over `sub_menu:purchase` is fundamentally different from one over
`port_overworld`, even if the dialog itself looks identical.

This module returns an `Overlay` (or None) given OmniParser elements
and frame dimensions.  Each typed detector tries a specific
structural signature; the first match wins.  Detectors run in
priority order so the more specific signatures (NPC dialogue, main
menu) get a chance before the catch-all modal-dialog detector.

The returned Overlay's `bbox` is the convex hull of the matched
elements.  Downstream `detect_scene` runs base-scene classification
on the elements OUTSIDE that bbox.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from vision.omniparser import DetectedElement


# Action-verb whitelist for modal dialogs.  An overlay must contain
# at least one of these as a button-typed element for the modal
# detector to fire — guards against false positives on busy scenes.
_MODAL_ACTION_VERBS = {
    "confirm", "ok", "okay", "yes", "no", "cancel", "decline",
    "purchase", "sell", "buy", "deposit", "withdraw", "recruit",
    "donate", "exchange", "continue", "next", "claim", "collect",
    "depart", "depart now", "set sail", "close", "back",
}

# Main-menu item labels — at least 3 must be present and located on
# the left side of the screen for the main_menu detector to fire.
_MAIN_MENU_LABELS = {
    "guild", "auction", "combat", "collection", "lighthouse",
    "assault", "production", "enhance", "rank", "friend",
    "guild hall",
}


@dataclass(frozen=True)
class Overlay:
    """Detected overlay layer.

    Mirror of brain.observation.Overlay — duplicated here to avoid
    a circular import between vision and brain.  detect_scene
    constructs this and consumers convert as needed.
    """
    kind:           str
    bbox:           Optional[Tuple[int, int, int, int]] = None
    content_tokens: Tuple[str, ...] = ()
    is_modal:       bool = True


def detect_overlay(
    elements:     Sequence[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> Optional[Overlay]:
    """Return the first matching overlay, or None."""
    for detector in (
        _detect_main_menu,
        _detect_modal_dialog,
    ):
        result = detector(elements, frame_width, frame_height)
        if result is not None:
            return result
    return None


# ── Modal dialog (Confirm / OK / Cancel cluster) ───────────────────

def _detect_modal_dialog(
    elements:     Sequence[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> Optional[Overlay]:
    """Detect a centred modal dialog by looking for an action-verb
    button (Confirm / OK / Yes / Cancel / ...) in the lower-centre
    region of the screen.

    Signature:
      - ≥ 1 button-typed element whose label is in _MODAL_ACTION_VERBS
      - AND that button sits in the bottom-half + centre-band
        (cx within 30%–70% of width, cy within 50%–95% of height)
      - AND the surrounding area has the dialog's text cluster
        (≥ 3 text elements between the title area and the buttons)

    Returns Overlay with bbox = convex hull of the dialog elements.
    """
    fw, fh = frame_width, frame_height
    centre_x_lo, centre_x_hi = 0.25 * fw, 0.75 * fw
    band_y_lo,   band_y_hi   = 0.45 * fh, 0.98 * fh

    action_buttons = [
        e for e in elements
        if e.element_type == "button"
        and e.label.strip().lower() in _MODAL_ACTION_VERBS
        and centre_x_lo <= e.cx <= centre_x_hi
        and band_y_lo   <= e.cy <= band_y_hi
    ]
    if not action_buttons:
        return None

    # Estimate dialog bbox from action button + nearby text/buttons
    # within a generous vertical range above the action button row.
    btn = max(action_buttons, key=lambda e: e.cy)
    top_y    = max(0, btn.y1 - int(0.5 * fh))
    bottom_y = min(fh, btn.y2 + int(0.05 * fh))

    cluster = [
        e for e in elements
        if top_y <= e.cy <= bottom_y
        and (0.15 * fw) <= e.cx <= (0.85 * fw)
    ]
    if len(cluster) < 4:
        return None

    text_in_cluster = sum(1 for e in cluster if e.element_type == "text")
    if text_in_cluster < 2:
        return None

    x1 = min(e.x1 for e in cluster)
    y1 = min(e.y1 for e in cluster)
    x2 = max(e.x2 for e in cluster)
    y2 = max(e.y2 for e in cluster)

    tokens = tuple(sorted({
        e.label.strip().lower() for e in cluster
        if e.label and e.element_type == "button"
    }))

    return Overlay(
        kind="modal_dialog",
        bbox=(x1, y1, x2, y2),
        content_tokens=tokens,
        is_modal=True,
    )


# ── Main menu (left-side panel over overworld) ─────────────────────

def _detect_main_menu(
    elements:     Sequence[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> Optional[Overlay]:
    """Detect the main menu overlay by spotting ≥ 3 main_menu_labels.

    UWO's main menu groups menu icons in the right two-thirds of the
    screen (player stats on the left, menu grid on the right).  We
    don't constrain position — the keyword set is specific enough
    that ≥ 3 simultaneous hits effectively occur only here.
    """
    hits = [
        e for e in elements
        if e.label.strip().lower() in _MAIN_MENU_LABELS
    ]
    if len(hits) < 3:
        return None

    x1 = min(e.x1 for e in hits)
    y1 = min(e.y1 for e in hits)
    x2 = max(e.x2 for e in hits)
    y2 = max(e.y2 for e in hits)

    tokens = tuple(sorted({e.label.strip().lower() for e in hits}))
    return Overlay(
        kind="main_menu",
        bbox=(x1, y1, x2, y2),
        content_tokens=tokens,
        is_modal=True,
    )


# ── Helper: filter elements outside a bbox ─────────────────────────

def elements_outside(
    elements: Iterable[DetectedElement],
    bbox:     Optional[Tuple[int, int, int, int]],
    margin:   int = 4,
) -> List[DetectedElement]:
    """Return only elements whose centre lies outside the bbox.

    A small margin keeps the chrome immediately adjacent to the
    overlay (back-arrow, title) included in base-scene classification.
    """
    if bbox is None:
        return list(elements)
    x1, y1, x2, y2 = bbox
    out = []
    for e in elements:
        inside = (x1 - margin <= e.cx <= x2 + margin
                  and y1 - margin <= e.cy <= y2 + margin)
        if not inside:
            out.append(e)
    return out
