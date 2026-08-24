# vision/ocr.py
# OCR helpers for reading text and numbers from captured frames.
#
# Phase 5e L2: the public helpers (read_port_name, read_screen_title,
# read_building_menu) now use OmniParser-detected text elements as the
# primary signal, with the legacy fixed-crop OCR path as a fallback
# when OmniParser is unavailable.
#
# Why: fixed pixel crops (OCR_PORT_NAME_REGION, SCREEN_TITLE_REGION,
# BUILDING_MENU_REGION) break when the phone resolution changes and
# silently fail when the game UI shifts.  OmniParser detects text
# elements wherever they actually are, and querying by NORMALISED
# region (cx/W, cy/H) is resolution-agnostic.

from __future__ import annotations

from typing import Iterable, Optional

import numpy as np
import easyocr
from PIL import Image

from config.settings import (
    OCR_PORT_NAME_REGION,
    BUILDING_MENU_REGION,
    BUILDING_MENU_OCR_MIN_CONFIDENCE,
    SCREEN_TITLE_REGION,
)

# Initialise once — model load is expensive (~2–3 s on first call)
_reader: easyocr.Reader | None = None


def _ocr_gpu_available() -> bool:
    """Use the GPU (Apple MPS / CUDA) for EasyOCR when present.  Measured 2026-08-18 on
    MPS: 1.62 s vs 6.06 s per full-frame OCR (~3.7× faster) with IDENTICAL output.  OCR is
    the dominant per-tick perceive cost (each sail tick ran several full-frame reads at
    ~6 s on CPU → 26-32 s/tick), so this cuts nav/sail tick time substantially.  Falls back
    to CPU on machines without a GPU (CI, etc.)."""
    try:
        import torch
        if torch.cuda.is_available():
            return True
        mps = getattr(torch.backends, "mps", None)
        return bool(mps and mps.is_available())
    except Exception:
        return False


def _get_reader() -> easyocr.Reader:
    global _reader
    if _reader is None:
        _reader = easyocr.Reader(["en"], gpu=_ocr_gpu_available(), verbose=False)
    return _reader


def read_text(region: Image.Image) -> str:
    """Run OCR on *region* and return the recognised string (stripped)."""
    arr = np.array(region)
    results = _get_reader().readtext(arr, detail=0)
    return " ".join(results).strip()


def read_number(region: Image.Image) -> int | None:
    """Convenience wrapper: run OCR and parse the result as an integer."""
    text = read_text(region).replace(",", "").replace(".", "").strip()
    try:
        return int(text)
    except ValueError:
        return None


def _looks_like_real_text(text: str) -> bool:
    """Sanity-check an OCR result — returns False on obvious garbage.

    Heuristics:
      - Must contain at least one vowel (a e i o u)
      - Vowel ratio must be strictly > 25 % (rejects "ccbaccot" style noise
        but accepts legitimate words like "harbor", "castle", etc.)
    """
    if not text:
        return False
    letters = [c for c in text.lower() if c.isalpha()]
    if not letters:
        return False
    vowels = set("aeiou")
    vowel_count = sum(1 for c in letters if c in vowels)
    if vowel_count == 0:
        return False
    return vowel_count / len(letters) > 0.25


# ── Normalised regions (Phase 5e L2) ─────────────────────────────────────────
#
# Centre-of-element matched against (left, top, right, bottom) all in [0, 1].

# Top-left title region — port name, building title, sub-menu title, "World Map".
_TITLE_REGION_NORM = (0.0, 0.0, 0.40, 0.10)

# Building list panel on port_overworld — right edge, mid-screen down.
# y_max 0.97 captures the very-bottom row (e.g. Fortune Teller at cy
# ~1030 in 1080-height frames).  The Atlantic Ocean / dotted-UID /
# build-version strip at cy >= 1060 still ends up inside the region
# but is filtered out by `_is_building_list_noise` below — far cleaner
# than the previous arbitrary cap that clipped legitimate rows.
_BUILDING_MENU_REGION_NORM = (0.77, 0.35, 1.0, 0.97)


