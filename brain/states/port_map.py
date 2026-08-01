# brain/states/port_map.py
# Port map navigation — the primary way the bot finds buildings.
#
# The port map is a clean grayscale overhead view with clearly labeled
# building icons. It is far more reliable than the right-panel building
# list, which contains noisy UI elements (status bar, mini map, clock,
# NPC bubbles) that confuse OCR.
#
# Flow:
#   1. Ensure the mini map is visible (tap location tab if hidden).
#   2. Tap the CENTRE of the mini map → port map opens.
#      (The globe icon in the bottom-right corner of the mini map opens the
#       WORLD map instead — do not tap it here.)
#   3. OCR the full port map to find building labels and their coordinates.
#   4. Tap the target building label.
#   5. Port map closes; character runs to the building.
#
# Screen title reference (top-left of every screen):
#   port overworld / port map  → port name  (e.g. "Amsterdam")
#   world map                  → "World Map"
#   building interior          → building name  (e.g. "Market", "Castle")
#
# Run standalone:
#   python -m brain.states.port_map castle

from __future__ import annotations

import time
import random

import numpy as np
from loguru import logger
from PIL import Image

from actions.adb_actions import tap
from capture.adb_capture import capture_screen
from vision.ocr import _get_reader
from config.settings import (
    MINIMAP_PORT_MAP_COORD,
    TAB_LOCATION_COORD,
    PORT_MAP_BACK_COORD,
    PORT_MAP_OCR_REGION,
)


# Minimum similarity score for a label to match a known building name.
_CANONICAL_MATCH_THRESHOLD = 0.55


def _variant_to_canonical_map() -> dict[str, str]:
    """
    Flat lookup table: variant string → canonical building type id.
    Built from ControlKB.building_name_variants() so adding a new building
    name (or a new spelling/translation) is config-only — no code change.
    """
    from brain.kb import control as _ckb
    mapping: dict[str, str] = {}
    variants_dict = _ckb()._ui.get("building_name_variants", {})
    for canonical, variants in variants_dict.items():
        canonical_lower = canonical.lower()
        for v in variants:
            mapping[v.lower()] = canonical_lower
    return mapping


def _canonical_name(label: str) -> str | None:
    """
    Return the canonical building type id if *label* fuzzy-matches any known
    variant above the similarity threshold, otherwise None.

    Partial OCR reads ('ipyard' for 'shipyard') and spelling variants
    ('harbour' for 'harbor') are both canonicalised to the type id so
    downstream code can compare against a stable name regardless of OCR
    quality or game-side spelling.
    """
    variants = _variant_to_canonical_map()
    best_variant: str | None = None
    best_score: float = 0.0
    for v in variants:
        score = _label_similarity(label, v)
        if score > best_score:
            best_score = score
            best_variant = v
    if best_score >= _CANONICAL_MATCH_THRESHOLD and best_variant:
        canonical = variants[best_variant]
        if canonical != label:
            logger.debug(
                f"Canonicalised {label!r} → {canonical!r} "
                f"(matched variant {best_variant!r}, similarity={best_score:.2f})"
            )
        return canonical
    logger.debug(
        f"Discarding unrecognised label {label!r} "
        f"(best variant {best_variant!r} score={best_score:.2f})"
    )
    return None


# ── detection helpers ─────────────────────────────────────────────────────────



def _read_screen_title(frame: Image.Image) -> str:
    """Delegate to the shared OCR helper."""
    from vision.ocr import read_screen_title
    return read_screen_title(frame)


