"""DialogModel — structural detector for bounded card dialogs.

Design: docs/dialog_and_event_models.md.  Phase 1: read-only
detection.  Build a DialogModel for every frame; the runtime
keeps using the existing interruptor pipeline.  Logging compares
the two so we can measure fire rate and agreement before any
behaviour change.

A dialog is recognised when a dense, centred element cluster is
anchored by at least ONE of:

  - **X close icon**       small icon in the cluster's top-right
  - **action verb button** Confirm / OK / Cancel / Claim / …
  - **NPC dialogue shape** NPC portrait bottom-left + Continue arrow

The X close anchor is the keystone signal — it handles informational
dialogs (no buttons), which the legacy keyword pipeline misses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from vision.omniparser import DetectedElement


# ── Action verb whitelist (mirrors overlay.py + adds collect verbs) ──

_DIALOG_ACTION_VERBS = {
    "confirm", "ok", "okay", "yes", "no", "cancel", "decline",
    "purchase", "sell", "buy", "deposit", "withdraw", "recruit",
    "donate", "exchange", "continue", "next", "claim", "collect",
    "depart", "depart now", "set sail", "close", "back",
    "accept", "skip", "receive", "open", "free", "get",
}


# ── Data shapes ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class TitleBar:
    bbox:       Tuple[int, int, int, int]
    text:       Optional[str]


@dataclass(frozen=True)
class DialogAction:
    label:      str
    bbox:       Tuple[int, int, int, int]
    is_positive: bool = False


DialogKind = str   # "informational"|"confirmation"|"system"|"reward"|"quest_offer"|"unknown"


@dataclass(frozen=True)
class DialogModel:
    """Bounded card with dark title bar.

    A Dialog is STRICTLY defined as: title bar + body + optional X
    close button + optional action buttons (Confirm/Cancel/OK/etc.).
    Action buttons are NOT mandatory — informational dialogs have
    only an X close.

    NPC overlays (transient post-transaction NPC speech in buildings)
    are NOT dialogs and live in vision.region_detectors.building_npc_overlay.
    """
    bbox:           Tuple[int, int, int, int]
    title_bar:      Optional[TitleBar] = None
    close_button:   Optional[Tuple[int, int, int, int]] = None
    body_text:      Tuple[str, ...] = ()
    actions:        Tuple[DialogAction, ...] = ()
    anchors_fired:  Tuple[str, ...] = ()

    def kind(self) -> DialogKind:
        """Classify by which fields are populated.

        Priority: quest_offer → confirmation → reward → system →
        informational → unknown.
        """
        action_labels = {a.label.strip().lower() for a in self.actions}
        if "accept" in action_labels and "decline" in action_labels:
            return "quest_offer"
        if "confirm" in action_labels and "cancel" in action_labels:
            return "confirmation"
        if action_labels & {"claim", "collect", "receive", "continue"}:
            return "reward"
        if action_labels & {"ok", "okay", "yes"} and len(self.actions) == 1:
            return "system"
        if not self.actions and self.close_button is not None:
            return "informational"
        if self.actions:
            return "confirmation"   # fallback when actions don't match priors
        return "unknown"

    def dismiss_action(self) -> str:
        """Recommended action to clear this dialog when the goal is to continue.

        Returns one of: 'tap_close', 'tap_ok', 'tap_claim',
        'caller_decides' (confirmation / quest_offer).
        """
        k = self.kind()
        if k == "informational":           return "tap_close"
        if k == "system":                  return "tap_ok"
        if k == "reward":                  return "tap_claim"
        if k in ("confirmation", "quest_offer"):
            return "caller_decides"
        return "unknown"


# ── Detection ───────────────────────────────────────────────────────


def detect_dialog(
    elements:     Sequence[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> Optional[DialogModel]:
    """Return a DialogModel when a bounded card overlay is detected.

    Strategy:
      1. Identify the central element cluster (excluding screen-edge
         chrome rows).
      2. Look for the three anchor signals inside the cluster.
      3. If ≥ 1 anchor fires, build the DialogModel.

    Phase-1 thresholds are tuned conservatively; the spotcheck script
    measures fire rate against labelled frames and we iterate.
    """
    fw, fh = frame_width, frame_height

    # Exclude top-and-bottom chrome strips (status bars, system chrome).
    top_chrome_y = int(0.06 * fh)
    bottom_chrome_y = int(0.97 * fh)
    candidates = [
        e for e in elements
        if top_chrome_y < e.cy < bottom_chrome_y
    ]
    if len(candidates) < 4:
        return None

    # Anchor 1 — X close icon: small icon in upper-half, isolated to its
    # right (no element with cx > this icon's cx + ~150 px on the same
    # band).
    close_btn = _find_x_close(candidates, fw, fh)

    # Anchor 2 — action verb buttons in the centre band.
    action_btns = _find_action_buttons(candidates, fw, fh)

    fired = []
    if close_btn:     fired.append("close")
    if action_btns:   fired.append("actions")
    if not fired:
        return None

    # Build the cluster bbox.  Anchors tell us the dialog exists; the
    # dialog's body / title extend WELL beyond the anchor bbox (the
    # title bar is at the top, the body in the middle, the action
    # buttons at the bottom).  So we use the anchor centroid to define
    # a horizontal band, and accept the entire usable vertical range.
    # ANCHORS MUST BELONG TO THE SAME DIALOG. `_find_x_close` returns the RIGHTMOST close-X
    # on the frame, which is often a side panel's rather than this dialog's. Averaging it
    # with the action row drags the band across unrelated UI, the cluster swallows the panel,
    # and the "is this an info panel?" height guard below then rejects a real dialog.
    #
    # Live 2026-08-23, the departure Notice on the world map: Ok@1306 / Cancel@1091 with the
    # Village Info panel's X at (2180,118). The mixed centroid was cx=1535, the band became
    # (575,2400), the cluster spanned (826,113)-(2237,1044) — height 931 > 864 — and a modal
    # dialog was reported as `kind=none`. The bot proceeded as though nothing blocked it.
    #
    # The action row is the reliable anchor: it is unambiguous, it belongs to the dialog by
    # construction, and it sits inside the dialog's width. Use the close-X only when there is
    # no action row, and only then as a last resort.
    if action_btns:
        anchor_pts = [a.bbox for a in action_btns]
        band_half_w = int(0.20 * fw)     # tight: a dialog is not much wider than its buttons
    else:
        anchor_pts = [close_btn]
        band_half_w = int(0.40 * fw)

    anchor_cx = sum((b[0] + b[2]) / 2 for b in anchor_pts) / len(anchor_pts)
    rx1 = max(0,  int(anchor_cx - band_half_w))
    rx2 = min(fw, int(anchor_cx + band_half_w))

    cluster = [
        e for e in candidates
        if rx1 <= e.cx <= rx2
    ]
    if len(cluster) < 3:
        return None

    bbox = (
        min(e.x1 for e in cluster),
        min(e.y1 for e in cluster),
        max(e.x2 for e in cluster),
        max(e.y2 for e in cluster),
    )

    # Geometric guards — distinguish a blocking modal dialog from an
    # information side panel (city info, nearby-ports list, fleet
    # sidebar, etc.).  A real modal:
    #   - is horizontally centred (bbox cx within central band)
    #   - is bounded vertically (does not span full screen height)
    #   - does not hug a screen edge
    # An info panel violates each of these — it sits flush against an
    # edge, runs nearly full height, and its centroid is past the
    # centred band.  Origin: 2026-05-23 Berber sail, world-map nearby-
    # ports panel at bbox=(1921, 123, 2399, 1028) was matched as a
    # dialog via a close-X icon in the panel header; the bot tried to
    # dismiss it, losing navigation context.
    bbox_w = bbox[2] - bbox[0]
    bbox_h = bbox[3] - bbox[1]
    bbox_cx = (bbox[0] + bbox[2]) / 2
    if bbox_h > 0.80 * fh:
        return None
    if not (0.30 * fw <= bbox_cx <= 0.70 * fw):
        return None
    if bbox[0] > 0.70 * fw or bbox[2] < 0.30 * fw:
        return None

    # Title bar — the topmost text element of substantial width.
    title_bar = _find_title_bar(cluster, bbox)

    # Body text — text elements between title and actions/footer.
    body_y_lo = (title_bar.bbox[3] + 8) if title_bar else bbox[1]
    body_y_hi = (min(a.bbox[1] for a in action_btns) - 8
                 if action_btns else bbox[3])
    body_text = tuple(
        e.label.strip()
        for e in cluster
        if e.element_type == "text"
        and body_y_lo <= e.cy <= body_y_hi
        and len(e.label.strip()) > 1
    )

    return DialogModel(
        bbox=bbox,
        title_bar=title_bar,
        close_button=close_btn,
        body_text=body_text,
        actions=tuple(action_btns),
        anchors_fired=tuple(fired),
    )


# ── Anchor detectors ────────────────────────────────────────────────


def _find_x_close(
    elements: Sequence[DetectedElement], fw: int, fh: int,
) -> Optional[Tuple[int, int, int, int]]:
    """An X close icon is small, in the upper half (but below chrome),
    and has no element-cluster to its right within ~10% of frame width.
    """
    min_y = int(0.08 * fh)
    max_y = int(0.55 * fh)
    candidates = [
        e for e in elements
        if e.element_type == "icon"
        and min_y < e.cy < max_y
        and 25 <= e.width  <= 100
        and 25 <= e.height <= 100
        and e.cx > 0.40 * fw
    ]
    if not candidates:
        return None

    # Prefer rightmost candidate with empty space to its right.
    candidates.sort(key=lambda e: -e.cx)
    for cand in candidates:
        gap_band_top = cand.cy - 60
        gap_band_bot = cand.cy + 60
        right_of = [
            o for o in elements
            if o is not cand
            and o.cx > cand.cx + 50
            and gap_band_top <= o.cy <= gap_band_bot
        ]
        if not right_of:
            return (cand.x1, cand.y1, cand.x2, cand.y2)
    return None


def _find_action_buttons(
    elements: Sequence[DetectedElement], fw: int, fh: int,
) -> List[DialogAction]:
    """Action-verb buttons in the centre band, lower half.

    Accepts BOTH `button`-typed and `text`-typed elements.  OmniParser
    types elements with visual fills/borders/rounded-corner backgrounds
    as `button` and everything else as `text`.  Material-Design-style
    action buttons (colored text on a transparent background, common in
    Android system dialogs and modern app chrome) lack those visual
    cues, so OmniParser types them as `text` — but they ARE buttons by
    behaviour.  Same pattern affects iOS-style nav-bar buttons, inline
    card links ("See more"), and tab-bar text controls.

    The action-verb whitelist (`ok`, `cancel`, `confirm`, `claim`, …)
    is specific enough that a text element matching it in the
    bottom-centre of the screen is overwhelmingly likely to be a real
    action element.  DO NOT tighten this back to `button` only — that
    will reintroduce the Android system-dialog miss.

    Centre band widened to 0.20..0.80 horizontally so right-aligned
    Android OK buttons (cx ~ 1855 on a 2400-wide frame, which is 77%)
    aren't excluded.
    """
    out: List[DialogAction] = []
    for e in elements:
        if e.element_type not in ("button", "text"):
            continue
        label = e.label.strip().lower()
        if label not in _DIALOG_ACTION_VERBS:
            continue
        if not (0.20 * fw <= e.cx <= 0.80 * fw):
            continue
        if not (0.45 * fh <= e.cy <= 0.95 * fh):
            continue
        out.append(DialogAction(
            label=e.label.strip(),
            bbox=(e.x1, e.y1, e.x2, e.y2),
            is_positive=label in {
                "confirm", "ok", "okay", "yes", "claim", "collect",
                "continue", "accept", "depart", "depart now", "set sail",
                "open", "receive", "purchase", "sell", "buy", "recruit",
            },
        ))
    return out


def _find_title_bar(
    cluster: Sequence[DetectedElement],
    cluster_bbox: Tuple[int, int, int, int],
) -> Optional[TitleBar]:
    """Topmost wide text element of the cluster."""
    cb_x1, cb_y1, cb_x2, _ = cluster_bbox
    cb_width = cb_x2 - cb_x1
    top_band_y = cb_y1 + int(0.20 * (cluster_bbox[3] - cb_y1))
    candidates = [
        e for e in cluster
        if e.element_type in ("text", "button")
        and e.cy <= top_band_y
        and e.width >= 0.20 * cb_width
    ]
    if not candidates:
        return None
    top = min(candidates, key=lambda e: e.y1)
    return TitleBar(
        bbox=(top.x1, top.y1, top.x2, top.y2),
        text=top.label.strip() or None,
    )