# Content patterns that mark a "building list row" as system-status
# noise, NOT a building.  These appear in the bottom strip and
# sometimes get OCR-merged with the lowest visible building label.
# Filtered out of read_building_menu so navigate_to_building does not
# try to fuzzy-match against e.g. 'fortune teller 4.0501.041.291
# 2605141023 atlantic ocean'.
import re as _re_ocr
_BUILDING_LIST_NOISE_PATTERNS = (
    # Server names (English UI shows ocean names; extend as new servers appear)
    _re_ocr.compile(r"\batlantic\s+ocean\b", _re_ocr.IGNORECASE),
    _re_ocr.compile(r"\bpacific\s+ocean\b",  _re_ocr.IGNORECASE),
    _re_ocr.compile(r"\bindian\s+ocean\b",   _re_ocr.IGNORECASE),
    _re_ocr.compile(r"\barctic\s+ocean\b",   _re_ocr.IGNORECASE),
    # Player UID — looks like 4.0501.041.291 or 2605141023
    _re_ocr.compile(r"\b\d{2,4}[.,]\d{3,5}[.,]\d{3,4}"),
    _re_ocr.compile(r"\b\d{8,12}\b"),
    # Version strings like 12.605.141023
    _re_ocr.compile(r"\b\d+\.\d+\.\d+"),
    # Date/season header row at the TOP of the right panel (above the
    # building list).  Doesn't scroll with the list; it's chrome.
    # Filtering these stabilises the building-list signature so the
    # scroll-stable early-exit fires correctly.
    _re_ocr.compile(r"^\d{1,2}[.:]\d{2}$"),   # 11.42, 09:34 — clock
    _re_ocr.compile(
        r"^(spring|summer|autumn|fall|winter|wet\s+season|dry\s+season)$",
        _re_ocr.IGNORECASE,
    ),
    _re_ocr.compile(
        r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\.?$",
        _re_ocr.IGNORECASE,
    ),
)


# A leading ICON artifact: OmniParser/OCR renders a building's icon as junk before
# the name — notably the Harbour's anchor ⚓ becomes "&", giving "& Harbor". Strip a
# leading run of non-alphanumerics so building matching sees a clean "harbor".
_ICON_PREFIX_RE = _re_ocr.compile(r"^[^0-9a-z]+")


def _strip_icon_prefix(label: str) -> str:
    return _ICON_PREFIX_RE.sub("", label)


def _is_building_list_noise(label: str) -> bool:
    """True if *label* matches a known system-status pattern that should
    not appear in the building list (server name, UID, build version)."""
    if not label:
        return False
    for pat in _BUILDING_LIST_NOISE_PATTERNS:
        if pat.search(label):
            return True
    return False


def _try_get_omniparser_elements(frame):
    """Return parse_fast_cached(frame) if OmniParser is available, else None.

    Wrapped in try/except so import or runtime failures fall through to the
    legacy fixed-crop path silently.  Logged at debug level for diagnosis.
    """
    try:
        from vision.omniparser import get_omniparser, parse_fast_cached
        if not get_omniparser().yolo_available():
            return None
        return parse_fast_cached(frame)
    except Exception:
        return None


def _element_in_norm_region(el, region, frame_w, frame_h) -> bool:
    cx_n = el.cx / frame_w
    cy_n = el.cy / frame_h
    l, t, r, b = region
    return l <= cx_n <= r and t <= cy_n <= b


# Minimum glyph height (px) for the port-name / screen-title text on a
# 1080-px-tall frame.  The real title font is ~60-80 px tall; NPC speech
# bubbles, chrome labels (Home/Back), and discovery overlays are ~18-24 px.
# 32 px sits comfortably between the two and rejects accidental text drift
# into the title region.  Scaled by frame height so other resolutions work.
_TITLE_MIN_HEIGHT_NORM = 32 / 1080


