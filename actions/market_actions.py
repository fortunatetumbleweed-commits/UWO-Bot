# actions/market_actions.py
# Buy and sell operations driven by the learned transaction flow.
#
# The exact UI sequence was captured and analyzed in:
#   memory/knowledge/flows/market_sell/flow.json
#
# Sell flow (9 steps):
#   sell_tab_goods_grid
#     → tap good tile
#   trade_goods_info_dialog  [verified: "Sales Cost" on screen]
#     → tap Max → (no dialog) → tap Load
#   basket_loaded_sell_ready  [verified: "Nego" on screen]
#     → tap Sell button (yellow)
#   confirm_sales_dialog  [verified: "Confirm Sales" on screen]
#     → tap Ok
#   negotiation_dialog  [optional — detected by "negotiat" keyword]
#     → tap No (skip) or Use 1 chance
#   sell_result_dialog  [verified: "Total Amount" on screen]
#     → tap Ok  → log result
#   sell_tab_goods_grid_updated  [success]
#
# Every tap is followed by a screen capture that verifies the expected state
# actually appeared before proceeding to the next step.

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from loguru import logger
from PIL import Image

from actions.adb_actions import tap, press_back
from capture.adb_capture import capture_screen
from config.settings import MARKET_COORDS, MARKET_CONTENT_REGION

# KB dialog confirmation helper — avoids repeating `from brain.kb import control`
# at every call site. Returns the keyword tuple for a dialog confirmation check.
def _ckb_lazy(key: str) -> tuple:
    """
    Lazy ControlKB accessor for market_actions.
    - For dialog detection keywords: pass the dialog_id (e.g. "confirm_purchase")
    - For action button labels: pass the action name (e.g. "purchase", "sell")
    All keys are resolved by ControlKB; no KB paths are hardcoded here.
    """
    from brain.kb import control
    ckb = control()
    labels = ckb.market_action_button_labels(key)
    if labels:
        return tuple(labels)
    return ckb.dialog_confirmation(key)




# ── Load the learned flow ──────────────────────────────────────────────────────

def _load_flow(flow_name: str) -> dict:
    from tools.analyze_flow import load_flow
    flow = load_flow(flow_name)
    if flow is None:
        raise RuntimeError(
            f"Flow '{flow_name}' not found. "
            f"Run: python tools/capture_flow.py {flow_name}"
        )
    return flow


def _step(flow: dict, name: str) -> dict:
    """Get a step dict by name, raise if missing."""
    s = next((s for s in flow["steps"] if s["name"] == name), None)
    if s is None:
        raise RuntimeError(f"Step '{name}' not found in flow")
    return s


# ── Flow coordinate scaling ────────────────────────────────────────────────────
#
# analyze_flow.py sends 1000px-wide thumbnails to Claude.
# Claude returns coordinates in that thumbnail space.
# The actual tap surface is 2400×1080, so all flow coords must be scaled.
#
# Scale factor = actual_frame_width / FLOW_THUMBNAIL_WIDTH
# Since the resize preserves aspect ratio, the same factor applies to both axes.

FLOW_THUMBNAIL_WIDTH: int = 1000
_flow_scale: float = 2.4          # default for 2400px-wide frames; updated on first tap


def _set_flow_scale(frame: Image.Image) -> float:
    """Compute and cache the flow→screen scale factor from the actual frame width."""
    global _flow_scale
    _flow_scale = frame.width / FLOW_THUMBNAIL_WIDTH
    return _flow_scale


