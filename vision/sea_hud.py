"""Sea-HUD text readers — speed and lat/lon.

Both readers use direct EasyOCR on tight crops (~30 ms each) rather
than OmniParser (~10 s) — locations calibrated 2026-05-30:

  - SPEED_CROP   = (1875, 220, 2000, 280)
    Vertical strip immediately LEFT of the mini-map, just below the
    "C/TW" tab.  Previous OmniParser zone (1900-2400, 80-420) was
    picking up the wrong token (day count or wind dir) and returned
    a stale 10.0/12.0 even when actual speed was 0.0.

  - LATLON_CROP  = (2255, 360, 2395, 400)
    Bottom-right INSIDE the mini-map area.  Active-sailing frames
    show "LL.LL,LL.LL" overlaid on the mini-map's bottom-right
    corner (see e.g. hug_debug_20260530_133557/tick_0030.png).
    Frames where the ship is sail_stopped show lat/lon elsewhere
    (in the "Restart Auto Move" pill near the bottom-center); for
    those this reader returns None and the caller can fall back to
    the prior reading.

Both return None on unreadable frames.  Callers should treat None as
"no reading this tick" rather than retrying.
"""
from __future__ import annotations

import math
import re

from loguru import logger
from typing import Optional, Tuple

# Lazy singleton — RapidOCR (ONNX-port of PP-OCR) is used ONLY for the
# lat/lon HUD read.  Recognizer-only mode (use_det=False) on the tight
# 140×40 crop is ~10 ms vs EasyOCR's ~75 ms, AND catches the leading
# minus glyph that CRAFT drops at equator crossing (session
# ai_nav_2026-07-27T20-20-44 t502-t509: EasyOCR read `-0.52,33.75` as
# `0,52,33.75`, dropping the sign and cascading into a 180° motion-
# lock-break heading flip).  The rest of the pipeline (speed, other
# HUD text) continues to use EasyOCR via actions.water_tap._get_reader.
_RAPID_OCR = None
def _get_rapid_ocr():
    global _RAPID_OCR
    if _RAPID_OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _RAPID_OCR = RapidOCR()
    return _RAPID_OCR

# Sea-HUD crops are expressed *relative to* the current mini-map bbox
# rather than as absolute frame coordinates.  The whole sea-view UI can
# slide horizontally by 100+ px between runs (locked at sail_start —
# see memory/project_ui_position_locked_at_sail_start_2026-07-21.md);
# absolute crops silently break OCR and dead-reckoning after the shift.
#
# Reference calibration (2026-06-25 / 2026-06-23) against the default
# (a historical MINIMAP_CROP value, (1979, 202, 2384, 395); the live one now
#  comes from vision.minimap_navigation_view.get_minimap_crop()):
#   SPEED_CROP  = (1910, 240, 1970, 280)   →  60×40 strip
#     LEFT of mini-map, in the C/TW column, at offsets from mm_x0/mm_y0:
#     x ∈ [-69, -9]  y ∈ [+38, +78]
#   LATLON_CROP = (2255, 360, 2395, 400)   →  140×40 strip
#     Bottom-right INSIDE mini-map, at offsets from mm_x1/mm_y1:
#     x ∈ [-129, +11]  y ∈ [-35, +5]

# Fallback absolute crops used when the mini-map bbox isn't available
# (rare — direct callers should pass the current MINIMAP_CROP).
SPEED_CROP  = (1910, 240, 1970, 280)
LATLON_CROP = (2255, 360, 2395, 400)


