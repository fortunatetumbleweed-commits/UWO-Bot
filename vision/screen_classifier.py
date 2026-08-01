# vision/screen_classifier.py
#
# Phase 5e L1 — OmniParser-primary screen classifier.
#
# Replaces the chrome-template + fixed-crop-OCR + Moondream chain that
# `brain/perceive.py:_classify_nav_state` has used so far.  This
# classifier identifies game state from the STRUCTURAL FINGERPRINT of
# OmniParser-detected elements rather than from fixed pixel boxes.
#
# Why:
#
#   - Chrome template matching breaks on contrast / position drift
#     (Plymouth right_panel template miss, May-2 incident).
#   - Fixed-crop OCR breaks if the phone's resolution changes.
#   - Both can fail silently and leave the bot in `state='unknown'`
#     for minutes — the stuck case from May-2 18:00.
#
# OmniParser is robust to visual drift and emits a structured element
# list every frame.  Most game states have a clear structural
# fingerprint that survives those failure modes:
#
#   building          home button + back arrow + building-name title
#                     + left-side sub-menu list
#                     + optional bottom-center building-NPC dialog
#   sub_menu          back arrow + menu-item title
#                     (sibling state of building)
#   port_overworld    top-right icon + port name in top-left
#                     + right-edge tab-bar / minimap cluster
#   world_map         'World Map' title + mode tabs (Port|Explore|Route|Trade)
#                     + 'Go to City' bottom-center
#                     + optional right-side City Info panel
#   port_map          back arrow + 'World map' button bottom-left
#                     + grayscale building icons across map
#   sea               sailing HUD tokens (Day, ETA, Sailing, ...)
#                     in top-left + bottom-center
#   sea_cinematic     no HUD tokens, no corner chrome, mostly empty
#
# Region matching uses NORMALISED coordinates (cx/W, cy/H) so the
# classifier is resolution-agnostic — moving to a different phone with
# a different screen size doesn't break the fingerprints.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from loguru import logger

from vision.omniparser import DetectedElement


# ── Confidence levels ────────────────────────────────────────────────────────

CONFIDENCE_HIGH:    str = "high"
CONFIDENCE_MEDIUM:  str = "medium"
CONFIDENCE_LOW:     str = "low"
CONFIDENCE_UNKNOWN: str = "unknown"


# ── Normalised regions ───────────────────────────────────────────────────────
#
# All bounds are normalised to [0.0, 1.0].  An element's centre is
# computed as (cx/W, cy/H) and tested against the region's bounds.

# Top-left corner — port name, building title, sub-menu title, "World Map".
_TOP_LEFT_REGION = (0.0, 0.0, 0.40, 0.10)

# Back-arrow region — left edge of the top-left corner.
_BACK_ARROW_REGION = (0.0, 0.0, 0.10, 0.10)

# Top-right corner — home / hamburger icon (parse_fast can't disambiguate
# them visually; we identify by what ELSE is on the screen).
_TOP_RIGHT_REGION = (0.65, 0.0, 1.0, 0.10)

# Top-center — world-map mode tabs (Port / Explore / Route / Trade).
_TOP_CENTER_REGION = (0.40, 0.0, 0.65, 0.08)

# Right-edge cluster — port_overworld tab bar + minimap.
_RIGHT_EDGE_REGION = (0.85, 0.05, 1.0, 0.45)

# Bottom-center — building NPC dialog box (fixed position, larger font,
# only inside buildings) and 'Go to City' button on world map.
_BOTTOM_CENTER_REGION = (0.30, 0.80, 0.70, 1.0)

# Bottom-left — 'World map' button on port_map (globe + label).
_BOTTOM_LEFT_REGION = (0.0, 0.80, 0.20, 1.0)


# ── Known label sets ─────────────────────────────────────────────────────────
#
# Lowercased.  These are the canonical English titles and tokens the game
# UI uses.  When the OCR comes back slightly garbled (e.g. 'Harbour' for
# 'Harbor', '1tem Shop' for 'Item Shop'), fuzzy matching elsewhere in the
# stack handles the corrections.