def _is_port_map_open(frame: Image.Image) -> bool:
    """
    Detect whether the port map overlay is currently on screen.

    The port map has a unique chrome signature:
      - flag present (top-left, same as overworld)
      - back arrow present (top-left, NOT present on overworld)
      - no hamburger (only on overworld)

    Primary check: chrome template matching (flag + back arrow, no hamburger).
    Fallback: grayscale saturation check on the centre region when templates
    are not yet available — the port map is a near-monochrome overlay.
    """
    from vision.chrome_detector import get_chrome_detector
    detector = get_chrome_detector()

    if "world_map_btn" in detector.templates_available:
        chrome = detector.detect(frame)
        return chrome.has_world_map_btn

    # Fallback: saturation heuristic (less reliable but works without templates)
    w, h = frame.size
    cx, cy = w // 2, h // 2
    sample = frame.crop((cx - 200, cy - 150, cx + 200, cy + 150)).convert("HSV")
    arr = np.array(sample)
    mean_saturation = arr[:, :, 1].mean()
    return float(mean_saturation) < 30


def _is_world_map_open(frame: Image.Image) -> bool:
    """Return True when the world map screen is open (title is 'world map')."""
    return "world" in _read_screen_title(frame)


# ── port map lifecycle ────────────────────────────────────────────────────────

def open_port_map() -> bool:
    """
    Open the port map overlay by tapping the mini map centre.

    The mini map panel is toggled on/off by the Location tab.  When hidden,
    MINIMAP_PORT_MAP_COORD lands on the building list, starting character
    navigation to a building instead of opening the port map.

    Strategy — test-and-cancel:
      1. Tap the mini map coordinate.
      2. Check quickly (3s) whether the port map opened.
         • Yes → done.
         • No  → mini map was hidden; the tap started building-list navigation.
                 Press BACK immediately to cancel the walk.
                 Toggle the Location tab to show the mini map.
                 Retry once.

    This avoids the need to track the Location-tab toggle state across restarts.

    Returns True when the port map is confirmed open.
    """
    from actions.adb_actions import press_back as _press_back

    for attempt in range(2):
        logger.info(f"Tapping mini map centre (attempt {attempt + 1})")
        tap(*MINIMAP_PORT_MAP_COORD)
        time.sleep(3.0)   # quick check — port map renders within 2-3s

        frame = capture_screen()
        if _is_port_map_open(frame):
            logger.info("Port map is open")
            return True

        logger.debug(
            f"Port map not detected after attempt {attempt + 1} — "
            "mini map was likely hidden; cancelling any navigation"
        )
        # Cancel any building-list navigation that the tap triggered.
        _press_back()
        time.sleep(random.uniform(1.0, 1.5))

        # Toggle the Location tab to show the mini map.
        logger.info("Toggling Location tab to show mini map")
        tap(*TAB_LOCATION_COORD)
        time.sleep(random.uniform(2.0, 2.5))   # let the panel render

    logger.warning("Could not open port map after 2 attempts")
    return False


def close_port_map() -> None:
    """Tap the back button to close the port map."""
    logger.info("Closing port map")
    tap(*PORT_MAP_BACK_COORD)
    time.sleep(random.uniform(1.0, 1.5))


# ── building label OCR ────────────────────────────────────────────────────────

def _background_brightness(frame: Image.Image, bbox_rel: list, region_origin: tuple[int, int]) -> float:
    """
    Return the mean grayscale brightness (0–255) of the background area
    surrounding a detected text bounding box.

    Port map labels sit on gray map surface (~100–180).
    Overworld building-name panels have a bright white background (>210).
    Speech bubbles vary but tend to be bright as well.

    Used both to filter obvious bleed-throughs and to break ties when two
    similar labels are detected — the one with the lower background brightness
    is on the gray map and is the genuine port map label.
    """
    PAD = 6
    ox, oy = region_origin
    xs = [p[0] for p in bbox_rel]
    ys = [p[1] for p in bbox_rel]
    x1 = max(0, int(min(xs)) - PAD) + ox
    x2 = int(max(xs)) + PAD + ox
    y_top = max(0, int(min(ys)) - PAD) + oy
    y_bot = int(max(ys)) + PAD + oy

    w, h = frame.size
    x1, x2 = max(0, x1), min(w, x2)
    y_top, y_bot = max(0, y_top), min(h, y_bot)

    if x2 <= x1 or y_bot <= y_top:
        return 128.0   # neutral fallback

    region = frame.crop((x1, y_top, x2, y_bot)).convert("L")
    return float(np.array(region, dtype=np.float32).mean())


