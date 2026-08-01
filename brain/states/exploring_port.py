# brain/states/exploring_port.py
# Port exploration — navigates between buildings using the right-side menu,
# the same way a human player would.
#
# Behaviour model:
#   - Scan the building menu via OCR each step to see what is currently visible
#     and where — coordinates are never hardcoded
#   - If the bottom-most detected item is near the screen edge, the bot notices
#     and scrolls up to check whether more items are hidden below
#   - Pick a building from the visible list (not the one just visited)
#   - Tap its detected position; occasionally mis-tap like a real player
#   - Pause between decisions — player is reading the screen, not a machine
#
# Run standalone:
#   python -m brain.states.exploring_port

from __future__ import annotations

import random
import threading
import time

from loguru import logger
from PIL import Image

from actions.adb_actions import swipe, tap
from capture.adb_capture import capture_screen
from memory.screen_discovery import save_building_discovery, get_visited_buildings
from vision.ocr import read_building_menu, read_port_name, read_screen_title
from classifier.predict import ScreenClassifier as _ScreenClassifier

_classifier = _ScreenClassifier()


def _classify(frame) -> str | None:
    pred = _classifier.predict(frame)
    return pred.screen_type if pred.confidence >= 0.70 else None
from config.settings import SCREEN_HEIGHT


# ── timing constants ────────────────────────────────────────────────────────

# How often to poll the screen while waiting for the character to enter a building.
ENTRY_POLL_INTERVAL: float = 1.5   # seconds between screen captures

# Maximum time to wait for a building entry before giving up.
# The castle can be far away — 45 s covers a slow run across a large port.
ENTRY_TIMEOUT: float = 45.0

# Brief pause after first tapping — gives the character time to start moving
# before we begin polling (avoids false "still on overworld" on the first check).
ENTRY_POLL_DELAY: float = 2.5

# Mean per-pixel difference (0–255) that indicates a full scene change.
# Normal character animation in the overworld stays well below this.
# A building-entry transition (world → interior) easily exceeds it.
SCENE_CHANGE_THRESHOLD: float = 20.0

# Time the player spends "thinking" before deciding where to go next.
THINK_MIN: float = 5.0
THINK_MAX: float = 12.0

# ── input constants ──────────────────────────────────────────────────────────

# Chance of a mis-tap (slightly wrong coords or adjacent item).
MISTAP_CHANCE: float = 0.10

# Vertical spacing between menu items — used only for mis-tap simulation.
MENU_ITEM_HEIGHT: int = 68

# ── scroll constants ─────────────────────────────────────────────────────────

# If the bottom-most detected item is within this many pixels of the screen
# bottom the bot decides to scroll, suspecting more items are hidden below.
SCROLL_TRIGGER_THRESHOLD: int = 200

# X-coordinate of the building menu panel — keeps the swipe within the panel.
MENU_PANEL_X: int = 2090

# How far (pixels) to scroll per swipe. Needs to be large enough that the game
# list doesn't snap back to its original position — 450px (~6 item heights)
# clears the snap-back threshold for most game UI lists.
MENU_SCROLL_DISTANCE: int = 450

# Duration of the scroll swipe in ms. Fast enough to register as a fling
# (avoids snap-back), slow enough not to overshoot the list end.
MENU_SCROLL_MS: int = 200


# ── helpers ──────────────────────────────────────────────────────────────────

def _assert_on_overworld() -> bool:
    """Return True only when the port overworld screen is visible."""
    screen = _classify(capture_screen())
    if screen != "port_overworld":
        logger.warning(
            f"Expected port_overworld but got {screen!r} — "
            "building menu not visible, skipping tap."
        )
        return False
    return True


