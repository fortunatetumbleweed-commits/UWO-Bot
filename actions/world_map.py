# actions/world_map.py
#
# World Map Intelligence — Milestone 3
#
# Provides:
#   - Region-based directional panning to find any port on the world map
#   - City Info panel reading (goods, facilities, cultural preferences)
#   - Port knowledge base persistence
#
# The world map is a 2D scrollable map.  When opened from sea, the viewport
# centres on the current ship position.  To find distant ports (e.g. Port Royal
# from London, or London from Ceylon) the bot must pan the map directionally.
#
# Design: every known port has an approximate (x, y) position in normalised
# map space [0,1]×[0,1] where (0,0) is the top-left corner of the full world
# map.  When searching for a port, the bot computes the direction from the
# current viewport centre to the target's expected position and issues a series
# of calibrated swipes until the port label is found or the search grid is
# exhausted.
#
# Standalone test:
#   python -m actions.world_map <port_name>
#   python -m actions.world_map --read-panel     # OCR City Info panel on screen

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger
from PIL import Image

from utils.fuzzy import fuzzy_contains


# ── Port name/position database ───────────────────────────────────────────────
# {port_name(lowercase): (x, y)} used for landmark localisation (matching OCR'd
# port labels to known ports). Sourced from the CANONICAL 224-port catalogue
# (memory/knowledge/world_map/port_coordinates.json), which holds the GAME's actual
# port names. The old config/port_positions.json (82, normalised coords) was removed
# 2026-08-15: its coord VALUES were never consumed, and ~15 of its unique names were
# stale real-world names (Beijing/Busan/Colombo) that the game never shows — the
# game uses Peking/Ceylon/Kozhikode, which ARE in the catalogue. See
# memory project_duplicate_port_lists. Only the KEYS (names) matter here; the
# catalogue coords are carried but unused by this module.


def _load_port_positions() -> dict[str, Tuple[float, float]]:
    try:
        from vision.world_map_parser import load_port_catalogue
        out: dict[str, Tuple[float, float]] = {}
        for name, rec in load_port_catalogue().items():
            if isinstance(rec, dict) and rec.get("x") is not None:
                out[str(name).lower()] = (float(rec["x"]), float(rec["y"]))
        return out
    except Exception as exc:
        logger.error(f"Failed to load port catalogue: {exc}")
        return {}


PORT_POSITIONS: dict[str, Tuple[float, float]] = _load_port_positions()


def reload_port_positions() -> None:
    """Reload PORT_POSITIONS from the port catalogue."""
    global PORT_POSITIONS
    PORT_POSITIONS = _load_port_positions()
    logger.info(f"Reloaded {len(PORT_POSITIONS)} port positions from catalogue")

# ── Landmark-based self-localization ─────────────────────────────────────────
# When the bot sees port labels on the world map it can cross-reference them
# against PORT_POSITIONS to estimate WHERE on the map the viewport is.
# "I see Alexandria (0.47, 0.29) and Istanbul (0.46, 0.27) → I'm near the
#  eastern Mediterranean; Las Palmas is to the west."


def ports_from_ocr_tokens(tokens: list[tuple[str, float]]) -> list[str]:
    """
    Given a list of (text, confidence) OCR tokens from a world-map scan,
    return the canonical PORT_POSITIONS keys that are recognisably visible.

    Used for landmark-based viewport localisation: the bot builds a rough
    estimate of its current map position from the ports it can actually see,
    rather than relying solely on pre-calibrated coordinates.
    """
    recognized: list[str] = []
    for text, conf in tokens:
        if conf < 0.50:
            continue
        t = text.lower().strip()
        if len(t) < 3:
            continue
        # Direct match
        if t in PORT_POSITIONS:
            recognized.append(t)
            continue
        # Substring match with 60% coverage guard (avoids short noise words)
        for port in PORT_POSITIONS:
            if (t in port or port in t) and len(t) >= len(port) * 0.6:
                recognized.append(port)
                break
    return list(dict.fromkeys(recognized))  # deduplicated, order preserved