def read_port_map_buildings(frame: Image.Image) -> list[tuple[str, int, int]]:
    """
    OCR the open port map and return all detected building labels as
    (label, abs_x, abs_y) sorted by y position.

    Post-processing steps:
      1. Filter obvious bleed-throughs by background brightness (>210 = white panel).
      2. Merge horizontally adjacent fragments on the same line so that
         multi-word labels like "Item Shop" or "Fortune Teller" are not split.
      3. Filter UI noise: pure numbers, very short strings, sentence fragments
         (speech bubbles), and "World Map" / "Back" chrome labels.
      4. Validate against KB building_name_variants — discard anything
         unrecognised; complete partial reads to the canonical type id
         (e.g. 'ipyard' → 'shipyard', 'harbour' → 'harbor').  Player arrow
         occlusion can block part of a label; garbled fragments that don't
         match any known variant are safely discarded.
      5. Deduplicate — when two labels map to the same canonical name, keep the
         one with lower background brightness (gray map surface, not bleed-through).
    """
    # Vertical tolerance for considering two fragments on the same line (px).
    LINE_TOLERANCE = 18
    # Maximum horizontal gap between two fragments to merge them (px).
    MERGE_GAP = 50
    # Known UI labels that are not buildings.
    _UI_NOISE = {"world map", "back"}

    ox, oy = PORT_MAP_OCR_REGION[0], PORT_MAP_OCR_REGION[1]
    region_origin = (ox, oy)
    region = frame.crop(PORT_MAP_OCR_REGION)
    raw = _get_reader().readtext(np.array(region), detail=1)

    BRIGHT_THRESHOLD = 210   # background brightness above this → definite panel/bubble

    # ── step 1: extract per-fragment geometry, filtering obvious bleed-throughs ─
    fragments: list[dict] = []
    for bbox, text, conf in raw:
        if conf < 0.35 or len(text.strip()) < 2:
            continue
        brightness = _background_brightness(frame, bbox, region_origin)
        if brightness > BRIGHT_THRESHOLD:
            logger.debug(
                f"Skipping bright bleed-through {text!r}  "
                f"brightness={brightness:.0f}"
            )
            continue
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        fragments.append({
            "text": text.strip(),
            "conf": conf,
            "x1": min(xs), "x2": max(xs),
            "cy": (min(ys) + max(ys)) / 2,
            "brightness": brightness,
        })

    fragments.sort(key=lambda f: f["cy"])

    # ── step 2: group into lines, then merge horizontally adjacent fragments ────
    lines: list[list[dict]] = []
    for f in fragments:
        for line in lines:
            line_cy = sum(lf["cy"] for lf in line) / len(line)
            if abs(f["cy"] - line_cy) <= LINE_TOLERANCE:
                line.append(f)
                break
        else:
            lines.append([f])

    # Each merged entry carries the minimum brightness of its fragments
    # (the darkest reading best represents the map surface).
    merged: list[tuple[str, int, int, float]] = []
    for line in lines:
        line.sort(key=lambda f: f["x1"])
        groups: list[list[dict]] = [[line[0]]]
        for f in line[1:]:
            if f["x1"] - groups[-1][-1]["x2"] <= MERGE_GAP:
                groups[-1].append(f)
            else:
                groups.append([f])
        for group in groups:
            label = " ".join(f["text"] for f in group).lower().strip()
            cx = int((group[0]["x1"] + group[-1]["x2"]) / 2) + ox
            cy = int(sum(f["cy"] for f in group) / len(group)) + oy
            brightness = min(f["brightness"] for f in group)
            merged.append((label, cx, cy, brightness))

    merged.sort(key=lambda e: e[2])   # top-to-bottom

    # ── step 3: filter UI noise, speech bubbles, and deduplicate ────────────
    # Speech bubbles and NPC chat bleed through the overlay as sentence
    # fragments. Building names are short (1-3 words), contain no sentence
    # punctuation, and do not read like natural language.
    #
    # When two similar labels survive (one genuine map label, one garbled
    # bleed-through that slipped past the brightness filter), keep the one
    # with the lower background brightness — that is the genuine gray-map label.
    _SENTENCE_PUNCT = set("!?,.")

    # entries stored as (label, cx, cy, brightness) during dedup, stripped at end
    entries: list[tuple[str, int, int, float]] = []
    for label, cx, cy, brightness in merged:
        if label in _UI_NOISE:
            continue
        if label.replace(" ", "").isnumeric():
            continue
        if len(label) < 3:
            continue
        # Reject sentence fragments: too many words or contains punctuation
        if len(label.split()) > 3:
            logger.debug(f"Skipping speech bubble (too many words): {label!r}")
            continue
        if any(c in label for c in _SENTENCE_PUNCT):
            logger.debug(f"Skipping speech bubble (punctuation): {label!r}")
            continue
        # Validate against known building types — discard unrecognised labels
        # (catches partial bleed-throughs like 'ipyard' and player-blocked
        # fragments like 'ard' that don't map to any known building type).
        # Partial matches are completed to the canonical name (e.g. 'ipyard'
        # → 'shipyard') so downstream code always works with clean names.
        canonical = _canonical_name(label)
        if canonical is None:
            continue
        label = canonical

        # Deduplicate: if a similar label already exists, keep the darker one
        # (lower brightness = gray map surface = genuine port map label)
        dup_idx = next(
            (i for i, (el, *_) in enumerate(entries)
             if el == label or _label_similarity(label, el) > 0.6),
            None,
        )
        if dup_idx is not None:
            existing_brightness = entries[dup_idx][3]
            if brightness < existing_brightness:
                logger.debug(
                    f"Replacing bleed-through {entries[dup_idx][0]!r} "
                    f"(brightness={existing_brightness:.0f}) with "
                    f"{label!r} (brightness={brightness:.0f})"
                )
                entries[dup_idx] = (label, cx, cy, brightness)
            else:
                logger.debug(
                    f"Skipping bleed-through duplicate {label!r} "
                    f"(brightness={brightness:.0f} ≥ existing {existing_brightness:.0f})"
                )
            continue
        entries.append((label, cx, cy, brightness))

    result = [(label, cx, cy) for label, cx, cy, _ in entries]
    logger.debug(f"Port map OCR → {[n for n, *_ in result]}")
    return result


