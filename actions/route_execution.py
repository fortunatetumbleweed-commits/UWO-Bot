# actions/route_execution.py
# Ticket #10 — execute a saved in-game sailing route (select + Move) and confirm
# the fleet is actually sailing.
#
# Validated live 2026-08-14 (Portobelo→Edinburgh via "Sailing Route 2"); see memory
# project_route_execution_flow_2026-08-14. ALL input goes through the sanctioned
# actions.adb_actions primitives (input swipe) — never raw `adb shell input tap`,
# which trips anti-cheat (feedback_manual_playthrough_triggers_anticheat).
#
# UI flow: port_overworld → globe (world map) → Route tab → select route → Move
#          → loading → sea auto-sail.
#
# Monitoring philosophy (per user 2026-08-15): do NOT poll continuously. Verify ONCE
# at the start that (a) supply covers the longest leg (route) / the whole voyage
# (free-sail) and (b) the ship is REALLY moving (the game sometimes fails to depart
# even after Move) — then just check back when the ETA elapses.

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from actions.adb_actions import tap, swipe, press_back

DEFAULT_LONGEST_LEG_DAYS = 6      # route auto-resupplies at waypoints → size to longest leg
FREE_SAIL_BUFFER_DAYS = 2         # free-sail cushion over ETA (matches task_runner)
# Auto-sail time compression: observed ~2 real-min per game-day (12-day route ≈ 25 min).
SEC_PER_GAME_DAY = 130
MOTION_CHECK_INTERVAL_S = 150     # long enough for ~1 game-day to elapse while moving

_NUM_RE = re.compile(r"(\d+)")


# ── Pure helpers (unit-tested) ────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# The route list is a left-hand column, but WHERE that column starts moves: the game
# re-bakes a camera-cutout offset per screen, and the Village Info panel was observed
# ~110px apart between two sessions (AUDIT.md).  A hard 520px gate is the same trap that
# made the trade-list parser read back empty, so the default is a FRACTION of the frame
# with headroom, and the string-similarity threshold does the real discriminating.
_ROUTE_LIST_FRAC = 0.35


def match_route_row(tokens, name: str, list_x_max: Optional[int] = None,
                    min_ratio: float = 0.6, frame_w: int = 2400):
    """Find the (cx, cy) of the route-list row best matching `name`.

    tokens: (text, conf, cx, cy). Route list is the left column (cx < list_x_max,
    defaulting to `frame_w * 0.35` rather than a fixed pixel count).
    Matches by string similarity AND, when both names carry a trailing number
    (Sailing Route 1 vs 2), requires that number to match exactly. Returns
    (cx, cy) or None."""
    if list_x_max is None:
        list_x_max = int(frame_w * _ROUTE_LIST_FRAC)
    want = _norm(name)
    want_num = _NUM_RE.findall(want)
    best, best_ratio = None, 0.0
    for text, _c, cx, cy in tokens:
        if cx is None or cx > list_x_max:
            continue
        cand = _norm(text)
        if len(cand) < 3:
            continue
        ratio = difflib.SequenceMatcher(None, want, cand).ratio()
        # If the requested route has a number, the candidate must share it —
        # otherwise "Sailing Route 2" would match "Sailing Route 1".
        if want_num:
            cand_num = _NUM_RE.findall(cand)
            if want_num[-1] not in cand_num:
                continue
        if ratio > best_ratio:
            best, best_ratio = (int(cx), int(cy)), ratio
    return best if best_ratio >= min_ratio else None


def find_text_button(tokens, keyword: str, min_ratio: float = 0.7):
    """(cx, cy) of the token whose text contains / closely matches `keyword`
    (case-insensitive). Used for the Route tab and the Move button."""
    kw = keyword.lower()
    best, best_ratio = None, 0.0
    for text, _c, cx, cy in tokens:
        t = _norm(text)
        if not t or cx is None:
            continue
        ratio = 1.0 if kw in t else difflib.SequenceMatcher(None, kw, t).ratio()
        if ratio > best_ratio:
            best, best_ratio = (int(cx), int(cy)), ratio
    return best if best_ratio >= min_ratio else None


def route_supply_ok(supply_days: Optional[int],
                    longest_leg_days: int = DEFAULT_LONGEST_LEG_DAYS) -> Optional[bool]:
    """A ROUTE only needs supply for its LONGEST inter-resupply leg (it auto-
    resupplies at waypoints). None if supply unreadable (caller must not assume)."""
    if supply_days is None:
        return None
    return supply_days >= longest_leg_days


def free_sail_supply_ok(supply_days: Optional[int], eta_days: Optional[int],
                        buffer_days: int = FREE_SAIL_BUFFER_DAYS) -> Optional[bool]:
    """FREE-SAIL needs supply for the whole voyage + buffer. None if unreadable."""
    if supply_days is None or eta_days is None:
        return None
    return supply_days >= eta_days + buffer_days


def is_moving(before: dict, after: dict) -> bool:
    """True if the fleet actually advanced between two read_sea_hud snapshots —
    ETA decreased or day-at-sea increased. Guards the game bug where the fleet
    fails to depart despite a selected destination."""
    b_eta, a_eta = before.get("eta_days"), after.get("eta_days")
    if b_eta is not None and a_eta is not None and a_eta < b_eta:
        return True
    b_day, a_day = before.get("day_at_sea"), after.get("day_at_sea")
    if b_day is not None and a_day is not None and a_day > b_day:
        return True
    return False