# Screen dimensions (used by other functions in this module).
_SCREEN_W = 2400
_SCREEN_H = 1080


@dataclass
class CityInfoPanel:
    """Contents of the City Info panel shown when a city is selected on the world map."""
    port:         str                    = ""
    country:      str                    = ""
    culture:      str                    = ""
    goods:        list[str]              = field(default_factory=list)
    facilities:   list[str]              = field(default_factory=list)
    tax_info:     str                    = ""
    raw_text:     list[str]              = field(default_factory=list)


# ── City Info panel reading ────────────────────────────────────────────────────

def read_city_info_panel(frame: Image.Image) -> Optional[CityInfoPanel]:
    """
    OCR the City Info panel that appears on the right side of the world map
    when a city is selected.

    The panel occupies roughly x > 1550 on the 2400-wide screen.
    It contains:
      - Title: "City Info" (top)
      - City name and country
      - List of goods available for purchase
      - List of facilities / buildings
      - Cultural preferences (e.g. "Islamic culture: no alcohol")
      - Tax rate

    Returns a CityInfoPanel dataclass, or None if no panel is detected.
    """
    from actions.sail_actions import _ocr_frame

    # City Info panel is in the right ~35% of the screen
    panel_x = int(_SCREEN_W * 0.62)
    panel_crop = frame.crop((panel_x, 0, _SCREEN_W, _SCREEN_H))

    tokens = _ocr_frame(panel_crop, min_conf=0.28)

    # Check the panel is actually visible: look for "City Info" title
    full_text = " ".join(t.lower() for t, _, _, _ in tokens)
    from brain.kb import control as _ckb
    if not any(fuzzy_contains(full_text, kw) for kw in _ckb().city_info_title_keywords()):
        logger.debug("City Info panel not detected")
        return None

    raw_lines = [t for t, _, _, _ in tokens]
    logger.info(f"City Info panel raw text: {raw_lines}")

    panel = CityInfoPanel()
    panel.raw_text = raw_lines

    # Parse port name: the first non-"City Info" line is usually the city name
    from brain.kb import control as _ckb
    skip_labels = _ckb().city_info_skip_labels()
    for text, _, _, _ in tokens:
        clean = text.strip()
        if clean.lower() in skip_labels or len(clean) < 3:
            continue
        # First substantial line is the city name
        if not panel.port:
            panel.port = clean
            continue
        # Cultural/country info often appears in parentheses or after a dash
        t_lower = text.lower()
        if any(w in t_lower for w in _ckb().culture_keywords()):
            panel.culture = clean
            continue
        if any(w in t_lower for w in _ckb().tax_keywords()):
            panel.tax_info = clean
            continue
        # Facilities list (buildings available at this city)
        if any(w in t_lower for w in _ckb().facility_keywords()):
            panel.facilities.append(clean)
            continue
        # Everything else that's reasonably long is likely a good or category
        if len(clean) >= 4 and not clean.replace(" ", "").isnumeric():
            panel.goods.append(clean)

    logger.info(
        f"City Info panel parsed: port={panel.port!r} culture={panel.culture!r} "
        f"goods={panel.goods[:5]} facilities={panel.facilities[:5]}"
    )
    return panel


# ── City Info: Trade sub-tabs (Preference / Cargo) ─────────────────────────────
# The City Info panel's Trade tab has sub-tabs: Market / Cargo / Preference.
#   • Preference — per-CATEGORY seasonal markup (Textile +50%, Food +30%, …).
#     ALWAYS readable regardless of trade-level range → drives sell-port choice.
#   • Cargo — the selected good's sell price + index%, shown ONLY when the port is
#     within trade-level visibility range (absent = out of range).
# Readers are stateless: the caller opens the sub-tab, the reader parses whatever
# is shown and returns None if its tab isn't the one visible.

import re as _re
from dataclasses import dataclass as _dataclass