def _label_similarity(a: str, b: str) -> float:
    """
    Simple character-overlap similarity between two strings (0.0–1.0).
    Used to deduplicate garbled OCR reads of the same building label.
    """
    if not a or not b:
        return 0.0
    # Longest common subsequence length as a fraction of the longer string
    la, lb = len(a), len(b)
    # Quick check: one is a substring of the other
    if a in b or b in a:
        return min(la, lb) / max(la, lb)
    # Count matching characters in order (greedy)
    i = j = matches = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            matches += 1
            i += 1
            j += 1
        elif la - i > lb - j:
            i += 1
        else:
            j += 1
    return matches / max(la, lb)


# ── tap-offset learning ───────────────────────────────────────────────────────
# On the port map, each building has a small icon above its text label.
# The icon is the tappable hot-spot; the text label center is often unreliable.
# We learn the best y-offset (pixels above the OCR text centre) per building
# type through experience and store it in a tiny JSON file.

import json
from pathlib import Path as _Path

_TAP_OFFSETS_FILE = _Path("memory/knowledge/port_map_tap_offsets.json")

# Default: start by aiming 40px above the text label (toward the icon).
# Negative = above text (where the icon usually sits).
_DEFAULT_Y_OFFSET = -40

# How many pixels of random scatter to add (human-like imprecision).
_TAP_SCATTER = 15


