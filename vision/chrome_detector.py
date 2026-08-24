# vision/chrome_detector.py
# Deterministic scene classifier based on stable UI chrome elements.
#
# The game's chrome (button positions, panel layout) is fixed and unaffected
# by dynamic content such as day/night cycle, weather, NPCs, or chat bubbles.
# Detecting a small set of chrome elements is enough to classify every scene:
#
#   hamburger (≡) present                    →  port_overworld
#   flag + back arrow, no hamburger          →  port_map
#   back arrow, no flag, title = building    →  building_interior
#   back arrow, no flag, title ≠ building    →  sub_menu
#   none of the above                        →  unknown  (escalate to LLM)
#
# Templates live in vision/assets/chrome/ as small PNG crops.
# Capture them once from a live screen:
#   python -m vision.chrome_detector capture
#
# Run standalone to test classification on the current screen:
#   python -m vision.chrome_detector

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from loguru import logger
from PIL import Image

from config.settings import (
    CHROME_TEMPLATES_DIR,
    CHROME_HAMBURGER_REGION,
    CHROME_HOME_REGION,
    CHROME_BACK_ARROW_REGION,
    CHROME_WORLD_MAP_BTN_REGION,
    CHROME_RIGHT_PANEL_REGION,
    CHROME_MATCH_THRESHOLD,
)

# Building names that can appear as the screen title when inside a building.
# Any title NOT in this set (and not a port name) is treated as a sub-menu item
# (e.g. "Purchase", "Sell", "Hire Crew").
KNOWN_BUILDING_TITLES: frozenset[str] = frozenset({
    "harbor", "harbour", "market", "castle", "inn", "bank",
    "cathedral", "church", "fortune teller", "item shop", "shop",
    "union", "bureau", "mercator estate", "estate", "tavern",
    "blacksmith", "guild", "warehouse", "exchange", "office",
    "shipyard",
})

# Template filenames (relative to CHROME_TEMPLATES_DIR)
_TEMPLATE_FILES: dict[str, str] = {
    "hamburger":       "hamburger.png",
    "home":            "home.png",
    "back_arrow":      "back_arrow.png",
    "world_map_btn":   "world_map_btn.png",   # globe+"World map" label, bottom-left of port map
    "right_panel":     "right_panel.png",
}

# Search regions for each element: (left, top, right, bottom)
_REGIONS: dict[str, tuple[int, int, int, int]] = {
    "hamburger":     CHROME_HAMBURGER_REGION,
    "home":          CHROME_HOME_REGION,
    "back_arrow":    CHROME_BACK_ARROW_REGION,
    "world_map_btn": CHROME_WORLD_MAP_BTN_REGION,
    "right_panel":   CHROME_RIGHT_PANEL_REGION,
}


# ── ChromeState ───────────────────────────────────────────────────────────────

