"""Explore-port task: enumerate buildings + sub-menus at a port.

Generic menu iterator that:
  - parses the current screen with `parse_screen()`
  - picks the left-strip menu column (largest cluster of tappable text
    in the left half, excluding chrome / noise roles)
  - taps each item, captures the resulting frame, presses Back, and
    moves on
  - detects locked items via a neighbouring `locked_indicator` role and
    records them without tapping

Built on the same primitive at three levels:

  1. **Inside a building** — walks the sub-menu list (Harbor's Supply /
     Repair / Recruit Crew strip, Bureau's Invest / Tax / Manage, etc.)
  2. **Top-level main menu** — same code (deferred wiring; see TODO at
     the bottom of this module).
  3. **Port-level orchestrator** — `explore_port()` opens the port map,
     enumerates every building label (marking locked / story-gated rows
     as `locked: true`), then for each non-locked building calls
     `navigate_to_building` → `explore_building` → `exit_to_overworld`.

Output:

  - Frames saved under `data/sessions/explore_<port>_<ts>/frames/...`
    with descriptive filenames so the existing seed-layout batch tool
    can pick them up unchanged.
  - `memory/knowledge/ports/<port>.json` — list of `{name, locked}`
    observed in the port map.
  - `memory/knowledge/buildings/<port>__<bldg>.json` — main-view frame
    path + per-sub-menu records (frame path, post-tap nav_state).

No Claude calls during the run.  Seeding layout `.md` files is a
separate batch pass via `tools/seed_layout_from_frame.py --batch`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from actions.adb_actions import tap, press_back
from capture.adb_capture import capture_screen
from vision.element_postprocess import (
    ROLE_APPELLATION,
    ROLE_BACK_ARROW,
    ROLE_BUILD_INFO,
    ROLE_BUILDING_NAMEPLATE,
    ROLE_BUILDING_TITLE,
    ROLE_CHROME_ICON,
    ROLE_CURRENCY_LABEL,
    ROLE_DATE_TIME,
    ROLE_EVENT_BANNER,
    ROLE_HAMBURGER,
    ROLE_LOCKED_INDICATOR,
    ROLE_MODE_TAB,
    ROLE_NOTIFICATION_DOT,
    ROLE_NPC_BUBBLE,
    ROLE_PHONE_OS,
    ROLE_PLAYER_NAMEPLATE,
    ROLE_PORT_NAME,
    ROLE_PROGRESS_BAR,
    ROLE_RIGHT_PANEL_TAB,
)
from vision.screen_perception import parse_screen


# Roles that are never tappable menu items.  Anything classified into
# these is excluded from the candidate pool before clustering.
_NON_MENU_ROLES = frozenset({
    ROLE_BACK_ARROW, ROLE_HAMBURGER,
    ROLE_BUILDING_TITLE, ROLE_PORT_NAME,
    ROLE_CHROME_ICON, ROLE_CURRENCY_LABEL,
    ROLE_NPC_BUBBLE, ROLE_PHONE_OS, ROLE_BUILD_INFO,
    ROLE_EVENT_BANNER, ROLE_LOCKED_INDICATOR,
    ROLE_DATE_TIME, ROLE_RIGHT_PANEL_TAB,
    ROLE_PROGRESS_BAR, ROLE_NOTIFICATION_DOT,
    ROLE_APPELLATION, ROLE_PLAYER_NAMEPLATE,
    ROLE_BUILDING_NAMEPLATE, ROLE_MODE_TAB,
})


# ── Data types ───────────────────────────────────────────────────────────────

@dataclass
class MenuItem:
    name:                str
    x:                   int
    y:                   int
    locked:              bool = False
    frame_path:          Optional[str] = None
    resulting_nav_state: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _slug(s: str) -> str:
    import re as _re
    out = (s or "").strip().lower()
    out = _re.sub(r"[\s/\\:|,;\.]+", "_", out)
    out = _re.sub(r"_+", "_", out).strip("_")
    return out or "unknown"


def _save_frame(
    session_dir: Path,
    slot: int,
    label: str,
    frame=None,
) -> tuple[Path, "Image.Image"]:
    """Save *frame* (or capture a fresh one) to <session_dir>/frames/."""
    frames_dir = session_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 10) % 10**10
    fname = f"{slot:04d}__{_slug(label)}__{ts}.png"
    path = frames_dir / fname
    if frame is None:
        frame = capture_screen()
    frame.save(path)
    return path, frame


# ── Generic menu iterator ────────────────────────────────────────────────────

def _collect_menu_candidates(inventory) -> list[MenuItem]:
    """Pick the left-strip menu column from a parsed inventory.

    Heuristic: tappable text/button elements with letter-bearing labels
    in the left half (cx ≤ 1200) and above the bottom chrome strip
    (cy ≤ 900), excluding chrome/noise roles.  Cluster by cx (±80px);
    pick the cluster with the most rows — that's the menu column.

    Locked rows are flagged when a `locked_indicator` element is within
    ±50 px y AND ±150 px x of the menu item — i.e. in the same UI
    cluster.  Right-panel "Unavailable" badges (e.g. Inn's empty-Hire
    panel) sit in the same y band as the left-strip Hire row but in a
    different column, so the x-proximity check keeps them from
    incorrectly locking unrelated menu items.

    Locked rows are returned in the result list with `locked=True` so
    the caller can record them without attempting a tap.
    """
    candidates = []
    for t in inventory.tagged:
        if t.role in _NON_MENU_ROLES:
            continue
        if t.omni_type not in ("button", "text"):
            continue
        label = (t.label or "").strip()
        if not label or not (2 <= len(label) <= 30):
            continue
        if not any(c.isalpha() for c in label):
            continue
        if t.cx > 1200:
            continue          # stay in the left strip; right side is detail
        if t.cy > 900:
            continue          # bottom chrome (Language Effect corner button etc.)
        candidates.append(t)

    if not candidates:
        return []

    # Cluster by cx — pick the largest cluster (the menu column).
    candidates.sort(key=lambda t: t.cx)
    clusters: list[list] = []
    for t in candidates:
        for c in clusters:
            mean_cx = sum(x.cx for x in c) / len(c)
            if abs(t.cx - mean_cx) <= 80:
                c.append(t)
                break
        else:
            clusters.append([t])

    clusters.sort(key=lambda c: (-len(c), sum(x.cx for x in c) / len(c)))
    best = clusters[0]

    lock_indicators = [(t.cx, t.cy) for t in inventory.tagged
                       if t.role == ROLE_LOCKED_INDICATOR]

    best.sort(key=lambda t: t.cy)
    items: list[MenuItem] = []
    for t in best:
        # Same column: lock indicator must be within ±150 px x.
        locked = any(
            abs(t.cy - ly) <= 50 and abs(t.cx - lx) <= 150
            for lx, ly in lock_indicators
        )
        items.append(MenuItem(name=t.label.strip(), x=t.cx, y=t.cy,
                              locked=locked))
    return items


# Nav-state depth ordering: Back goes from deeper → shallower.  If the
# observed state is shallower than the expected target, we've overshot
# and pressing Back again will leave the game entirely (the system
# Exit Game? confirmation dialog appears when Back fires from
# port_overworld).  See the 2026-05-15 Amsterdam run: after Cathedral's
# Pray sub-menu the screen ended up at port_overworld and the back-loop
# kept pressing Back, summoning the Exit Game dialog and getting stuck.
_NAV_DEPTH = {
    "sea":            0,
    "sea_cinematic":  0,
    "world_map":      0,
    "port_overworld": 1,
    "port_map":       1,
    "building":       2,
    "sub_menu":       3,
}


def _back_to_state(
    expected_nav: str,
    max_backs: int = 3,
    settle_s: float = 1.5,
) -> bool:
    """Press Back until perceive() reports *expected_nav* (or attempts run out).

    Bails out early if the observed nav state is *shallower* than the
    expected target (e.g. expected 'building' but already at
    'port_overworld').  Back only goes outward — pressing it from a
    shallower-than-target state cannot reach the target and risks
    opening the system Exit Game? dialog.
    """
    from brain.perceive import perceive
    expected_depth = _NAV_DEPTH.get(expected_nav)
    for attempt in range(max_backs):
        press_back()
        time.sleep(settle_s)
        loc = perceive(capture_screen()).to_location_dict()
        current = loc.get("location")
        if current == expected_nav:
            return True
        logger.info(
            f"[explore]      back #{attempt + 1}: now at "
            f"{current!r}; expected {expected_nav!r}"
        )
        # Overshoot guard: if we've gone past the target (shallower
        # than expected), more Back presses cannot help and may open
        # the Exit Game dialog.  Stop now.
        current_depth = _NAV_DEPTH.get(current)
        if (
            expected_depth is not None
            and current_depth is not None
            and current_depth < expected_depth
        ):
            logger.info(
                f"[explore]      overshoot: {current!r}(depth {current_depth}) "
                f"is shallower than {expected_nav!r}(depth {expected_depth}); "
                "stopping back-presses"
            )
            return False
    return False


def _consult_claude_for_new_screen(
    frame,
    parent_building: str,
    tapped_item: str,
    nav_state: str,
) -> None:
    """Ask Claude to identify the UI elements on a newly-encountered sub-
    menu (or unknown post-tap screen).

    Sends the frame thumbnail + the FULL OmniParser DetectedElement list
    to Claude.  Claude returns a structured SceneInventory naming each
    element's purpose (e.g. for Cathedral's Pray sub-menu it labels the
    horizontal tile "Pray Favor of God" as a prayer option, identifies
    "Belief in God" as the buff name, etc.).

    Side effects (handled by ClaudeVision):
      - Caches a SceneInventory at memory/knowledge/scenes/<...>.json
      - Persists a learned fingerprint at
        memory/knowledge/learned_fingerprints/<...>.json so the same
        screen is recognised on next encounter without an API call
        (the registry auto-loads learned fingerprints at startup).

    Skips silently when ANTHROPIC_API_KEY is not set or Claude is
    otherwise unavailable.
    """
    try:
        from vision.claude_vision import get_claude_vision
        from vision.omniparser import parse_fast_cached
        from vision.ocr import read_screen_title
    except Exception as e:
        logger.warning(f"[explore]   Claude consult skipped — import error: {e}")
        return

    cv = get_claude_vision()
    if not cv.available:
        return  # no API key — silently skip

    try:
        elements = parse_fast_cached(frame)
    except Exception as e:
        logger.warning(f"[explore]   OmniParser parse for Claude failed: {e}")
        return

    screen_title = (read_screen_title(frame) or "").strip()
    # Use a contextful scene key so different sub-menus under different
    # buildings cache separately (Harbor's Repair vs Shipyard's Repair).
    scene_type = f"sub_menu_in_{parent_building}"
    cache_key  = screen_title or _slug(tapped_item)

    logger.info(
        f"[explore]        Claude consult: scene={scene_type!r} title={cache_key!r} "
        f"(omniparser={len(elements)} elements)"
    )
    try:
        inv = cv.analyse_scene(
            frame=frame,
            scene_type=scene_type,
            screen_title=cache_key,
            detected_elements=elements,
        )
    except Exception as e:
        logger.warning(f"[explore]   Claude consult failed: {e}")
        return

    if inv is None:
        logger.info(f"[explore]        Claude returned no inventory")
        return

    interactive = [el.label for el in inv.interactive()]
    logger.info(
        f"[explore]        Claude identified {len(inv.elements)} elements "
        f"({len(interactive)} interactive): "
        + ", ".join(interactive[:8])
        + (" …" if len(interactive) > 8 else "")
    )


def iterate_menu_items(
    parent_label: str,
    session_dir: Path,
    expected_nav: str = "building",
    max_items: int = 20,
    consult_claude_on_new: bool = True,
) -> list[MenuItem]:
    """Walk every menu item visible at the current screen.

    Tap → capture → Back → next.  Locked rows are recorded but not
    tapped.  Returns the list of MenuItem records (one per candidate,
    including locked).

    Caller must be at `parent_label`'s main view when this is invoked.
    """
    from brain.perceive import perceive

    logger.info(f"[explore] iterate_menu_items({parent_label!r})")

    parent_frame = capture_screen()
    parent_inv = parse_screen(parent_frame, nav_state=expected_nav)
    items = _collect_menu_candidates(parent_inv)
    if not items:
        logger.warning(f"[explore]   no menu candidates in {parent_label!r}")
        return []

    logger.info(
        f"[explore]   {len(items)} candidates: "
        + ", ".join(f"{it.name}{'(L)' if it.locked else ''}" for it in items)
    )

    results: list[MenuItem] = []
    for i, item in enumerate(items[:max_items]):
        slot = i + 1
        if item.locked:
            logger.info(f"[explore]   {slot:2d}. {item.name!r}  locked — skipping")
            results.append(item)
            continue

        logger.info(
            f"[explore]   {slot:2d}. tap {item.name!r} @ ({item.x},{item.y})"
        )
        tap(item.x, item.y)
        time.sleep(2.5)

        frame_path, frame = _save_frame(
            session_dir, slot,
            f"{parent_label}__{item.name}",
        )
        item.frame_path = str(frame_path)
        loc = perceive(frame).to_location_dict()
        item.resulting_nav_state = loc.get("location")
        logger.info(
            f"[explore]        → {frame_path.name}  "
            f"nav={item.resulting_nav_state!r}"
        )

        if consult_claude_on_new:
            _consult_claude_for_new_screen(
                frame=frame,
                parent_building=parent_label,
                tapped_item=item.name,
                nav_state=item.resulting_nav_state or "unknown",
            )

        if not _back_to_state(expected_nav):
            logger.warning(
                f"[explore]        couldn't return to {parent_label!r} after taps; "
                "aborting menu walk"
            )
            results.append(item)
            break
        results.append(item)

    return results


# ── Per-building exploration ─────────────────────────────────────────────────

def explore_building(
    port: str,
    building_name: str,
    session_dir: Path,
) -> dict:
    """Capture the building's main view frame + walk every sub-menu.

    Returns a dict suitable for writing into
    `memory/knowledge/buildings/<port>__<bldg>.json`.
    """
    logger.info(f"[explore] explore_building({port!r}, {building_name!r})")

    main_path, _ = _save_frame(session_dir, 0,
                               f"{building_name}__main")
    sub_session = session_dir / building_name
    items = iterate_menu_items(building_name, sub_session)

    return {
        "port":           port,
        "building_name":  building_name,
        "main_frame":     str(main_path),
        "sub_menus": [
            {
                "name":      it.name,
                "locked":    it.locked,
                "frame":     it.frame_path,
                "nav_state": it.resulting_nav_state,
            }
            for it in items
        ],
        "explored_at":    datetime.now(timezone.utc).isoformat(),
    }


# ── Port-level orchestrator ──────────────────────────────────────────────────

def _detect_locked_buildings_on_port_map(map_frame) -> list[int]:
    """Return cy positions of locked_indicator elements on the port map.

    Locked buildings on the port map carry a `Locked` / `Unavailable` /
    `Requires …` text overlay near their label.  We collect those y
    positions so the caller can flag the labelled buildings nearest to
    them as locked."""
    inv = parse_screen(map_frame, nav_state="port_map")
    return [t.cy for t in inv.tagged if t.role == ROLE_LOCKED_INDICATOR]


def _enumerate_buildings_via_list(
    max_up_swipes: int = 4,
    max_down_swipes: int = 6,
) -> list[str]:
    """Enumerate this port's buildings by scrolling the right-panel
    building list end-to-end.  Returns canonical building names in the
    order they appear (top-to-bottom of the list).

    Why not the port map?  At large ports (Amsterdam, Lisbon, London
    with Mercator Estate / Franco Estate / Fortune Teller) the port
    map's visible area can be smaller than the full set of buildings
    — buildings sit off-screen and require panning/zooming to see.
    The right-panel list, in contrast, is **always complete**; we
    just scroll through it.

    Algorithm:
      1. Swipe down (= scroll content up) until the list signature
         stops changing — we're at the top.
      2. Read what's visible.
      3. Swipe up (= scroll content down) one window at a time,
         reading after each swipe and merging new entries into the
         collected set, until the signature stops changing — we're
         at the bottom.
      4. Return the union in observed order.

    Canonicalisation: labels are passed through `_canonical_name` so
    OCR variants (`"harbour"`, `"item shop"`, `"venturer association"`)
    map to the same canonical building-type slug, and we de-duplicate
    by canonical name (e.g. `"venturer association"` and `"union"`
    both resolve to `union` and only count once).
    """
    from actions.adb_actions import swipe_fast
    from actions.sail_actions import token_sim
    from brain.states.port_map import _canonical_name
    from capture.adb_capture import capture_screen
    from config.settings import BUILDING_MENU_REGION
    from vision.ocr import read_building_menu

    mx = (BUILDING_MENU_REGION[0] + BUILDING_MENU_REGION[2]) // 2
    my = (BUILDING_MENU_REGION[1] + BUILDING_MENU_REGION[3]) // 2

    # THE SHARED "HAS THIS LIST MOVED?" TEST (actions.ui.lists.signature). This was a private
    # third copy of it — the building list had one, this had one, and the live world-map path
    # had none at all, which is how it came to swipe five times at a list and be stopped for
    # making no progress (2026-09-03). One implementation now.
    from actions.ui.lists import signature as _signature

    collected: list[str] = []
    seen_canonical: set[str] = set()

    def _record(rows):
        for lbl, _cx, _cy in rows:
            canon = _canonical_name(lbl) or lbl.strip().lower()
            if not canon:
                continue
            if canon in seen_canonical:
                continue
            seen_canonical.add(canon)
            collected.append(canon)

    # Step 1: rewind to top of list.
    prev_sig: tuple = ()
    for i in range(1, max_up_swipes + 1):
        logger.info(
            f"[explore_port]   rewinding building list to top "
            f"(up-swipe {i}/{max_up_swipes})"
        )
        swipe_fast(mx, my - 150, mx, my + 150,
                    duration_ms=300, settle_ms=600)
        frame = capture_screen()
        rows = read_building_menu(frame)
        sig = _signature(rows)
        if sig == prev_sig:
            break
        prev_sig = sig

    # Record the top view.
    frame = capture_screen()
    rows_top = read_building_menu(frame)
    _record(rows_top)
    prev_sig = _signature(rows_top)
    logger.info(
        f"[explore_port]   top of list ({len(rows_top)} entries): "
        + ", ".join(lbl for lbl, *_ in rows_top)
    )

    # Step 2: scroll down collecting new entries.
    for i in range(1, max_down_swipes + 1):
        swipe_fast(mx, my + 150, mx, my - 150,
                    duration_ms=300, settle_ms=600)
        frame = capture_screen()
        rows = read_building_menu(frame)
        sig = _signature(rows)
        if sig == prev_sig:
            logger.info(
                f"[explore_port]   signature unchanged after down-swipe "
                f"{i} — reached bottom of list"
            )
            break
        prev_sig = sig
        new_before = len(collected)
        _record(rows)
        logger.info(
            f"[explore_port]   down-swipe {i}: now {len(collected)} unique "
            f"buildings (+{len(collected) - new_before})"
        )

    return collected


def _assert_at_port(port: str, home_port: Optional[str] = None) -> bool:
    """Verify we're at the port_overworld of *port* before navigation.

    Slice 5 (2026-05-17): primary check now uses the SceneModel —
    `scene_kind == "port_overworld"` AND `top_left.title` matches the
    target port (case-insensitive).  This is more reliable than the
    old port-name OCR check because the SceneModel verifies the
    FULL chrome structure (lighthouse icon present, no back-arrow,
    overworld panel detected) — not just that the port name string
    is readable somewhere.  Past bugs the old check missed:

      - 'Amsterdamded' / 'home -' false-positive overworlds when the
        OCR'd port name happened to fuzzy-match a KB port; the
        SceneModel rejects these because the BIG ICON test fails.
      - Main-menu and company-overview screens accepted as overworld;
        SceneModel rejects because scene_family != 'overworld'.

    The legacy OCR check is kept as a fallback for cases where the
    SceneModel returns low confidence — e.g. transitional frames
    where the top_left detector hasn't stabilised yet.  In practice
    the SceneModel is the answer ~95% of the time; legacy path
    catches the remainder.

    Returns True when the current screen confirms as the expected port.
    On miss, calls `recover_to_port_overworld` once and re-checks.
    """
    from vision.scene_model import get_scene_model
    from actions.orientation import ensure_canonical_orientation

    # World-switch gate: the port screen's notch offset is baked on entry from
    # the phone's orientation.  If it isn't the canonical rotation, every
    # hardcoded port/building/market coord is off — refuse rather than mis-tap.
    if not ensure_canonical_orientation(raise_on_mismatch=False):
        logger.error(
            f"[explore_port] _assert_at_port({port!r}): wrong display "
            f"orientation — hardcoded coords would mis-hit; not confirming port."
        )
        return False

    def _scene_says_we_are_at_target() -> bool:
        """Primary check via SceneModel: scene_kind + title both match."""
        sm = get_scene_model()   # captures + parses + classifies
        if sm.is_at_port_overworld(port_name=port):
            logger.debug(
                f"[explore_port] _assert_at_port: SceneModel confirms "
                f"port_overworld at {port!r}  ({sm.summary()})"
            )
            return True
        # Distinguish "wrong port" vs "wrong scene" for clearer logs.
        if sm.scene_kind == "port_overworld":
            actual_title = (
                sm.top_left.title.text if sm.top_left and sm.top_left.title
                else "<none>"
            )
            logger.debug(
                f"[explore_port] _assert_at_port: SceneModel says overworld "
                f"but title is {actual_title!r}, expected {port!r}"
            )
        else:
            logger.debug(
                f"[explore_port] _assert_at_port: SceneModel says scene_kind="
                f"{sm.scene_kind!r}, not port_overworld  ({sm.summary()})"
            )
        return False

    def _legacy_ocr_check() -> bool:
        """Fallback: legacy OCR-based port-name check.  Used when the
        SceneModel returns low confidence.  This is the pre-slice-5
        behaviour kept as a safety net."""
        from capture.adb_capture import capture_screen
        from memory.knowledge_base import KnowledgeBase
        from vision.ocr import read_port_name

        kb_ports = {p.lower() for p in KnowledgeBase().known_ports()}
        kb_ports.add(port.lower())
        read = (read_port_name(capture_screen()) or "").strip().lower()
        if not read:
            return False
        return read == port.lower() or any(
            k in read or read in k for k in kb_ports
        )

    if _scene_says_we_are_at_target():
        return True
    # Fall back to legacy OCR check before declaring failure — covers
    # the case where SceneModel returned low confidence but the OCR
    # check would still succeed.
    if _legacy_ocr_check():
        logger.info(
            f"[explore_port] _assert_at_port: legacy OCR check confirmed "
            f"port {port!r} (SceneModel was uncertain)"
        )
        return True

    logger.warning(
        f"[explore_port] not at expected port_overworld for {port!r} — "
        "calling recover_to_port_overworld"
    )
    from brain.recovery import recover_to_port_overworld
    recover_to_port_overworld(home_port=home_port or port, timeout=60.0)
    # After recovery, re-check via the same two-tier path
    return _scene_says_we_are_at_target() or _legacy_ocr_check()


def explore_port(
    port: str,
    session_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> dict:
    """Full port-exploration run.

    Pre-condition: caller is at the port_overworld for *port*.

    Steps:
      1. Scroll the right-panel building list end-to-end and collect
         every distinct building name (de-duped by canonical type).
         This replaced the port-map enumeration on 2026-05-15 — large
         ports' port-map view doesn't fit every building, and the
         right panel always shows them all.
      2. Persist the building list to `memory/knowledge/ports/<port>.json`.
      3. For each building: assert we're at the right port overworld
         (recover if not), navigate via list-then-nameplate-tap, run
         `explore_building`, persist a building record, then return to
         port_overworld for the next one.  Locked / unenterable
         buildings surface as `navigate_to_building` failures and get
         the `tried_but_failed` flag.
    """
    from actions.sail_actions import exit_to_overworld, navigate_to_building

    if session_dir is None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        session_dir = Path("data/sessions") / f"explore_{_slug(port)}_{ts}"
    session_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"[explore_port] port={port!r}  session={session_dir}")

    # Enumerate buildings via the right-panel building list, not the
    # port map.  At large ports the port-map visible area can be smaller
    # than the full set of buildings — the list always shows everything,
    # we just have to scroll through it.  Locked-building detection
    # drops here: we discover unenterable buildings via the existing
    # tried_but_failed flow when navigate_to_building fails.
    canonical_names = _enumerate_buildings_via_list()
    if not canonical_names:
        logger.error(
            "[explore_port] right-panel building list returned no entries; "
            "aborting — possibly not on port_overworld"
        )
        return {"port": port, "buildings": [], "error": "building_list_empty"}

    buildings = [{"name": name, "locked": False} for name in canonical_names]

    logger.info(
        f"[explore_port]   {len(buildings)} buildings via building list: "
        + ", ".join(b["name"] for b in buildings)
    )

    # Merge prior `tried_but_failed` knowledge from the existing port
    # record so we don't re-attempt buildings that failed last time
    # (palace, fortune_teller on London 2026-05-14, etc.).  Future
    # human edits can clear the flag to retry.
    prior_failed = _load_prior_failed_buildings(port)
    for b in buildings:
        if b["name"] in prior_failed and not b["locked"]:
            b["tried_but_failed"] = True

    _update_port_record(port, buildings)

    visited: list[dict] = []
    failed_now: list[str] = []
    for b in buildings:
        if b["locked"]:
            logger.info(f"[explore_port] skip locked {b['name']!r}")
            continue
        if b.get("tried_but_failed"):
            logger.info(
                f"[explore_port] skip {b['name']!r} — tried_but_failed "
                "from a prior run (delete the flag in ports/<port>.json to retry)"
            )
            continue
        if dry_run:
            logger.info(f"[explore_port] dry-run: would explore {b['name']!r}")
            continue
        # Port-overworld guard — bail out of mis-navigation cascades
        # before they multiply (Main Menu / Ship Info / Company
        # Overview screens that the chrome detector accepts as overworld).
        if not _assert_at_port(port):
            logger.error(
                f"[explore_port] cannot establish port_overworld for "
                f"{port!r}; aborting remaining buildings"
            )
            break
        try:
            if not navigate_to_building(b["name"]):
                logger.warning(
                    f"[explore_port] navigate_to_building({b['name']!r}) "
                    "failed; marking tried_but_failed"
                )
                b["tried_but_failed"] = True
                failed_now.append(b["name"])
                continue
            time.sleep(2.0)
            record = explore_building(port, b["name"], session_dir)
            _update_building_record(record)
            visited.append(record)
        except Exception as e:
            logger.error(
                f"[explore_port] error exploring {b['name']!r}: {e}"
            )
            b["tried_but_failed"] = True
            failed_now.append(b["name"])
        finally:
            try:
                exit_to_overworld(timeout=45.0)
            except Exception as e:
                logger.error(f"[explore_port] exit_to_overworld failed: {e}")

    if failed_now:
        # Re-persist with the new failure flags
        _update_port_record(port, buildings)
        logger.info(
            f"[explore_port] persisted tried_but_failed for: "
            + ", ".join(failed_now)
        )

    summary = {
        "port":      port,
        "session":   str(session_dir),
        "buildings": buildings,
        "visited":   visited,
    }
    (session_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False)
    )
    logger.info(
        f"[explore_port] done — visited {len(visited)}/"
        f"{sum(1 for b in buildings if not b['locked'])} non-locked buildings; "
        f"summary → {session_dir/'summary.json'}"
    )
    return summary


# ── KB writers ───────────────────────────────────────────────────────────────

_PORT_KB  = Path("memory/knowledge/ports")
_BLDG_KB  = Path("memory/knowledge/buildings")


def _load_prior_failed_buildings(port: str) -> set[str]:
    """Return the set of building names previously marked
    `tried_but_failed: true` for *port* — but ONLY when the flag was
    stamped by the current (or newer) bot version.

    Flags from older bot versions are treated as expired: a code
    improvement may now unblock work that previously failed, so we
    let the bot retry instead of trusting stale pessimism.  This is
    the version-aware retry policy from brain/version.py.

    Returns an empty set when the record doesn't exist.
    """
    from brain.version import is_older_than_current

    path = _PORT_KB / f"{_slug(port)}.json"
    if not path.exists():
        return set()
    try:
        rec = json.loads(path.read_text())
    except Exception:
        return set()
    out: set[str] = set()
    expired_count = 0
    for b in rec.get("buildings", []) or []:
        if not isinstance(b, dict) or not b.get("tried_but_failed"):
            continue
        name = b.get("name")
        if not name:
            continue
        flag_version = b.get("failed_on_version", "")
        if is_older_than_current(flag_version):
            expired_count += 1
            logger.info(
                f"[explore_port] retry {name!r} — tried_but_failed "
                f"stamp from {flag_version or 'pre-versioning'} is older "
                "than current bot version, treating as expired"
            )
            continue
        out.add(name)
    if expired_count:
        logger.info(
            f"[explore_port] {expired_count} expired tried_but_failed flag(s) "
            f"in {port!r}; will retry on this run"
        )
    return out


def _update_port_record(port: str, buildings: list[dict]) -> None:
    from brain.version import BOT_VERSION

    _PORT_KB.mkdir(parents=True, exist_ok=True)
    path = _PORT_KB / f"{_slug(port)}.json"
    record: dict = {}
    if path.exists():
        try:
            record = json.loads(path.read_text())
        except Exception:
            record = {}
    now = datetime.now(timezone.utc).isoformat()
    record["port"] = port
    record["buildings"] = [
        {
            "name":   b["name"],
            "locked": b["locked"],
            **(
                {"tried_but_failed":   True,
                 "failed_on_version":  BOT_VERSION,
                 "failed_at":          now}
                if b.get("tried_but_failed") else {}
            ),
        }
        for b in buildings
    ]
    record.setdefault("first_seen", now)
    record["last_explored"] = now
    record["last_explored_by_version"] = BOT_VERSION
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    logger.info(f"[explore_port]   wrote {path}")


def _update_building_record(record: dict) -> None:
    _BLDG_KB.mkdir(parents=True, exist_ok=True)
    port = record["port"]
    name = record["building_name"]
    path = _BLDG_KB / f"{_slug(port)}__{_slug(name)}.json"
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            existing = {}
    now = datetime.now(timezone.utc).isoformat()
    existing.update({
        "port":             port,
        "building_name":    name,
        "building_type":    name,
        "sub_menus":        record["sub_menus"],
        "main_frame":       record["main_frame"],
        "last_explored_at": now,
        "last_visited_at":  now,
    })
    existing.setdefault("first_visited_at", now)
    existing["visit_count"] = existing.get("visit_count", 0) + 1
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    logger.info(f"[explore_port]   wrote {path}")


# TODO: top-level main menu (hamburger) uses the same iterate_menu_items
# primitive — wire as `explore_main_menu()` when ready.