def _scroll_menu_down() -> None:
    """Swipe downward on the panel — brings items *above* back into view (scroll toward top)."""
    x = MENU_PANEL_X + random.randint(-8, 8)
    y_start = int(SCREEN_HEIGHT * 0.45) + random.randint(-20, 20)
    y_end = y_start + MENU_SCROLL_DISTANCE
    logger.info(f"Scrolling building menu downward ({y_start} → {y_end})")
    swipe(x, y_start, x, y_end, MENU_SCROLL_MS)
    time.sleep(random.uniform(0.6, 1.4))


def _scroll_menu_up() -> None:
    """Swipe upward on the building menu panel to reveal items below.

    Starts from the centre of the list (~60% down the screen) so the finger
    has plenty of travel room before it leaves the scrollable area.
    """
    x = MENU_PANEL_X + random.randint(-8, 8)
    y_start = int(SCREEN_HEIGHT * 0.60) + random.randint(-20, 20)   # ~648 px
    y_end = y_start - MENU_SCROLL_DISTANCE                           # ~198 px
    logger.info(f"Scrolling building menu upward ({y_start} → {y_end})")
    swipe(x, y_start, x, y_end, MENU_SCROLL_MS)
    time.sleep(random.uniform(0.6, 1.4))


def _scroll_to_top() -> None:
    """Scroll the building menu back to the top with a long downward swipe."""
    x = MENU_PANEL_X + random.randint(-8, 8)
    y_start = int(SCREEN_HEIGHT * 0.35) + random.randint(-20, 20)
    y_end = int(SCREEN_HEIGHT * 0.90) + random.randint(-20, 20)
    logger.debug("Resetting building menu to top")
    swipe(x, y_start, x, y_end, MENU_SCROLL_MS + 100)
    time.sleep(random.uniform(0.6, 1.2))


def _get_visible_buildings(frame) -> list[tuple[str, int, int]]:
    """
    Scan the building menu in *frame*.

    If the bottom-most detected item is close to the screen edge, the bot
    behaves like a human who suspects the list continues — it pauses briefly,
    scrolls, re-captures, and re-scans.

    Returns a list of (label, tap_x, tap_y) sorted top-to-bottom.
    """
    visible = read_building_menu(frame)

    if not visible:
        logger.warning("OCR found no building labels in the menu panel")
        return visible

    bottom_y = max(y for _, _, y in visible)
    if bottom_y >= SCREEN_HEIGHT - SCROLL_TRIGGER_THRESHOLD:
        logger.info(
            f"Bottom menu item at y={bottom_y} (screen {SCREEN_HEIGHT}px) — "
            "might be more items below, scrolling to check"
        )
        # Human micro-pause: player reads the list and realises it ends at the edge
        time.sleep(random.uniform(0.8, 2.0))
        _scroll_menu_up()
        # Re-capture after the scroll animation settles
        visible = read_building_menu(capture_screen())

    return visible


def _wait_for_entry_and_record(
    name: str,
    port: str,
    stop_event: threading.Event | None = None,
) -> bool:
    """
    Wait until the character enters a building by watching the screen title.

    The title stays as the port name while the character is running across the
    overworld — including during the port-map close animation, which used to
    cause false-positive scene-change detections.  Only when the character
    actually steps inside does the title change to the building name
    (e.g. "Amsterdam" → "Market").

    Returns True when entry is confirmed, False on timeout or stop signal.
    """
    port_lower = port.lower().strip()
    deadline = time.monotonic() + ENTRY_TIMEOUT

    while time.monotonic() < deadline:
        if stop_event and stop_event.is_set():
            return False

        time.sleep(ENTRY_POLL_INTERVAL)
        frame = capture_screen()
        title = read_screen_title(frame)
        logger.debug(f"Screen title while navigating to '{name}': {title!r}")

        if not title:
            # OCR returned nothing — could be a transition; keep waiting
            continue

        if "world" in title:
            # World map opened unexpectedly — bail out
            logger.warning(f"World map opened while navigating to '{name}' — aborting")
            return False

        # Title still shows the port name → character is on the overworld, still running
        if port_lower in title or title in port_lower:
            continue

        # Title is now something other than the port name → inside a building
        elapsed = ENTRY_TIMEOUT - (deadline - time.monotonic())
        logger.info(
            f"Entered building after ~{elapsed:.0f}s — "
            f"screen title: {title!r} (navigating to '{name}')"
        )
        _record_interior(name, frame, port)
        return True

    logger.warning(
        f"Timed out waiting to enter '{name}' after {ENTRY_TIMEOUT:.0f}s "
        f"— still showing port overworld"
    )
    return False


