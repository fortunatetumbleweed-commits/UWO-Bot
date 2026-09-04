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


# ── The brown title bar — the game's own layer marker ───────────────
#
# EVERY DIALOG IN THIS GAME HAS A BROWN TITLE BAR (user, 2026-09-03), and the game composites
# a flat ~50% scrim over whatever a dialog covers. So a bar's own brightness says WHICH LAYER
# it is on, and that is the signal the element-cluster approach could never see.
#
# Measured on the San Village barter, the same `Insufficient Empty Space` dialog in front
# (frame 334) and then behind a `Notice` (frame 335):
#
#                          R     G     B    R/B   G/B    median luminance
#   IES bar, in front     71    46    35   2.03  1.31         50.5
#   Notice bar, in front  70    46    35   2.00  1.31         50.5
#   IES bar, behind       36    23    18   2.00  1.28         25.4
#
# Two things fall out, and both are load-bearing:
#
#   * THE RATIOS SURVIVE THE DIM — scaling every channel by ½ leaves R/B and G/B untouched —
#     so COLOUR identifies "this is a title bar" regardless of layer, and LUMINANCE then
#     identifies the layer. Ranking bars by brightness needs no absolute constant.
#   * A FRONT BAR IS DARKER THAN A DIMMED BODY (50.5 against a rear card's 105-120). So the
#     bar can never be found by thresholding brightness, and a dialog's extent can never be
#     "the bright pixels": the title bar is dark BY DESIGN. That inversion is why the same
#     trick had to be re-derived for tab selection, and it is the whole reason this is a
#     colour test with a luminance ranking rather than a brightness test.
#
# Two false positives were found by sweeping 400 frames across 243 recorded sessions, and the
# guards below exist for them specifically:
#
#   * WARM SCENERY. Market beams, sacks and tavern wood are the same hue family and pass the
#     ratios outright. Chrome is FLAT PAINT and a photograph is not: real bars measured
#     std 2.1-5.0, scenery 12.8-23.0.
#   * THE MARKET'S TAN PRICE STRIPS (R=149 G=121 B=82) — flat, horizontal, and warm, but
#     R/B 1.82 and G/B 1.48 against a real bar's 2.00 and 1.33.
#
# After both guards the sweep gives at most TWO bars on any frame, front bars clustering at
# 48-54 and dimmed ones at 24-28.
#
# NOTE this finds any CHROME HEADER, not only a dialog's — a side panel's `Requested Trade
# Goods` header reads 51.7, front-level and perfectly real. Telling a modal from a panel is
# the geometric guards' job, and they can finally do it because they are handed the card
# rather than a full-height stripe.

_BAR_R_OVER_B = (1.88, 2.16)
_BAR_G_OVER_B = (1.20, 1.42)
_BAR_MIN_R, _BAR_MAX_R = 12, 170
_BAR_MIN_WIDTH_FRAC = 0.12
_BAR_MIN_H, _BAR_MAX_H = 18, 90
_BAR_MAX_STD = 8.0


@dataclass(frozen=True)
class TitleBarBand:
    """One brown chrome header, with the brightness that places it in the stack."""
    bbox:      Tuple[int, int, int, int]
    luminance: float
    flatness:  float


def find_title_bars(frame) -> List[TitleBarBand]:
    """Every brown chrome header on `frame`, brightest (frontmost) FIRST.

    `frame` is a PIL Image. Returns [] when none is found, which is the common case —
    most screens are not showing a dialog or a panel.
    """
    import numpy as np

    a = np.asarray(frame.convert("RGB")).astype(float)
    h, w, _ = a.shape
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    Bs = np.maximum(B, 1.0)
    brown = ((R / Bs > _BAR_R_OVER_B[0]) & (R / Bs < _BAR_R_OVER_B[1])
             & (G / Bs > _BAR_G_OVER_B[0]) & (G / Bs < _BAR_G_OVER_B[1])
             & (R > _BAR_MIN_R) & (R < _BAR_MAX_R) & (R > G) & (G > B))
    lum = 0.2126 * R + 0.7152 * G + 0.0722 * B

    wide_enough = brown.sum(axis=1) > _BAR_MIN_WIDTH_FRAC * w
    bars: List[TitleBarBand] = []
    y = 0
    while y < h:
        if not wide_enough[y]:
            y += 1
            continue
        y0 = y
        while y < h and wide_enough[y]:
            y += 1
        if not (_BAR_MIN_H <= y - y0 <= _BAR_MAX_H):
            continue
        cols = np.where(brown[y0:y].sum(axis=0) > 0.5 * (y - y0))[0]
        if cols.size < _BAR_MIN_WIDTH_FRAC * w:
            continue
        x1, x2 = int(cols.min()), int(cols.max())
        paint = lum[y0:y, x1:x2][brown[y0:y, x1:x2]]
        if paint.size == 0 or float(paint.std()) > _BAR_MAX_STD:
            continue
        bars.append(TitleBarBand(bbox=(x1, y0, x2, y),
                                 luminance=float(np.median(paint)),
                                 flatness=float(paint.std())))
    bars.sort(key=lambda b: -b.luminance)
    return bars


