# memory/screen_discovery.py
# Unknown screen handler — fires when the screen classifier returns None.
#
# Flow:
#   1. Capture screenshot + run full-frame OCR dump
#   2. Pause the bot and ask the user to label the screen
#   3. Save a structured discovery record to memory/discoveries/<id>.json
#   4. Save the screenshot to vision/assets/screens/<id>.png
#      (so the classifier can match it next time, once a handler is implemented)
#   5. Return — bot idles until next dev session wires up the handler
#
# Every file in memory/discoveries/ is a pending implementation task for
# the next Claude Code dev session.

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image
from loguru import logger

from config.settings import ASSETS_DIR

DISCOVERIES_DIR = Path("memory/discoveries")
SCREENS_ASSETS_DIR = Path(ASSETS_DIR) / "screens"
BUILDING_SCREENSHOTS_DIR = Path("memory/discoveries/screenshots")


def _slugify(text: str) -> str:
    """Convert user label to a safe filename stem, e.g. 'Auction House' → 'auction_house'."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", "_", text)
    return text


def _ocr_dump(frame: Image.Image) -> list[str]:
    """Run OCR across the full frame and return all detected text strings."""
    try:
        from vision.ocr import _get_reader
        results = _get_reader().readtext(np.array(frame), detail=0)
        return [r.strip() for r in results if r.strip()]
    except Exception as exc:
        logger.warning(f"OCR dump failed: {exc}")
        return []


def save_building_discovery(
    port: str,
    building: str,
    frame: Image.Image,
) -> dict:
    """
    Autonomously record a building interior visit — no user input required.

    Saves:
      - A screenshot to memory/discoveries/screenshots/<port>_<building>_<ts>.png
      - A JSON record to memory/discoveries/<port>_<building>.json
        (updated in place on repeat visits — visit_count increments each time)

    Returns the discovery record dict.
    """
    DISCOVERIES_DIR.mkdir(parents=True, exist_ok=True)
    BUILDING_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    port_slug = _slugify(port) if port else "unknown_port"
    building_slug = _slugify(building)
    record_path = DISCOVERIES_DIR / f"{port_slug}__{building_slug}.json"
    ts = datetime.now(timezone.utc).isoformat()

    # Save screenshot with timestamp so each visit is preserved
    screenshot_name = f"{port_slug}__{building_slug}__{ts.replace(':', '-')}.png"
    screenshot_path = BUILDING_SCREENSHOTS_DIR / screenshot_name
    frame.save(screenshot_path)

    logger.info(f"Running OCR on {building} interior…")
    ocr_texts = _ocr_dump(frame)
    logger.info(f"OCR found {len(ocr_texts)} text items in {building}")

    # Load existing record if this building was visited before
    if record_path.exists():
        with open(record_path, encoding="utf-8") as f:
            record = json.load(f)
        record["visit_count"] = record.get("visit_count", 1) + 1
        record["last_visited_at"] = ts
        record["screenshots"].append(str(screenshot_path))
        record["ocr_snapshots"].append({"ts": ts, "texts": ocr_texts})
    else:
        record = {
            "port": port,
            "building_name": building,
            "visit_count": 1,
            "first_visited_at": ts,
            "last_visited_at": ts,
            "screenshots": [str(screenshot_path)],
            "ocr_snapshots": [{"ts": ts, "texts": ocr_texts}],
        }

    with open(record_path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)

    logger.info(
        f"Discovery saved: {building} @ {port} "
        f"(visit #{record['visit_count']}, {len(ocr_texts)} OCR items)"
    )
    return record


def handle_unknown_screen(
    frame: Image.Image,
    prior_fsm_state: str,
) -> str | None:
    """
    Pause the bot, ask the user to identify the unknown screen, and save a
    discovery record.

    Returns the screen id (slug) that was saved, or None if the user skipped.
    """
    DISCOVERIES_DIR.mkdir(parents=True, exist_ok=True)
    SCREENS_ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    logger.warning("Unknown screen detected — pausing for user input.")

    # Save a temporary screenshot so the user can look at it
    tmp_path = Path("debug_unknown_screen.png")
    frame.save(tmp_path)
    print("\n" + "=" * 60)
    print("UNKNOWN SCREEN DETECTED")
    print(f"Screenshot saved to: {tmp_path.resolve()}")
    print("=" * 60)

    # Ask the user to label the screen
    print("\nWhat is this screen? Describe it briefly.")
    print("Examples:")
    print("  'Market — buy and sell trade goods'")
    print("  'Harbor — manage fleet and depart to sea'")
    print("  'Auction house — players buy and sell items'")
    print("  'Inn — rest and hire companions'")
    print("\nPress Enter without typing to skip (bot will idle).\n")

    raw = input("Your label: ").strip()
    if not raw:
        logger.info("User skipped unknown screen label — bot will idle.")
        return None

    # Parse label and optional description (split on " — " or " - ")
    parts = re.split(r"\s*[—\-–]\s*", raw, maxsplit=1)
    label = parts[0].strip()
    description = parts[1].strip() if len(parts) > 1 else ""
    screen_id = _slugify(label)

    # OCR dump for context
    print("Running OCR dump on screen (this may take a moment)...")
    ocr_texts = _ocr_dump(frame)

    # Save screenshot to vision/assets/screens/ for future template matching
    screenshot_path = SCREENS_ASSETS_DIR / f"{screen_id}.png"
    frame.save(screenshot_path)

    # Build and save discovery record
    discovery = {
        "id": screen_id,
        "label": label,
        "description": description,
        "user_words": raw,
        "screenshot": str(screenshot_path),
        "ocr_dump": ocr_texts,
        "prior_fsm_state": prior_fsm_state,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "implemented": False,
    }
    discovery_path = DISCOVERIES_DIR / f"{screen_id}.json"
    with open(discovery_path, "w", encoding="utf-8") as f:
        json.dump(discovery, f, indent=2, ensure_ascii=False)

    print(f"\nSaved discovery: {discovery_path}")
    print(f"Saved screenshot: {screenshot_path}")
    print(f"Screen id: '{screen_id}'")
    print("\nBot will idle. Start a new dev session to implement the handler.")
    print("=" * 60 + "\n")

    logger.info(f"Unknown screen labeled as '{screen_id}' and saved to discoveries.")
    return screen_id


def get_visited_buildings(port: str) -> set[str]:
    """
    Return the set of building names already recorded for *port*.
    Used by the exploration loop to skip buildings already in the knowledge base.
    """
    if not DISCOVERIES_DIR.exists():
        return set()
    port_slug = _slugify(port) if port else "unknown_port"
    visited = set()
    for path in DISCOVERIES_DIR.glob(f"{port_slug}__*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                record = json.load(f)
            visited.add(record.get("building_name", "").lower())
        except Exception:
            pass
    return visited


def list_unimplemented() -> list[dict]:
    """Return all discovery records that have not yet been implemented."""
    if not DISCOVERIES_DIR.exists():
        return []
    records = []
    for path in sorted(DISCOVERIES_DIR.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        if not record.get("implemented", False):
            records.append(record)
    return records


def is_implemented(screen_id: str) -> bool:
    """Return True if the given screen has been marked implemented."""
    path = DISCOVERIES_DIR / f"{screen_id}.json"
    if not path.exists():
        return True   # no discovery record = built-in screen, treat as implemented
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("implemented", False)
