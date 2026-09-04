# vision/chrome_via_omniparser.py
# OmniParser-backed chrome flag derivation.
#
# Phase 5c Layer 2: derive ChromeState flags from an OmniParser DetectedElement
# list instead of running OpenCV template matching against pre-captured PNG
# crops.  See docs/vision_layered_perception.md for the rationale.
#
# What this module DOES populate from OmniParser elements alone:
#   - has_back_arrow      → icon present in CHROME_BACK_ARROW_REGION (top-left).
#   - has_world_map_btn   → text element matching "world map" in
#                           CHROME_WORLD_MAP_BTN_REGION (bottom-left).
#   - has_right_panel     → ≥ MIN_RIGHT_PANEL_ELEMENTS detected in
#                           CHROME_RIGHT_PANEL_REGION (4 tab icons + minimap).
#
# What this module CANNOT populate from parse_fast alone:
#   - has_home / has_hamburger.  Both icons share the SAME top-right region
#     and parse_fast does not run Florence-2 captioning, so the two icons
#     come back as element_type="icon", label="icon" — indistinguishable
#     from each other.  Callers that need to distinguish them must layer
#     template matching on top, or use Florence-2 captions when available.
#
# Design choice: this helper returns a ChromeState with has_home and
# has_hamburger left at their defaults (False).  ChromeDetector.detect()
# fuses this OmniParser-derived state with template-matched home/hamburger
# flags before returning.

from __future__ import annotations

from typing import Iterable, List

from config.settings import (
    CHROME_BACK_ARROW_REGION,
    CHROME_RIGHT_PANEL_REGION,
    CHROME_WORLD_MAP_BTN_REGION,
)
from vision.chrome_detector import ChromeState
from vision.omniparser import DetectedElement

# Minimum number of distinct elements within the right-panel region for it
# to be considered "the right panel is present".  The panel contains 4 tab
# icons across the top + the minimap below.  Two clustered elements is a
# conservative threshold that ignores stray single icons.
MIN_RIGHT_PANEL_ELEMENTS: int = 2


def _bbox_centre_in_region(
    el: DetectedElement,
    region: tuple[int, int, int, int],
) -> bool:
    """True if the element's centre point lies inside (left, top, right, bottom)."""
    l, t, r, b = region
    return l <= el.cx <= r and t <= el.cy <= b


def _looks_like_world_map_label(label: str) -> bool:
    """The world-map button has the literal text 'World map' alongside its
    globe icon.  Match loosely on the lowercased text."""
    s = label.lower().strip()
    return "world" in s and "map" in s


def detect_chrome_from_elements(
    elements: Iterable[DetectedElement],
) -> ChromeState:
    """Derive a ChromeState from an OmniParser element list.

    Populates has_back_arrow, has_world_map_btn, has_right_panel.
    Leaves has_home and has_hamburger at their defaults (False) — the caller
    must layer template matching on top to distinguish those two.
    """
    elems: List[DetectedElement] = list(elements)

    has_back_arrow = any(
        _bbox_centre_in_region(el, CHROME_BACK_ARROW_REGION)
        and el.element_type in ("icon", "button")
        for el in elems
    )

    has_world_map_btn = any(
        _bbox_centre_in_region(el, CHROME_WORLD_MAP_BTN_REGION)
        and _looks_like_world_map_label(el.label)
        for el in elems
    )

    right_panel_elements = [
        el for el in elems
        if _bbox_centre_in_region(el, CHROME_RIGHT_PANEL_REGION)
    ]
    has_right_panel = len(right_panel_elements) >= MIN_RIGHT_PANEL_ELEMENTS

    return ChromeState(
        has_hamburger     = False,  # cannot disambiguate from parse_fast
        has_home          = False,  # cannot disambiguate from parse_fast
        has_back_arrow    = has_back_arrow,
        has_world_map_btn = has_world_map_btn,
        has_right_panel   = has_right_panel,
        scores = {
            "back_arrow_via_omniparser":    1.0 if has_back_arrow    else 0.0,
            "world_map_btn_via_omniparser": 1.0 if has_world_map_btn else 0.0,
            "right_panel_via_omniparser":
                float(len(right_panel_elements)) / MIN_RIGHT_PANEL_ELEMENTS,
        },
    )


def fuse_chrome_states(
    omniparser_state: ChromeState,
    template_state: ChromeState,
) -> ChromeState:
    """Combine OmniParser-derived flags (back, world_map, right_panel) with
    template-matched flags (home, hamburger).

    OmniParser is preferred for back/world_map/right_panel because template
    matching has been observed to fail under contrast / position drift
    (e.g. Plymouth right_panel template miss, May-2 incident).

    Template matching is preferred for home/hamburger because parse_fast
    cannot disambiguate icons that share the same region.
    """
    from dataclasses import replace
    return replace(
        omniparser_state,
        has_home      = template_state.has_home,
        has_hamburger = template_state.has_hamburger,
        scores = {**template_state.scores, **omniparser_state.scores},
    )


def port_overworld_is_drawn(frame, elements=None) -> bool:
    """Has a port overworld finished RENDERING, or is the scene still arriving?

    A port arrives in two stages. The 3-D scene draws first; the UI — the name banner top
    left, the ☰, the right-edge tab/minimap/building cluster — lands a beat later. In
    between the frame is unmistakably a port to a classifier that reads pixels, and carries
    none of the things a port is read FOR.

    Live 2026-09-02, four seconds after the sea: the family classifier said port_overworld
    at 1.00 and `read_port_name` returned None, because the banner did not exist yet. The
    voyage's arrival check then compared the destination against a name that had come from
    somewhere other than that frame, decided the fleet was still at Lisboa, and failed a
    mission whose fleet was standing in Faro.

    Measured on that frame against two settled ones (the second with the port-info overlay
    OPEN, which is what a tapped lighthouse icon shows and must NOT read as undrawn):

        Lisboa, settled          right-panel score 2.5   47 elements
        Faro, MID-RENDER         right-panel score 0.0   13 elements
        Faro + overlay, settled  right-panel score 1.5   41 elements

    The right-edge cluster is what separates them, and it is the same cluster the
    port_overworld fingerprint is written on ("port name top-left + right-edge tab
    cluster"). The port NAME alone would also separate these three, but it is one field and
    OCR can lose it on a drawn screen; the cluster is structural.

    This answers "is it drawn", never "where am I" — an undrawn port is still a port.
    """
    if elements is None:
        from vision.omniparser import parse_fast_cached
        elements = parse_fast_cached(frame)
    return bool(detect_chrome_from_elements(elements).has_right_panel)
