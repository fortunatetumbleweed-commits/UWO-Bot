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


# NOTE: there is a SECOND matcher in this module, `_map_label_matches` — fuzzy, returns a
# float, for map labels sitting under a discovery icon. It was called `_label_matches` too,
# and being defined later it SHADOWED this one at import time, so `_find_button` below — the
# bot's core button finder — was silently running fuzzy map semantics with its two arguments
# REVERSED (this one takes (target, candidate); that one takes (label, target)). Two test
# files each tested a different function under the one name; the map file passed because it
# was testing the shadow. Keep the names distinct.
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


# Both transition screens are built the same way (frames labelled 2026-05-24 / 2026-04-16):
#
#   ARRIVAL      big "City"    top-left, subtitle "Entering..."
#   DEPARTURE    big "Sailing" top-left, subtitle "Preparing for Voyage..."
#
# with an info card on the right, a tip line along the bottom and a progress percentage in the
# bottom-right corner. The game is inconsistent about which one it shows — sometimes both
# appear before a voyage — but either one means the same thing: the OVERWORLD IS IN
# TRANSITION, so nothing on screen is actionable and the bot must wait and re-perceive
# afterwards (user, 2026-08-25).
# ONLY THE SUBTITLE WORDS. The big title ("City", "Sailing") is NOT usable: the sea HUD
# prints "14 Days of Sailing Left" in this same corner, so matching "sailing" made the bot
# read open sea as a transition screen — it stopped knowing where it was, re-selected the
# port it had just reached, and looped on departure (live 2026-08-25, my own regression).
# "Entering" / "Preparing for Voyage" / "Loading" appear on nothing else.
_LOADING_TITLE_WORDS = ("entering", "preparing", "loading")
# The title block occupies the top-left corner; nothing else is looked at.
_LOADING_TITLE_FRAC_X = 0.34
_LOADING_TITLE_FRAC_Y = 0.20


def _is_loading_screen(frame: Image.Image) -> bool:
    """True when a transition screen is up (arriving at a port, or leaving for sea).

    Read from the TOP-LEFT TITLE BLOCK, not from text anywhere on screen. The whole-frame
    keyword version had already misfired once — "voyage" had to be removed because NPC speech
    bubbles say "Bon voyage!" — and speech bubbles carry arbitrary sentences, so any of these
    words could appear in one and halt the bot on a screen that is not loading. The title is
    what identifies these screens, exactly as it identifies every other chromed screen.
    """
    w, h = frame.width, frame.height
    corner = int(w * _LOADING_TITLE_FRAC_X), int(h * _LOADING_TITLE_FRAC_Y)
    words = [t.lower() for t, _c, x, y in _ocr_frame(frame)
             if x <= corner[0] and y <= corner[1]]
    hit = [word for word in words
           if any(fuzzy_contains(word, kw) for kw in _LOADING_TITLE_WORDS)]
    if hit:
        logger.info(f"  Loading screen: title block reads {hit} — the overworld is in "
                    "transition")
        return True
    return False


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
# THE SUPPLY LINE, AND NOTHING ABOVE IT. This started at y=140, which reaches into the row
# of badges over it — measured on a live sea frame the crop caught the shield's '20' at
# y=156, directly before the supply digits, giving OCR '20 15 days of sailing left'.
#
# `read_sea_hud` matches (\d+) immediately before "days", so any stray number that merges
# with the real one is swallowed whole: live 2026-08-29 the same fleet reported 15, 115, 415
# and 415 days on one voyage. 15 was always the right answer — the HUD says "15 Days of
# Sailing Left" — and the extra leading digit came from above the line.
#
# Tightening the crop was the wrong instrument — it clips the glyph tops and OCR fragments
# the line ('15 Days of =' / 'Sailing "' / 'Left'). The tokens carry POSITIONS, and the badge
# is on a different ROW, so the fix is to read row by row instead of joining the whole crop
# into one string. See `_hud_rows`.
_HUD_SUPPLY_CROP = (0,   140, 600,  320)
# Destination + ETA: bottom-centre strip
_HUD_ETA_CROP    = (700, 920, 1700, 1080)


# HUD TEXT IS LAID OUT IN ROWS, and a row is the unit of meaning. Joining a whole crop into
# one string puts a badge from the line above directly in front of the number below it —
# live 2026-08-29 that produced '20 15 days of sailing left', and a greedy (\d+) before
# "days" swallowed both. Reading row by row keeps each line's numbers to itself.
_HUD_ROW_TOL = 24


def _search_elements(elements, pattern):
    """First match of `pattern` in any ONE OmniParser label.

    Per element, never across two: an element is a rendered line, so a number in one cannot
    run into the words of another. That is the whole reason to prefer this over a crop.
    """
    import re
    for e in elements or []:
        label = (getattr(e, "label", "") or "").strip().lower()
        if not label:
            continue
        m = re.search(pattern, label)
        if m:
            return m
    return None


def _hud_rows(tokens) -> list:
    """OCR tokens grouped into rows, each row ordered left to right."""
    rows: list = []
    for text, _conf, cx, cy in sorted(tokens, key=lambda t: (t[3], t[2])):
        for row in rows:
            if abs(row[0] - cy) <= _HUD_ROW_TOL:
                row[1].append((cx, text))
                break
        else:
            rows.append((cy, [(cx, text)]))
    return [" ".join(t for _x, t in sorted(items)).lower() for _cy, items in rows]


def _search_rows(rows, pattern):
    """First match of `pattern` in any row — never across two of them."""
    import re
    for row in rows:
        m = re.search(pattern, row)
        if m:
            return m
    return None


def read_sea_hud(frame=None, *, elements=None) -> dict:
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
    #
    # TWO READS, COMBINED — because they fail on different things.
    #
    # OmniParser returns the supply line as ONE element, with the badge above it as another:
    #     x=212 y=156 '20'   ·   x=216 y=206 '15 Days of Sailing Left'
    # so the badge can never join the number. It also drops the small digit in 'Day 1',
    # returning a bare 'Day'. The crop OCR is the opposite: it reads 'day 1' fine, and it is
    # what fused '20' onto '15' to report 415 and 115 days on one voyage (live 2026-08-29).
    #
    # So OmniParser is authoritative for supply and the crop fills in the day — the same
    # split the Village Info trade list needed, where the whole frame lost a '44' the crop
    # caught. `parse_fast_cached` means the element read is usually already paid for: the
    # classifier parses this frame anyway, and the sea is the hottest loop in the bot.
    supply_rows = _hud_rows(_ocr_frame(frame.crop(_HUD_SUPPLY_CROP), min_conf=0.25))

    if elements is None:
        try:
            from vision.omniparser import parse_fast_cached
            elements = list(parse_fast_cached(frame))
        except Exception as exc:
            logger.debug(f"[sea-hud] OmniParser unavailable, crop only: {exc}")
            elements = []
    m = _search_elements(elements, r'(\d+)\s*days?\s*of\s*sailing')
    if m is None:
        m = _search_rows(supply_rows, r'(\d+)\s*days?\s*of\s*sailing')
    if m:
        result["supply_days"] = int(m.group(1))

    m = _search_rows(supply_rows, r'day\s*(\d+)')
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


def tap_exit_to_overworld(frame=None) -> dict:
    """ONE tap toward the port overworld, using the GAME's own controls. Returns immediately.

    The in-game HOME button leaves any building in a single tap, and the chromed title's BACK
    ARROW goes up exactly one level. Both are drawn by the game, so neither can leave the app
    — which the Android back key can, and did: `exit_to_overworld` pressed back in a loop and
    carried a whole mechanism to notice and dismiss the "exit game?" dialog it caused, then
    escalated into `recover_to_port_overworld` when that got stuck. All of that existed to
    survive a control the game never intended us to use.

    Returns {tapped, control, position}. `tapped` means a control was pressed, NOT that the
    world changed — the caller perceives and decides, per the task-runner contract.
    """
    from vision.chrome_detector import get_chrome_detector

    frame = frame if frame is not None else capture_screen()
    state = get_chrome_detector().detect(frame)
    positions = getattr(state, "positions", {}) or {}

    # HOME FIRST: one tap out of any depth. The title arrow is for stepping up a single level
    # when the caller wants to stay inside the building.
    for control in ("home", "back_arrow"):
        pos = positions.get(control)
        if pos:
            logger.info(f"[exit] tapping the game's {control} @ {pos}")
            tap(*pos)
            return {"tapped": True, "control": control, "position": pos}

    logger.info("[exit] no game exit control on screen — nothing tapped")
    return {"tapped": False, "control": None, "position": None}


def exit_to_overworld(timeout: float = 90.0) -> bool:
    """Tap the game's Home button until the port overworld is confirmed.

    Still a loop, and still blocking, because twenty-odd callers depend on that today. What
    it waits on is its OWN effect — "did Home get me out?" — which is the only question a
    primitive's loop may ask.

    What it no longer does is escalate into `brain.recovery.recover_to_port_overworld` when
    no exit control appears. That call is not a bigger version of this one: its sea branch
    SAILS THE FLEET to a home port. Reached from here it meant that failing to close a market
    panel could put to sea — and the two functions could each re-enter the other with no
    shared budget. Leaving a panel and repositioning the fleet are different sizes of decision
    and only the task knows whether the second one is wanted.

    So when there is no exit control, the screen is covered by something. Clearing what covers
    it IS this function's business (it needs that control); deciding where the bot should be
    is not. After two looks it reports and returns.
    """
    logger.info("Exiting to port overworld…")
    deadline = time.time() + timeout
    unchanged = 0
    clears = 0
    MAX_CLEARS = 2      # bounded: a clear that keeps "succeeding" is not making progress

    while time.time() < deadline:
        frame = capture_screen()

        if _is_on_overworld(frame):
            logger.info("Port overworld confirmed")
            return True

        if tap_exit_to_overworld(frame)["tapped"]:
            unchanged = 0
        else:
            unchanged += 1
            if clears < MAX_CLEARS:
                # A promo, the daily news or the idle lock is sitting on top of the control.
                clears += 1
                try:
                    from brain.unexpected_dialog import clear_blockers
                    if clear_blockers(frame).get("cleared"):
                        logger.info("  Cleared a blocker covering the exit control")
                        unchanged = 0          # the next look gets a fair try at the control
                        time.sleep(1.0)
                        continue
                except Exception as exc:
                    logger.debug(f"  clear_blockers failed: {exc}")
            if unchanged >= 2:
                logger.warning("  No exit control for two looks and nothing to clear — "
                               "reporting instead of recovering")
                return False
        time.sleep(1.2)

    logger.warning(f"Could not reach the port overworld within {timeout:.0f}s")
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
    hits = _row_only(hits)
    return [(e.cx, e.cy) for e in hits]


# A TAB STRIP IS A ROW, NOT ANY ICON IN THE BAND.
# Live 2026-08-24 at Bordeaux an event popup covered the right panel, and its own close-X at
# (1917,165) sat inside the tab band and was offered as a "tab". Tapping popup furniture
# cannot select the Buildings tab, so the search failed and the caller went on to match the
# word "market" inside a quest line. Real tabs come as three or more similar icons at the
# same height, evenly spaced (~100px apart, measured); two icons 300px apart are not a strip.
_TAB_MIN_ROW = 3
_TAB_SPACING_TOL = 0.45
_TAB_SAME_ROW_PX = 25