def _load_tap_offsets() -> dict:
    try:
        return json.loads(_TAP_OFFSETS_FILE.read_text())
    except Exception:
        return {}


def _save_tap_offsets(data: dict) -> None:
    _TAP_OFFSETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TAP_OFFSETS_FILE.write_text(json.dumps(data, indent=2))


def get_tap_position(building_type: str, label_x: int, label_y: int) -> tuple[int, int]:
    """
    Return a human-realistic tap position for a port-map building.

    Uses the learned y-offset for this building type (or the default) to aim
    above the text label where the building icon sits, then adds random scatter
    so the bot never hits exactly the same pixel twice.
    """
    offsets = _load_tap_offsets()
    entry = offsets.get(building_type, {})
    y_offset = entry.get("y_offset", _DEFAULT_Y_OFFSET)

    # Add scatter: human fingers don't land on the same pixel every time
    jitter_x = random.randint(-_TAP_SCATTER, _TAP_SCATTER)
    jitter_y = random.randint(-_TAP_SCATTER // 2, _TAP_SCATTER // 2)

    tap_x = label_x + jitter_x
    tap_y = max(55, label_y + y_offset + jitter_y)   # keep on-screen

    return tap_x, tap_y


def record_tap_success(building_type: str, label_y: int, actual_tap_y: int) -> None:
    """
    Record that a tap at *actual_tap_y* successfully entered *building_type*.

    Updates the running average y-offset so future taps aim toward the spot
    that has historically worked.
    """
    y_offset_used = actual_tap_y - label_y
    offsets = _load_tap_offsets()
    entry = offsets.get(building_type, {"y_offset": _DEFAULT_Y_OFFSET, "successes": 0})

    # Exponential moving average — recent observations weighted more heavily
    n = entry["successes"]
    alpha = max(0.3, 1.0 / (n + 1))   # start fast, slow down as data accumulates
    entry["y_offset"] = round((1 - alpha) * entry["y_offset"] + alpha * y_offset_used)
    entry["successes"] = n + 1

    offsets[building_type] = entry
    _save_tap_offsets(offsets)
    logger.debug(
        f"Tap offset learned: {building_type!r} → y_offset={entry['y_offset']}px "
        f"(n={entry['successes']})"
    )


def get_tap_candidates(building_type: str, label_x: int, label_y: int) -> list[tuple[int, int]]:
    """
    Return a list of tap positions to try in order.

    First position uses the learned offset + scatter.  Subsequent positions
    are fallback probes at different icon heights in case the learned offset
    is still being calibrated.
    """
    primary = get_tap_position(building_type, label_x, label_y)

    # Fallback probes: try a range of y-offsets around the icon area
    probes = []
    for extra_offset in (-20, +20, -50, 0):
        fx = label_x + random.randint(-_TAP_SCATTER, _TAP_SCATTER)
        fy = max(55, label_y + extra_offset + random.randint(-8, 8))
        if (fx, fy) != primary:
            probes.append((fx, fy))

    return [primary] + probes


# ── main navigation function ──────────────────────────────────────────────────

def find_building_on_map(
    target: str,
    fallback_random: bool = False,
    exclude: set[str] | None = None,
) -> tuple[str, int, int] | None:
    """
    Open the port map and find *target* in it.

    Returns (matched_label, tap_x, tap_y) with the map still open so the
    caller can tap the coordinates.  The returned tap position already
    incorporates the learned icon offset and human-realistic scatter — the
    caller should NOT add extra jitter.  Returns None if nothing is found
    or if the port map could not be opened; in either case the map is closed.

    Args:
        target:          Building name to search for (fuzzy match).
        fallback_random: If True and target not found, pick a random building
                         from the visible list rather than returning None.
                         Useful when the bot wants to visit something but
                         doesn't know exactly what buildings this port has.
        exclude:         Set of building names (lowercase) to skip when
                         selecting the fallback.

    The caller is responsible for tapping the returned coordinates and
    waiting for the character to enter the building.
    """
    if not open_port_map():
        return None

    time.sleep(0.8)   # let the map fully render
    frame = capture_screen()
    buildings = read_port_map_buildings(frame)

    logger.debug(f"Port map labels: {[n for n, *_ in buildings]}")

    target_lower = target.lower().strip()
    for name, lx, ly in buildings:
        if target_lower in name or name in target_lower:
            tap_x, tap_y = get_tap_position(name, lx, ly)
            logger.info(
                f"Found '{name}' on port map — label ({lx},{ly}), "
                f"tap ({tap_x},{tap_y})"
            )
            return name, tap_x, tap_y

    logger.warning(
        f"'{target}' not found on port map. "
        f"Visible: {[n for n, *_ in buildings]}"
    )

    if fallback_random and buildings:
        excluded = exclude or set()
        candidates = [(n, x, y) for n, x, y in buildings if n not in excluded]
        if candidates:
            name, lx, ly = random.choice(candidates)
            tap_x, tap_y = get_tap_position(name, lx, ly)
            logger.info(
                f"Fallback: randomly selected '{name}' — label ({lx},{ly}), "
                f"tap ({tap_x},{tap_y})"
            )
            return name, tap_x, tap_y

    close_port_map()
    return None


if __name__ == "__main__":
    import sys
    from pathlib import Path
    from memory.logger import setup_logging
    setup_logging()

    target = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "castle"
    logger.info(f"Testing port map navigation — target: '{target}'")

    # Open the port map and capture a debug screenshot with detected positions marked
    if not open_port_map():
        logger.error("Could not open port map")
        sys.exit(1)

    time.sleep(0.8)
    frame = capture_screen()
    buildings = read_port_map_buildings(frame)

    # Save debug image with detected label positions and proposed tap positions marked
    try:
        from PIL import ImageDraw, ImageFont
        debug = frame.copy()
        draw = ImageDraw.Draw(debug)
        ICON_OFFSET = 70

        for name, lx, ly in buildings:
            tx = lx
            ty = max(55, ly - ICON_OFFSET)
            # Draw label position (blue circle)
            r = 12
            draw.ellipse((lx - r, ly - r, lx + r, ly + r), outline="blue", width=3)
            # Draw proposed tap position (red circle)
            draw.ellipse((tx - r, ty - r, tx + r, ty + r), outline="red", width=3)
            # Draw line between them
            draw.line((lx, ly, tx, ty), fill="yellow", width=2)
            # Label text
            draw.text((lx + 15, ly - 10), name, fill="cyan")
            draw.text((tx + 15, ty - 10), f"tap→{name}", fill="red")

        debug_path = Path("debug_port_map.png")
        debug.save(debug_path)
        logger.info(
            f"Debug image saved to {debug_path.resolve()}\n"
            "  Blue circles  = detected text label positions (OCR)\n"
            "  Red circles   = proposed tap positions (icon, 40px above label)\n"
            "  Compare these against what you see on the port map to verify accuracy."
        )
    except Exception as e:
        logger.warning(f"Could not save debug image: {e}")

    logger.info(f"Detected buildings: {[(n, x, y) for n, x, y in buildings]}")

    target_lower = target.lower()
    result = next(
        ((n, x, y) for n, x, y in buildings
         if target_lower in n or n in target_lower),
        None,
    )
    if result:
        name, x, y = result
        logger.info(f"Found '{name}' — label ({x},{y}), icon tap ({x},{max(55,y-ICON_OFFSET)})")
    else:
        logger.warning(f"'{target}' not found. Visible: {[n for n,*_ in buildings]}")

    close_port_map()
