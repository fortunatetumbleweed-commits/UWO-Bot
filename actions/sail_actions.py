# actions/sail_actions.py
# Sail from current port to a destination port.
#
# Actual flow (from harbour_depart flow.json):
#   1. Exit current building → port overworld
#   2. Port map → navigate to Harbour
#   3. Harbour: tap "Depart Now"
#   4. Wait through loading screens (city + sailing, both automatic)
#   5. Sea view: dismiss system notice if present (once-per-day popup)
#   6. Sea view: tap mini-map → world map opens
#   7. World map: swipe/OCR to find destination → tap port icon
#   8. City info panel: tap "Go to City"
#   9. Auto-sail: keep screen alive (anti-idle tap every 25s);
#      dismiss any system notices (new-day popups)
#  10. Arrival loading screen → wait
#  11. Arrival overlay → tap outside to enter overworld
#
# Flow coords are in thumbnail space (1000px wide) — scale by FLOW_SCALE=2.4.
#
# Standalone test:
#   python test_sail.py <destination>
#   python test_sail.py <destination> --from-overworld

from __future__ import annotations

import random
import time
from typing import Optional, Tuple

from loguru import logger
from PIL import Image

from actions.adb_actions import tap, swipe, press_back
from capture.adb_capture import capture_screen
from utils.fuzzy import fuzzy_contains, token_sim

# ── Flow coordinate scaling ────────────────────────────────────────────────────
# analyse_flow.py sends 1000px-wide thumbnails; actual screen is 2400×1080.
FLOW_SCALE: float = 2.4

def _fs(tx: float, ty: float) -> Tuple[int, int]:
    """Scale thumbnail coords to full-screen coords."""
    return (int(tx * FLOW_SCALE), int(ty * FLOW_SCALE))

# Key coords from harbour_depart/flow.json (thumbnail space → actual)
_DEPART_NOW      = _fs(896, 374)   # "Depart Now" button in Harbour right panel
_SYSTEM_NOTICE_X = _fs(746,  98)   # X close button on system notice popup
# Mini-map on sea view: use the same coordinate as the port overworld mini-map
# (2240, 250) since they occupy the same physical position in the right panel.
# The flow.json coord (893,100)→(2143,240) lands on the top-right icon bar instead.
_SEA_MINIMAP     = (2240, 250)     # mini-map tap on sea view → opens world map
_GO_TO_CITY      = _fs(527, 421)   # "Go to City" button on world map city panel
_ARRIVAL_DISMISS = _fs(200, 300)   # tap outside arrival overlay to enter overworld


# ── Shared perception helpers ──────────────────────────────────────────────────

# Per-frame OCR cache.  EasyOCR/PaddleOCR readtext on a 2400×1080 frame is
# ~1-2s; perceive() invokes _ocr_frame 5-6 times per tick (interruptors,
# nav classification, qwen prep, flow detection) on the same frame.  Caching
# the raw readtext output by id(frame) collapses those into one inference
# while preserving per-caller min_conf filtering — different thresholds
# share the cache because filtering happens at query time.
#
# Cap matches parse_fast_cached's 4-frame window: PIL frame ids can be
# reused after GC, but cross-tick reuse is bounded by clearing wholesale
# when the cap is hit.
_OCR_CACHE: dict[int, list] = {}
_OCR_CACHE_MAX = 4


def _ocr_frame(frame: Image.Image, min_conf: float = 0.30):
    """OCR full frame and return tokens above *min_conf*.

    Raw readtext output is cached by id(frame); repeated calls on the
    same frame (across perceive helpers) share one OCR inference.

    The cache stores (frame, raw) so the entry PINS the frame alive: `id()` only
    identifies a live object, and a freed PIL image's address is immediately reused by the
    next one (measured: 200 images, 3 distinct ids). Caching the raw tokens against a bare
    id let one screen's OCR be served for another — the world-map frame that came back
    reading "Inn / recruit mates" from the port overworld (live 2026-08-21).
    """
    import numpy as np
    fid = id(frame)
    entry = _OCR_CACHE.get(fid)
    raw = None
    if entry is not None:
        cached_frame, cached_raw = entry
        if cached_frame is frame:
            raw = cached_raw
        else:
            _OCR_CACHE.pop(fid, None)        # stale id — the old frame is gone
    if raw is None:
        if len(_OCR_CACHE) >= _OCR_CACHE_MAX:
            _OCR_CACHE.clear()
        from vision.ocr import _get_reader
        raw = _get_reader().readtext(np.array(frame), detail=1)
        _OCR_CACHE[fid] = (frame, raw)       # the reference pins the id

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


def clear_ocr_frame_cache() -> None:
    """Clear the per-frame OCR cache.  Used by tests and after long pauses
    where stale frame ids might be reused."""
    _OCR_CACHE.clear()


def _screen_contains(frame: Image.Image, *keywords: str) -> bool:
    tokens = _ocr_frame(frame)
    full = " ".join(t.lower() for t, _, _, _ in tokens)
    return any(fuzzy_contains(full, kw) for kw in keywords)


def _wait_for_screen(
    *keywords: str,
    timeout: float = 30.0,
    interval: float = 2.0,
) -> Tuple[Optional[Image.Image], bool]:
    """Wait until ALL keywords appear (AND). Returns (frame, ok)."""
    deadline = time.time() + timeout
    frame = None
    while time.time() < deadline:
        frame = capture_screen()
        tokens = _ocr_frame(frame)
        full = " ".join(t.lower() for t, _, _, _ in tokens)
        if all(fuzzy_contains(full, kw) for kw in keywords):
            return frame, True
        time.sleep(interval)
    return frame, False


def _find_yellow_button(
    frame: Image.Image,
    x_min: Optional[int] = None,
    x_max: Optional[int] = None,
    y_min: Optional[int] = None,
    y_max: Optional[int] = None,
) -> Optional[Tuple[int, int]]:
    """
    Find the primary composite action button by its yellow background (HSV detection).

    Yellow buttons (Purchase, Supply Departure, Auto Supply, Recruit, etc.) all share
    the same visual style: a solid yellow/amber fill with a ducat amount and text.
    This is more reliable than text matching because the label varies by context.

    Returns (cx, cy) of the largest yellow button region found, or None.
    Region constraints are in full-frame pixel coordinates.
    """
    import numpy as np
    try:
        import cv2
    except ImportError:
        return None

    arr = np.array(frame.convert("RGB"))
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)

    # Yellow/amber HSV range (OpenCV: H is 0-179, S and V are 0-255)
    # Hue 15-35 covers yellow-amber; adjust slightly if false-negatives occur.
    lower = np.array([15, 100, 150])
    upper = np.array([35, 255, 255])
    mask  = cv2.inRange(hsv, lower, upper)

    # Apply region constraints to the mask
    h, w = mask.shape
    if x_min is not None: mask[:, :max(0, x_min)] = 0
    if x_max is not None: mask[:, min(w, x_max):]  = 0
    if y_min is not None: mask[:max(0, y_min), :]  = 0
    if y_max is not None: mask[min(h, y_max):, :]  = 0

    # Find contours of yellow regions
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    # Take the largest contour that is big enough to be a button
    # (at least 4000 px² on a 2400×1080 frame — roughly 80×50 minimum)
    MIN_AREA = 4000
    best = max(contours, key=cv2.contourArea)
    if cv2.contourArea(best) < MIN_AREA:
        return None

    x, y, bw, bh = cv2.boundingRect(best)
    cx, cy = x + bw // 2, y + bh // 2
    logger.debug(f"  yellow_button @ ({cx},{cy})  area={cv2.contourArea(best):.0f}")
    return (cx, cy)


_WORD_RE_CACHE: dict[str, "re.Pattern"] = {}


def _label_matches(target: str, candidate: str) -> bool:
    """True if every whole-word in *target* appears as a whole word in *candidate*.

    Whole-word matching prevents short canonical labels (ok, x, no) from
    accidentally matching unrelated words (Stock, Exit, Maximum) — the
    May-2 class of dialog-button confusion.  Multi-word targets still
    work: "quantity display" requires both "quantity" AND "display" as
    whole words inside the candidate label.
    """
    import re
    cand_lower = candidate.lower()
    for word in target.lower().split():
        pat = _WORD_RE_CACHE.get(word)
        if pat is None:
            pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])")
            _WORD_RE_CACHE[word] = pat
        if not pat.search(cand_lower):
            return False
    return True


def _find_button(
    frame: Image.Image,
    *labels: str,
    x_min: Optional[int] = None,
    x_max: Optional[int] = None,
    y_min: Optional[int] = None,
    y_max: Optional[int] = None,
    allow_title: bool = False,
) -> Optional[Tuple[int, int]]:
    """Find a button by label: OmniParser first, EasyOCR fallback.

    Matching uses whole-word equality on each whitespace-split component
    of *label* against the candidate's label text.  This prevents short
    canonical labels (ok, x, no) from accidentally matching substrings
    inside unrelated words (Stock, Exit, Maximum).

    The chromed TITLE is skipped unless `allow_title=True`. On every chromed screen the
    title bar is a BACK control (the Android paradigm), and the title word is usually the
    same word a caller is searching for — the Sell page is titled "Sell", the Purchase page
    "Purchase". A forward-intent search therefore collides with it exactly when the bot has
    ALREADY reached the screen it wanted, and the tap navigates back out. Live 2026-08-22:
    `_find_button(frame, "sell")` matched the title at (107,53) on a Sell page already
    showing Ebony 700 / Coral 797, left the market, and the mission planned zero rounds.
    """
    from vision.omniparser import get_omniparser, parse_fast_cached

    def _is_title(cx: int, cy: int) -> bool:
        """The `< Title` back control, in the TOP-LEFT corner. Kept as a fraction of the
        frame so it survives rotation and the camera-cutout offset; scoped to the corner so
        a dialog's own close-X (top-RIGHT of the dialog) is still findable."""
        return (not allow_title
                and cx < 0.15 * frame.width and cy < 0.10 * frame.height)

    parser = get_omniparser()
    if parser.yolo_available():
        for el in parse_fast_cached(frame):
            for label in labels:
                if not _label_matches(label, el.label):
                    continue
                cx, cy = el.cx, el.cy
                if _is_title(cx, cy): continue
                if x_min is not None and cx < x_min: continue
                if x_max is not None and cx > x_max: continue
                if y_min is not None and cy < y_min: continue
                if y_max is not None and cy > y_max: continue
                logger.debug(f"  OmniParser: {label!r} @ ({cx},{cy})")
                return (cx, cy)

    for text, _, cx, cy in _ocr_frame(frame):
        for label in labels:
            if not _label_matches(label, text):
                continue
            if _is_title(cx, cy): continue
            if x_min is not None and cx < x_min: continue
            if x_max is not None and cx > x_max: continue
            if y_min is not None and cy < y_min: continue
            if y_max is not None and cy > y_max: continue
            logger.debug(f"  EasyOCR: {label!r} @ ({cx},{cy})")
            return (cx, cy)
    return None


def _is_loading_screen(frame: Image.Image) -> bool:
    """
    True when a loading screen is active (city or sailing).

    Keyword rationale:
      "entering"  — "Entering London" transition screen
      "preparing" — "Preparing for Voyage" transition screen (also covers "Preparing...")
      "loading"   — explicit "Loading..." text on transition screens

    "voyage" was removed: it triggered false positives when NPC speech bubbles
    on the port overworld say "Bon voyage!" or "Safe voyage!".
    "preparing" already catches all sailing-departure loading screens.
    """
    from brain.kb import control
    return _screen_contains(frame, *control().loading_keywords())


def _is_idle_cinematic(frame: Image.Image) -> bool:
    """
    True when the idle/cinematic sailing view is active (top bar + HUD gone).
    The constant background text '4.04... atlantic ocean' appears on ALL screens
    so it cannot be used as an idle signal. Instead, idle = no sailing HUD tokens.
    Only valid in sea context (called from _wait_for_arrival / _open_world_map).
    Loading screens are excluded separately.
    """
    if _is_loading_screen(frame):
        return False
    tokens = _ocr_frame(frame, min_conf=0.3)
    full = " ".join(t.lower() for t, _, _, _ in tokens)
    sea_hud_present = any(fuzzy_contains(full, kw) for kw in _SEA_HUD_TOKENS)
    return not sea_hud_present



# ── Location awareness ───────────────────────────────────────────────────────

# Sea HUD tokens loaded from KB at first use.
# Do NOT hardcode here — edit memory/knowledge/control/ui_signals.json instead.
def _get_sea_hud_tokens() -> tuple:
    from brain.kb import control
    return control().sea_hud_tokens()


# Keep module-level alias for code that still references _SEA_HUD_TOKENS directly.
# Will be removed once all callers use _get_sea_hud_tokens().
class _SeaHudTokensProxy:
    """Lazy proxy so existing `for kw in _SEA_HUD_TOKENS` code reads from KB."""
    def __iter__(self):
        return iter(_get_sea_hud_tokens())
    def __contains__(self, item):
        return item in _get_sea_hud_tokens()

_SEA_HUD_TOKENS = _SeaHudTokensProxy()

# Crop regions for sea HUD elements (2400×1080 landscape)
# Supply + day-at-sea counter: top-left, below ship icon row
_HUD_SUPPLY_CROP = (0,   140, 600,  320)
# Destination + ETA: bottom-centre strip
_HUD_ETA_CROP    = (700, 920, 1700, 1080)


def read_sea_hud(frame=None) -> dict:
    """
    Read key values from the sailing HUD.

    Returns a dict with:
      supply_days  — int | None   "11 Days of Sailing Left"
      day_at_sea   — int | None   "Day 1" (in-game days elapsed since departure)
      eta_days     — int | None   ETA in in-game days shown at bottom-centre
      destination  — str | None   destination port name shown above the ETA

    Any field is None if it cannot be parsed from the current frame.
    """
    import re
    from capture.adb_capture import capture_screen

    if frame is None:
        frame = capture_screen()

    result: dict = {
        "supply_days": None,
        "day_at_sea":  None,
        "eta_days":    None,
        "destination": None,
    }

    # ── Supply + day-at-sea (top-left) ───────────────────────────────────────
    supply_crop = frame.crop(_HUD_SUPPLY_CROP)
    supply_tokens = _ocr_frame(supply_crop, min_conf=0.25)
    supply_text = " ".join(t.lower() for t, _, _, _ in supply_tokens)

    m = re.search(r'(\d+)\s*days?\s*of\s*sailing', supply_text)
    if m:
        result["supply_days"] = int(m.group(1))

    m = re.search(r'day\s*(\d+)', supply_text)
    if m:
        result["day_at_sea"] = int(m.group(1))

    # ── Destination + ETA (bottom-centre) ────────────────────────────────────
    eta_crop = frame.crop(_HUD_ETA_CROP)
    eta_tokens = _ocr_frame(eta_crop, min_conf=0.25)

    # Sort tokens top→bottom so destination appears before ETA line
    eta_tokens_sorted = sorted(eta_tokens, key=lambda t: t[3])  # sort by cy

    dest_candidate: Optional[str] = None
    for tok, conf, cx, cy in eta_tokens_sorted:
        t = tok.strip()
        tl = t.lower()

        # ETA line: "ETA ≈ 1 d", "ETA ~ 2 d", "eta 1d", etc.
        m = re.search(r'eta.*?(\d+)\s*d', tl)
        if m:
            result["eta_days"] = int(m.group(1))
            # Port name was the last non-ETA token above this one
            if dest_candidate:
                result["destination"] = dest_candidate
            continue

        # Skip noise tokens (numbers, icons, single chars)
        if len(t) >= 3 and not re.match(r'^[\d\s\W]+$', t):
            dest_candidate = t

    # Fallback: on a saved-ROUTE auto-sail the HUD reads "Sailing Route N" +
    # "ETA" + "12d" as SEPARATE tokens, so the per-token regex above misses it.
    # Re-run over the joined text.  Also surface the route name as destination.
    eta_text = " ".join(t.lower() for t, _, _, _ in eta_tokens_sorted)
    if result["eta_days"] is None and ("eta" in eta_text or "sailing route" in eta_text):
        # Route HUD splits "ETA" and "12d" into separate tokens whose sort order
        # varies, so match the standalone "<n>d" token in the ETA context.
        m = re.search(r'(\d+)\s*d\b', eta_text)
        if m:
            result["eta_days"] = int(m.group(1))
    if result["destination"] is None:
        m = re.search(r'(sailing route\s*\d+)', eta_text)
        if m:
            result["destination"] = m.group(1)

    logger.debug(
        f"Sea HUD — supply={result['supply_days']}d  "
        f"day={result['day_at_sea']}  "
        f"eta={result['eta_days']}d  "
        f"dest={result['destination']!r}"
    )
    return result