KNOWN_BUILDING_TITLES: frozenset[str] = frozenset({
    "harbor", "harbour", "market", "castle", "inn", "bank",
    "cathedral", "church", "fortune teller", "item shop", "shop",
    "union", "bureau", "mercator estate", "estate", "tavern",
    "blacksmith", "guild", "warehouse", "exchange", "office",
    "shipyard", "palace",
})

KNOWN_SUB_MENU_TITLES: frozenset[str] = frozenset({
    # Market
    "purchase", "sell", "auto-buy", "auto-sell", "negotiation",
    # Inn / harbor
    "recruit crew", "hire crew", "redistribute crew",
    "tavern story", "drink",
    # Bank
    "deposit", "withdraw",
    # Harbor
    "supply", "supply departure", "depart", "depart now",
    "fleet management", "repair", "ready to sail",
})

# Sea HUD tokens — fuzzy substrings.
SEA_HUD_TOKENS: frozenset[str] = frozenset({
    "day", "sailing", "eta", "destination",
    "supply", "remaining",
})

# World-map control labels (chrome, NOT port candidates).
_WORLD_MAP_TITLE_LABELS: frozenset[str] = frozenset({"world map"})
_WORLD_MAP_MODE_TAB_LABELS: frozenset[str] = frozenset({
    "port", "explore", "route", "trade",
})
_GO_TO_CITY_LABEL: frozenset[str] = frozenset({"go to city"})


# ── Result type ──────────────────────────────────────────────────────────────


@dataclass
class ScreenClassification:
    """Result of an OmniParser-driven screen classification.

    Fields:
      state:       one of 'building', 'sub_menu', 'port_overworld',
                   'world_map', 'port_map', 'sea', 'sea_cinematic',
                   'unknown'.
      title:       the screen title text (e.g. 'Harbor', 'Purchase',
                   'World Map') when one is identified.  Empty string
                   otherwise.
      port:        the port name (e.g. 'london') when state is
                   port_overworld and the port name was readable.
                   None otherwise.
      detail:      human-readable explanation of which signals fired.
      confidence:  CONFIDENCE_HIGH / MEDIUM / LOW / UNKNOWN.
      signals:     identifier strings that fired during classification
                   (for debug / logging).
    """
    state:      str = "unknown"
    title:      str = ""
    port:       Optional[str] = None
    detail:     str = ""
    confidence: str = CONFIDENCE_UNKNOWN
    signals:    List[str] = field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _in_region(
    el: DetectedElement,
    region: tuple[float, float, float, float],
    frame_w: int,
    frame_h: int,
) -> bool:
    cx_n = el.cx / frame_w
    cy_n = el.cy / frame_h
    l, t, r, b = region
    return l <= cx_n <= r and t <= cy_n <= b


def _text_elements_in_region(elements, region, frame_w, frame_h):
    out = []
    for el in elements:
        if el.element_type not in ("text", "button"):
            continue
        if not el.label or not el.label.strip():
            continue
        if _in_region(el, region, frame_w, frame_h):
            out.append(el)
    return out


def _icon_elements_in_region(elements, region, frame_w, frame_h):
    return [
        el for el in elements
        if el.element_type in ("icon", "button")
        and _in_region(el, region, frame_w, frame_h)
    ]


def _label_lower_set(elements) -> set[str]:
    return {el.label.lower().strip() for el in elements if el.label and el.label.strip()}


# Minimum glyph height (px) for the title text on a 1080-px-tall frame.
# Port names and screen titles render at ~60-80 px; NPC speech-bubble
# tails, chrome button labels, and discovery banners are ~18-24 px.
# 32 px sits between the two and rejects text-drift into the title
# region.  Scaled by frame height for resolution-independence.
_TITLE_MIN_HEIGHT_NORM = 32 / 1080


def _read_top_left_title(elements, frame_w, frame_h) -> str:
    """Return the title text from the top-left region.

    Picks the **largest-height** text element above a minimum glyph
    height, breaking ties (within 20% of max height) by leftmost-
    topmost order.  See the matching helper in vision/ocr.py for the
    full origin story — 2026-05-15 Amsterdam run picked up "Home"
    from an NPC bubble that drifted into the title region.
    """
    candidates = _text_elements_in_region(
        elements, _TOP_LEFT_REGION, frame_w, frame_h,
    )
    if not candidates:
        return ""
    min_h = _TITLE_MIN_HEIGHT_NORM * frame_h
    big = [el for el in candidates if el.height >= min_h]
    if not big:
        return ""
    max_h = max(el.height for el in big)
    top = [el for el in big if el.height >= 0.8 * max_h]
    top.sort(key=lambda el: (el.y1, el.x1))
    return top[0].label.strip()