def _top_left_title_from_elements(elements, frame_w, frame_h) -> str:
    """Find the title text in the top-left region.

    The title (port name on port_overworld, building name in a building,
    sub-menu name in a sub-menu, "World Map" on the world map) is always
    rendered in a much larger font than any other text that can drift
    into the title region: NPC speech-bubble tails, chrome button labels,
    or discovery-overlay banners.

    Pick rule: **largest-height text element** in the title region,
    above a minimum glyph height.  Falls back to leftmost-topmost only
    if there's a tie within 20% of the max height (defensive — the real
    font-size gap is typically 3-4×, but the tiebreaker keeps behaviour
    stable when two title-sized elements coexist).

    Origin: 2026-05-15 Amsterdam run picked up "Home" from an NPC bubble
    tail that drifted into the title region.  The leftmost-topmost rule
    used here previously had no awareness of font size, so a 20 px
    "Home..." text won over the 70 px "Amsterdam" port label.
    """
    min_h = _TITLE_MIN_HEIGHT_NORM * frame_h
    candidates = []
    for el in elements:
        if el.element_type not in ("text", "button"):
            continue
        if not el.label or not el.label.strip():
            continue
        if not _element_in_norm_region(el, _TITLE_REGION_NORM, frame_w, frame_h):
            continue
        if el.height < min_h:
            continue
        candidates.append(el)
    if not candidates:
        return ""
    max_h = max(el.height for el in candidates)
    # Keep elements within 20% of the tallest.
    top = [el for el in candidates if el.height >= 0.8 * max_h]
    top.sort(key=lambda el: (el.y1, el.x1))

    # Multi-word titles like "Las Palmas" / "Item Shop" / "Fortune
    # Teller" / "Port Royal" get tokenised into multiple elements by
    # OmniParser.  Concatenate same-row top-candidates left-to-right
    # if they're vertically adjacent (Δy within half the tallest's
    # height — i.e. on the same baseline).
    # Origin: 2026-05-22 — Las Palmas was being read as "Las" because
    # only top[0] was returned; SailToGoal then couldn't match the
    # port name against the catalogue.
    head = top[0]
    same_row_tolerance = max(20, head.height * 0.5)
    same_row = [el for el in top if abs(el.y1 - head.y1) <= same_row_tolerance]
    same_row.sort(key=lambda el: el.x1)
    if len(same_row) > 1:
        parts = [el.label.strip() for el in same_row if el.label.strip()]
        merged = " ".join(parts)
        return merged
    return head.label.strip()


# ── Public helpers ───────────────────────────────────────────────────────────


def read_screen_title(
    frame: Image.Image,
    *,
    elements=None,
) -> str:
    """Read the top-left title area that every screen shows.

    Reference values:
      port overworld / port map  → port name
      world map                  → 'World Map'
      building interior          → building name
      sub-menu                   → sub-menu name (Purchase, Sell, ...)

    Returns lowercased text, or '' if the OCR result looks garbled.

    *elements*: pre-computed OmniParser DetectedElement list.  Pass when
    the caller already has parse_fast_cached(frame) for some other
    purpose to skip the duplicate parse_fast call.
    """
    if elements is None:
        elements = _try_get_omniparser_elements(frame)

    if elements is not None:
        title = _top_left_title_from_elements(
            elements, frame.width, frame.height,
        )
        if title:
            # OmniParser-sourced text is already conf ≥ 0.4 filtered;
            # the vowel-ratio garbage check (designed for fixed-crop
            # OCR noise) wrongly rejects names like 'Plymouth' (2/8 =
            # 25 %, ≤ threshold) and 'World Map' (2/8 = 25 %).  Trust
            # the upstream confidence filter and return directly.
            return title.lower().strip()

    # Legacy fixed-crop fallback.
    region = frame.crop(SCREEN_TITLE_REGION)
    result = read_text(region).lower().strip()
    if not _looks_like_real_text(result):
        if result:
            from loguru import logger
            logger.debug(f"Screen title OCR discarded as garbled: {result!r}")
        return ""
    return result