# Tab / header words that are never a preference category or a good name.
_CITYINFO_CHROME = {
    "city", "info", "base", "trade", "facility", "invest", "market", "cargo",
    "preference", "free", "port", "safe", "waters", "dangerous", "storage",
}
_PCT_RE = _re.compile(r"([+-]?\d+)\s*%")
# Preference markups are SIGNED (+50%, +30%); requiring the sign avoids mistaking
# the Cargo sub-tab's unsigned index% (e.g. '101%') for a category preference.
_PREF_PCT_RE = _re.compile(r"([+-]\d+)\s*%")
_PRICE_RE = _re.compile(r"^\d[\d,]*$")


def _cityinfo_tokens(frame: "Image.Image"):
    """OCR the right-side City Info panel → (text, conf, cx, cy) in panel-local px."""
    from actions.sail_actions import _ocr_frame
    panel_x = int(_SCREEN_W * 0.62)
    return _ocr_frame(frame.crop((panel_x, 0, _SCREEN_W, _SCREEN_H)), min_conf=0.25)


def _parse_preferences(tokens, row_tol: int = 28) -> dict[str, int]:
    """Pair each '+N%' token with the category label on its row (to its left).

    Pure over (text, conf, cx, cy) tokens.  Junk tokens right of the % column or
    matching City Info chrome are excluded."""
    pcts, labels = [], []
    for text, _c, cx, cy in tokens:
        m = _PREF_PCT_RE.search(text or "")
        if m:
            pcts.append((cx, cy, int(m.group(1))))
            continue
        clean = (text or "").strip()
        alpha = "".join(ch for ch in clean if ch.isalpha())
        if len(alpha) >= 3 and clean.lower() not in _CITYINFO_CHROME:
            labels.append((cx, cy, clean))
    prefs: dict[str, int] = {}
    for pcx, pcy, pct in pcts:
        row = [(lcx, lt) for lcx, lcy, lt in labels
               if abs(lcy - pcy) <= row_tol and lcx < pcx]
        if not row:
            continue
        _, category = max(row, key=lambda z: z[0])   # nearest label left of the %
        prefs[category] = pct
    return prefs


def read_port_preferences(frame: "Image.Image", port: str = "", season: str = ""):
    """Read the City Info → Trade → Preference sub-tab into a PortPreference.

    Returns None if no category/percent rows are visible (wrong sub-tab or no
    panel).  `port` should be supplied by the caller (it selected the city)."""
    from memory.barter_kb import PortPreference
    prefs = _parse_preferences(_cityinfo_tokens(frame))
    if not prefs:
        logger.debug("Preference sub-tab not detected (no category %% rows)")
        return None
    logger.info(f"Port preferences [{port or '?'}]: {prefs}")
    return PortPreference(port=port, season=season, preferences=prefs)


@_dataclass
class PortGoodPrice:
    """The City Info → Cargo sub-tab reading for one good.  price/index_pct are
    None when the port is OUT of trade-level range (no price shown)."""
    good: str
    price: Optional[int] = None
    index_pct: Optional[int] = None
    in_range: bool = False


def _parse_good_price(tokens, row_tol: int = 28) -> Optional[PortGoodPrice]:
    """From the Cargo sub-tab: the good label + its price/index on the same row.

    A price shows only in range; its absence (good listed, no bare number) means
    out of range → in_range=False.  Returns None if no good row is found."""
    # Candidate good labels: alpha tokens in the content area, left column.
    labels = [(cx, cy, (t or "").strip()) for t, _c, cx, cy in tokens
              if cy > 380 and cx < 560
              and len("".join(ch for ch in (t or "") if ch.isalpha())) >= 3
              and (t or "").strip().lower() not in _CITYINFO_CHROME]
    if not labels:
        return None
    lcx, lcy, good = min(labels, key=lambda z: z[1])   # topmost good row
    price, index_pct = None, None
    for text, _c, cx, cy in tokens:
        if abs(cy - lcy) > row_tol or cx <= lcx:
            continue
        clean = (text or "").strip()
        m = _PCT_RE.search(clean)
        if m:
            index_pct = int(m.group(1))
        elif _PRICE_RE.match(clean.replace(" ", "")):
            price = int(clean.replace(",", "").replace(" ", ""))
    return PortGoodPrice(good=good, price=price, index_pct=index_pct,
                         in_range=price is not None)