def checkback_seconds(eta_days: Optional[int],
                      sec_per_game_day: int = SEC_PER_GAME_DAY) -> int:
    """Real seconds until the route's ETA elapses — when to check back for arrival
    (instead of polling). Falls back to one game-day if ETA is unknown."""
    days = eta_days if eta_days else 1
    return int(days * sec_per_game_day)


# ── Orchestration (safe primitives) ───────────────────────────────────────────

@dataclass
class RouteStart:
    ok: bool
    reason: str
    eta_days: Optional[int] = None
    supply_days: Optional[int] = None
    checkback_s: Optional[int] = None


def _cap():
    from capture.adb_capture import capture_screen
    return capture_screen()


def _tokens(frame):
    return _ocr(frame)


def _ocr(frame):
    from actions.sail_actions import _ocr_frame
    return _ocr_frame(frame, min_conf=0.3)


def _wake_if_locked(frame) -> bool:
    """If the game idle/lock screen ('Slide up to unlock') is showing, swipe up to
    wake. Returns True if a wake was performed."""
    txt = " ".join(t.lower() for t, _c, _x, _y in _ocr(frame))
    if "unlock" in txt or "slide up" in txt:
        logger.info("[route] idle-lock detected — waking screen")
        swipe(1200, 880, 1200, 320, 500)
        time.sleep(3)
        return True
    return False


def open_world_map() -> bool:
    """From port_overworld, open the world map — delegates to the ONE canonical
    sail_actions.open_world_map() (port globe / sea minimap), after handling the idle-lock."""
    if _wake_if_locked(_cap()):
        pass
    from actions.sail_actions import open_world_map as _open_world_map
    return _open_world_map(context="port_overworld")


def select_route_and_move(route_name: str) -> bool:
    """On the world map: Route tab → select `route_name` → Move. Returns True if the
    Move tap was issued (i.e. the route was found and selected)."""
    # Route tab
    tab = find_text_button(_ocr(_cap()), "route")
    if not tab:
        logger.error("[route] Route tab not found")
        return False
    tap(*tab)
    time.sleep(1.5)

    # Select the named route
    row = match_route_row(_ocr(_cap()), route_name)
    if not row:
        logger.error(f"[route] route {route_name!r} not found in list")
        return False
    tap(*row)
    time.sleep(1.5)

    # Move
    move = find_text_button(_ocr(_cap()), "move")
    if not move:
        logger.error("[route] Move button not found after selecting route")
        return False
    tap(*move)
    time.sleep(2)
    return True


def verify_sailing_started(mode: str = "route",
                           longest_leg_days: int = DEFAULT_LONGEST_LEG_DAYS,
                           settle_s: float = 8.0,
                           motion_interval_s: float = MOTION_CHECK_INTERVAL_S) -> RouteStart:
    """After Move, confirm the fleet is at sea, has enough supply, and is REALLY
    moving. One-shot check (no continuous polling) — the caller then sleeps
    `checkback_s` and re-checks for arrival.

    mode: 'route' (supply ≥ longest leg) or 'free_sail' (supply ≥ ETA + buffer)."""
    from actions.sail_actions import read_sea_hud

    time.sleep(settle_s)   # let the loading screens clear into the sea view
    before = read_sea_hud(_cap())
    if before.get("eta_days") is None and before.get("supply_days") is None:
        return RouteStart(False, "not at sea / sailing HUD unreadable after Move")

    supply = before.get("supply_days")
    eta = before.get("eta_days")
    if mode == "route":
        sup_ok = route_supply_ok(supply, longest_leg_days)
    else:
        sup_ok = free_sail_supply_ok(supply, eta)
    if sup_ok is False:
        return RouteStart(False, f"insufficient supply ({supply}d) for {mode}",
                          eta_days=eta, supply_days=supply)

    # Motion check — catch the "selected but not sailing" game bug.
    time.sleep(motion_interval_s)
    after = read_sea_hud(_cap())
    if not is_moving(before, after):
        return RouteStart(False, "ship not moving after Move (game did not depart)",
                          eta_days=after.get("eta_days") or eta, supply_days=supply)

    eta_now = after.get("eta_days") or eta
    return RouteStart(True, "sailing", eta_days=eta_now, supply_days=supply,
                      checkback_s=checkback_seconds(eta_now))


def execute_route(route_name: str,
                  longest_leg_days: int = DEFAULT_LONGEST_LEG_DAYS) -> RouteStart:
    """Full route execution: open map → select route → Move → verify sailing started.
    On success the result carries `eta_days` + `checkback_s` so the caller can sleep
    until the ETA and then check for arrival, rather than polling."""
    if not open_world_map():
        return RouteStart(False, "could not open world map")
    if not select_route_and_move(route_name):
        return RouteStart(False, f"could not select/Move route {route_name!r}")
    result = verify_sailing_started("route", longest_leg_days)
    logger.info(f"[route] execute_route({route_name!r}) -> {result}")
    return result
