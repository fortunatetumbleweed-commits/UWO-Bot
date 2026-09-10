"""World-map perception primitives.

Extracted from the 2026-05-12 / 2026-05-13 empirical calibration pass on 7
labelled world-map frames.  See `docs/four_layer_nav_classification.md`
for the architectural principle: OmniParser for whole-screen detection,
EasyOCR for cropped regions only.

This module is the single source of truth for "given a world-map frame,
which catalogue ports are visible and where are they on screen?".  Both
`tools/world_map_calibrate.py` and `actions/world_map_nav.py` consume
it.

Key design decisions, all anchored in the calibration evidence:

  • Map-area crop (50,80)..(1860,950) — excludes top mode tabs, bottom
    controls, right-side City Info panel, bottom-right legend.  Validated
    on Frame 4 where the panel-open view contaminated calibration; the
    crop fixed it.

  • OmniParser first.  YOLO+Florence-2 gives us `text` / `button` /
    `icon` element types with pixel-accurate bboxes — far more robust
    than raw OCR centroids.

  • Merged-label split (Frame 6 case).  When OmniParser fuses several
    nearby labels into one `[button] 'ICape Verde Bathurst Bissau
    Sierra Leone'`, we re-OCR that bbox crop with EasyOCR to recover
    individual port positions.  This is the OmniParser→OCR escalation
    pattern in miniature.

  • Banner filter.  Server-wide event tickers (`'Maca Boom occurred in
    Yeongil |'`) contain port names but ARE NOT located at the port's
    pixel position.  Skipping them prevents 4000-game-unit calibration
    outliers (Frame 6 went from x CV 126% → 4.8% after filtering).

  • Diacritic folding.  EasyOCR consistently misreads `ø → o`, `å → a`,
    `ñ → n` etc.  Folding both sides during matching closes the gap
    (Vardø ↔ "Vardo").

  • Length guard against short-name false positives.  `'port'` could
    match catalogue `'porto'` at sim≈0.89 via token_sim — disallowed for
    candidates of length ≤ 6 unless the OCR text is at least as long.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image
from loguru import logger


# ── Constants ────────────────────────────────────────────────────────────────

# Map-area crop bounds (validated on 7 labelled world-map frames).
# MAP_X_MAX is the FALLBACK right bound: it was tuned for the Port tab where the right ~540px is
# the port-list panel.  On the Explore tab there is NO right panel and the map runs nearly
# full-width — a fixed 1860 crop silently discarded every village label in the right band
# (Melanesian hunt, 2026-08-20: Tongatapu/Yawuru/Apache all matched then dropped → "0 visible" →
# panned straight past the target).  parse_visible_ports now DETECTS the list panel (a chromed UI
# column of stacked rows) and excludes only that; the map area otherwise extends to the frame edge.
MAP_X_MIN = 50
MAP_Y_MIN = 80
MAP_X_MAX = 1860
MAP_Y_MAX = 950


def _detect_list_panel_xmin(elements, frame_w: int) -> Optional[int]:
    """Left edge of the right-side LIST PANEL (port list / search results) if one is open,
    else None.  The panel is chromed UI — a vertical COLUMN of ≥4 stacked text/button rows with
    near-identical x-centres spanning a tall run — structurally unlike scattered map labels
    (user 2026-08-20: exclude detected chrome, not a fixed slice of the map)."""
    right = [el for el in elements
             if el.element_type in ("text", "button")
             and el.cx > 0.72 * frame_w and (el.label or "").strip()]
    if len(right) < 4:
        return None
    xs = sorted(el.cx for el in right)
    med = xs[len(xs) // 2]
    col = [el for el in right if abs(el.cx - med) < 45]
    if len(col) >= 4:
        ys = sorted(el.cy for el in col)
        if ys[-1] - ys[0] > 300:                 # a tall stacked run = a list panel
            return min(el.x1 for el in col) - 12
    return None

# Path to the baked port catalogue (224 ports from voyage.tw).
_PORT_CATALOGUE_PATH = Path("memory/knowledge/world_map/port_coordinates.json")
_VILLAGE_CATALOGUE_PATH = Path("memory/knowledge/world_map/village_coordinates.json")

# OCR-text → catalogue match threshold (token_sim).
_SIM_THRESHOLD = 0.88

# Event-banner phrases that contain port names but are not located at
# that port.  Tickers like "Plague occurred in Lisboa" must be filtered.
_EVENT_BANNER_PHRASES = (
    "occurred", "ongoing", "ended",
    "boom", "festival", "plague", "flood",
    "war ", " war",
    "sponsor", "development", "extravagance",
    "maca",
)

# World-map UI chrome tokens that look like port names but are panel /
# tab labels.  Rejected unconditionally before matching.
_WORLD_MAP_CHROME_TOKENS = frozenset({
    "explore", "port", "route", "trade",
    "world", "map", "world map",
    "filter", "trade event", "trade event schedule",
    "schedule",
    "search", "go to city", "go to", "set sail", "depart",
    "north", "south", "east", "west",
    # Player-location chrome — these appear as labels on the world map
    # but are NOT ports.  Missing entries here got fuzzy-matched against
    # real port names and corrupted calibration on 2026-05-20.
    "my location", "my company location", "company location",
    "target location",
    "undiscovered area", "222 undiscovered area",
})


# ── Public types ─────────────────────────────────────────────────────────────

@dataclass
class VisiblePort:
    """One catalogue port located on the current world-map frame."""
    key:        str          # lowercase catalogue key
    name:       str          # display name from catalogue
    game_x:     int          # catalogue x
    game_y:     int          # catalogue y
    pix_cx:     int          # observed pixel centre x
    pix_cy:     int          # observed pixel centre y
    label_text: str          # raw OCR / OmniParser label text
    via_split:  bool = False # True if recovered via merged-label re-OCR

    @property
    def tap_pos(self) -> Tuple[int, int]:
        return (self.pix_cx, self.pix_cy)


# ── Catalogue loader ─────────────────────────────────────────────────────────

_catalogue_cache: Optional[dict] = None
_village_catalogue_cache: Optional[dict] = None


def load_port_catalogue() -> dict:
    """Return the lowercase-keyed port dict from the baked KB file."""
    global _catalogue_cache
    if _catalogue_cache is None:
        _catalogue_cache = json.loads(_PORT_CATALOGUE_PATH.read_text())["ports"]
    return _catalogue_cache


def load_village_catalogue() -> dict:
    """Return the lowercase-keyed village dict from the baked KB file.

    Same coord system as the port catalogue (game-internal x/y); each
    village entry carries {name, id, x, y, rank, culture_tag, barters}.
    """
    global _village_catalogue_cache
    if _village_catalogue_cache is None:
        _village_catalogue_cache = json.loads(
            _VILLAGE_CATALOGUE_PATH.read_text()
        )["villages"]
    return _village_catalogue_cache


# ── Helpers ──────────────────────────────────────────────────────────────────

def _strip_diacritics(s: str) -> str:
    # This one carried the o-slash and l-stroke that the normal forms leave alone, and it
    # was the only one that did. That knowledge now lives in `fold_name` for everybody.
    from memory.places import fold_name
    return fold_name(s)


def _is_event_banner(text: str) -> bool:
    text_low = text.lower()
    return any(p in text_low for p in _EVENT_BANNER_PHRASES)


def _match_token_to_port(
    text: str, ports: dict, port_aliases: dict,
) -> Optional[str]:
    """Best catalogue key matching *text*, or None.  See module docstring
    for the matching rules (length guards, diacritic folding, alias-table
    lookup, chrome-token rejection)."""
    from utils.fuzzy import token_sim
    text_low = text.lower().strip()
    if len(text_low) < 3:
        return None
    if text_low in _WORLD_MAP_CHROME_TOKENS:
        return None
    text_fold = _strip_diacritics(text_low)

    best_key, best_score = None, 0.0
    for key, info in ports.items():
        names = [info["name"].lower(), key]
        for canon, variants in port_aliases.items():
            if canon == key or key in variants:
                for v in variants:
                    if v not in names:
                        names.append(v)
        for n in names:
            n_fold = _strip_diacritics(n)
            if len(n_fold) >= 4 and n_fold in text_fold \
                    and len(text_fold) <= len(n_fold) + 4:
                score = 1.0
            else:
                sim = token_sim(text_fold, n_fold)
                if sim < _SIM_THRESHOLD:
                    continue
                if len(text_fold) < len(n_fold) * 0.6:
                    continue
                if len(n_fold) <= 6 and len(text_fold) < len(n_fold):
                    continue
                score = sim
            if score > best_score:
                best_score, best_key = score, key
    return best_key


def _ports_named_in_text(
    text: str, ports: dict, port_aliases: dict,
) -> list[str]:
    """List of catalogue keys whose names appear in *text*.  Skips event
    banners (they mention ports without being located there)."""
    if _is_event_banner(text):
        return []
    text_low = _strip_diacritics(text.lower())
    hits: list[str] = []
    for key, info in ports.items():
        names = [info["name"].lower(), key]
        for canon, variants in port_aliases.items():
            if canon == key or key in variants:
                for v in variants:
                    if v not in names:
                        names.append(v)
        for n in names:
            n_fold = _strip_diacritics(n)
            if len(n_fold) >= 4 and n_fold in text_low and key not in hits:
                hits.append(key)
                break
    return hits


def _split_merged_label(
    element, frame: Image.Image,
    ports: dict, port_aliases: dict,
) -> list[Tuple[str, int, int, str]]:
    """When an OmniParser element's label contains ≥2 catalogue port
    names, re-OCR its bbox crop via EasyOCR to recover individual label
    positions.  Returns list of (port_key, abs_cx, abs_cy, sub_text)."""
    if len(_ports_named_in_text(element.label, ports, port_aliases)) < 2:
        return []

    PAD = 12
    x1 = max(0, element.x1 - PAD)
    y1 = max(0, element.y1 - PAD)
    x2 = min(frame.width,  element.x2 + PAD)
    y2 = min(frame.height, element.y2 + PAD)
    crop = frame.crop((x1, y1, x2, y2))

    from actions.sail_actions import _ocr_frame
    sub_tokens = _ocr_frame(crop, min_conf=0.25)
    out: list[Tuple[str, int, int, str]] = []
    for text, conf, sub_cx, sub_cy in sub_tokens:
        key = _match_token_to_port(text, ports, port_aliases)
        if key:
            out.append((key, sub_cx + x1, sub_cy + y1, text))
    return out


# ── Main entry point ─────────────────────────────────────────────────────────

def parse_visible_ports(
    frame: Image.Image,
    ports: Optional[dict] = None,
    port_aliases: Optional[dict] = None,
) -> list[VisiblePort]:
    """Detect catalogue ports visible on the world-map *frame*.

    Pipeline:
      1. OmniParser parse → element list (text / button / icon).
      2. Crop to map area (excludes panels and chrome).
      3. For each text-like element, count catalogue port-name matches:
         - 0 matches → skip.
         - 1 match  → use OmniParser bbox centre.
         - ≥2 matches → re-OCR bbox crop via EasyOCR to split into
           per-port positions (merged-label recovery).
      4. Deduplicate by port key, preferring split positions over the
         merged-element centre.

    Returns a list of VisiblePort.  Empty when OmniParser is unavailable
    or no catalogue port is visible.
    """
    if ports is None:
        ports = load_port_catalogue()
    if port_aliases is None:
        try:
            from actions.sail_actions import _PORT_ALIASES as port_aliases
        except Exception:
            port_aliases = {}

    try:
        from vision.omniparser import get_omniparser, parse_fast_cached
        if not get_omniparser().yolo_available():
            logger.warning("[world_map_parser] OmniParser YOLO unavailable")
            return []
        elements = parse_fast_cached(frame)
    except Exception as e:
        logger.warning(f"[world_map_parser] OmniParser parse failed: {e}")
        return []

    # Right bound: exclude the LIST PANEL if one is open (detected as a chromed column of
    # stacked rows), else the map runs to near the frame edge (Explore tab has no panel —
    # a fixed crop there discarded genuinely-on-map village labels).
    panel_xmin = _detect_list_panel_xmin(elements, frame.width)
    x_max = panel_xmin if panel_xmin is not None else frame.width - 55
    map_elements = [
        el for el in elements
        if MAP_X_MIN <= el.cx <= x_max
        and MAP_Y_MIN <= el.cy <= MAP_Y_MAX
    ]
    text_like = [el for el in map_elements
                 if el.element_type in ("text", "button")]

    best_per_port: dict[str, VisiblePort] = {}
    for el in text_like:
        named = _ports_named_in_text(el.label, ports, port_aliases)
        if not named:
            # Exact-substring match found nothing.  The label may be
            # OCR-corrupted — notably the docked-fleet marker renders over
            # the CURRENT port's label, turning "Port Royal" → "Pont Royal"
            # (r→n), which makes the home port undetectable and sends
            # pan_to_port oscillating around a target it can see but can't
            # read.  Fall back to the fuzzy single-token matcher (same
            # _SIM_THRESHOLD used everywhere else) so a small OCR error
            # still resolves.  Only fires when substring found nothing, so
            # clean labels and merged multi-port labels are unaffected.
            fuzzy_key = _match_token_to_port(el.label, ports, port_aliases)
            if fuzzy_key is not None:
                named = [fuzzy_key]
        if not named:
            continue

        # Dedupe keys aliasing the SAME catalogue entry — village entries are dual-keyed
        # ('melanesian' AND 'melanesian village'), and treating the two hits as TWO ports
        # sent one label into the merged-label SPLIT path, producing bogus tap positions
        # (live 2026-08-20: found the village, tapped the wrong place).
        _seen_ids: set = set()
        named = [k for k in named
                 if id(ports[k]) not in _seen_ids and not _seen_ids.add(id(ports[k]))]

        if len(named) == 1:
            key = named[0]
            vp = VisiblePort(
                key=key, name=ports[key]["name"],
                game_x=ports[key]["x"], game_y=ports[key]["y"],
                pix_cx=el.cx, pix_cy=el.cy,
                label_text=el.label, via_split=False,
            )
            if key not in best_per_port:
                best_per_port[key] = vp
        else:
            for sub_key, sub_cx, sub_cy, sub_text in \
                    _split_merged_label(el, frame, ports, port_aliases):
                if sub_key in best_per_port:
                    continue
                best_per_port[sub_key] = VisiblePort(
                    key=sub_key, name=ports[sub_key]["name"],
                    game_x=ports[sub_key]["x"], game_y=ports[sub_key]["y"],
                    pix_cx=sub_cx, pix_cy=sub_cy,
                    label_text=sub_text, via_split=True,
                )

    return list(best_per_port.values())