@dataclass
class ChromeState:
    """Result of a chrome detection pass."""

    has_hamburger:       bool = False
    has_home:            bool = False
    has_back_arrow:      bool = False
    has_world_map_btn:   bool = False   # globe+"World map" label, unique to port map
    has_right_panel:     bool = False

    # Match scores for each element (0–1), useful for debugging
    scores: dict[str, float] = field(default_factory=dict)

    # Where each matched element actually IS, as frame coordinates of its centre.
    # Callers must tap THIS rather than a remembered slot: the game re-bakes its
    # camera-cutout offset per screen, so the chrome row slides between screens. On
    # Jakarta's Market (2026-08-21) the Home icon sat at centre x≈2219 while
    # actions/screen_exit.py tapped a fixed (2300, 45) — 81px away, on nothing.
    positions: dict[str, tuple[int, int]] = field(default_factory=dict)

    def classify(self, ocr_title: str) -> str:
        """
        Apply the decision tree to determine scene type.

        ocr_title should be the lowercased screen title from OCR
        (or "" if OCR returned nothing).

        Decision tree:
          world_map_btn present                →  port_map       (unique to port map)
          hamburger present                    →  port_overworld
          back_arrow, no world_map_btn/burger:
            title is a known building name     →  building_interior
            title is a menu item / empty       →  sub_menu
          right_panel only                     →  port_overworld  (fallback)
          none of the above                    →  unknown
        """
        title = ocr_title.lower().strip()

        # ── World map button → port map ──────────────────────────────────────
        # The globe + "World map" label appears ONLY on the port map overlay.
        if self.has_world_map_btn:
            return "port_map"

        # ── Hamburger → port overworld ───────────────────────────────────────
        if self.has_hamburger:
            return "port_overworld"

        # ── Back arrow, inside something ─────────────────────────────────────
        if self.has_back_arrow:
            if title in KNOWN_BUILDING_TITLES:
                return "building_interior"
            # Title is a menu item (Purchase, Sell, Hire Crew, …) or empty
            return "sub_menu"

        # ── Home button without hamburger or world_map_btn ───────────────────
        # Home appears in buildings AND on the port map.
        # world_map_btn already ruled out port_map above, so if we see home
        # here we must be inside a building (back_arrow template may have missed).
        if self.has_home:
            if title in KNOWN_BUILDING_TITLES:
                return "building_interior"
            return "sub_menu"

        # ── Right panel without hamburger/back — treat as overworld ──────────
        if self.has_right_panel:
            return "port_overworld"

        return "unknown"

    def normalize(self, scene: str) -> "ChromeState":
        """
        Return a copy with flags cleared that are impossible for *scene*.

        Template matching produces false positives when similar-looking
        elements exist in the search region.  Once the scene is classified,
        we know which elements cannot be present — clearing them prevents
        downstream code from acting on spurious matches.

          port_overworld   → no home, no back_arrow, no world_map_btn
          port_map         → no hamburger, no right_panel
          building_interior/sub_menu → no hamburger, no world_map_btn, no right_panel
        """
        from dataclasses import replace
        if scene == "port_overworld":
            return replace(self, has_home=False, has_back_arrow=False,
                           has_world_map_btn=False)
        if scene == "port_map":
            return replace(self, has_hamburger=False, has_right_panel=False)
        if scene in ("building_interior", "sub_menu"):
            return replace(self, has_hamburger=False, has_world_map_btn=False,
                           has_right_panel=False)
        return self

    def summary(self) -> str:
        flags = []
        if self.has_hamburger:     flags.append("hamburger")
        if self.has_home:          flags.append("home")
        if self.has_back_arrow:    flags.append("back_arrow")
        if self.has_world_map_btn: flags.append("world_map_btn")
        if self.has_right_panel:   flags.append("right_panel")
        return f"chrome=[{', '.join(flags) or 'none'}]"


# ── ChromeDetector ────────────────────────────────────────────────────────────