def read_port_good_price(frame: "Image.Image") -> Optional[PortGoodPrice]:
    """Read the City Info → Trade → Cargo sub-tab: selected good's sell price +
    whether the port is within trade-level visibility range."""
    result = _parse_good_price(_cityinfo_tokens(frame))
    if result is None:
        logger.debug("Cargo sub-tab good row not detected")
    else:
        logger.info(f"Cargo price: {result.good} price={result.price} "
                    f"in_range={result.in_range}")
    return result


# ── Zoom support ───────────────────────────────────────────────────────────────

# Pinch gestures are centred on the map area (right portion of screen), not the
# port-list panel on the left, so they zoom the map rather than interact with the list.
_MAP_SCROLL_X = 1550   # centre of the map area (right ~65% of 2400px screen)
_MAP_SCROLL_Y = 540    # vertical centre


def zoom_world_map_to_max(pinches: int = 3) -> None:
    """
    Zoom the world map out to maximum using pinch-in gestures (sendevent).

    Each pinch moves two fingers from start_radius apart down to end_radius apart
    over the map area centre, which the game interprets as a zoom-out gesture.
    Extra pinches when already at max zoom are silently ignored.

    If the map zooms IN instead of out, swap start_radius / end_radius in the
    pinch_zoom call below (the gesture direction is inverted in that case).
    """
    from actions.adb_actions import pinch_zoom

    logger.info(f"Zooming world map to max ({pinches} pinch-in gestures)…")
    for _ in range(pinches):
        pinch_zoom(
            center_x=_MAP_SCROLL_X,
            center_y=_MAP_SCROLL_Y,
            start_radius=480,   # fingers start wide apart
            end_radius=80,      # fingers end close together → zoom out
        )
        time.sleep(0.4)
    time.sleep(0.5)
    logger.info("Zoom-out complete")


# ── Port KB persistence ────────────────────────────────────────────────────────

def _port_slug(port_name: str) -> str:
    """Normalise a port name to a filesystem-safe slug."""
    import re
    return re.sub(r"[^a-z0-9]+", "_", port_name.lower().strip()).strip("_")