# ── State fingerprint matchers ───────────────────────────────────────────────


def _match_building(elements, frame_w, frame_h):
    title = _read_top_left_title(elements, frame_w, frame_h)
    title_lower = title.lower().strip()

    if title_lower in KNOWN_BUILDING_TITLES:
        signals = [f"title='{title}'"]
        if _icon_elements_in_region(elements, _TOP_RIGHT_REGION, frame_w, frame_h):
            signals.append("top_right_icon")
        if _icon_elements_in_region(elements, _BACK_ARROW_REGION, frame_w, frame_h):
            signals.append("back_arrow")
        if _text_elements_in_region(elements, _BOTTOM_CENTER_REGION, frame_w, frame_h):
            signals.append("bottom_center_dialog")

        confidence = CONFIDENCE_HIGH if len(signals) >= 2 else CONFIDENCE_MEDIUM
        return ScreenClassification(
            state="building",
            title=title,
            detail=f"building: {title_lower}",
            confidence=confidence,
            signals=signals,
        )
    return None


def _match_sub_menu(elements, frame_w, frame_h):
    title = _read_top_left_title(elements, frame_w, frame_h)
    title_lower = title.lower().strip()

    if title_lower in KNOWN_SUB_MENU_TITLES:
        signals = [f"title='{title}'"]
        if _icon_elements_in_region(elements, _BACK_ARROW_REGION, frame_w, frame_h):
            signals.append("back_arrow")
        confidence = CONFIDENCE_HIGH if len(signals) >= 2 else CONFIDENCE_MEDIUM
        return ScreenClassification(
            state="sub_menu",
            title=title,
            detail=f"sub_menu: {title_lower}",
            confidence=confidence,
            signals=signals,
        )
    return None


def _match_world_map(elements, frame_w, frame_h):
    title = _read_top_left_title(elements, frame_w, frame_h)
    title_lower = title.lower().strip()

    signals = []
    if title_lower in _WORLD_MAP_TITLE_LABELS:
        signals.append(f"title='{title}'")

    top_center_labels = _label_lower_set(_text_elements_in_region(
        elements, _TOP_CENTER_REGION, frame_w, frame_h,
    ))
    mode_tabs_seen = top_center_labels & _WORLD_MAP_MODE_TAB_LABELS
    if len(mode_tabs_seen) >= 2:
        signals.append(f"mode_tabs={sorted(mode_tabs_seen)}")

    bottom_center_labels = _label_lower_set(_text_elements_in_region(
        elements, _BOTTOM_CENTER_REGION, frame_w, frame_h,
    ))
    if bottom_center_labels & _GO_TO_CITY_LABEL:
        signals.append("go_to_city")

    if not signals:
        return None

    confidence = CONFIDENCE_HIGH if len(signals) >= 2 else CONFIDENCE_MEDIUM
    return ScreenClassification(
        state="world_map",
        title=title if title_lower in _WORLD_MAP_TITLE_LABELS else "World Map",
        detail=f"world_map: {', '.join(signals)}",
        confidence=confidence,
        signals=signals,
    )


def _match_port_map(elements, frame_w, frame_h):
    bottom_left_labels = _label_lower_set(_text_elements_in_region(
        elements, _BOTTOM_LEFT_REGION, frame_w, frame_h,
    ))
    has_world_map_btn = any("world map" in lbl for lbl in bottom_left_labels)
    has_back_arrow = bool(_icon_elements_in_region(
        elements, _BACK_ARROW_REGION, frame_w, frame_h,
    ))
    if has_world_map_btn and has_back_arrow:
        return ScreenClassification(
            state="port_map",
            detail="port_map: world_map_btn + back_arrow",
            confidence=CONFIDENCE_HIGH,
            signals=["world_map_btn", "back_arrow"],
        )
    return None


