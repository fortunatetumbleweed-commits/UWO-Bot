"""Structural obstruction classifier — Layer A of the goal-aware
perception redesign.

Pure OmniParser-driven detection of what KIND of UI obstruction is on
screen.  No keyword matching, no LLM.  Classifies into one of:

  - ``none``    — normal screen, no obstruction
  - ``dialog``  — centred modal frame with an action button row
  - ``overlay`` — full-screen translucent overlay with central NPC
                  speech bubble; underlying screen still visible
  - ``popup``   — small bounded frame with a close-X (event banner,
                  perk popup, attendance reminder)

Layer A's job is to recognise that *some* obstruction is present and
roughly where it sits, so downstream layers (B: semantic understanding,
C: goal-aware action selection) can interpret it.  The classifier
returns the bounding box of the obstruction so the LLM call in Layer B
can be scoped to just that region.

Origin: 2026-05-15.  The flat 27-entry interruptor registry was
false-firing across every building view because pure substring keyword
matching can't tell a modal dialog apart from the underlying screen's
normal content.  This classifier provides a structural pre-filter that
Layer B and the legacy keyword detector can both consult.

See `feedback_goal_aware_perception_design.md` for the full layered
architecture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Roles whose elements are CHROME or player-UI — never part of an
# obstruction.  Filtering them out before the detectors run prevents
# the "Happy 2026" appellation, NPC speech bubbles, the player
# nameplate, building nameplates, and right-panel building rows from
# being misread as popup/dialog/overlay signals.
#
# `building_nameplate` is critical here: it's a floating sign above
# the entrance that LOOKS like a small popup with a close-X (the
# decorative frame), but it's the *target* of navigation, not an
# obstruction.  Tapping it enters the building (Phase nameplate-tap
# step).
from vision.element_postprocess import (
    ROLE_APPELLATION, ROLE_BUILDING_NAMEPLATE, ROLE_BUILDING_TITLE,
    ROLE_BUILD_INFO, ROLE_CHROME_ICON, ROLE_CURRENCY_LABEL,
    ROLE_DATE_TIME, ROLE_EVENT_BANNER, ROLE_HAMBURGER,
    ROLE_LOCKED_INDICATOR, ROLE_MODE_TAB, ROLE_NOTIFICATION_DOT,
    ROLE_NPC_BUBBLE, ROLE_PHONE_OS, ROLE_PLAYER_NAMEPLATE,
    ROLE_PORT_NAME, ROLE_PROGRESS_BAR, ROLE_RIGHT_PANEL_ROW,
    ROLE_RIGHT_PANEL_TAB, ROLE_BACK_ARROW,
)

_OBSTRUCTION_NOISE_ROLES = frozenset({
    ROLE_APPELLATION,
    ROLE_BACK_ARROW,
    ROLE_BUILDING_NAMEPLATE,
    ROLE_BUILDING_TITLE,
    ROLE_BUILD_INFO,
    ROLE_CHROME_ICON,
    ROLE_CURRENCY_LABEL,
    ROLE_DATE_TIME,
    ROLE_EVENT_BANNER,
    ROLE_HAMBURGER,
    ROLE_LOCKED_INDICATOR,
    ROLE_MODE_TAB,
    ROLE_NOTIFICATION_DOT,
    ROLE_NPC_BUBBLE,
    ROLE_PHONE_OS,
    ROLE_PLAYER_NAMEPLATE,
    ROLE_PORT_NAME,
    ROLE_PROGRESS_BAR,
    ROLE_RIGHT_PANEL_ROW,
    ROLE_RIGHT_PANEL_TAB,
})


def _foreground_elements(inventory):
    """Return raw DetectedElement objects whose role is NOT chrome/UI.

    The obstruction classifier consumes raw element geometry so its
    existing size/position checks (bbox, cx/cy ranges, panel-vs-button
    cap) keep working unchanged — but it now ignores tokens that the
    role tagger identified as chrome.

    Falls back to the full raw list when the inventory lacks tagged
    roles (defensive — happens in stub inventories used by older
    tests)."""
    if not getattr(inventory, "tagged", None):
        return inventory.raw_elements
    return [
        t.raw for t in inventory.tagged
        if t.role not in _OBSTRUCTION_NOISE_ROLES
    ]


# ── Result types ────────────────────────────────────────────────────────────


# Use plain string constants instead of an Enum so the result is
# trivially JSON-serializable and easy to log.
KIND_NONE    = "none"
KIND_DIALOG  = "dialog"
KIND_OVERLAY = "overlay"
KIND_POPUP   = "popup"

VALID_KINDS = frozenset({KIND_NONE, KIND_DIALOG, KIND_OVERLAY, KIND_POPUP})


@dataclass
class ObstructionResult:
    """The classifier's verdict on what's blocking the underlying screen."""

    kind:        str                                    # one of KIND_*
    bbox:        Optional[Tuple[int, int, int, int]] = None    # (x1, y1, x2, y2) — pixel coords
    confidence:  str = "medium"                         # "high" / "medium" / "low"
    signals:     List[str] = field(default_factory=list)  # which heuristics fired

    @property
    def is_obstructed(self) -> bool:
        return self.kind != KIND_NONE


# ── Heuristic thresholds ────────────────────────────────────────────────────
#
# All tuned against the labelled frames in this repo (Harbor and Inn
# crew_hired overlays, Bureau Invest false-fires, Cathedral Pray
# sub-menu).  Adjust as more obstruction kinds are observed.

# Overlay — large central icon (NPC sprite + speech bubble area).
# Lower bounds avoid matching mini-icons; upper bound avoids matching
# whole-screen splash backgrounds.
_OVERLAY_ICON_MIN_W = 600
_OVERLAY_ICON_MIN_H = 200
_OVERLAY_ICON_MAX_W = 1700
_OVERLAY_ICON_MAX_H = 700
_OVERLAY_ICON_CX_RANGE = (0.30, 0.70)   # normalised — centred
_OVERLAY_ICON_CY_RANGE = (0.25, 0.70)

# Overlay — vertical text pair (NPC name above, body below) inside the
# central speech-bubble zone.
_OVERLAY_TEXT_ZONE = (0.30, 0.55, 0.65, 0.85)   # normalised
_OVERLAY_PAIR_MIN_GAP = 30      # px
_OVERLAY_PAIR_MAX_GAP = 130

# Dialog — bounded centred cluster of tappable elements (buttons +
# text) with a HORIZONTAL action button row at the bottom (≥ 2 buttons
# at similar cy).
_DIALOG_CLUSTER_ZONE = (0.20, 0.20, 0.80, 0.85)   # normalised
_DIALOG_MIN_CLUSTER_ELEMENTS = 3                  # at least button + title + body
_DIALOG_ROW_Y_TOLERANCE      = 60                 # px — buttons sharing a row
_DIALOG_MAX_WIDTH            = 1700               # px — modal-sized cap
_DIALOG_MAX_HEIGHT           = 800                # px

# Popup — small bounded frame near top-right with a close-X icon.
# Zone deliberately excludes the chrome icon row at cy < 0.08
# (currency / settings / mail icons at the very top of every screen).
_POPUP_CLOSE_X_ZONE  = (0.65, 0.08, 1.00, 0.35)   # normalised
# Body cluster must fit within a popup-sized region around the close-X.
# Tightened from (1400, 800) → (800, 700) after 2026-05-15 false-fire
# where the right panel + central in-world icons matched the body
# criteria across a 1316×869 bbox.  The bbox cap below is the
# primary defense; these reach values are a permissive first filter.
_POPUP_BODY_X_REACH  = 800
_POPUP_BODY_Y_REACH  = 700
# Hard cap on the resulting popup bbox — primary false-positive
# rejector.  Real popups in UWO (perk event, attendance, daily news,
# event banners) all fit within ~900×700.  Anything bigger is chrome
# + content masquerading as a popup, not a real one.
_POPUP_MAX_BBOX_W    = 1000
_POPUP_MAX_BBOX_H    = 700


# ── Geometry helpers ────────────────────────────────────────────────────────


def _in_norm_zone(cx: int, cy: int, zone, fw: int, fh: int) -> bool:
    l, t, r, b = zone
    return l * fw <= cx <= r * fw and t * fh <= cy <= b * fh


def _bbox_from_elements(elements) -> Optional[Tuple[int, int, int, int]]:
    if not elements:
        return None
    x1 = min(e.x1 for e in elements)
    y1 = min(e.y1 for e in elements)
    x2 = max(e.x2 for e in elements)
    y2 = max(e.y2 for e in elements)
    return (x1, y1, x2, y2)


# ── Per-kind detectors ──────────────────────────────────────────────────────


def _detect_overlay(inventory) -> Optional[ObstructionResult]:
    """Full-screen translucent overlay — typically NPC sprite + speech.

    Two-part signature:
      1.  A large central icon element (the NPC sprite cluster).  Its
          bbox dimensions sit between button-sized and full-screen.
      2.  At least two text elements forming a vertical pair inside
          the speech-bubble zone — the NPC name above, body below.

    The presence of (2) alone is the strongest signal: NPC speech
    overlays always show paired text, and that pattern doesn't occur
    on normal building views (sub-menu titles are single-line at the
    top-left, not stacked in the centre).
    """
    fw, fh = inventory.frame_dims
    foreground = _foreground_elements(inventory)
    signals: List[str] = []

    # Step 1: vertical text pair inside the speech-bubble zone.
    bubble_texts = [
        e for e in foreground
        if e.element_type == "text"
        and (e.label or "").strip()
        and _in_norm_zone(e.cx, e.cy, _OVERLAY_TEXT_ZONE, fw, fh)
    ]
    if len(bubble_texts) < 2:
        return None

    bubble_texts.sort(key=lambda e: e.cy)
    pair_top = pair_bot = None
    for i, top in enumerate(bubble_texts):
        for bot in bubble_texts[i + 1:]:
            gap = bot.cy - top.cy
            if _OVERLAY_PAIR_MIN_GAP <= gap <= _OVERLAY_PAIR_MAX_GAP:
                pair_top, pair_bot = top, bot
                break
        if pair_top is not None:
            break
    if pair_top is None:
        return None
    signals.append(
        f"vertical_pair=({pair_top.label!r}@{pair_top.cy},"
        f"{pair_bot.label!r}@{pair_bot.cy})"
    )

    # Step 2: REQUIRE a large central icon (NPC sprite cluster).  Text
    # pair alone is too weak — building main views also have central
    # NPC sprites with name+body speech that structurally match a
    # vertical pair.  The size+position constraint distinguishes
    # overlay sprites (covers roughly half the screen vertically) from
    # full-building backgrounds (huge, > size bounds) and from small
    # NPC dialog panels (smaller than size bounds).
    sprite_icons = [
        e for e in foreground
        if e.element_type == "icon"
        and _OVERLAY_ICON_MIN_W <= e.width <= _OVERLAY_ICON_MAX_W
        and _OVERLAY_ICON_MIN_H <= e.height <= _OVERLAY_ICON_MAX_H
        and _OVERLAY_ICON_CX_RANGE[0] * fw <= e.cx <= _OVERLAY_ICON_CX_RANGE[1] * fw
        and _OVERLAY_ICON_CY_RANGE[0] * fh <= e.cy <= _OVERLAY_ICON_CY_RANGE[1] * fh
    ]
    if not sprite_icons:
        return None

    biggest = max(sprite_icons, key=lambda e: e.width * e.height)
    signals.append(f"central_sprite={biggest.width}x{biggest.height}")
    confidence = "high"
    bbox = _bbox_from_elements([pair_top, pair_bot, biggest])

    return ObstructionResult(
        kind=KIND_OVERLAY,
        bbox=bbox,
        confidence=confidence,
        signals=signals,
    )


def _detect_dialog(inventory) -> Optional[ObstructionResult]:
    """Detect a modal dialog via the typed DialogModel detector.

    As of 2026-05-23, DialogModel is the single source of truth for
    dialog detection AND dismissal.  DialogModel requires at least one
    STRUCTURAL anchor — a close-X icon OR a whitelisted action verb
    button (`confirm`, `ok`, `cancel`, `claim`, `continue`, …).  If
    neither anchor fires, the frame is not a dialog regardless of how
    many text elements happen to sit in the centre of the screen.

    This replaces the legacy element-cluster heuristic (2026-05-15)
    that false-fired on the Berber Village interior — village's
    bottom action menu ('Achievement Reward', 'Weekly Reward', …)
    was a horizontal row of buttons inside a bounded centre cluster,
    so the legacy detector flagged the village as `kind='dialog'`
    even though there was no close-X and no real positive/negative
    action pair.  Visit #148 of a wrong cached consult told the bot
    to tap_anywhere, which never dismissed the village UI, looping
    forever.  See 2026-05-23 Berber sail trace.

    The legacy heuristic is preserved as `_detect_dialog_legacy_heuristic`
    and is invoked here ONLY to log when the two disagree, so we can
    review whether any of its signals are worth porting into DialogModel.
    The legacy verdict has NO behavioural effect.
    """
    from vision.region_detectors.dialog import detect_dialog as _detect_typed
    from loguru import logger

    fw, fh = inventory.frame_dims
    # DialogModel does its own chrome filtering (top/bottom strips,
    # element-type checks), so we hand it the raw element list rather
    # than the chrome-stripped foreground set used by the legacy
    # heuristics.  Filtering by ROLE here would drop the close-X icon
    # (tagged ROLE_CHROME_ICON in some cases) and silence DialogModel.
    elements = list(inventory.raw_elements)
    dialog = _detect_typed(elements, fw, fh)

    # Comparison logging — see if the legacy heuristic would have
    # fired on this frame and disagreed with DialogModel.
    legacy = _detect_dialog_legacy_heuristic(inventory)
    if dialog is None and legacy is not None:
        logger.warning(
            "[obstruction_classifier] dialog-detector disagreement: "
            "DialogModel says clean, legacy heuristic says dialog "
            f"(legacy signals={legacy.signals}, bbox={legacy.bbox}). "
            "Suppressing legacy verdict — review whether legacy signal "
            "should be ported into DialogModel."
        )
    elif dialog is not None and legacy is None:
        logger.debug(
            "[obstruction_classifier] DialogModel fired but legacy heuristic "
            f"did not — dialog anchors={dialog.anchors_fired} bbox={dialog.bbox}"
        )

    if dialog is None:
        return None

    return ObstructionResult(
        kind=KIND_DIALOG,
        bbox=dialog.bbox,
        confidence="high",
        signals=[
            f"anchors={list(dialog.anchors_fired)}",
            f"close_button={dialog.close_button is not None}",
            f"actions=[{','.join(repr(a.label) for a in dialog.actions)}]",
        ],
    )


def _detect_dialog_legacy_heuristic(inventory) -> Optional[ObstructionResult]:
    """Pre-2026-05-23 element-cluster heuristic — RETAINED FOR LOGGING.

    Called only by `_detect_dialog` to log when this older detector
    would have fired but DialogModel said the frame was clean.  Its
    verdict has no behavioural effect.

    The original heuristic, kept verbatim for fidelity:

      1.  An ACTION ROW exists: ≥ 2 button elements with similar cy
          (within ±_DIALOG_ROW_Y_TOLERANCE), inside the central zone.
          This is the canonical "Accept / Decline" or "OK / Cancel"
          pair.
      2.  At least one text element above the row (the body / title).
      3.  The cluster bbox is bounded — width ≤ 1700, height ≤ 800
          (real dialogs don't span the full frame).
    """
    fw, fh = inventory.frame_dims
    foreground = _foreground_elements(inventory)
    cluster = [
        e for e in foreground
        if e.element_type in ("button", "text")
        and (e.label or "").strip()
        and _in_norm_zone(e.cx, e.cy, _DIALOG_CLUSTER_ZONE, fw, fh)
    ]
    if len(cluster) < _DIALOG_MIN_CLUSTER_ELEMENTS:
        return None

    buttons = [e for e in cluster if e.element_type == "button"]
    if len(buttons) < 2:
        return None

    # Find a horizontal action row: 2+ buttons clustered by cy
    buttons_by_cy = sorted(buttons, key=lambda b: b.cy)
    action_row: List = []
    for i, anchor in enumerate(buttons_by_cy):
        row = [b for b in buttons
               if abs(b.cy - anchor.cy) <= _DIALOG_ROW_Y_TOLERANCE]
        if len(row) >= 2:
            # Distinct x positions — not the same button detected twice
            if len({b.cx for b in row}) >= 2:
                action_row = sorted(row, key=lambda b: b.cx)
                break
    if not action_row:
        return None

    # Action row should sit in the lower half of the cluster (button
    # row at bottom).  Catches "tab bar at top" false positives.
    action_row_cy = sum(b.cy for b in action_row) / len(action_row)
    cluster_cys = sorted(e.cy for e in cluster)
    median_cy = cluster_cys[len(cluster_cys) // 2]
    if action_row_cy < median_cy:
        return None

    # Bbox bounded — real dialogs fit within a modal-sized region.
    cluster_bbox = _bbox_from_elements(cluster)
    if cluster_bbox is not None:
        cw = cluster_bbox[2] - cluster_bbox[0]
        ch = cluster_bbox[3] - cluster_bbox[1]
        if cw > _DIALOG_MAX_WIDTH or ch > _DIALOG_MAX_HEIGHT:
            return None

    return ObstructionResult(
        kind=KIND_DIALOG,
        bbox=cluster_bbox,
        confidence="medium",
        signals=[
            f"cluster_size={len(cluster)}",
            f"action_row=[{','.join(repr(b.label) for b in action_row)}]",
        ],
    )


def _detect_popup(inventory) -> Optional[ObstructionResult]:
    """Small bounded frame with a close-X icon near top-right.

    Heuristic: an icon-type element in the top-right corner whose bbox
    fits the close-X size profile (small, square-ish), and there's a
    surrounding frame of bounded elements forming the popup body.

    This is the weakest detector — close-X icons are common chrome too.
    We require additional context: at least one button-type element
    below the close-X but inside a popup-shaped region.
    """
    fw, fh = inventory.frame_dims
    foreground = _foreground_elements(inventory)
    # Candidate close-X: small icon in top-right
    close_x_candidates = [
        e for e in foreground
        if e.element_type == "icon"
        and _in_norm_zone(e.cx, e.cy, _POPUP_CLOSE_X_ZONE, fw, fh)
        and 30 <= e.width <= 120
        and 30 <= e.height <= 120
    ]
    if not close_x_candidates:
        return None

    # Pick the right-most close-X candidate (popups attach the X to the
    # right edge of their frame).
    close_x = max(close_x_candidates, key=lambda e: e.cx)

    # Look for popup body: bounded elements left of and below the X
    # within a TIGHT popup-shaped region around the close-X.
    body_zone_x_lo = close_x.cx - _POPUP_BODY_X_REACH
    body_zone_y_hi = close_x.cy + _POPUP_BODY_Y_REACH
    body = [
        e for e in foreground
        if e is not close_x
        and e.cx < close_x.cx
        and e.cx > body_zone_x_lo
        and e.cy >= close_x.cy - 50
        and e.cy <= body_zone_y_hi
        and (e.label or "").strip()
    ]
    if len(body) < 2:
        return None

    # The body should include at least one labelled element that isn't
    # just chrome (currency counters etc.).  Cheap proxy: a button.
    if not any(e.element_type == "button" for e in body):
        return None

    bbox = _bbox_from_elements(body + [close_x])
    if bbox is None:
        return None

    # Final bounded-frame check — the popup bbox must FIT a real popup.
    # The right panel of port_overworld has scattered icons / list rows
    # that previously passed the "body left and below close-X" filter
    # and produced 1316×869 monsters.  Caps based on the largest real
    # popups observed in UWO (event banners, perk announcements):
    # roughly 700×500.  900×700 leaves headroom for outlier event
    # popups while still rejecting the right-panel false positives.
    bw = bbox[2] - bbox[0]
    bh = bbox[3] - bbox[1]
    if bw > _POPUP_MAX_BBOX_W or bh > _POPUP_MAX_BBOX_H:
        return None

    # Right-edge anchor guard.  The persistent right-side HUD panel on
    # port_overworld and sea (building list + mini-map + nearby ports)
    # has the same close-X-glyph + below-body shape as a popup, but is
    # anchored flush to the right screen edge and runs from near the top
    # of the screen.  Real popups have a margin from the right edge and
    # appear centred or middle-anchored.  Reject when the bbox right
    # edge is within ~50 px of the screen edge AND the bbox top is at
    # the screen-top area (y < 200).
    # Origin: 2026-05-22 Palma harbor entry — the right-side panel was
    # flagged as popup repeatedly, costing ~10 s per consult+dismiss
    # cycle.  Each frame produced a slightly different bbox so cache
    # hits never fired.
    if bbox[2] >= fw - 50 and bbox[1] < 200:
        return None

    return ObstructionResult(
        kind=KIND_POPUP,
        bbox=bbox,
        confidence="medium",
        signals=[
            f"close_x@({close_x.cx},{close_x.cy})",
            f"body_size={len(body)}",
            f"bbox={bw}x{bh}",
        ],
    )


# ── Public entry point ──────────────────────────────────────────────────────


# Order matters: most specific first.  Overlay's vertical-pair signal
# is distinctive enough to bypass dialog/popup checks when it fires.
_DETECTORS = (_detect_overlay, _detect_popup, _detect_dialog)


def classify_obstruction(inventory) -> ObstructionResult:
    """Inspect a parsed screen inventory and return the dominant
    obstruction (or KIND_NONE when no obstruction signature fires).

    Detectors run in priority order — overlay (most specific) →
    popup → dialog → none.  First match wins; the result includes a
    bbox so Layer B can scope its LLM call to the obstruction area.
    """
    for detector in _DETECTORS:
        result = detector(inventory)
        if result is not None:
            return result
    return ObstructionResult(kind=KIND_NONE)