class ChromeDetector:
    """
    Detects stable chrome UI elements using OpenCV template matching.

    Templates are loaded once at construction and cached.  A missing template
    file causes that element to be skipped (treated as not present) so the
    classifier degrades gracefully while templates are still being calibrated.
    """

    def __init__(self, threshold: float = CHROME_MATCH_THRESHOLD) -> None:
        self.threshold = threshold
        self._templates: dict[str, np.ndarray | None] = {}
        self._load_templates()

    def _load_templates(self) -> None:
        base = Path(CHROME_TEMPLATES_DIR)
        for name, filename in _TEMPLATE_FILES.items():
            path = base / filename
            if path.exists():
                img = cv2.imread(str(path), cv2.IMREAD_COLOR)
                if img is not None:
                    self._templates[name] = img
                    logger.debug(f"Chrome template loaded: {name} ({path.name})")
                else:
                    logger.warning(f"Chrome template unreadable: {path}")
                    self._templates[name] = None
            else:
                logger.debug(f"Chrome template not found (skipped): {path}")
                self._templates[name] = None

    @property
    def templates_available(self) -> set[str]:
        return {k for k, v in self._templates.items() if v is not None}

    def detect(self, frame: Image.Image) -> ChromeState:
        """
        Run template matching on *frame* and return a ChromeState.

        Missing templates are treated as not present (score = 0.0).
        """
        frame_bgr = cv2.cvtColor(np.array(frame), cv2.COLOR_RGB2BGR)
        scores: dict[str, float] = {}
        positions: dict[str, tuple[int, int]] = {}

        def _match(name: str) -> bool:
            tmpl = self._templates.get(name)
            if tmpl is None:
                scores[name] = 0.0
                return False
            l, t, r, b = _REGIONS[name]
            region = frame_bgr[t:b, l:r]
            if region.size == 0 or tmpl.shape[0] > region.shape[0] or tmpl.shape[1] > region.shape[1]:
                scores[name] = 0.0
                return False
            result = cv2.matchTemplate(region, tmpl, cv2.TM_CCOEFF_NORMED)
            _mn, score, _mnl, max_loc = cv2.minMaxLoc(result)
            score = float(score)
            scores[name] = score
            if score >= self.threshold:
                # Record the CENTRE of the match in frame coords, so callers tap where the
                # element is rather than where it used to be.
                positions[name] = (int(l + max_loc[0] + tmpl.shape[1] / 2),
                                   int(t + max_loc[1] + tmpl.shape[0] / 2))
                return True
            return False

        state = ChromeState(
            has_hamburger     = _match("hamburger"),
            has_home          = _match("home"),
            has_back_arrow    = _match("back_arrow"),
            has_world_map_btn = _match("world_map_btn"),
            has_right_panel   = _match("right_panel"),
            scores            = scores,
            positions         = positions,
        )

        logger.debug(
            f"{state.summary()}  "
            f"scores={{{', '.join(f'{k}:{v:.2f}' for k, v in scores.items())}}}"
        )
        return state

    def detect_with_omniparser(
        self,
        frame: Image.Image,
        elements,
    ) -> ChromeState:
        """Run detection using a pre-computed OmniParser element list for
        the back-arrow / world-map-button / right-panel flags, and template
        matching only for home / hamburger (which parse_fast cannot
        disambiguate).

        Use this path when a parse_fast() result is already available for
        the frame — it avoids running template matching for the three
        OmniParser-disambiguatable flags and is more robust to template
        drift (Plymouth right_panel miss, May-2 incident).
        """
        from vision.chrome_via_omniparser import (
            detect_chrome_from_elements,
            fuse_chrome_states,
        )

        omni_state = detect_chrome_from_elements(elements)

        # Template-match only what OmniParser can't disambiguate from
        # parse_fast: home and hamburger share the top-right region.
        frame_bgr = cv2.cvtColor(np.array(frame), cv2.COLOR_RGB2BGR)
        scores: dict[str, float] = {}
        positions: dict[str, tuple[int, int]] = {}

        def _match(name: str) -> bool:
            tmpl = self._templates.get(name)
            if tmpl is None:
                scores[name] = 0.0
                return False
            l, t, r, b = _REGIONS[name]
            region = frame_bgr[t:b, l:r]
            if region.size == 0 or tmpl.shape[0] > region.shape[0] or tmpl.shape[1] > region.shape[1]:
                scores[name] = 0.0
                return False
            result = cv2.matchTemplate(region, tmpl, cv2.TM_CCOEFF_NORMED)
            _mn, score, _mnl, max_loc = cv2.minMaxLoc(result)
            score = float(score)
            scores[name] = score
            if score >= self.threshold:
                # Record the CENTRE of the match in frame coords, so callers tap where the
                # element is rather than where it used to be.
                positions[name] = (int(l + max_loc[0] + tmpl.shape[1] / 2),
                                   int(t + max_loc[1] + tmpl.shape[0] / 2))
                return True
            return False

        template_state = ChromeState(
            has_hamburger = _match("hamburger"),
            has_home      = _match("home"),
            scores        = scores,
        )

        fused = fuse_chrome_states(omni_state, template_state)
        logger.debug(
            f"{fused.summary()}  (omniparser-backed; "
            f"template-only flags: home={template_state.has_home}, "
            f"hamburger={template_state.has_hamburger})"
        )
        return fused

    def classify_scene(self, frame: Image.Image, ocr_title: str) -> tuple[str, ChromeState]:
        """
        Convenience wrapper: detect chrome then classify.

        Returns (scene_type, chrome_state).
        If no templates are loaded, returns ("unknown", empty ChromeState)
        so the caller can fall back to the LLM.
        """
        if not self.templates_available:
            logger.debug("No chrome templates available — scene classification skipped")
            return "unknown", ChromeState()

        state = self.detect(frame)
        scene = state.classify(ocr_title)
        state = state.normalize(scene)
        return scene, state


# ── Template capture utility ──────────────────────────────────────────────────