def _tap_building(name: str, coords: tuple[int, int], port: str = "") -> bool:
    """
    Tap a building menu item (building list) and wait until the character enters.
    Returns True when entry is confirmed, False on timeout.
    """
    x, y = coords

    if random.random() < MISTAP_CHANCE:
        offset_x = random.randint(-15, 15)
        offset_y = random.choice([
            random.randint(-8, 8),
            random.choice([-MENU_ITEM_HEIGHT, MENU_ITEM_HEIGHT]),
        ])
        x += offset_x
        y += offset_y
        logger.debug(f"Mis-tap on '{name}' — offset ({offset_x:+d}, {offset_y:+d})")

    logger.info(f"Tapping building list: '{name}' at ({x}, {y})")
    tap(x, y)

    # Brief pause so the character starts moving before we take the reference frame
    time.sleep(ENTRY_POLL_DELAY)
    return _wait_for_entry_and_record(name, port)


def _record_interior(building: str, frame, port: str) -> None:
    """Capture the current screen and save a discovery record for this building.

    Waits long enough for the building interior — and any first-visit training
    mode overlay — to fully render before taking the screenshot.
    """
    # First-visit training mode takes a few seconds to appear; wait for it
    time.sleep(4.0)
    interior_frame = capture_screen()
    save_building_discovery(port=port, building=building, frame=interior_frame)


# ── exit building ────────────────────────────────────────────────────────────

def exit_building() -> bool:
    """
    Leave the current building and return to the port overworld.

    Presses the Android back button and polls until the building menu
    reappears — which signals we are back on the overworld.

    If the first back press doesn't take effect (swallowed by a dialog,
    animation still playing, etc.) a second press is issued at the halfway
    point so the player doesn't have to retry manually.
    """
    from actions.adb_actions import press_back

    logger.info("Exiting building — pressing back")
    press_back()
    time.sleep(1.5)  # let transition animation start

    deadline = time.monotonic() + ENTRY_TIMEOUT
    retry_at = time.monotonic() + ENTRY_TIMEOUT / 2
    retried = False

    while time.monotonic() < deadline:
        frame = capture_screen()
        if read_building_menu(frame):
            logger.info("Back on port overworld")
            return True

        # Halfway through the timeout, try pressing back again in case the
        # first press was eaten by a dialog or didn't register
        if not retried and time.monotonic() >= retry_at:
            logger.info("Still inside building — pressing back again")
            press_back()
            retried = True

        logger.debug("Waiting to return to overworld…")
        time.sleep(ENTRY_POLL_INTERVAL)

    logger.warning(f"Timed out waiting to return to overworld after {ENTRY_TIMEOUT:.0f}s")
    return False


# ── targeted navigation ──────────────────────────────────────────────────────

# Seconds to wait after tapping a port-map building label before the map
# fully closes and the character starts moving.
PORT_MAP_CLOSE_WAIT: float = 3.5