def _match_port_overworld(elements, frame_w, frame_h, *, known_ports=None):
    title = _read_top_left_title(elements, frame_w, frame_h)
    title_lower = title.lower().strip()

    # Reject if the title is a known building / sub-menu / world-map title —
    # those states are matched by their dedicated matchers earlier.
    if title_lower in KNOWN_BUILDING_TITLES:
        return None
    if title_lower in KNOWN_SUB_MENU_TITLES:
        return None
    if title_lower in _WORLD_MAP_TITLE_LABELS:
        return None

    signals = []
    port_name = None

    if known_ports and title_lower:
        for p in known_ports:
            p_lower = p.lower()
            if title_lower == p_lower or (
                len(title_lower) >= len(p_lower) * 0.6
                and (title_lower in p_lower or p_lower in title_lower)
            ):
                port_name = p_lower
                signals.append(f"port_name='{title}'")
                break

    right_edge_elements = _icon_elements_in_region(
        elements, _RIGHT_EDGE_REGION, frame_w, frame_h,
    )
    if len(right_edge_elements) >= 2:
        signals.append("right_edge_panel")

    if not signals:
        return None

    confidence = CONFIDENCE_HIGH if len(signals) >= 2 else CONFIDENCE_MEDIUM
    return ScreenClassification(
        state="port_overworld",
        title=title,
        port=port_name,
        detail=(
            f"port_overworld: {port_name}" if port_name
            else "port_overworld: right_edge_panel"
        ),
        confidence=confidence,
        signals=signals,
    )


def _match_sea(elements, frame_w, frame_h):
    all_labels = _label_lower_set(elements)
    matched = [tok for tok in SEA_HUD_TOKENS if any(tok in lbl for lbl in all_labels)]
    if len(matched) >= 2:
        return ScreenClassification(
            state="sea",
            detail=f"sea: HUD tokens {matched}",
            confidence=CONFIDENCE_HIGH,
            signals=[f"sea_hud={matched}"],
        )
    return None


# ── Entry point ──────────────────────────────────────────────────────────────


# ── Async learning trigger ───────────────────────────────────────────────────
#
# Track which screens we've already kicked off learning for, keyed by a
# stable signature of the OmniParser element labels.  Prevents re-firing
# Claude for the same screen on every perceive tick while the bot is
# stuck on it.

_LEARNING_TRIGGERED: set[tuple] = set()
_MAX_LEARNING_TRIGGERS = 100   # cap memory; clear oldest when reached


def _signature(elements) -> tuple:
    """Sorted set of element labels — stable identifier for a screen."""
    return tuple(sorted({
        (e.label or "").lower().strip()
        for e in elements
        if e.label and len(e.label.strip()) >= 2
    }))


def _trigger_async_learning(frame, elements: list) -> None:
    """Fire claude_vision.analyse_scene in a background thread.

    Claude analyses the screen, the learning hook derives a Fingerprint
    from the result, and the new fingerprint is auto-registered so the
    NEXT classify_screen call sees it.

    De-duped by element signature — repeated encounters of the same
    unknown screen don't trigger Claude repeatedly.
    """
    if not elements:
        return
    sig = _signature(elements)
    if sig in _LEARNING_TRIGGERED:
        return
    if len(_LEARNING_TRIGGERED) >= _MAX_LEARNING_TRIGGERS:
        _LEARNING_TRIGGERED.clear()
    _LEARNING_TRIGGERED.add(sig)

    import threading

    def _run():
        try:
            from vision.claude_vision import get_claude_vision
            cv = get_claude_vision()
            cv.analyse_scene(
                frame=frame,
                scene_type="unknown",
                screen_title="",
                detected_elements=elements,
            )
        except Exception as exc:
            logger.debug(f"[screen_classifier] async learning failed: {exc}")

    threading.Thread(target=_run, daemon=True, name="claude_learning").start()
    logger.info(
        f"[screen_classifier] no fingerprint match — kicked off Claude "
        f"learning thread ({len(elements)} elements; signature size "
        f"{len(sig)})"
    )