_CAPTURE_INSTRUCTIONS: dict[str, str] = {
    "hamburger":     "Port overworld — ≡ hamburger button visible top-right",
    "home":          "Inside any building — ⌂ home button visible top-right",
    "back_arrow":    "Inside any building OR port map open — ← back arrow top-left",
    "world_map_btn": "Port map open — globe + 'World map' label visible bottom-left",
    "right_panel":   "Port overworld — right panel visible (tab bar + mini map)",
}


def capture_template(name: str) -> None:
    """
    Capture a single chrome template crop from the live screen.

    Usage:
        python -m vision.chrome_detector capture <name>

    Where <name> is one of: hamburger, home, back_arrow, world_map_btn, right_panel
    """
    if name not in _TEMPLATE_FILES:
        print(f"Unknown template '{name}'. Choose from: {', '.join(_TEMPLATE_FILES)}")
        return

    from capture.adb_capture import capture_screen

    base = Path(CHROME_TEMPLATES_DIR)
    base.mkdir(parents=True, exist_ok=True)

    out = base / _TEMPLATE_FILES[name]
    desc = _CAPTURE_INSTRUCTIONS[name]

    print(f"\nCapturing: {name}")
    print(f"Required screen state: {desc}")
    input("Press ENTER when ready… ")

    frame = capture_screen()
    frame_bgr = cv2.cvtColor(np.array(frame), cv2.COLOR_RGB2BGR)
    l, t, r, b = _REGIONS[name]
    crop = frame_bgr[t:b, l:r]
    cv2.imwrite(str(out), crop)
    print(f"Saved → {out}  ({crop.shape[1]}×{crop.shape[0]} px)")
    print(f"Test with:  python -m vision.chrome_detector")


def capture_all_templates() -> None:
    """Capture all chrome templates in sequence."""
    for name in _TEMPLATE_FILES:
        capture_template(name)
    print("\nAll templates captured.")


# ── module-level singleton ────────────────────────────────────────────────────

_detector: ChromeDetector | None = None


def get_chrome_detector() -> ChromeDetector:
    global _detector
    if _detector is None:
        _detector = ChromeDetector()
    return _detector


# ── standalone ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from memory.logger import setup_logging

    setup_logging()

    args = sys.argv[1:]

    if args and args[0] == "capture":
        # python -m vision.chrome_detector capture <name>   — capture one template
        # python -m vision.chrome_detector capture           — capture all
        if len(args) >= 2:
            capture_template(args[1])
        else:
            capture_all_templates()

    elif args and args[0] == "list":
        # python -m vision.chrome_detector list  — show which templates are present
        base = Path(CHROME_TEMPLATES_DIR)
        print("Chrome templates:")
        for name, filename in _TEMPLATE_FILES.items():
            path = base / filename
            status = "✓" if path.exists() else "✗ missing"
            print(f"  {status}  {name:16s}  {_CAPTURE_INSTRUCTIONS[name]}")

    else:
        # python -m vision.chrome_detector  — classify current screen
        from capture.adb_capture import capture_screen
        from vision.ocr import read_screen_title

        frame = capture_screen()
        title = read_screen_title(frame)
        detector = get_chrome_detector()

        raw_state = detector.detect(frame)
        chrome_scene = raw_state.classify(title)
        norm_state = raw_state.normalize(chrome_scene)

        # Apply L2 OCR fallback (same logic as agent.perceive)
        final_scene = chrome_scene
        ocr_note = ""
        if chrome_scene == "unknown" and title:
            if title.lower() in KNOWN_BUILDING_TITLES:
                final_scene = "building_interior"
                ocr_note = "  ← L2 OCR fallback (chrome had no match)"

        print(f"OCR title   : {title!r}")
        print(f"Chrome scene: {chrome_scene!r}")
        print(f"Final scene : {final_scene!r}{ocr_note}")
        print(f"Raw scores  : { {k: f'{v:.3f}' for k, v in raw_state.scores.items()} }")
        print(f"Raw chrome  : {raw_state.summary()}")
        print(f"Norm chrome : {norm_state.summary()}  (impossible flags cleared)")
        print()
        print("To recapture a template:  python -m vision.chrome_detector capture <name>")
        print("To list templates:        python -m vision.chrome_detector list")