def go_to_building(
    target: str,
    max_scrolls: int = 8,
    port: str = "",
    stop_event: threading.Event | None = None,
) -> bool:
    """
    Navigate to *target* building.

    Primary:  port map (clean grayscale overlay — reliable OCR).
    Fallback: building list scrolling (used only if the port map fails).

    The port map approach:
      1. Open the port map via the globe icon on the mini map.
      2. OCR all building labels on the map.
      3. Fuzzy-match *target* against the labels.
      4. Tap the matched label — the map closes and the character runs there.
      5. Wait for the scene change that signals the character entered the building.
    """
    from brain.states.port_map import (
        find_building_on_map, close_port_map,
        get_tap_candidates, record_tap_success,
        open_port_map, read_port_map_buildings,
    )

    # ── primary: port map ─────────────────────────────────────────────────────
    result = find_building_on_map(target)
    if result:
        name, tap_x, tap_y = result

        # find_building_on_map returns the already-adjusted tap position (icon
        # offset + scatter).  Try up to _PORT_MAP_TAP_RETRIES positions before
        # giving up on the port map approach.
        _PORT_MAP_TAP_RETRIES = 3

        # Build candidate list: primary tap + fallback probes at different offsets.
        # We need label_y to generate the fallback candidates — re-read it from
        # the find_building_on_map internals is awkward, so just use tap_y as
        # label_y proxy (offset already applied).  The candidates are relative
        # anyway and will still scatter around the icon area.
        from brain.states.port_map import get_tap_candidates
        candidates = [(tap_x, tap_y)] + [
            (fx, fy) for fx, fy in get_tap_candidates(name, tap_x, tap_y)
            if (fx, fy) != (tap_x, tap_y)
        ]

        for attempt, (cx, cy) in enumerate(candidates[:_PORT_MAP_TAP_RETRIES]):
            logger.info(
                f"Port map: tapping '{name}' at ({cx},{cy}) "
                f"(attempt {attempt+1}/{_PORT_MAP_TAP_RETRIES})"
            )
            tap(cx, cy)
            time.sleep(PORT_MAP_CLOSE_WAIT)
            entered = _wait_for_entry_and_record(name, port, stop_event)
            if entered:
                record_tap_success(name, tap_y, cy)
                return True
            logger.warning(
                f"Port map tap did not trigger entry (attempt {attempt+1}) "
                f"— retrying with different offset"
            )
            # Re-open port map for next attempt (tap may have closed it)
            if attempt + 1 < min(_PORT_MAP_TAP_RETRIES, len(candidates)):
                open_port_map()
                import time as _time
                _time.sleep(0.8)

    # ── fallback: building list scroll ────────────────────────────────────────
    logger.info(f"'{target}' not on port map — falling back to building list")

    target_lower = target.lower().strip()

    def _find_match(visible: list[tuple[str, int, int]]) -> tuple[str, int, int] | None:
        for name, x, y in visible:
            if target_lower in name or name in target_lower:
                return name, x, y
        return None

    _scroll_to_top()
    prev_labels: frozenset[str] = frozenset()

    for attempt in range(max_scrolls + 1):
        frame = capture_screen()
        visible = read_building_menu(frame)
        current_labels = frozenset(n for n, *_ in visible)
        logger.debug(f"Fallback scan {attempt + 1}: {sorted(current_labels)}")

        if not visible:
            logger.warning("Menu scan returned nothing — is the building menu open?")
            return False

        match = _find_match(visible)
        if match:
            name, x, y = match
            logger.info(f"Fallback: tapping '{name}' at ({x}, {y})")
            return _tap_building(name, (x, y), port=port)

        if current_labels == prev_labels:
            logger.warning(
                f"Bottom of building list reached — '{target}' not found"
            )
            return False

        prev_labels = current_labels
        logger.info(
            f"'{target}' not visible yet (scan {attempt + 1}/{max_scrolls + 1}), "
            "scrolling to see more"
        )
        time.sleep(random.uniform(0.5, 1.2))
        _scroll_menu_up()

    logger.warning(f"Could not find '{target}' in building menu after {max_scrolls} scrolls")
    return False


# ── full-port discovery sweep ────────────────────────────────────────────────

DEFAULT_EXPLORE_TIMEOUT_MINUTES: float = 15.0