def classify_screen(
    frame,
    elements: Optional[Iterable[DetectedElement]] = None,
    *,
    known_ports: Optional[Iterable[str]] = None,
) -> ScreenClassification:
    """Classify the current screen by structural fingerprint.

    Each matcher is a positive identifier ("are the signals for THIS
    state present?") rather than a process of elimination, so
    misclassification cost is bounded: an unrecognised screen returns
    state='unknown' with confidence=UNKNOWN, leaving the caller free
    to fall back to whatever legacy logic they have.

    Parameters:
      frame:        PIL.Image.  Used for width/height and (if
                    elements is None) to run parse_fast_cached.
      elements:     pre-computed OmniParser DetectedElement list.
                    Pass this when the caller has already run
                    parse_fast_cached(frame) to avoid a duplicate call.
      known_ports:  iterable of canonical port names.  None disables
                    fuzzy port-name matching (other signals still
                    classify port_overworld).
    """
    frame_w = frame.width
    frame_h = frame.height

    if elements is None:
        from vision.omniparser import parse_fast_cached
        elements = parse_fast_cached(frame)
    elements = list(elements)

    if not elements:
        return ScreenClassification(
            state="unknown",
            detail="no OmniParser elements detected",
            confidence=CONFIDENCE_UNKNOWN,
        )

    # Phase 6: registry-based classification.  Each state's fingerprint
    # is data-derived from data/labels.jsonl (see vision/fingerprint_survey.py).
    # The registry-based path is preferred over the ad-hoc matchers below.
    registry_returned_match = False
    try:
        # Importing _data registers all fingerprints into FINGERPRINT_REGISTRY.
        import vision.state_fingerprints_data  # noqa: F401
        from vision.state_fingerprints import classify_via_registry
        from dataclasses import replace
        registry_result = classify_via_registry(elements, frame_w, frame_h)
        if registry_result is not None and registry_result.confidence in (
            CONFIDENCE_HIGH, CONFIDENCE_MEDIUM,
        ):
            registry_returned_match = True
            # Backward-compat: populate title/port from the elements so
            # downstream callers (perceive) keep working unchanged.  The
            # registry evaluator is generic — these per-state niceties
            # are extracted here.
            title_text = _read_top_left_title(elements, frame_w, frame_h)
            kw = {}
            if title_text:
                kw["title"] = title_text
                if (registry_result.state == "port_overworld"
                        and known_ports):
                    t_lower = title_text.lower().strip()
                    for p in known_ports:
                        p_lower = p.lower()
                        if t_lower == p_lower or (
                            len(t_lower) >= len(p_lower) * 0.6
                            and (t_lower in p_lower or p_lower in t_lower)
                        ):
                            kw["port"] = p_lower
                            break
            if kw:
                registry_result = replace(registry_result, **kw)
            logger.debug(
                f"[screen_classifier] registry matched {registry_result.state!r} "
                f"({registry_result.confidence}) signals={registry_result.signals}"
            )
            return registry_result
    except Exception as e:
        logger.debug(f"[screen_classifier] registry path skipped: {e}")

    # Phase 6.5 — when the registry has no fingerprint for this screen,
    # kick off Claude scene-analysis in a background thread.  Claude's
    # response is captured by the learning hook in claude_vision.py
    # (_maybe_save_learned_candidate), which derives a Fingerprint from
    # the OmniParser elements and registers it immediately, so the very
    # next call to classify_screen for the same kind of screen matches
    # without falling through to legacy.
    #
    # The current call still falls through to legacy and returns
    # whatever it can — the bot doesn't block waiting for Claude.
    # Subsequent encounters benefit from the learned fingerprint.
    if not registry_returned_match:
        _trigger_async_learning(frame, list(elements))

    # Legacy ad-hoc matchers — fall through when the registry returned
    # no match or only LOW confidence.  Will be removed once registry
    # coverage is comprehensive.
    matchers = (
        _match_building,
        _match_sub_menu,
        _match_world_map,
        _match_port_map,
        lambda e, w, h: _match_port_overworld(e, w, h, known_ports=known_ports),
        _match_sea,
    )

    for matcher in matchers:
        result = matcher(elements, frame_w, frame_h)
        if result is not None:
            logger.debug(
                f"[screen_classifier] legacy matched {result.state!r} "
                f"({result.confidence}) signals={result.signals}"
            )
            return result

    return ScreenClassification(
        state="unknown",
        detail=f"no fingerprint matched; {len(elements)} elements detected",
        confidence=CONFIDENCE_UNKNOWN,
    )
