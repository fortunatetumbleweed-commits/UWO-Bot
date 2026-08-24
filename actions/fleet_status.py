"""Fleet supply/cargo status — open the MAIN MENU, read the fleet panel, close it.

The numbers a barter plan needs before it can size a gather (hold capacity, current
cargo, water + food held) are not on the port overworld.  They ARE on the main menu's
top-left fleet panel (user 2026-08-20: "you can see the food/water amount by opening
the main menu, the info is in the top/left panel").  Water and food burn at the same
rate, so one reading also calibrates per-day consumption
(`brain.supply_planner.per_day_from_reading`).

This module owns ONE concern: get to the main menu, read, come back.  The parsing is
`vision.hud_readers.read_supply` / `read_cargo` (anchor-based, so they work on this
panel as well as the Cargo Hold); the decision of what to DO with a short reading
belongs to the caller (escalate, don't absorb).

⚠️  The main-menu navigation here is NOT yet live-validated — the ☰ position comes from
config.settings.CHROME_HAMBURGER_REGION and the read is verified by re-perceiving
(`where_am_i` → 'main_menu'), so a miss reports a structured failure rather than
tapping blind.
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

from config.settings import CHROME_HAMBURGER_REGION

# Last-resort ☰ centre from the chrome template region.  NOT the primary path: the
# camera-cutout shift moves this icon between sessions (the panel it shares a screen with
# rendered ~110px left on 2026-08-21), and a fixed point then taps bare chrome while
# reporting success.  `actions.ui.find_top_right_icon` detects it on the live frame first.
_HAMBURGER_FALLBACK = ((CHROME_HAMBURGER_REGION[0] + CHROME_HAMBURGER_REGION[2]) // 2,
                       (CHROME_HAMBURGER_REGION[1] + CHROME_HAMBURGER_REGION[3]) // 2)

# States that carry the ☰ (see memory/project_home_button_is_chromed_only_escape).
_HAMBURGER_STATES = ("port_overworld", "sea", "sea_cinematic")


def read_fleet_status(close: bool = True, attempts: int = 3) -> dict:
    """{ok, reason, water, food, cargo_used, cargo_capacity, supply_days}.

    Opens the main menu only when not already there, and leaves the screen as it
    found it when `close`.  Any unreadable field comes back None — the caller must
    NOT treat None as zero (never act blind).

    RETRIES, because the port overworld carries transient overlays: NPC chatter bubbles
    drift across it, and one sitting over the top-right icon strip both hid the ☰ and made
    `where_am_i` report 'building' (live 2026-08-21), failing a read that would have
    succeeded seconds later.  Re-perceiving is the rule for a state we cannot trust —
    failing on the first ambiguous frame is not."""
    last = None
    for attempt in range(max(1, attempts)):
        last = _read_fleet_status_once(close)
        if last.get("ok"):
            return last
        if attempt + 1 < attempts:
            logger.info(f"[fleet_status] attempt {attempt + 1}/{attempts} failed "
                        f"({last.get('reason')}) — re-perceiving")
            from actions import ui
            ui.settle("screen", why="let a transient overlay clear before re-reading")
    return last


def _read_fleet_status_once(close: bool = True) -> dict:
    """One attempt — see `read_fleet_status` for the contract."""
    from actions import ui
    from actions.sail_actions import where_am_i
    from capture.adb_capture import capture_screen
    from vision.hud_readers import read_cargo, read_supply, read_supply_rows
    from vision.omniparser import parse_fast_cached
    from brain.supply_planner import Supply, days_of_supply

    out = {"ok": False, "reason": "", "water": None, "food": None,
           "cargo_used": None, "cargo_capacity": None, "supply_days": None}

    frame = capture_screen()
    loc = where_am_i(frame).get("location")
    opened = False
    if loc != "main_menu":
        if loc not in _HAMBURGER_STATES:
            out["reason"] = f"cannot open the main menu from {loc!r} (no ☰ there)"
            logger.warning(f"[fleet_status] {out['reason']}")
            return out
        icon = ui.find_menu_icon(frame)
        if icon is not None:
            ui.tap_element(icon, dwell="screen", why="open the main menu (detected ☰)")
        else:
            logger.warning("[fleet_status] ☰ not detected — falling back to the "
                           "calibrated point, which the cutout shift can invalidate")
            ui.tap_at(*_HAMBURGER_FALLBACK, dwell="screen", why="open the main menu (☰ fallback)")
        frame = capture_screen()
        loc = where_am_i(frame).get("location")
        if loc != "main_menu":
            out["reason"] = (f"main menu did not open (still {loc!r}) — the ☰ was "
                             f"{'detected' if icon is not None else 'NOT detected'}")
            logger.warning(f"[fleet_status] {out['reason']}")
            return out
        opened = True

    elements = parse_fast_cached(frame)
    # The main menu renders each amount INSIDE its label's tile, so the tile-crop reader
    # is the primary path; `read_supply` (separate numeric elements) is the Cargo Hold
    # shape and only a fallback here.
    supply = read_supply_rows(frame, elements) or read_supply(elements)
    cargo = read_cargo(elements)
    if supply:
        out["water"], out["food"] = supply
    if cargo:
        out["cargo_used"], out["cargo_capacity"] = cargo
    if out["water"] is not None and out["food"] is not None:
        out["supply_days"] = round(days_of_supply(Supply(out["water"], out["food"])), 2)

    if close and opened:
        ui.back(why="close the main menu")

    if out["water"] is None and out["food"] is None and cargo is None:
        out["reason"] = "main menu open but neither supply nor cargo could be read"
        logger.warning(f"[fleet_status] {out['reason']}")
        return out
    out["ok"] = True
    logger.info(f"[fleet_status] water={out['water']} food={out['food']} "
                f"({out['supply_days']}d) cargo={out['cargo_used']}/{out['cargo_capacity']}")
    return out


def verify_supply_for_leg(min_days: float, status: Optional[dict] = None) -> dict:
    """Gate a leg on days-of-supply.  `ok=False` when the fleet is short OR when supply
    could not be read — an unreadable supply is NOT a pass (the fleet-death lesson:
    the 2026-08-20 loss happened because nothing ever checked)."""
    status = status if status is not None else read_fleet_status()
    days = status.get("supply_days")
    if days is None:
        return {"ok": False, "reason": f"supply unreadable ({status.get('reason') or 'no reading'})",
                "supply_days": None, "status": status}
    if days < min_days:
        return {"ok": False, "reason": f"supply {days}d < {min_days}d needed for this leg",
                "supply_days": days, "status": status}
    return {"ok": True, "reason": f"supply {days}d ≥ {min_days}d", "supply_days": days,
            "status": status}