def _interruptible_sleep(
    seconds: float,
    stop_event: threading.Event,
) -> bool:
    """
    Sleep for *seconds* in short increments, checking *stop_event* each tick.
    Returns True if the stop was requested before the sleep finished.
    """
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if stop_event.is_set():
            return True
        time.sleep(min(1.0, end - time.monotonic()))
    return False


def _get_all_buildings_from_port_map(port: str) -> list[str]:
    """
    Get the complete building list for *port* via the port map.

    Noise strategy — two layers:

    L1 (Knowledge Base): if we have visited this port before, we already know
    which labels are buildings.  OCR results that match a previously confirmed
    building name pass through immediately.  Anything not in the KB is
    tentative and gets logged for future L3 review.

    L2 (known building types): a bootstrap set of building-type names seen
    across many ports.  Tentative labels that match a known type are accepted.
    Those that don't are logged as likely NPC noise and skipped for now.

    TODO: feed unrecognised labels to L3 (Claude Vision API) once that module
    is implemented, so novel building types are discovered and added to the KB.
    """
    from brain.states.port_map import open_port_map, read_port_map_buildings, close_port_map
    from capture.adb_capture import capture_screen as _cap
    from memory.knowledge_base import KnowledgeBase

    # ── known building types across all ports (bootstrap whitelist) ───────────
    KNOWN_TYPES: set[str] = {
        "harbor", "harbour", "shipyard", "market", "castle", "inn", "bank",
        "cathedral", "church", "fortune teller", "item shop", "shop",
        "union", "bureau", "mercator estate", "estate", "tavern",
        "blacksmith", "guild", "warehouse", "exchange", "office",
    }

    if not open_port_map():
        logger.warning("Could not open port map — building list will be empty")
        return []

    time.sleep(1.0)
    raw = read_port_map_buildings(_cap())
    close_port_map()
    time.sleep(random.uniform(1.0, 1.8))

    ocr_labels = [n for n, *_ in raw]
    logger.debug(f"Port map raw OCR for {port}: {ocr_labels}")

    # ── L1: cross-check against KB ────────────────────────────────────────────
    kb = KnowledgeBase()
    port_record = kb.get_port(port)
    known_buildings: set[str] = set(port_record["buildings"]) if port_record else set()

    confirmed: list[str] = []
    tentative: list[str] = []

    for label in ocr_labels:
        if label in known_buildings:
            confirmed.append(label)
        else:
            tentative.append(label)

    # ── L2: filter tentative labels against known building types ─────────────
    accepted: list[str] = []
    skipped: list[str] = []

    for label in tentative:
        if any(t in label or label in t for t in KNOWN_TYPES):
            accepted.append(label)
        else:
            skipped.append(label)

    if skipped:
        logger.info(
            f"Skipped labels not matching any known building type "
            f"(likely NPC noise — needs L3 review): {skipped}"
        )

    result = confirmed + accepted
    logger.info(f"Port map buildings for {port}: {result}")
    return result