def read_port_name(
    frame: Image.Image,
    *,
    elements=None,
) -> str | None:
    """Return the port name shown in the top-left, or None if unreadable.

    *elements*: pre-computed OmniParser list (skips duplicate parse_fast).

    The raw OCR read is passed through `vision.text_correction.correct_port_name`
    which fuzzy-matches against the known port list (KB + seed).  This
    canonicalises corrupted reads like 'Amsterdamads!' / 'Amsterdanted'
    back to 'Amsterdam' — see the function's docstring for the empirical
    background.  When fuzzy match returns None (no candidate above the
    similarity cutoff), we return the raw read with a warning so the
    caller can decide whether to retry or escalate.
    """
    from vision.text_correction import correct_port_name as _correct
    from loguru import logger

    raw: str | None = None
    if elements is None:
        elements = _try_get_omniparser_elements(frame)

    if elements is not None:
        title = _top_left_title_from_elements(
            elements, frame.width, frame.height,
        )
        if title:
            # OmniParser-sourced text is already conf ≥ 0.4 filtered;
            # skip the vowel-ratio garbage check that rejected legitimate
            # port names like 'Plymouth' (just below the 25 % threshold).
            raw = title.strip()

    if raw is None:
        # Fallback crop — use the NORMALIZED title region, NOT the absolute
        # OCR_PORT_NAME_REGION: the 118px camera-notch/landscape shift moves the port
        # name out of a fixed pixel box (the Malé port-name=None → round-trip bug). A
        # normalized crop rides the shift. See docs/ui_region_cluster_perception_design.md
        # / project_notch_orientation_shifts_ui_118px.
        w, h = frame.width, frame.height
        x0, y0, x1, y1 = _TITLE_REGION_NORM
        region = frame.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
        legacy = read_text(region)
        if not legacy:
            return None
        if not _looks_like_real_text(legacy):
            logger.debug(f"Port name OCR discarded as garbled: {legacy!r}")
            return None
        raw = legacy

    # Fuzzy match against known ports.  Canonicalise on success; pass
    # through with a warning on miss so the caller can react.
    canonical, ratio = _correct(raw)
    if canonical is not None:
        return canonical
    # A bare UI / tab title ('Purchase', 'Sell', 'Village') is definitively NOT a
    # port name — return None so callers (e.g. _is_on_overworld) don't treat it as
    # a visible port and falsely confirm overworld on a market screen.  Only the
    # raw pass-through survives, for plausibly-novel port names not yet catalogued.
    from vision.text_correction import _is_generic_title
    if _is_generic_title(raw):
        logger.debug(f"[read_port_name] {raw!r} is a UI title, not a port — returning None")
        return None
    # A port name is a NAME. It never contains digits, so anything with a number in it is
    # a stat readout, a counter or a coordinate — not a place.
    #
    # This matters because callers treat ANY non-empty return as proof of where they are.
    # Live 2026-08-22: on the main menu this read the player's level, 'LV 92', and
    # `_is_on_overworld` concluded "Overworld confirmed: port name 'LV 92' visible" on all
    # five labelled main-menu frames. `exit_to_overworld` then reported success while the
    # bot sat on the main menu, and `open_world_map` — which classifies properly — kept
    # waiting for a port, so the two deadlocked until the run gave up.
    # (Earlier the same day the same pass-through returned 'World' on the world map and
    #  aborted a destination selection.)
    if any(ch.isdigit() for ch in raw):
        logger.debug(f"[read_port_name] {raw!r} contains digits — not a port name, "
                     "returning None")
        return None
    logger.warning(
        f"[read_port_name] raw OCR {raw!r} did not match any known port "
        f"(best similarity {ratio:.2f}); returning raw read"
    )
    return raw


def read_building_menu(
    frame: Image.Image,
    *,
    elements=None,
) -> list[tuple[str, int, int]]:
    """Scan the right-side building menu panel and return every detected
    label as (text, abs_x, abs_y) in full-frame pixel coordinates,
    sorted top-to-bottom.

    *elements*: pre-computed OmniParser list (skips duplicate parse_fast).
    """
    if elements is None:
        elements = _try_get_omniparser_elements(frame)

    if elements is not None:
        entries: list[tuple[str, int, int]] = []
        for el in elements:
            if el.element_type not in ("text", "button"):
                continue
            if not el.label or not el.label.strip():
                continue
            if not _element_in_norm_region(
                el, _BUILDING_MENU_REGION_NORM, frame.width, frame.height,
            ):
                continue
            if el.confidence < BUILDING_MENU_OCR_MIN_CONFIDENCE:
                continue
            label = _strip_icon_prefix(el.label.strip().lower())   # '& harbor' → 'harbor'
            if len(label) < 2:
                continue
            if _is_building_list_noise(label):
                continue       # server name / UID / build version
            entries.append((label, el.cx, el.cy))
        if entries:
            entries.sort(key=lambda e: e[2])
            return entries
        # If OmniParser found nothing in the panel region, that probably
        # means the panel isn't open.  Fall through to legacy crop only as
        # a defensive fallback (caller may legitimately want to scan
        # elsewhere).

    # Legacy fixed-crop fallback.
    region = frame.crop(BUILDING_MENU_REGION)
    arr = np.array(region)
    results = _get_reader().readtext(arr, detail=1)

    ox, oy = BUILDING_MENU_REGION[0], BUILDING_MENU_REGION[1]
    entries: list[tuple[str, int, int]] = []
    for bbox, text, confidence in results:
        if confidence < BUILDING_MENU_OCR_MIN_CONFIDENCE:
            continue
        label = _strip_icon_prefix(text.strip().lower())   # '& harbor' → 'harbor'
        if len(label) < 2:
            continue
        if _is_building_list_noise(label):
            continue       # server name / UID / build version
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        cx = int((min(xs) + max(xs)) / 2) + ox
        cy = int((min(ys) + max(ys)) / 2) + oy
        entries.append((label, cx, cy))

    entries.sort(key=lambda e: e[2])
    return entries


if __name__ == "__main__":
    from capture.adb_capture import capture_screen
    frame = capture_screen()
    port = read_port_name(frame)
    print(f"Current port: {port!r}")