def save_city_info_to_kb(port_name: str, info: CityInfoPanel) -> None:
    """
    Persist a CityInfoPanel to the port knowledge base.

    Writes to:
      memory/knowledge/ports/<slug>__city_info.json
    """
    import json
    from datetime import datetime, timezone

    slug = _port_slug(port_name)
    kb_dir = Path(__file__).parent.parent / "memory" / "knowledge" / "ports"
    kb_dir.mkdir(parents=True, exist_ok=True)
    out_path = kb_dir / f"{slug}__city_info.json"

    record = {
        "port":       port_name,
        "culture":    info.culture,
        "goods":      info.goods,
        "facilities": info.facilities,
        "tax_info":   info.tax_info,
        "raw_text":   info.raw_text,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Merge with existing record if any
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
            # Merge goods / facilities (union, deduplicated)
            existing_goods = set(existing.get("goods", []))
            existing_fac   = set(existing.get("facilities", []))
            record["goods"]      = sorted(existing_goods | set(info.goods))
            record["facilities"] = sorted(existing_fac   | set(info.facilities))
        except Exception:
            pass  # overwrite on parse error

    out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    logger.info(f"Saved city info for {port_name!r} → {out_path}")


def load_city_info_from_kb(port_name: str) -> Optional[dict]:
    """Load a port's city info from the KB, or None if not stored."""
    import json

    slug = _port_slug(port_name)
    path = Path(__file__).parent.parent / "memory" / "knowledge" / "ports" / f"{slug}__city_info.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def list_known_ports() -> list[str]:
    """Return port names that have any KB record (city info or trade info)."""
    kb_dir = Path(__file__).parent.parent / "memory" / "knowledge" / "ports"
    if not kb_dir.exists():
        return []
    slugs: set[str] = set()
    for p in kb_dir.glob("*.json"):
        # strip known suffixes to get the raw slug
        slug = p.stem
        for suffix in ("__city_info", "__trade_info"):
            if slug.endswith(suffix):
                slug = slug[: -len(suffix)]
                break
        slugs.add(slug)
    return sorted(slug.replace("_", " ").title() for slug in slugs)


# ── Explore world map: visit all visible cities and read City Info ─────────────

# Right-edge x where the City Info panel begins.  Candidates inside this
# region are not port labels on the map — they're City Info panel contents
# (the previously-selected city's tabs and text).  Skip them.
_CITY_INFO_PANEL_X_START: int = 1500

# Maximum sensible length for a port label.  "Santiago de Cuba" is 16 chars
# — anything past 25 is almost certainly NPC dialogue, an article fragment,
# or a player chat message.
_MAX_PORT_LABEL_LEN: int = 25
_MIN_PORT_LABEL_LEN: int = 3


# Common lowercase connectors that legitimately appear inside multi-word
# port names (e.g. 'Santiago de Cuba', 'Las Palmas de Gran Canaria').
_PORT_NAME_CONNECTORS: frozenset[str] = frozenset({
    "de", "del", "des", "la", "le", "el", "los", "las",
    "of", "the", "da", "do", "dos", "y",
})

# Chrome / control labels that appear on the world map view but are NOT
# port candidates.  All title-case strings, so they would otherwise pass
# the proper-noun shape filter.  Lowercased here for case-insensitive
# matching.
#
# This blocklist applies ONLY to the "is this string a port candidate?"
# question.  The same labels remain valid tap targets through their own
# call paths (e.g. tap_world_map_mode_tab(), tap_go_to_city()).
_WORLD_MAP_CONTROL_LABELS: frozenset[str] = frozenset({
    # World-map mode tabs (top centre): default 'Port' shows the port
    # list and is what explore_visible_ports relies on; the other three
    # switch to alternate world views and are not yet wired.
    "port", "explore", "route", "trade",
    # World-map screen title (top-left)
    "world map",
    # Bottom-centre action button (commits a fleet to sail to the
    # currently-selected city).
    "go to city",
    # Bottom-left utility controls.
    "trade event schedule", "my location", "filter",
    "enemy company lv",
})


def _looks_like_port_label(text: str) -> bool:
    """Shape filter for port-name candidates.

    Port names are proper nouns formatted as Title Case, optionally
    multi-word with lowercase connectors ('Santiago de Cuba'), and
    optionally hyphenated within a word ('Saint-Malo').

    Each whitespace-separated word must EITHER:
      - start with a capital letter and contain only lowercase letters
        afterwards (with hyphenated capitals allowed: 'Saint-Malo'),
      - or be a known connector ('de', 'of', 'la', etc.).

    This rejects the May-2 incident strings:
      - 'Tne dig one IS'  → 'IS' is uppercase, not a word
      - 'We hired a'      → 'hired' is lowercase, not a connector
      - 'TaylorFP'        → internal uppercase 'F' inside a word
      - 'easier:', 'Britain!', '00.00-23.59', etc. (digits/punctuation)
    """
    s = text.strip()
    if not (_MIN_PORT_LABEL_LEN <= len(s) <= _MAX_PORT_LABEL_LEN):
        return False
    # No digits in port names.
    if any(c.isdigit() for c in s):
        return False
    # No sentence punctuation.
    if any(c in s for c in (":", ";", "!", "?", ".", ",", "(", ")", "/")):
        return False
    # Allowed character set: letters, spaces, hyphens, apostrophes.
    for c in s:
        if not (c.isalpha() or c in " -'"):
            return False

    words = s.split()
    if not words:
        return False

    for i, word in enumerate(words):
        # Connectors are accepted as-is for non-first words.
        if i > 0 and word.lower() in _PORT_NAME_CONNECTORS:
            continue
        # First character must be uppercase letter.
        if not (word and word[0].isalpha() and word[0].isupper()):
            return False
        # Within the word, after the first character, allow:
        #   - lowercase letters
        #   - hyphen followed by another uppercase ('Saint-Malo')
        #   - apostrophe
        # Reject internal uppercase NOT preceded by a hyphen
        # (kills 'TaylorFP', 'McDonald' is rare and not a port here).
        for j, c in enumerate(word[1:], start=1):
            if c.isupper():
                if word[j - 1] != "-":
                    return False
            # Other allowed chars already filtered above.
    return True


def find_world_map_city_candidates(elements, *, port_kb=None):
    """Filter an OmniParser element list to port-name candidates.

    Strategy:
      1. Direct hit on the port_positions KB (fuzzy substring match) →
         high-confidence candidate, use the canonical name.
      2. Fallback shape filter for unknown ports — accept text that
         looks like a proper-noun port name.

    Both passes skip elements inside the City Info panel region
    (right side of screen) since those are panel contents, not map
    labels.

    Returns a list of (canonical_name, cx, cy) tuples — same shape as
    the previous OCR-based candidate list.

    *elements* is an iterable of vision.omniparser.DetectedElement.
    *port_kb* is an iterable of canonical port names (lowercased); if
    None, PORT_POSITIONS is used.
    """
    if port_kb is None:
        port_kb = list(PORT_POSITIONS.keys())
    port_kb_lower = [p.lower() for p in port_kb]

    candidates: list[Tuple[str, int, int]] = []
    seen: set[str] = set()

    for el in elements:
        # Only text-bearing elements can be port labels; skip pure icons.
        if el.element_type not in ("text", "button"):
            continue
        # Skip elements that overlap the City Info panel region — those
        # are panel contents, not map labels.
        if el.cx >= _CITY_INFO_PANEL_X_START:
            continue

        label = el.label.strip()
        if not label:
            continue

        # World-map control labels masquerade as title-case proper nouns
        # ('Port', 'Explore', 'World Map', 'Go to City').  They live at
        # fixed chrome positions but the shape filter would otherwise
        # accept them.  Reject before either matching pass.
        if label.lower() in _WORLD_MAP_CONTROL_LABELS:
            continue

        # Pass 1: KB allow-list (fuzzy)
        canonical = None
        t_lower = label.lower()
        if t_lower in port_kb_lower:
            canonical = port_kb_lower[port_kb_lower.index(t_lower)]
        else:
            for p in port_kb_lower:
                # Coverage-guarded substring match (same rule as
                # ports_from_ocr_tokens) — avoids matching 'Lon' to
                # 'London' on a noise crop.
                if (t_lower in p or p in t_lower) and len(t_lower) >= len(p) * 0.6:
                    canonical = p
                    break

        # Pass 2: shape filter for unknown ports
        if canonical is None and _looks_like_port_label(label):
            canonical = label

        if canonical and canonical not in seen:
            seen.add(canonical)
            candidates.append((canonical, el.cx, el.cy))

    return candidates


def explore_visible_ports_on_world_map() -> list[str]:
    """
    For every port visible on the current world map view, select it,
    read the City Info panel, and save to KB.

    Refactored (Phase 5d) to use OmniParser for structural detection
    instead of OCR-everything-on-screen.  Three guards prevent the
    May-2 article-text-as-port-name incident:

      1. Screen-state pre-check — perceive() must report 'world_map';
         otherwise an interrupting popup or unexpected state would
         contaminate the candidate set.
      2. Candidate validation — text elements must either fuzzy-match
         a known port in PORT_POSITIONS, or pass the proper-noun shape
         filter.  Article fragments ('We hired a', 'easier:'),
         timestamps ('00.00-23.59'), and NPC chat are all rejected.
      3. Right-panel skip — elements inside the City Info panel region
         (x >= 1500) are not map labels and are excluded.

    Returns a list of port names successfully recorded.
    """
    from capture.adb_capture import capture_screen
    from actions.adb_actions import tap
    from vision.omniparser import get_omniparser, parse_fast_cached
    from brain.perceive import perceive

    recorded: list[str] = []
    frame = capture_screen()

    # Guard 1: must be on world_map.
    pr = perceive(frame)
    if pr.state != "world_map":
        logger.warning(
            f"explore_visible_ports: not on world map (state={pr.state!r}, "
            f"detail={pr.detail!r}) — aborting"
        )
        return []

    # Run OmniParser; fall back gracefully if the model is unavailable.
    parser = get_omniparser()
    if not parser.yolo_available():
        logger.warning(
            "explore_visible_ports: OmniParser unavailable — aborting "
            "(legacy OCR-everything path retired in Phase 5d)"
        )
        return []
    elements = parse_fast_cached(frame)

    candidates = find_world_map_city_candidates(elements)
    logger.info(
        f"World map visible port candidates "
        f"(OmniParser, validated): {candidates}"
    )

    for port_name, cx, cy in candidates:
        logger.info(f"  Tapping {port_name!r} @ ({cx},{cy}) to read City Info")
        tap(cx, cy)
        time.sleep(3.0)  # wait for panel to appear

        frame = capture_screen()
        info = read_city_info_panel(frame)
        if info is None:
            # Tapping this candidate did not bring up a City Info panel.
            # Either the candidate was a false positive, or the tap
            # missed.  Skip rather than save garbage.
            logger.info(
                f"  No City Info panel after tapping {port_name!r} — skipping"
            )
            continue
        if info.port:
            save_city_info_to_kb(info.port, info)
            recorded.append(info.port)
        else:
            save_city_info_to_kb(port_name, info)
            recorded.append(port_name)

    logger.info(f"explore_visible_ports: recorded {len(recorded)} ports: {recorded}")
    return recorded


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from memory.logger import setup_logging
    setup_logging()

    args = sys.argv[1:]

    if "--zoom-out" in args:
        print("Zooming world map to max…")
        zoom_world_map_to_max()
        print("Done. Check that the map is fully zoomed out.")
        sys.exit(0)

    if "--read-panel" in args:
        from capture.adb_capture import capture_screen
        frame = capture_screen()
        info = read_city_info_panel(frame)
        if info:
            print(f"Port:       {info.port}")
            print(f"Culture:    {info.culture}")
            print(f"Goods:      {info.goods}")
            print(f"Facilities: {info.facilities}")
            print(f"Tax:        {info.tax_info}")
        else:
            print("No City Info panel detected on screen")
        sys.exit(0)

    if "--list-ports" in args:
        ports = list_known_ports()
        print("Known ports in KB:")
        for p in ports:
            info = load_city_info_from_kb(p)
            goods = (info or {}).get("goods", [])
            fac   = (info or {}).get("facilities", [])
            print(f"  {p}: {len(goods)} goods, {len(fac)} facilities")
        sys.exit(0)

    if "--explore" in args:
        from capture.adb_capture import capture_screen
        from actions.sail_actions import where_am_i
        loc = where_am_i()
        if loc["location"] not in ("world_map", "building"):
            print(f"Not on world map (got {loc['location']!r}) — open world map first")
            sys.exit(1)
        recorded = explore_visible_ports_on_world_map()
        print(f"Recorded {len(recorded)} ports: {recorded}")
        sys.exit(0)

    if args:
        port_name = " ".join(args)
        info = load_city_info_from_kb(port_name)
        if info:
            print(f"KB entry for {port_name!r}:")
            import json
            print(json.dumps(info, indent=2))
        else:
            print(f"No KB entry found for {port_name!r}")
            print(f"Known ports: {list_known_ports()}")
        sys.exit(0)

    print("Usage:")
    print("  python -m actions.world_map --zoom-out        # Zoom world map to maximum")
    print("  python -m actions.world_map --read-panel      # OCR City Info panel on screen")
    print("  python -m actions.world_map --explore         # Read all visible ports on world map")
    print("  python -m actions.world_map --list-ports      # List ports in KB")
    print("  python -m actions.world_map <port_name>       # Show KB entry for port")