# The front card's BODY is bright, and the scrim makes "bright" relative rather than
# absolute: measured against a front bar at 50.5, its own body reads 235 (x4.7) while the
# card behind it reads 105-120 (x2.1-2.4). Three times the bar sits cleanly between them and
# needs no constant of its own — if the game ever re-grades its palette, the bar moves with
# the body and the ratio holds.
_BODY_OVER_BAR = 3.0


def card_from_bar(frame, bar: TitleBarBand) -> Optional[Tuple[int, int, int, int]]:
    """The full card under `bar` — the bar itself plus the bright body beneath it.

    This is what replaces "every element in a vertical band". The band had no bottom, so it
    ran from a dialog's title straight through the panel behind it to the footer at the
    bottom of the screen — 932px on the San Village frame, which then tripped the very height
    guard meant to reject side panels. The card is 566px, and it is the dialog.
    """
    import numpy as np
    from scipy import ndimage

    a = np.asarray(frame.convert("RGB")).astype(float)
    lum = 0.2126 * a[:, :, 0] + 0.7152 * a[:, :, 1] + 0.0722 * a[:, :, 2]
    bx1, by1, bx2, by2 = bar.bbox

    body = lum > bar.luminance * _BODY_OVER_BAR
    body[:by2, :] = False                       # the card is BELOW its own bar
    labelled, n = ndimage.label(body)
    if not n:
        return None
    # The card is the biggest bright thing whose columns overlap the bar's.
    best, best_size = None, 0
    for sl, idx in zip(ndimage.find_objects(labelled), range(1, n + 1)):
        if sl is None:
            continue
        y1, y2 = sl[0].start, sl[0].stop
        x1, x2 = sl[1].start, sl[1].stop
        if x2 < bx1 or x1 > bx2:
            continue                            # a bright thing somewhere else entirely
        size = int((labelled[sl] == idx).sum())
        if size > best_size:
            best, best_size = (x1, y1, x2, y2), size
    if best is None:
        return None
    return (min(bx1, best[0]), by1, max(bx2, best[2]), best[3])


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
    frame=None,
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

    # THE CARD FIRST, WHEN WE HAVE PIXELS (user, 2026-09-03). With a frame we can ask the
    # screen where the dialog actually is, instead of inferring it from where elements happen
    # to sit. `_card_cluster` returns the elements INSIDE the frontmost card; everything
    # below then proceeds unchanged on a set that contains only this dialog's own widgets.
    #
    # This is what fixes the stacked case. Before it, hunting the anchors across the whole
    # frame took a side panel's close-X (2173,336) over the dialog's own (1616,238), and
    # counted the panel's `Receive` button as one of the dialog's actions — then the
    # full-height cluster tripped the side-panel height guard and a plainly-visible modal was
    # reported CLEAN on every frame for two minutes.
    card_bbox = None
    if frame is not None:
        try:
            card_bbox, elements = _card_cluster(frame, elements)
        except Exception:                        # noqa: BLE001 — pixels are a bonus, never required
            card_bbox = None

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

    bbox = card_bbox or (
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
    # THE HEIGHT GUARD WAS FOR A CONTAMINATED CLUSTER, NOT FOR A CARD. It was written when
    # the "cluster" was a full-height vertical STRIPE that swallowed whatever shared its
    # columns, so height stood in for "this is not really one widget". Now that the card is
    # segmented off its own title bar, height means what it says — and real dialogs are tall.
    #
    # Live 2026-09-04: a `Gear Info` modal measured 880px against this 864px limit and was
    # reported CLEAN, so nothing could close a dialog the bot had just opened, and the run
    # died. ff60c83's own commit message predicted exactly this ("it would be rejecting
    # genuinely tall dialogs") and deferred it; the deferral cost the mission.
    #
    # The guard's original case is covered twice over WITHOUT it. The 2026-05-23 world-map
    # nearby-ports panel sat at (1921,123)-(2399,1028): centre x=2160 (0.90 fw) fails the
    # centring test below, and left edge 1921 > 0.70 fw fails the edge test. Those two are
    # what actually distinguish a side panel from a modal — a panel HUGS AN EDGE, a modal is
    # centred and inset. Height never separated them; it only happened to correlate.
    #
    # SO THE LIMIT DEPENDS ON HOW THE BBOX WAS DERIVED, which the frameless path proves:
    # without pixels the cluster is still that full-height stripe — 932px on the very frame
    # this fix was built from — and there the guard is doing real work. Segmented from the
    # card, 0.92 leaves room for a genuinely tall dialog; inferred from elements, 0.80 keeps
    # the contaminated cluster out.
    if bbox_h > (0.92 if card_bbox else 0.80) * fh:
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



def _card_cluster(frame, elements: Sequence[DetectedElement]):
    """(card bbox, the elements inside it) for the frontmost dialog card.

    Falls back to (None, elements) whenever the screen does not offer a card, so a caller
    that hands us a frame is never worse off than one that does not.
    """
    bars = find_title_bars(frame)
    if not bars:
        return None, elements
    card = card_from_bar(frame, bars[0])
    if card is None:
        return None, elements
    x1, y1, x2, y2 = card
    inside = [e for e in elements if x1 <= e.cx <= x2 and y1 <= e.cy <= y2]
    if len(inside) < 3:
        return None, elements          # a card we cannot populate is not evidence
    return card, inside

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