def explore_all_buildings(
    stop_event: threading.Event | None = None,
    timeout_minutes: float = DEFAULT_EXPLORE_TIMEOUT_MINUTES,
) -> None:
    """
    Autonomous full-port discovery sweep via the port map.

    Strategy:
      1. Open the port map once to get the complete, unambiguous building list
         (the port map has clean OCR — no clock, NPC bubbles, or weather noise).
      2. For each unvisited building, use port map navigation to go there.
      3. Record the interior, exit, then move to the next one.

    Stops when any of these occur:
      - All buildings in the port have been visited
      - *stop_event* is set (user typed 'stop')
      - *timeout_minutes* have elapsed (default 15 min)
    """
    if stop_event is None:
        stop_event = threading.Event()

    deadline = time.monotonic() + timeout_minutes * 60
    port = read_port_name(capture_screen()) or "unknown_port"
    logger.info(
        f"Starting port-map discovery sweep of {port} "
        f"(timeout: {timeout_minutes:.0f} min)"
    )

    def _timed_out() -> bool:
        if time.monotonic() >= deadline:
            logger.info(
                f"Session time limit ({timeout_minutes:.0f} min) reached — stopping."
            )
            return True
        return False

    def _stopped() -> bool:
        if stop_event.is_set():
            logger.info("Exploration stopped by user.")
            return True
        return False

    # ── get the complete building list from the port map ──────────────────────
    all_buildings = _get_all_buildings_from_port_map(port)

    if not all_buildings:
        logger.warning("Port map returned no buildings — aborting sweep")
        return

    # Seed from prior knowledge so already-visited buildings are skipped
    known: set[str] = get_visited_buildings(port)
    visited_this_run: list[str] = []
    failed: list[str] = []

    unvisited = [b for b in all_buildings if b not in known]
    logger.info(
        f"{len(unvisited)} unvisited building(s) to explore "
        f"(skipping {len(known)} already known)"
    )

    for building_name in unvisited:
        if _stopped() or _timed_out():
            break

        logger.info(f"Next target: '{building_name}'")

        ok = go_to_building(building_name, port=port, stop_event=stop_event)
        if not ok:
            logger.warning(f"Could not reach '{building_name}' — skipping")
            failed.append(building_name)
            continue

        if _stopped() or _timed_out():
            break

        if not exit_building():
            logger.warning(f"Could not exit '{building_name}' — aborting sweep")
            break

        known.add(building_name)
        visited_this_run.append(building_name)

        if _stopped() or _timed_out():
            break

        # Think pause — interruptible so stop/timeout takes effect quickly
        if _interruptible_sleep(random.uniform(THINK_MIN, THINK_MAX), stop_event):
            logger.info("Exploration stopped during think pause.")
            break

    # ── summary ──────────────────────────────────────────────────────────────
    elapsed = (time.monotonic() - (deadline - timeout_minutes * 60)) / 60
    if visited_this_run:
        logger.info(
            f"Sweep ended after {elapsed:.1f} min — "
            f"newly explored: {visited_this_run}"
        )
    else:
        logger.info(
            f"Sweep ended after {elapsed:.1f} min — "
            f"all buildings in {port} were already known."
        )
    if failed:
        logger.warning(f"Could not enter: {failed}")


# ── main entry point ─────────────────────────────────────────────────────────

def explore(steps: int = 10) -> None:
    """
    Visit *steps* buildings by reading the right-side menu from the live screen.

    Each step:
      1. Capture the current frame.
      2. OCR-scan the building menu to find what is visible and where.
      3. If the list appears to end near the screen bottom, scroll to reveal more.
      4. Pick a building (not the one just visited) and tap its detected position.
    """
    last: str | None = None

    for i in range(steps):
        think = random.uniform(THINK_MIN, THINK_MAX)
        logger.debug(f"Thinking for {think:.1f}s before next action")
        time.sleep(think)

        logger.info(f"Step {i + 1}/{steps} — checking screen")
        if not _assert_on_overworld():
            logger.info("Not on port overworld — skipping this step")
            continue

        frame = capture_screen()
        visible = _get_visible_buildings(frame)

        if not visible:
            logger.warning("No buildings detected — skipping this step")
            continue

        logger.debug(f"Detected buildings: {[n for n, *_ in visible]}")

        # Prefer a building other than the last visited; fall back if only one item
        choices = [(n, x, y) for n, x, y in visible if n != last]
        if not choices:
            choices = visible

        name, x, y = random.choice(choices)
        last = name
        _tap_building(name, (x, y))


if __name__ == "__main__":
    import sys
    from memory.logger import setup_logging
    setup_logging()

    if len(sys.argv) > 1:
        # e.g.  python -m brain.states.exploring_port castle
        target = " ".join(sys.argv[1:])
        logger.info(f"Navigating to building: '{target}'")
        found = go_to_building(target)
        sys.exit(0 if found else 1)
    else:
        logger.info("Starting port exploration via building menu — press Ctrl+C to stop")
        explore(steps=10)
