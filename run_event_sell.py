# run_event_sell.py — sell a cargo category into a live trade-event bazaar.
#
#   python run_event_sell.py Spices
#
# The plan (user, 2026-08-23): the fleet uses LONDON as its hub, reads the Trade Event
# Schedule, and if a Bazaar for a category it is carrying starts within 12 hours it waits for
# the window and sails out. Every spice-bazaar city is about two days from London, and the
# event row's LOCATION PIN does the navigation — tap it and the fleet sails, then walks to the
# market on arrival. No world-map search, no saved route.
#
# Two things are verified rather than assumed:
#   * after tapping the pin, that the fleet is genuinely MOVING (the speed-0 bug shows a
#     destination as set while the tap did nothing);
#   * before selling, that the tiles are actually at BAZAAR pricing — a normal market sits
#     near 100%, a bazaar runs 150%+.

import os
import sys
import time

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from memory.logger import setup_logging

setup_logging()

from loguru import logger

# No argument = ask the HOLD what it is carrying. Naming a category by hand is what sent the
# fleet to a Spices bazaar on 2026-08-24 while the manifest was never checked against it.
CATEGORY = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
# Every bazaar city is ~2 days from London; at the measured ~90 s/game-day that is ~3 min.
SAIL_ESTIMATE_S = 2 * 90
ARRIVAL_POLL_S = 45
ARRIVAL_TIMEOUT_S = 25 * 60


def _events():
    """Open the Trade Event Schedule (if it is not already up) and read it."""
    from capture.adb_capture import capture_screen
    from vision.omniparser import parse_fast_cached
    from vision.trade_event_reader import read_trade_events
    from actions import ui
    from actions.sail_actions import open_world_map

    frame = capture_screen()
    events = read_trade_events(frame)
    if events:
        return events, frame

    if not open_world_map():
        logger.error("[event_sell] could not open the world map")
        return [], frame
    frame = capture_screen()
    # The button sits in the world map's bottom-left corner.
    if not ui.tap_text(frame, "trade event schedule", "trade event", dwell="dialog",
                       why="world map → Trade Event Schedule"):
        logger.error("[event_sell] no Trade Event Schedule button on the world map")
        return [], frame
    frame = capture_screen()
    return read_trade_events(frame), frame


def _wait_for_arrival(city: str) -> bool:
    """Poll until the fleet is in a port again. True when it lands."""
    from capture.adb_capture import capture_screen
    from actions.sail_actions import where_am_i

    deadline = time.time() + ARRIVAL_TIMEOUT_S
    while time.time() < deadline:
        try:
            here = where_am_i(capture_screen())
            state, port = here.get("location"), here.get("port")
        except Exception as exc:
            logger.debug(f"[event_sell] perceive failed while sailing: {exc}")
            state, port = None, None
        if state in ("port_overworld", "building", "sub_menu"):
            logger.info(f"[event_sell] arrived — state={state!r} port={port!r}")
            return True
        logger.info(f"[event_sell] still under way (state={state!r})…")
        time.sleep(ARRIVAL_POLL_S)
    logger.error(f"[event_sell] never saw an arrival at {city} within "
                 f"{ARRIVAL_TIMEOUT_S // 60} min")
    return False


def _cargo_categories() -> list:
    """The categories in the hold, read off the Market's Sell page.

    The Sell tab lists exactly what the fleet carries, so it is the manifest. Reading it is
    what makes the choice of event follow the CARGO rather than a typed argument — live
    2026-08-24 the bot was told "Spices", sailed to a spice bazaar, and only at the market did
    the question "do we even hold spices?" get asked.
    """
    from capture.adb_capture import capture_screen
    from actions.sail_actions import navigate_to_building
    from actions.buy_materials import _on_sell_tab
    from actions import ui
    from vision.market_reader import read_market_page_omni
    from brain.event_selling import categories_on_hand

    if not navigate_to_building("Market"):
        logger.error("[event_sell] could not reach the Market to read the hold")
        return []
    frame = capture_screen()
    if not _on_sell_tab(frame):
        ui.tap_text(frame, "sell", dwell="panel", why="market → Sell (read the hold)")
        frame = capture_screen()
    return categories_on_hand(read_market_page_omni(frame, tab="sell") or [])


def main() -> int:
    from datetime import datetime, timezone

    from capture.adb_capture import capture_screen
    from actions.sail_actions import navigate_to_building
    from actions.trade_events import sail_to_event
    from brain.event_selling import plan_event_sale, should_sell_now
    from vision.market_reader import read_market_page_omni

    carrying = [CATEGORY] if CATEGORY else _cargo_categories()
    if not carrying:
        logger.error("[event_sell] could not read the cargo — nothing to plan a sale around")
        return 1
    logger.info(f"[event_sell] carrying: {', '.join(carrying)}")

    events, _frame = _events()
    if not events:
        logger.error("[event_sell] no trade events readable")
        return 1

    plan = plan_event_sale(events, carrying, lambda _city: SAIL_ESTIMATE_S)
    if plan is None:
        logger.error(f"[event_sell] nothing sellable for {CATEGORY!r} within the horizon")
        return 1
    ev = plan.event

    wait = plan.sleep_seconds
    if wait > 0:
        logger.info(f"[event_sell] window opens later — sleeping {wait / 60:.0f} min before "
                    f"setting out for {ev.city}")
        time.sleep(wait)

    sailed = sail_to_event(ev)
    if not sailed.get("ok"):
        logger.error(f"[event_sell] could not get under way: {sailed.get('reason')}")
        return 1

    if not _wait_for_arrival(ev.city):
        return 1

    if not navigate_to_building("Market"):
        logger.error(f"[event_sell] arrived at {ev.city} but could not reach the Market")
        return 1

    # PROVE the bazaar before selling: a normal market sits near 100%.
    from actions.buy_materials import _on_sell_tab
    from actions import ui
    frame = capture_screen()
    if not _on_sell_tab(frame):
        ui.tap_text(frame, "sell", dwell="panel", why="market → Sell")
        frame = capture_screen()
    goods = read_market_page_omni(frame, tab="sell", port=ev.city) or []

    sell_it, why = should_sell_now(ev, goods, now=datetime.now(timezone.utc))
    logger.info(f"[event_sell] {why}")
    if not sell_it:
        logger.error("[event_sell] NOT selling — the bazaar is not showing on the tiles")
        return 1

    # Sell ONLY the bazaar's category. The rest of the hold is at ordinary pricing (~96-99%
    # measured at Bremen) and may be barter materials the mission still needs.
    from actions.sell_goods import sell_goods
    only = [g.name for g in goods
            if (getattr(g, "category", "") or "").strip().lower() == ev.goods.strip().lower()]
    logger.info(f"[event_sell] selling only the {ev.goods}: {only}")
    res = sell_goods(ev.city, goal="profit", only=only)
    logger.info(f"[event_sell] sell → ok={res.get('ok')} {res.get('reason', '')}")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