def latlon_crop_for(minimap_crop: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Compute the LATLON_CROP from the current MINIMAP_CROP."""
    _, _, mx1, my1 = minimap_crop
    return (mx1 - 129, my1 - 35, mx1 + 11, my1 + 5)


def _current_latlon_crop() -> tuple[int, int, int, int]:
    """Live LATLON_CROP based on the current MINIMAP_CROP module attr."""
    try:
        from brain.ai_nav.vision_input import MINIMAP_CROP
        return latlon_crop_for(MINIMAP_CROP)
    except Exception:
        return LATLON_CROP


# Same regex as tools/sail_capture.py — kept in sync.
_LATLON_RE = re.compile(r"^\s*(-?\d{1,3}\.\d{1,2})\s*,\s*(-?\d{1,3}\.\d{1,2})\s*$")
# Speed token after OCR — typically "27.0", "11.5", "0.0".  Strict
# decimal form; anything else is rejected.
_SPEED_RE = re.compile(r"^\s*(\d{1,2}\.\d{1,2})\s*$")


# ── Locating the speed tile instead of assuming where it is ──────────────────
#
# The speed number sits in the gauge strip attached to the LEFT of the overworld right panel:
# tide state, then a ship icon over the speed (a DECIMAL, e.g. "27.5"), a windsock over wind
# strength (an integer), water over current strength (an integer).
#
# THE STRIP IS PART OF THE PANEL, so it is found with it — see
# `vision.region_detectors.overworld_panel`, which anchors on the season row and measures
# every box from what it reads. Nothing here is an offset from anything any more.
#
# WHAT WAS HERE BEFORE, kept because it is the argument for the change. The band was four
# offsets from `MINIMAP_CROP` (230 left, inset 5, 10 down, 180 tall) with `SPEED_CROP` as an
# absolute fallback. The offsets produced a 225px-wide band over open sea for an 84px tile,
# which READ CORRECTLY — the speed is the only decimal in the strip and `_SPEED_RE` demands
# one, so the looseness was deliberate and it worked. The fallback did not: measured
# 2026-08-24, `MINIMAP_CROP` said the map began at x 1984 when its real left edge is ~1862,
# a ~120px error that put the crop INSIDE the mini-map disc, and `read_speed` returned None
# on every at-sea frame until the locate path was added. 1862 is, to within two pixels, the
# panel edge the detector now measures on its own.
#
# `SPEED_CROP` survives below because `tools/calibrate_ship_arc.py` still saves crops with it.
# It is a calibration tool's constant now, not a reader's.
#
# (A second copy of this detection still lives inline in `tools/run_ai_nav_live.py`, which
# recalibrates `MINIMAP_CROP` at startup. The two should be consolidated.)


def locate_minimap_bbox(elements) -> Optional[tuple]:
    """The mini-map compound from OmniParser elements, or None.

    Thin alias for the canonical detector in `vision.minimap_navigation_view`, which is where
    the mini-map's geometry lives; kept here so speed-tile callers read naturally.
    """
    from vision.minimap_navigation_view import detect_minimap_bbox
    return detect_minimap_bbox(elements)


def locate_speed_band(img, elements=None) -> Optional[tuple]:
    """The speed tile's box, taken from the overworld right panel. None if it is not there.

    THROUGH THE PANEL, AND ONLY THROUGH IT (user, 2026-09-11: *"I would like it to be only
    accessed through the panel, because they exist at the same time"*). The gauge strip is
    part of that panel and is drawn at sea and nowhere else, so "no panel" and "no speed to
    read" are the same fact — and returning None then is the honest answer, not a degradation.
    `SeaActivity` already treats an unreadable speed as deciding nothing and falls through to
    the ETA.

    WHAT THIS REPLACES, and why the old way was not merely different. The band used to be
    computed as four offsets from the mini-map: 230px left of it, inset 5, 10 down, 180 tall.
    Measured on frame 0588 of `data/sessions/trace_barter_cmd_2026-09-11T13-47-33` that gave
    (1632, 211, 1857, 391) — 225px wide against a tile that is 84px wide, starting 146px left
    of the strip, out over open sea. It read correctly anyway, because the SPEED IS THE ONLY
    DECIMAL in the strip and `_SPEED_RE` demands one; the looseness was deliberate and it
    worked. What did not work was the fallback beneath it. `SPEED_CROP` is (1915, 243, 1975,
    283) and the panel's minimap runs x[1864,2266] y[200,399], so that crop lands INSIDE the
    map disc. The module's own comment records it failing exactly that way on 2026-08-24,
    when `MINIMAP_CROP` said 1984 and the real edge was ~1862 — which is, to within two
    pixels, the panel's left edge this now measures.

    THE CELL IS FOUND BY ITS CONTENT, not by its index in the strip. `gauges[1]` is the speed
    tile on every frame looked at so far, and relying on that would be the same assumption as
    a calibrated coordinate wearing a different hat — a port that stacks the cells otherwise,
    or a strip that gains one, would silently read the wind. So the caller scans the cells and
    keeps the decimal, which is the rule that made the loose band safe in the first place.
    """
    boxes = speed_candidate_boxes(img, elements)
    return boxes[0] if boxes else None


def speed_candidate_boxes(img, elements=None) -> list:
    """Every gauge cell of the overworld right panel, top to bottom. Empty when at a port.

    One of them holds the speed. Which one is decided by what is IN it, not by where it sits.
    """
    if elements is None:
        try:
            from vision.omniparser import parse_fast_cached
            elements = list(parse_fast_cached(img))
        except Exception:
            return []
    try:
        from vision.region_detectors.overworld_panel import detect_overworld_panel
        panel = detect_overworld_panel(img, elements)
    except Exception as exc:                       # noqa: BLE001 — a read, not a decision
        logger.debug(f"[sea_hud] overworld panel unavailable: {exc}")
        return []
    return list(panel.gauges) if panel is not None else []


def read_speed(img, *, elements=None, locate: bool = False) -> Optional[float]:
    """Extract current sailing speed (knots) from a tight crop just
    left of the mini-map.

    Returns None if no plausible reading is found.  Typical valid
    range is 0–30 (top speed ~27 for fast ships, ~11 for slow).
    **0.0 is a real reading, not a failure** — it is the whole point of
    the speed-0 check, so callers must distinguish it from None.

    `locate=True` (or passing `elements`) finds the tile from the
    mini-map's ACTUAL position first and only falls back to the fixed
    crop — use it wherever the UI may have drifted.  It is opt-in
    because locating costs an OmniParser parse (~2-3s), which the
    manual-navigation loop cannot afford at its sub-1s cadence; that
    path calibrates `MINIMAP_CROP` once at startup instead.
    """
    try:
        import numpy as np
        from actions.water_tap import _get_reader
    except Exception:
        return None
    # THE PANEL'S GAUGE CELLS, and nothing else. No offsets from the mini-map and no
    # absolute fallback: the strip and the panel are drawn together, so if the panel is not
    # there, there is no speed on screen to read.
    boxes = speed_candidate_boxes(img, elements)

    raw = []
    for box in boxes:
        try:
            raw = _get_reader().readtext(np.asarray(img.crop(box)), detail=1)
        except Exception:
            continue
        if any(_SPEED_RE.match((t or "").strip().replace(",", ".").replace(" ", ""))
               for _b, t, _c in raw):
            break
    # EasyOCR returns [(bbox, text, conf), …].  Speed crop is single
    # line; iterate over all tokens, pick the highest-confidence
    # decimal that parses cleanly.
    best: Optional[tuple[float, float]] = None  # (conf, value)
    for _bbox, text, conf in raw:
        if conf < 0.30:
            continue
        # Strip stray non-digit / non-decimal artefacts (commas → dots).
        t = (text or "").strip().replace(",", ".").replace(" ", "")
        m = _SPEED_RE.match(t)
        if not m:
            continue
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        if not (0.0 <= v <= 50.0):
            continue
        if best is None or conf > best[0]:
            best = (conf, v)
    return best[1] if best else None


def read_latlon(
    img,
    prev_latlon: Optional[Tuple[float, float]] = None,
) -> Optional[Tuple[float, float]]:
    """Read the ship's world lat/lon from the bottom-right corner
    inside the mini-map area.

    Returns (lat, lon) or None.  The format is two signed decimal
    numbers separated by a comma (e.g. "33.59,13.50").  Returns None
    when the ship is sail_stopped — the lat/lon shows in a different
    place then; callers should reuse their last reading.

    `prev_latlon` is an optional tiebreaker hint: when present, the
    parser uses it to disambiguate OCR garbles (e.g. "8.73" misread
    as "8773" when a mini-map marker partially occludes the "."), by
    preferring the candidate interpretation closest to it.  See
    `_parse_latlon_text` for the full strategy.

    Implementation notes:
      - The text is small (~16 px tall in the source); EasyOCR
        consistently returns empty on the raw crop.  We upsample 2×
        before OCR — measured to bring confidence from 0.0 to ~0.45.
      - Even with upscaling, decimal points often drop out — "31.63,
        17.77" becomes "31 63 17 77".  Parser accommodates by
        joining four 2-digit integers into (XX.YY, ZZ.WW).
      - Diamond mini-map markers occasionally overlap a digit and
        the decimal becomes a digit ("8.73" → "8773").  See
        `project_latlon_marker_occlusion.md`; Path C below recovers
        via plausibility-vs-prev.
    """
    try:
        import numpy as np
    except Exception:
        return None
    crop = img.crop(_current_latlon_crop())
    # 2× upscale — small thin text gets missed without it.
    crop = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)
    arr = np.asarray(crop)
    try:
        # Recognizer-only: skip detection (crop is already tight and fixed
        # position; det stage was dropping the leading minus glyph).
        raw, _elapse = _get_rapid_ocr()(
            arr, use_det=False, use_cls=False, use_rec=True,
        )
    except Exception:
        return None
    if not raw:
        return None
    tokens = [text for text, conf in raw if conf >= 0.20]
    if not tokens:
        return None
    joined = " ".join(tokens)
    return _parse_latlon_text(joined, prev_latlon)


def _parse_latlon_text(
    joined: str,
    prev_latlon: Optional[Tuple[float, float]] = None,
) -> Optional[Tuple[float, float]]:
    """Parser for the OCR-joined token string.  Extracted from
    `read_latlon` so it's testable without invoking EasyOCR.

    Tries three strategies, in order:

      - Path A: clean "31.63,17.77" — regex finds two decimals.
      - Path B: decimals dropped — "31 63 17 77" reassembles as two
        XX.YY pairs.
      - Path C: digit-run plausibility — handles cases like
        "8773,32,69" where a misread "." inflated a number.  Requires
        `prev_latlon` to disambiguate; without it we conservatively
        return None rather than guess.
    """
    # Path A: strict game format — HUD always shows `LAT.YY,LON.YY`
    # with LAT 1-2 digits, LON 1-3 digits, and EXACTLY 2 decimal digits
    # on each side.  Sprite occlusion (Nubia village text overlay in
    # ai_nav_2026-07-22T20-33-50 t109-t111) drops the leading digit of
    # one axis, producing garbles like "83,32.19" that the old loose
    # `\d{1,3}\.\d{1,2}` regex accepted as (83.32, 2.19).  Requiring
    # `.YY` on both sides rejects those garbles → fallthrough to Path C.
    #
    # Two OCR variants covered:
    #   Variant 1: "22.65,32.15" — clean, comma preserved as separator
    #   Variant 2: "23,00,32.23" — decimal misread as comma (then
    #              normalized to "23.00.32.23" with optional inter-
    #              separator dot).
    for text, sep in ((joined, r"\s*,\s*"),
                       (joined.replace(",", "."), r"\.?")):
        m = re.search(
            rf"(?<!\d)(-?\d{{1,2}}\.\d{{2}}){sep}(-?\d{{1,3}}\.\d{{2}})(?!\d)",
            text,
        )
        if m:
            try:
                lat, lon = float(m.group(1)), float(m.group(2))
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    return lat, lon
            except ValueError:
                pass

    # Path B: decimals dropped — "31 63 17 77" or "31 63 17.77".
    # Same reasoning as Path A: four clean digit pieces reassembling
    # into two in-range XX.YY values is trustworthy structural evidence.
    pieces = re.findall(r"\d{1,3}\.\d{1,2}|\d{1,3}", joined)
    if len(pieces) >= 4:
        try:
            def pair(a, b):
                if "." in a:
                    return float(a)
                return float(f"{int(a)}.{int(b):02d}"[:7])
            lat = pair(pieces[0], pieces[1])
            lon = pair(pieces[2], pieces[3])
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
        except (ValueError, IndexError):
            pass

    # Path C: digit-run plausibility — only when prev_latlon supplied.
    if prev_latlon is not None:
        return _parse_with_prior(joined, prev_latlon)

    return None


# Plausibility budget for one HUD-read interval (~5 ticks ≈ 12 s).
# Latitude is ≈ 111 km/° everywhere on Earth, so 3° lat per read interval
# (~33 km) is impossibly fast for a sailing ship.  Longitude is also
# 111 km/° at the equator but only ≈ 19 km/° at 80° latitude, so this
# budget is conservatively wrong near the poles — fine for the game's
# Mediterranean / Nile / Atlantic operating range; revisit if voyages
# go above ±60° lat.  Same threshold on both axes for simplicity.
_MAX_LATLON_DELTA_PER_READ_DEG = 3.0


# Confirmation-required acceptance gate for clean Path-A / Path-B
# parses that disagree with `prev_latlon` by more than this distance.
# Calibrated from an observed Nile voyage where cruise + cache-then-
# refresh jumps stayed below 1.0° while a stuck-bad OCR lock landed
# at 14-63° from prev.  A 2.0° threshold sits cleanly in the gap.
_JUMP_THRESHOLD_DEG = 2.0

# Number of consecutive matching wild reads required to confirm a
# jump.  2 = "the next tick agrees with this one"; smaller is laggy,
# larger is over-cautious.  Bounds cascade-lock-in to N ticks instead
# of the 100+ that motivated removing the original motion gate.
_JUMP_CONFIRM_COUNT = 2

# Velocity-extrapolation residual threshold.  When ≥2 accepted history
# entries exist, the gate linearly extrapolates the next expected
# position and rejects candidates that deviate beyond this distance
# from the prediction.  Catches hundredths-digit OCR misreads
# (27.59 → 27.0) that the static `_JUMP_THRESHOLD_DEG` (compares to
# prev only) can't, because the bad value is geographically close to
# `prev` while being far from where the ship is actually moving.
# See the t44-t68 failure pattern in `explore_port_20260606_230213`
# and the diagnosis in `memory/project_first_autonomous_nile_descent_2026_06_06.md`.
_VELOCITY_RESIDUAL_DEG = 0.3

# A small dataclass-shaped tuple the caller threads from tick to
# tick alongside `prev_latlon`.  Encoded as a plain tuple so JSON
# logging is trivial.  `value` is the candidate (lat, lon) we are
# waiting to confirm; `count` is how many consecutive reads have
# agreed with it.  Both `None` / 0 means no pending jump.
PendingJump = Tuple[Optional[Tuple[float, float]], int]
_NO_PENDING: PendingJump = (None, 0)

# Position history threaded by the caller alongside `prev` + `pending`.
# Each entry is `(lat, lon, tick)`.  Keep small — only the last two
# are needed for linear extrapolation; we hold a few more so a noisy
# tick can be skipped without breaking the velocity estimate.
PositionHistory = list  # list[Tuple[float, float, int]]
_HISTORY_FOR_VELOCITY = 4


def accept_or_defer(
    candidate: Optional[Tuple[float, float]],
    prev:      Optional[Tuple[float, float]],
    pending:   PendingJump = _NO_PENDING,
    threshold_deg: float = _JUMP_THRESHOLD_DEG,
    confirm_count: int = _JUMP_CONFIRM_COUNT,
    history:   Optional[list] = None,
    current_tick: Optional[int] = None,
    velocity_residual_deg: float = _VELOCITY_RESIDUAL_DEG,
) -> Tuple[Optional[Tuple[float, float]], PendingJump]:
    """Confirmation-required acceptance gate.

    Decides whether to accept the parser's candidate reading given
    the previous accepted value and the pending-jump state from the
    last tick.  Pure function; the caller threads `pending` from tick
    to tick the same way it already threads `prev`.

    Returns `(accepted_value, new_pending)`:
      - `accepted_value` is the (lat, lon) to use this tick, or
        `None` when the candidate is being deferred for confirmation.
        Caller should reuse its last-known position when `None`.
      - `new_pending` is the updated state to thread into the next
        call.

    Three branches:

    1. **No candidate / no prev**: nothing to gate against.  Return
       candidate unchanged (first reading of a voyage, or this tick
       had no OCR hit at all).
    2. **Candidate within `threshold_deg` of prev**: accept; reset
       pending.  Normal cruise + cache-refresh case.
    3. **Candidate beyond threshold from prev**: defer unless the
       same wild value has been seen `confirm_count` times in a row.
       If the new candidate disagrees with both `prev` and the
       pending value, reset the pending counter to 1 (start fresh).
    """
    if candidate is None or prev is None:
        return candidate, _NO_PENDING

    # Two parallel plausibility checks — candidate is "wild" if it
    # fails either:
    #   (a) static jump check vs `prev` — catches wholesale digit
    #       garbles (e.g. lon 30 → 61 → 30 cascade).
    #   (b) velocity-extrapolation residual vs a linear prediction
    #       from `history` — catches hundredths-digit misreads
    #       (27.59 → 27.0) where the bad value is close to `prev`
    #       but far from where the ship is actually moving.
    static_wild = _euclid_deg(candidate, prev) > threshold_deg
    velocity_wild = False
    if (history is not None and current_tick is not None
            and len(history) >= 2):
        predicted = _predict_next_position(history, current_tick)
        if predicted is not None:
            velocity_wild = (
                _euclid_deg(candidate, predicted) > velocity_residual_deg
            )
    if not (static_wild or velocity_wild):
        return candidate, _NO_PENDING

    # Candidate is wild relative to prev OR predicted.
    pending_val, pending_n = pending
    if (pending_val is not None
            and _euclid_deg(candidate, pending_val) <= threshold_deg):
        # Same wild value as last tick — increment confirmation count.
        new_n = pending_n + 1
        if new_n >= confirm_count:
            # Confirmed.  Accept and reset.
            return candidate, _NO_PENDING
        return None, (candidate, new_n)

    # Different wild value than what we were pending (or no pending) —
    # reset and start a fresh confirmation cycle for this value.
    return None, (candidate, 1)


def _euclid_deg(
    a: Tuple[float, float], b: Tuple[float, float],
) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _predict_next_position(
    history: list,
    current_tick: int,
) -> Optional[Tuple[float, float]]:
    """Linear extrapolation from the last two history entries.

    `history` is a list of `(lat, lon, tick)`.  Returns the predicted
    (lat, lon) for `current_tick`, or None when history is too short
    or degenerate (same tick recorded twice → undefined velocity).
    """
    if len(history) < 2:
        return None
    (lat1, lon1, t1) = history[-2]
    (lat2, lon2, t2) = history[-1]
    dt = t2 - t1
    if dt <= 0:
        return None
    steps = current_tick - t2
    dlat = (lat2 - lat1) / dt
    dlon = (lon2 - lon1) / dt
    return (lat2 + dlat * steps, lon2 + dlon * steps)


def _within_motion_budget(
    lat: float, lon: float,
    prev: Optional[Tuple[float, float]],
) -> bool:
    """True when the new (lat, lon) is within plausible distance of
    `prev`.  When `prev` is None (first read) anything passes — caller
    can't disambiguate without prior context."""
    if prev is None:
        return True
    return (abs(lat - prev[0]) <= _MAX_LATLON_DELTA_PER_READ_DEG
            and abs(lon - prev[1]) <= _MAX_LATLON_DELTA_PER_READ_DEG)


def _parse_with_prior(
    joined: str,
    prev: Tuple[float, float],
) -> Optional[Tuple[float, float]]:
    """Path C — recover the most plausible (lat, lon) from OCR garble.

    Strategy: extract all digit runs, regardless of decimal placement.
    Try every way to split runs into a lat-token and a lon-token; for
    each token try every decimal position; filter by valid range
    (|lat|≤90, |lon|≤180); among surviving (lat, lon) candidates,
    return the one minimising squared Euclidean distance to `prev`.

    Concrete reference case: OCR returns "8773,32,69" (the "." in
    "8.73" was misread as "7" because a mini-map marker overlapped
    it).  Digit runs are ["8773", "32", "69"].  Splits to consider:
        (8773), (32, 69)  →  lat from "8773", lon from "3269"
        (8773, 32), (69)  →  lat from "877332", lon from "69"
        (8773), (32)       →  lat from "8773", lon from "32"
        (8773), (69)       →  lat from "8773", lon from "69"
    For lat "8773", possible values with decimal: 8.773, 87.73, 877.3.
    Valid (|lat|≤90): 8.773, 87.73.
    For lon "3269": 3.269, 32.69, 326.9.  Valid: 3.269, 32.69.
    Of (8.773,3.269), (8.773,32.69), (87.73,3.269), (87.73,32.69) the
    closest to prev=(8.50,33.16) is (8.773, 32.69).  Return that.
    """
    digit_runs = re.findall(r"\d+", joined)
    if not digit_runs:
        return None

    def decimal_candidates(
        digits: str, max_abs: float, include_drops: bool,
    ) -> list[float]:
        """All ways to interpret a string of digits as a decimal
        number, filtered by absolute-value range.  When
        `include_drops` is True, also tries dropping the leading or
        trailing digit to handle OCR-prepend garbles (e.g. t331:
        overlay rendered as a leading "2" turned "8.73" into
        "2873"; dropping the leading digit recovers 873 → 8.73)
        AND prepending a "1"/"2"/"3" to handle OCR-drop garbles
        where the pirate sprite obscures the leading digit (e.g.
        "27.7" → "7.7" or "33.04" → "13.04" during Nile-descent
        voyages 2026-07-22 t38-t42).

        Drops/prepends are reserved as a fallback so they don't
        introduce spurious-close candidates that beat the legitimate
        as-is interpretation when both are plausible (cf. the
        "31 63 17.77" mixed-decimal case).
        """
        out: list[float] = []
        if not digits:
            return out
        variants = [digits]
        if include_drops and len(digits) >= 2:
            variants.append(digits[1:])    # drop leading digit
            variants.append(digits[:-1])   # drop trailing digit
        if include_drops:
            # Prepend leading digit — recovers OCR-drop garbles where
            # the pirate sprite obscures the leading digit.  Only
            # tries "1","2","3" (world coord magnitudes rarely exceed
            # 3-digit lat/lon for this game).
            for p in ("1", "2", "3"):
                variants.append(p + digits)
        seen: set[float] = set()
        for variant in variants:
            for pos in range(1, min(len(variant), 3) + 1):
                try:
                    v = float(f"{variant[:pos]}.{variant[pos:]}")
                    if abs(v) <= max_abs and v not in seen:
                        seen.add(v)
                        out.append(v)
                except ValueError:
                    pass
        return out

    def collect(include_drops: bool) -> list[tuple[float, float]]:
        """Enumerate (lat, lon) candidates over every split of
        digit_runs into two non-empty groups.  `include_drops`
        toggles the drop-digit variants.

        Previous versions also tried consecutive run-pairs (digit
        runs i, i+1 as lat/lon directly), but those generated spurious
        in-budget candidates that beat the legitimate drop-variant
        interpretations — e.g. "28,73,32,82" had run-pair (73, 32)
        produce (7.3, 32), which is within motion budget of
        prev (8.67, 32.88) by coincidence but wildly wrong.  Splits
        alone cover every right answer because adjacent OCR runs are
        already contiguous within a split.
        """
        cands: list[tuple[float, float]] = []
        n = len(digit_runs)
        for split in range(1, n):
            lat_digits = "".join(digit_runs[:split])
            lon_digits = "".join(digit_runs[split:])
            for lat in decimal_candidates(lat_digits, 90.0, include_drops):
                for lon in decimal_candidates(lon_digits, 180.0,
                                                include_drops):
                    cands.append((lat, lon))
        return cands

    def dist2(c: tuple[float, float]) -> float:
        return (c[0] - prev[0]) ** 2 + (c[1] - prev[1]) ** 2

    # Pass 1: as-is candidates within motion budget.  Prefer these so
    # legitimate readings (e.g. "31.63,17.77") aren't beaten by drop-
    # variants that happen to land marginally closer to prev.
    cands = [c for c in collect(include_drops=False)
              if _within_motion_budget(c[0], c[1], prev)]
    if cands:
        return min(cands, key=dist2)

    # Pass 2: drop-variant candidates within motion budget.  Catches
    # the t331 OCR-prepend case ("2873" → drop leading → "873" → 8.73).
    cands = [c for c in collect(include_drops=True)
              if _within_motion_budget(c[0], c[1], prev)]
    if cands:
        return min(cands, key=dist2)

    # Pass 3: no in-budget candidates anywhere.  Either OCR is very
    # garbled or `prev` is stale (e.g. long blackout, bot teleported).
    # Fall back to closest-to-prev among all candidates including
    # drops, to preserve the previous (less safe) Path-C behaviour.
    cands = collect(include_drops=True)
    if not cands:
        return None
    return min(cands, key=dist2)


# We import Image lazily inside read_latlon if needed; centralise here
# to avoid a hot-path import.
from PIL import Image  # noqa: E402