def _row_only(hits) -> list:
    """Keep the hits that actually form an evenly spaced row; [] if none do."""
    if len(hits) < _TAB_MIN_ROW:
        if hits:
            logger.info(f"  Only {len(hits)} icon(s) in the tab band — not a tab strip "
                        "(a strip is a row of similar icons); ignoring")
        return []
    ys = sorted(e.cy for e in hits)
    mid_y = ys[len(ys) // 2]
    row = [e for e in hits if abs(e.cy - mid_y) <= _TAB_SAME_ROW_PX]
    if len(row) < _TAB_MIN_ROW:
        logger.info("  Tab-band icons are not at a common height — not a tab strip")
        return []
    gaps = [b.cx - a.cx for a, b in zip(row, row[1:])]
    med = sorted(gaps)[len(gaps) // 2] if gaps else 0
    if med <= 0 or any(abs(g - med) > _TAB_SPACING_TOL * med for g in gaps):
        logger.info(f"  Tab-band icons are unevenly spaced (gaps={gaps}) — not a tab strip")
        return []
    return row


# TWO ICONS CAN BE LIT AT ONCE, AND ONLY ONE GROUP IS A TAB SET.
# In port the strip is: Tasks | Buildings | Players | location-pin. The FIRST THREE are
# mutually exclusive — exactly one is selected — while the LOCATION PIN is INDEPENDENT: it
# toggles the in-port minimap and is lit warm whenever that toggle is on (user, 2026-08-24).
# Measured on a live port frame, the pin was the warmest thing in the strip:
#     scroll/Tasks 17.7 | house/Buildings 46.5 (SELECTED) | person/Players 22.0 | pin 49.4
# So "the warmest icon is the selected tab" picks the toggle and is wrong. Reading the
# mutually exclusive group ALONE makes the highlight decisive again: within it, exactly one
# is lit, and that one is the selected tab.
_TAB_WARM_MIN = 30.0
# A strip whose selected tab is a light panel instead of a gold one: the lit tab must be
# this many times the median of its neighbours AND this far above it. Measured on the
# world map, 2026-08-27: 193.3 against a 56.1 median.
_TAB_BRIGHT_RATIO = 1.8
_TAB_BRIGHT_GAP = 50.0

# The trailing independent toggle is not part of the mutually exclusive tab group.
_TAB_TRAILING_TOGGLES = 1
# A tab tap goes unheard while the world map is still settling — measured live 2026-08-25,
# the same coordinate failed right after the map opened and worked once it had been up a
# while. Retries wait progressively longer rather than moving the tap.
_TAB_TAP_ATTEMPTS = 3
_TAB_SETTLE_S = 1.6


def _brightest_tab(frame, tabs) -> Optional[int]:
    """Index of the one tab far brighter than its neighbours, or None if none stands out.

    For strips whose selected tab is a LIGHT panel rather than a gold one. Relative by
    construction — the comparison is against the median of the other tabs in the same strip
    on the same frame — so it needs no calibrated threshold and rides dimming.
    """
    import statistics

    import numpy as np
    try:
        lums = []
        for (cx, cy) in tabs:
            cell = np.asarray(frame.crop((cx - 35, cy - 28, cx + 35, cy + 28))
                              .convert("L")).astype(float)
            lums.append(float(cell.mean()))
    except Exception as exc:
        logger.debug(f"  tab brightness read failed: {exc}")
        return None
    if len(lums) < 2:
        return None
    top = max(range(len(lums)), key=lambda i: lums[i])
    others = [l for i, l in enumerate(lums) if i != top]
    ref = statistics.median(others)
    if lums[top] >= ref * _TAB_BRIGHT_RATIO and (lums[top] - ref) >= _TAB_BRIGHT_GAP:
        return top
    return None


def selected_tab_index(frame, tabs, *, trailing_toggles: int = None) -> Optional[int]:
    """Index of the currently SELECTED tab in `tabs`, or None if it cannot be told.

    Lets the bot know which tab it is on instead of inferring it from what the list happens
    to contain — a Tasks tab full of quest text reads as a perfectly healthy list.
    """
    if not tabs:
        return None
    # Only the mutually exclusive group can answer "which tab is selected".
    # The PORT strip ends with an independent location-pin toggle that is not part of the
    # mutually exclusive group; the WORLD MAP strip has no such trailing toggle, so callers
    # there pass 0 rather than losing a real tab from the comparison.
    drop = _TAB_TRAILING_TOGGLES if trailing_toggles is None else trailing_toggles
    n_group = len(tabs) - drop if drop and len(tabs) > drop else len(tabs)
    group_tabs = tabs[:n_group]

    # LUMINANCE FIRST (user, 2026-09-02: warmth is not the right measure in this game).
    #
    # A selected tab is a LIGHTER panel; an unselected one is a dark translucent panel with
    # the world showing THROUGH it. So brightness measures the highlight, and warmth (R-B)
    # measures whatever happens to lie behind the tabs that are NOT selected.
    #
    # Live 2026-09-02 the route tail died on that. The Route tab was selected — opaque white,
    # which blocks the map — and scored the LOWEST warmth of the four, because white has
    # R=G=B. Warm terrain under Explore scored 49.9, so warmth named Explore, confidently,
    # three times, and the mission failed with the cargo aboard:
    #
    #     warmth [19.8, 49.9, 7.7, 13.2]  -> Explore   WRONG
    #     luma   [70.6, 64.4, 186.2, 49.0] -> Route    right, by 2.6x
    #
    # Warmth was already known to be the wrong style here — "this strip lights white, not
    # gold" — and the brightness fallback existed. It just ran only when warmth said "cannot
    # tell", and a fallback for UNCERTAINTY cannot save you from a confident wrong answer.
    # Asking the better signal first is the whole fix.
    bright = _brightest_tab(frame, group_tabs)
    if bright is not None:
        logger.info(f"  Selected tab is #{bright + 1}/{len(tabs)} by LUMINANCE "
                    "(the selected tab is a lighter panel; the others show the world through)")
        return bright

    # WARMTH, only where luminance declined. The PORT strip lights its tab GOLD — bright, but
    # measured 1.18x its neighbours against the 1.8x that `_brightest_tab` requires, where the
    # map's white tab is 2.89x. So brightness cannot separate that strip and warmth still can
    # (gold 56.3 vs 22.0 / 21.9). It is a HINT there and never an authority: `_ensure_tab`
    # only re-orders which tab it tries first, so a wrong answer costs one tap.
    try:
        import numpy as np
        warmth = []
        for (cx, cy) in tabs:
            cell = np.asarray(frame.crop((cx - 35, cy - 28, cx + 35, cy + 28))
                              .convert("RGB")).astype(float)
            warmth.append(float(cell[..., 0].mean() - cell[..., 2].mean()))
    except Exception as exc:
        logger.debug(f"  tab highlight read failed: {exc}")
        return None
    shown = [round(w, 1) for w in warmth]
    group = warmth[:n_group]
    lit = [i for i, w in enumerate(group) if w >= _TAB_WARM_MIN]
    if len(lit) != 1:
        # WARMTH IS ONE HIGHLIGHT STYLE, NOT THE ONLY ONE. The PORT strip lights its tab
        # GOLD, which is what R−B measures. The WORLD MAP strip lights its tab near-WHITE,
        # and white is not warm — so on a Port-tab world map this scored the lit tab LOWEST
        # (warmth: port 0.6 vs route 17.8) and answered "cannot tell", live 2026-08-27.
        # Luminance separates that strip outright: port 193.3 against 48.8 / 60.8 / 56.1.
        #
        # Compared WITHIN the strip on the same frame, so no absolute threshold is involved:
        # one tab must be far brighter than the median of its neighbours.
        logger.info(f"  Cannot tell which tab is selected — luminance declined and "
                    f"{len(lit)} of the {len(group)} mutually exclusive tabs are warm "
                    f"(warmth={shown})")
        return None
    logger.info(f"  Selected tab is #{lit[0] + 1}/{len(tabs)} (warmth={shown}; the trailing "
                "location pin is an independent toggle and is ignored)")
    return lit[0]


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


# How much longer than the building's own name a label may be and still count as a
# substring match — enough for 'the Market' or a trailing glyph, not a sentence.
_NAME_SLACK = 6


# ── The building-entry primitive ──────────────────────────────────────────────
#
# ONE attempt to tap a building's entry, and a report of what happened. It does three things
# that are its OWN effect and nothing else:
#   • selects the Buildings tab, verified by re-reading the list
#   • scrolls the list, to bring a row into view
#   • taps the row (or the port-map icon, or a visible nameplate)
#
# What it must never do is decide the bot is in the wrong PLACE and fix that. The old
# `navigate_to_building` did: it pressed Back on a sub-menu, on a wrong building, on the world
# map, and Home-escaped when Back stopped working. Live 2026-08-22 the fleet was already
# sailing to Melanesian Village; this function read "not the harbour", pressed Back, and
# cancelled a departure that had SUCCEEDED — four times, 18.5 minutes, to undo what the first
# attempt achieved in 65 seconds.
#
# Being in the wrong place is the TASK's problem, because only the task knows what the bot is
# trying to achieve and whether getting there is still worth it.


def _building_row(buildings, target: str):
    """The row that IS this building, or None.

    A BUILDING LABEL IS A NAME, NOT A SENTENCE CONTAINING ONE. `target in lbl` matched the
    quest objective "move to market in ..." when the right panel was showing the Tasks tab
    (live 2026-08-24 at Bordeaux): tapping it handed the fleet to a quest voyage to Jakarta.
    So a substring hit is trusted only when the label is about as long as the name itself
    ("Market", "the Market"), never when the name is buried in running text. `token_sim`
    still catches OCR mangling of the real label.
    """
    def _is_it(lbl: str) -> bool:
        low = lbl.lower().strip()
        if token_sim(lbl, target) >= 0.75:
            return True
        return target in low and len(low) <= len(target) + _NAME_SLACK

    return next(((x, y) for lbl, x, y in buildings if _is_it(lbl)), None)


def _list_signature(buildings) -> tuple:
    """Where the building list is scrolled to — now `actions.ui.lists.signature`.

    Kept as a name because callers and tests use it, but the logic moved: three copies of
    "has this list moved?" existed (here, `explore_actions`, and nowhere at all in the live
    world-map path), so they now all ask one function. See actions/ui/lists.py.
    """
    from actions.ui.lists import signature
    return signature(buildings)


def _scroll_list_for(target: str, frame, *, max_scrolls: int = 4, reset_swipes: int = 4):
    """Rewind the list to the top, then page down, looking for *target*. Returns a tap coord.

    NOW THE SHARED PAGER (actions.ui.lists.find_in_list). The paging, the movement test and
    the rewind all moved there so every list in the game uses one implementation; what stays
    here is what is specific to THIS list — how to read its rows (`read_building_menu`) and
    what counts as a match (`_building_row`, which knows a label is a name and not a sentence
    containing one).

    The lesson that made the column matter is now enforced inside the pager: it swipes down
    the median entry x, because a region-centre swipe can miss the list entirely — the rewind
    then never scrolls, the signature stays unchanged, "at top" is assumed, and a clipped top
    building never comes back (live 2026-08-19, Jakarta).
    """
    from vision.ocr import read_building_menu
    from actions.adb_actions import swipe_fast
    from actions.ui.lists import find_in_list
    from config.settings import BUILDING_MENU_REGION

    rows_now = read_building_menu(frame)
    my = (rows_now[len(rows_now) // 2][2] if rows_now
          else (BUILDING_MENU_REGION[1] + BUILDING_MENU_REGION[3]) // 2)
    fallback_x = (BUILDING_MENU_REGION[0] + BUILDING_MENU_REGION[2]) // 2

    return find_in_list(
        target,
        match=_building_row,
        read_rows=read_building_menu,
        capture=capture_screen,
        swipe=lambda x1, y1, x2, y2: swipe_fast(x1, y1, x2, y2,
                                                duration_ms=300, settle_ms=600),
        fallback_x=fallback_x, y=my,
        max_pages=max_scrolls, rewind_pages=reset_swipes,
        label="building list")


def select_buildings_tab(frame) -> dict:
    """Put the right panel on the Buildings tab. Returns {ok, frame, reason}.

    The tab bar above the minimap toggles Tasks / Buildings / Players, and on arrival the
    Tasks tab can be auto-selected (live 2026-08-19 at Jakarta → empty list → 60s timeout).
    Which index is Buildings varies with how many tabs a port shows, and a wrong guess does
    not fail quietly — it SELECTS another tab. So each candidate is tried and VERIFIED by
    re-reading the list.
    """
    from vision.ocr import read_building_menu

    if _on_buildings_tab(read_building_menu(frame)):
        return {"ok": True, "frame": frame, "reason": "already there"}

    candidates = _tab_strip_candidates(frame)
    if not candidates:
        logger.warning("  Building list not on screen and no tab icons detected")
        return {"ok": False, "frame": frame, "reason": "no tab icons"}

    # Try the tabs we are NOT already on first. The highlight read is a hint, not an authority
    # (the location toggle lights up warm too), so it may only RE-ORDER the attempts.
    already = selected_tab_index(frame, candidates)
    order = ([i for i in range(len(candidates)) if i != already]
             + ([already] if already is not None else []))
    for i in order:
        bx, by = candidates[i]
        logger.info(f"  Trying tab {i + 1}/{len(candidates)} @ ({bx},{by})")
        tap(bx, by)
        time.sleep(1.5)
        frame = capture_screen()
        if _on_buildings_tab(read_building_menu(frame)):
            logger.info(f"  Buildings tab selected @ ({bx},{by})")
            return {"ok": True, "frame": frame, "reason": "selected"}

    # KNOWING IT IS THE WRONG LIST AND TAPPING ANYWAY IS THE WORST OUTCOME.
    return {"ok": False, "frame": frame, "reason": f"none of {len(candidates)} tabs listed buildings"}


def _nameplate_for(target: str, frame):
    """The floating nameplate above a building entrance, if it names *target*.

    It appears once the character has walked to the door, and TAPPING IT ENTERS — far more
    reliable than waiting for auto-entry, which an ambient popup can block.
    """
    from vision.screen_perception import parse_screen
    from vision.element_postprocess import ROLE_BUILDING_NAMEPLATE
    from brain.states.port_map import _canonical_name

    inv = parse_screen(frame, nav_state="port_overworld")
    for t in inv.tagged:
        if t.role != ROLE_BUILDING_NAMEPLATE:
            continue
        lbl = (t.label or "").lower().strip()
        if not lbl:
            continue
        if (target in lbl or (_canonical_name(lbl) or "") == target
                or token_sim(lbl, target) >= 0.7):
            return t
    return None


def _port_map_entry(target: str):
    """Fallback: open the port map and tap the building icon. Returns a tap coord or None."""
    from brain.states.port_map import open_port_map, read_port_map_buildings, close_port_map

    logger.info(f"  {target!r} not in the list — trying the port map")
    if not open_port_map():
        logger.error("  Could not open the port map")
        return None
    time.sleep(0.8)
    hit = next(((name, x, y) for name, x, y in read_port_map_buildings(capture_screen())
                if target in name.lower() or token_sim(name, target) >= 0.75), None)
    if hit is None:
        close_port_map()
        logger.error(f"  {target!r} is not on the port map either")
        return None
    return hit[1], hit[2]


def tap_building_entry(building_name: str, frame=None) -> dict:
    """Tap the way into *building_name*, ONCE. Returns {tapped, via, position, reason}.

    `tapped` means a control was pressed — NOT that the bot is inside. Entering takes a walk
    across the port and there is no local signal separating "walking" from "the tap missed",
    so the caller perceives on its next tick and decides. Per the task-runner contract.
    """
    target = building_name.lower()
    frame = frame if frame is not None else capture_screen()

    plate = _nameplate_for(target, frame)
    if plate is not None:
        logger.info(f"  Nameplate {plate.label!r} @ ({plate.cx},{plate.cy}) — tapping to enter")
        tap(plate.cx, plate.cy)
        return {"tapped": True, "via": "nameplate", "position": (plate.cx, plate.cy), "reason": ""}

    tab = select_buildings_tab(frame)
    if not tab["ok"]:
        # Refusing to tap is the correct outcome. Falling through here used to run the fuzzy
        # match over whatever the panel was showing — at Bordeaux that was the Tasks tab, and
        # the match hit the word "market" inside a quest objective.
        logger.error(f"  Not the building list ({tab['reason']}) — refusing to tap {building_name!r}")
        return {"tapped": False, "via": None, "position": None, "reason": tab["reason"]}
    frame = tab["frame"]

    from vision.ocr import read_building_menu
    rows = read_building_menu(frame)
    logger.info(f"  Building list: {[lbl for lbl, *_ in rows]}")
    pos = _building_row(rows, target) or _scroll_list_for(target, frame)
    via = "list"
    if pos is None:
        pos, via = _port_map_entry(target), "port_map"
    if pos is None:
        return {"tapped": False, "via": None, "position": None, "reason": "not found"}

    logger.info(f"  Tapping {building_name!r} ({via}) @ {pos}")
    tap(*pos)
    return {"tapped": True, "via": via, "position": pos, "reason": ""}


def inside_building(target: str, title: str) -> bool:
    """Does this building's on-screen title name *target*?

    Optimistic when the title is unreadable — the alternative is walking back out of a
    building the bot is very probably standing in. KB variants cover the cases where the
    interior title differs from the canonical name (item_shop reads "Shop"; harbor "Harbour").
    """
    from brain.kb import control as _ckb

    title = (title or "").strip().lower().split(" — ", 1)[0].strip()
    if not title or "unreadable" in title:
        return True
    variants = {target} | {v.lower() for v in _ckb().building_name_variants(target)}
    return (target in title
            or title in variants
            or any(v in title for v in variants if len(v) >= 3)
            or token_sim(title, target) >= 0.65)


def harbor_panel_open(target: str, frame) -> bool:
    """The harbour does NOT open a new screen — it replaces the right-panel list content, so
    `where_am_i` stays 'port_overworld' throughout and only the panel content gives it away."""
    from brain.kb import control as _ckb
    from vision.ocr import read_building_menu

    if target not in {v.lower() for v in _ckb().building_name_variants("harbor")}:
        return False
    labels = {lbl.lower() for lbl, *_ in read_building_menu(frame)}
    return bool(labels & _ckb().harbor_panel_keywords())


def navigate_to_building(building_name: str, timeout: float = 60.0) -> bool:
    """Enter a named building from within a port. True once the bot is inside it.

    Interim shape: still a blocking loop, because thirteen callers depend on that today. What
    it no longer does is RECOVER. Every branch that moved the bot somewhere else is gone —
    Back on a sub-menu, Back out of a wrong building, Back off the world map, the Home-escape
    when Back stopped working, and the learned-recovery plan. Those existed because this
    function was the only scope available when something looked wrong, and each was locally
    reasonable and globally wrong.

    Now: if the bot is not at this port's overworld and not inside the target, this reports
    what it saw and RETURNS. The caller knows what the bot is trying to achieve; it can
    re-plan, sail, or give up. See docs/one_loop_task_drives_state.md.
    """
    from brain.perceive import perceive as _perceive

    target = building_name.lower()
    logger.info(f"Navigating to {building_name!r}…")

    deadline = time.time() + timeout
    tapped_at = 0.0
    # Patience after a tap: the character may have to walk across the port, and a retap issued
    # mid-walk can land INSIDE the destination once the scene loads (on an NPC in the inn, say).
    TAP_RETRY_COOLDOWN = 60.0

    while time.time() < deadline:
        frame = capture_screen()

        # Non-game blockers freeze navigation: the idle lock/screensaver and the graphical
        # promos the interruptor layer does not cover.
        try:
            from brain.unexpected_dialog import clear_blockers
            if clear_blockers(frame).get("cleared"):
                time.sleep(1.0)
                frame = capture_screen()
        except Exception as exc:
            logger.debug(f"  [{building_name}] clear_blockers failed: {exc}")

        if harbor_panel_open(target, frame):
            logger.info("  Harbour panel is open — confirmed by panel content")
            return True

        loc = _perceive(frame).to_location_dict()
        where, detail = loc["location"], loc.get("detail", "")
        logger.info(f"  [{building_name}] state={where!r} — {detail}")

        if where == "building":
            title = detail.replace("building:", "").strip()
            if inside_building(target, title):
                logger.info(f"Inside {building_name!r} — confirmed (screen: {title!r})")
                return True
            if _screen_says_under_way(where, title.lower()):
                # Not a wrong building at all — the fleet is at sea. Naming it precisely is
                # what lets the caller tell "we sailed already" from "we walked in the wrong
                # door", and those want opposite responses.
                logger.info(f"  Screen says {title!r} — the fleet is under way, so "
                            f"{building_name!r} is moot. Leaving the screen alone.")
            else:
                logger.warning(f"  In {title!r}, not {building_name!r} — reporting, not pressing Back")
            return False

        if where == "port_overworld":
            if tapped_at and time.time() - tapped_at < TAP_RETRY_COOLDOWN:
                time.sleep(2.0)                      # still walking — do not retap
                continue
            res = tap_building_entry(building_name, frame)
            if not res["tapped"]:
                # Harbor/Market/Inn/Bureau/Shipyard exist at EVERY port, so "not in this
                # parse" is a perception miss, never absence. Pace and re-read — that is this
                # function waiting on its own read to improve, not on the world to change.
                from brain.kb import control as _kb_control
                if res["reason"] == "not found" and _kb_control().is_always_present(building_name):
                    logger.warning(f"  {building_name!r} not found this pass but it exists at "
                                   "every port — re-reading rather than giving up")
                    time.sleep(random.uniform(0.6, 1.1))
                    continue
                logger.error(f"  Cannot reach {building_name!r} here ({res['reason']})")
                return False
            tapped_at = time.time()
            deadline = max(deadline, tapped_at + timeout)   # the attempt gets its full budget
            time.sleep(2.0)
            continue

        if where == "loading":
            time.sleep(1.5)                          # a transition is in progress
            continue

        # Anywhere else — a sub-menu, the world map, the sea, the main menu. The bot is not
        # where this call assumed, and moving it is the task's decision, not this one's.
        logger.warning(f"  {building_name!r} needs a port overworld; the screen says {where!r} "
                       f"({detail}) — handing back")
        return False

    logger.error(f"Could not enter {building_name!r} within {timeout:.0f}s")
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


def read_fleet_readiness(frame=None) -> dict:
    """Can the fleet depart? A READ of the harbour Departure panel.

    Returns {ready, blocker, on_departure_panel, detail}. Two signals, because either alone
    is wrong: the KB's blocking_signals catch a named blocker in the OCR, and the yellow
    departure button catches an UNNAMED one — if OCR misses the text but no yellow button is
    drawn, the fleet still cannot leave.

    `on_departure_panel` tells apart the two ways this can say "no": the fleet is blocked, or
    the bot is not looking at the departure panel at all. Those want opposite responses —
    resolve the blocker, versus go back to the harbour — and collapsing them is why the old
    version re-navigated after every single resolution attempt.

    Pass a `frame` for a read that touches nothing. Without one it calls
    `_ensure_harbor_top_level`, which steps up out of a harbour sub-menu — that is reaching
    the panel it was asked to read, not navigating.
    """
    from brain.fsm_registry import get_fsm_registry

    frame = _ensure_harbor_top_level() if frame is None else frame
    tokens = _ocr_frame(frame, min_conf=0.3)
    text = " ".join(t.lower() for t, _, _, _ in tokens)

    dep_flow = get_fsm_registry().flows.get("harbor_departure")
    signals = dep_flow._raw.get("blocking_signals", []) if dep_flow else []
    blocker = next((s for s in signals if fuzzy_contains(text, s["text"])), None)
    yellow = _find_yellow_button(frame, x_min=1600)
    on_panel = any(fuzzy_contains(text, t) for t in _DEPART_PANEL_TOKENS)

    if blocker is None and yellow is not None:
        return {"ready": True, "blocker": None, "on_departure_panel": True,
                "detail": "all clear", "text": text, "frame": frame}
    if blocker:
        detail = f"{blocker['text']!r} — {blocker.get('description', '')}"
    else:
        detail = f"no yellow departure button (unknown blocker). OCR: {text[:120]!r}"
    return {"ready": False, "blocker": blocker, "on_departure_panel": on_panel,
            "detail": detail, "text": text, "frame": frame}


def resolve_fleet_blocker(reading: dict) -> dict:
    """Climb ONE rung of the resolution ladder for whatever is blocking departure.

    KB resolution → learned recovery → Claude Vision → the human operator, first match wins.
    Returns {attempted, via, resolved}.

    What it no longer does is re-enter the harbour after each rung and loop three times.
    That loop was a second copy of the one `SailToGoal` already runs: the goal tracks three
    consecutive FLEET_CHECK failures and owns the phase machine that walks back to the
    harbour. Two nested budgets meant nine attempts where the goal thought it had allowed
    three, and the inner one re-navigated on the goal's behalf without being asked.
    """
    from brain.perceive import perceive

    blocker, frame, text = reading["blocker"], reading["frame"], reading["text"]

    if blocker and blocker.get("action") == "resolve":
        from brain.recovery import execute_resolution
        try:
            if execute_resolution(blocker, context="pre_departure_readiness"):
                return {"attempted": True, "via": "kb", "resolved": True}
        except Exception as exc:
            logger.error(f"  execute_resolution raised: {exc}")

    from brain.human_escalation import _match_learned_recovery, _execute_plan
    plan = _match_learned_recovery("building", f"building building: harbor {text}")
    if plan:
        logger.info(f"  Trying learned recovery: {plan.scenario_id!r}")
        _execute_plan(plan)
        time.sleep(2.0)
        return {"attempted": True, "via": f"learned:{plan.scenario_id}", "resolved": True}

    logger.info("  No predefined or learned resolution — asking Claude Vision")
    if _resolve_blocker_with_reasoning(frame, perceive(frame), text):
        return {"attempted": True, "via": "claude", "resolved": True}

    logger.warning("  Claude could not resolve — escalating to the human operator")
    from brain.human_escalation import escalate
    result = escalate(context="fleet cannot depart", perceive_result=perceive(frame))
    return {"attempted": True, "via": "human", "resolved": result.state == "building"}


def _ensure_fleet_ready() -> bool:
    """True when the harbour panel says the fleet can depart.

    One read, and at most one resolution attempt. On a False the caller re-checks on its next
    tick — `SailToGoal` already counts three consecutive FLEET_CHECK failures and owns the
    phase that walks back to the harbour, so the retrying belongs there and only there.
    """
    reading = read_fleet_readiness()
    if reading["ready"]:
        logger.info("  Fleet readiness: all clear")
        return True

    logger.warning(f"  Fleet not ready: {reading['detail']}")
    if not reading["on_departure_panel"]:
        # Nothing to resolve — this is not the departure panel. Resolving a blocker that is
        # not on screen means acting on a screen the bot has not identified.
        logger.warning("  …and this is not the departure panel — reporting, not resolving")
        return False

    res = resolve_fleet_blocker(reading)
    logger.info(f"  Resolution attempt via {res['via']}: resolved={res['resolved']}")
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


def tap_supply_departure(frame=None) -> dict:
    """ONE tap on the harbour's departure button. Returns immediately.

    The action, separated from everything `_depart_from_harbour` wrapped around it: waiting
    out the loading screens, re-navigating when the button is absent, and escalating into
    `recover_to_port_overworld` when a blocker appears. Those are decisions, and decisions
    belong to whoever perceives between them.

    Returns {tapped, blocked, reason}:
      tapped=True   the button was pressed. NOT that the fleet left — the caller perceives.
      blocked=<sig> a blocking signal sits over the harbour panel; nothing was tapped.
      tapped=False  no departure button on this screen (probably not the harbour).
    """
    frame = frame if frame is not None else capture_screen()
    result = _tap_depart_button(frame)

    if isinstance(result, dict):
        # A blocker over the panel. Report it — do NOT escalate into recovery from here:
        # recovery crosses worlds, and a world change is never a primitive's decision.
        logger.warning(f"[depart] departure blocked ({result.get('text')!r})")
        return {"tapped": False, "blocked": result,
                "reason": f"blocked by {result.get('text')!r}"}

    if result == "not_found":
        logger.info("[depart] no departure button on this screen")
        return {"tapped": False, "blocked": None, "reason": "no departure button here"}

    logger.info("[depart] departure button tapped — handing back")
    return {"tapped": True, "blocked": None, "reason": "departure tapped"}


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
    tap_res = tap_supply_departure()
    if tap_res["blocked"]:
        # REPORT, DO NOT RECOVER. This used to call recover_to_port_overworld() from inside
        # the departure, which crosses worlds — a decision the caller must make, and one half
        # of the exit_to_overworld <-> recover_to_port_overworld cycle. The caller perceives a
        # harbour with a blocker over it and decides what that means.
        logger.warning("  Departure blocked despite the fleet readiness check — reporting")
        return False
    result = "not_found" if not tap_res["tapped"] else None
    if result == "not_found":
        # Not in harbour — try to navigate there first
        logger.warning("  Depart button not found on first try — re-navigating to harbour")
        if not _navigate_to_harbour():
            logger.error("  Could not navigate to harbour")
            return False
        again = tap_supply_departure()
        if again["blocked"]:
            logger.warning("  Departure still blocked after re-navigating — reporting")
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

def _world_map_button(tokens) -> Optional[tuple]:
    """(x, y) of the port map's "World Map" globe, from the two words beside each other.

    EasyOCR returns them separately — `World` at (204,1013) and `Map` at (279,1015) — with
    unrelated labels between them in the joined text, so no phrase match finds it. What marks
    the button is the two words sitting side by side on one line.
    """
    words = [(t.lower(), x, y) for t, _c, x, y in tokens]
    for t1, x1, y1 in words:
        if t1 != "world":
            continue
        for t2, x2, y2 in words:
            if t2 == "map" and abs(y2 - y1) <= 20 and 0 < (x2 - x1) <= 160:
                return ((x1 + x2) // 2, y1)
    return None


def _is_on_port_map(frame: Image.Image, *, tokens=None) -> bool:
    """True when the PORT MAP is open — the city plan, not the world map.

    Identified by the one thing it has and the world map has not: its own globe button
    labelled "World Map", at the bottom left, which is the way out to the real world map.

    THE CLASSIFIER CANNOT ANSWER THIS ONE. The family CNN's classes are sea / port_overworld /
    world_map / chromed / transient — `port_map` is not among them, and it reads a port map as
    a port_overworld at confidence 1.00, short-circuiting before any fingerprint gets to
    disagree. So this is the exception to "ask the classifier": it is asked whether this is the
    WORLD map, which it answers well (0.99 live), and the globe button settles the rest.

    What was here before compared the title against "world map" through `title_text`, which
    returns a SINGLE element. On the world map that element was 'Port' — the first mode tab,
    reachable because the title crop ran to 0.40 of the frame — so this said "port map" about
    the world map on 2026-08-26 and the fleet re-perceived an open map instead of sailing.
    """
    if tokens is None:
        tokens = _ocr_frame(frame, min_conf=0.3)
    if _world_map_button(tokens) is None:
        return False
    return not _is_on_world_map(frame)


def _is_on_world_map(frame: Image.Image) -> bool:
    """True when the world map is open.

    ASK THE CLASSIFIER — DO NOT RE-DERIVE IT. `brain.perceive.classify_nav_state` already
    answers this, with the family CNN plus the registered fingerprints behind it, and it is
    the canonical home for "which screen is this" (CLAUDE.md: one canonical implementation
    per concern). This function used to run its own title OCR instead, and the two disagreed.

    Live 2026-08-26, on the world map with the fleet bound for Lisboa:

        [classify] → world_map (family-classifier conf=0.99) — short-circuit
        World map check: has_sea_hud=False title_says_world_map=False port_map=True

    The classifier was right. The local check read the title through `title_text`, which
    returns ONE element and returned 'Port' — the first mode tab — while the actual title sat
    beside it as the separate tokens 'World' and 'Map', both at ≥0.97. That same 'Port' then
    made `_is_on_port_map` say yes, so `open_world_map` sat re-perceiving a map that was open
    in front of it, and the voyage never started.

    The sea HUD still disqualifies: the sea view can carry a stale world-map title while the
    fleet is under way, and that is a genuinely different question from "which screen".
    """
    tokens = _ocr_frame(frame, min_conf=0.3)
    full = " ".join(t.lower() for t, _, _, _ in tokens)
    has_sea_hud = any(kw in full for kw in _SEA_HUD_TOKENS)

    from brain.perceive import _classify_nav_state
    verdict = (_classify_nav_state(frame) or {}).get("location")
    logger.info(f"  World map check: classifier={verdict!r} has_sea_hud={has_sea_hud}")
    return verdict == "world_map" and not has_sea_hud


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
    # DEPRECATED, AND LOUD ABOUT IT. This does FIVE jobs in one ten-attempt loop — waking an
    # OS lock, exiting a building, refusing a village, tapping, and verifying — and four of
    # them have owners now: the bootstrap, the dispatcher's routing, and the next perceive.
    # Live 2026-08-28 it was handed a lock screen, a daily-news popup and an Investment
    # Season banner, and its three available responses were all wrong: it waited for a
    # TransientActivity it reached ZERO times, read "Season" out of the banner, accepted it
    # as a port name, and reported "Overworld confirmed".
    #
    # The replacement is the OPEN_WORLD_MAP intent plus WorldMapActivity. Three live callers
    # remain (village_check, route_execution, nav_step) and four dormant ones; this line
    # names whichever fires so they are retired on evidence rather than guesswork, the way
    # `recover_to_port_overworld` was.
    import inspect as _inspect
    _caller = "unknown"
    for _fr in _inspect.stack()[1:]:
        if _fr.filename != __file__:
            _caller = f"{_fr.filename.rsplit('/', 1)[-1]}:{_fr.lineno} in {_fr.function}()"
            break
    logger.warning(f"[open_world_map] DEPRECATED — called from {_caller}. Use the "
                   "OPEN_WORLD_MAP intent + WorldMapActivity; report this caller.")
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
    # SEVERAL DISTINCT PORTS, NOT ONE LABEL. One port name in the left area is not the world
    # map — the PORT MAP's own title is a port name, sitting exactly there, so opening the
    # port map at Lisbon or Venice would be read as "world map opened". This check is ORed
    # with `_is_on_world_map`, so a single match here re-introduces the very confusion the
    # title check exists to prevent. The world map scatters MANY port labels across the map;
    # a chromed screen names one place.
    found, distinct = [], set()
    for _, text, conf in raw:
        if conf < 0.25:
            continue
        for port in known_ports:
            if port in text.lower():
                found.append(f"{text!r}({conf:.2f})")
                distinct.add(port)

    if len(distinct) >= 2 and not _is_on_port_map(frame):
        logger.info(f"  World map port labels in left area: {', '.join(found)}")
        return True
    if found:
        logger.info(f"  Only {len(distinct)} distinct port label(s) in the left area "
                    f"({', '.join(found)}) — not enough to call this the world map")
        return False

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


# A ROUTE'S COMMIT BUTTON IS A BARE 'Move' — no noun (live 2026-09-03, the Hutu mission).
# The saved-route panel's button says only `Move`, so the verb+noun rule below cannot see it,
# `_commit_control_showing` answered False, the world map classified the screen as ROUTE_LIST,
# and the activity re-tapped the route row it had already selected until the stall guard ended
# a mission with 4,597 units aboard and the route drawn on the map.
#
# THE NOUN IS NOT DECORATION, though — a bare `Move` is ALSO the sea-waypoint marker the game
# raises when a tap misses a port and lands on open water, and `_find_destination_button` is
# what tells those apart (see `_has_destination_button_text` and its caller around line 4537).
# So the word alone must never be enough. What makes it unambiguous is the ROUTE TAB: a saved
# route is selected and its panel is showing, which is not a state a waypoint marker occurs
# in. Callers that know that pass `allow_bare_move=True`; nobody else's behaviour changes.
#
# Why this and not a looser word list: the same action bar carries `My Location` and `Invest`
# on the very frame this was found on. Widening the verbs would match those.
_BARE_MOVE_MIN_W = 120           # the route panel's button measured 227px; a map label is small


def _bare_move_control(elements, min_y: int = None):
    """(cx, cy) of a route panel's bare `Move` button, or None.

    Deliberately strict: an OmniParser BUTTON (not loose map text), labelled exactly `move`,
    in the bottom action bar, and wide enough to be a real action control.
    """
    if min_y is None:
        min_y = _DESTINATION_BUTTON_MIN_Y
    for e in elements or []:
        if (getattr(e, "element_type", "") or "").lower() != "button":
            continue
        if (getattr(e, "label", "") or "").strip().lower() != "move":
            continue
        cy = getattr(e, "cy", 0) or 0
        if cy < min_y:
            continue
        if ((getattr(e, "x2", 0) or 0) - (getattr(e, "x1", 0) or 0)) < _BARE_MOVE_MIN_W:
            continue
        cx = getattr(e, "cx", 0) or 0
        logger.info(f"  [panel-commit] bare 'Move' button @ ({cx},{cy}) — a saved route's "
                    "commit control")
        return (cx, cy)
    return None


def destination_commit_control(frame=None, *, elements=None, tokens=None,
                               allow_bare_move: bool = False):
    """WHERE the commit control is on an OPEN destination panel — or None.

    ONE TAP'S WORTH. `commit_departure` opens the map and navigates to a destination; this is
    only the last step of it, for a caller already standing on the panel with the place
    selected. Calling the whole flow from there re-opens the map and re-navigates.

    The rule is `_find_destination_button`'s and stays there: the button lives in the bottom
    action bar (`y >= _DESTINATION_BUTTON_MIN_Y`) and is a verb+noun pair, merged or split.
    This wrapper only lets an OmniParser caller ask the same question — the classifier holds
    elements, not OCR tokens, and a second implementation of "where is the Move button" is
    how the positional rule got lost the first time.
    """
    if tokens is None:
        if elements is None:
            if frame is None:
                return None
            from vision.omniparser import parse_fast_cached
            elements = list(parse_fast_cached(frame))
        tokens = [((getattr(e, "label", "") or "").strip(), 1.0,
                   getattr(e, "cx", 0) or 0, getattr(e, "cy", 0) or 0)
                  for e in elements or []]
        tokens = [t for t in tokens if t[0]]
    pos = _find_destination_button(tokens, label="panel-commit")
    if pos is None and allow_bare_move:
        if elements is None and frame is not None:
            from vision.omniparser import parse_fast_cached
            elements = list(parse_fast_cached(frame))
        pos = _bare_move_control(elements)
    return pos


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
            # FUZZY, like the verb above. These were asymmetric — the verb tolerant of OCR
            # and the noun an exact substring — so a single dropped character in the noun
            # sank the whole match while the verb beside it read perfectly. Live 2026-09-04
            # the San Village departure failed on 'Move to' + 'Villags': the button was
            # mid-render, the 'e' was lost, and 'village' in 'villags' is False. The pair is
            # one button and one OCR risk; both halves need the same tolerance.
            and any(fuzzy_contains(nxt_text, n) for n in _DESTINATION_BUTTON_NOUNS)
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


# The band the tab row lives in, and how like a tab name a label must read before the row is
# allowed to call it one. 0.72 sits above 'Explosives' (0.71 against 'explore') and below the
# worst real reading seen ('Exp1oregh', 0.75) — set by what actually collides, not by taste.
_TAB_BAND_Y = 200
_TAB_MIN_SCORE = 0.72

# THE BLEED IS SEPARABLE BY BRIGHTNESS (user, 2026-09-01). The bar is translucent, so the map
# shows through it — but the game draws the tab labels in a flat UI grey, measured at exactly
# 149 on Explore, Route and Trade alike, while whatever shows through is DIMMED by the overlay
# before it reaches the eye: Plymouth's strokes measured 89, indistinguishable from the bar's
# own background at 92. Erasing everything below this level deletes the bleed and leaves the
# labels untouched. 145 sits in a gap ~50 wide, so it is not a tuned number.
_TAB_TEXT_LUMA = 145
_TAB_BAND_CACHE: dict = {}


def _tab_band_without_bleed(frame):
    """The tab band with the map's own text erased — see `_TAB_TEXT_LUMA`.

    Parsing THIS instead of the whole frame is what makes the tab names exact, and it fixes a
    failure that no amount of scoring could. Live 2026-08-31 the shared whole-frame parse
    returned Explore, Weymouth and Route as ONE element reading 'Explo eymoutRoute'; that
    token contains 'route', so it scored 0.95 by containment and consumed Explore's element
    along with it. The row came back ['port', 'route', 'trade'], `select_world_map_tab`
    refused to read another tab's rail, and the mission failed at sail_to_village. A tab name
    that is not a separate token cannot be recovered downstream — the merge has to be
    prevented, and dimmed pixels are how the game itself distinguishes the two layers.

    Cached against the SOURCE frame and holding a reference to it: `id()` is reused the
    moment a frame is freed, which is the collision `parse_fast_cached` documents at length.
    """
    import numpy as np
    from PIL import Image as _Image

    cached = _TAB_BAND_CACHE.get(id(frame))
    if cached is not None and cached[0] is frame:
        return cached[1]

    band = np.asarray(frame.crop((0, 0, frame.width, _TAB_BAND_Y)).convert("RGB"))
    lum = 0.299 * band[..., 0] + 0.587 * band[..., 1] + 0.114 * band[..., 2]
    clean = _Image.fromarray(
        np.where((lum >= _TAB_TEXT_LUMA)[..., None], band, 0).astype(np.uint8))

    _TAB_BAND_CACHE.clear()          # one frame at a time; the id above is only unique alive
    _TAB_BAND_CACHE[id(frame)] = (frame, clean)
    return clean


def _world_map_tab_strip(frame) -> list:
    """[(name, cx, cy, y2)] for the world map's tab row, left to right.

    BBOX AND FUZZY WORD, TOGETHER — neither is enough alone (user, 2026-08-29).

    THE TAB BAR IS TRANSLUCENT, so map text underneath bleeds into the label and the
    corruption changes with whatever the map is showing. The same Explore tab read
    'Exploregh' in one capture and 'Explorgh' in the next — Edinbur-GH showing through — and
    'NarExplore' was the same thing from the other side. An exact match, a containment test
    and a prefix each fail on some frame or other, because the damage is not in the OCR: it
    is on the screen.

    THE BLEED IS NOW ERASED BEFORE THE PARSE — `_tab_band_without_bleed` drops it by
    brightness, and on the frames that used to read 'Explo eymoutRoute' the labels come back
    exactly 'Port', 'Explore', 'Route', 'Trade'. What follows is therefore no longer the
    thing standing between us and a wrong answer; it is the residual guard for whatever the
    mask does not catch, and it is kept because a merged token is unrecoverable downstream
    and a cheap second line is worth having.

    A FUZZY word score identifies them all — 0.80, 0.88 and 0.82 against 'explore'. It cannot
    stand alone either: 'Explosives' scores 0.71, close enough to a badly bled tab to be taken
    for one.

    THE BBOX SETTLES IT. Tabs live in a band at the top of the screen and appear in a FIXED
    ORDER, so the reading must be a left-to-right assignment: whatever is chosen for Explore
    sits right of Port and left of Route. An unrelated word cannot take a slot without
    displacing a better-scoring neighbour or breaking the order. This picks the ordered
    assignment with the best total score — the structure constrains what the words are
    allowed to mean.
    """
    from vision.omniparser import get_omniparser
    parser = get_omniparser()
    if not parser.yolo_available():
        return []

    # The BAND, with the map's bleed erased — not the shared whole-frame parse. The crop
    # starts at the origin, so cx/cy/y2 are already frame coordinates.
    els = sorted((e for e in parser.parse_fast(_tab_band_without_bleed(frame))
                  if (e.label or "").strip()),
                 key=lambda e: e.cx)
    if not els:
        return []

    tabs = _WORLD_MAP_TABS
    scores = [[_tab_score(e.label, t) for t in tabs] for e in els]

    # The best assignment of tabs to elements that keeps BOTH in left-to-right order.
    # best[i][j] = best total using elements[i:] for tabs[j:] — small enough to be exact.
    #
    # THREE MOVES, NOT TWO: take this element as this tab, skip the ELEMENT (it is something
    # else on the bar), or skip the TAB (this row does not show it). Leaving out the third
    # made a row that is genuinely missing a tab stall on it and drop every tab after —
    # ['port','route','trade'] came back as ['port'].
    n, m = len(els), len(tabs)
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    move = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            options = [(best[i + 1][j], "skip-element"), (best[i][j + 1], "skip-tab")]
            if scores[i][j] >= _TAB_MIN_SCORE:
                options.append((scores[i][j] + best[i + 1][j + 1], "take"))
            best[i][j], move[i][j] = max(options)

    out, i, j = [], 0, 0
    while i < n and j < m:
        if move[i][j] == "take":
            e, score = els[i], scores[i][j]
            if score < 0.999:
                logger.info(f"[world-map-tab] {e.label!r} @{e.cx} read as {tabs[j]!r} "
                            f"(score {score:.2f}, and it sits where {tabs[j]!r} must) — "
                            f"the tab bar is translucent")
            out.append((tabs[j], e.cx, e.cy, e.y2))
            i, j = i + 1, j + 1
        elif move[i][j] == "skip-element":
            i += 1
        else:
            j += 1
    return _recover_missing_tabs(frame, out)


def _recover_missing_tabs(frame, found: list) -> list:
    """Read any tab the band-wide parse lost, in ITS OWN BOX. Returns the row, left to right.

    THE SELECTED TAB IS THE ONE THIS LOSES, and it loses it two different ways at once.
    `_tab_band_without_bleed` keeps pixels BRIGHTER than the cut, which is right for the
    unselected tabs — light labels on dark, with the dim map bleed dropped. The SELECTED tab
    is the other way round: a white slab with DARK text, so the mask keeps the slab and erases
    the label. And unmasked, a map pin drawn under the strip merges with it.

    Live 2026-09-03 at Casablanca, both together:

        band OCR   'CasahlanPort'  conf=0.64      the pin's name fused with the tab's
        masked     (nothing)                      the dark label erased with the bleed
        row        ['explore', 'route', 'trade']
        -> 'port' is not in the row -> the tab cannot be selected -> the course cannot be set
        -> FAILED at step mission: gather:Faro

    Reading the tab's OWN box recovers it: that crop reads 'anPort' — the label plus the tail
    of 'Casablanca' — which `_tab_score` identifies by containment. The box comes from the
    SPACING of the tabs that WERE read, since they are evenly spaced in a fixed order, so
    nothing here is a remembered coordinate.

    AND IT IS A READ, NOT A GUESS. The position is computed, but the name still has to come
    off the screen and score: a slot whose crop does not name the tab is left out, and the
    caller gets a short row and refuses, exactly as before. That distinction is the whole
    lesson of `MARKET_COORDS["sell"]` — a fallback that guesses is worse than one that
    refuses.
    """
    from vision.ocr import read_text

    tabs = list(_WORLD_MAP_TABS)
    if len(found) >= len(tabs) or len(found) < 2:
        return found                      # nothing missing, or too little to place anything

    by_name = {n: (n, cx, cy, y2) for n, cx, cy, y2 in found}
    xs = [(tabs.index(n), cx) for n, cx, _, _ in found]
    gaps = [(xs[i + 1][1] - xs[i][1]) / max(xs[i + 1][0] - xs[i][0], 1)
            for i in range(len(xs) - 1)]
    if not gaps:
        return found
    pitch = sorted(gaps)[len(gaps) // 2]
    if pitch <= 0:
        return found
    anchor_idx, anchor_cx = xs[0]
    _, _, cy, y2 = found[0]

    for j, name in enumerate(tabs):
        if name in by_name:
            continue
        cx = int(anchor_cx + (j - anchor_idx) * pitch)
        half = int(pitch * 0.37)
        if cx - half < 0 or cx + half > frame.width:
            continue
        text = read_text(frame.crop((cx - half, max(cy - 30, 0), cx + half, cy + 30)))
        score = _tab_score(text, name)
        if score >= _TAB_MIN_SCORE:
            logger.info(f"[world-map-tab] {name!r} was missing from the row; its own box "
                        f"reads {text!r} (score {score:.2f}) @{cx} — recovered")
            by_name[name] = (name, cx, cy, y2)
        else:
            logger.info(f"[world-map-tab] {name!r} is missing and its box reads {text!r}, "
                        f"which does not name it (score {score:.2f}) — leaving it out")
    return [by_name[n] for n in tabs if n in by_name]


def _tab_score(label: str, name: str) -> float:
    """How well an OCR'd label names this tab. 1.0 exact, 0.0 nothing like it.

    A bled label carries the whole tab name plus a neighbour's fragment, so containment is
    worth more than the raw ratio it would score: 'NarExplore' is 0.82 by similarity and
    certain by inspection.
    """
    import difflib

    lab = (label or "").strip().lower()
    if not lab:
        return 0.0
    if lab == name:
        return 1.0
    if name in lab:
        return 0.95
    return difflib.SequenceMatcher(None, lab, name).ratio()


def active_world_map_tab(frame=None) -> Optional[str]:
    """WHICH world-map tab is lit right now — 'port' / 'explore' / 'route' / 'trade', or
    None when the tab row is not on screen or the highlight cannot be read.

    THE OBSERVATION, exposed. Before this the codebase could COMMAND the tab
    (`select_world_map_tab`) but not ASK it: the reader lived inside that function as a means
    to its own end, so every other world-map operation proceeded on an assumption instead.

    That gap produced the same bug twice, in opposite directions:
      * 2026-08-25 — a village list hunted on the PORT tab, which opened the trade-goods
        filter instead;
      * 2026-08-27 — `_try_port_search` typing "Barc" into the EXPLORE tab's rail, getting
        back `carved horn`, and persisting the VILLAGE icon as `port_list_icon`.

    Each was fixed where it was found. Neither could have happened if the tab were checked,
    which is the fix at the level of the rule rather than the instance (user, 2026-08-27).

    The left rail IS the lit tab's list, so anything that reads or taps that rail must know
    this first. And the map reopens on 'port' after a close, which is why the answer must be
    read and never remembered.
    """
    if frame is None:
        from capture.adb_capture import capture_screen as _capture
        frame = _capture()
    tabs = _world_map_tab_strip(frame)
    if not tabs:
        return None
    lit = selected_tab_index(frame, [(t[1], t[2]) for t in tabs], trailing_toggles=0)
    if lit is None:
        return None
    return tabs[lit][0]


def require_world_map_tab(tab: str, why: str, frame=None) -> bool:
    """The LEFT RAIL IS THE LIT TAB'S LIST — so read the tab before reading the rail.

    Returns True when `tab` is already lit, otherwise selects it and confirms. False means
    the caller must NOT touch the rail: on the wrong tab its icons belong to another list,
    and the usual acceptance test ("did a panel open?") is satisfied by every one of them,
    because every list has a search box.

    This is the check whose absence produced the same bug twice — a village list hunted on
    the Port tab (2026-08-25) and a port typed into the Explore rail (2026-08-27). Both were
    fixed at the call site that failed; neither fix reached the other. Checking the tab is
    the fix at the level of the rule.
    """
    seen = active_world_map_tab(frame)
    if seen == tab:
        return True
    logger.info(f"[world-map-tab] rail belongs to {seen!r}, need {tab!r} — switching ({why})")
    return select_world_map_tab(tab)


def select_world_map_tab(tab_name: str) -> bool:
    """Switch the world map to the named tab. True only when the tab is SELECTED afterwards.

    It used to tap and report success, on the reasoning that tapping an already-active tab is
    a harmless no-op. But a tap that never lands reads the same way, and the tab strip sits at
    the very top of the screen where taps can be swallowed: live 2026-08-25 the tap landed
    inside the Explore tab at (1047,55), the tab stayed on PORT, and the caller then hunted
    the Port tab's icons for a village list — opening the trade-goods filter instead, giving
    up, and falling back to panning the map to find a village that was in plain view.

    So the switch is confirmed by EFFECT: the requested tab must be the lit one afterwards.
    A tap that does not take is simply REPEATED, at the same place, after a longer settle —
    the trace shows the identical coordinate (1046,51) failing moments after the map opened
    and working later, so the point was never wrong, the map was not ready. Tapping somewhere
    else would have been a guess dressed as a fix.
    """
    tab_lc = tab_name.lower().strip()
    if tab_lc not in _WORLD_MAP_TABS:
        logger.warning(f"[world-map-tab] unknown tab {tab_name!r}; expected one of "
                       f"{_WORLD_MAP_TABS}")
        return False

    from vision.omniparser import get_omniparser
    from actions.adb_actions import tap as _tap
    from capture.adb_capture import capture_screen as _capture
    import time as _t

    parser = get_omniparser()
    if not parser.yolo_available():
        logger.warning("[world-map-tab] OmniParser unavailable — cannot locate tab")
        return False

    _strip = _world_map_tab_strip

    for attempt in range(_TAB_TAP_ATTEMPTS):
        frame = _capture()
        tabs = _strip(frame)
        if not tabs:
            logger.warning(f"[world-map-tab] could not find the tab row on this screen")
            return False

        names = [t[0] for t in tabs]
        if active_world_map_tab(frame) == tab_lc:
            logger.info(f"[world-map-tab] already on {tab_name!r}")
            return True
        if tab_lc not in names:
            # A PARTIAL ROW IS A NOT-READY ROW, and this loop already exists for exactly that.
            # Live 2026-08-30, mid-mission at Hutu Village: the strip read back as
            # ['port', 'route', 'trade'] — three of the four — and the missing one was the
            # 'explore' the village list lives on. The tab had been selected successfully
            # ninety seconds earlier, so it was plainly there; the READ dropped it. Bailing
            # out on the first look ended the whole mission over one flaky glance.
            #
            # A row that is SHORT is incomplete, so wait and look again on the next attempt.
            # A row that is COMPLETE and still lacks the tab is a different screen, and no
            # amount of waiting changes that — say so and stop.
            if len(names) < len(_WORLD_MAP_TABS) and attempt < _TAB_TAP_ATTEMPTS - 1:
                logger.warning(f"[world-map-tab] {tab_name!r} is not in the row {names} — "
                               f"only {len(names)} of {len(_WORLD_MAP_TABS)} tabs read, so "
                               f"the row is incomplete; looking again")
                _t.sleep(_TAB_SETTLE_S * (attempt + 1))
                continue
            logger.warning(f"[world-map-tab] {tab_name!r} is not in the row {names}")
            return False

        _name, cx, cy, _y2 = tabs[names.index(tab_lc)]
        # THE MAP HAS TO BE READY, not the tap moved. Same coordinate every time; each retry
        # simply waits longer for the world map to finish settling before looking again.
        logger.info(f"[world-map-tab] tapping {tab_name!r} @ ({cx},{cy})"
                    + ("" if attempt == 0 else f" (retry {attempt})"))
        _tap(cx, cy)
        _t.sleep(_TAB_SETTLE_S * (attempt + 1))

        after = _capture()
        tabs2 = _strip(after)
        lit2 = selected_tab_index(after, [(t[1], t[2]) for t in tabs2], trailing_toggles=0)
        if lit2 is not None and [t[0] for t in tabs2][lit2] == tab_lc:
            logger.info(f"[world-map-tab] {tab_name!r} is now selected")
            return True
        logger.warning(f"[world-map-tab] {tab_name!r} did not take — the tap was swallowed")

    logger.error(f"[world-map-tab] could not select {tab_name!r} after "
                 f"{_TAB_TAP_ATTEMPTS} attempts")
    return False


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
    # NEVER MATCH OUR OWN TYPING. The search box holds the prefix the bot just typed, so it
    # matches the destination at 1.00 EVERY TIME and outscores the real row. Live 2026-09-03
    # hunting Faro with the list filtered to exactly one entry:
    #
    #     'faro'(1.00) @ (303,144)   the SEARCH BOX — what this returned
    #     'Faro'(0.62) @ (209,200)   the list row — what was wanted
    #
    # `_on_list` refuses to tap the box, so nothing was found at all, and it swiped the map
    # looking for a port sitting in plain view (user). The rule is already written down —
    # "the search box always matches the query, so it is a guaranteed false positive" — and
    # this path did not apply it. Narrowing to the rail does not help: the box IS in the rail.
    _box = None
    try:
        _box = search_box_element(frame)
    except Exception as exc:                      # noqa: BLE001 — no box is not an error
        logger.debug(f"  could not locate the search box: {exc}")

    def _is_our_own_query(cx: int, cy: int) -> bool:
        if _box is None:
            return False
        return (_box.x1 <= cx <= _box.x2) and (_box.y1 <= cy <= _box.y2)

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
        # `cx`/`cy` are already FRAME coordinates — the +40 undoes the crop off the top —
        # and the box is read from the frame, so they are compared in the same space.
        if _is_our_own_query(cx, cy):
            logger.info(f"  Skipping {text!r} @ ({cx},{cy}) — that is the SEARCH BOX holding "
                        "our own query, not a row in the list")
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

    THE TAB COMES FIRST. Ports and villages live on DIFFERENT world-map tabs — Port and
    Explore — and the left rail is that tab's list, so the icons cannot be mixed (user,
    2026-08-27). Searching without selecting the tab searches whatever list happens to be
    showing.

    Live 2026-08-27, sailing back for Matchlock Gun: the map had been left on EXPLORE by the
    village navigation, so typing "Barc" into the rail returned `carved horn` (a trade good)
    and a scatter of island names, never Barcelona. `_save_icon_pos` then wrote (70,290) to
    `world_map_ui.json` as the port-list icon — the very position `_try_village_search` uses
    for the VILLAGE list — poisoning the next run's candidate ranking.

    `select_world_map_tab`'s own docstring records this same confusion in the opposite
    direction (a village list hunted on the Port tab, opening the trade-goods filter). It was
    fixed there and not here.

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

    # SELECT THE PORT TAB BEFORE LOOKING FOR ANYTHING. `select_world_map_tab` confirms by
    # EFFECT — the requested tab must be the lit one afterwards — so this is an observation,
    # not a tap-and-hope. Without it the icon sweep below ranks candidates from another tab's
    # rail and the acceptance test ("did a panel open?") is satisfied by the wrong list.
    if not require_world_map_tab("port", why="the port list lives on the Port tab"):
        logger.warning("[port-search] not on the Port tab — refusing to search another "
                       "tab's rail, which is how the village icon was learned as the port "
                       "icon and 'Barc' matched a trade good")
        return None

    # NO LEARNED ICON POSITION IS PERSISTED. World-map state — which tab is lit, what the
    # rail lists, where its icons sit — is PANEL-owned: it dies when the map closes (CLAUDE.md,
    # "Data has an OWNER, and dies with it"). Writing it to memory/knowledge/config/ stored it
    # at COMPANY lifetime, so it outlived not just the panel but the whole session.
    #
    # And it CANNOT be right across a close: the map reopens on the PORT tab (user,
    # 2026-08-27), so a position learned while another tab was lit describes a rail that is no
    # longer there. Live 2026-08-27 that persisted the VILLAGE icon (70,290) as
    # `port_list_icon`, and because the saved value RANKS the candidate sweep, a wrong memory
    # pulled the next attempt back toward the same wrong icon.
    #
    # Nothing is lost by dropping it: the sweep detects the icons on the frame in front of it,
    # which is the reading that was always authoritative.
    # ── (historical: learned icon position, removed 2026-08-27) ──────────────
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

    saved_pos = None          # PANEL-owned: never carried across a map close
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
                # not persisted — see the ownership note above
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
        # `transient` belongs in this set, and its absence cost a voyage on 2026-08-26.
        #
        # A successful departure plays a CINEMATIC, and a cinematic is a full-screen notice —
        # which perception now names `transient` rather than putting through the legacy
        # cascade. This list had never seen that string, so the tap that WORKED was read as
        # "unexpected location — aborting", the goal concluded the departure had failed, and
        # it re-opened the world map mid-voyage, re-targeted the port it had just left, and
        # sailed back to it. Eleven seconds later the same perception said `sea`.
        #
        # It sits beside `loading` and `sea_cinematic` for the same reason all three are here:
        # the world being mid-change is what success LOOKS like from inside the tap.
        in_flux = ("sea", "sea_cinematic", "loading", "port_overworld", "transient")
        loc = _wait_for_state_change(expected=in_flux, max_wait=8.0)
        logger.info(f"  State after 'Go to City' tap: {loc['location']!r}")
        if loc["location"] in in_flux:
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

# ── Village search (Explore tab) ─────────────────────────────────────────────
#
# Villages DO have a searchable list — it just lives on a different tab from the ports'
# (user, 2026-08-24). World map → **Explore** → the left icon bar CHANGES, and its SECOND
# icon (a house) opens a village list with the same Search box the port list has.
#
# This replaces panning as the first choice. Panning needs an anchor port near the village,
# a scale estimate and up to 8 stride pans; the search needs a name. Panning stays as the
# fallback for a village the list does not carry.
_VILLAGE_LIST_ICON_INDEX = 1          # 0-based: the house is the second icon


# THE RAIL IS FIXED CHROME, AND ON A BARE MAP IT CANNOT BE DETECTED AT ALL.
#
# Measured 2026-09-01 across three frames: OmniParser reports the rail's icons only when a
# LIST IS ALREADY OPEN behind them — that dark panel is what gives the small glyphs contrast.
# Over the bare map the strip is translucent, and the parse returns NOTHING at their x; not a
# misread, an absence. So on the one screen where the list must be opened, there is nothing to
# look up, and `_explore_left_icons` correctly returns [] rather than offering a port pin.
#
# These are the positions the rail actually occupies, and they are chrome, not content:
#   the port-list icon   frame_0014 (70,184); the tap that opened it live (68,176)
#   the village-list one frame_0014 (70,301)
#
# CLAUDE.md's exception applies squarely — "`ui.tap_at(x, y, why=…)` is the loud, logged
# exception for genuinely calibrated HUD controls" — and the tap is JUDGED: the next tick asks
# whether a list opened, exactly as it does for a detected icon. That is what separates this
# from the calibrated Sell coordinate deleted earlier today, where a working lookup existed
# and the constant was its stale shadow.
_RAIL_FALLBACK_POINTS = ((69, 180), (70, 300))
_VILLAGE_SEARCH_PREFIX = 3            # type a prefix — OCR of the full name is not needed
# The village list occupies the left edge; the Village Info panel is on the right.
_VILLAGE_LIST_MAX_X = 700
# An occluded label must still carry this many characters before it can name a village — a
# shorter fragment is not evidence, it is a coincidence waiting to mis-tap.
_LABEL_MIN_FRAGMENT = 4
_LABEL_FUZZY_MIN = 0.78
# Scrolling the list is keyboard-free; the rows are ~53px apart on a ~10-row panel.
_VILLAGE_LIST_SCROLL_X, _VILLAGE_LIST_SCROLL_Y = 300, 700
_VILLAGE_LIST_SCROLL_DY = -260
_VILLAGE_LIST_SCROLLS = 10


# The rail's search box: a single line at the top of the panel, well right of the icon strip
# and well above the rows. Positional, because its TEXT is exactly what cannot be relied on.
_SEARCH_BOX_X = (200, 700)
_SEARCH_BOX_Y = (110, 180)


# The rail's search box is a WIDE field: measured 384-392px across, whether it is empty
# ("Search") or holding a query ("svea"). The port pins that were mistaken for it top out
# at 95. Anything between separates them with room to spare.
_SEARCH_BOX_MIN_W = 200


def search_box_present(frame, elements=None) -> bool:
    """True when the rail's search box is on screen — WHATEVER it says.

    AN EMPTY BOX READS 'Search'; A FILLED ONE READS WHAT YOU TYPED. Every test for an open
    list looked for the word, so typing into the box destroyed the evidence that the list was
    open: the screen fell back to `map_open` and the bot went on looking at the map while
    standing in the list (live 2026-08-29, 'Svea' left in the field from an earlier run).

    So the box is identified by WHERE IT SITS, not by what it holds. That is the one thing
    about it that a query cannot change.

    BUT WHERE ALONE IS NOT WHAT (live 2026-09-01). This accepted ANY element inside the
    window, of any kind, so on a bare map with no list at all a couple of port PINS landed in
    it and the screen classified as `destination_list`. The activity then tapped the search
    box that was not there and typed 'Barc' into the map — twice, exhausting its typing
    budget — and fell through to scrolling a list it had never opened. Six scrolls later the
    leg failed: "'Barcelona' is not in the port list — looked, typed and scrolled".

    The same file says the rule two functions down: `_village_list_open` is "identified by
    WHAT IS THERE ... not by where anything sits". So ask BOTH. The box is WIDE — measured
    384-392px across, empty or holding 'svea' — while the map pins that fooled it are 95px at
    the widest. A margin of nearly three hundred pixels is not a tuned number.
    """
    return search_box_element(frame, elements) is not None


def search_box_element(frame, elements=None):
    """The rail's search box element, or None. Where it sits AND being a box — see above."""
    if elements is None:
        from vision.omniparser import parse_fast_cached
        elements = list(parse_fast_cached(frame))
    for e in elements:
        if not (_SEARCH_BOX_X[0] < e.cx < _SEARCH_BOX_X[1]
                and _SEARCH_BOX_Y[0] < e.cy < _SEARCH_BOX_Y[1]):
            continue
        # NEVER AN ICON. The port pins that classified a bare map as an open list were icons,
        # and a search field is not one.
        if (getattr(e, "element_type", "") or "") == "icon":
            continue
        # THE PARSE RETURNS EITHER THE BOX OR THE WORD IN IT, and both are the box (live
        # 2026-09-01): 'Search' came back as a 393x99 BUTTON on one frame and as a 96x32 TEXT
        # on another. Requiring the width alone rejected the second, so a list that WAS open
        # read as closed — and the caller then re-tapped the rail, which toggles an open list
        # SHUT. It reopened and re-closed it on every tick.
        if (e.x2 - e.x1) >= _SEARCH_BOX_MIN_W:
            return e
        if (getattr(e, "label", "") or "").strip().lower() == "search":
            return e
    return None


def search_box_holds(prefix: str, frame, elements=None) -> bool:
    """Did what we typed actually reach the box?

    THE BOX SHOWS THE QUERY, which is the only way to tell a typing that LANDED from one that
    was sent. Live 2026-09-01 the map was mistaken for an open list, two prefixes were typed
    into it, and the budget for typing was spent before the real list ever opened — after
    which the search could only scroll.

    Not to be confused with `never-match-your-own-typing`: reading the box to find the PORT
    is a guaranteed false positive, because the box always contains the query. Reading it to
    confirm the QUERY is the one question it can honestly answer.
    """
    el = search_box_element(frame, elements)
    if el is None:
        return False
    from actions.world_map_nav import fold_name
    return fold_name((prefix or "").lower()) in fold_name((getattr(el, "label", "") or "").lower())


def _village_list_open(frame, elements=None) -> bool:
    """True when the Explore tab's village list is on screen.

    Identified by WHAT IS THERE — a Search box above several '<name> Village' rows — not by
    where anything sits (see docs/ui_anatomy.md, "Identify by association").
    """
    if elements is None:
        from vision.omniparser import parse_fast_cached
        elements = list(parse_fast_cached(frame))
    left = [(getattr(e, "label", "") or "").strip().lower()
            for e in elements if e.cx < 700]
    # A FILTERED LIST IS STILL AN OPEN LIST. Requiring several village rows made a search
    # that had narrowed to ONE result read as "no list", so the caller went hunting for the
    # icon again and toggled the panel shut (measured 2026-08-24).
    # The box is found by POSITION, not by the word 'search' — a box holding a query no
    # longer says 'Search', and requiring the word made a list that had been typed into
    # read as no list at all.
    return search_box_present(frame, elements) and any("village" in l for l in left)


def _port_list_open(frame, elements=None) -> bool:
    """True when the Port tab's port list is on screen.

    THE SAME QUESTION THE VILLAGE LIST WAS ALREADY ASKED (user, 2026-09-01). `_on_list` checked
    which list it was reading for a VILLAGE goal and took the classifier's word for a PORT one,
    so a screen wrongly called `destination_list` sent it straight to reading and typing into a
    list that was not there. Live 2026-09-01 the bare map was classified that way and the
    prefix went into the MAP, twice, at frames 100 and 103.

    A list is open when the search box is (the same anchor the village test uses, and for the
    same reason: a box holding a query no longer says 'Search'). It is the PORT list when it is
    not the village one — the two rails differ by their rows, and the tab decides which is
    shown. Asking "is it not the other one" rather than listing port names keeps this from
    needing to know every port in the world.
    """
    if elements is None:
        from vision.omniparser import parse_fast_cached
        elements = list(parse_fast_cached(frame))
    return search_box_present(frame, elements) and not _village_list_open(frame, elements)


# How far apart two rail icons' centres may sit horizontally and still be one column.
# Measured: a real rail reads cx = 70, 70, 71 — a spread of ONE pixel, because the game draws
# them at one x. The map pins mistaken for it spread 124 and 177, and even the three nearest
# the edge sit 12 apart. The rail is drawn, not scattered.
#
# A rail icon is also drawn WHOLLY ON SCREEN: the real ones start at x1 = 39, 37, 36, while
# the two pins that were tapped start at x1 = 0 — clipped by the frame edge, which is what a
# port half-off the left of the map looks like and what a UI control never does.
_RAIL_COLUMN_TOL_PX = 12
# A control is not clipped by the frame; a port pin at the map's edge is.
_RAIL_NOT_CLIPPED_PX = 2


def _explore_left_icons(frame, elements=None) -> list:
    """The Explore tab's left icon strip, top to bottom. Empty when there is no strip.

    A COLUMN of similar icons at the frame's left edge — the same structural test the port
    tab strip uses, turned on its side.

    THAT SENTENCE WAS THE DOCSTRING AND NOT THE CODE. It filtered `element_type == "icon"`
    inside a box and returned whatever was there, with nothing asking whether the results
    formed a column at all. On a map panned so Iberia sits at the left edge, PORT PINS
    qualify: live 2026-09-01 this returned [(13,222), (25,275), (190,341), (40,530)] with no
    rail on screen, `_open_list` tapped the first, and it was FARO's flag — half off the
    frame, its label beside it. The City Info panel that opened was Faro's, the commit went in
    on it, and the fleet was asked to sail somewhere nobody had chosen.

    The rail's icons carry no label — OmniParser returns bare 'icon's — so position is the
    only identity available, and "a column" is the whole of it. One icon that happens to be
    at the left edge is not a rail, and neither are four at scattered x.

    Returning [] is the useful answer when there is no rail: `_open_list` says "no icon rail
    detected — not tapping a remembered point" and taps nothing, which is what should have
    happened here.
    """
    if elements is None:
        from vision.omniparser import parse_fast_cached
        elements = list(parse_fast_cached(frame))
    icons = [e for e in elements
             if getattr(e, "element_type", "") == "icon"
             and e.cx < 200 and 100 < e.cy < 700
             and e.x1 > _RAIL_NOT_CLIPPED_PX]
    if len(icons) < 2:
        return []                       # one icon at the edge is not a strip

    # THE BIGGEST COLUMN WINS. Each candidate proposes the column centred on its own x; the
    # rail is the one that gathers the most members, because a rail HAS several and stray map
    # pins do not line up with each other.
    best: list = []
    for anchor in icons:
        column = [e for e in icons if abs(e.cx - anchor.cx) <= _RAIL_COLUMN_TOL_PX]
        if len(column) > len(best):
            best = column
    if len(best) < 2:
        return []
    best.sort(key=lambda e: e.cy)
    return [(e.cx, e.cy) for e in best]


def _map_label_matches(label: str, target: str) -> float:
    """How well an OCR'd label names `target`. 0.0 = no, 1.0 = exact.

    A map label is often PARTIALLY BLOCKED — Svear Village sits under a discovery icon
    (user, 2026-08-24/25), so OCR returns a fragment like 'vear' or 'Svea'. An equality or
    prefix test misses every one of those, which is why a village in plain view was hunted
    for by panning instead of tapped.

    Kept deliberately tight: a fragment must be at least `_LABEL_MIN_FRAGMENT` characters, so
    'S' cannot match Svear and drag a tap onto the wrong place.
    """
    import difflib
    import re as _re

    lab = (label or "").strip().lower()
    tgt = (target or "").replace("Village", "").strip().lower()
    if not lab or not tgt:
        return 0.0
    # A TRUNCATED NAME ENDS IN AN ELLIPSIS. The fleet list cuts long names — "Imai Sokun
    # Merc...", OCR'd as "Imai Sokun Merc__." — and those trailing marks defeat a prefix test
    # that would otherwise match perfectly. Strip them before comparing; they carry no
    # information beyond "there was more".
    lab = _re.sub(r"[.\u2026_\-\s]+$", "", lab)
    lab_short = lab.replace("village", "").strip()
    # THE WORD "VILLAGE" IS ITSELF EVIDENCE: it narrows the candidates to a handful, so a
    # shorter fragment beside it is still safe. 'ear Village' is Svear with its head behind
    # an icon; 'ear' alone would not be enough to act on.
    min_fragment = 3 if "village" in lab else _LABEL_MIN_FRAGMENT
    if lab_short == tgt:
        return 1.0
    if lab_short.startswith(tgt) or tgt.startswith(lab_short):
        # A prefix either way: the label is cut short, or it carries a suffix.
        if len(lab_short) >= min_fragment:
            return 0.95
    if len(lab_short) >= min_fragment and lab_short in tgt:
        return 0.9                      # a middle fragment: the head is behind the icon
    ratio = difflib.SequenceMatcher(None, lab_short, tgt).ratio()
    return ratio if ratio >= _LABEL_FUZZY_MIN else 0.0


def _visible_row(elements, name: str, *, x_max: int = None):
    """The on-screen element naming `name`, or None — the BEST match, not the first.

    Villages and ports are frequently ALREADY VISIBLE — in an open list, or as a label on the
    map itself (user, 2026-08-24): Hutu Village sat in the unfiltered village list, and Gijon
    was in view from Bordeaux. Both were searched for anyway, which costs a panel, a keyboard
    and several seconds each time. Look before you hunt.
    """
    # THE LIST IS ON THE LEFT by default. Scanning the whole frame matched the village's own
    # name in the right-hand Village Info PANEL and tapped that instead of a list row (live
    # 2026-08-24, Svear at x=1961) — which left the list unopened, the trade list already
    # scrolled, and the read returned the wrong good entirely. Callers looking at the MAP
    # rather than a list pass a wider `x_max`.
    limit = _VILLAGE_LIST_MAX_X if x_max is None else x_max
    best, best_score = None, 0.0
    for e in elements or []:
        if getattr(e, "cx", 0) > limit:
            continue
        score = _map_label_matches(getattr(e, "label", ""), name)
        if score > best_score:
            best, best_score = e, score
    return best


def _try_village_search(village: str, capture_fn=None) -> bool:
    """Find `village` through the Explore tab's village list. True when its row was tapped.

    THE TAB IS CHECKED, NOT ASSUMED. The name says Explore, and until 2026-08-27 nothing
    verified it — this worked only while the map happened to be on that tab. It is the exact
    mirror of the port-side bug (`_try_port_search` typing into whichever rail was up), and
    `select_world_map_tab`'s docstring records this side failing first: a village list hunted
    on the PORT tab, which opened the trade-goods filter.

    Returns False (never raises) so the caller can fall back to panning.
    """
    import time as _t
    from actions import ui
    from actions.adb_actions import tap as _tap, input_text as _input_text
    from vision.omniparser import parse_fast_cached
    from vision.text_correction import correct_port_name    # folds accents/OCR slips

    if not require_world_map_tab("explore", why="the village list lives on the Explore tab"):
        logger.warning("[village-search] not on the Explore tab — refusing to search "
                       "another tab's rail")
        return False

    frame = capture_screen()
    els = list(parse_fast_cached(frame))

    # 0. ALREADY ON SCREEN? Then tap it — no tab, no panel, no typing.
    hit = _visible_row(els, village)
    if hit is not None:
        logger.info(f"[village-search] {village!r} is already visible @ "
                    f"({hit.cx},{hit.cy}) — tapping it directly")
        _tap(hit.cx, hit.cy)
        _t.sleep(2.0)
        return True

    # 1. the Explore tab (by its NAME, so the tab bar may move)
    if not _village_list_open(frame, els):
        tab = _find_button(frame, "explore", y_max=200)
        if tab is None:
            logger.info("[village-search] no Explore tab on the world map")
            return False
        _tap(*tab)
        _t.sleep(1.5)
        frame = capture_screen()
        els = list(parse_fast_cached(frame))

    # 2. the village-list icon. The house is second, but VERIFY BY EFFECT rather than
    #    trusting the order — the strip's contents differ per tab and per progress.
    if not _village_list_open(frame, els):
        icons = _explore_left_icons(frame, els)
        if not icons:
            logger.info("[village-search] no icon strip on the Explore tab")
            return False
        order = ([icons[_VILLAGE_LIST_ICON_INDEX]] if len(icons) > _VILLAGE_LIST_ICON_INDEX
                 else []) + icons
        for (ix, iy) in order:
            _tap(ix, iy)
            _t.sleep(1.5)
            frame = capture_screen()
            els = list(parse_fast_cached(frame))
            if _village_list_open(frame, els):
                logger.info(f"[village-search] village list opened via the icon @ ({ix},{iy})")
                break
        else:
            logger.info("[village-search] none of the Explore icons opened a village list")
            return False

    # 2b. the list is open — the row may be right there in it
    hit = _visible_row(els, village)
    if hit is not None:
        logger.info(f"[village-search] {village!r} is in the open list @ "
                    f"({hit.cx},{hit.cy}) — tapping without searching")
        _tap(hit.cx, hit.cy)
        _t.sleep(2.0)
        return True

    # 2c. SCROLL THE LIST — no keyboard needed.
    # Typing is the fragile path: tapping the search box raises the soft keyboard, and while
    # it is up the characters sit in the IME's composing buffer ("che | Che | check") instead
    # of reaching the field, so the list never filters and the village reads as absent
    # (measured 2026-08-24, Cheyenne). Scrolling touches no text field at all.
    for _ in range(_VILLAGE_LIST_SCROLLS):
        ui.scroll(_VILLAGE_LIST_SCROLL_X, _VILLAGE_LIST_SCROLL_Y, _VILLAGE_LIST_SCROLL_DY,
                  why="village list — looking for the row")
        _t.sleep(0.8)
        els = list(parse_fast_cached(capture_fn() if capture_fn else capture_screen()))
        hit = _visible_row(els, village)
        if hit is not None:
            logger.info(f"[village-search] {village!r} found by scrolling @ "
                        f"({hit.cx},{hit.cy}) — tapping")
            _tap(hit.cx, hit.cy)
            _t.sleep(2.0)
            return True

    # 3. still not visible — search box, then a PREFIX of the name
    box = next((e for e in els
                if "search" in (getattr(e, "label", "") or "").strip().lower()), None)
    if box is None:
        logger.info("[village-search] the village list has no search box")
        return False
    short = (village or "").replace("Village", "").strip()
    _tap(box.cx, box.cy)
    _t.sleep(1.0)
    # CLEAR FIRST. The box keeps the PREVIOUS query: after searching 'Sve' for Svear, typing
    # 'Che' for Cheyenne produced 'SveChe' and matched nothing, and the village was reported
    # as not in the list (measured 2026-08-24).
    # BATCH INPUT, NOT PER-CHARACTER KEYEVENTS.
    # Tapping the box raises the soft keyboard — which is the NORMAL state on this device
    # (user, 2026-08-24). With an IME up, per-character keyevents COMPOSE: the letters sit in
    # the suggestion bar ("che | Che | check") and never reach the field, so the list stays
    # unfiltered and the village reads as absent while it is simply further down. `input
    # text` commits straight to the focused field regardless of the IME — measured: it
    # filtered the list to Cheyenne/Comanche/Apache first try.
    # (`actions.adb_actions.input_text` types per-character for UNITY text fields, which is
    # right there and wrong here; this box takes the batch form.)
    import subprocess as _sp
    query = short[:_VILLAGE_SEARCH_PREFIX]
    try:
        _sp.run(["adb", "shell", "input", "text", query], check=False, timeout=15)
    except Exception as exc:
        logger.debug(f"[village-search] batch input failed ({exc}) — falling back")
        _input_text(query)
    logger.info(f"[village-search] typed {short[:_VILLAGE_SEARCH_PREFIX]!r} "
                f"(prefix of {village!r})")
    _t.sleep(2.0)

    # 4. the matching row
    frame = capture_screen()
    rows = [e for e in parse_fast_cached(frame)
            if e.cx < 700 and (getattr(e, "label", "") or "").strip()]
    want = short.lower()
    hit = None
    for e in rows:
        lab = (e.label or "").strip().lower()
        if lab.startswith(want) or want in lab:
            hit = e
            break
    if hit is None:
        logger.info(f"[village-search] {village!r} is not in the filtered list")
        return False
    logger.info(f"[village-search] found {village!r} @ ({hit.cx},{hit.cy}) — tapping")
    _tap(hit.cx, hit.cy)
    _t.sleep(2.0)
    return True


def _navigate_world_map_to_village(
    destination: str, from_port: Optional[str] = None,
) -> bool:
    """Pan to a village on the Explore tab and commit the sail.

    Returns True iff sailing started (world map closed and bot is on
    sea / loading), False otherwise.

    Villages DO have a searchable list, on the Explore tab (user, 2026-08-24): the second
    icon in that tab's left bar opens it, with the same Search box the port list has. That is
    tried first; panning to a neighbouring port is the fallback. Reuses the generalised
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

    # SEARCH FIRST, PAN AS THE FALLBACK. The Explore tab carries a village list with the
    # same Search box the ports have (user, 2026-08-24) — this function's docstring used to
    # say villages "don't appear in the toolbar search panel", which is why every village was
    # reached by panning to a neighbouring port and hunting. A name beats an anchor port, a
    # scale estimate and up to 8 stride pans.
    if _try_village_search(destination):
        btn = _find_destination_button(_ocr_frame(capture_screen()), label="village")
        if btn is not None:
            logger.info(f"  {destination!r} selected from the village list")
            pos = btn
        else:
            logger.info("  village list tapped but no 'Move to Village' button — panning")
            pos = pan_to_village(destination, from_port=from_port)
    else:
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

    # COMMIT AND HAND BACK. Tapping "Move to Village" is the world change; whether the map
    # then closed, and what the fleet is doing afterwards, is read by the next perceive.
    # This used to poll `_wait_for_state_change` for up to 8s expecting sea / loading /
    # port_overworld and call anything else an abort — a primitive waiting out a transition
    # and judging it, which is the dispatcher's job.
    logger.info(f"  Tapping 'Move to Village' @ {btn}")
    tap(*btn)

    # THE VILLAGE FLOW MUST ANSWER THE SAME NOTICE THE PORT FLOW DOES.
    # Committing a destination raises "Moving to <X> after Auto Supply. Continue?" and
    # NOTHING happens until it is answered — the port flow calls this straight after
    # selecting its destination. The village flow did not, so the wait below looked for
    # sea/loading/port_overworld while the notice sat on screen classifying as 'unknown',
    # gave up, and the mission aborted (live 2026-08-24, San Village: perception named the
    # dialog eight times — "San Village notice with Continue option", dismissal 'tap_ok' —
    # and nothing ever tapped it). Answering a dialog raised BY our own tap is activity work,
    # not a transition, so it stays here.
    notice = confirm_departure_notice(destination)
    if notice["seen"] and not notice["confirmed"]:
        logger.warning(f"  Could not confirm the departure notice for {destination!r}: "
                       f"{notice['reason']}")
        return False

    logger.info(f"  'Move to Village' committed for {destination!r} — handing back")
    return True

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


def location_panel_is_for(frame, destination: str) -> Optional[bool]:
    """Is the open City/Location Info panel the one for `destination`? None if unreadable.

    CHECK BEFORE ACTING (Guiding Principle #6). The panel's gold commit button is found by
    its own label — "Go to City" — which says what it DOES and nothing about WHERE. Live
    2026-09-01 the world map could not find Barcelona, taps meant for the port list opened
    FARO's panel instead, and the commit was pressed on it: the button was detected correctly,
    on the wrong city.

    THE PANEL, NOT THE FRAME. The map behind it is covered in port names — that same frame
    showed Porto, Azores, Madeira, Valencia and Tunis — so a whole-frame search for the
    destination answers yes almost anywhere. The panel region is the only place the question
    means anything.
    """
    try:
        from actions.world_map_gather import (_PANEL_BOTTOM, _PANEL_LEFT, _PANEL_RIGHT,
                                              _PANEL_TOP)
        crop = frame.crop((_PANEL_LEFT, _PANEL_TOP, _PANEL_RIGHT, _PANEL_BOTTOM))
        text = " ".join((t or "") for t, _c, _x, _y in _ocr_frame(crop, min_conf=0.3))
    except Exception as exc:                       # noqa: BLE001 — unreadable is not "no"
        logger.debug(f"[world-map] could not read the location panel: {exc}")
        return None
    if not text.strip():
        return None
    from actions.world_map_nav import fold_name
    return fold_name((destination or "").split()[0]) in fold_name(text.lower())


def _is_departure_notice(text: str) -> bool:
    """True when this frame's text is a 'Moving to X after Auto Supply. Continue?' notice.

    Matched on the Auto-Supply wording rather than on the word 'Notice' alone, because
    'Notice' titles several unrelated popups and tapping OK on the wrong one is exactly
    the class of mistake that closed the game once already.

    SAYS NOTHING ABOUT WHERE. That is a separate question, and conflating the two is what
    let a notice for the wrong port read as no notice at all — see `confirm_departure_notice`.
    """
    t = (text or "").lower()
    return all(m in t for m in _DEPART_NOTICE_MARKERS)


def _notice_names(text: str, destination: str) -> bool:
    """Does the notice name the place we asked for? Accents folded, as the port lookup folds
    them, so a 'Malé' dialog answers a 'Male' request."""
    from actions.world_map_nav import fold_name
    return fold_name((destination or "").split()[0]) in fold_name((text or "").lower())


def _looks_like_departure_notice(text: str, destination: str) -> bool:
    """The two questions together. Kept for callers that want the old single answer."""
    return _is_departure_notice(text) and _notice_names(text, destination)


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
        if not _is_departure_notice(text):
            if i == 0:
                logger.info("[depart] no auto-supply notice on screen — nothing to confirm")
                return {"seen": False, "confirmed": False, "reason": "no notice"}
            return {"seen": True, "confirmed": True, "reason": "notice cleared"}

        # A NOTICE FOR SOMEWHERE ELSE IS NOT "NO NOTICE" (live 2026-09-01).
        #
        # These were one test, so a dialog reading "Moving to Faro after Auto Supply" while
        # we had asked for Barcelona failed it and took the branch above — "no notice on
        # screen — nothing to confirm" — which `_commit_departure` reads as SUCCESS, because
        # nothing needed confirming. The mission was told its course was set for Barcelona,
        # walked to the harbour, tapped Supply Departure, and sailed with no destination at
        # all. It reached the sea and spent the next twenty minutes asking to enter a market.
        #
        # The fleet is being asked to sail somewhere nobody chose, so this is a hard failure
        # and emphatically not an OK to tap: the caller must go back and set the course it
        # actually wants.
        if not _notice_names(text, destination):
            logger.error(f"[depart] the auto-supply notice does NOT name {destination!r} — "
                         f"refusing to confirm a departure we did not ask for")
            return {"seen": True, "confirmed": False,
                    "reason": f"the notice is for somewhere other than {destination!r}"}

        logger.info(f"[depart] auto-supply notice for {destination!r} — confirming (OK)")
        from actions import ui
        if not ui.tap_text(frame, "ok", why=f"confirm departure to {destination}"):
            logger.warning("[depart] the notice is up but its OK button was not found")
            return {"seen": True, "confirmed": False, "reason": "OK button not found"}
        ui.settle("dialog")

    return {"seen": True, "confirmed": False, "reason": f"notice still up after {attempts} taps"}


def commit_departure(destination: str) -> dict:
    """Select `destination` on the world map and COMMIT it. Returns the moment it is tapped.

    ONE ACTION, THEN HAND BACK. This is the primitive the task runner drives: open the map,
    pick the destination, confirm any notice, tap — and return. It does not wait for the sea,
    does not judge whether the fleet moved, and does not fall back to the harbour. Those are
    decisions, and decisions belong to the loop that perceives between them.

    `depart_from_port_via_world_map` did all of that internally, and the cost was measured on
    2026-08-25: the goal called it ONCE at 16:34 and did not regain control until 16:41. In
    those six minutes the primitive decided the departure had failed, walked to the harbour,
    hunted a Depart button, tapped Supply Departure and re-selected the destination three
    times — while perception was correctly reporting `port_overworld / Amsterdam` to nobody
    who could act on it. The fleet had ALREADY ARRIVED; the task runner would have seen that
    on its next tick and moved to the gather step.

    Returns {ok, reason}. `ok` means the destination was committed, NOT that the fleet moved.
    """
    if not open_world_map():
        return {"ok": False, "reason": "could not open the world map"}

    if not _navigate_world_map_to_destination(destination):
        return {"ok": False, "reason": f"could not select {destination!r} on the map"}

    notice = confirm_departure_notice(destination)
    if notice["seen"] and not notice["confirmed"]:
        return {"ok": False, "reason": "a departure notice appeared and was not confirmed"}

    logger.info(f"[depart] {destination!r} committed — handing back to the task runner, "
                "which will perceive and decide what happens next")
    return {"ok": True, "reason": f"{destination!r} committed"}


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


def _confirm_making_way(motion_wait_s: float, is_moving_fn):
    """(moving, hud_after) — whether the fleet is genuinely MOVING, not merely at sea.

    Two HUD reads separated by enough time for a game-day to tick: motion shows up as a
    falling ETA or a rising day-at-sea. A selected destination is not evidence of movement
    — that is the whole point of the speed-0 bug: the game will happily DISPLAY the
    destination while the tap that set it did nothing at all, and the only cure is to
    re-open the world map and set it again. So movement is the decisive test here and the
    destination readout is not; see `_bound_elsewhere` for the narrow thing it IS good for.

    SPEED IS THE DIRECT ANSWER and is tried first. The game prints the fleet's speed in
    knots in the tile-strip left of the mini-map, so "are we moving?" does not have to be
    inferred from two readings separated by a game-day: >0 is under way, 0.0 is not. That
    makes departure confirmation immediate in the common case instead of a 45s wait, and it
    fixes the case the ETA test cannot answer at all — a SHORT hop, where a 1-day ETA has no
    finer granularity to be seen falling (live 2026-08-24: the fleet reached Bremen while
    this check was still calling `eta=None→1` the speed-0 bug).

    `read_sea_hud` has never carried a `speed` key, so the old `before.get("speed")` here
    was always None and only ever printed as if it were evidence. The reader lives in
    `vision.sea_hud.read_speed`; `locate=True` finds the tile from the mini-map's real
    position, because the fixed crop is derived from a `MINIMAP_CROP` constant that the UI
    has drifted ~120px away from.

    Speed is a signal, not a gate: an unreadable speed (None) falls through to the ETA test
    rather than failing, and a 0.0 immediately after the tap does NOT decide anything on its
    own — the ship may still be accelerating, so it is re-checked after the wait.

    Returns the post-wait HUD too, so callers can inspect the destination without paying
    for another OCR pass."""
    from capture.adb_capture import capture_screen
    from vision.sea_hud import read_speed

    def _speed(frame):
        try:
            return read_speed(frame, locate=True)
        except Exception as exc:
            logger.debug(f"[depart] speed read failed: {exc}")
            return None

    frame = capture_screen()
    before = dict(read_sea_hud(frame))
    speed_before = _speed(frame)
    # Attach the measured speed to the HUD AS SOON AS IT IS READ, on every path. Setting it
    # only on the failure branch made `hud["speed"]` exist or not depending on WHY the call
    # returned, which is not a contract a caller can use.
    before["speed"] = speed_before
    if speed_before is not None and speed_before > 0:
        logger.info(f"[depart] confirmed under way — speed {speed_before} kt")
        return True, before

    time.sleep(max(20.0, motion_wait_s))
    frame = capture_screen()
    after = dict(read_sea_hud(frame))
    speed_after = _speed(frame)
    after["speed"] = speed_after
    if speed_after is not None and speed_after > 0:
        logger.info(f"[depart] confirmed under way — speed {speed_after} kt")
        return True, after

    if is_moving_fn(before, after):
        logger.info(f"[depart] confirmed under way (eta {before.get('eta_days')}→"
                    f"{after.get('eta_days')}d)")
        return True, after

    # The measured speed rides out on the HUD (attached above) so a caller that allows for
    # "cannot tell" — the short-hop case, where a 1-day ETA cannot be seen to fall — can
    # tell a MISSING reading from a definite 0.0. Otherwise its allowance would mask the
    # very bug this function exists to catch.
    if speed_before == 0.0 and speed_after == 0.0:
        logger.warning(f"[depart] speed is 0.0 kt after {motion_wait_s:.0f}s — the fleet is "
                       "NOT moving (the speed-0 bug); the destination must be set again")
    else:
        logger.info(f"[depart] no progress in {motion_wait_s:.0f}s "
                    f"(speed={speed_before}→{speed_after}, eta={before.get('eta_days')}→"
                    f"{after.get('eta_days')})")
    return False, after


# ── world-map pieces the WorldMapActivity uses ───────────────────────────────
#
# Extracted from `_try_port_search` so the activity can call the ACTIONS without the loop
# around them. Each does ONE thing and reports; none verifies its own success — the next
# perceive does that (CLAUDE.md: a primitive that acts does not report whether it worked).

def _destination_list_icon(frame) -> Optional[Tuple[int, int]]:
    """The left-rail list icon, DETECTED on this frame.

    Never a remembered position: one was learned and persisted as `port_list_icon` after it
    opened the VILLAGE list, and because the saved value ranked the candidate sweep it pulled
    later attempts back toward the same wrong icon (2026-08-27). The tab is what decides
    WHICH list this opens, and `require_world_map_tab` settles that before we get here.
    """
    from vision.omniparser import get_omniparser
    parser = get_omniparser()
    if not parser.yolo_available():
        return None
    left = [e for e in parser.parse_fast(frame) if e.cx < 200 and e.cy > 60]
    if not left:
        return None
    left.sort(key=lambda e: e.cy)
    return (left[0].cx, left[0].cy)


def _map_search_box(frame) -> Tuple[int, int]:
    """Where the rail's search box is. Detected, with the observed position as a fallback."""
    from vision.omniparser import parse_fast_cached
    for el in parse_fast_cached(frame):
        lab = (el.label or "").strip().lower()
        if el.cx < 700 and any(k in lab for k in ("search", "edit", "input", "field")):
            return (el.cx, el.cy)
    return (420, 141)


def _type_search_prefix(search_xy: Tuple[int, int], prefix: str) -> None:
    """Tap the box and type a PREFIX at a human interval.

    A prefix, not the whole name: four characters filter the list enough, and typing the lot
    is both slower and more anti-cheat-visible (user, 2026-08-18). `clear_first` wipes a
    leftover query so a re-search does not append onto the previous one.
    """
    from actions.adb_actions import input_text as _input_text, tap as _tap
    _tap(*search_xy)
    time.sleep(1.0)
    _input_text(prefix, max_chars=len(prefix), clear_first=True)
    time.sleep(1.5)


def tap_world_map_control(frame=None) -> dict:
    """Tap the control that opens the world map. ONE tap, and nothing else.

    The globe at a port, the minimap centre at sea. That is all — no waking a lock, no
    exiting a building, no refusing a village, no verifying. `open_world_map`'s ten-attempt
    loop did all five at once, and when it met an OS lock screen, a daily-news popup and an
    Investment Season banner on 2026-08-28 its three available responses — wait, guess, give
    up — were all wrong: it read "Season" out of the banner, called it a port name, and
    reported "Overworld confirmed".

    Those four other jobs have owners now: waking and popups are the bootstrap and the
    clearing activities, leaving a building is the dispatcher's routing, and verifying is the
    next perceive, which happens after every action anyway.

    Returns {tapped, reason} — it does NOT report whether the map opened, because it cannot
    know (CLAUDE.md: a primitive that acts does not report whether it worked).
    """
    # `where_am_i` is defined in THIS module (line ~565), not in brain.perceive. Importing
    # it from there raised ImportError on every call — and this is what `dispatch()` calls for
    # an OPEN_WORLD_MAP intent, so the map could not be opened at all. The tests for this
    # function `inspect.getsource` it and never run it, which is why they stayed green.
    frame = frame if frame is not None else capture_screen()
    loc = (where_am_i(frame) or {}).get("location")

    if loc in ("sea", "sea_cinematic"):
        tx, ty = _sea_minimap_center()
        if tx is None:
            return {"tapped": False, "reason": "the sea minimap region could not be located"}
        logger.info(f"[world-map-tap] SEA — minimap centre @ ({tx},{ty})")
        tap(tx, ty)
        return {"tapped": True, "via": "minimap"}

    if loc == "port_overworld":
        # CORROBORATE A CALIBRATED POINT. A village's left menu is not a port's, and the
        # globe coordinate would land on something else there.
        if _looks_like_a_village(frame):
            return {"tapped": False,
                    "reason": "the left menu is a VILLAGE's — not tapping the port globe"}
        tx, ty = _PORT_WORLD_MAP_GLOBE
        logger.info(f"[world-map-tap] PORT — globe @ ({tx},{ty})")
        tap(tx, ty)
        return {"tapped": True, "via": "globe"}

    if loc == "port_map":
        # THE THIRD PLACE THE GLOBE LIVES (user, 2026-08-29): the sea minimap, the port
        # globe, and the port map overlay's own 'World Map' button — bottom-left, beside the
        # back arrow. `state_fingerprints_data` calls that button "unique to port_map", which
        # is exactly why it can be found by its LABEL rather than a calibrated point: the
        # control says what it is, and it is the one thing this screen is identified by.
        #
        # Without this branch the routing and the primitive disagreed. `to_intent` answers
        # OPEN_WORLD_MAP from port_map — correctly, it is not "inside" anything — and the tap
        # refused with "no world-map control on 'port_map'", which is the same wedge that
        # stopped a run in the market's Sell submenu: dispatch, refuse, repeat, stall.
        pos = _find_button(frame, "world map", "world", allow_title=False)
        if pos is None:
            return {"tapped": False,
                    "reason": "port_map is showing but its 'World Map' button was not found"}
        logger.info(f"[world-map-tap] PORT MAP — 'World Map' button @ {pos}")
        tap(*pos)
        return {"tapped": True, "via": "port_map button"}

    # Anywhere else is not this primitive's problem. It reports, and the dispatcher routes —
    # which is the whole difference from the loop this replaces.
    return {"tapped": False, "reason": f"no world-map control on {loc!r}"}