def _dialog_ok_pos(
    frame: Image.Image,
    labels: tuple = ("ok", "okay", "confirm", "yes"),
) -> Optional[Tuple[int, int]]:
    """Return (x, y) of an OK-like action button in the current DialogModel,
    or None when no typed dialog is detected.

    Phase 3.1: market/sell transaction dialogs prefer this typed
    structural detection over the legacy hard-coded `_flow_coords`
    positions in flow_coords.json.  When DialogModel doesn't fire on a
    frame, the caller falls back to flow_coords — so behaviour is
    unchanged on misses and pixel-accurate on hits.

    Uses OmniParser's cached per-frame parse, so the cost is one
    cached lookup plus the lightweight detector code.
    """
    try:
        from vision.omniparser import get_omniparser
        from vision.region_detectors.dialog import detect_dialog
        parser = get_omniparser()
        elements = parser.parse_fast(frame)
        dialog = detect_dialog(elements, frame.width, frame.height)
        if dialog is None:
            return None
        wanted = {lbl.lower() for lbl in labels}
        for action in dialog.actions:
            if action.label.strip().lower() in wanted:
                x1, y1, x2, y2 = action.bbox
                return ((x1 + x2) // 2, (y1 + y2) // 2)
    except Exception as e:
        logger.debug(f"  [dialog_ok_pos] detection failed: {e}")
    return None


def _resolve_dialog_ok(
    frame: Image.Image,
    flow: dict,
    fallback_step: str,
    handler: str,
) -> Tuple[int, int]:
    """Pick the best OK-button position for *frame*: DialogModel first,
    flow_coords fallback.  Records telemetry under `handler`.
    """
    from brain.dismissal_telemetry import record as _telem
    typed = _dialog_ok_pos(frame)
    if typed is not None:
        _telem(handler, "typed")
        return typed
    _telem(handler, "legacy")
    return _flow_coords(flow, fallback_step)


def _flow_coords(flow: dict, step_name: str) -> Tuple[int, int]:
    """Return scaled (x, y) from a step's action_target (thumbnail → screen coords)."""
    s = _step(flow, step_name)
    t = s["action_target"]
    return (int(t["tap_x"] * _flow_scale), int(t["tap_y"] * _flow_scale))


def _flow_btn_coords(step: dict, label_keyword: str) -> Optional[Tuple[int, int]]:
    """
    Find a key_element in a step by label keyword and return its scaled coords.
    Returns None if not found.
    """
    kw = label_keyword.lower()
    for el in step.get("key_elements", []):
        if kw in el["label"].lower():
            return (int(el["tap_x"] * _flow_scale), int(el["tap_y"] * _flow_scale))
    return None


# ── Perception helpers ─────────────────────────────────────────────────────────
#
# Two complementary layers:
#
#   _get_elements(frame)   — OmniParser fast mode (YOLO + EasyOCR, no Florence-2).
#                            Returns DetectedElement list with bounding boxes.
#                            Falls back to empty list if YOLO unavailable.
#
#   _ocr_frame(frame)      — EasyOCR text-only scan.
#                            Used for keyword presence checks and as fallback
#                            when OmniParser cannot find an element by label.
#
# Button-finding strategy (in _find_button):
#   1. Try OmniParser elements — searches "button" and "text" elements by label.
#      This finds icon-based buttons even if OCR alone would miss them.
#   2. Fall back to EasyOCR text scan — handles text that OmniParser misses.
#
# Screen-state verification (_screen_contains, _wait_for_screen) stays
# EasyOCR-based: we are checking for the presence of specific text keywords
# like "Sales Cost" or "Total Amount", which OCR handles well.


def _get_elements(frame: Image.Image):
    """
    Run OmniParser fast mode on a frame.
    Returns list[DetectedElement] — YOLO icon bboxes merged with EasyOCR text.
    Returns [] if OmniParser YOLO is not available (graceful fallback).
    """
    from vision.omniparser import get_omniparser
    parser = get_omniparser()
    if not parser.yolo_available():
        return []
    return parser.parse_fast(frame)


# ── Reasoning-fallback escalation ─────────────────────────────────────────────
# When a scripted transaction step stalls on a dialog it did NOT expect (e.g. the
# load-ratio / capacity warning that blocks the Confirm dialog), hand THAT screen
# to the LLM reasoning layer to resolve it (tap OK / proceed), then let the flow
# resume. The deterministic flow does the fast bulk work; the LLM only handles the
# surprise dialog. See docs/reasoning_fallback_layer_design.md.
_TXN_ESCALATION_INTENT = (
    "You are in the middle of completing a market transaction (buying or selling "
    "trade goods). A dialog is on screen that the scripted flow did not expect — "
    "often a load-ratio / capacity warning or a confirmation. If it is a warning "
    "and supplies are sufficient for the voyage, confirm it (tap OK / the [COMMIT] "
    "button) to proceed. Handle whatever dialog is shown so the purchase/sale can "
    "complete."
)


def _escalate_unexpected_dialog(intent: str = _TXN_ESCALATION_INTENT,
                                max_steps: int = 3) -> bool:
    """Hand the current screen to the reasoning layer to clear an unexpected dialog.

    Returns True if the LLM acted (tapped something); the caller should then re-check
    its expected state. Isolated import so market_actions doesn't hard-depend on the
    reasoning layer at module load.
    """
    try:
        from brain.reasoning_loop import resolve
        from brain.world_model import WorldModel
        logger.info("  [escalation] unexpected dialog — handing to reasoning layer…")
        out = resolve(intent, WorldModel(), shadow=False, max_steps=max_steps,
                      trigger="market_dialog_escalation")
        acted = any((s.get("exec") or {}).get("ok") for s in out.get("steps", []))
        logger.info(f"  [escalation] acted={acted} — {out.get('reason')}")
        return acted
    except Exception as exc:
        logger.warning(f"  [escalation] failed: {exc}")
        return False


def _ocr_frame(frame: Image.Image, min_conf: float = 0.35) -> List[Tuple[str, float, int, int]]:
    """
    Run EasyOCR on a full frame.
    Returns list of (text, conf, cx, cy) — centre-pixel coordinates.
    """
    from vision.ocr import _get_reader
    arr = np.array(frame)
    raw = _get_reader().readtext(arr, detail=1)
    out = []
    for bbox, text, conf in raw:
        if conf < min_conf:
            continue
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        cx = int((min(xs) + max(xs)) / 2)
        cy = int((min(ys) + max(ys)) / 2)
        out.append((text, conf, cx, cy))
    return out


def _screen_contains(frame: Image.Image, *keywords: str, min_conf: float = 0.35) -> bool:
    """True if ANY of the keywords appear anywhere in the frame OCR output."""
    tokens = _ocr_frame(frame, min_conf=min_conf)
    full = " ".join(t.lower() for t, _, _, _ in tokens)
    return any(kw.lower() in full for kw in keywords)


def _wait_for_screen(
    *keywords: str,
    timeout: float = 8.0,
    interval: float = 0.8,
    min_conf: float = 0.35,
) -> Tuple[Optional[Image.Image], bool]:
    """
    Capture repeatedly until the screen contains ALL keywords (AND logic), or timeout.
    Uses EasyOCR for keyword matching.
    Returns (frame, True) when all keywords found, (last_frame, False) on timeout.
    """
    deadline = time.time() + timeout
    frame = None
    while time.time() < deadline:
        frame = capture_screen()
        tokens = _ocr_frame(frame, min_conf=min_conf)
        full = " ".join(t.lower() for t, _, _, _ in tokens)
        if all(kw.lower() in full for kw in keywords):
            return frame, True
        time.sleep(interval)
    return frame, False


def _find_button(
    frame: Image.Image,
    *labels: str,
    x_max: Optional[int] = None,
    x_min: Optional[int] = None,
    y_min: Optional[int] = None,
    y_max: Optional[int] = None,
) -> Optional[Tuple[int, int]]:
    """
    Find a UI button by its label text.  Tries each label in order.
    Optional x_min/x_max/y_min/y_max constrain the search region.

    Strategy:
      1. OmniParser elements (YOLO bbox + EasyOCR label) — searches button and
         text elements. Better positional accuracy; also finds icon-only buttons.
      2. EasyOCR fallback — plain text scan when OmniParser is unavailable or
         returns no match.

    Returns (cx, cy) in full-frame coordinates, or None if not found.
    """
    from vision.omniparser import DetectedElement

    # ── Pass 1: OmniParser elements ──────────────────────────────────────────
    elements = _get_elements(frame)
    if elements:
        for label in labels:
            label_l = label.lower()
            for el in elements:
                if el.element_type not in ("button", "text", "icon"):
                    continue
                if label_l not in el.label.lower():
                    continue
                cx, cy = el.cx, el.cy
                if x_max is not None and cx > x_max: continue
                if x_min is not None and cx < x_min: continue
                if y_min is not None and cy < y_min: continue
                if y_max is not None and cy > y_max: continue
                logger.debug(f"OmniParser found {label!r}: {el}")
                return (cx, cy)

    # ── Pass 2: EasyOCR fallback ─────────────────────────────────────────────
    tokens = _ocr_frame(frame)
    for label in labels:
        label_l = label.lower()
        for text, _, cx, cy in tokens:
            if label_l not in text.lower(): continue
            if x_max is not None and cx > x_max: continue
            if x_min is not None and cx < x_min: continue
            if y_min is not None and cy < y_min: continue
            if y_max is not None and cy > y_max: continue
            logger.debug(f"EasyOCR fallback found {label!r} @ ({cx},{cy})")
            return (cx, cy)

    return None


def _read_cargo_capacity(frame: Image.Image) -> Tuple[int, int]:
    """
    Read the cargo used/total counter from the market right panel.
    The counter shows "X/Y" or "X,XXX/Y,YYY" (used/total).

    Strategy:
      1. Narrow OCR crop on the right panel header area (fast path).
      2. If that returns nothing, widen the scan to the full right 1/4 of screen
         (x > frame.width * 3/4) using both OCR and OmniParser text elements.

    Returns (used, total), or (0, 0) if not found.
    """
    import re
    from vision.ocr import _get_reader

    def _parse_slash_number(text: str) -> Optional[Tuple[int, int]]:
        m = re.search(r'([\d,]+)\s*/\s*([\d,]+)', text)
        if m:
            used  = int(m.group(1).replace(',', ''))
            total = int(m.group(2).replace(',', ''))
            if 100 <= total <= 200_000:
                return used, total
        return None

    # ── Pass 1: narrow crop (fast) ────────────────────────────────────────────
    cargo_region = frame.crop((1650, 40, frame.width, 350))
    arr = np.array(cargo_region)
    raw = _get_reader().readtext(arr, detail=1)
    for _, text, conf in raw:
        if conf < 0.25:
            continue
        result = _parse_slash_number(text)
        if result:
            logger.debug(f"  Cargo: {result[0]}/{result[1]} (pass 1, text {text!r})")
            return result

    # ── Pass 2: full right-quarter fallback (OmniParser + OCR) ───────────────
    logger.debug("  Cargo not found in narrow crop — scanning right 1/4 of screen")
    x_start = frame.width * 3 // 4
    right_quarter = frame.crop((x_start, 0, frame.width, frame.height))

    # OmniParser fast scan
    elements = _get_elements(right_quarter)
    for el in elements:
        result = _parse_slash_number(el.label)
        if result:
            logger.debug(f"  Cargo: {result[0]}/{result[1]} (pass 2 OmniParser, {el.label!r})")
            return result

    # EasyOCR on full right quarter
    arr2 = np.array(right_quarter)
    raw2 = _get_reader().readtext(arr2, detail=1)
    for _, text, conf in raw2:
        if conf < 0.20:
            continue
        result = _parse_slash_number(text)
        if result:
            logger.debug(f"  Cargo: {result[0]}/{result[1]} (pass 2 OCR, text {text!r})")
            return result

    logger.debug("  Cargo counter not found in either pass")
    return 0, 0


def _log_elements(frame: Image.Image, step_name: str) -> None:
    """
    Debug helper: log all OmniParser-detected elements at a flow step.
    Emits at DEBUG level — silent in normal operation, visible with --debug.

    OmniParser inference (~5-15s) is skipped entirely unless DEBUG logging
    is enabled — previously it ran unconditionally, blocking the hot path.
    """
    # loguru has no is_enabled_for() (that is stdlib logging.Logger).
    # LOG_LEVEL is always DEBUG in this project so proceed unconditionally.
    elements = _get_elements(frame)
    if not elements:
        logger.debug(f"  [{step_name}] OmniParser: no elements (YOLO unavailable or empty)")
        return
    logger.debug(f"  [{step_name}] OmniParser detected {len(elements)} elements:")
    for el in elements:
        logger.debug(f"    {el}")


# Keep the old name as an alias so existing callers still work
def _find_button_ocr(
    frame: Image.Image,
    *labels: str,
    min_conf: float = 0.35,
    x_max: Optional[int] = None,
    x_min: Optional[int] = None,
) -> Optional[Tuple[int, int]]:
    return _find_button(frame, *labels, x_max=x_max, x_min=x_min)


# ── Tile position via EasyOCR ──────────────────────────────────────────────────

_RIGHT_PANEL_X = 1650   # never tap anything to the right of this


def _find_tile_pos(frame: Image.Image, good_name: str) -> Optional[Tuple[int, int]]:
    """
    Use EasyOCR to find the centre of a good's tile in the sell/purchase grid.
    Returns (x, y) in full-frame coords, or None if not found.

    Two-pass matching:
      1. Exact substring — zero false positives, handles normal reads.
      2. Fuzzy (SequenceMatcher ≥ 0.65) — catches OCR misreads where the stored
         name differs from the current read (e.g. "Goia Dust" vs "Gold Dust").
    """
    from difflib import SequenceMatcher
    from vision.ocr import _get_reader

    ox, oy, ox2, oy2 = MARKET_CONTENT_REGION
    ox2 = min(ox2, _RIGHT_PANEL_X)
    region = frame.crop((ox, oy, ox2, oy2))
    arr = np.array(region)
    raw = _get_reader().readtext(arr, detail=1)

    # Apply learned corrections — the stored name may be an old OCR misread
    from training.collector import apply_name_correction
    good_name = apply_name_correction(good_name)

    name_lower = good_name.lower()
    candidates: list[tuple[float, tuple[int, int], str]] = []  # (score, pos, ocr_text)

    for bbox, text, conf in raw:
        if conf < 0.3:
            continue
        t_lower = text.lower()
        # Pass 1: exact substring
        if name_lower in t_lower or t_lower in name_lower:
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            cx = int((min(xs) + max(xs)) / 2) + ox
            cy = int((min(ys) + max(ys)) / 2) + oy
            logger.debug(f"Tile OCR found (exact): {good_name!r} → {text!r} @ ({cx}, {cy})")
            return (cx, cy)
        # Collect fuzzy candidates
        score = SequenceMatcher(None, name_lower, t_lower).ratio()
        if score >= 0.65:
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            cx = int((min(xs) + max(xs)) / 2) + ox
            cy = int((min(ys) + max(ys)) / 2) + oy
            candidates.append((score, (cx, cy), text))

    if candidates:
        best_score, best_pos, best_text = max(candidates, key=lambda c: c[0])
        logger.info(f"Tile OCR found (fuzzy {best_score:.2f}): {good_name!r} → {best_text!r} @ {best_pos}")
        return best_pos

    logger.debug(f"Tile OCR: '{good_name}' not found")
    return None


# ── Result parser ──────────────────────────────────────────────────────────────

@dataclass
class SellResult:
    good: str = ""
    quantity: int = 0
    total_amount: int = 0
    profit: int = 0
    tax: int = 0
    surcharge: int = 0
    trade_exp: int = 0
    trade_fame: int = 0
    negotiated: bool = False
    negotiation_used: bool = False


def _parse_number(s: str) -> Optional[int]:
    """Parse a number string that may have commas, +/- signs."""
    import re
    m = re.search(r'[\d,]+', s.replace("+", "").replace("-", ""))
    if m:
        try:
            return int(m.group().replace(",", ""))
        except ValueError:
            pass
    return None


def _parse_sell_result(
    frame: Image.Image,
    good_name: str,
    result: SellResult,
) -> SellResult:
    """
    Read the sell result dialog and populate the SellResult fields.

    The result screen has labelled rows (from flow.json observation):
      Mate Trade EXP  259
      Contribution    8
      Trade Fame      87
      Sales Cost      132,440
      Tax             -23,540
      Surcharge       +34,650
      Profit          88,660
      Total Amount    143,550
      Balance         15,055,159,567
    """
    import re

    arr = np.array(frame)
    from vision.ocr import _get_reader
    raw = _get_reader().readtext(arr, detail=1)

    # Build a list of (text, conf) pairs for label→value matching
    tokens = [(t.strip(), c) for _, t, c in raw if c > 0.3 and t.strip()]

    # Label → (field_name, max_plausible_value)
    # max_plausible_value caps OCR misfires where a large balance/price is
    # accidentally parsed as a small stat (e.g. Trade Fame should never be
    # in the millions).
    from brain.kb import control
    label_map = control().sell_result_label_map()

    values: dict = {}
    for i, (text, conf) in enumerate(tokens):
        tl = text.lower()
        for label, (field_name, max_val) in label_map.items():
            if label not in tl:
                continue
            if field_name in values:
                continue
            # Look at the next 1-3 tokens for the value
            for j in range(i + 1, min(i + 4, len(tokens))):
                n = _parse_number(tokens[j][0])
                if n is not None and 0 < n <= max_val:
                    values[field_name] = n
                    break

    result.trade_exp    = values.get("trade_exp", 0)
    result.trade_fame   = values.get("trade_fame", 0)
    result.tax          = values.get("tax", 0)
    result.surcharge    = values.get("surcharge", 0)
    result.profit       = values.get("profit", 0)
    result.total_amount = values.get("total_amount", 0)

    logger.debug(
        f"  Result parsed: total={result.total_amount} profit={result.profit} "
        f"tax={result.tax} surcharge={result.surcharge} "
        f"exp={result.trade_exp} fame={result.trade_fame}"
    )
    return result


# ── Negotiation ────────────────────────────────────────────────────────────────

def _sell_handle_negotiation_and_wait_result(
    flow: dict,
    strategy: str = "no",
    max_rounds: int = 10,
) -> tuple:
    """Loop-based negotiation + result detection for the sell flow.

    Mirrors `_buy_handle_negotiation`: each iteration captures a fresh
    frame, checks for the result dialog FIRST, then for an active
    negotiation round.  Returns as soon as the result dialog is
    visible OR neither dialog is found for several polls in a row.

    Why this exists (2026-05-19): the previous single-shot pattern
    (`_handle_negotiation` once, then `_wait_for_screen("total amount")`
    with 10 s timeout) was missing the result dialog in real sell
    transactions.  Two plausible causes:
      - UWO's sell can have MULTIPLE negotiation rounds; the single-
        shot caller only handled one and got stuck on the next round's
        dialog while waiting for "Total Amount".
      - Race: the result dialog may appear between the post-Skip
        sleep and the first poll cycle, then transition fast enough
        that the static "total amount" check misses it on the next
        capture.

    Returns:
        (rounds: int, result_frame: Optional[Image.Image]).
        `result_frame` is None when the result dialog never appeared
        within `max_rounds` polls — callers should treat this as the
        transaction-may-have-failed case and bail out gracefully.
    """
    from brain.kb import control as _ckb
    rounds = 0
    consecutive_neither = 0

    while rounds < max_rounds:
        time.sleep(1.2)
        frame = capture_screen()

        # ── Check result FIRST ─────────────────────────────────────────────
        # Mirror of buy: if the negotiation auto-resolved or the user-
        # facing result transitioned in between polls, catch the result
        # dialog before mistakenly tapping a negotiation button.
        if _screen_contains(frame, *_ckb_lazy("sell_result")):
            logger.info(
                f"  Result dialog detected after {rounds} negotiation round(s)"
            )
            return rounds, frame

        # ── Then check for an active negotiation round ─────────────────────
        if not _screen_contains(frame, *_ckb().dialog_confirmation("negotiation")):
            # Neither dialog visible — probably a transition frame.
            # Wait a couple more polls before giving up so we don't
            # bail out during the brief lull between rounds.
            consecutive_neither += 1
            if consecutive_neither >= 3:
                logger.warning(
                    f"  Sell post-confirm: neither negotiation nor result "
                    f"dialog seen for {consecutive_neither} polls "
                    f"(rounds={rounds}) — giving up"
                )
                return rounds, None
            rounds += 1
            continue
        consecutive_neither = 0

        rounds += 1
        logger.info(f"  Negotiation dialog (round {rounds}) — strategy: {strategy!r}")
        _handle_negotiation(flow, frame, strategy=strategy)

        # ── Re-check immediately after the tap ─────────────────────────────
        # If "no" was tapped, the result usually appears within ~1–2 s.
        time.sleep(1.2)
        frame = capture_screen()
        if _screen_contains(frame, *_ckb_lazy("sell_result")):
            logger.info(
                f"  Result dialog appeared after skipping negotiation round {rounds}"
            )
            return rounds, frame

        # Otherwise loop and check again — the next round (or final
        # result) may still be transitioning.

    logger.warning(
        f"  Sell post-confirm: hit max_rounds={max_rounds} without seeing "
        "the result dialog — transaction may have failed"
    )
    return rounds, None


def _handle_negotiation(
    flow: dict,
    frame: Image.Image,
    strategy: str = "no",
) -> None:
    """
    Handle the negotiation dialog (already confirmed present via OCR by caller).
    strategy: "no"   → skip, sell at base price
              "once" → use 1 attempt
              "all"  → use all remaining attempts
    """
    from brain.kb import control as _ckb
    nego_step = _step(flow, "negotiation_dialog")

    if strategy == "once":
        for label in _ckb().negotiation_button_labels("use_one"):
            pos = _flow_btn_coords(nego_step, label)
            if pos:
                logger.info(f"  Negotiation: Use 1 chance @ {pos}")
                tap(*pos)
                return
        logger.warning("  Negotiation: 'Use 1 chance' button not found — falling through to No")

    if strategy == "all":
        for label in _ckb().negotiation_button_labels("use_all"):
            pos = _flow_btn_coords(nego_step, label)
            if pos:
                logger.info(f"  Negotiation: Use all @ {pos}")
                tap(*pos)
                return
        logger.warning("  Negotiation: 'Use all' button not found — falling through to No")

    # Default: No (skip negotiation)
    no_pos = None
    for label in _ckb().negotiation_button_labels("skip"):
        no_pos = _flow_btn_coords(nego_step, label)
        if no_pos:
            break
    if no_pos is None:
        t = nego_step["action_target"]
        no_pos = (int(t["tap_x"] * _flow_scale), int(t["tap_y"] * _flow_scale))
        from actions.sail_actions import where_am_i
        loc = where_am_i()
        logger.warning(f"  Negotiation No button not in flow key_elements — using hardcoded fallback @ {no_pos}; actual location: {loc['location']!r}")

    logger.info(f"  Negotiation: Skip (No) @ {no_pos}")
    tap(*no_pos)


# ── Core: sell one good (perception-first) ────────────────────────────────────

_STEP_TIMEOUT = 10.0    # seconds to wait for each screen transition
_STEP_INTERVAL = 0.8    # polling interval


def _sell_one_good(
    flow: dict,
    good_name: str,
    negotiation_strategy: str = "no",
) -> Optional[SellResult]:
    """
    Execute the full sell flow for a single good.
    After every tap the screen is captured and verified before proceeding.
    Returns SellResult on success, None on failure.
    """
    result = SellResult(good=good_name)

    # ── Step 0: Find tile and tap it ─────────────────────────────────────────
    logger.info(f"  Locating tile: {good_name!r}")
    frame = capture_screen()

    # Set scale from actual frame size (flow coords are in 1000px thumbnail space)
    scale = _set_flow_scale(frame)
    logger.debug(f"  Flow coord scale: {scale:.3f} (frame {frame.width}×{frame.height})")

    pos = _find_tile_pos(frame, good_name)
    if pos is None:
        logger.warning(f"  Tile for '{good_name}' not found — skipping")
        return None

    logger.info(f"  Tapping tile: {good_name} @ {pos}")
    tap(*pos)

    # ── Step 1: Wait for Trade Goods Info dialog ──────────────────────────────
    # Confirmed by: "Sales Cost" label (unique to this dialog)
    logger.info("  Waiting for Trade Goods Info dialog…")
    from brain.kb import control as _ckb
    frame, ok = _wait_for_screen(
        *_ckb().dialog_confirmation("trade_goods_info"),
        timeout=_STEP_TIMEOUT,
        interval=_STEP_INTERVAL,
    )
    if not ok:
        logger.warning(f"  Trade Goods Info dialog did not appear for {good_name!r} — aborting")
        return None
    logger.info("  Trade Goods Info dialog confirmed")
    _log_elements(frame, "trade_goods_info_dialog")

    # ── Step 2: Tap Max button ────────────────────────────────────────────────
    # Max sets the quantity bar to green (all available) — does NOT open a number dialog.
    # Use scaled flow coords (ground truth from recorded session).
    # OmniParser logs elements at DEBUG level for verification only.
    _log_elements(frame, "trade_goods_info_dialog")
    info_step = _step(flow, "trade_goods_info_dialog")
    max_pos = _flow_btn_coords(info_step, "max")
    if max_pos is None:
        logger.warning("  Max button not in flow key_elements — falling back to OmniParser/OCR")
        max_pos = _find_button(frame, "max")

    if max_pos:
        logger.info(f"  Tapping Max @ {max_pos}")
        tap(*max_pos)
        time.sleep(0.8)
    else:
        from actions.sail_actions import where_am_i
        loc = where_am_i()
        logger.warning(f"  Max button not found — will try Load with default quantity; actual location: {loc['location']!r} — {loc['detail']}")

    # ── Step 2b: Re-read screen ───────────────────────────────────────────────
    # If, unexpectedly, a number input dialog appeared (e.g. tapped calculator
    # icon instead of Max), handle it by tapping Max-in-keypad then Enter.
    frame = capture_screen()
    from brain.kb import control as _ckb
    if _screen_contains(frame, *_ckb().dialog_confirmation("number_input")):
        logger.info("  Unexpected number input dialog — tapping Max then Enter")
        num_step = _step(flow, "number_input_dialog")
        kp_max_pos = _flow_btn_coords(num_step, "max")
        if kp_max_pos:
            tap(*kp_max_pos)
            time.sleep(0.3)
        enter_pos = _flow_coords(flow, "number_input_dialog")
        tap(*enter_pos)
        time.sleep(0.8)
        frame = capture_screen()

    # ── Step 3: Tap Load button ───────────────────────────────────────────────
    # Use scaled flow coords — Load is a fixed button in the Trade Goods Info dialog.
    load_pos = _flow_btn_coords(info_step, "load button")
    if load_pos is None:
        load_pos = _flow_btn_coords(info_step, "load")
    if load_pos is None:
        logger.warning("  Load not in flow key_elements — falling back to OmniParser/OCR")
        load_pos = _find_button(frame, "load")

    if load_pos is None:
        logger.warning(f"  Load button not found for {good_name!r} — aborting")
        return None

    logger.info(f"  Tapping Load @ {load_pos}")
    tap(*load_pos)

    # ── Step 4: Wait for basket to populate ──────────────────────────────────
    # After Load the dialog closes and we return to the sell grid.
    # The right basket panel shows "Nego. Chance" once an item is loaded.
    logger.info("  Waiting for basket to populate…")
    from brain.kb import control as _ckb
    frame, ok = _wait_for_screen(
        *_ckb().dialog_confirmation("basket_loaded"),
        timeout=_STEP_TIMEOUT,
        interval=_STEP_INTERVAL,
    )
    if not ok:
        logger.warning("  Basket population not confirmed — attempting to proceed")
        frame = capture_screen()

    # ── Step 5: Tap Sell button ───────────────────────────────────────────────
    # The Sell button is in the right panel at a fixed position (turns yellow when basket
    # has items). Flow coords are reliable here; OCR "Sell" may match the tab label.
    basket_step = _step(flow, "basket_loaded_sell_ready")
    sell_coords = _flow_coords(flow, "basket_loaded_sell_ready")
    logger.info(f"  Tapping Sell button @ {sell_coords}")
    tap(*sell_coords)

    # ── Step 6: Wait for Confirm Sales dialog ────────────────────────────────
    logger.info("  Waiting for Confirm Sales dialog…")
    from brain.kb import control as _ckb
    frame, ok = _wait_for_screen(
        *_ckb().dialog_confirmation("confirm_sales"),
        timeout=_STEP_TIMEOUT,
        interval=_STEP_INTERVAL,
    )
    if not ok:
        # unexpected dialog (e.g. load-ratio warning) blocked the Confirm —
        # hand it to the reasoning layer, then re-check for the Confirm.
        if _escalate_unexpected_dialog():
            frame, ok = _wait_for_screen(
                *_ckb().dialog_confirmation("confirm_sales"),
                timeout=_STEP_TIMEOUT, interval=_STEP_INTERVAL,
            )
        if not ok:
            logger.warning("  Confirm Sales dialog did not appear — aborting")
            return None
    logger.info("  Confirm Sales dialog confirmed")
    _log_elements(frame, "confirm_sales_dialog")

    # ── Step 7: Tap Ok to confirm the sale ───────────────────────────────────
    # Phase 3.1: typed DialogModel.actions first; flow_coords fallback.
    ok_pos = _resolve_dialog_ok(frame, flow, "confirm_sales_dialog",
                                handler="market_confirm_sales")
    logger.info(f"  Tapping Ok (confirm sale) @ {ok_pos}")
    tap(*ok_pos)

    # ── Steps 8-9: Negotiation rounds + Result dialog (loop-based) ────────────
    # Replaces the previous single-shot pattern that missed the result
    # dialog when (a) multiple negotiation rounds appeared, or (b) the
    # result dialog transitioned faster than the static 10 s OCR poll
    # could catch it.  See _sell_handle_negotiation_and_wait_result.
    rounds, frame = _sell_handle_negotiation_and_wait_result(
        flow, strategy=negotiation_strategy,
    )
    if rounds > 0:
        result.negotiation_used = True
    if frame is None:
        logger.warning("  Result dialog did not appear — transaction may have failed")
        return None

    logger.info("  Result dialog confirmed — parsing…")
    _log_elements(frame, "sell_result_dialog")

    # ── Step 10: Parse result ─────────────────────────────────────────────────
    result = _parse_sell_result(frame, good_name, result)

    # ── Step 11: Dismiss result dialog ───────────────────────────────────────
    # Phase 3.1: typed DialogModel.actions first; flow_coords fallback.
    ok_pos = _resolve_dialog_ok(frame, flow, "sell_result_dialog",
                                handler="market_sell_result")
    logger.info(f"  Tapping Ok (dismiss result) @ {ok_pos}")
    tap(*ok_pos)
    time.sleep(1.5)

    logger.info(
        f"  Sold {good_name}  "
        f"total={result.total_amount:,}  profit={result.profit:,}  "
        f"fame+{result.trade_fame}  exp+{result.trade_exp}"
    )
    return result


# ── Public API ─────────────────────────────────────────────────────────────────

def sell_all_cargo(
    port: str,
    negotiation_strategy: str = "no",
) -> List[SellResult]:
    """
    Sell all goods in the ship's cargo at the current market in a single transaction.

    Uses the "Load All" button to place all cargo goods into the sell basket at once,
    then executes one Sell → Confirm → (optional negotiation) → Result cycle.

    Args:
        port:                  Current port name (for logging / KB).
        negotiation_strategy:  "no"   — skip negotiation (guaranteed base price)
                               "once" — use 1 attempt per round (+~23% if successful)
                               "all"  — use all remaining attempts

    Returns a list with a single aggregate SellResult covering all goods sold.
    """
    from vision.market_reader import read_market_page_claude

    flow = _load_flow("market_sell")
    logger.info(f"[{port}] Selling all cargo via Load All (negotiation={negotiation_strategy!r})")

    # Switch to Sell tab
    tap(*MARKET_COORDS["sell"])
    time.sleep(1.5)

    # Gate on the reliable "Cargo X/Y" counter, NOT the sell-page grid parse —
    # the sell layout differs from the buy grid so that parse is unreliable and
    # must not abort the sale.  Only bail when cargo is CONFIDENTLY empty
    # (counter read AND used == 0); a failed read (0,0) proceeds to Load All.
    frame = capture_screen()
    _set_flow_scale(frame)
    used, total = _read_cargo_capacity(frame)
    if total > 0 and used == 0:
        logger.info(f"[{port}] Cargo empty ({used}/{total}) — nothing to sell")
        return []

    # Good names / sellable list are best-effort (sell-page parse is unreliable):
    # used for logging, KB, and the per-good fallback — NOT to gate the sale.
    goods = read_market_page_claude(frame, tab="sell", port=port)
    sellable = [g for g in goods if not g.sold_out]
    good_names = [g.name for g in sellable] or ["<cargo>"]
    logger.info(f"[{port}] Cargo {used}/{total}; selling (goods best-effort: {good_names})")

    # ── Tap Load All ──────────────────────────────────────────────────────────
    # Structural detection first (OmniParser/OCR) so the tap lands on the actual
    # button regardless of the recorded flow's baked offset; flow coords are the
    # fallback.  "Load All" lives in the bottom action band.
    grid_step = _step(flow, "sell_tab_goods_grid")
    load_all_pos = _find_button(frame, "Load All", y_min=int(frame.height * 0.85))
    if load_all_pos is None:
        load_all_pos = _flow_btn_coords(grid_step, "load all")
    if load_all_pos is None:
        logger.warning("  'Load All' not found by vision or flow — falling back to per-good sell")
        return _sell_all_cargo_per_good(flow, port, sellable, negotiation_strategy)

    logger.info(f"  Tapping Load All @ {load_all_pos}")
    tap(*load_all_pos)

    # ── Wait for basket to populate ───────────────────────────────────────────
    logger.info("  Waiting for basket to populate…")
    frame, ok = _wait_for_screen("nego", timeout=_STEP_TIMEOUT)
    if not ok:
        logger.warning("  Basket population not confirmed — attempting to proceed")
        frame = capture_screen()

    # ── Tap Sell button ───────────────────────────────────────────────────────
    # Structural first (bottom-right action button); flow coords fallback.
    sell_pos = _find_button(frame, "Sell",
                            x_min=int(frame.width * 0.60),
                            y_min=int(frame.height * 0.85))
    if sell_pos is None:
        sell_pos = _flow_coords(flow, "basket_loaded_sell_ready")
    logger.info(f"  Tapping Sell @ {sell_pos}")
    tap(*sell_pos)

    # ── Wait for Confirm Sales dialog ─────────────────────────────────────────
    logger.info("  Waiting for Confirm Sales dialog…")
    frame, ok = _wait_for_screen("confirm", timeout=_STEP_TIMEOUT)
    if not ok:
        # unexpected dialog blocked the Confirm — hand it to the reasoning layer.
        if _escalate_unexpected_dialog():
            frame, ok = _wait_for_screen("confirm", timeout=_STEP_TIMEOUT)
        if not ok:
            logger.warning("  Confirm Sales dialog did not appear — aborting")
            return []
    logger.info("  Confirm Sales dialog confirmed")

    # ── Tap OK ────────────────────────────────────────────────────────────────
    ok_pos = _resolve_dialog_ok(frame, flow, "confirm_sales_dialog",
                                handler="market_confirm_sales")
    tap(*ok_pos)

    # ── Handle negotiation rounds + wait for Result dialog (loop-based) ──────
    # Replaces the previous single-shot pattern.  See
    # _sell_handle_negotiation_and_wait_result for the rationale.
    rounds, frame = _sell_handle_negotiation_and_wait_result(
        flow, strategy=negotiation_strategy,
    )
    negotiated = rounds > 0
    if frame is None:
        logger.warning("  Result dialog did not appear — transaction may have failed")
        return []
    logger.info("  Result dialog confirmed — parsing…")

    # ── Parse and dismiss ─────────────────────────────────────────────────────
    result = SellResult(good=", ".join(good_names), negotiated=negotiated)
    result = _parse_sell_result(frame, result.good, result)

    ok_pos = _resolve_dialog_ok(frame, flow, "sell_result_dialog",
                                handler="market_sell_result")
    tap(*ok_pos)
    time.sleep(1.5)

    logger.info(
        f"[{port}] Sell complete — {len(good_names)} goods\n"
        f"  Total received : {result.total_amount:,}\n"
        f"  Profit         : {result.profit:,}\n"
        f"  Trade Fame     : +{result.trade_fame}\n"
        f"  Trade EXP      : +{result.trade_exp}"
    )

    results = [result]
    _save_sell_summary(port, results)
    return results


def _sell_all_cargo_per_good(
    flow: dict,
    port: str,
    sellable: list,
    negotiation_strategy: str,
) -> List[SellResult]:
    """Fallback: sell each good individually (original approach)."""
    results = []
    for good in sellable:
        logger.info(f"[{port}] === Selling: {good.name} ===")
        r = _sell_one_good(flow, good.name, negotiation_strategy)
        if r:
            results.append(r)
        time.sleep(1.0)

    total  = sum(r.total_amount for r in results)
    profit = sum(r.profit       for r in results)
    logger.info(
        f"[{port}] Sell complete (per-good)  {len(results)}/{len(sellable)}\n"
        f"  Total : {total:,}  Profit : {profit:,}"
    )
    if results:
        _save_sell_summary(port, results)
    return results


def _save_sell_summary(port: str, results: List[SellResult]) -> None:
    """Append a sell transaction summary to the market KB."""
    import json
    from datetime import datetime, timezone
    from memory.market_kb import load_market, _market_path, _MARKETS_DIR

    _MARKETS_DIR.mkdir(parents=True, exist_ok=True)
    record = load_market(port)
    record.setdefault("transactions", []).append({
        "type": "sell",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "goods": [
            {
                "name": r.good,
                "total_amount": r.total_amount,
                "profit": r.profit,
                "tax": r.tax,
                "surcharge": r.surcharge,
                "trade_exp": r.trade_exp,
                "trade_fame": r.trade_fame,
                "negotiated": r.negotiation_used,
            }
            for r in results
        ],
        "total_amount": sum(r.total_amount for r in results),
        "total_profit": sum(r.profit for r in results),
    })

    _market_path(port).write_text(
        json.dumps(record, indent=2, ensure_ascii=False)
    )
    logger.debug(f"Transaction saved to market KB: {port}")


# ── Buy flow ───────────────────────────────────────────────────────────────────

@dataclass
class BuyOrder:
    """A single good to purchase."""
    name: str
    quantity: object = "max"    # int for specific qty, "max" for all available


@dataclass
class BuyResult:
    """Aggregate result of one purchase transaction (may cover multiple goods)."""
    goods: List[str] = None
    total_amount: int = 0
    purchase_cost: int = 0
    tax: int = 0
    discount: int = 0
    negotiation_rounds: int = 0

    def __post_init__(self):
        if self.goods is None:
            self.goods = []


def _detect_keypad_digit_positions(frame: Image.Image) -> dict:
    """
    Detect where the numeric keypad digit buttons actually are on screen.

    The keypad dialog occupies the right portion of the screen.  We crop to
    x > 1350 (x=1320 has been observed to be outside the dialog) and run OCR
    with digits-only allowlist.  Digit *buttons* are large and roughly square
    (~50-120px wide); price text in the underlying dialog groups into wide
    multi-digit strings that EasyOCR reports with proportionally wider bboxes.

    Returns {digit_str: (cx, cy)} in full-frame pixel coordinates.
    Any digit not detected is absent from the dict (caller falls back to
    hardcoded positions for missing entries).
    """
    from vision.ocr import _get_reader

    x_off, y_off = 1350, 100
    crop = frame.crop((x_off, y_off, frame.width, frame.height - 100))
    arr = np.array(crop)
    raw = _get_reader().readtext(arr, detail=1, allowlist="0123456789")

    positions: dict = {}
    for bbox, text, conf in raw:
        text = text.strip()
        if not text or text not in "0123456789" or len(text) != 1:
            continue
        if conf < 0.45:
            continue
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        # Button digit: large (30-130px), roughly square (not a wide price string)
        if not (30 <= w <= 130 and 20 <= h <= 90):
            continue
        if w > 2.5 * h:
            continue
        cx = int((min(xs) + max(xs)) / 2) + x_off
        cy = int((min(ys) + max(ys)) / 2) + y_off
        if text not in positions:
            positions[text] = (cx, cy)
            logger.debug(f"  Keypad detected: '{text}' @ ({cx}, {cy})")

    logger.debug(f"  Keypad auto-detected {len(positions)}/10 digit positions")
    return positions


def _enter_keypad_quantity(flow: dict, quantity: int) -> bool:
    """
    Enter a specific quantity on the numeric keypad overlay.
    Assumes the keypad is already visible on screen.
    Returns True on success, False if entry failed (e.g. dialog dismissed).

    Strategy:
      1. Detect actual digit button positions from screen (OCR on keypad region).
      2. Fall back to hardcoded positions (adjusted from flow geometry) for any
         digits that OCR didn't find.
      3. After EACH digit tap, verify the keypad is still open.  If the dialog
         was dismissed (tap landed outside), return False immediately so the
         caller can handle the situation rather than blindly continuing.
    """
    # Fallback positions in thumbnail space (1000px-wide frame).
    # Phone-style layout: 1-2-3 top, 7-8-9 third row.
    # Columns shifted +50 rightward vs original estimates (observed: old col1
    # at x=550→1320 was outside dialog; right-shift keeps us inside).
    _DIGIT_FALLBACK: dict = {
        "1": (600, 185), "2": (660, 185), "3": (720, 185),
        "4": (600, 230), "5": (660, 230), "6": (720, 230),
        "7": (600, 275), "8": (660, 275), "9": (720, 275),
        "0": (660, 320),
    }

    kp_step = _step(flow, "numeric_keypad_input")

    # ── Clear existing value ──────────────────────────────────────────────────
    bs_pos = _flow_btn_coords(kp_step, "backspace")
    if bs_pos:
        for _ in range(6):
            tap(*bs_pos)
            time.sleep(0.08)
    else:
        logger.warning("  Keypad: backspace not in flow")

    # ── Detect actual digit positions ─────────────────────────────────────────
    frame = capture_screen()
    detected = _detect_keypad_digit_positions(frame)

    # ── Tap each digit ────────────────────────────────────────────────────────
    for ch in str(quantity):
        if ch in detected:
            pos = detected[ch]
            src = "detected"
        else:
            thumb = _DIGIT_FALLBACK.get(ch)
            if thumb is None:
                logger.warning(f"  Keypad: no position for digit '{ch}'")
                return False
            pos = (int(thumb[0] * _flow_scale), int(thumb[1] * _flow_scale))
            src = "fallback"

        logger.debug(f"  Keypad: tapping '{ch}' @ {pos} ({src})")
        tap(*pos)
        time.sleep(0.2)

        # Verify the keypad is still open — tapping outside dismisses the dialog
        frame = capture_screen()
        if not _screen_contains(frame, *_ckb_lazy("number_input")):
            logger.warning(
                f"  Keypad: dialog dismissed after tapping '{ch}' @ {pos} — aborting"
            )
            return False
        # Re-detect positions on fresh frame (layout unchanged, but fresh is safer)
        detected = _detect_keypad_digit_positions(frame)

    # ── Tap Enter ─────────────────────────────────────────────────────────────
    enter_pos = _flow_coords(flow, "numeric_keypad_input")
    logger.debug(f"  Keypad: tapping Enter @ {enter_pos}")
    tap(*enter_pos)
    time.sleep(0.5)
    return True


def _buy_load_one_good(flow: dict, order: BuyOrder) -> bool:
    """
    Load a single good into the purchase basket.
    Tap its tile → set quantity → tap Load.
    Returns True if the good was successfully added to basket.
    """
    frame = capture_screen()
    _set_flow_scale(frame)

    # Find the tile
    pos = _find_tile_pos(frame, order.name)
    if pos is None:
        logger.warning(f"  Tile for '{order.name}' not found — skipping")
        return False

    logger.info(f"  Tapping tile: {order.name} @ {pos}")
    tap(*pos)

    # Wait for Trade Goods Info dialog
    # Buy dialog is identified by "purchase cost" (unique — sell dialog shows "sales cost")
    logger.info(f"  Waiting for Trade Goods Info dialog…")
    frame, ok = _wait_for_screen(*_ckb_lazy("buy_result"), timeout=_STEP_TIMEOUT)
    if not ok:
        logger.warning(f"  Trade Goods Info dialog did not appear for {order.name!r}")
        return False
    _log_elements(frame, "trade_goods_info_dialog")

    info_step = _step(flow, "trade_goods_info_dialog")

    if order.quantity == "max":
        # Tap Max button — sets quantity bar to full without opening keypad
        max_pos = _flow_btn_coords(info_step, "max")
        if max_pos:
            logger.info(f"  Tapping Max @ {max_pos}")
            tap(*max_pos)
            time.sleep(0.6)
        else:
            logger.warning("  Max button not in flow — will load with default quantity")

    else:
        # Tap the number display field to open numeric keypad
        num_field_pos = _flow_btn_coords(info_step, "quantity display")
        if num_field_pos is None:
            num_field_pos = _flow_btn_coords(info_step, "number input")
        if num_field_pos:
            logger.info(f"  Tapping quantity field @ {num_field_pos} to open keypad")
            tap(*num_field_pos)
        else:
            logger.warning("  Quantity field not in flow — falling back to OmniParser")
            frame = capture_screen()
            num_field_pos = _find_button(frame, "quantity display", "number input")
            if num_field_pos:
                tap(*num_field_pos)

        # Wait for keypad
        frame, ok = _wait_for_screen(*_ckb_lazy("number_input"), timeout=_STEP_TIMEOUT)
        if not ok:
            logger.warning("  Numeric keypad did not appear")
            return False

        logger.info(f"  Entering quantity: {order.quantity}")
        if not _enter_keypad_quantity(flow, int(order.quantity)):
            logger.warning("  Quantity entry failed")
            return False

        # After Enter, keypad closes and we're back in the dialog
        frame, ok = _wait_for_screen(*_ckb_lazy("buy_result"), timeout=_STEP_TIMEOUT)
        if not ok:
            logger.warning("  Dialog did not return after keypad entry")
            return False

    # Tap Load button to add to basket
    load_pos = _flow_btn_coords(info_step, "load button")
    if load_pos is None:
        load_pos = _flow_btn_coords(info_step, "load")
    if load_pos is None:
        logger.warning(f"  Load button not in flow for {order.name!r} — aborting")
        return False

    logger.info(f"  Tapping Load @ {load_pos}")
    tap(*load_pos)

    # Wait for dialog to close — "purchase cost" should disappear once Load is tapped
    # (dialog closes and we're back on the goods grid).
    # Use a short poll: if "purchase cost" is gone within 5s, the good was loaded.
    deadline = time.time() + 5.0
    loaded_ok = False
    while time.time() < deadline:
        time.sleep(0.6)
        chk = capture_screen()
        if not _screen_contains(chk, *_ckb_lazy("buy_result")):
            loaded_ok = True
            break

    if loaded_ok:
        logger.info(f"  {order.name} added to basket")
    else:
        logger.warning(f"  {order.name}: dialog still open after Load — may not have loaded")
    return loaded_ok


def _buy_handle_negotiation(
    flow: dict,
    strategy: str,
) -> tuple:
    """
    Handle the buy negotiation dialog — may loop if each attempt succeeds.
    strategy: "no" / "once" / "all"

    Returns (rounds: int, result_frame: Optional[Image.Image]).
    result_frame is non-None when the purchase result dialog was detected
    DURING negotiation handling (e.g. negotiation auto-resolved or failed and
    the result appeared before the loop could check again).  In that case the
    caller must NOT call _wait_for_screen("total amount") — just use the frame.
    """
    nego_step = _step(flow, "negotiation_dialog")
    rounds = 0
    max_rounds = 10  # safety cap

    while rounds < max_rounds:
        time.sleep(1.2)
        frame = capture_screen()

        # ── Check for result dialog FIRST ────────────────────────────────────
        # Negotiation may have auto-resolved (failed/succeeded) and the result
        # dialog appeared.  If we don't catch it here, we might tap it thinking
        # it's the negotiation dialog and accidentally dismiss it.
        if _screen_contains(frame, *_ckb_lazy("sell_result")):
            logger.info(
                f"  Result dialog detected after {rounds} negotiation round(s) "
                f"— returning frame to caller"
            )
            return rounds, frame

        if not _screen_contains(frame, "negotiat", "chance", "should i"):
            logger.info(f"  Negotiation dialog gone after {rounds} round(s)")
            break   # dialog gone — done

        rounds += 1
        logger.info(f"  Negotiation dialog (round {rounds}) — strategy: {strategy!r}")
        _log_elements(frame, "negotiation_dialog")

        if strategy == "no":
            no_pos = (_flow_btn_coords(nego_step, "no button")
                      or _flow_btn_coords(nego_step, "no"))
            if no_pos is None:
                t = nego_step["action_target"]
                no_pos = (int(t["tap_x"] * _flow_scale), int(t["tap_y"] * _flow_scale))
            tap(*no_pos)
            # After tapping No, check once if result appeared
            time.sleep(1.2)
            frame = capture_screen()
            if _screen_contains(frame, *_ckb_lazy("sell_result")):
                logger.info("  Result dialog appeared after skipping negotiation")
                return rounds, frame
            break

        # "once" or "all" — use 1 chance per loop iteration
        once_pos = _flow_btn_coords(nego_step, "use 1 chance")
        if once_pos is None:
            logger.warning("  'Use 1 chance' not found — skipping negotiation")
            break
        tap(*once_pos)
        # Loop back — top of loop checks for result dialog before negotiation

    return rounds, None


def _parse_buy_result(frame: Image.Image, result: BuyResult) -> BuyResult:
    """
    Read the purchase result dialog fields.

    Result dialog rows (from flow.json):
      Purchase Cost   2,724,525
      Tax             320,638
      Discount        -1,197,038
      Total Amount    1,848,125
      Balance         15,609,035,436
    """
    arr = np.array(frame)
    from vision.ocr import _get_reader
    raw = _get_reader().readtext(arr, detail=1)
    tokens = [(t.strip(), c) for _, t, c in raw if c > 0.3 and t.strip()]

    from brain.kb import control
    label_map = control().buy_result_label_map()

    values: dict = {}
    for i, (text, conf) in enumerate(tokens):
        tl = text.lower()
        for label, (field_name, max_val) in label_map.items():
            if label not in tl or field_name in values:
                continue
            for j in range(i + 1, min(i + 4, len(tokens))):
                n = _parse_number(tokens[j][0])
                if n is not None and 0 < n <= max_val:
                    values[field_name] = n
                    break

    result.purchase_cost = values.get("purchase_cost", 0)
    result.tax           = values.get("tax",           0)
    result.discount      = values.get("discount",      0)
    result.total_amount  = values.get("total_amount",  0)

    logger.debug(
        f"  Buy result: cost={result.purchase_cost:,}  tax={result.tax:,}  "
        f"discount={result.discount:,}  total={result.total_amount:,}"
    )
    return result


# ── Bulk-buy helpers ───────────────────────────────────────────────────────────
#
# "Put in Bulk" is a checkbox at the bottom-left of the Purchase screen,
# after the Language Effect message box.  When checked, tapping a good tile
# loads the full available stock (up to remaining cargo capacity) directly into
# the basket — no Trade Goods Info dialog opens.  The tile greys out if fully
# loaded; the cargo counter in the right panel (Cargo: used/total) updates.
#
# When unchecked, tapping a tile opens the dialog flow (existing code path).


def _find_put_in_bulk_anchor(frame: Image.Image) -> Optional[Tuple[int, int]]:
    """
    Return the full-frame (cx, cy) of the word "Put" in the "Put in Bulk" label.

    Layout at bottom-left of the Purchase screen:
        [☐ Put in Bulk]  [☐ Apply Load Ratio]

    We anchor on "Put" (the leftmost word) so that:
    - The checkbox we tap/inspect is unambiguously to the LEFT of "Put".
    - The "Apply Load Ratio" checkbox to the RIGHT of "Bulk" is never touched.

    Returns None if the label is not found.
    """
    h = frame.height
    # Search the bottom 120px, left third — "Put in Bulk" is well to the left
    bottom = frame.crop((0, h - 120, frame.width // 3, h))
    tokens = _ocr_frame(bottom)

    # Prefer a token that contains "put" at the start (OCR may merge words)
    for text, _, cx, cy in tokens:
        tl = text.lower().strip()
        if tl.startswith("put") or tl == "put":
            return cx, cy + (h - 120)   # convert to full-frame y

    # Fallback: any token containing "put in" (merged OCR)
    for text, _, cx, cy in tokens:
        if "put in" in text.lower():
            return cx, cy + (h - 120)

    logger.debug("'Put in Bulk' anchor ('Put') not found in bottom strip")
    return None


def _is_bulk_mode_on(frame: Image.Image) -> bool:
    """
    Return True if the 'Put in Bulk' checkbox shows a green checkmark.

    The checkbox sits immediately to the LEFT of the word "Put".
    Green detection: G−R > 50 and G−B > 50 and G > 100, ≥5 pixels in the
    ~50×50px region to the left of the "Put" anchor.
    """
    anchor = _find_put_in_bulk_anchor(frame)
    if anchor is None:
        return False

    put_x, put_y = anchor
    arr = np.array(frame)

    # Checkbox is ~30-60px to the left of "Put", vertically centred on it
    x1 = max(0, put_x - 70)
    x2 = max(0, put_x - 5)
    y1 = max(0, put_y - 25)
    y2 = min(arr.shape[0], put_y + 25)
    region = arr[y1:y2, x1:x2]
    if region.size == 0:
        return False

    r = region[:, :, 0].astype(int)
    g = region[:, :, 1].astype(int)
    b = region[:, :, 2].astype(int)
    green_pixels = int(((g - r > 50) & (g - b > 50) & (g > 100)).sum())
    is_on = green_pixels >= 5
    logger.debug(f"'Put in Bulk' checkbox: {green_pixels} green px → {'ON' if is_on else 'OFF'}")
    return is_on


def _ensure_bulk_mode(target_on: bool, frame: Image.Image) -> bool:
    """
    Ensure 'Put in Bulk' matches *target_on*.  Taps the checkbox if needed.
    Returns True when the target state is confirmed or assumed correct.

    Detection failure policy:
      - If the "Put" anchor cannot be found at all, we cannot read OR change the
        checkbox state.  Assume it is already in the desired state and return True
        so the caller proceeds — a wrong assumption will surface as a downstream
        error (e.g. dialog appears when bulk expected, or no dialog when dialog
        expected) rather than a silent skip of the whole buy step.
      - If the anchor IS found but the green-pixel check is ambiguous after a
        toggle attempt, also assume success and proceed.
    """
    # _is_bulk_mode_on returns True only when the green checkmark is positively
    # detected.  A False result means either OFF or "can't detect" — ambiguous.
    # Rule: only tap the checkbox when the WRONG state is confirmed:
    #   - target ON : tap only if green is positively detected as absent AND
    #                 it was previously confirmed ON (we can't safely distinguish
    #                 "OFF" from "detection failed") → so never tap when targeting ON
    #                 unless we just saw it turn OFF.  For now: assume it is already ON.
    #   - target OFF: tap only if green IS positively detected (definitely ON).
    #
    # This prevents the common failure mode where detection returns False on a checkbox
    # that is already ON, causing an unintended toggle that breaks bulk behaviour.

    current = _is_bulk_mode_on(frame)
    anchor = _find_put_in_bulk_anchor(frame)

    if anchor is None:
        logger.debug("'Put in Bulk' label not found — assuming already "
                     f"{'ON' if target_on else 'OFF'}, proceeding")
        return True

    if target_on:
        if current:
            logger.debug("'Put in Bulk' confirmed ON")
        else:
            # False = "OFF or undetectable" — don't tap; assume it's already ON
            logger.debug("'Put in Bulk' not confirmed ON (green not detected) — "
                         "assuming already ON, proceeding without toggle")
        return True

    else:  # target OFF
        if not current:
            logger.debug("'Put in Bulk' not detected as ON — assuming already OFF")
            return True
        # Green IS detected → definitely ON → need to turn it OFF
        put_x, put_y = anchor
        chk_x = put_x - 40
        chk_y = put_y
        logger.info(f"  Tapping 'Put in Bulk' checkbox @ ({chk_x}, {chk_y}) (ON → OFF)")
        tap(chk_x, chk_y)
        time.sleep(0.5)
        return True


def _buy_load_bulk(good_name: str, cargo_used: int) -> Tuple[bool, int]:
    """
    Load one good in 'Put in Bulk' mode: tap tile, wait for cargo counter to update.
    Returns (success, new_cargo_used).
    cargo_used is the current basket count before the tap (used to detect the update).

    Optimistic fallback: if the cargo counter can't be read but no "purchase cost"
    dialog appeared, the good was almost certainly loaded (bulk mode is on, tile tap
    loaded directly into basket).  Return (True, cargo_used) in that case so the
    basket state — not the unreadable counter — is the source of truth.
    """
    frame = capture_screen()
    pos = _find_tile_pos(frame, good_name)
    if pos is None:
        logger.warning(f"  [bulk] Tile for '{good_name}' not found — skipping")
        return False, cargo_used

    logger.info(f"  [bulk] Tapping tile: {good_name} @ {pos}")
    tap(*pos)

    # Wait for the cargo counter in the right panel to increase.
    # Also watch for a "purchase cost" dialog — that signals bulk mode is OFF.
    deadline = time.time() + 6.0
    dialog_appeared = False
    while time.time() < deadline:
        time.sleep(0.6)
        chk = capture_screen()
        if _screen_contains(chk, *_ckb_lazy("buy_result")):
            dialog_appeared = True
            logger.warning(f"  [bulk] {good_name}: 'purchase cost' dialog appeared — "
                           f"bulk mode appears OFF; dismissing and marking as failed")
            press_back()
            return False, cargo_used
        used, total = _read_cargo_capacity(chk)
        if used > cargo_used:
            logger.info(f"  [bulk] {good_name}: +{used - cargo_used} units "
                        f"(cargo {used}/{total if total else '?'})")
            return True, used

    if not dialog_appeared:
        # Cargo counter unreadable but no dialog → bulk tap went through.
        # Treat as success; the basket has the good even though we can't verify count.
        logger.info(f"  [bulk] {good_name}: cargo counter unreadable but no dialog appeared "
                    f"— assuming loaded (optimistic)")
        return True, cargo_used

    return False, cargo_used


def buy_goods(
    orders: List[BuyOrder],
    port: str,
    negotiation_strategy: str = "once",
) -> BuyResult:
    """
    Buy a list of goods at the current market (Purchase tab must be accessible).

    Each BuyOrder specifies a good name and quantity ("max" or a specific integer).
    All goods are loaded into the basket first, then purchased in a single transaction.

    Args:
        orders:                List of BuyOrder(name, quantity).
        port:                  Current port name (for logging/KB).
        negotiation_strategy:  "no"   — skip negotiation (pay full price)
                               "once" — use 1 attempt per round (default; saves ~20%)
                               "all"  — use all remaining attempts each round

    Returns a single BuyResult covering the whole transaction.
    """
    flow = _load_flow("market_buy")
    result = BuyResult(goods=[o.name for o in orders])

    logger.info(f"[{port}] Buying {len(orders)} good(s): {[o.name for o in orders]}")

    # Switch to Purchase tab
    tap(*MARKET_COORDS["purchase"])
    time.sleep(1.5)

    # ── Dispatch: bulk mode (all "max") vs dialog mode (any specific quantity) ──
    #
    # Bulk mode ("Put in Bulk" checkbox ON):
    #   Tap tile → entire available stock loads straight into basket, no dialog.
    #   Fastest path; used by auto_buy() which always requests quantity="max".
    #
    # Dialog mode ("Put in Bulk" checkbox OFF):
    #   Tap tile → Trade Goods Info dialog → set quantity → Load.
    #   Used when a specific quantity is requested (e.g. BuyOrder("Horses", 200)).
    #   If bulk mode is currently ON it will be toggled OFF first.

    use_bulk = all(o.quantity == "max" for o in orders)
    loaded: List[str] = []

    if use_bulk:
        frame = capture_screen()
        _set_flow_scale(frame)
        _ensure_bulk_mode(True, frame)  # logs state; never taps when targeting ON

        used, total = _read_cargo_capacity(capture_screen())
        logger.info(f"[{port}] [bulk] Cargo before load: {used}/{total if total else '?'}")

        for order in orders:
            if total > 0 and used >= total:
                logger.info(f"  Cargo full — skipping {order.name} and remaining goods")
                break
            logger.info(f"[{port}] [bulk] Loading: {order.name}")
            ok, used = _buy_load_bulk(order.name, used)
            if ok:
                loaded.append(order.name)
            time.sleep(0.5)

        if not loaded:
            # No tiles were found at all (all _find_tile_pos calls returned None).
            # This is different from "tiles found but counter unreadable" — those
            # return True optimistically now.  Zero tiles found means the goods
            # grid is empty or OCR failed to locate any of the requested goods.
            logger.warning(f"[{port}] [bulk] No tiles found for any requested goods — "
                           f"aborting bulk load (not falling back to dialog; basket state unknown)")
            return result

    if not use_bulk:
        # Ensure bulk mode is OFF so tile taps open the dialog
        frame = capture_screen()
        if _is_bulk_mode_on(frame):
            _ensure_bulk_mode(False, frame)

        for order in orders:
            logger.info(f"[{port}] === Loading: {order.name} (qty={order.quantity}) ===")
            ok = _buy_load_one_good(flow, order)
            if ok:
                loaded.append(order.name)
            time.sleep(0.8)

    if not loaded:
        logger.warning(f"[{port}] No goods loaded into basket — aborting purchase")
        return result

    result.goods = loaded

    # ── Find and tap Purchase button ──────────────────────────────────────────
    # Try dynamic detection first (OmniParser / OCR) so the tap lands on the
    # actual button regardless of minor layout shifts.  Static flow coords are
    # the fallback, not the primary method.
    frame = capture_screen()
    _btn_labels = _ckb_lazy("purchase")  # reads action_buttons.purchase from market.json
    # The Purchase action button is always at the bottom-right.
    # Constrain to right 40% and bottom 35% to avoid matching the screen title
    # (top-left) or the Language Effect tile (bottom-left).
    _btn_y_min = int(frame.height * 0.65)
    _btn_x_min = int(frame.width  * 0.60)
    purchase_pos = _find_button(frame, *_btn_labels, y_min=_btn_y_min, x_min=_btn_x_min) if _btn_labels else None
    if purchase_pos:
        logger.info(
            f"[{port}] Basket loaded: {loaded} — tapping Purchase (found @ {purchase_pos})"
        )
        tap(*purchase_pos)
    else:
        fallback = _flow_coords(flow, "basket_building")
        logger.warning(
            f"[{port}] Basket loaded: {loaded} — Purchase button not found by vision; "
            f"tapping flow-coord fallback @ {fallback}"
        )
        tap(*fallback)

    # Wait for Confirm Purchase dialog — look for "purchase cost" column header
    # which is unique to this dialog (avoids matching "confirm" elsewhere).
    logger.info("  Waiting for Confirm Purchase dialog…")
    frame, ok = _wait_for_screen(*_ckb_lazy("confirm_purchase"), timeout=_STEP_TIMEOUT)
    if not ok:
        # Confirm dialog never appeared — basket is likely empty (items already
        # purchased in a previous run) or the Purchase button was disabled.
        # Do NOT enter recovery: recovery would loop trying to tap the disabled
        # Purchase button forever.  Just exit the market gracefully.
        logger.warning("  Confirm Purchase dialog did not appear — basket likely empty; exiting market")
        from actions.adb_actions import tap as _tap_home
        _tap_home(2300, 45)   # home button — exits building to port_overworld
        time.sleep(2.0)
        return result
    logger.info("  Confirm Purchase dialog confirmed")

    # ── Tap OK to confirm ─────────────────────────────────────────────────────
    # The Confirm Purchase dialog shows a table of all selected goods.  With
    # more goods the table grows taller, pushing the OK/Cancel buttons off the
    # bottom of the screen.
    #
    # Strategy:
    #   1. Try to find OK via OmniParser/OCR.  If visible, tap it directly.
    #   2. If not found (button below viewport), swipe up inside the dialog to
    #      scroll the content and reveal the buttons, then retry.
    #   3. After each tap, verify the dialog actually closed before moving on.
    from brain.dismissal_telemetry import record as _telem
    for attempt in range(3):
        # Phase 3.1: try DialogModel first (typed structural detection),
        # then fall back to keyword _find_button on the same frame.
        ok_pos = _dialog_ok_pos(frame)
        if ok_pos is not None:
            _telem("market_confirm_purchase", "typed")
            logger.info(f"  OK button via DialogModel @ {ok_pos} [attempt {attempt + 1}]")
        else:
            ok_pos = _find_button(frame, "ok", "OK")
            if ok_pos is not None:
                _telem("market_confirm_purchase", "legacy")
                logger.info(f"  OK button via _find_button @ {ok_pos} [attempt {attempt + 1}]")
            elif attempt < 2:
                # Button not visible — scroll down inside the dialog to reveal it.
                # Swipe from lower-center upward (scrolls content up → reveals bottom).
                logger.info(f"  OK button not visible — scrolling dialog to reveal buttons [attempt {attempt + 1}]")
                from actions.adb_actions import swipe as adb_swipe
                adb_swipe(1200, 700, 1200, 300, duration_ms=400)
                time.sleep(1.0)
                frame = capture_screen()
                continue   # re-try detection on the fresh frame
            else:
                # Last resort: use flow coords
                ok_pos = _flow_coords(flow, "confirm_purchase_dialog")
                _telem("market_confirm_purchase", "noop")
                logger.warning(f"  OK button still not found — using flow coords {ok_pos}")

        logger.info(f"  Tapping OK (confirm) @ {ok_pos}")
        tap(*ok_pos)

        # Verify the dialog closed
        time.sleep(1.5)
        frame = capture_screen()
        if not _screen_contains(frame, *_ckb_lazy("confirm_sales")):
            logger.info("  Confirm Purchase dialog dismissed")
            break
        logger.warning(f"  Dialog still visible after OK tap — retrying (attempt {attempt + 1})")
    else:
        logger.warning("  Could not dismiss Confirm Purchase dialog after 3 attempts")

    # Handle negotiation (may loop multiple rounds)
    # Returns (rounds, result_frame) — result_frame is non-None if the result
    # dialog was already detected during negotiation (don't wait again).
    rounds, result_frame = _buy_handle_negotiation(flow, negotiation_strategy)
    result.negotiation_rounds = rounds

    # Wait for result dialog (or use the frame already captured by negotiation handler)
    logger.info("  Waiting for purchase result dialog…")
    if result_frame is not None:
        logger.info("  Result dialog already detected during negotiation handling — using captured frame")
        frame = result_frame
    else:
        frame, ok = _wait_for_screen(*_ckb_lazy("sell_result"), timeout=_STEP_TIMEOUT)
        if not ok:
            logger.warning("  Purchase result dialog did not appear")
            return result
    logger.info("  Purchase result dialog confirmed")
    _log_elements(frame, "purchase_result_dialog")

    # Parse result
    result = _parse_buy_result(frame, result)

    # Dismiss result dialog — Phase 3.1: typed DialogModel first.
    ok_pos = _resolve_dialog_ok(frame, flow, "purchase_result_dialog",
                                handler="market_purchase_result")
    logger.info(f"  Tapping OK (dismiss result) @ {ok_pos}")
    tap(*ok_pos)
    time.sleep(1.5)

    logger.info(
        f"[{port}] Buy complete  {len(loaded)}/{len(orders)} goods\n"
        f"  Total paid     : {result.total_amount:,}\n"
        f"  Purchase cost  : {result.purchase_cost:,}\n"
        f"  Tax            : {result.tax:,}\n"
        f"  Discount       : {result.discount:,}\n"
        f"  Nego rounds    : {result.negotiation_rounds}"
    )

    _save_buy_summary(port, result)
    return result


def auto_buy(
    port: str,
    negotiation_strategy: str = "once",
    destination: Optional[str] = None,
) -> BuyResult:
    """
    Automatically buy the best available goods at the current market.

    Priority order:
      1. Specialty goods (category contains "Specialties") — limited supply, high value
      2. Highest buy_price first — maximises profit potential when selling elsewhere

    Buys max available quantity of each good in priority order until:
      - All buyable goods have been ordered, OR
      - Cargo is already at capacity (skips remaining goods)

    If available_qty is known from the market reader, uses that exact quantity.
    Otherwise uses "max" and lets the game enforce the cargo limit.

    Args:
        port:                  Current port name (for logging / KB).
        negotiation_strategy:  "no" / "once" (default) / "all"
        destination:           Destination port — used for culture-aware filtering
                               (e.g. skip pigs/alcohol for Islamic ports).  If None,
                               no cultural filtering is applied.

    Returns a BuyResult covering the whole transaction.
    """
    from vision.market_reader import read_market_page_claude

    logger.info(f"[{port}] Auto-buy: reading market…")

    # Switch to Purchase tab and read available goods. Read-with-verify: the
    # goods grid can lag the tab tap (or a transient/greeting frame gets captured),
    # so retry the read a few times instead of bailing on a single empty read.
    tap(*MARKET_COORDS["purchase"])
    goods: list = []
    for attempt in range(4):
        time.sleep(1.5)
        frame = capture_screen()
        _set_flow_scale(frame)
        goods = read_market_page_claude(frame, tab="purchase", port=port)
        if goods:
            break
        logger.info(f"  [auto_buy] no goods grid on read attempt {attempt + 1}/4 — retrying")
    buyable = [g for g in goods if not g.sold_out and g.buy_price]

    # Culture-aware filtering: skip goods that cannot be sold at the destination
    if destination and buyable:
        try:
            from memory.knowledge.culture_rules import is_good_sellable_at
            before = len(buyable)
            buyable = [
                g for g in buyable
                if is_good_sellable_at(g.name, destination, category=g.category)
            ]
            skipped = before - len(buyable)
            if skipped:
                logger.info(
                    f"[{port}] Culture filter ({destination}): "
                    f"skipped {skipped} restricted good(s)"
                )
        except Exception as e:
            logger.debug(f"Culture filter skipped: {e}")

    if not buyable:
        logger.info(f"[{port}] No goods available to buy")
        return BuyResult()

    # Read current cargo capacity
    used, total = _read_cargo_capacity(frame)
    remaining = (total - used) if total > 0 else 99_999
    logger.info(
        f"[{port}] Cargo: {used}/{total if total else '?'} — {remaining} units free"
    )

    # Sort: specialties first, then by price descending
    def _priority(g):
        is_specialty = "specialties" in (g.category or "").lower()
        return (0 if is_specialty else 1, -(g.buy_price or 0))

    buyable.sort(key=_priority)

    # Log the buy plan
    for g in buyable:
        tag = "[S]" if "specialties" in (g.category or "").lower() else "   "
        qty_info = f"  avail={g.available_qty}" if g.available_qty else ""
        logger.info(f"  {tag} {g.name}  {g.buy_price:,}  {g.category}{qty_info}")

    # Build orders — skip if cargo already full
    orders = []
    for g in buyable:
        if remaining <= 0:
            logger.info(f"  Cargo full — skipping {g.name} and remaining goods")
            break
        # Always use "max" quantity — the Max button in the Trade Goods Info
        # dialog sets the quantity to all available stock without touching the
        # numeric keypad, which is unreliable for position detection.
        orders.append(BuyOrder(name=g.name, quantity="max"))
        # Deduct known stock from remaining capacity estimate
        if g.available_qty:
            remaining -= g.available_qty

    if not orders:
        logger.info(f"[{port}] Nothing to order after capacity check")
        return BuyResult()

    logger.info(f"[{port}] Placing {len(orders)} order(s): {[o.name for o in orders]}")
    return buy_goods(orders, port=port, negotiation_strategy=negotiation_strategy)


def _save_buy_summary(port: str, result: BuyResult) -> None:
    """Append a buy transaction summary to the market KB."""
    import json
    from datetime import datetime, timezone
    from memory.market_kb import load_market, _market_path, _MARKETS_DIR

    _MARKETS_DIR.mkdir(parents=True, exist_ok=True)
    record = load_market(port)
    record.setdefault("transactions", []).append({
        "type": "buy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "goods": result.goods,
        "total_amount": result.total_amount,
        "purchase_cost": result.purchase_cost,
        "tax": result.tax,
        "discount": result.discount,
        "negotiation_rounds": result.negotiation_rounds,
    })
    _market_path(port).write_text(
        json.dumps(record, indent=2, ensure_ascii=False)
    )
    logger.debug(f"Buy transaction saved to market KB: {port}")


# ── Market layout discovery (used by test scripts) ────────────────────────────

def _discover_market_layout(frame: Image.Image) -> dict:
    """
    Discover Put-In-Bulk checkbox and action button positions from a market screenshot.
    Returns a dict with 'put_in_bulk', 'action_button', and 'notes' keys.
    """
    tokens = _ocr_frame(frame)
    result: dict = {"notes": ""}

    # Find Put-In-Bulk checkbox (bottom-left area, y > 800)
    pib: dict = {}
    for text, conf, cx, cy in tokens:
        if "bulk" in text.lower() and cy > 700:
            pib = {"tap_x": cx, "tap_y": cy, "checked": None, "label": text}
            break
    result["put_in_bulk"] = pib

    # Find active action button (Sell / Buy / Purchase) — typically bottom-right
    action_btn: dict = {}
    for text, conf, cx, cy in tokens:
        if text.lower() in ("sell", "buy", "purchase") and cx > 800:
            action_btn = {
                "tap_x": cx, "tap_y": cy,
                "label": text,
                "enabled": True,
            }
            break
    result["action_button"] = action_btn

    if not pib:
        result["notes"] += "Put-In-Bulk not found. "
    if not action_btn:
        result["notes"] += "Action button not found."

    return result