def _dismiss_android_dialog_if_present(frame) -> object:
    """
    Detect the Android-style connection-unstable dialog and dismiss it.

    The dialog shows title "Notice" and body text containing "connection" and
    "unstable".  It has a single OK button and cannot be dismissed by tapping
    outside.  After tapping OK the game reloads, so we poll until loading
    finishes and return the fresh frame.

    Returns the (possibly refreshed) frame for the caller to continue with.
    """
    import numpy as np

    h, w = frame.height, frame.width

    # Fast pixel-brightness guard: the connection dialog is a bright white modal
    # centred on an otherwise dark game background.  Sample the centre strip; if
    # average brightness < 200 (out of 255), no white dialog is present — skip OCR
    # entirely.  This check is ~0.001s vs ~2s for the OCR call.
    centre_strip = np.array(frame.crop((w // 3, h // 3, 2 * w // 3, 2 * h // 3)))
    if centre_strip.mean() < 200:
        return frame  # no bright modal — fast exit

    # Bright region detected — pay the OCR cost to confirm it is the dialog.
    dialog_crop = frame.crop((w // 4, h // 4, 3 * w // 4, 3 * h // 4))
    tokens = _ocr_frame(dialog_crop, min_conf=0.3)
    text = " ".join(t.lower() for t, _, _, _ in tokens)

    if not ("notice" in text and "connection" in text and "unstable" in text):
        return frame  # not the dialog — return frame unchanged

    logger.warning("Android connection dialog detected — tapping OK and waiting for reload")

    # Find "OK" in the full frame (it sits in the bottom-right of the dialog)
    full_tokens = _ocr_frame(frame, min_conf=0.3)
    ok_x, ok_y = w // 2, h // 2   # fallback centre
    for tok, conf, cx, cy in full_tokens:
        if tok.strip().lower() in ("ok", "okay") and cx > w // 2 and cy > h // 3:
            ok_x, ok_y = cx, cy
            break

    from actions.adb_actions import tap
    tap(ok_x, ok_y)

    # Wait for the game to reload — poll until we leave the loading screen
    import time
    logger.info("  Waiting for game to reload after connection dialog…")
    for _ in range(60):          # up to ~60 s
        time.sleep(1.0)
        frame = capture_screen()
        check = _ocr_frame(frame.crop((w // 4, h // 4, 3 * w // 4, 3 * h // 4)),
                           min_conf=0.3)
        check_text = " ".join(t.lower() for t, _, _, _ in check)
        # Stop waiting once the dialog is gone
        if "notice" not in check_text or "connection" not in check_text:
            logger.info("  Game reloaded — resuming.")
            break

    return capture_screen()


def _wait_for_state_change(
    expected: tuple,
    max_wait: float = 8.0,
    poll_interval: float = 1.0,
    initial_delay: float = 0.5,
) -> dict:
    """Poll `where_am_i()` until the location is in *expected*, or until
    *max_wait* seconds elapse.

    Returns the final where_am_i() dict.  Used after taps that the game
    responds to in 1-3 s on the happy path — replaces blanket
    `time.sleep(N)` calls that paid the worst-case animation time on
    every voyage start.

    initial_delay gives the game a moment to begin the transition
    before the first perceive (perceive itself is ~2-3 s of cost; no
    point doing it on a frame that hasn't changed yet).
    """
    import time as _time
    _time.sleep(initial_delay)
    deadline = _time.time() + max_wait
    loc = None
    while _time.time() < deadline:
        loc = where_am_i()
        if loc["location"] in expected:
            logger.debug(
                f"  [wait_for_state] reached {loc['location']!r} "
                f"after {_time.time() - (deadline - max_wait):.1f}s"
            )
            return loc
        remaining = deadline - _time.time()
        if remaining <= 0:
            break
        _time.sleep(min(poll_interval, remaining))
    return loc or where_am_i()


def where_am_i(frame: Optional[Image.Image] = None) -> dict:
    """
    Backward-compatible wrapper around brain.perceive.perceive().

    Returns the same dict shape callers used to consume:
      { "location": str, "port": str|None, "detail": str }

    Callers that want the richer PerceiveResult (flow, flow_step, confidence,
    interruptors) should import perceive directly.  This shim exists only to
    keep the in-helper polling loops in this module running while Phase 4
    collapses them into the goal layer.

    Note: perceive() runs the full three-pass pipeline (interruptor dismissal
    → active-flow detection → navigation state).  This means every call site
    now also dismisses known interruptors as a side effect — that is a desired
    upgrade over the single-pass classifier this used to be.
    """
    from brain.perceive import perceive
    return perceive(frame).to_location_dict()


# ── Step 1: exit building to port overworld ───────────────────────────────────

def _is_on_overworld(frame: Image.Image) -> bool:
    """
    Overworld detection (port context only — not valid on sea or world map).

    Slice 6 (2026-05-17): primary check now goes through the SceneModel.
    A SceneModel with high confidence is authoritative — `scene_kind ==
    "port_overworld"` confirms overworld; any other high-confidence
    scene_kind (chromed, world_map, sea) confirms NOT overworld.  This
    is more reliable than chrome-flag heuristics because the SceneModel
    verifies the structural picture (lighthouse big icon + right panel +
    no back arrow) as a conjunction, not a single ambiguous flag.

    When the SceneModel returns medium/low confidence (e.g. transitional
    frames where the top_left detector hasn't stabilised), we fall back
    to the legacy chrome-flag heuristic.

    Chrome signatures (legacy fallback path):
      - Port overworld          : home=False, back=False  (hamburger unreliable)
      - Building sub-menu       : home=True,  back=True
      - Building main screen    : home=False, back=True   ← NOT overworld
      - Port map                : home=True,  back=True

    Legacy rule: overworld iff home=False AND back=False, plus a positive
    signal (port name OR right panel).
    """
    # ── Primary: SceneModel structural check ──────────────────────────
    from vision.scene_model import get_scene_model
    sm = get_scene_model(frame=frame)

    if sm.confidence == "high":
        if sm.is_at_port_overworld():
            title_text = (
                sm.top_left.title.text if sm.top_left and sm.top_left.title
                else "<none>"
            )
            # The MAIN MENU sits ON TOP of the overworld, so the scene model is right to
            # report kind=port_overworld with overlay=main_menu — but the overworld's
            # controls are covered and cannot be tapped. Callers ask this to decide whether
            # they can act, so an overlay means "no".
            #
            # Live 2026-08-22: `exit_to_overworld` reported "Port overworld confirmed" on
            # the main menu while `open_world_map` — which classifies properly — kept
            # reporting main_menu. The two deadlocked for all 10 attempts and the run died
            # with one action recorded.
            _ov = getattr(sm, "overlay", None)
            if _ov is not None and getattr(_ov, "is_modal", False):
                logger.info(
                    f"  Not actionable overworld — a MODAL overlay "
                    f"({getattr(_ov, 'kind', '?')!r}) is over it ({sm.summary()})"
                )
                return False
            logger.info(
                f"  Overworld confirmed via SceneModel "
                f"(title={title_text!r}, {sm.summary()})"
            )
            return True
        # High-confidence non-overworld scene — authoritative reject.
        logger.info(
            f"  Not overworld via SceneModel ({sm.summary()})"
        )
        return False

    # SceneModel was uncertain (medium/low confidence) — fall through to
    # legacy chrome-flag heuristic below.
    logger.info(
        f"  SceneModel uncertain ({sm.summary()}) — falling back to chrome heuristic"
    )

    # ── Fallback: legacy chrome-flag heuristic ────────────────────────
    from vision.chrome_detector import get_chrome_detector
    chrome = get_chrome_detector().detect(frame)

    logger.info(f"  Chrome: hamburger={chrome.has_hamburger} back={chrome.has_back_arrow} "
                f"home={chrome.has_home}")

    if chrome.has_home:
        logger.info("  Not overworld: home button visible (inside building or port map)")
        return False

    if chrome.has_back_arrow:
        from vision.ocr import read_screen_title
        title = read_screen_title(frame)
        logger.info(f"  Not overworld: back arrow visible (title={title!r})")
        return False

    # home=False, back=False — could be overworld OR a full-screen overlay
    # (e.g. mate level-up notice, discovery splash) that has no standard chrome.
    # Require at least one positive overworld signal before declaring success.
    from vision.ocr import read_port_name
    port = read_port_name(frame)
    if port:
        logger.info(f"  Overworld confirmed: port name {port!r} visible")
        return True
    if chrome.has_right_panel:
        logger.info("  Overworld confirmed: right panel visible")
        return True

    # No port name and no right panel — likely a full-screen overlay.
    # Tap to dismiss and let the caller retry.
    logger.info("  No home, no back, no port name, no right panel — likely overlay; tapping to dismiss")
    from actions import ui as _ui
    _ui.tap_centre(frame, why="dismiss a suspected full-screen overlay")
    return False


def _advance_mandatory_flow(
    frame: Image.Image,
    prefer_complete: bool = False,
) -> Optional[Tuple[int, int]]:
    """Thin shim — delegates to brain.recovery to keep logic centralised."""
    from brain.recovery import advance_mandatory_flow
    return advance_mandatory_flow(frame, prefer_complete=prefer_complete)


def exit_to_overworld(timeout: float = 90.0) -> bool:
    """Press back until port overworld is confirmed.

    Uses chrome detection (fast, ~0.1s) as the primary loop signal.
    OCR is only invoked once per iteration when has_home=False, to rule out
    the "exit game?" dialog that appears if back is pressed on the overworld.

    When back presses stop making progress (building state persists after 2
    consecutive backs), tries to dismiss any blocking dialog (e.g. the
    negotiation dialog left open after a buy_all crash) before retrying.
    """
    logger.info("Exiting to port overworld…")
    deadline = time.time() + timeout
    stuck_count = 0   # consecutive back presses that left us in building state

    while time.time() < deadline:
        frame = capture_screen()

        if _is_on_overworld(frame):
            if _screen_contains(frame, "exit game", "leave game", "quit game",
                                 "exit the game", "do you want to exit"):
                logger.info("Exit dialog visible — on overworld, dismissing")
                cancel = _find_button(frame, "cancel", "no", "stay")
                if cancel:
                    tap(*cancel)
                else:
                    press_back()
                time.sleep(1.0)
            else:
                logger.info("Port overworld confirmed")
            return True

        # Still inside a building.  After 2 stuck backs, delegate to the
        # general recovery module which identifies the exact state and
        # completes any mandatory dialog flow before retrying.
        if stuck_count >= 2:
            logger.info(
                f"  Still in building after {stuck_count} backs — "
                "delegating to recover_to_port_overworld()"
            )
            from brain.recovery import recover_to_port_overworld
            r = recover_to_port_overworld(timeout=30.0)
            if r.state == "port_overworld":
                logger.info("Port overworld confirmed (via recovery)")
                return True
            stuck_count = 0
            continue

        logger.info("Not on overworld — pressing back")
        press_back()
        time.sleep(2.0)
        stuck_count += 1

    logger.warning("Could not reach port overworld")
    return False


# ── Building navigation — shared state machine ────────────────────────────────

# Known building names, to tell "the right panel is on the BUILDINGS tab" (list rendered) from
# "it's on the Tasks / Players tab" (list absent) — the latter is why navigate_to_building can read
# an empty building list on a perfectly-good port overworld (live 2026-08-19: Tasks tab auto-opened
# on arrival at Jakarta → 60s timeout).
# Common buildings ALWAYS near the top of the Buildings-tab list, chosen to be single words that do
# NOT appear in Tasks/quest text — 'union'/'palace' were dropped because quests like "from Istanbul
# Union" / "Palace:" false-matched a SUBSTRING check and made the bot think it was on the Buildings
# tab when it was actually on Tasks (live 2026-08-19). Matched EXACTLY, not as substrings.
_BUILDING_NAMES = frozenset({"harbor", "market", "shipyard", "bank", "inn", "sanctuary",
                             "item shop", "bureau"})


# A real building list names SEVERAL buildings; one stray match is not evidence.
_MIN_BUILDING_MATCHES = 2


def _on_buildings_tab(buildings) -> bool:
    """True if the read list is the BUILDINGS tab.

    Matched EXACTLY, not as substrings: Tasks entries 'from istanbul union' / 'palace:' must
    not count (live 2026-08-19).

    And matched at least twice. A single hit is not evidence, because the port overworld has
    a standalone "⚓ Harbor" shortcut button that the list read picks up: at Jakarta on
    2026-08-21 the PLAYER tab read as ['tropical', 'kingdavid', '0.#', 'lv 70', 'harbor'] —
    one building word, from a button that is not in the list at all. That single match made
    this return True, so the "wrong tab?" branch never ran, and `navigate_to_building`
    scrolled a list of player names for 60s before giving up. A genuine building list carries
    seven or more matches (harbor, market, shipyard, bank, inn, sanctuary, bureau…), so the
    two cases are not close together.
    """
    hits = sum(1 for lbl, *_ in buildings
               if lbl.strip().strip(":.").lower() in _BUILDING_NAMES)
    return hits >= _MIN_BUILDING_MATCHES


def _tab_strip_band() -> Tuple[int, int, int, int]:
    """SEARCH region for the port-overworld tab strip, derived from the calibrated minimap.

    A region is a safe use of a calibrated constant; a tap target is not. The strip sits just
    above the minimap, so this brackets it generously and lets detection pick the icons out.
    """
    try:
        from brain.ai_nav.vision_input import MINIMAP_CROP as _MC
    except Exception:
        _MC = (1984, 205, 2379, 395)
    x0, y0, x1, y1 = _MC
    return (x0 - 80, y0 - 100, x1 + 80, y0 - 5)


def _tab_strip_candidates(frame) -> list:
    """Detected tab icons above the minimap, ordered left→right.

    The strip is Tasks / Buildings / Players / Location. Their POSITIONS are not stable — the
    game re-bakes its camera-cutout offset per screen — so they are detected, not computed.
    That distinction is not academic: `_buildings_tab_pos()` used to return a calibrated
    ≈(2146,156), and on Jakarta 2026-08-21 the real tabs sat at ≈2016 (Buildings) and ≈2114
    (Players). The constant landed on PLAYERS, so the bot selected the player tab itself,
    the panel listed 'kingdavid LV 70' instead of buildings, and `gather:Jakarta` failed
    twice with "Could not enter 'Market' after 60s" and aborted the mission.
    """
    try:
        from vision.omniparser import parse_fast_cached
        els = parse_fast_cached(frame) or []
    except Exception as exc:
        logger.debug(f"  tab-strip detection unavailable ({type(exc).__name__}: {exc})")
        return []
    bx0, by0, bx1, by1 = _tab_strip_band()
    hits = [e for e in els if bx0 <= e.cx <= bx1 and by0 <= e.cy <= by1]
    hits.sort(key=lambda e: e.cx)
    if hits:
        logger.info("  Tab-strip candidates: "
                    + ", ".join(f"{e.label!r}@({e.cx},{e.cy})" for e in hits))
    return [(e.cx, e.cy) for e in hits]


# Screens whose title says the fleet is ALREADY UNDER WAY. Reaching a building is
# meaningless once the ship has left, and forcing the screen back to match a stale plan
# destroys the voyage that was just started.
_UNDER_WAY_TITLE_MARKERS = ("sailing to", "en route to", "moving to")


def _screen_says_under_way(location: str, title: str) -> bool:
    """True when the SCREEN says the fleet has already departed.

    The screen is ground truth (user 2026-08-22). A title that does not match the building
    we were asked for usually means our own idea of where we are is stale — not that the
    screen must be pressed back into line.
    """
    if location in ("sea", "sea_cinematic"):
        return True
    t = (title or "").lower()
    return any(m in t for m in _UNDER_WAY_TITLE_MARKERS)


def navigate_to_building(building_name: str, timeout: float = 60.0) -> bool:
    """
    Enter a named building from anywhere in a port, driven by where_am_i().

    State machine:
      Any state → where_am_i() → act on result → loop

      "building"        → success (we're inside a building)
      "loading"         → wait (transition in progress)
      "port_overworld"  → find building in list → tap (with retry cooldown)
      "sea"/"world_map" → press_back to recover to overworld
      "sea_cinematic"   → likely popup overlay; wait and re-poll
      "unknown"         → wait and re-poll

    Navigation order when on port_overworld:
      1. Building list (right panel) — direct entry, fast
      2. One scroll if building not immediately visible
      3. Port map — fallback; character walks, where_am_i() handles entry

    Args:
      building_name: name to search for (fuzzy-matched against list labels)
      timeout: total seconds before giving up
    """
    from vision.ocr import read_building_menu
    from actions.adb_actions import swipe_fast
    from config.settings import BUILDING_MENU_REGION

    target = building_name.lower()

    def _find_in_list(frame=None) -> Optional[Tuple[int, int]]:
        """Scan building list; return tap coord or None."""
        buildings = read_building_menu(frame or capture_screen())
        logger.info(f"  Building list: {[lbl for lbl, *_ in buildings]}")
        return next(
            ((x, y) for lbl, x, y in buildings
             if target in lbl.lower() or token_sim(lbl, target) >= 0.75),
            None
        )

    def _list_signature(buildings):
        """Build a stable scroll-position signature.

        Uses the FIRST visible row's cy (rounded to a 30 px bucket) AS
        WELL AS the building-name tokens.  Catches "list shifted by
        20 px" as a real change while ignoring the second-by-second
        clock tick in the header row (which `read_building_menu`
        already filters via `_is_building_list_noise`, but we belt-
        and-brace the signature in case OCR variance sneaks one in).

        Buildings list is `[(label, cx, cy), ...]` sorted by cy.
        """
        if not buildings:
            return ()
        first_cy_bucket = buildings[0][2] // 30
        labels = tuple(lbl for lbl, *_ in buildings)
        return (first_cy_bucket, labels)

    def _tap_from_list(frame=None, max_scrolls: int = 4,
                        reset_swipes: int = 4) -> bool:
        """Try building list, scrolling up to *max_scrolls* times until the
        target is visible.  Stops scrolling early when the list signature
        stops changing (we're at the bottom).

        Before searching, scrolls UP up to *reset_swipes* times so the
        panel starts from the top of the list — earlier navigate_to_building
        calls may have left the panel scrolled past the target.  London's
        Harbor sits at the top of the list, and if we exited from a
        building further down (Union, Bank, ...), the panel is pre-scrolled
        and Harbor is no longer visible.  Scrolling down further would
        never find it.

        London and other large ports have ~12 buildings that overflow the
        visible panel; one scroll isn't enough to reach the bottom items
        (palace, bureau, fortune teller).  Scrolling repeatedly until
        found avoids unnecessary port-map fallback, which has its own
        in-transit retap risks.
        """
        # ── Step 0: make sure the right panel is on the BUILDINGS tab ────
        # The port-overworld tab bar (above the minimap) toggles Tasks / Buildings / Players; the
        # building list only renders on the Buildings tab.  On arrival the Tasks tab can be auto-
        # selected (live 2026-08-19: Jakarta → empty list → 60s timeout).  If the read doesn't look
        # like the building list, tap the Buildings tab (house icon) and re-read.  Bounded: one tap.
        frame = frame or capture_screen()
        if not _on_buildings_tab(read_building_menu(frame)):
            # Try the detected tabs in turn and VERIFY by re-reading the list, rather than
            # trusting one computed position. Which index is Buildings varies with how many
            # tabs a port shows, and a wrong guess does not fail quietly — it SELECTS another
            # tab, which is how the bot put itself on the Players tab at Jakarta.
            candidates = _tab_strip_candidates(frame)
            if not candidates:
                logger.warning("  Building list not on screen and no tab icons detected — "
                               "proceeding with the current view")
            for i, (bx, by) in enumerate(candidates):
                logger.info(f"  Building list not on screen — trying tab {i + 1}/"
                            f"{len(candidates)} @ ({bx},{by})")
                tap(bx, by)
                time.sleep(1.5)
                frame = capture_screen()
                if _on_buildings_tab(read_building_menu(frame)):
                    logger.info(f"  Buildings tab selected @ ({bx},{by})")
                    break
            else:
                if candidates:
                    logger.warning(f"  None of the {len(candidates)} detected tabs showed a "
                                   "building list")

        # ── Step 1: try current view (cheap, often hits) ─────────────────
        pos = _find_in_list(frame)
        if pos is not None:
            logger.info(f"  Tapping '{building_name}' in building list @ {pos}")
            tap(*pos)
            return True

        # Scroll within the ACTUAL list column, not the region centre.  The list sits at the right
        # edge (x≈0.91·W ≈ 2184); a region-centre swipe (x≈2125) can miss it entirely, so the rewind
        # never scrolls, the signature stays unchanged → "at top" is assumed, and clipped top
        # buildings (Harbor/Market on 10-building ports) never come back (live 2026-08-19, Jakarta).
        _bl = read_building_menu(frame if frame is not None else capture_screen())
        if _bl:
            xs = sorted(x for _, x, _ in _bl)
            mx = xs[len(xs) // 2]                    # median entry x = the real list column
        else:
            mx = (BUILDING_MENU_REGION[0] + BUILDING_MENU_REGION[2]) // 2
        my = (BUILDING_MENU_REGION[1] + BUILDING_MENU_REGION[3]) // 2

        # ── Step 2: reset to top of list with up-swipes ─────────────────
        # Swipe DOWN (finger moves down) → content scrolls UP → list rewinds
        # toward the top.  Stop early when signature stops changing.
        prev_signature: Optional[tuple] = None
        for i in range(1, reset_swipes + 1):
            logger.info(
                f"  Rewinding building list to top (up-swipe {i}/{reset_swipes})"
            )
            swipe_fast(mx, my - 150, mx, my + 150,
                        duration_ms=300, settle_ms=600)
            frame_after = capture_screen()
            buildings = read_building_menu(frame_after)
            signature = _list_signature(buildings)
            logger.info(f"  Building list: {[lbl for lbl, *_ in buildings]}")

            pos = next(
                ((x, y) for lbl, x, y in buildings
                 if target in lbl.lower() or token_sim(lbl, target) >= 0.75),
                None,
            )
            if pos:
                logger.info(
                    f"  Tapping '{building_name}' in building list @ {pos}"
                )
                tap(*pos)
                return True
            if signature == prev_signature:
                logger.info("  List signature unchanged — at top of scroll")
                break
            prev_signature = signature

        # ── Step 3: scroll down looking for the target ──────────────────
        prev_signature = None
        for attempt in range(1, max_scrolls + 1):
            logger.info(
                f"  '{building_name}' not visible — scrolling building list "
                f"(attempt {attempt}/{max_scrolls})"
            )
            swipe_fast(mx, my + 150, mx, my - 150,
                        duration_ms=300, settle_ms=600)
            frame_after = capture_screen()
            buildings = read_building_menu(frame_after)
            signature = _list_signature(buildings)
            logger.info(f"  Building list: {[lbl for lbl, *_ in buildings]}")

            if signature == prev_signature:
                logger.info(
                    "  List signature unchanged — reached end of scroll"
                )
                break
            prev_signature = signature

            pos = next(
                ((x, y) for lbl, x, y in buildings
                 if target in lbl.lower() or token_sim(lbl, target) >= 0.75),
                None,
            )
            if pos:
                logger.info(
                    f"  Tapping '{building_name}' in building list @ {pos}"
                )
                tap(*pos)
                return True

        return False

    def _tap_from_port_map() -> bool:
        """Fallback: open port map, tap the building icon."""
        from brain.states.port_map import open_port_map, read_port_map_buildings, close_port_map
        logger.info(f"  '{building_name}' not in list — trying port map")
        if not open_port_map():
            logger.error("  Could not open port map")
            return False
        time.sleep(0.8)
        pm_buildings = read_port_map_buildings(capture_screen())
        result = next(
            ((name, x, y) for name, x, y in pm_buildings
             if target in name.lower() or token_sim(name, target) >= 0.75),
            None
        )
        if result is None:
            close_port_map()
            logger.error(f"  '{building_name}' not found on port map")
            return False
        name, tx, ty = result
        logger.info(f"  Tapping '{name}' on port map @ ({tx},{ty})")
        tap(tx, ty)
        return True

    def _tap_nameplate_if_visible(frame=None) -> bool:
        """When the bot's character has walked to a building, a floating
        nameplate appears above the entrance (small) or expands (large)
        when right in front.  The nameplate IS tappable — tapping it
        enters the building.  This is far more reliable than waiting
        for auto-entry after a port-map tap, which can be blocked by
        ambient popups (Happy 2026 / NPC tooltip / location info).

        Returns True if a matching nameplate was found and tapped.
        """
        from vision.screen_perception import parse_screen
        from vision.element_postprocess import ROLE_BUILDING_NAMEPLATE
        from brain.states.port_map import _canonical_name

        if frame is None:
            frame = capture_screen()
        inv = parse_screen(frame, nav_state="port_overworld")
        for t in inv.tagged:
            if t.role != ROLE_BUILDING_NAMEPLATE:
                continue
            lbl = (t.label or "").lower().strip()
            if not lbl:
                continue
            # Direct substring OR canonical-name match (handles
            # 'Fortune Teller' ↔ 'fortune_teller' and similar drift)
            canonical = _canonical_name(lbl) or ""
            if (
                target in lbl
                or canonical == target
                or token_sim(lbl, target) >= 0.7
            ):
                logger.info(
                    f"  Building nameplate visible: {t.label!r} @ ({t.cx},{t.cy}) "
                    f"— tapping to enter"
                )
                tap(t.cx, t.cy)
                return True
        return False

    # Keywords that indicate the harbor panel is open in the right panel.
    # The harbor does NOT open a new screen — it replaces the building list
    # content in the port overworld right panel.  where_am_i() stays
    # 'port_overworld' the whole time, so we detect it by OCR content instead.

    from brain.kb import control as _ckb
    _HARBOR_PANEL_KWS    = _ckb().harbor_panel_keywords()
    _HARBOR_TARGET_NAMES = {v.lower() for v in _ckb().building_name_variants("harbor")}

    def _harbor_panel_open(frame) -> bool:
        """True if the right-panel building list shows harbor content."""
        if target.lower() not in _HARBOR_TARGET_NAMES:
            return False
        labels = {lbl.lower() for lbl, *_ in read_building_menu(frame)}
        return bool(labels & _HARBOR_PANEL_KWS)

    # Canonical building names + variants for signature filtering.  The
    # building-menu OCR region overlaps NPC speech bubbles, which produce
    # alpha-looking fragments ("you", "lution", "perharb"...) on every
    # frame.  Restricting the signature to known building types eliminates
    # this NPC-driven churn so a constant-NPC scene still produces a
    # stable signature.
    from brain.kb import control as _ckb_for_sig
    _CANONICAL_BUILDINGS: set[str] = set()
    for _btype, _bdata in _ckb_for_sig().known_building_types().items():
        _CANONICAL_BUILDINGS.add(_btype.lower())
        for _v in _bdata.get("all_names", []):
            _CANONICAL_BUILDINGS.add(_v.lower())

    def _overworld_signature(frame) -> tuple[str, ...]:
        """
        Stable signature of the port_overworld for change detection.
        Restricted to canonical building names from the KB so NPC speech
        bubbles (which the building-menu OCR region picks up alongside
        real labels) don't perturb the signature on every frame.
        Identical signature ⇒ tap likely didn't register;
        different signature ⇒ something is changing — keep waiting.
        """
        return tuple(sorted({
            lbl.lower().strip()
            for lbl, *_ in read_building_menu(frame)
            if lbl.lower().strip() in _CANONICAL_BUILDINGS
        }))

    def _reparse_for_always_present() -> None:
        """A basic building the list scan + port map both missed this pass, but it
        MUST exist (Harbor/Market/Inn/Bureau/Shipyard).  Don't give up — log it as
        a perception miss and pace the loop so the next iteration re-captures and
        re-scans (fresh frame + port-map retry), bounded by the outer deadline."""
        import random
        logger.warning(
            f"  '{building_name}' not found this pass but it ALWAYS exists at every "
            "port — reparsing (perception miss / scrolled off), not giving up"
        )
        time.sleep(random.uniform(0.6, 1.1))

    logger.info(f"Navigating to '{building_name}'…")

    deadline           = time.time() + timeout
    last_tap_time      = 0.0    # 0 → first overworld tap has no waiting period
    first_tap_time     = 0.0    # time of the most recent tap that has not yet
                                # produced a state change — used to cap total
                                # post-tap wait when signature flicker would
                                # otherwise extend patience indefinitely
    last_tap_signature: Optional[tuple[str, ...]] = None
    # Patience after a tap before retrying.  Long because a character may have
    # to walk across the port to the building before the scene transitions —
    # there is no local signal that distinguishes "walking" from "tap didn't
    # register".  Signature comparison handles partial OCR flicker (NPC bubbles,
    # weather effects) by extending patience when the canonical-building
    # signature changes, while MAX_WAIT_AFTER_TAP guarantees retap eventually
    # even if the signature keeps flickering.
    # 60s covers character walking time across most ports plus perception
    # overhead.  Retap before this would re-issue the tap while the character
    # is still in transit — the queued tap can land inside the destination
    # building once the scene loads (e.g. on an NPC mate inside the inn).
    TAP_RETRY_COOLDOWN  = 60.0
    MAX_WAIT_AFTER_TAP  = 120.0

    # Basic buildings (Harbor, Market, Inn, Bureau, Shipyard) exist at EVERY
    # port and the right-panel list is scrollable — so "not found in this parse"
    # is a perception miss (scrolled off / icon-prefix mis-OCR), never absence.
    # For these, don't give up on a single failed scan: reparse until the
    # deadline (the port-map fallback + a fresh capture next iteration).  See
    # brain.kb always_present_buildings / memory project_basic_buildings_always_present.
    from brain.kb import control as _kb_control
    _always_present = _kb_control().is_always_present(building_name)

    # Cross-iteration no-progress tracker for the `building` handler.  A wrong
    # building → Back normally returns to port_overworld, which the loop already
    # drives to the target — so Back is a single action that hands control back to
    # the perceive loop (NOT an inline perceive-and-branch).  Only when we keep
    # landing in the SAME wrong building (Back changing nothing — a modal eating
    # Back) do we escalate to a bounded Home-escape.  See
    # docs/navigate_to_building_review.md.
    _wrong_building_sig    = None
    _wrong_building_streak = 0

    while time.time() < deadline:
        # Capture once per loop — share the frame with all sub-calls to avoid
        # redundant ADB screencaps (~5s each).
        frame = capture_screen()

        # Clear unexpected NON-GAME blockers that freeze navigation — the idle
        # lock/screensaver and the game's promo/store popups (perceive's interruptor
        # layer doesn't cover these graphical promos / the lock state). Dismiss +
        # re-capture. See brain/unexpected_dialog.clear_blockers.
        try:
            from brain.unexpected_dialog import clear_blockers
            if clear_blockers(frame).get("cleared"):
                time.sleep(1.0)
                frame = capture_screen()
        except Exception as _exc:
            logger.debug(f"  [{building_name}] clear_blockers failed: {_exc}")

        # Special case: harbor is a right-panel overlay, not a new screen.
        # Detect it by its unique panel content before checking where_am_i().
        if _harbor_panel_open(frame):
            logger.info(f"  Harbor panel is open — confirmed by panel content")
            return True

        # Use perceive() so interruptors are dismissed before state detection.
        from brain.perceive import perceive as _perceive
        _pr  = _perceive(frame)
        loc  = _pr.to_location_dict()
        logger.info(f"  [{building_name}] state={loc['location']!r} — {loc['detail']}")

        # State 'sub_menu' (Phase 5e L1): bot is one level deeper than a
        # building (e.g. inside Recruit Crew under Harbor).  If the
        # sub-menu name is a known sub-menu of the target building, press
        # Back once to reach the building's main pane and re-check.
        # Without this, navigate_to_building polls for state='building'
        # forever and the 60s timeout fires while the bot is sitting at a
        # legitimate sub-menu inside the target.
        if loc["location"] == "sub_menu":
            sm_title = (loc.get("detail", "")
                        .replace("sub_menu:", "")
                        .strip().lower()
                        .split(" — ", 1)[0].strip())
            from brain.kb import control as _ckb
            target_kb   = _ckb().known_building_types().get(target.lower(), {})
            sub_menus_kb = [
                s.get("id", "").lower().replace("_", " ") if isinstance(s, dict) else s.lower()
                for s in target_kb.get("sub_menus", [])
            ]
            sub_screens  = [s.lower() for s in target_kb.get("sub_screens", [])]
            known_subs   = set(sub_menus_kb) | set(sub_screens)
            if sm_title in known_subs or any(sm_title in s or s in sm_title for s in known_subs if s):
                logger.info(
                    f"  At sub_menu {sm_title!r} which is a known sub-menu of "
                    f"{target!r} — pressing Back to reach building's main pane"
                )
                press_back()
                time.sleep(2.0)
                continue
            # Unknown sub_menu — press Back to back out one level and
            # let the main loop re-evaluate (we may end up in a building
            # we still need to navigate out of).
            logger.info(
                f"  At sub_menu {sm_title!r} (not a known sub-menu of "
                f"{target!r}) — pressing Back to back out one level"
            )
            press_back()
            time.sleep(2.0)
            continue

        if loc["location"] == "building":
            # Verify we are in the RIGHT building, not just any building.
            # detail format: "building: <title>" or "building (title unreadable)"
            # When Qwen appends a description, format becomes
            # "building: <ocr_title> — <qwen_description>".  Split on " — " to
            # get the OCR title alone — Qwen's description can hallucinate
            # (e.g. labels the inn as "harbor") and must not feed KB lookups.
            detail          = loc.get("detail", "")
            bld_title       = detail.replace("building:", "").strip().lower()
            bld_title_short = bld_title.split(" — ", 1)[0].strip()
            # Building-name variants from KB — handles cases where the
            # in-game title bar differs from the building canonical name
            # (e.g. item_shop's interior reads as "Shop"; harbor's reads
            # as "Harbour" in some locales).
            from brain.kb import control as _ckb_for_variants
            _target_variants = {target} | {
                v.lower() for v in _ckb_for_variants().building_name_variants(target)
            }
            title_ok        = (
                not bld_title                                # unreadable — optimistically accept
                or "unreadable" in bld_title_short           # unreadable — optimistically accept
                or target in bld_title_short                 # e.g. "harbor" in "harbour"
                or bld_title_short in _target_variants       # KB variants (e.g. "shop" → item_shop)
                or any(v in bld_title_short for v in _target_variants if len(v) >= 3)
                or token_sim(bld_title_short, target) >= 0.65
            )
            if title_ok:
                logger.info(f"Inside '{building_name}' — confirmed (screen: {bld_title_short!r})")
                return True

            # Title doesn't match — check KB whether it's a known sub-screen of
            # the target building (e.g. "interview" is inside the inn).  If so,
            # press Back to reach the main menu and trust the destination —
            # don't re-validate against the post-Back title (Qwen often
            # hallucinates it; OCR may read it as garbled).
            from brain.kb import control as _ckb
            target_kb   = _ckb().known_building_types().get(target.lower(), {})
            sub_screens = [s.lower() for s in target_kb.get("sub_screens", [])]
            if bld_title_short in sub_screens:
                logger.info(
                    f"  {bld_title_short!r} is a known sub-screen of {target!r} — "
                    "pressing Back to reach main menu"
                )
                press_back()
                time.sleep(2.0)
                frame_back = capture_screen()
                loc_back   = _perceive(frame_back).to_location_dict()
                if loc_back["location"] == "building":
                    logger.info(
                        f"  Back from {bld_title_short!r} → still in building — "
                        f"trusting it's {target!r}"
                    )
                    return True
                logger.warning(
                    f"  Back from {bld_title_short!r} left building "
                    f"(now {loc_back['location']!r}) — re-evaluating from main loop"
                )
                continue

            # Not a known sub-screen.  Before trying a blind Back, look up a
            # learned recovery for the CURRENT screen — keywords from the live
            # screen are far more reliable than the post-Back hallucinated title.
            from brain.human_escalation import _match_learned_recovery
            lr_text_pre = f"building {bld_title} navigate to {target}"
            plan = _match_learned_recovery("building", lr_text_pre)
            if plan:
                from brain.human_escalation import _execute_plan
                logger.info(
                    f"  Applying learned recovery (matched on current screen): "
                    f"{plan.scenario_id!r}"
                )
                _execute_plan(plan)
                time.sleep(2.0)
                continue

            # No KB match and this is the WRONG building.  The single correct
            # action is Back: a wrong building → Back normally returns to
            # port_overworld, which the loop's port_overworld handler already
            # drives to the target (tap from list / port map).  Do NOT inline-
            # perceive-and-branch here — press Back and hand control back to the
            # loop top (perceive → dispatch).  Track no-progress so a Back that
            # changes nothing (a modal eating Back) escalates to a bounded
            # Home-escape rather than looping until the deadline.
            _sig = (loc["location"], bld_title_short)
            if _sig == _wrong_building_sig:
                _wrong_building_streak += 1
            else:
                _wrong_building_sig    = _sig
                _wrong_building_streak = 0

            if _wrong_building_streak >= 2:
                logger.warning(
                    f"  Still in {bld_title_short!r} after {_wrong_building_streak + 1} "
                    "Back attempts (Back not changing the screen) — Home-escape"
                )
                # Route through the canonical exit helper rather than tapping the Home slot
                # blind. It checks the chrome first: that slot is the HAMBURGER on an
                # overworld, so a blind tap there opens the main menu instead of leaving
                # (memory: project_home_button_is_chromed_only_escape). It also prefers an
                # on-screen close target over system Back.
                from actions.screen_exit import exit_current_screen
                res = exit_current_screen()
                logger.info(f"  exit_current_screen → {getattr(res, 'method', res)}")
                time.sleep(2.0)
                last_tap_time          = time.time() - TAP_RETRY_COOLDOWN
                first_tap_time         = 0.0
                last_tap_signature     = None
                _wrong_building_sig    = None
                _wrong_building_streak = 0
                continue

            # BEFORE forcing anything: does the screen contradict the premise of this call?
            # Live 2026-08-22 the departure had ALREADY succeeded and the screen read
            # "sailing to Melanesian Village". This branch pressed Back, cancelled the
            # voyage, and the mission re-ran the whole village search — four times, 18.5
            # minutes, to achieve what the first attempt had done in 65 seconds.
            #
            # A stale caller does not get to overwrite what the bot can see. Hand the
            # perceived state back and let the caller update itself.
            if _screen_says_under_way(loc["location"], bld_title_short):
                logger.info(
                    f"  Screen says {bld_title_short!r} — the fleet is already under way, so "
                    f"{building_name!r} is moot. Leaving the screen ALONE and reporting the "
                    "real state instead of pressing Back."
                )
                return False

            logger.info(
                f"  Title {bld_title_short!r} doesn't match {building_name!r} — "
                "pressing Back, then re-perceiving from the loop top"
            )
            press_back()
            time.sleep(2.0)

        elif loc["location"] == "loading":
            pass  # transition in progress — wait for next poll

        elif loc["location"] == "port_overworld":
            # First: if the target's building NAMEPLATE is visible in the
            # current frame, tap it directly.  This handles the case where
            # the character has walked to the entrance (after a port-map
            # tap or a previous list tap) but auto-entry was blocked by an
            # ambient overlay or the entrance just doesn't auto-trigger.
            # Tapping the nameplate is the same as tapping the door —
            # bypasses the wait-for-state-change cycle entirely.
            if _tap_nameplate_if_visible(frame):
                last_tap_time      = time.time()
                if not first_tap_time:
                    first_tap_time = last_tap_time
                last_tap_signature = _overworld_signature(frame)
                deadline           = time.time() + timeout
                time.sleep(2.0)
                continue

            elapsed_cooldown = time.time() - last_tap_time
            elapsed_total    = time.time() - first_tap_time if first_tap_time else 0.0
            force_retap      = (first_tap_time and elapsed_total >= MAX_WAIT_AFTER_TAP)

            if last_tap_signature is not None and elapsed_cooldown < TAP_RETRY_COOLDOWN:
                # Inside the post-tap patience window — wait, don't retap.
                pass
            elif force_retap:
                logger.warning(
                    f"  No state change in {elapsed_total:.0f}s since first tap "
                    "— forcing retap"
                )
                if _tap_from_list(frame) or _tap_from_port_map():
                    last_tap_time      = time.time()
                    first_tap_time     = last_tap_time  # reset the total wait clock
                    last_tap_signature = _overworld_signature(capture_screen())
                    deadline           = time.time() + timeout
                elif _always_present:
                    _reparse_for_always_present()
                    first_tap_time     = 0.0
                    last_tap_signature = None
                else:
                    logger.error(f"  Cannot find '{building_name}' anywhere — giving up")
                    return False
            else:
                # Eligible for retap.  Compare overworld signature against the
                # snapshot taken right after the last tap.  If it changed,
                # something is happening (overlay dismissed, transit starting)
                # — extend patience instead of issuing another tap.
                current_sig = _overworld_signature(frame)
                if (last_tap_signature is not None
                        and current_sig != last_tap_signature):
                    logger.info(
                        f"  Overworld signature changed since last tap "
                        f"(now {len(current_sig)} labels) — extending patience"
                    )
                    last_tap_signature = current_sig
                    last_tap_time      = time.time()
                else:
                    # Reuse the already-captured frame for the building list scan.
                    if _tap_from_list(frame) or _tap_from_port_map():
                        last_tap_time      = time.time()
                        if not first_tap_time:
                            first_tap_time = last_tap_time
                        # Snapshot AFTER the tap — if a popup was on screen,
                        # it may have been dismissed by this tap, so the
                        # signature for change-detection is the post-tap one.
                        last_tap_signature = _overworld_signature(capture_screen())
                        # Reset deadline so the new attempt gets its full time budget.
                        # Without this, a tap issued near the deadline has no time to confirm.
                        deadline = time.time() + timeout
                    elif _always_present:
                        _reparse_for_always_present()
                        first_tap_time     = 0.0
                        last_tap_signature = None
                    else:
                        logger.error(f"  Cannot find '{building_name}' anywhere — giving up")
                        return False

        elif loc["location"] == "world_map":
            # Back on the WORLD MAP safely returns to the overworld (this is NOT the Exit-Game
            # trap — that's Back on the overworld / main_menu).
            logger.info("  On world map — pressing Back to return to overworld")
            press_back()
            time.sleep(1.5)

        elif loc["location"] in ("sea", "sea_cinematic", "main_menu"):
            # DON'T blind-Back here.  Back is context-dependent: on the overworld / main_menu it
            # opens the "Exit Game?" prompt (a shutdown near-miss, live 2026-08-19), and Back at sea
            # is unhelpful.  Right after arrival the port is still SETTLING (arrival cinematic /
            # stale sea overlays), so WAIT and RE-PERCEIVE until it settles into a stable
            # port_overworld with the building list — never act on an assumed state.  If an Exit-
            # Game / Notice dialog is already up, dismiss it (Back on a DIALOG = Cancel, which is
            # safe; never tap OK).  We do NOT reset the deadline, so the outer timeout fires if it
            # never settles and the caller can re-plan instead of looping forever.
            if _screen_contains(frame, "exit game", "leave game", "quit game",
                                "do you want to exit"):
                logger.info("  Exit-Game dialog up — dismissing via Cancel (Back-on-dialog)")
                press_back()
                time.sleep(1.5)
            else:
                logger.info(f"  Unsettled {loc['location']!r} (port not ready) — waiting to "
                            "re-perceive, NOT blind-Backing")
                time.sleep(2.0)

        # "unknown" — likely a transient overlay; wait and re-poll

    logger.error(f"Could not enter '{building_name}' after {timeout:.0f}s")
    return False


# ── Step 2: navigate to Harbour ───────────────────────────────────────────────

def _navigate_to_harbour() -> bool:
    return navigate_to_building("harbor")


# ── Step 3: depart from Harbour ───────────────────────────────────────────────

def _tap_depart_button(frame) -> str:
    """
    Detect and tap the correct departure button using OmniParser + OCR.

    The harbour right panel can show different button sets depending on supply state:
      Fully supplied     → one button: "Depart Now" (yellow)
      Not fully supplied → two buttons: "Depart Now" (inactive) + "Supply Depart" (yellow)

    Strategy: scan ALL elements in the right panel (x > 1600), collect every one
    whose label contains "depart" or "supply".  Log the full list so misses are
    visible in logs.  Then:
      - If a "supply depart" variant is found → tap it
      - Else tap the single "depart now" / "depart" button found

    Returns: "supply_depart" | "depart_now" | "not_found"
    """
    # ── Verify we are actually in the harbour top level ─────────────────────
    # Harbour sub-menus (Supply, Repair, etc.) show their own title — they
    # do NOT have departure buttons.  Load sub-menu labels from KB
    # (building_types/harbor.json) and match against screen title.
    from vision.ocr import read_screen_title
    from brain.kb import ControlKB
    bld_title = read_screen_title(frame).lower().strip()

    # Load non-departure sub-menu labels from KB
    harbor_type = ControlKB().known_building_types().get("harbor", {})
    non_departure_labels = set()
    for sm in harbor_type.get("sub_menus", []):
        if sm["id"] != "departure":
            for lbl in sm.get("labels", []):
                non_departure_labels.add(lbl.lower())

    if bld_title and any(lbl in bld_title for lbl in non_departure_labels):
        logger.info(
            f"  In harbour sub-menu {bld_title!r} — pressing Back to harbour top level"
        )
        press_back()
        time.sleep(2.0)
        frame = capture_screen()
        bld_title = read_screen_title(frame).lower().strip()
    if bld_title and not ControlKB().detail_mentions_building(bld_title, "harbor"):
        logger.warning(f"  _tap_depart_button called but screen title is {bld_title!r} — not harbour")

    from vision.omniparser import get_omniparser

    # ── OmniParser scan of harbour right panel ────────────────────────────────
    parser = get_omniparser()
    all_elements = parser.parse_fast(frame) if parser.yolo_available() else []
    right_panel = [e for e in all_elements if e.cx > 1600]
    logger.info(f"  Harbour right panel: {len(right_panel)} elements detected")
    for e in right_panel:
        logger.info(f"    [{e.element_type}] {e.label!r} @ ({e.cx},{e.cy})")

    # ── OCR fallback: scan right panel region for button text ─────────────────
    ocr_tokens = _ocr_frame(frame.crop((1600, 600, frame.width, frame.height)))
    logger.info(f"  Harbour right panel OCR ({len(ocr_tokens)} tokens):")
    for text, conf, cx, cy in ocr_tokens:
        logger.info(f"    {text!r} conf={conf:.2f} @ ({cx + 1600},{cy + 600})")

    # ── Check blocking signals before attempting departure ────────────────────
    # If "Not Enough Crew" or similar blocking text is visible, departure will
    # fail. Return "blocked" immediately so the caller can escalate rather than
    # wasting 90s waiting for a sea view that will never come.
    from brain.fsm_registry import get_fsm_registry as _get_fsm_reg
    _dep_flow = _get_fsm_reg().flows.get("harbor_departure")
    _blocking = _dep_flow._raw.get("blocking_signals", []) if _dep_flow else []
    _ocr_text = " ".join(t.lower() for t, _, _, _ in ocr_tokens)
    for sig in _blocking:
        if fuzzy_contains(_ocr_text, sig["text"]):
            logger.warning(
                f"  Departure blocked: {sig['text']!r} visible — {sig['description']}"
            )
            # Return the full signal dict so the caller can dispatch to resolution
            return sig

    # ── Find depart-related buttons ───────────────────────────────────────────
    # OCR candidates come FIRST — OCR text positions are more accurate than
    # OmniParser YOLO bboxes for text-labeled buttons (YOLO can misalign
    # vertically, e.g. reporting "Depart Now" ~100px above the actual button).
    # OmniParser is kept as fallback for when OCR finds nothing.
    candidates = []
    for text, conf, cx, cy in ocr_tokens:
        tl = text.lower()
        if "depart" in tl or "supply" in tl:
            candidates.append((tl, cx + 1600, cy + 600))
    for e in right_panel:
        lbl = e.label.lower()
        if "depart" in lbl or "supply" in lbl:
            candidates.append((lbl, e.cx, e.cy))

    logger.info(f"  Depart-related candidates: {candidates}")

    # Load button definitions from KB (harbor_departure flow → departure_buttons).
    # Ordered by priority — first match wins.
    # New button variants are added to flows.json; no code change needed.
    from brain.fsm_registry import get_fsm_registry
    flow = get_fsm_registry().flows.get("harbor_departure")
    departure_buttons = flow._raw.get("departure_buttons", []) if flow else []

    if not departure_buttons:
        # KB missing — fall back to hardcoded minimal set so the bot never breaks
        logger.warning("  harbor_departure KB has no departure_buttons — using built-in fallback")
        departure_buttons = [
            {"id": "supply_depart", "label_variants": ["supply departure", "supply depart"], "priority": 1},
            {"id": "auto_supply",   "label_variants": ["auto supply"],                       "priority": 2},
            {"id": "depart_now",    "label_variants": ["depart now", "depart"],               "priority": 3},
        ]

    departure_buttons = sorted(departure_buttons, key=lambda b: b.get("priority", 99))

    # Context-dependent buttons (e.g. "auto_supply" in supply_prep_dialog)
    # are only valid when their context is active.  Detect context from OCR:
    # if any candidate contains "depart", we're in the departure context.
    _has_depart_candidate = any("depart" in lbl for lbl, _, _ in candidates)

    for btn_def in departure_buttons:
        # Skip context-dependent buttons when their context isn't active.
        # The "context" field in KB (flows.json) specifies when a button
        # is valid — e.g. "supply_prep_dialog" means only tap it when
        # a depart button is also visible (we're in the prep dialog).
        btn_context = btn_def.get("context")
        if btn_context and not _has_depart_candidate:
            logger.info(
                f"  Skipping {btn_def['id']!r} — context {btn_context!r} "
                f"not active (no 'depart' candidate visible)"
            )
            continue

        variants = btn_def.get("label_variants", [])
        pos = next(
            ((cx, cy) for lbl, cx, cy in candidates
             if any(v in lbl for v in variants)),
            None,
        )
        if pos:
            logger.info(
                f"  Tapping {btn_def['id']!r} "
                f"(matched variants {variants}) @ {pos}"
            )
            tap(*pos)
            return btn_def["id"]

    # Nothing found — log actual location before giving up
    loc = where_am_i()
    logger.warning(
        f"  No depart button found — current location: {loc.get('location')} "
        f"port={loc.get('port')} detail={loc.get('detail')!r}"
    )
    return "not_found"


def _ensure_harbor_top_level() -> "Image.Image":
    """
    Ensure we're on the harbor top level (the departure view).

    The harbor top level is the DEFAULT view when you first enter — it shows
    "Depart Now" and fleet status.  There is no "Departure" tab.  The sub-menus
    (Supply, Repair, Recruit Crew) are entered by tapping; pressing Back returns
    to the top level.

    If the screen title matches any sub-menu label from KB, press Back.
    Returns the current frame.
    """
    from vision.ocr import read_screen_title
    from brain.kb import ControlKB

    frame = capture_screen()
    title = read_screen_title(frame).lower().strip()

    # Load sub-menu labels from KB — any match means we're in a sub-menu
    harbor_type = ControlKB().known_building_types().get("harbor", {})
    sub_menu_labels = set()
    for sm in harbor_type.get("sub_menus", []):
        if sm["id"] != "departure":  # "departure" in KB represents the top level
            for lbl in sm.get("labels", []):
                sub_menu_labels.add(lbl.lower())

    if title and any(lbl in title for lbl in sub_menu_labels):
        logger.info(
            f"  In harbour sub-menu {title!r} — pressing Back to top level"
        )
        press_back()
        time.sleep(2.0)
        frame = capture_screen()

    return frame


def _ensure_fleet_ready() -> bool:
    """
    Pre-departure readiness check: scan harbour panel for blocking conditions
    and resolve them BEFORE attempting departure.

    Detection: OCR text matched against KB blocking_signals, PLUS a visual
    check for enabled (yellow) departure buttons.  If OCR misses the blocker
    text but no yellow button is visible, the fleet still can't depart.

    Resolution chain (first match wins):
      1. KB blocking_signals with action="resolve" → predefined resolution steps
      2. Learned recoveries (learned_recoveries.json) → replay what worked before
      3. Claude Vision → reason about the screen and prescribe an action
      4. Human operator → describe what to do; saved to KB for next time

    Re-navigates to harbour after each resolution so the next check reads fresh state.
    Retries up to 3 times (covers multi-blocker scenarios: no crew AND low supply).

    Returns True when fleet is ready to depart, False on unrecoverable failure.
    """
    from brain.fsm_registry import get_fsm_registry

    dep_flow = get_fsm_registry().flows.get("harbor_departure")
    blocking_signals = dep_flow._raw.get("blocking_signals", []) if dep_flow else []

    MAX_ATTEMPTS = 3
    for attempt in range(MAX_ATTEMPTS):
        # Ensure we're on the Departure tab before checking.
        # Other sub-menus (Supply, Recruit Crew) have their own yellow buttons
        # that would cause false "all clear".
        frame = _ensure_harbor_top_level()
        tokens = _ocr_frame(frame, min_conf=0.3)
        text = " ".join(t.lower() for t, _, _, _ in tokens)

        # ── Check 1: Known blocker text (fuzzy OCR match) ────────────────
        blocker = next(
            (sig for sig in blocking_signals if fuzzy_contains(text, sig["text"])),
            None,
        )

        # ── Check 2: No yellow departure button → fleet can't depart ────
        yellow = _find_yellow_button(frame, x_min=1600)
        if blocker is None and yellow is not None:
            logger.info("  Fleet readiness check: all clear")
            return True

        if blocker:
            logger.warning(
                f"  Fleet not ready (attempt {attempt + 1}/{MAX_ATTEMPTS}): "
                f"{blocker['text']!r} — {blocker.get('description', '')}"
            )
        else:
            logger.warning(
                f"  Fleet not ready (attempt {attempt + 1}/{MAX_ATTEMPTS}): "
                f"no yellow departure button visible (unknown blocker). "
                f"OCR: {text[:120]!r}"
            )

        # ── Resolution chain ─────────────────────────────────────────────

        # Step 1: Try KB predefined resolution (flows.json blocking_signals)
        if blocker and blocker.get("action") == "resolve":
            from brain.recovery import execute_resolution
            try:
                ok = execute_resolution(blocker, context="pre_departure_readiness")
            except Exception as e:
                logger.error(f"  execute_resolution raised: {e}")
                ok = False
            if ok:
                if not _navigate_to_harbour():
                    logger.warning("  Could not re-enter harbour after resolution")
                    return False
                continue  # re-check

        # Step 2: Try learned recoveries (what worked before)
        from brain.human_escalation import _match_learned_recovery, _execute_plan
        state_text = f"building building: harbor {text}"
        plan = _match_learned_recovery("building", state_text)
        if plan:
            logger.info(f"  Trying learned recovery: {plan.scenario_id!r}")
            _execute_plan(plan)
            time.sleep(2.0)
            # Re-check: navigate back to harbour to verify
            if not _navigate_to_harbour():
                logger.warning("  Could not re-enter harbour after learned recovery")
                return False
            continue  # re-check

        # Step 3: Ask Claude Vision to reason about the screen
        logger.info("  No predefined or learned resolution — asking Claude Vision")
        from brain.perceive import perceive, PerceiveResult
        pr = perceive(frame)
        resolved = _resolve_blocker_with_reasoning(frame, pr, text)
        if resolved:
            if not _navigate_to_harbour():
                logger.warning("  Could not re-enter harbour after Claude resolution")
                return False
            continue  # re-check

        # Step 4: Escalate to human
        logger.warning("  Claude could not resolve — escalating to human operator")
        from brain.human_escalation import escalate
        pr = perceive(frame)
        result = escalate(context="fleet cannot depart", perceive_result=pr)
        if result.state == "building":
            # Human resolved something — re-check
            if not _navigate_to_harbour():
                return False
            continue
        # Human couldn't help or no tty
        return False

    logger.warning("  Fleet readiness check exhausted retries")
    return False


def _build_omniparser_table(frame) -> tuple[str, list]:
    """Format the current frame's OmniParser elements as a text table for
    inclusion in Claude prompts.  Mirrors `vision.claude_vision.analyse_scene`
    so Claude reasons over structured element data — type, exact tap
    coordinates, label — rather than pixel-hunting on a downscaled
    thumbnail.

    Returns (table_str, elements).  Empty string + empty list when
    OmniParser is unavailable / yielded nothing.

    2026-05-12: added because `_resolve_blocker_with_reasoning` was
    sending Claude only a 1200×540 thumbnail + 300 chars of truncated OCR
    text.  Claude's resulting plans baked in pixel-tight regions that
    didn't survive any UI shift (the `Supply Departure region 220×40`
    failure of 2026-05-12 17:19).
    """
    try:
        from vision.omniparser import get_omniparser, parse_fast_cached
        if not get_omniparser().yolo_available():
            return "", []
        elements = parse_fast_cached(frame)
        if not elements:
            return "", []
        lines = ["idx | type   | tap_x | tap_y | label"]
        lines.append("----|--------|-------|-------|------")
        for i, el in enumerate(elements):
            lines.append(
                f"{i:3d} | {el.element_type:6s} | {el.cx:5d} | {el.cy:5d} | {el.label}"
            )
        return "\n".join(lines), list(elements)
    except Exception as e:
        logger.debug(f"[claude] _build_omniparser_table failed: {e}")
        return "", []


# Tokens that appear on the harbour Departure panel.  Used as a positive
# anchor in `_check_blocker_resolved` — only when one of these is visible
# do we declare the blocker resolved (otherwise we may just be on a
# different screen where the blocker text simply isn't drawn).
_DEPART_PANEL_TOKENS = ("depart now", "supply departure", "set sail")


def _check_blocker_resolved(blocker_text: str, frame=None) -> Optional[bool]:
    """Re-check whether a known blocker (e.g. 'Not Enough Crew') is still
    on screen.

    Conservative semantics — only declares the blocker resolved on
    POSITIVE evidence:

      True  — depart-panel anchor ('Depart Now' / 'Supply Departure' /
              'Set Sail') visible AND the blocker phrase is NOT visible.
              Caller can exit escalation early.
      False — blocker phrase IS visible somewhere on screen.  Still
              blocked, continue.
      None  — cannot determine.  Neither blocker text nor depart-panel
              anchor visible — we're on some intermediate screen and
              this check has no opinion.

    The "depart-panel anchor" guard is critical: during escalation the
    bot is usually in a sub-menu (recruit / supply) where the blocker
    text never appears because the depart panel isn't drawn.  Without
    the anchor, absence-of-blocker would falsely declare resolution mid-
    escalation.
    """
    if not blocker_text:
        return None
    if frame is None:
        from capture.adb_capture import capture_screen
        frame = capture_screen()
    tokens = _ocr_frame(frame, min_conf=0.3)
    text = " ".join(t.lower() for t, _, _, _ in tokens)

    blocker_low = blocker_text.lower()
    # Significant words (len >= 4) — drops connectors like "not", "is".
    # All must be visible for the blocker to count as "still there".
    blocker_words = [w for w in blocker_low.split() if len(w) >= 4]
    blocker_visible = bool(blocker_words) and all(w in text for w in blocker_words)
    on_depart_panel = any(t in text for t in _DEPART_PANEL_TOKENS)

    if blocker_visible:
        return False
    if on_depart_panel:
        return True
    return None


def _resolve_blocker_with_reasoning(frame, perceive_result, ocr_text: str) -> bool:
    """
    Ask Claude Vision to look at the harbour screen with a blocker and
    prescribe an action.  If the action changes the state, save it as a
    learned recovery for next time.

    Returns True if state changed (blocker likely resolved), False otherwise.
    """
    import base64, io, json as _json, os
    try:
        import anthropic
    except ImportError:
        return False

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return False

    # ── Pre-flight: is the blocker still actually there? ─────────────────────
    # The caller may have decided "not enough crew" was the blocker some time
    # ago; the screen state may have changed since (game ticked, redistribution
    # auto-fired, OCR misread).  Re-check before spending an API call + 3+
    # plan steps on a problem that no longer exists.
    pre_check = _check_blocker_resolved(ocr_text, frame=frame)
    if pre_check is True:
        logger.info(
            "  [blocker_check] depart panel clear — original blocker no "
            "longer present; skipping Claude resolution entirely"
        )
        return True

    # Build the OmniParser element table so Claude reasons over structured
    # text instead of pixel-hunting on a downscaled thumbnail (2026-05-12).
    element_table, _omni_elements = _build_omniparser_table(frame)

    thumb = frame.copy()
    thumb.thumbnail((1200, 540))
    buf = io.BytesIO()
    thumb.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    prompt = f"""\
The bot is in the Harbor and wants to depart (set sail), but the departure button
is DISABLED. Something is blocking departure.

A local UI detector (OmniParser) has already identified the following elements
on screen with their exact tap coordinates:

{element_table or "(OmniParser unavailable — infer from image only)"}

Current OCR text from screen (for context): {ocr_text!r}

Look at the screenshot AND the element table above, then determine:
1. What is blocking departure? (e.g. "Not Enough Crew", "Not Enough Supply")
2. What is the COMPLETE action sequence to resolve it and return to departure?

IMPORTANT: Include ALL steps to FULLY resolve the blocker — not just navigation.
For example, if crew is needed:
  - Tap the "Recruit Crew" tab to open recruit screen
  - Tap the yellow "Recruit" action button to actually hire the crew
  - Press Back to return to the harbor departure tab
Just navigating to a screen without tapping the action button does NOT resolve anything.

The harbor has tabs in the top-left: Supply, Repair, Recruit Crew, Departure.
The bot can also press Back to leave and navigate to other buildings (Inn, Market, etc).

── Choosing the action shape ─────────────────────────────────────────────────
STRONGLY PREFER "find_and_tap" with the EXACT label string from the element
table above. The bot will look up that element at runtime and tap its precise
coordinates — this survives small UI shifts (e.g. text rendering, animation
frames) that pixel-coords would not.

Only fall back to "tap" with raw (x, y) if the element you want is genuinely
not in the table. When you do use raw (x, y), use the original 2400×1080 space
(NOT the half-resolution thumbnail).

Avoid info-only labels — anything that looks like "Fleet Crew Size 786/1,676"
or "Total Load Capacity 1,139/2,500" is a stats display, not an action button.
Action buttons say things like "Recruit", "Confirm", "Depart Now", "Supply
Departure", "OK".

Return ONLY valid JSON:
{{
  "blocker": "<what is blocking departure>",
  "reasoning": "<why this action sequence resolves it>",
  "scenario_id": "short_snake_case_id",
  "detection_keywords": ["keyword1", "keyword2"],
  "actions": [
    {{"type": "find_and_tap", "label": "exact label from element table"}},
    {{"type": "tap", "x": 123, "y": 456}},
    {{"type": "press_back"}}
  ]
}}
"""

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = _json.loads(raw)
    except Exception as e:
        logger.warning(f"  _resolve_blocker_with_reasoning failed: {e}")
        return False

    logger.info(
        f"  Claude blocker analysis: {data.get('blocker')!r} "
        f"→ {data.get('reasoning', '')[:80]}"
    )

    # Execute the prescribed actions
    from brain.human_escalation import ActionStep, _execute_plan, EscalationPlan
    from brain.human_escalation import _save_as_learned_recovery
    actions = []
    for a in data.get("actions", []):
        actions.append(ActionStep(
            type    = a.get("type", "tap"),
            label   = a.get("label", ""),
            x       = a.get("x", 0),
            y       = a.get("y", 0),
            x2      = a.get("x2", 0),
            y2      = a.get("y2", 0),
            seconds = a.get("seconds", 1.0),
            x_min   = a.get("x_min", -1),
            y_min   = a.get("y_min", -1),
            x_max   = a.get("x_max", -1),
            y_max   = a.get("y_max", -1),
        ))

    plan = EscalationPlan(
        scenario_id        = data.get("scenario_id", "harbor_blocker"),
        category           = "flow_step",
        description        = data.get("reasoning", ""),
        actions            = actions,
        detection_keywords = data.get("detection_keywords", []),
    )
    # Context-aware drain: extract goal keywords from the Claude-reported
    # blocker and pass them down so the post-tap drain only taps buttons
    # relevant to the active goal.  Without this, the drain would tap
    # any visible positive label (e.g. the harbour 'Trade Goods' tab
    # while trying to recruit crew — 2026-05-04 19:03 live log).
    from brain.human_escalation import _extract_goal_keywords
    blocker_text = data.get("blocker") or ocr_text
    goal_keywords = _extract_goal_keywords(blocker_text)
    if goal_keywords:
        logger.info(
            f"  Context-aware drain enabled — blocker={blocker_text!r}, "
            f"goal_keywords={goal_keywords!r}"
        )
    transactions, early_exit = _execute_plan(
        plan, goal_keywords=goal_keywords, blocker_text=blocker_text,
    )
    import time as _time
    _time.sleep(2.0)

    # Per-step blocker re-check (2026-05-12) — if mid-plan the blocker
    # vanished, _execute_plan signals early_exit and we treat the plan as
    # successful regardless of transaction count.  This stops the sticky-
    # belief escalation loop: the bot used to keep executing a multi-step
    # recovery plan even after the original blocker had already resolved
    # (e.g. via game-side state change), wasting minutes on no-op taps.
    if early_exit:
        logger.info(
            f"  Plan {plan.scenario_id!r} exited early — blocker resolved "
            f"mid-plan; saving as learned recovery (transactions={transactions})"
        )
        _save_as_learned_recovery(plan)
        return True

    # Plan-completeness rule (CLAUDE.md → "Claude-generated plans are
    # recommendations, not truth"): a plan that produced zero transactions
    # is invalid.  Try one round of Claude revamp before giving up.
    if transactions == 0:
        logger.warning(
            f"  Plan {plan.scenario_id!r} produced no transactions — "
            "asking Claude to revamp from current screen state"
        )
        revised = _revamp_plan_with_claude(plan, ocr_text=ocr_text)
        if revised is not None:
            transactions, early_exit = _execute_plan(
                revised, goal_keywords=goal_keywords, blocker_text=blocker_text,
            )
            _time.sleep(2.0)
            plan = revised  # use the revised plan id when saving below
            if early_exit:
                logger.info(
                    f"  Revised plan {plan.scenario_id!r} exited early — "
                    "blocker resolved mid-plan"
                )
                _save_as_learned_recovery(plan)
                return True

    # Plan-completeness save-gate: refuse to persist a plan that completed
    # without producing any transaction.  Caching a transaction-less plan
    # reproduces the broken-recovery pattern (the harbour Recruit-Crew
    # failure of 2026-05-04 was exactly this).
    if transactions == 0:
        logger.warning(
            f"  Plan {plan.scenario_id!r} still produced no transactions "
            "after revamp — NOT saving as learned recovery, returning "
            "False so caller can fall back further"
        )
        return False

    logger.info(
        f"  Claude resolution executed ({transactions} transaction(s)) — "
        f"saving learned recovery: {plan.scenario_id!r}"
    )
    _save_as_learned_recovery(plan)
    return True


def _revamp_plan_with_claude(
    failed_plan, ocr_text: str = "",
):
    """Ask Claude for a revised plan when the original produced no
    transactions.  Sends the failed plan + a fresh screenshot + a prompt
    asking to revise based on the actual screen state.

    Returns a revised EscalationPlan or None if the model is unavailable
    / returned an unparseable response / declined to revise.

    Per CLAUDE.md plan-completeness rule: only one revamp per resolution
    round to avoid loops.  Callers must NOT recurse on this function.
    """
    import base64, io, json as _json, os
    try:
        import anthropic
    except ImportError:
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    from capture.adb_capture import capture_screen
    frame = capture_screen()

    # OmniParser element table (2026-05-12) — same shape as
    # _resolve_blocker_with_reasoning so Claude can pick a real label from
    # the actual current screen instead of inventing one.
    element_table, _omni_elements = _build_omniparser_table(frame)

    thumb = frame.copy()
    thumb.thumbnail((1200, 540))
    buf = io.BytesIO()
    thumb.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    failed_steps_summary = []
    for s in failed_plan.actions:
        if s.type == "find_and_tap":
            failed_steps_summary.append(f"find_and_tap('{s.label}')")
        elif s.type == "tap":
            failed_steps_summary.append(f"tap({s.x},{s.y})")
        elif s.type == "press_back":
            failed_steps_summary.append("press_back")
        else:
            failed_steps_summary.append(s.type)

    prompt = f"""\
Your previous plan to resolve the bot's blocker did not produce any positive
transaction (e.g. recruit / buy / sell / confirm / set sail).  Either a
labelled button was missing on the post-tap screen, or every step was
navigation-only.

Your previous plan:
  {failed_steps_summary}

A local UI detector (OmniParser) has identified the elements currently on screen:

{element_table or "(OmniParser unavailable — infer from image only)"}

Current OCR text from screen: {ocr_text!r}

Look at THIS screenshot AND the element table above — the actual current
screen state — and return a REVISED action sequence that produces at least
one positive transaction.

── Choosing the action shape ─────────────────────────────────────────────────
STRONGLY PREFER "find_and_tap" with the EXACT label string from the table
above. The bot looks up the element at runtime and taps its precise centre —
this survives small UI shifts.  Fall back to "tap" with raw (x, y) only when
the element you want isn't in the table.

If a gold/yellow primary action button is in the table, that is almost
certainly the next correct tap — its label will be something like "Recruit",
"Confirm", "OK", "Depart Now", "Supply Departure".

Avoid info-only labels — anything that looks like "Fleet Crew Size 786/1,676"
or "Total Load Capacity 1,139/2,500" is a stats display, not an action button.
The previous plan's failure may have been triggered by tapping such an info
label as if it were a button.

Screen resolution: 2400×1080.  Image is at half resolution — when using
raw (x, y), report coordinates in ORIGINAL 2400×1080 space.

Return ONLY valid JSON:
{{
  "scenario_id": "short_snake_case_id",
  "reasoning": "<why these steps actually commit a transaction now>",
  "actions": [
    {{"type": "find_and_tap", "label": "exact label from element table"}},
    {{"type": "tap", "x": 123, "y": 456}},
    {{"type": "press_back"}}
  ]
}}
"""

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = _json.loads(raw)
    except Exception as e:
        logger.warning(f"  _revamp_plan_with_claude failed: {e}")
        return None

    from brain.human_escalation import ActionStep, EscalationPlan
    actions = []
    for a in data.get("actions", []):
        actions.append(ActionStep(
            type    = a.get("type", "tap"),
            label   = a.get("label", ""),
            x       = a.get("x", 0),
            y       = a.get("y", 0),
            x2      = a.get("x2", 0),
            y2      = a.get("y2", 0),
            seconds = a.get("seconds", 1.0),
            x_min   = a.get("x_min", -1),
            y_min   = a.get("y_min", -1),
            x_max   = a.get("x_max", -1),
            y_max   = a.get("y_max", -1),
        ))
    if not actions:
        return None

    revised = EscalationPlan(
        scenario_id        = data.get("scenario_id", failed_plan.scenario_id + "_revised"),
        category           = "flow_step",
        description        = data.get("reasoning", "Revised after no transaction"),
        actions            = actions,
        detection_keywords = failed_plan.detection_keywords,
    )
    logger.info(
        f"  [revamp] Claude returned revised plan {revised.scenario_id!r} "
        f"with {len(actions)} step(s)"
    )
    return revised


def _depart_from_harbour() -> bool:
    """
    Tap the departure button and wait until the sea view is confirmed.

    Assumes _ensure_fleet_ready() has already been called — blocking conditions
    should have been resolved before reaching here.  If a blocker is still
    detected (race condition or new one), escalates immediately rather than
    attempting inline resolution.
    """
    logger.info("Departing from Harbour…")
    # Phase 3.3: invalidate Moondream family cache — the bot is about to
    # transition from in_town to at_sea.  The classifier's early-arbiter
    # gate would otherwise serve the stale "in_town" verdict for ~10 min.
    try:
        from brain import moondream_family_cache as _mfc
        _mfc.invalidate("set_sail")
    except Exception as e:
        logger.debug(f"[depart] moondream cache invalidate failed: {e}")
    frame = capture_screen()
    result = _tap_depart_button(frame)
    if isinstance(result, dict):
        # Blocking signal detected despite pre-check — escalate immediately
        sig = result
        logger.warning(
            f"  Departure blocked ({sig['text']!r}) despite fleet readiness check — escalating"
        )
        from brain.recovery import recover_to_port_overworld
        recover_to_port_overworld()
        return False
    if result == "not_found":
        # Not in harbour — try to navigate there first
        logger.warning("  Depart button not found on first try — re-navigating to harbour")
        if not _navigate_to_harbour():
            logger.error("  Could not navigate to harbour")
            return False
        frame = capture_screen()
        result = _tap_depart_button(frame)
        if isinstance(result, dict):
            logger.warning(f"  Departure still blocked after re-navigating ({result['text']!r}) — escalating")
            from brain.recovery import recover_to_port_overworld
            recover_to_port_overworld()
            return False
    time.sleep(2.0)

    # Post-tap notes:
    # "Supply Departure" and "Depart Now" (full supply) depart immediately.
    # "Auto Supply" (supply prep dialog) resupplies then departs — may take a
    # few extra seconds for the supply animation before the loading screen.
    # All three paths eventually reach the sea loading screen; the wait loop below handles it.

    # Wait until confirmed on sea view.
    # IMPORTANT: port overworld also has has_home=False, so chrome alone is
    # insufficient — use where_am_i() to distinguish sea from overworld.
    logger.info("Waiting through departure loading screens…")
    deadline = time.time() + 90.0
    in_loading = False

    while time.time() < deadline:
        time.sleep(2.0)
        frame = capture_screen()

        if _is_loading_screen(frame):
            if not in_loading:
                logger.info("  Loading screen active — waiting…")
            in_loading = True
            continue

        in_loading = False
        from vision.chrome_detector import get_chrome_detector
        chrome = get_chrome_detector().detect(frame)

        if chrome.has_home:
            # Still inside a building — verify it's actually the harbour before retrying
            loc = where_am_i()
            if loc.get("location") != "building":
                # Shouldn't happen but handle it
                break
            result = _tap_depart_button(frame)
            if result == "not_found":
                # Not in harbour — navigate back to it first
                logger.warning("  Not in harbour (depart button absent) — re-navigating to harbour")
                if not _navigate_to_harbour():
                    logger.error("  Could not re-navigate to harbour")
                    return False
            time.sleep(2.0)
            continue

        # has_home=False — could be sea OR port overworld.
        # Call where_am_i() once to confirm; fires only when has_home=False.
        loc = where_am_i(frame)
        if loc["location"] in ("sea", "sea_cinematic"):
            logger.info("On sea view — departure confirmed")
            # The remembered settlement was the fleet's LOCATION; once at sea it is only the
            # voyage's origin. Dropping it here beats guessing an expiry — the moment the bot
            # does the thing that makes a fact untrue is the moment it knows for certain.
            from memory.observed_facts import forget
            forget("settlement")
            return True
        if loc["location"] == "port_overworld":
            logger.warning(f"  On port overworld ({loc.get('port')!r}) — "
                           f"back pressed too far; aborting departure")
            return False
        # location="building" with has_home=False means a MODAL dialog
        # is overlaid on the harbour — typically the Supply Departure
        # confirmation ("Confirm purchase of supplies for X ducats").
        # The yellow OK button is the canonical positive-confirm; tap
        # it to advance.  Harmless when no yellow button is present
        # (loading transition, etc).
        if loc["location"] == "building":
            yb = _find_yellow_button(frame)
            if yb is not None:
                logger.info(
                    f"  Modal dialog detected on harbour view — "
                    f"tapping yellow confirm button @ {yb}"
                )
                tap(*yb)
                time.sleep(2.0)
                continue
        # loading / unknown — keep waiting

    logger.warning("Did not reach sea view within 90s")
    return False


# ── Step 4: dismiss system notice (conditional) ───────────────────────────────

def _handle_post_departure_sea(frame: Image.Image) -> Image.Image:
    """
    Dismiss all blocking popups visible after departure (or anywhere).
    Delegates to perceive.dismiss_interruptors — state-agnostic.
    Returns the current frame (refreshed after all dismissals).
    """
    from brain.perceive import dismiss_interruptors
    return dismiss_interruptors(frame)


# ── Steps 5-7: open world map, find destination, tap Go to City ───────────────

def _is_on_world_map(frame: Image.Image) -> bool:
    """
    True when the world map is open.
    Requires BOTH:
      - No sea-HUD keywords ("days of sailing", "day 1/2/3")
      - At least one port-label keyword
    The sea view right panel also shows nearby port names, so port keywords alone
    are not sufficient — we must also confirm the sailing HUD is gone.
    """
    tokens = _ocr_frame(frame, min_conf=0.3)
    full = " ".join(t.lower() for t, _, _, _ in tokens)
    has_sea_hud         = any(kw in full for kw in _SEA_HUD_TOKENS)
    has_world_map_title = fuzzy_contains(full, "world map")
    logger.info(f"  World map check: has_sea_hud={has_sea_hud} "
                f"has_world_map_title={has_world_map_title} text={full[:100]!r}")
    return has_world_map_title and not has_sea_hud


def _clear_sea_popups(frame: Image.Image) -> Image.Image:
    """
    Dismiss any popups before tapping the mini-map.
    Delegates to perceive.dismiss_interruptors — state-agnostic.
    Note: "dangerous waters" and "pass" are normal sea-view UI elements, not popups.
    """
    from brain.perceive import dismiss_interruptors
    return dismiss_interruptors(frame)


def _confirm_at_sea_moondream(frame: Image.Image) -> Optional[bool]:
    """
    Ask Moondream (or best available local vision model) whether the frame shows
    the open ocean sailing view.

    Returns True if confirmed at sea, False if confirmed NOT at sea (port, building,
    dialog, etc.), or None if the model is unavailable.

    This is intentionally a simple yes/no question so even the lightweight Moondream
    model can answer reliably.  Port scenes also show ships and water, but always have
    buildings, land structures, or NPCs visible — the prompt captures this distinction.

    Result is cached per (id(frame), "at_sea") via vision.moondream_cache so
    callers that ask the same question on the same frame share inference cost.
    """
    from vision.moondream_cache import ask_cached
    return ask_cached(frame, "at_sea", lambda: _confirm_at_sea_moondream_uncached(frame))


def _confirm_at_sea_moondream_uncached(frame: Image.Image) -> Optional[bool]:
    """Moondream sea-check inference — see _confirm_at_sea_moondream for the
    cached entry point.  Kept as a separate function so tests can exercise
    the inference path without engaging the cache."""
    from vision.local_vision import get_vision
    vision = get_vision()
    if not vision.check_available():
        return None

    thumb = frame.copy()
    thumb.thumbnail((800, 400))
    # User-confirmed: 'sailing' covers open ocean AND river / coastal
    # passages (where land/banks may occupy a large portion of the
    # screen).  Both are valid sailing states and answer 'yes'.  The
    # earlier 'open ocean only' phrasing wrongly said 'no' to river
    # passages.
    #
    # The prompt enumerates non-sailing UI states explicitly because
    # Moondream tended to answer 'yes' for the harbor's Recruit Crew
    # screen (lists ships with crew counts, looks ship-related —
    # May-1 16:46 incident).  Any UI panel = no.
    answer = vision.ask(
        "Screenshot from the mobile game Uncharted Waters Origin. "
        "I need to know if the player is currently SAILING — that means "
        "a ship is travelling on a body of water.  This includes:\n"
        "  - open ocean (vast water, minimal land)\n"
        "  - coastal sailing (water with shoreline visible)\n"
        "  - river sailing (water flanked by river banks)\n\n"
        "Answer 'yes' if a ship is visibly travelling on water, even if "
        "banks / coastline / islands occupy part of the view.\n\n"
        "Answer 'no' if you see ANY of:\n"
        "  - a rectangular dialog box, popup, or modal\n"
        "  - a menu panel with buttons labeled OK, Cancel, Confirm, Recruit, etc.\n"
        "  - a list of ships or items with status labels (crew counts, prices, etc.)\n"
        "  - a left/right panel split (menu on one side, content on the other)\n"
        "  - an NPC character with a speech bubble in a dialog box\n"
        "  - the player character standing on land / on a dock\n"
        "  - port buildings or city scenery in the foreground\n\n"
        "Answer ONLY 'yes' or 'no'.",
        frame=thumb,
    )
    if not answer:
        return None
    result = answer.lower().strip().startswith("y")
    logger.info(f"  Moondream sea-check → {'at sea' if result else 'NOT at sea'}  (raw: {answer[:40]!r})")
    return result


def _confirm_in_town_moondream(frame: Image.Image) -> Optional[bool]:
    """
    Ask Moondream whether the frame shows a port-overworld (town) view.

    Returns True if the player character is standing in a port town with
    buildings and NPCs around them, False if not (sea / building interior /
    real modal dialog), None if the model is unavailable.

    Used by perceive's Phase-5b vision-led fallback: when rule-based
    classification fails (port-name OCR returns nothing because an in-world
    overlay obscures it, or chrome.has_right_panel template misses), this
    positive 'is in town?' signal recovers the classification before the
    bot falls through to the harmful 'unknown' path that triggered
    handle_unknown_blocking on transient overlays.

    Empirical reliability (May-2 testing against labeled frames):
      - 0002 (expanded plate + NPC bubble) → YES ✓
      - 0003 (expanded plate, no bubble)   → YES ✓
      - 0010 (small plates + NPC bubbles)  → YES ✓

    Result is cached per (id(frame), "in_town") via vision.moondream_cache.
    """
    from vision.moondream_cache import ask_cached
    return ask_cached(frame, "in_town", lambda: _confirm_in_town_moondream_uncached(frame))


def _confirm_in_town_moondream_uncached(frame: Image.Image) -> Optional[bool]:
    """Moondream town-check inference — see _confirm_in_town_moondream for
    the cached entry point."""
    from vision.local_vision import get_vision
    vision = get_vision()
    if not vision.check_available():
        return None

    thumb = frame.copy()
    thumb.thumbnail((800, 400))
    answer = vision.ask(
        "Screenshot from the mobile game Uncharted Waters Origin.\n\n"
        "Is the player character standing inside a port town, with port "
        "buildings (houses, docks, market stalls) visible around them and "
        "the character free to walk between buildings?  This is sometimes "
        "called the 'port overworld' view.\n\n"
        "It IS a port town if you see:\n"
        "  - the player character standing in a town environment\n"
        "  - multiple buildings visible in the scene\n"
        "  - NPCs walking around\n"
        "  - the world camera angled down into the town (top-down isometric)\n\n"
        "It is NOT a port town if you see:\n"
        "  - the open ocean with a single ship sailing across it\n"
        "  - the inside of a building (UI panels, menu items, sub-screens)\n"
        "  - a full-screen modal dialog with OK/Cancel buttons\n"
        "  - a loading screen\n\n"
        "Note: name plates above buildings (small white pills with the "
        "building name) and NPC speech bubbles (white quoted-text bubbles "
        "above NPCs) are NORMAL parts of the port town view — their "
        "presence does NOT mean it's a dialog.\n\n"
        "Answer ONLY 'yes' (port town visible) or 'no' (something else).",
        frame=thumb,
    )
    if not answer:
        return None
    result = answer.lower().strip().startswith("y")
    logger.info(f"  Moondream town-check → {'in town' if result else 'NOT in town'}  (raw: {answer[:40]!r})")
    return result


def _ensure_active_sea_view(frame: Optional[Image.Image] = None) -> Optional[Image.Image]:
    """
    Ensure the sailing HUD is visible before tapping the mini-map.

    Returns the current frame if we are confirmed at sea (HUD visible or cinematic
    successfully woken).  Returns None when we are NOT at sea — caller should
    call perceive() to reorient rather than continuing the sailing flow.

    Decision tree:
      1. Sea HUD present → return frame (nothing to do)
      2. Loading screen → return frame (HUD will appear when load completes)
      3. dismiss_interruptors → recheck HUD
      4. Moondream confirmation: not sea → return None (caller re-perceives)
      5. Confirmed sea (cinematic): double-tap to wake, poll for HUD return
      6. Moondream unavailable + cinematic: proceed heuristically, warn if still stuck
    """
    if frame is None:
        frame = capture_screen()
    tokens = _ocr_frame(frame, min_conf=0.3)
    full = " ".join(t.lower() for t, _, _, _ in tokens)
    logger.info(f"  Sea view check text: {full[:80]!r}")

    if _is_loading_screen(frame):
        logger.info("  Sea view: loading screen — no wake needed")
        return frame

    sea_hud_present = any(fuzzy_contains(full, kw) for kw in _SEA_HUD_TOKENS)
    if sea_hud_present:
        logger.info("  Sea view HUD active — no wake needed")
        return frame

    # Step 3: dismiss any known interruptors (full frame OCR — catches top-of-screen dialogs)
    from brain.perceive import dismiss_interruptors
    frame = dismiss_interruptors(frame)
    tokens2 = _ocr_frame(frame, min_conf=0.3)
    full2 = " ".join(t.lower() for t, _, _, _ in tokens2)
    if any(fuzzy_contains(full2, kw) for kw in _SEA_HUD_TOKENS):
        logger.info("  Sea view restored after interruptor dismiss")
        return frame

    # Step 4: HUD still absent — ask Moondream whether this is actually the sea
    at_sea = _confirm_at_sea_moondream(frame)
    if at_sea is False:
        logger.warning(
            "  Moondream: this is NOT the open sea — returning None so caller can re-perceive"
        )
        return None  # caller must call perceive() and handle the actual state

    if at_sea is None:
        logger.debug("  Moondream unavailable — proceeding heuristically")

    # Step 5: confirmed sea (or unknown) — treat as idle cinematic, wake with tap
    logger.info("  Idle cinematic detected (no sailing HUD) — double-tapping centre to wake")
    cx, cy = frame.width // 2, frame.height // 2
    tap(cx, cy)
    time.sleep(0.5)
    tap(cx, cy)

    deadline = time.time() + 10.0
    while time.time() < deadline:
        time.sleep(2.0)
        frame = capture_screen()
        if not _is_idle_cinematic(frame):
            logger.info("  Sea view restored")
            return frame

    # Still stuck — one last Moondream check before giving up
    at_sea_final = _confirm_at_sea_moondream(frame)
    if at_sea_final is False:
        logger.warning("  Moondream post-wake: still NOT at sea — returning None")
        return None

    loc = where_am_i(frame)
    logger.warning(
        f"  Could not restore sea view — actual location: {loc['location']!r} — {loc['detail']}"
    )
    return frame


def _read_sea_speed(frame: Image.Image) -> Optional[float]:
    """
    Read the ship's current speed from the sea HUD.

    The speed indicator sits below the wind compass in the bottom-left area of
    the sea view.  It shows a decimal like "0.0" or "12.5" followed by "kn"
    (OCR sometimes reads "kn" as "ln" or "k n").

    Returns the speed as a float, or None if it cannot be read.
    Speed == 0.0 means the ship is not moving (stalled at a waypoint or anchor).
    """
    import re
    # Scan bottom-left quarter — wind/speed HUD is in that corner
    h, w = frame.height, frame.width
    region = frame.crop((0, h * 2 // 3, w // 2, h))
    tokens = _ocr_frame(region, min_conf=0.25)

    full_text = " ".join(t.lower() for t, _, _, _ in tokens)
    logger.debug(f"  Sea speed OCR: {full_text!r}")

    # Pattern: decimal number followed by kn / ln / k n (unit)
    m = re.search(r'(\d{1,2}\.\d)\s*(?:kn|ln|k\s*n)', full_text)
    if m:
        return float(m.group(1))

    # Fallback: any standalone small decimal that looks like a speed
    for text, _, _, _ in tokens:
        m2 = re.fullmatch(r'(\d{1,2}\.\d)', text.strip())
        if m2:
            val = float(m2.group(1))
            if val <= 30.0:   # realistic ship speed range
                return val

    return None


# States that are PLACES the fleet occupies, not screens laid over one. Leaving one costs
# position, so a primitive may never do it to satisfy its own state test.
_SETTLEMENT_STATES = ("village", "port_overworld")


def _looks_like_a_village(frame) -> bool:
    """True when the frame carries a VILLAGE's left menu, whatever the classifier said.

    barter + gifting together appear on no port screen, and the menu is present on every
    village sub-screen — including the barter panel, which the family classifier has called
    both 'sea' and 'port_overworld' on different frames.
    """
    try:
        from vision.omniparser import parse_fast_cached
        from vision.region_detectors.left_menu import detect_left_menu
        menu = detect_left_menu(list(parse_fast_cached(frame)), frame.width, frame.height)
        labels = {l.strip().lower() for l in (menu.labels() if menu else [])}
        return {"barter", "gifting"} <= labels
    except Exception as exc:
        logger.debug(f"[open_world_map] village check failed: {exc}")
        return False


_PORT_WORLD_MAP_GLOBE = (2227, 361)   # port minimap GLOBE icon → world map (proven set-sail path)


def _sea_minimap_center() -> tuple:
    """Centre of the live sea MINIMAP_CROP — the SAME calibrated region the nav reads every tick,
    so a tap follows the minimap (notch-aware) instead of a stale hardcoded coord that drifts onto
    the hamburger."""
    try:
        from brain.ai_nav.vision_input import MINIMAP_CROP as _MC
    except Exception:
        _MC = (1984, 205, 2379, 395)
    x0, y0, x1, y1 = _MC
    return ((x0 + x1) // 2, (y0 + y1) // 2)


# How many patient re-perceives to spend on an unrecognised screen before concluding it is
# not a passing overlay and trying to leave it. Chatter bubbles clear within a couple of
# re-perceives; an open panel never does.
_WAIT_BEFORE_EXIT_ATTEMPT = 3


def open_world_map(context: Optional[str] = None) -> bool:
    """ONE canonical way to open the world map — from PORT or SEA.

      PORT: the minimap shows a GLOBE icon → tap it (_PORT_WORLD_MAP_GLOBE).
      SEA:  there is NO globe — tap the MINIMAP itself.  Use the CENTRE of the calibrated
            MINIMAP_CROP (the region the nav reads every tick) so the tap follows the minimap and
            never drifts onto the hamburger.  The old sea path hardcoded (2240, 300–420) which
            landed on the hamburger and opened the MAIN MENU instead — looping the whole voyage
            (2026-08-19).

    Verifies with _is_on_world_map(); retries a few times (waking an idle cinematic first).
    `context` may be pre-supplied ('sea'/'sea_cinematic'/'port_overworld'); otherwise where_am_i()
    derives it each attempt.  This replaces _open_world_map_from_sea and the duplicate port opens."""
    logger.info("[open_world_map] opening world map…")
    waited_out = 0
    for attempt in range(10):
        frame = capture_screen()
        # The game drops into a "Slide up to unlock" standby whenever it sits idle, and it
        # does so quickly between mission legs. Nothing downstream can act through it —
        # perceive recognises the screen but only records a learning note — so a run that
        # started against the lock made zero taps and simply timed out (live 2026-08-21).
        # Wake first; it is a no-op when the game is already awake.
        try:
            from actions.route_execution import _wake_if_locked
            if _wake_if_locked(frame):
                logger.info("[open_world_map] game was on the standby lock — woke it")
                frame = capture_screen()
        except Exception as exc:
            logger.debug(f"[open_world_map] wake check skipped: {exc}")
        if _is_on_world_map(frame):
            logger.info("[open_world_map] already open")
            return True
        loc = context or where_am_i(frame).get("location")

        if loc in ("sea", "sea_cinematic"):
            if loc == "sea_cinematic":
                frame = _ensure_active_sea_view(frame)
                if frame is None:
                    logger.warning("[open_world_map] not at sea (dialog/port) — aborting")
                    return False
            tx, ty = _sea_minimap_center()
            logger.info(f"[open_world_map] SEA — tap minimap centre @ ({tx},{ty})")
        elif loc == "port_overworld":
            # CORROBORATE BEFORE TAPPING A CALIBRATED POINT. The globe coordinate is only
            # meaningful on a real port overworld; on anything else it is a blind tap into
            # whatever happens to be there. Live 2026-08-23 the village BARTER panel was
            # classified 'port_overworld', this branch tapped (2227,361), hit a
            # Check-Barter-Effect / Village-Influence control, and the loop then spun
            # re-perceiving a screen it had opened itself.
            #
            # A village's left menu — barter / explore / gifting / loot / recruit crew —
            # appears on every village screen and on no port screen, so it is the cheap
            # disproof. (CLAUDE.md: "if you are about to write a number that means where on
            # the screen, find the element instead" — until the globe itself is detected,
            # this at least refuses to tap it on the wrong screen.)
            if _looks_like_a_village(frame):
                logger.warning("[open_world_map] classified 'port_overworld' but the left "
                               "menu is a VILLAGE's — refusing to tap the port globe here; "
                               "reporting so the caller can decide about leaving")
                return False
            tx, ty = _PORT_WORLD_MAP_GLOBE
            logger.info(f"[open_world_map] PORT — tap globe @ ({tx},{ty})")
        else:
            # Not a state we can open from (transient / brief false port-overworld right
            # after departure) — wait and re-perceive rather than blind-tapping.
            #
            # Patience matters here: a busy port carries ambient NPC chatter bubbles, and
            # the family CNN calls those 'transient', which makes perceive gate the
            # overworld verdict to 'unknown' (see brain/perceive _classify_nav_state).
            # The bubbles drift away on their own, so re-perceiving across a couple of
            # minutes lands on a clear frame — where 4 quick tries all hit chatter and the
            # whole mission aborted at the first step (live 2026-08-21).
            # Being INSIDE something is not transient — no amount of waiting turns a
            # Market into an overworld. Walk out, then re-perceive. Live 2026-08-21: a
            # mission started while the fleet was on the Market's Purchase screen spent
            # its whole attempt budget re-perceiving 'sub_menu' and never once tried to
            # leave. Waiting is right for chatter bubbles; it is useless for a building.
            if loc in ("building", "sub_menu", "market"):
                logger.info(f"[open_world_map] loc={loc!r} — inside a screen, exiting to "
                            f"the overworld first (attempt {attempt + 1}/10)")
                exit_to_overworld()
                context = None
                continue

            # Patience is right for a genuinely TRANSIENT overlay (a chatter bubble drifts
            # away on its own), but it is useless against a screen that is simply OPEN. The
            # family CNN cannot always tell them apart: it labelled the fleet/cargo panel
            # 'transient' → 'unknown', and this branch then re-perceived it ten times and
            # gave up, ending a run with ZERO actions (live 2026-08-21).
            #
            # So: stay patient for a few rounds, then stop waiting and try to LEAVE. Bubbles
            # will have cleared by then; a panel will not have. Exiting is harmless if we
            # were already on an overworld.
            # A PLACE is not a screen to escape. Leaving an unrecognised panel is mechanics
            # — the same answer whatever the bot is doing — but leaving a SETTLEMENT spends
            # position the task may have sailed for. Live 2026-08-22 the fleet stood in
            # Melanesian Village, the mission's own destination with the materials aboard,
            # and this loop pressed Back three times until it was at sea.
            #
            # So: report that the world map cannot be opened from here and let the caller
            # decide whether leaving is acceptable (from a village it IS the only route to
            # the map — but that is the task's call, not this primitive's).
            # See docs/one_loop_task_drives_state.md — "the state machine owns the HOW, the
            # task owns the WHETHER".
            if loc in _SETTLEMENT_STATES:
                logger.warning(f"[open_world_map] the fleet is AT {loc!r} — cannot open the "
                               "world map from here without leaving. Reporting instead of "
                               "forcing; the caller decides whether to give up the position.")
                return False

            waited_out += 1
            if waited_out >= _WAIT_BEFORE_EXIT_ATTEMPT:
                # An unrecognised screen that is simply OPEN never clears by waiting — the
                # fleet/cargo panel was labelled 'transient' → 'unknown' and re-perceived ten
                # times, ending a run with ZERO actions (live 2026-08-21). Leaving a panel
                # costs nothing, so this escalation stays.
                logger.info(f"[open_world_map] loc={loc!r} has persisted {waited_out} "
                            "re-perceives — not transient, trying to exit to the overworld")
                exit_to_overworld()
                waited_out = 0
                context = None
                continue

            logger.info(f"[open_world_map] loc={loc!r} not port/sea — waiting to "
                        f"re-perceive (attempt {attempt + 1}/10)")
            time.sleep(random.uniform(3.0, 6.0))
            context = None
            continue

        tap(tx, ty)
        for _ in range(10):
            time.sleep(1.0)
            f2 = capture_screen()
            if _is_on_world_map(f2) or _world_map_port_labels_visible(f2):
                logger.info("[open_world_map] world map opened")
                return True

        # Accidental main-menu (mis-tap) — close it and retry.
        full = " ".join(t.lower() for t, _, _, _ in _ocr_frame(capture_screen(), min_conf=0.3))
        if (fuzzy_contains(full, "company overview")
                or ("fleet" in full and "storage" in full)
                or ("auction" in full and "guild" in full)):
            logger.info("[open_world_map] main menu opened accidentally — pressing back")
            press_back()
            time.sleep(1.5)
        context = None                       # re-derive context next attempt

    logger.warning("[open_world_map] failed to open world map after retries")
    return False


def _open_world_map_from_sea(_frame: Image.Image = None) -> bool:
    """Deprecated shim → the unified open_world_map().  Auto-detects port vs sea, so it also
    handles the 'actually still at port' case the old sea-only version aborted on."""
    return open_world_map()


def _world_map_port_labels_visible(frame: Image.Image) -> bool:
    """
    Check whether port labels are visible in the LEFT part of the screen.
    On the sea view, nearby port names appear only in the right panel (x > 1800).
    On the world map, port labels are scattered across the full map area.
    Finding any known port label at x < 1800 strongly implies the world map is open.
    """
    import numpy as np
    from vision.ocr import _get_reader

    # Only scan the left 75% of the frame (map area, not sea right panel)
    map_area = frame.crop((0, 40, int(frame.width * 0.75), frame.height - 40))
    raw = _get_reader().readtext(np.array(map_area), detail=1)

    known_ports = (
        "diu", "goa", "hormuz", "calicut", "ceylon", "colombo", "aceh",
        "malacca", "muscat", "masulipatnam", "lisbon", "amsterdam", "venice",
        "alexandria", "aden", "pasay", "kozhikode", "pegu", "lopburi",
        "oman", "basra", "mombasa", "zanzibar", "mogadishu",
    )
    found = []
    for _, text, conf in raw:
        if conf >= 0.25 and any(p in text.lower() for p in known_ports):
            found.append(f"{text!r}({conf:.2f})")

    if found:
        logger.info(f"  World map port labels in left area: {', '.join(found)}")
        return True

    # Also log everything OCR read in that area for diagnosis
    all_text = [(text, conf) for _, text, conf in raw if conf >= 0.25]
    logger.info(f"  Left-area OCR ({len(all_text)} tokens): "
                + ", ".join(f"{t!r}({c:.2f})" for t, c in all_text))
    return False


# Port name aliases: maps the bot's canonical name → list of in-game display names.
# The game uses local-language names that differ from English (Lisboa, Las Palmas…).
# Also includes common OCR garbles of high-traffic ports.
_PORT_ALIASES: dict[str, list[str]] = {
    "lisbon":          ["lisboa", "lisbon"],
    "seville":         ["sevilla", "seville"],
    "canary islands":  ["las palmas", "la palma", "tenerife", "gran canaria"],
    "las palmas":      ["las palmas", "canary islands"],
    "london":          ["london", "lcndon"],   # OCR typo seen in logs
    "ceylon":          ["ceylon", "colombo"],
    "calicut":         ["calicut", "kozhikode"],
    "hormuz":          ["hormuz", "ormuz"],
    "havana":          ["havana", "la habana"],
    "port royal":      ["port royal"],
    "istanbul":        ["istanbul", "constantinople"],
    "beijing":         ["beijing", "peking"],
    "canton":          ["canton", "guangzhou"],
    "nagasaki":        ["nagasaki"],
    "aceh":            ["aceh", "banda aceh"],
    "malacca":         ["malacca", "melaka"],
    "diu":             ["diu"],
    "goa":             ["goa"],
    "aden":            ["aden"],
    "alexandria":      ["alexandria", "alexand"],  # OCR truncation seen in logs
}


def _ascii_search_prefix(destination: str, max_len: int = 4) -> str:
    """The prefix to TYPE into the port search — ASCII-only, so accents can't break it.

    The catalogue spells ports as the game does ('Malé'), while callers arrive with the
    accent-stripped form ('Male', which is what `catalogue_coords()` produces). Typing
    'Male' finds nothing, because the game's fourth character is 'é' — the very letter
    that differs (live 2026-08-21: the gather leg could not reach its nearest supplier).
    Typing 'Mal' matches both spellings, so truncate at the first non-ASCII character of
    the CANONICAL name and let the shorter prefix filter the list."""
    canonical = destination
    try:
        from vision.world_map_parser import load_port_catalogue
        from actions.world_map_nav import WorldMapNavigator
        folded = WorldMapNavigator._fold(destination)
        for key, rec in load_port_catalogue().items():
            if WorldMapNavigator._fold(key) == folded:
                canonical = (rec.get("name") if isinstance(rec, dict) else None) or key
                break
    except Exception as exc:
        logger.debug(f"[port-search] canonical lookup failed: {exc}")
    ascii_run = ""
    for ch in canonical:
        if ord(ch) > 127:
            break
        ascii_run += ch
    prefix = ascii_run[:max_len]
    if len(prefix) < 2:
        # The name is accented too early to give a usable ASCII run ('Málaga' → 'M'), so
        # type the accent-FOLDED spelling instead. It is ASCII by construction, and ADB's
        # `input text` cannot reliably send non-ASCII characters anyway.
        from actions.world_map_nav import WorldMapNavigator
        prefix = WorldMapNavigator._fold(canonical or destination)[:max_len]
    if prefix.lower() != destination[:len(prefix)].lower():
        logger.info(f"[port-search] typing ASCII-safe prefix {prefix!r} for "
                    f"{destination!r} (canonical {canonical!r})")
    return prefix


def _port_names_to_search(destination: str) -> list[str]:
    """Return all name variants to search for (canonical + aliases)."""
    dest_lower = destination.lower().strip()
    # Collect from aliases table
    if dest_lower in _PORT_ALIASES:
        names = list(_PORT_ALIASES[dest_lower])
    else:
        names = [dest_lower]
        # Also check if dest matches any alias value
        for canon, variants in _PORT_ALIASES.items():
            if dest_lower in variants and canon not in names:
                names.append(canon)
    # Always include the original
    if dest_lower not in names:
        names.insert(0, dest_lower)
    return names


# Verb/noun variants for the world-map destination action button.  City
# panels show "Go to City"; village panels (Explore tab) show "Move to
# Village".  Both buttons sit in the same screen location; OCR may emit
# the label as one merged token or two split tokens on the same row.
_DESTINATION_BUTTON_VERBS = ("go to", "move to")
_DESTINATION_BUTTON_NOUNS = ("city", "village")


# Minimum y-coordinate for the destination-button match.  The "Go to
# City" / "Move to Village" button is always rendered as a large action
# button at the bottom-centre of the world map (y ≈ 970 on a 1080-tall
# screen).  Tokens above this threshold are panel labels / info text
# (e.g. the "Berber Village" title inside the right-side Village Info
# panel) and must be rejected — tapping them does nothing useful.
# 2026-05-21 origin: bot read "Move to Village" inside the info panel
# at (2084, 596), tapped there, and the panel stayed up.
_DESTINATION_BUTTON_MIN_Y = 850


def _find_destination_button(
    tokens: list,
    *,
    label: str = "",
    min_y: int = _DESTINATION_BUTTON_MIN_Y,
) -> Optional[Tuple[int, int]]:
    """Locate the world-map destination action button (Go to City /
    Move to Village) inside an OCR token list.

    Two-pass match (both passes filter by *min_y* — the button only ever
    sits in the bottom action-bar of the world map; anything higher is
    inside the info panel):
      1. Merged token containing "verb noun" (e.g. one OCR result spans
         the full button label).
      2. Verb token + noun token on the same row (|Δy| ≤ 100 px).  When
         multiple noun tokens are on the row, picks the one horizontally
         nearest the verb.

    Returns the centre-point pixel position of the button, or None if no
    matching pair was found.  *label* is a diagnostic tag inserted into
    log messages so callers can distinguish the source OCR pass.
    """
    # Pass 1: merged form ("Go to City" / "Move to Village").
    merged = [f"{v} {n}" for v in _DESTINATION_BUTTON_VERBS
                          for n in _DESTINATION_BUTTON_NOUNS]
    for text, conf, cx, cy in tokens:
        if cy < min_y:
            continue
        for phrase in merged:
            if fuzzy_contains(text, phrase):
                logger.info(
                    f"  [{label}] merged {phrase!r} @ ({cx},{cy}) "
                    f"conf={conf:.2f}"
                )
                return (cx, cy)

    # Pass 2: split verb + noun on the same row (both in bottom band).
    # Row tolerance is tight (~25 px) because same-line OCR bbox centroids
    # land within ~10-15 px of each other; anything larger means the
    # verb and noun come from different visual elements.
    # 2026-05-22 origin: the bot's Pass-2 logic paired 'Move to' at
    # (1210, 1014) — actual button — with 'Village' at (1313, 950) —
    # the 'Berber Village' label above the button — at the old 100 px
    # threshold.  The averaged centroid landed between them and missed
    # the button.  At 25 px, that mispairing no longer fires.
    _ROW_TOLERANCE_PX = 25
    for text, conf, cx, cy in tokens:
        if cy < min_y:
            continue
        if not any(fuzzy_contains(text, v) for v in _DESTINATION_BUTTON_VERBS):
            continue
        same_row = [
            (nxt_cx, nxt_cy, nxt_text, nxt_conf)
            for nxt_text, nxt_conf, nxt_cx, nxt_cy in tokens
            if nxt_cy >= min_y
            and any(n in nxt_text.lower() for n in _DESTINATION_BUTTON_NOUNS)
            and abs(nxt_cy - cy) <= _ROW_TOLERANCE_PX
        ]
        if not same_row:
            continue
        nxt_cx, nxt_cy, nxt_text, nxt_conf = min(
            same_row, key=lambda t: abs(t[0] - cx),
        )
        btn_x = (cx + nxt_cx) // 2
        btn_y = (cy + nxt_cy) // 2
        logger.info(
            f"  [{label}] split {text!r}@({cx},{cy}) conf={conf:.2f} + "
            f"{nxt_text!r}@({nxt_cx},{nxt_cy}) conf={nxt_conf:.2f} "
            f"→ button @ ({btn_x},{btn_y})"
        )
        return (btn_x, btn_y)
    return None


def _has_destination_button_text(tokens) -> bool:
    """Lightweight presence check: True iff a destination-button verb+noun
    pair (Go to City / Move to Village / Move to City) appears anywhere
    in *tokens*.  Used to distinguish "real destination panel" from a
    bare 'Move' sea-waypoint marker.
    """
    full = " ".join(t.lower() for t, *_ in tokens)
    return any(
        fuzzy_contains(full, f"{v} {n}")
        for v in _DESTINATION_BUTTON_VERBS
        for n in _DESTINATION_BUTTON_NOUNS
    )


_WORLD_MAP_TABS = ("port", "explore", "route", "trade")


def pan_to_village(
    name: str,
    from_port: Optional[str] = None,
    max_pans: int = 8,
) -> Optional[Tuple[int, int]]:
    """High-level village pan: switch to the Explore tab, then pan toward
    *name* using the village catalogue.

    Returns the tap position of the village label, or None if not found.
    Caller is responsible for tapping the returned position and handling
    the "Move to Village" panel (use _find_destination_button on the
    next frame to locate the button).

    This is a thin wrapper: the heavy lifting (scale calibration,
    odometry, dead-reckoning, stuck detection, safe-rect anchoring)
    lives in WorldMapNavigator.pan_to_port and is reused identically.
    The only village-specific bits are (a) switching tabs and (b)
    using the village catalogue.
    """
    # ── PRIMARY: anchor on the nearest catalogued PORT via the typed search ──
    # Blind-panning to a village over open water proved fragile (live 2026-08-20:
    # noisy water-tap lat/lon fixes around the Melanesian islands sent the camera
    # east across the Pacific seam into the Caribbean; gave up after 8 pans).
    # Ports are searchable by NAME — the typed search jumps the camera straight
    # to the port, no panning, no odometry.  Every village has a port within a
    # screen-width (Melanesian ← Samarai ~340 game units), so: search-select the
    # nearest port → the map centres on it → switch to the Explore tab (closes
    # the port panel, keeps the camera) → the village label is on screen →
    # the navigator's FIRST visible-label parse finds it.
    from actions.world_map_nav import make_village_navigator
    nav = make_village_navigator()
    v_info = nav._lookup_port(name)
    if v_info is not None:
        try:
            from vision.world_map_parser import load_port_catalogue
            ports = load_port_catalogue()
            vx, vy = v_info["x"], v_info["y"]
            anchor = min(
                (p for p in ports.values() if p.get("x") is not None),
                key=lambda p: (p["x"] - vx) ** 2 + (p["y"] - vy) ** 2,
            )
            logger.info(f"[pan_to_village] anchor port for {name!r}: "
                        f"{anchor['name']!r} @ ({anchor['x']},{anchor['y']}) "
                        f"(village @ ({vx},{vy}))")
            if _try_port_search(anchor["name"]) is not None:
                # Map is centred on the anchor (its info panel is open; do NOT
                # tap Go to City).  Switching tabs closes the panel.
                if select_world_map_tab("explore"):
                    pos = nav.pan_to_port(name, max_pans=3)
                    if pos is not None:
                        return pos
                    logger.warning(f"[pan_to_village] {name!r} not visible from "
                                   f"anchor {anchor['name']!r} — falling back to pan")
        except Exception as exc:
            logger.warning(f"[pan_to_village] anchor-port search failed: {exc} — "
                           "falling back to pan")

    # ── FALLBACK: the original pan path ─────────────────────────────────────
    # Pre-calibrate on the Port tab when no persisted scale is on disk.
    from actions.world_map_nav import _load_persisted_scale
    sx, sy = _load_persisted_scale()
    if not (sx and sy):
        logger.info(
            "[pan_to_village] no persisted scale on disk — "
            "running one-shot calibration on Port tab before switching to Explore"
        )
        _calibrate_scale_on_port_tab()

    if not select_world_map_tab("explore"):
        logger.warning(
            f"[pan_to_village] could not select Explore tab — "
            f"village {name!r} cannot be located on the Port tab"
        )
        return None

    return nav.pan_to_port(name, max_pans=max_pans, from_port=from_port)


def _calibrate_scale_on_port_tab() -> bool:
    """One-shot scale calibration on the Port tab.

    The Explore tab shows villages only — too sparse for reliable pair-ratio
    calibration.  The Port tab shows many ports per view, so a single frame
    is usually enough to produce a clean, isotropic scale.  Camera zoom is
    shared across tabs, so the saved scale transfers to Explore.

    Returns True if a plausible+isotropic scale was computed and persisted,
    False otherwise (caller continues without; downstream code falls back
    to blind pan).
    """
    from capture.adb_capture import capture_screen
    from vision.world_map_parser import parse_visible_ports, load_port_catalogue
    from actions.world_map_nav import (
        WorldMapNavigator,
        _is_plausible_scale,
        _is_isotropic_scale,
        _save_persisted_scale,
    )

    if not select_world_map_tab("port"):
        logger.warning("[port-tab-calibrate] could not select Port tab")
        return False

    frame = capture_screen()
    ports = load_port_catalogue()
    try:
        aliases = _PORT_ALIASES
    except NameError:
        aliases = {}
    visible = parse_visible_ports(frame, ports, aliases)
    if len(visible) < 2:
        logger.info(
            f"[port-tab-calibrate] only {len(visible)} visible port(s); "
            "need ≥2 for pair-ratio calibration — skipping"
        )
        return False

    nav = WorldMapNavigator(catalogue=ports, aliases=aliases)
    scale_x, scale_y = nav._calibrate(visible)
    if not (_is_plausible_scale(scale_x) and _is_plausible_scale(scale_y)):
        logger.info(
            f"[port-tab-calibrate] implausible scale ({scale_x}, {scale_y}) "
            f"from {len(visible)} ports — skipping save"
        )
        return False
    if not _is_isotropic_scale(scale_x, scale_y):
        logger.warning(
            f"[port-tab-calibrate] anisotropic scale ({scale_x:.2f}, "
            f"{scale_y:.2f}) from {len(visible)} ports — skipping save"
        )
        return False

    _save_persisted_scale(scale_x, scale_y)
    logger.info(
        f"[port-tab-calibrate] saved scale=({scale_x:.2f}, {scale_y:.2f}) "
        f"from {len(visible)} visible ports"
    )
    return True


def select_world_map_tab(tab_name: str) -> bool:
    """Switch the world map to the named tab.

    Idempotent in the game: tapping an already-active tab is a no-op.
    We still emit the tap so callers can use this without tracking
    current tab state — re-running the function never breaks anything.

    Returns True if a tab was tapped, False if the tab couldn't be
    located in the top toolbar (OmniParser miss, or wrong screen).
    """
    tab_lc = tab_name.lower().strip()
    if tab_lc not in _WORLD_MAP_TABS:
        logger.warning(
            f"[world-map-tab] unknown tab {tab_name!r}; expected one of "
            f"{_WORLD_MAP_TABS}"
        )
        return False

    from vision.omniparser import get_omniparser
    from actions.adb_actions import tap as _tap
    from capture.adb_capture import capture_screen as _capture

    frame = _capture()
    parser = get_omniparser()
    if not parser.yolo_available():
        logger.warning("[world-map-tab] OmniParser unavailable — cannot locate tab")
        return False

    # Tabs sit in the top toolbar (y < ~150 on 1080-tall screen).
    elements = parser.parse_fast(frame)
    candidates = [
        el for el in elements
        if el.cy < 200
        and (el.label or "").strip().lower() == tab_lc
    ]
    if not candidates:
        logger.warning(
            f"[world-map-tab] could not find {tab_name!r} tab "
            f"in top toolbar of current frame"
        )
        return False

    # If multiple matches, pick the one closest to the typical tab row
    # centre (y ≈ 50) — guards against a "Port" or "Trade" tooltip text
    # picked up elsewhere on the map.
    target = min(candidates, key=lambda el: abs(el.cy - 50))
    logger.info(
        f"[world-map-tab] tapping {tab_lc!r} tab @ ({target.cx},{target.cy})"
    )
    _tap(target.cx, target.cy)
    time.sleep(1.5)
    return True


def _find_port_on_world_map(
    frame: Image.Image,
    destination: str,
    x_max: Optional[int] = None,
) -> Tuple[Optional[Tuple[int, int]], list]:
    """
    OCR world map for destination port label.

    Returns:
        (pixel_pos, visible_ports) where:
          pixel_pos     — (cx, cy) of the destination label, or None if not found.
          visible_ports — list of PORT_POSITIONS keys visible in the current view.

    Searches all alias names for the destination (e.g. "Lisboa" for "Lisbon").
    x_max: if set, ignore matches with cx > x_max (excludes city-info panel text).
    """
    import numpy as np
    from vision.ocr import _get_reader
    from actions.world_map import ports_from_ocr_tokens

    region = frame.crop((0, 40, frame.width, frame.height - 40))
    raw = _get_reader().readtext(np.array(region), detail=1)

    # Log every readable token so we can see exactly what the world map shows
    all_tokens = [(text, conf) for _, text, conf in raw if conf >= 0.25]
    logger.info(f"  World map OCR ({len(all_tokens)} tokens): "
                + ", ".join(f"{t!r}({c:.2f})" for t, c in all_tokens))

    # Extract landmark ports for self-localisation
    visible_ports = ports_from_ocr_tokens(all_tokens)
    if visible_ports:
        logger.debug(f"  Landmarks visible: {visible_ports}")

    search_names = _port_names_to_search(destination)
    logger.debug(f"  Searching for {destination!r} using names: {search_names}")

    best_pos, best_conf = None, 0.0
    for bbox, text, conf in raw:
        if conf < 0.25:
            continue
        text_lower = text.lower()

        # Match logic:
        #   A. fuzzy_contains(text_lower, name) → port name found in OCR text
        #   B. token_sim for truncated labels (e.g. "Port Roy" for "Port Royal").
        #      Threshold 0.85 (stricter than port-list 0.75) — the world map shows
        #      190+ ports simultaneously; loose matching causes wrong-port selections
        #      (e.g. "Candia" matching alias "canaria" at 0.77 when seeking Las Palmas).
        matched = any(
            fuzzy_contains(text_lower, name) or
            (token_sim(text_lower, name) >= 0.85 and len(text_lower) >= len(name) * 0.6)
            for name in search_names
        )
        if not matched:
            continue
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        cx = int((min(xs) + max(xs)) / 2)
        cy = int((min(ys) + max(ys)) / 2) + 40
        if x_max is not None and cx > x_max:
            logger.debug(f"  Skipping {text!r} @ ({cx},{cy}) — beyond x_max={x_max} (likely panel text)")
            continue
        if conf > best_conf:
            best_conf, best_pos = conf, (cx, cy)
            logger.info(f"  Matched {destination!r}: {text!r} conf={conf:.2f} @ ({cx},{cy})")

    if best_pos is None:
        logger.info(f"  {destination!r} not found in current world map view")
    return best_pos, visible_ports


def _refresh_tap_position(
    stale_pos: Tuple[int, int],
    fresh_pos: Optional[Tuple[int, int]],
    destination: str,
    *,
    drift_threshold: int = 5,
) -> Tuple[int, int]:
    """Return the position to actually tap, given a stale position
    and a freshly re-located one.

    Pre-tap refresh exists because by the time tap() actually lands on
    screen, *stale_pos* may be 6-15 s old (OmniParser + OCR + matching +
    the upstream tap()'s anti-bot delay).  Overlays such as the Aconite-
    Boom announcement banner or weather animations can shift labels in
    that window.  When a fresh re-OCR succeeds, prefer the fresh position;
    otherwise keep the stale one (better than nothing).

    Logs a diff only when the drift exceeds *drift_threshold* pixels —
    sub-pixel jitter is noise, not signal.
    """
    if fresh_pos is None:
        logger.info(
            f"  Pre-tap refresh did not relocate {destination!r}; "
            f"using prior position {stale_pos}"
        )
        return stale_pos
    dx = fresh_pos[0] - stale_pos[0]
    dy = fresh_pos[1] - stale_pos[1]
    if abs(dx) > drift_threshold or abs(dy) > drift_threshold:
        logger.info(
            f"  Pre-tap refresh: {destination!r} moved "
            f"{stale_pos} → {fresh_pos} (Δ={dx:+d},{dy:+d})"
        )
    return fresh_pos


def _match_port_list_tokens(
    tokens: list,
    dest_lower: str,
    *,
    x_offset: int = 0,
    y_offset: int = 0,
    min_conf: float = 0.30,
    min_len: int = 3,
    row_tol: int = 25,
    sim_threshold: float = 0.75,
) -> Tuple[Optional[Tuple[int, int]], frozenset]:
    """
    Find *dest_lower* among OCR tokens from the world-map port-list panel.

    Returns (best_match_pos | None, label_set).
    *best_match_pos* is in full-frame coordinates (offsets applied).

    Match strategy:
      1. Each OCR token vs *dest_lower* — handles single-token names and
         OCR groupings like "Port Royal" detected as a single token.
      2. For multi-word destinations, group tokens into rows by y-coord
         and try concatenating adjacent tokens — catches EasyOCR splitting
         "Port Royal" into ["Port", "Royal"], or rows that include
         metadata after the name (e.g. "Port Royal 12d").

    Args:
        tokens:    list of (text, conf, cx, cy) — coords in crop space.
        dest_lower: destination name, lowercased.
        x_offset, y_offset: added to returned coords (crop → full-frame).
        min_conf:   tokens below this confidence are ignored.
        min_len:    tokens shorter than this are ignored.
        row_tol:    y-pixel tolerance for grouping tokens into the same row.
        sim_threshold: SequenceMatcher ratio required for a match.
    """
    dest_words = dest_lower.split()
    best: Optional[Tuple[int, int]] = None
    best_score = 0.0
    labels: list[str] = []
    filtered: list = []
    for text, conf, cx, cy in tokens:
        if cx < 50 or conf < min_conf:
            continue
        t = text.lower().strip()
        if len(t) >= min_len:
            labels.append(t)
        if len(t) < min_len:
            continue
        filtered.append((t, conf, cx, cy))
        sim = token_sim(t, dest_lower)
        if sim >= sim_threshold:
            score = conf * sim
            if score > best_score:
                best_score = score
                best = (cx + x_offset, cy + y_offset)

    # Multi-token row matching for two-word names.
    if len(dest_words) >= 2:
        # Cluster tokens into rows by y-coord proximity.
        tokens_by_y = sorted(filtered, key=lambda r: r[3])
        rows: list[list] = []
        for tok in tokens_by_y:
            if rows and abs(tok[3] - rows[-1][-1][3]) <= row_tol:
                rows[-1].append(tok)
            else:
                rows.append([tok])

        win = len(dest_words)
        for row in rows:
            if len(row) < win:
                continue
            row.sort(key=lambda r: r[2])   # left-to-right

            # Sliding window of size = #dest words.  Port name sits at
            # the row start; tail tokens (distance, flag) are skipped
            # by considering each contiguous sub-window.
            for start in range(len(row) - win + 1):
                window = row[start:start + win]
                combined = " ".join(r[0] for r in window)
                sim = token_sim(combined, dest_lower)
                if sim >= sim_threshold:
                    avg_conf = sum(r[1] for r in window) / win
                    score = avg_conf * sim
                    if score > best_score:
                        best_score = score
                        rx = sum(r[2] for r in window) // win
                        ry = sum(r[3] for r in window) // win
                        best = (rx + x_offset, ry + y_offset)
                        logger.info(
                            f"  Multi-token row match: "
                            f"{combined!r} ≈ {dest_lower!r} "
                            f"(sim={sim:.2f} conf={avg_conf:.2f})"
                        )

    return best, frozenset(labels)


def _try_port_search(destination: str) -> Optional[Tuple[int, int]]:
    """
    Open the world map port-list panel, search for *destination*, tap the result.

    The world map has two stacked icons in the top-left corner just below the
    back arrow:
      Top icon    — port list / search  ← we tap this
      Bottom icon — market / goods list

    Strategy:
      1. Run OmniParser on the top-left area to find the port icon.
         Probe a column of candidate positions if needed (icon is graphical,
         not text, so we verify by checking whether the panel opened).
      2. After each tap, check if the search panel opened: look for an input
         element or new left-panel content via OmniParser + OCR.
      3. Once panel is open, locate the input field via OmniParser and tap it.
      4. Type the destination name; wait for results.
      5. Verify results appeared; find the best-matching row.
      6. Tap it; wait for "Go to City" button.

    Returns the pixel position of "Go to City", or None on failure (caller
    falls back to visual panning).
    """
    from actions.adb_actions import tap as _tap, input_text as _input_text
    from vision.omniparser import get_omniparser
    import json as _json
    import numpy as np
    from pathlib import Path as _Path

    logger.info(f"[port-search] Searching for {destination!r}")

    # ── Step 1: find and tap the port-list icon ───────────────────────────────
    # The icon sits in a narrow strip on the left edge of the world map,
    # below the back arrow.  We scan a column of positions from y=80 to y=200
    # and verify the panel opened after each attempt.
    #
    # OmniParser detects it as an 'icon' element in the left margin (x < 120).
    # If OmniParser finds candidates we try those first, otherwise probe the
    # column at x=55 (empirical center of the icon strip).

    def _panel_is_open(fr) -> bool:
        """Return True if the port-list search panel is now showing."""
        # The panel is open when the left side of the screen (~x<600) gains
        # substantial new content compared to the bare world map.
        # Indicators: an input/edit element, or a list of port names.
        parser = get_omniparser()
        if parser.yolo_available():
            left_els = [e for e in parser.parse_fast(fr) if e.cx < 650]
            # Only the search/input element is a reliable "panel fully loaded" signal.
            # The generic non_icon >= 4 check was removed — it fired on partially-loaded
            # panels (no search box yet), causing the scan to start before the port list
            # had rendered and falsely hitting "end of list" after 1-2 scrolls.
            for el in left_els:
                if any(kw in el.label.lower() for kw in
                       ("edit", "input", "search", "text", "field")):
                    logger.info(f"  Panel open — OmniParser input element: {el.label!r} @ ({el.cx},{el.cy})")
                    return True
        # OCR fallback: check for typical list-panel tokens on the left side
        for text, conf, cx, cy in _ocr_frame(fr):
            if cx > 650:
                continue
            t = text.lower()
            if any(kw in t for kw in ("search", "filter", "port name", "city")):
                logger.info(f"  Panel open — OCR token {text!r} @ ({cx},{cy})")
                return True
        return False

    # ── Learned icon position (saved on first successful tap) ────────────────
    # Once the bot finds the port-list icon, save its position so future runs
    # can go straight to the right spot without probing.
    _UI_POS_FILE = _Path("memory/knowledge/config/world_map_ui.json")

    def _load_icon_pos() -> Optional[Tuple[int, int]]:
        try:
            d = _json.loads(_UI_POS_FILE.read_text())
            p = d.get("port_list_icon")
            if p:
                return (int(p[0]), int(p[1]))
        except Exception:
            pass
        return None

    def _save_icon_pos(x: int, y: int) -> None:
        try:
            d: dict = {}
            if _UI_POS_FILE.exists():
                d = _json.loads(_UI_POS_FILE.read_text())
            d["port_list_icon"] = [x, y]
            _UI_POS_FILE.write_text(_json.dumps(d, indent=2))
            logger.info(f"  Saved port-list icon position ({x},{y}) → {_UI_POS_FILE}")
        except Exception as exc:
            logger.warning(f"  Could not save icon position: {exc}")

    saved_pos = _load_icon_pos()
    parser = get_omniparser()

    def _omniparser_icon_candidates(fr) -> list[Tuple[int, int]]:
        """Run OmniParser and return left-edge element positions (below back arrow)."""
        if not parser.yolo_available():
            return []
        all_left = [e for e in parser.parse_fast(fr) if e.cx < 200 and e.cy > 60]
        all_left.sort(key=lambda e: e.cy)
        logger.info(
            f"  OmniParser left-side elements: "
            + (", ".join(f"{e.label!r}({e.element_type})@({e.cx},{e.cy})"
                         for e in all_left) or "none")
        )
        return [(e.cx, e.cy) for e in all_left]

    icon_tapped = False
    frame_after_icon = capture_screen()

    # ── Step 1: FIND the icon, don't assume where it is ──────────────────────
    # The saved position is a PRIOR over detections, never a tap target on its own.
    # Tapping it blind was the old first move, and when it goes stale — the game re-bakes
    # a camera-cutout offset per screen, so the whole UI shifts between sessions — that
    # tap lands on whatever is now at those pixels. At sea on 2026-08-21 that meant taps
    # into the sea view and its destination list while the bot believed it was opening a
    # search panel (user: "it was blindly tapping a position assuming the icon is there
    # but it is not due to the screen rotation ... we need to really find where it is").
    for omni_attempt in range(3):
        fr = capture_screen()
        candidates = _omniparser_icon_candidates(fr)
        if candidates and saved_pos:
            # Prefer the candidate nearest where the icon was last seen — the memory
            # disambiguates between detections instead of replacing them.
            candidates.sort(key=lambda c: (c[0] - saved_pos[0]) ** 2 + (c[1] - saved_pos[1]) ** 2)
            logger.info(f"  Ranking {len(candidates)} candidate(s) by distance to the "
                        f"last-known icon position {saved_pos}")
        for ix, iy in candidates:
            logger.info(f"  Tapping detected candidate @ ({ix},{iy})")
            _tap(ix, iy)
            time.sleep(3.0)
            fr2 = capture_screen()
            if _panel_is_open(fr2):
                logger.info(f"  Panel opened @ ({ix},{iy}) (detect attempt {omni_attempt + 1})")
                icon_tapped = True
                _save_icon_pos(ix, iy)
                time.sleep(1.5)
                frame_after_icon = capture_screen()
                break
        if icon_tapped:
            break
        if candidates:
            logger.info(f"  candidates tried, none opened the panel "
                        f"(attempt {omni_attempt + 1}/3)")
        else:
            logger.info(f"  no icon detected (attempt {omni_attempt + 1}/3) — re-perceiving")
            time.sleep(1.5)

    # Last resort: the icon was never detected on any attempt. Only now is the remembered
    # position worth a try, and it is announced as the guess it is.
    if not icon_tapped and saved_pos:
        logger.warning(f"  icon never detected — falling back to the remembered position "
                       f"{saved_pos}, which may be stale if the UI has shifted")
        _tap(*saved_pos)
        time.sleep(3.0)
        if _panel_is_open(capture_screen()):
            logger.info(f"  Panel opened via remembered position {saved_pos}")
            icon_tapped = True
            time.sleep(1.5)
            frame_after_icon = capture_screen()

    if not icon_tapped:
        logger.info("  Could not open port-list panel — skipping port search")
        return None

    # ── Step 2: TYPE the destination into the search box, then read the result ──
    # input_text() injects per-CHARACTER keyevents (not on-screen-keyboard taps), which
    # DO register in the game's search field — verified live 2026-08-18: tapping the
    # 'Search' box + typing 'Masulipatnam' filtered the port list to exactly it.  The old
    # list-scroll approach (below-comment claimed typing was impossible) could not reach
    # mid-alphabet ports: the scroll swipe didn't advance the list, so it declared "end"
    # after one scan and gave up (live 2026-08-18: Masulipatnam, an 'M' port, never found).
    dest_lower = destination.lower().strip()
    _LIST_CROP = (0, 150, 650, 1080)   # left panel; crop before OCR → ~4× faster

    def _scan_list(frame) -> Tuple[Optional[Tuple[int, int]], frozenset]:
        """OCR the left panel once → (best_match_coord | None, label_set), full-frame coords."""
        return _match_port_list_tokens(
            list(_ocr_frame(frame.crop(_LIST_CROP))),
            dest_lower, x_offset=_LIST_CROP[0], y_offset=_LIST_CROP[1],
        )

    # Locate + tap the search input box (OmniParser 'Search'/'input'/'edit' element, left).
    search_xy = None
    for el in parser.parse_fast(capture_screen()):
        if el.cx < 650 and any(k in (el.label or "").lower()
                               for k in ("search", "edit", "input", "field")):
            search_xy = (el.cx, el.cy)
            break
    if search_xy is None:
        search_xy = (420, 141)   # observed position; safe default if OmniParser misses it
    # Type only a short PREFIX at a human interval (anti-cheat, user 2026-08-18): 4 chars
    # filter the list enough to find the port without typing the whole word.  clear_first
    # wipes any leftover query so a re-search doesn't append onto the previous one.
    _typed = _ascii_search_prefix(destination)
    logger.info(f"  Typing {_typed!r} (prefix of {destination!r}) into search box @ {search_xy}")
    _tap(*search_xy)
    time.sleep(1.0)
    _input_text(_typed, max_chars=len(_typed), clear_first=True)
    time.sleep(1.5)

    best_match: Optional[Tuple[int, int]] = None
    _labels: frozenset = frozenset()
    for _try in range(2):                       # the filtered result can lag a moment
        best_match, _labels = _scan_list(capture_screen())
        if best_match:
            break
        time.sleep(1.5)
    if best_match is None:
        logger.info(f"  {destination!r} not found after typing — saw {sorted(_labels)[:8]}")
        return None
    logger.info(f"  Found {destination!r} via search @ {best_match}")

    # ── Step 3: tap result; wait for "Go to City" ─────────────────────────────
    # IMPORTANT: after tapping, verify the City Info panel that opened is
    # actually for the intended destination — not a stale panel or a mis-tap.
    # If "Go to City" is found but the destination name is absent, the wrong
    # port's panel is showing.  Re-tap and wait again.
    logger.info(f"  Tapping {destination!r} @ {best_match}")
    _tap(*best_match)
    time.sleep(3.0)

    def _panel_is_for_destination(tokens) -> bool:
        """True if the destination name appears somewhere in the OCR tokens."""
        full = " ".join(t.lower() for t, c, cx, cy in tokens)
        return fuzzy_contains(full, dest_lower)

    deadline = time.time() + 15.0
    while time.time() < deadline:
        fr = capture_screen()
        tokens = list(_ocr_frame(fr))
        go_to_pos = _find_destination_button(tokens, label="search-tap")

        if go_to_pos:
            if _panel_is_for_destination(tokens):
                logger.info(f"  Destination button @ {go_to_pos} — panel confirmed for {destination!r}")
                return go_to_pos
            else:
                visible_names = [t for t, c, cx, cy in tokens if len(t) > 3 and c > 0.5]
                logger.warning(
                    f"  Destination button found @ {go_to_pos} but panel is NOT for "
                    f"{destination!r} — visible: {visible_names[:6]} — re-tapping"
                )
                _tap(*best_match)
                time.sleep(3.0)
                continue

        time.sleep(2.0)

    diag_tokens = list(_ocr_frame(capture_screen()))
    logger.info(
        "  Destination button did not appear. Screen OCR: "
        + ", ".join(f"{t!r}({c:.2f})@({x},{y})" for t, c, x, y in diag_tokens)
    )
    return None


def _navigate_world_map_to_port(destination: str, from_port: Optional[str] = None) -> bool:
    """
    Find destination on world map and tap 'Go to City'.

    Tries two strategies in order:
      1. Port search (toolbar "Port" button → type name → tap result) — fast and
         reliable for any discovered port.
      2. Visual panning — landmark-guided directional pan + fine grid sweep.
         Used as fallback for undiscovered ports or when search panel is unavailable.

    Args:
        destination: Port name to sail to (e.g. "Port Royal").
        from_port:   Current port name — used to compute the pan direction.
                     If None, where_am_i() is queried.

    IMPORTANT: every swipe is guarded by a where_am_i() check.
    Swiping on the sea view rotates the 3D camera — we must never swipe
    unless we have confirmed we are on the world map.
    """
    logger.info(f"Finding {destination!r} on world map…")

    # Lower-cased destination — used by the closure _panel_showing to verify
    # that the 'Go to City' panel is actually for the intended port (vs an
    # accidental tap on a neighbouring city).  Was missing previously, which
    # caused a NameError when _panel_showing was called.
    dest_lower = destination.lower().strip()

    # Guard: confirm we're actually on the world map before doing anything.
    # 'building' is a known chrome-detector false positive for the world map;
    # allow it through if port labels are visible in the left screen area.
    frame = capture_screen()
    loc = where_am_i(frame)
    logger.info(f"  Entry state: {loc['location']!r} — {loc['detail']}")
    if loc["location"] == "building" and _world_map_port_labels_visible(frame):
        logger.info("  'building' false positive confirmed as world map — proceeding")
    elif loc["location"] != "world_map":
        logger.warning(f"  Not on world map (got {loc['location']!r}) — aborting to avoid sea camera rotation")
        return False

    # Determine the port we're departing from (used for directional panning).
    if from_port is None:
        # where_am_i() was already called above; use its port if available.
        from_port = loc.get("port") or ""

    # ── Strategy 1: TYPED port search (fast + deterministic for any DISCOVERED port) ──
    # Typing a short name-prefix into the search box filters the port list to the exact
    # match, at ANY distance (verified live 2026-08-18).  Primary over coordinate panning,
    # which is slow and — near map edges / with few visible ports — mis-estimates scale and
    # gets stuck (live 2026-08-18: the Masulipatnam pan aborted 'stuck' seeing only Kolkata,
    # while a typed search resolved it instantly).  Was demoted to backup on 2026-05-13 when
    # search meant list-SCROLL (couldn't reach mid-alphabet / far ports); typing removes that
    # limitation, so search is primary again.
    pos = None
    _search_found_go_to_city = False
    go_pos = _try_port_search(destination)
    if go_pos is not None:
        logger.info(f"  Port search succeeded — 'Go to City' @ {go_pos}")
        pos = go_pos
        _search_found_go_to_city = True   # skip Phase 1 city-tap; go straight to Go-to-City
    else:
        logger.info("  Port search failed — falling back to coordinate pan")
        # Dismiss soft keyboard if still open — it covers the lower half of the world map,
        # breaking visual port detection and panning.  BACK closes only the keyboard.
        _kb_toks = list(_ocr_frame(capture_screen()))
        if sum(1 for text, conf, cx, cy in _kb_toks
               if len(text.strip()) <= 2 and cy > 500) >= 3:
            logger.info("  Soft keyboard detected — pressing BACK to dismiss before panning")
            from actions.adb_actions import press_back as _press_back
            _press_back()
            time.sleep(1.5)

        # Re-verify we're still on the world map before panning.
        _wm_loc = where_am_i()["location"]
        if _wm_loc not in ("world_map", "building"):
            logger.warning(f"  World map lost after port search (now {_wm_loc!r}) — reopening")
            if not _open_world_map_from_sea(capture_screen()):
                logger.error("  Could not reopen world map — aborting")
                return False
            time.sleep(1.5)

        # ── Strategy 2: coordinate pan — fallback for UNDISCOVERED ports (no search row) ──
        # Every discovered port's game (x,y) is in the catalogue at
        # memory/knowledge/world_map/port_coordinates.json; pan toward it using scale
        # calibrated from visible labels.  See actions/world_map_nav.py.
        try:
            from actions.world_map_nav import WorldMapNavigator
            nav = WorldMapNavigator()
            pos = nav.pan_to_port(destination, max_pans=8, from_port=from_port)
            if pos is not None:
                logger.info(f"  WorldMapNavigator found {destination!r} @ {pos}")
        except Exception as e:
            logger.warning(f"  WorldMapNavigator failed ({type(e).__name__}: {e})")
            pos = None

    # No further fallback — WorldMapNavigator (catalogue + odometry) and
    # port-search list-scroll are the two paths.  The legacy landmark
    # panner was removed 2026-05-20 after the live-run failure showed
    # it loops forever issuing the same vector when the visible-port set
    # is identical across attempts.
    if pos is None:
        logger.warning(
            f"  {destination!r} not found on world map "
            "(pan_to_port + port-search both failed)"
        )
        return False

    # ── Phase 1: tap the city until "Go to City" panel appears ──────────────────
    # Three outcomes after tapping a spot on the world map:
    #   A. "Go to City" appears → city was hit, map panned to center it  ✓
    #   B. "Move" appears       → sea waypoint selected, no panning happened
    #   C. Neither appears      → land tap; a transient "can't go there" message
    #                             appears and fades, leaving the map unchanged
    # In cases B and C the city may be anywhere on screen (not necessarily
    # centered), so always re-find via OCR before the next tap.

    _CITY_SELECTION_PANS = [
        (800, 400, 200, 400),   # pan right
        (200, 400, 800, 400),   # pan left
        (500, 600, 500, 200),   # pan down
        (500, 200, 500, 600),   # pan up
    ]

    def _extract_go_to_city_pos(fr: Image.Image) -> Optional[Tuple[int, int]]:
        """
        Find the destination action button pixel position
        ("Go to City" / "Move to Village").
        Runs OCR on BOTH the full frame and a 40px-cropped frame (same crop as
        _find_port_on_world_map).  Logs what each scan finds so we can compare.
        Uses the cropped result when available — EasyOCR detects the split tokens
        (verb + noun) more reliably on the crop than on the full frame.
        """
        import numpy as np
        from vision.ocr import _get_reader

        def _relevant(toks):
            """Pick tokens that could be part of the destination button label
            (verb or noun match) — used purely for diagnostic logging."""
            keywords = _DESTINATION_BUTTON_VERBS + _DESTINATION_BUTTON_NOUNS
            return [(t, f"{c:.2f}", cx, cy)
                    for t, c, cx, cy in toks
                    if any(k in t.lower() for k in keywords)]

        # ── Full-frame OCR ────────────────────────────────────────────────────
        full_tokens = _ocr_frame(fr)
        full_relevant = _relevant(full_tokens)
        logger.info(f"  [full-frame OCR] destination button tokens "
                    f"({len(full_relevant)}): "
                    + (", ".join(f"{t!r}({s})@({cx},{cy})" for t, s, cx, cy in full_relevant)
                       if full_relevant else "none"))
        full_pos = _find_destination_button(full_tokens, label="full")

        # ── Cropped-frame OCR (40 px off top/bottom, matches _find_port_on_world_map) ──
        crop = fr.crop((0, 40, fr.width, fr.height - 40))
        raw_crop = _get_reader().readtext(np.array(crop), detail=1)
        crop_tokens = []
        for bbox, text, conf in raw_crop:
            if conf < 0.30:
                continue
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            cx = int((min(xs) + max(xs)) / 2)
            cy = int((min(ys) + max(ys)) / 2) + 40  # restore 40 px crop offset
            crop_tokens.append((text, conf, cx, cy))
        crop_relevant = _relevant(crop_tokens)
        logger.info(f"  [crop OCR]       destination button tokens "
                    f"({len(crop_relevant)}): "
                    + (", ".join(f"{t!r}({s})@({cx},{cy})" for t, s, cx, cy in crop_relevant)
                       if crop_relevant else "none"))
        crop_pos = _find_destination_button(crop_tokens, label="crop")

        # ── Reconcile ─────────────────────────────────────────────────────────
        if full_pos and crop_pos:
            logger.info(f"  Both OCRs found button — full @ {full_pos}, crop @ {crop_pos} → using crop")
            return crop_pos
        if crop_pos:
            logger.info(f"  Crop OCR found button @ {crop_pos}; full-frame OCR did NOT")
            return crop_pos
        if full_pos:
            logger.info(f"  Full-frame OCR found button @ {full_pos}; crop OCR did NOT")
            return full_pos
        logger.debug("  Neither OCR found 'Go to City' button")
        return None

    def _panel_showing() -> Tuple[bool, Optional[Image.Image], Optional[Tuple[int, int]]]:
        """
        Capture a fresh frame and check whether the 'Go to City' panel is visible
        AND is showing the correct destination.
        Returns (panel_visible, frame, go_to_city_pos).
        A panel for the wrong port returns (False, frame, None) to force a re-tap.
        """
        fr = capture_screen()
        gpos = _extract_go_to_city_pos(fr)
        if gpos is None:
            return False, fr, None
        # Verify the panel is for the intended destination
        tokens = list(_ocr_frame(fr))
        full = " ".join(t.lower() for t, c, cx, cy in tokens)
        if not fuzzy_contains(full, dest_lower):
            visible = [t for t, c, cx, cy in tokens if len(t) > 3 and c > 0.5]
            logger.warning(
                f"  'Go to City' panel open but NOT for {destination!r} "
                f"— visible text: {visible[:6]} — ignoring"
            )
            return False, fr, None
        return True, fr, gpos

    def _select_city_on_map(current_pos: Tuple[int, int]) -> Tuple[Optional[Image.Image], Optional[Tuple[int, int]]]:
        """
        Tap current_pos and wait for the 'Go to City' panel to appear.
        Returns (frame, go_to_city_pos) — go_to_city_pos is None on failure.
        tap() fires the physical tap and then blocks for the anti-bot delay (5–10 s),
        so _wait_for_screen starts well after the tap has landed.  The map panning
        animation adds another 2–5 s, so we need a generous timeout.

        Pre-tap refresh: re-OCR a fresh frame and re-locate *destination*.
        See _refresh_tap_position for rationale.
        """
        try:
            fresh = capture_screen()
            fresh_pos, _ = _find_port_on_world_map(fresh, destination)
        except Exception as exc:
            logger.info(f"  Pre-tap refresh skipped ({type(exc).__name__}: {exc})")
            fresh_pos = None
        tap_pos = _refresh_tap_position(current_pos, fresh_pos, destination)

        logger.info(f"  Tapping {destination!r} @ {tap_pos}")
        tap(*tap_pos)   # physical tap fires, then blocks 5–10 s
        # Poll for the panel.  Use a 20 s window — the tap already fired; we're
        # waiting for the panning animation to complete and the panel to render.
        deadline = time.time() + 20.0
        poll = 0
        while time.time() < deadline:
            poll += 1
            fr = capture_screen()
            logger.info(f"  Panel poll #{poll} (remaining: {deadline - time.time():.0f}s)")
            gpos = _extract_go_to_city_pos(fr)
            if gpos is not None:
                logger.info(f"  Panel detected on poll #{poll} — button @ {gpos}")
                return fr, gpos
            time.sleep(2.0)
        logger.info(f"  Panel not detected after {poll} polls (20 s window expired)")
        return None, None

    def _refind_city() -> Optional[Tuple[int, int]]:
        """
        Re-find the destination city on the current world map view.
        First checks whether the 'Go to City' panel is already open (the tap that
        triggered this recovery may have succeeded but the timeout expired before
        the panel was detected).  If the panel is showing, returns None to signal
        that the caller should read the button position directly rather than re-tap.
        If the panel is not showing, searches for the city icon on the map and
        pans if necessary.
        Does NOT press back — keeps the world map open.
        """
        # Quick check: panel already open?
        visible, fr, _ = _panel_showing()
        if visible:
            logger.info(f"  Panel already showing — skipping city re-tap")
            return None   # sentinel: caller must call _panel_showing() directly

        # Find city icon on map — exclude far-right positions that are likely
        # part of the city info panel overlay rather than the map icon.
        p, _ = _find_port_on_world_map(fr, destination, x_max=1600)
        if p:
            return p
        # City not visible on current view — pan to find it
        for dx1, dy1, dx2, dy2 in _CITY_SELECTION_PANS:
            sf = capture_screen()
            sloc = where_am_i(sf)
            if sloc["location"] not in ("world_map", "building"):
                logger.warning(f"  Lost world map during recovery pan — stopping")
                return None
            logger.info(f"  Panning to re-find {destination!r}…")
            swipe(dx1, dy1, dx2, dy2, duration_ms=600)
            time.sleep(1.5)
            fr = capture_screen()
            p, _ = _find_port_on_world_map(fr, destination, x_max=1600)
            if p:
                return p
        return None

    if not _search_found_go_to_city:
        # ── Phase 1: tap the city icon until "Go to City" panel appears ───────
        frame, go_pos = _select_city_on_map(pos)
        for city_retry in range(2):
            if go_pos is not None:
                break
            visible, fresh_frame, fresh_gpos = _panel_showing()
            if visible:
                logger.info(f"  Panel appeared after timeout — proceeding (button @ {fresh_gpos})")
                frame, go_pos = fresh_frame, fresh_gpos
                break

            check = fresh_frame
            # Sea-waypoint "Move" marker shows just "Move" with no
            # destination noun.  Distinguish from "Move to Village" /
            # "Move to City" / "Go to City" by checking the destination
            # button helper (matches all forms).
            has_move = (
                _screen_contains(check, " move")
                and not _has_destination_button_text(list(_ocr_frame(check)))
            )
            if has_move:
                logger.warning(f"  Tap hit sea waypoint ('Move' visible) — re-finding {destination!r}")
            else:
                logger.info(f"  No panel appeared (possible land tap) — waiting 2s for transient to clear")
                time.sleep(2.0)
            pos2 = _refind_city()
            if pos2 is None:
                visible2, fresh_frame2, fresh_gpos2 = _panel_showing()
                if visible2:
                    logger.info(f"  Panel already open — using button @ {fresh_gpos2}")
                    frame, go_pos = fresh_frame2, fresh_gpos2
                    break
                logger.warning(f"  Cannot re-find {destination!r} on world map — aborting")
                return False
            frame, go_pos = _select_city_on_map(pos2)

        if go_pos is None:
            logger.warning(f"  'Go to City' panel did not appear after city-tap retries — aborting")
            return False

    logger.info(f"  Using 'Go to City' button @ {go_pos}")

    # ── Phase 3: tap "Go to City" and verify sailing started ────────────────────
    # Outcomes after tapping go_pos:
    #   • World map closes (sea/loading) → success
    #   • World map still open, "Move" visible → tap hit the map (sea waypoint)
    #   • World map still open, no panel → tap hit the map (land); wait + re-select
    for go_attempt in range(3):
        logger.info(f"  Tapping 'Go to City' @ {go_pos} (attempt {go_attempt+1})")
        tap(*go_pos)
        # The game raises "Moving to X after Auto Supply. Continue?" here, and NOTHING
        # proceeds until it is answered — the state never becomes sea/loading while the
        # modal is up, so the wait below would time out and this whole navigation would
        # report failure. Live 2026-08-21 (Kochi → Malé) that is exactly what happened:
        # "could not select 'Male' on the map", then a fallback to the harbour flow.
        # Confirming has to happen HERE, between the tap and the verdict.
        confirm_departure_notice(destination)
        loc = _wait_for_state_change(
            expected=("sea", "sea_cinematic", "loading", "port_overworld"),
            max_wait=8.0,
        )
        logger.info(f"  State after 'Go to City' tap: {loc['location']!r}")
        if loc["location"] in ("sea", "sea_cinematic", "loading", "port_overworld"):
            logger.info("  World map closed — sailing started")
            return True

        if loc["location"] != "world_map":
            logger.warning(f"  Unexpected location {loc['location']!r} after tap — aborting")
            return False

        # Still on world map — tap missed the button.  Check panel state.
        visible3, check3, gpos3 = _panel_showing()
        if visible3 and gpos3:
            logger.info(f"  Panel still showing (tap missed) — re-reading button @ {gpos3}")
            go_pos = gpos3
            continue

        check = check3
        has_move = (
            _screen_contains(check, " move")
            and not _has_destination_button_text(list(_ocr_frame(check)))
        )
        if has_move:
            logger.warning(f"  'Move' visible — 'Go to City' tap hit sea; re-selecting {destination!r}")
        else:
            logger.info(f"  No panel after tap (land tap?) — waiting 2s then re-selecting {destination!r}")
            time.sleep(2.0)

        pos3 = _refind_city()
        if pos3 is None:
            visible4, ff4, gpos4 = _panel_showing()
            if visible4 and gpos4:
                logger.info(f"  Panel open after re-find — using button @ {gpos4}")
                go_pos = gpos4
                continue
            logger.warning(f"  Cannot re-find {destination!r} — aborting")
            return False
        frame2, gpos2 = _select_city_on_map(pos3)
        if gpos2 is None:
            logger.warning("  City panel did not reappear after re-selecting — aborting")
            return False
        go_pos = gpos2

    logger.warning("Could not start sailing — world map did not close after 'Go to City'")
    return False


# ── Village navigation ──────────────────────────────────────────────────────

def _navigate_world_map_to_village(
    destination: str, from_port: Optional[str] = None,
) -> bool:
    """Pan to a village on the Explore tab and commit the sail.

    Returns True iff sailing started (world map closed and bot is on
    sea / loading), False otherwise.

    Simpler than the port flow: no list-scroll fallback (villages don't
    appear in the toolbar search panel).  Reuses the generalised
    _find_destination_button to locate the "Move to Village" action.
    """
    logger.info(f"Finding village {destination!r} on world map…")

    # Confirm we're on the world map (or a known false-positive thereof).
    frame = capture_screen()
    loc = where_am_i(frame)
    if loc["location"] == "building" and _world_map_port_labels_visible(frame):
        pass   # known false positive for world map
    elif loc["location"] != "world_map":
        logger.warning(
            f"  Not on world map (got {loc['location']!r}) — aborting"
        )
        return False

    # Pan: switches to Explore tab + uses village catalogue + same
    # scale/odometry machinery as port pan.
    pos = pan_to_village(destination, from_port=from_port)
    if pos is None:
        logger.warning(
            f"  pan_to_village could not locate village {destination!r}"
        )
        return False

    # ── Tap village, wait for "Move to Village" button ────────────────
    # Pre-tap refresh: re-OCR the current frame and re-locate the
    # village label.  pan_to_village returned a position computed
    # several seconds ago — the safe-tap-position fix from earlier
    # applies here too.
    try:
        fresh = capture_screen()
        from vision.world_map_parser import (
            parse_visible_ports, load_village_catalogue,
        )
        villages_visible = parse_visible_ports(
            fresh, load_village_catalogue(), {},
        )
        dest_key = destination.lower().strip()
        # Strip " village" suffix to match catalogue slug convention.
        dest_key_short = dest_key.replace(" village", "").strip()
        fresh_pos = None
        for vp in villages_visible:
            if vp.key in (dest_key, dest_key_short):
                fresh_pos = vp.tap_pos
                break
        if fresh_pos is not None and fresh_pos != pos:
            logger.info(
                f"  Pre-tap refresh: village {destination!r} moved "
                f"{pos} → {fresh_pos}"
            )
            pos = fresh_pos
    except Exception as e:
        logger.debug(f"  Pre-tap refresh skipped ({type(e).__name__}: {e})")

    logger.info(f"  Tapping village {destination!r} @ {pos}")
    tap(*pos)
    # Brief initial pause so the panel begins rendering before the
    # first OCR — much shorter than the old 3 s blanket sleep.
    time.sleep(0.8)

    # Poll for the Move-to-Village button on the village info panel.
    # Poll interval tightened from 2.0 s → 1.0 s; each iteration also
    # costs ~1-2 s of capture+OCR so the effective cadence is ~2-3 s.
    deadline = time.time() + 20.0
    btn = None
    poll = 0
    while time.time() < deadline:
        poll += 1
        fr = capture_screen()
        tokens = list(_ocr_frame(fr))
        btn = _find_destination_button(tokens, label=f"village-poll-{poll}")
        if btn:
            break
        logger.info(
            f"  Village panel poll #{poll} "
            f"(remaining: {deadline - time.time():.0f}s)"
        )
        time.sleep(1.0)

    if btn is None:
        logger.warning(
            f"  'Move to Village' button did not appear after tapping "
            f"{destination!r} — aborting"
        )
        return False

    # Commit: tap Move to Village, then poll for the world-map close.
    # Polled wait exits as soon as the game transitions (1-3 s on the
    # happy path) instead of blocking the full worst-case 8 s.
    logger.info(f"  Tapping 'Move to Village' @ {btn}")
    tap(*btn)
    loc_after = _wait_for_state_change(
        expected=("sea", "sea_cinematic", "loading", "port_overworld"),
        max_wait=8.0,
    )
    if loc_after["location"] in ("sea", "sea_cinematic", "loading", "port_overworld"):
        logger.info(
            f"  World map closed (now {loc_after['location']!r}) "
            f"— sailing to {destination!r} started"
        )
        return True

    logger.warning(
        f"  After 'Move to Village' tap, unexpected location "
        f"{loc_after['location']!r} — aborting"
    )
    return False


# ── Smart destination dispatch ───────────────────────────────────────────────

def _classify_destination(name: str) -> Optional[str]:
    """Return "port" if *name* matches a port (or alias), "village" if
    it matches a village, or None if neither.

    Port match first because:
      - the port catalogue is the historical default;
      - some port aliases overlap with village name fragments (e.g.
        "Frankish" appears in both Frankish Village and a port alias —
        prefer the named city).
    Lookups are slug-style: lowercase, whitespace-trimmed, optional
    " village" suffix stripped for village matching.
    """
    key = name.lower().strip()
    if not key:
        return None

    # Port catalogue check (including aliases).
    try:
        from vision.world_map_parser import load_port_catalogue
        ports = load_port_catalogue()
        if key in ports:
            return "port"
        # Alias-aware lookup.
        for alias_key, variants in _PORT_ALIASES.items():
            if key == alias_key or key in variants:
                return "port"
    except Exception:
        pass

    # Village catalogue check (slug or slug + " village").
    try:
        from vision.world_map_parser import load_village_catalogue
        villages = load_village_catalogue()
        if key in villages:
            return "village"
        # Try stripping " village" suffix — catalogue slug is "berber",
        # user may have typed "berber village".
        short = key.replace(" village", "").strip()
        if short and short in villages:
            return "village"
    except Exception:
        pass

    return None


def _navigate_world_map_to_destination(
    destination: str, from_port: Optional[str] = None,
) -> bool:
    """Smart-dispatch destination navigator.

    Looks up *destination* in the port catalogue (with aliases) first;
    if it's a known village instead, routes through the village flow.
    Falls back to the port flow when classification is ambiguous —
    consistent with historical behaviour for any pre-village caller.
    """
    kind = _classify_destination(destination)
    if kind == "village":
        logger.info(f"[dest-dispatch] {destination!r} → village navigation")
        return _navigate_world_map_to_village(destination, from_port=from_port)
    # Default: port flow.  Even unknown names fall here so port-search
    # list-scroll can attempt fuzzy resolution.
    logger.info(
        f"[dest-dispatch] {destination!r} → port navigation "
        f"(classified {kind!r})"
    )
    return _navigate_world_map_to_port(destination, from_port=from_port)


# ── Step 8: wait for arrival ─────────────────────────────────────────────────

def _dismiss_arrival_overlay(frame: Image.Image) -> None:
    """
    Dismiss all overlays blocking the port overworld after arrival.

    Multiple popups can be stacked: e.g. daily news popup on top of a perk/event
    popup, with a discovery notice behind both.  Each dismiss attempt peels off
    the topmost layer.  We loop until the screen is clean (port name readable,
    right panel visible, or sea HUD present) or we exhaust attempts.

    Per-pass strategy:
      1. If it looks like a system/event popup (has_system_notice) → use the
         dedicated X-button dismiss path (more reliable than generic find_button).
      2. Otherwise look for explicit dismiss buttons (OK, Close, X, Got it).
      3. Blind fallback: tap top-right corner then centre.
    """
    logger.info("  Dismissing arrival overlay(s)…")

    for attempt in range(6):  # up to 6 layers (generous — rarely more than 2)
        # Re-check every pass with a fresh frame
        if attempt > 0:
            time.sleep(1.2)
            frame = capture_screen()

        # Done when the overworld or sea is readable again
        loc = where_am_i(frame)
        if loc["location"] not in ("sea_cinematic", "unknown"):
            logger.info(f"    Overlay cleared after {attempt} tap(s) — location={loc['location']!r}")
            return

        # Pass 1: proper popup (system notice, daily news, event) → interruptor system
        from brain.perceive import dismiss_interruptors
        frame2 = dismiss_interruptors(frame)
        if frame2 is not frame:
            logger.info(f"    Pass {attempt+1}: interruptors dismissed")
            frame = frame2
            continue

        # Pass 2: explicit dismiss button (OK, Got it, Close, discovery confirm)
        btn = _find_button(frame, "ok", "close", "confirm", "got it", "x")
        if btn:
            logger.info(f"    Pass {attempt+1}: dismiss button @ {btn}")
            tap(*btn)
            continue

        # Pass 3: blind taps — top-right (X icons) then centre (translucent banners)
        logger.info(f"    Pass {attempt+1}: blind taps (top-right + centre)")
        tap(frame.width - 80, 80)
        time.sleep(0.5)
        tap(frame.width // 2, frame.height // 2)

    # Final diagnostic
    loc = where_am_i()
    logger.warning(f"    Could not fully clear overlays — location={loc['location']!r} — {loc['detail']}")


def _wait_for_arrival(
    destination: str,
    timeout: float = 900.0,
    check_interval: float = 30.0,
) -> bool:
    """
    Poll where_am_i() until we land on destination's port overworld.

    Strategy:
      - Anti-idle tap every 20s (keeps game alive, also wakes idle cinematic)
      - where_am_i() check every check_interval seconds (default 30s)
      - All transient states (loading, arrival overlay, idle cinematic) are
        transparent — they self-resolve into 'port_overworld' on the next poll
      - System notices are dismissed immediately when detected

    check_interval: increase for very long voyages to reduce OCR overhead.
    timeout: generous upper bound (15 min default; a 5-day voyage is ~real 30min).
    """
    # Sanity: world map should be closed before we enter this function.
    if where_am_i()["location"] == "world_map":
        logger.warning("World map still open at start of _wait_for_arrival — sailing may not have started")

    logger.info(f"Waiting for arrival at {destination!r} "
                f"(timeout={int(timeout)}s, check_interval={int(check_interval)}s)")
    dest_lower    = destination.lower().strip()
    deadline      = time.time() + timeout
    last_tap              = time.time()
    last_check            = 0.0          # force an immediate first check
    zero_speed_count      = 0            # consecutive speed=0.0 readings at sea
    post_loading_cinematic = 0           # consecutive sea_cinematic readings after loading
    ANTI_IDLE             = 20.0
    STALL_THRESHOLD       = 2
    current_interval      = check_interval

    while time.time() < deadline:
        now = time.time()

        # ── Anti-idle tap (also wakes idle cinematic and dismisses arrival overlay) ──
        if now - last_tap >= ANTI_IDLE:
            from actions import ui as _ui
            _ui.tap_centre(why="anti-idle / wake the cinematic")
            last_tap = now
            time.sleep(1.0)

        # ── Periodic location check ───────────────────────────────────────────
        if now - last_check >= current_interval:
            loc = where_am_i()
            last_check = now
            location   = loc["location"]
            port       = loc["port"]
            detail     = loc.get("detail", "")

            logger.info(f"  where_am_i → {location!r}  port={port!r}  ({detail})")

            if location == "port_overworld":
                if port and dest_lower[:5] in port.lower():
                    logger.info(f"Arrived at {destination!r}")
                    # Phase 3.3: arrival invalidates the Moondream family cache.
                    # The bot just transitioned from at_sea → port_overworld;
                    # the next ambiguous-chrome tick needs a fresh family probe.
                    try:
                        from brain import moondream_family_cache as _mfc
                        _mfc.invalidate("arrival")
                    except Exception as e:
                        logger.debug(f"[arrival] moondream cache invalidate failed: {e}")
                    return True
                elif port:
                    logger.warning(f"  On overworld of {port!r}, not {destination!r} — waiting")
                current_interval = check_interval

            elif location == "loading":
                logger.info("  Loading screen — switching to fast poll (5s) for arrival")
                current_interval = 5.0
                zero_speed_count = 0
                post_loading_cinematic = 0

            elif location == "sea_cinematic":
                if current_interval < check_interval:
                    # We were in fast-poll mode (post-loading) — this is likely the
                    # arrival overlay/discovery notice blocking the port overworld.
                    post_loading_cinematic += 1
                    logger.info(f"  Arrival overlay still showing "
                                f"({post_loading_cinematic} consecutive) — dismissing")
                    _dismiss_arrival_overlay(capture_screen())
                else:
                    post_loading_cinematic = 0

            elif location == "sea":
                post_loading_cinematic = 0
                # Check ship speed — 0.0 means stalled at a waypoint
                speed = _read_sea_speed(capture_screen())
                if speed is not None:
                    logger.info(f"  Ship speed: {speed} kn")
                    if speed == 0.0:
                        zero_speed_count += 1
                        logger.info(f"  Speed = 0.0 ({zero_speed_count}/{STALL_THRESHOLD})")
                    else:
                        zero_speed_count = 0
                else:
                    logger.debug("  Speed unreadable — skipping stall check")

                if zero_speed_count >= STALL_THRESHOLD:
                    logger.warning(
                        f"  Ship speed 0.0 for {zero_speed_count} consecutive checks — "
                        f"stalled at sea waypoint; re-navigating to {destination!r}"
                    )
                    zero_speed_count = 0
                    if _open_world_map_from_sea(capture_screen()):
                        if _navigate_world_map_to_port(destination):
                            last_check = 0.0
                        else:
                            logger.warning("  Re-navigation failed — will retry next cycle")
                    else:
                        logger.warning("  Could not open world map for re-navigation")

                if current_interval < check_interval:
                    pass   # keep fast poll after loading screen

            elif location == "world_map":
                logger.warning("  World map detected during sailing — pressing back")
                press_back()
                time.sleep(2.0)
                last_check = 0.0

            # "building", "unknown" → just wait

            # Dismiss any popups detected during sailing
            from brain.perceive import dismiss_interruptors
            notice_frame = capture_screen()
            new_frame = dismiss_interruptors(notice_frame)
            if new_frame is not notice_frame:
                logger.info("  Interruptor dismissed during sailing")
                last_tap = now

        time.sleep(3.0)

    logger.warning(f"Arrival at {destination!r} not confirmed within {int(timeout)}s")
    return False


# ── Public API ────────────────────────────────────────────────────────────────

def _navigate_sea_to_destination(
    destination: str,
    from_port: Optional[str] = None,
    timeout: float = 900.0,
) -> bool:
    """
    Phase 2 of sailing: from confirmed sea state, navigate to destination and arrive.

    Only called after successful departure — the bot must already be at sea.
    Handles: dismiss popups → open world map → select destination → auto-sail → arrive.

    Returns True on successful arrival, False on failure.
    """
    frame = capture_screen()
    frame = _handle_post_departure_sea(frame)

    if not _open_world_map_from_sea(frame):
        logger.error("Could not open world map from sea")
        return False
    time.sleep(1.0)

    if not _navigate_world_map_to_port(destination, from_port=from_port):
        logger.error(f"Could not navigate to {destination!r} on world map")
        press_back()
        return False

    arrived = _wait_for_arrival(destination, timeout=timeout)
    if arrived:
        logger.info(f"=== Arrived at {destination!r} ===")
    else:
        logger.warning(f"=== Arrival at {destination!r} not confirmed ===")
    return arrived


def sail_to_port(
    destination: str,
    from_building: bool = True,
    arrival_timeout: float = 900.0,
) -> bool:
    """
    Sail from current location to *destination* port.

    Args:
        destination:     Port to sail to (e.g. "Aceh", "Calicut").
        from_building:   True if currently inside a building.
                         False if already on port overworld.
        arrival_timeout: Seconds to wait for arrival (default 15 min —
                         long voyages can take several in-game days).
    """
    logger.info(f"=== Sailing to {destination!r} ===")

    # 0. Already there?  (common when task loop re-enters the same port)
    current_port: Optional[str] = None
    if not from_building:
        loc = where_am_i()
        current_port = loc.get("port")
        loc_type = loc["location"]

        if loc_type == "port_overworld" and current_port:
            if destination.lower()[:5] in current_port.lower():
                logger.info(f"Already at {destination!r} — skipping sail")
                return True

        # Inside a building (e.g. market left open after a buy_all crash).
        # Exit to overworld before attempting harbour navigation — there is no
        # point calling navigate_to_building('harbor') from inside the market.
        if loc_type == "building":
            bld = loc.get("detail", "")
            logger.info(
                f"  Currently inside a building ({bld!r}) — "
                "exiting to overworld before sailing"
            )
            if not exit_to_overworld():
                logger.error("Could not exit building to reach port overworld")
                return False
            time.sleep(1.0)
            loc2 = where_am_i()
            loc_type     = loc2["location"]
            current_port = loc2.get("port") or current_port

        # Already at sea (e.g. previous sail step failed after departure).
        # Skip harbour/depart entirely — open world map directly.
        # If _open_world_map_from_sea then discovers we're actually at
        # port_overworld (game auto-returned due to supply-out), fall through
        # to the normal departure path below rather than giving up.
        if loc_type in ("sea", "sea_cinematic"):
            logger.info(
                f"Already at sea — skipping harbour/depart, "
                f"navigating world map directly to {destination!r}"
            )
            result = _navigate_sea_to_destination(
                destination, from_port=current_port, timeout=arrival_timeout
            )
            if result:
                return True
            # Sea navigation failed — possibly game auto-returned to port.
            # Re-read location and fall through to normal departure path.
            loc2 = where_am_i()
            loc_type = loc2["location"]
            current_port = loc2.get("port") or current_port
            logger.info(f"  Sea nav failed — re-read location: {loc_type!r} @ {current_port!r}")
            if loc_type in ("sea", "sea_cinematic"):
                return False  # still at sea but can't navigate → give up

    # 1. Exit to overworld
    if from_building:
        if not exit_to_overworld():
            logger.error("Could not reach port overworld")
            return False
        time.sleep(1.0)

    # current_port may be None if we entered via from_building=True (no where_am_i call above).
    # It's only used for directional world map panning — best-effort, not required.
    # Avoid a redundant capture here; sail_to_port already called where_am_i() above.

    # ── Phase 1: Port preparation + departure ──────────────────────────────────

    # 2. Navigate to Harbour
    if not _navigate_to_harbour():
        logger.error("Could not enter Harbour")
        return False
    time.sleep(0.5)

    # 3. Pre-departure readiness check (crew, supplies) — resolve blockers
    if not _ensure_fleet_ready():
        logger.error("Fleet not ready for departure — could not resolve blockers")
        return False

    # 4. Depart (tap Depart Now + wait through loading screens)
    if not _depart_from_harbour():
        logger.error("Departure failed")
        return False

    # ── Phase 2: Sea navigation ──────────────────────────────────────────────
    return _navigate_sea_to_destination(
        destination, from_port=current_port, timeout=arrival_timeout
    )


# ── Depart from PORT by picking the destination on the world map ──────────────

# Text that identifies the departure confirmation the game raises after a destination is
# chosen from a port. The dialog is OURS — the bot asked for this move — so it must be
# COMPLETED, not dismissed (CLAUDE.md dialog rule).
_DEPART_NOTICE_MARKERS = ("auto supply", "continue")


def _looks_like_departure_notice(text: str, destination: str) -> bool:
    """True when this frame's text is the 'Moving to X after Auto Supply. Continue?' notice.

    Matched on the Auto-Supply wording rather than on the word 'Notice' alone, because
    'Notice' titles several unrelated popups and tapping OK on the wrong one is exactly
    the class of mistake that closed the game once already.
    """
    t = (text or "").lower()
    if not all(m in t for m in _DEPART_NOTICE_MARKERS):
        return False
    # The destination should be named in the dialog. Fold the accent so 'Malé' matches a
    # 'Male' request (and vice versa) — the same folding the port lookup uses.
    from actions.world_map_nav import fold_name
    return fold_name(destination.split()[0]) in fold_name(t)


def confirm_departure_notice(destination: str, *, attempts: int = 3) -> dict:
    """Tap OK on the auto-supply departure confirmation, if it is up.

    Returns {seen, confirmed, reason}. `seen=False` is NOT a failure — the game only
    raises this dialog when "Do not show for a day" is unchecked.

    Why this is a named step rather than something the recovery layer absorbs: the
    perception layer deliberately REFUSES to auto-confirm dialogs it identified through
    OCR ("Semantic dismissal 'tap_ok' — learning only, leaving action to caller"), because
    a wrong OK is unrecoverable. Confirming is therefore the caller's job, and the caller
    is the one that knows it just asked to sail to `destination`. Live 2026-08-21: without
    this the run looped on the Malé notice for 70s, re-consulting Qwen every 8s and never
    tapping anything.
    """
    for i in range(attempts):
        frame = capture_screen()
        text = " ".join((t or "") for t, _c, _x, _y in _ocr_frame(frame, min_conf=0.3))
        if not _looks_like_departure_notice(text, destination):
            if i == 0:
                logger.info("[depart] no auto-supply notice on screen — nothing to confirm")
                return {"seen": False, "confirmed": False, "reason": "no notice"}
            return {"seen": True, "confirmed": True, "reason": "notice cleared"}

        logger.info(f"[depart] auto-supply notice for {destination!r} — confirming (OK)")
        from actions import ui
        if not ui.tap_text(frame, "ok", why=f"confirm departure to {destination}"):
            logger.warning("[depart] the notice is up but its OK button was not found")
            return {"seen": True, "confirmed": False, "reason": "OK button not found"}
        ui.settle("dialog")

    return {"seen": True, "confirmed": False, "reason": f"notice still up after {attempts} taps"}


def depart_from_port_via_world_map(destination: str, *, settle_s: float = 8.0,
                                   motion_wait_s: float = 45.0,
                                   max_retries: int = 2) -> dict:
    """Set sail from a PORT by choosing the destination on the world map.

    This is the short path, and the safe one for supply. Selecting Move to City / Move to
    Village from inside a port makes the game do the whole departure itself: the player
    runs to the harbour, the fleet is SUPPLIED automatically, and it sets sail. Picking the
    destination at sea instead costs a separate harbour trip and leaves the fleet burning
    supply while the bot pans the map (user 2026-08-21). The world-map operation is
    identical either way — only the consequence differs.

    Two things go wrong in practice, and neither announces itself, so both are checked:

      1. **The fleet does not leave.** The destination is accepted but the player stays in
         port. The fix is to walk to the harbour and tap Supply Departure by hand.
      2. **It leaves but never moves** — a game bug where the fleet is at sea with speed 0
         and the ETA never falls. The fix is to select the destination AGAIN, which kicks
         it into motion.

    Returns {ok, reason, departed_via, retries}."""
    from actions.route_execution import is_moving
    from actions.world_map_nav import fold_name

    for attempt in range(max_retries + 1):
        here = where_am_i()
        loc = here.get("location")

        # ALREADY THERE. Selecting the port you are standing in is a no-op: the world map
        # closes straight back to the same overworld, `_wait_until_at_sea` sees "still in
        # port", and the failure-1 path fires a manual Supply Departure — which puts the
        # fleet to sea WITH NO DESTINATION. That is how the 2026-08-21 run left the fleet
        # drifting off Malé at speed 0 having bought nothing: the mission's first gather
        # node was `gather:Male` and the fleet was already docked at Malé.
        # Departing is not the goal — BEING at the destination is, and we are.
        # Folded compare because the mission carries 'Male' while the port reads 'Malé'.
        here_port = here.get("port")
        if loc in ("building", "sub_menu") and not here_port:
            # Inside a building the port name is not on screen, but the bot still KNOWS
            # where it is — `last_known_settlement` is carried across ticks and persisted
            # to disk for exactly this. Without it, "am I already there?" answers "no"
            # from inside a Market and the mission sails to the port it is standing in.
            # Live 2026-08-21: gather:Jakarta ran while the fleet sat in Jakarta's Market
            # Purchase screen — the one place it needed to be — and instead of buying, it
            # tried to exit, open the world map and sail to Jakarta, failed to get out of
            # the building, escalated to the teaching loop and aborted after 600s.
            try:
                from brain import observation as _obs
                cur = _obs.current()
                here_port = (cur.last_known_settlement if cur else None) \
                    or _obs._ensure_persisted_loaded()
            except Exception as exc:
                logger.debug(f"[depart] could not resolve the settlement from inside a "
                             f"building ({type(exc).__name__}: {exc})")

        if loc in ("port_overworld", "building", "sub_menu"):
            if here_port and fold_name(here_port) == fold_name(destination):
                logger.info(f"[depart] already at {here_port!r} (state={loc!r}) — "
                            "no sailing needed")
                return {"ok": True, "reason": "already at the destination",
                        "departed_via": "no departure needed", "retries": attempt}

        if loc in ("sea", "sea_cinematic"):
            # Already at sea (a retry, or we were never in port) — go straight to the
            # motion check rather than re-running the harbour flow.
            moved, hud = _confirm_making_way(motion_wait_s, is_moving)
            if moved and not _bound_elsewhere(hud, destination):
                return {"ok": True, "reason": "under way", "departed_via": "already at sea",
                        "retries": attempt}
        else:
            if not open_world_map():
                return {"ok": False, "reason": "could not open the world map",
                        "departed_via": None, "retries": attempt}
            if not _navigate_world_map_to_destination(destination):
                return {"ok": False, "reason": f"could not select {destination!r} on the map",
                        "departed_via": None, "retries": attempt}
            # The game asks to confirm before it runs to the harbour: "Moving to X after
            # Auto Supply. Continue?". Nothing happens until this is answered.
            notice = confirm_departure_notice(destination)
            if notice["seen"] and not notice["confirmed"]:
                return {"ok": False,
                        "reason": f"could not confirm the departure notice: {notice['reason']}",
                        "departed_via": None, "retries": attempt}

            logger.info(f"[depart] {destination!r} selected — the game should now run to "
                        "the harbour, supply, and sail")
            time.sleep(settle_s)

            # FAILURE 1: still ashore. The auto-departure did not happen.
            if not _wait_until_at_sea(settle_s):
                logger.warning("[depart] still in port after selecting the destination — "
                               "departing by hand via Supply Departure")
                if _depart_from_harbour():
                    # A hand-fired Supply Departure leaves the port but does NOT carry a
                    # destination with it, so "moving" is not enough — check WHERE it is
                    # headed (user 2026-08-21). A mismatch falls through to the retry,
                    # which re-selects the destination from the map.
                    moved, hud = _confirm_making_way(motion_wait_s, is_moving)
                    if moved and not _bound_elsewhere(hud, destination):
                        return {"ok": True, "reason": "under way after a manual departure",
                                "departed_via": "supply_departure", "retries": attempt}
                    logger.warning("[depart] departed by hand but the fleet is not making "
                                   f"way toward {destination!r}")
                else:
                    logger.warning("[depart] manual Supply Departure did not work either")
                continue

            # FAILURE 2: at sea, but going nowhere — or going somewhere else.
            moved, hud = _confirm_making_way(motion_wait_s, is_moving)
            if moved and not _bound_elsewhere(hud, destination):
                return {"ok": True, "reason": "under way", "departed_via": "auto",
                        "retries": attempt}
            logger.warning(f"[depart] at sea but not making way toward {destination!r} — "
                           f"re-selecting it to set the fleet going "
                           f"(attempt {attempt + 1}/{max_retries + 1})")

    return {"ok": False, "reason": f"{destination!r} selected but the fleet never got "
                                   f"under way after {max_retries + 1} attempts",
            "departed_via": None, "retries": max_retries + 1}


def _bound_elsewhere(hud: dict, destination: str) -> bool:
    """True only when the HUD NAMES a different port than the one we asked for.

    Deliberately narrow. The destination readout cannot be used to confirm a departure
    worked — the game's own bug is that it shows the destination as set while the tap had
    no effect (user 2026-08-21), so a matching name proves nothing and movement is what
    decides. A blank or unreadable destination proves nothing either, and must NOT
    override a fleet that is demonstrably making way.

    What it does catch is the one case motion alone gets wrong: a hand-fired Supply
    Departure leaves port carrying NO destination, and `is_moving` can still read as true
    off a ticking day-at-sea. If the HUD names somewhere we did not ask for, re-select.
    """
    from actions.world_map_nav import fold_name
    shown = (hud or {}).get("destination")
    if not shown:
        return False
    if fold_name(shown) == fold_name(destination):
        return False
    logger.warning(f"[depart] the fleet is bound for {shown!r}, not {destination!r}")
    return True


def _wait_until_at_sea(settle_s: float) -> bool:
    """True once perceive reports the fleet at sea. The auto-departure includes a walk to
    the harbour and a loading screen, so this waits rather than judging on one frame."""
    deadline = time.time() + max(20.0, settle_s * 3)
    while time.time() < deadline:
        loc = where_am_i().get("location")
        if loc in ("sea", "sea_cinematic"):
            return True
        if loc == "loading":
            time.sleep(3.0)
            continue
        time.sleep(random.uniform(2.0, 3.5))
    return False


def _confirm_making_way(motion_wait_s: float, is_moving_fn):
    """(moving, hud_after) — whether the fleet is genuinely MOVING, not merely at sea.

    Two HUD reads separated by enough time for a game-day to tick: motion shows up as a
    falling ETA or a rising day-at-sea. A selected destination is not evidence of movement
    — that is the whole point of the speed-0 bug: the game will happily DISPLAY the
    destination while the tap that set it did nothing at all, and the only cure is to
    re-open the world map and set it again. So movement is the decisive test here and the
    destination readout is not; see `_bound_elsewhere` for the narrow thing it IS good for.

    Returns the post-wait HUD too, so callers can inspect the destination without paying
    for another OCR pass."""
    before = read_sea_hud()
    speed = before.get("speed")
    time.sleep(max(20.0, motion_wait_s))
    after = read_sea_hud()
    if is_moving_fn(before, after):
        logger.info(f"[depart] confirmed under way (eta {before.get('eta_days')}→"
                    f"{after.get('eta_days')}d)")
        return True, after
    logger.info(f"[depart] no progress in {motion_wait_s:.0f}s "
                f"(speed={speed}→{after.get('speed')}, eta={before.get('eta_days')}→"
                f"{after.get('eta_days')})")
    return False, after
