"""Act on the Trade Event Schedule dialog.

Each event row carries two small buttons at its right edge (user, 2026-08-23):

    [scales]  market / trade info
    [pin]     LOCATION — tap it and the fleet sails there; on arrival the player walks to
              the market on its own

The pin is the whole navigation story for event selling. No world-map port search, no saved
route, no ETA arithmetic: every city hosting a spice bazaar is within about two days of
London, so the trip is short by construction.

Tapping the pin does NOT sail — it opens a **Location Info** dialog (the city with a `Nearby`
badge, a mini-map, and a gold **Move** button), and Move is what commits the voyage.

The one thing that must be checked is the game's **speed-0 bug**: the destination can read as
set while the tap that set it did nothing at all. Movement is the decisive evidence — a
falling ETA or a rising day-at-sea — and a displayed destination is not
(`actions.sail_actions._confirm_making_way`).
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

# The row's buttons sit right of the Location column; the PIN is the rightmost of the pair.
_MIN_ICON_X_FRAC = 0.70
# Voyages this short finish before the day counter can tick (~90 s/game-day, measured).
_SHORT_HOP_MAX_DAYS = 2
_ROW_BAND_PX = 60


def find_event_pin(event, elements, frame_width: int) -> Optional[tuple]:
    """(cx, cy) of the location pin on `event`'s row, or None.

    The row comes from the reader's own `row_cy`, not from an index and not from the city
    text — a scrolled list shifts indices, and the city text is the least reliable cell on
    the dialog. None means "the row's buttons are not on screen" (the clipped bottom row),
    which is a refusal, not a fallback.
    """
    # USE THE ROW POSITION THE READER ALREADY HAS. Re-finding the row by its city TEXT fails
    # on exactly the rows that need care: Edinburgh reads as 'Fdinhuroh', nothing matched, and
    # the search landed on the dialog's title bar at (1899,135) — a tap there closes the
    # dialog instead of sailing anywhere.
    row_cy = getattr(event, "row_cy", None)
    if row_cy is None:
        city = (event.city or "").strip().lower()
        for e in elements:
            label = (getattr(e, "label", "") or "").strip().lower()
            if label and label == city:
                row_cy = e.cy
                break
    if row_cy is None:
        logger.warning(f"[trade_events] no row position for {event.city!r} — refusing to "
                       "guess which row to tap")
        return None

    icons = [e for e in elements
             if abs(e.cy - row_cy) <= _ROW_BAND_PX
             and e.cx >= _MIN_ICON_X_FRAC * frame_width
             and getattr(e, "element_type", "") in ("icon", "button")]
    if not icons:
        logger.warning(f"[trade_events] no row buttons beside {event.city!r}")
        return None

    pin = max(icons, key=lambda e: e.cx)      # scales then pin — the pin is rightmost
    return (pin.cx, pin.cy)


def _tap_move(capture_fn, tap_fn, *, settle_s: float = 2.0) -> bool:
    """Tap **Move** on the Location Info dialog the pin opened. True when it was tapped.

    Move is the gold commit button of that dialog; the colour is what tells it apart from the
    surrounding text, exactly as on every other commit in this game.
    """
    import time as _t
    from brain.commit_actions import has_positive_background
    from vision.omniparser import parse_fast_cached

    _t.sleep(settle_s)                       # let the dialog land
    frame = capture_fn()
    els = list(parse_fast_cached(frame))
    if not any("location info" in (getattr(e, "label", "") or "").strip().lower()
               for e in els):
        logger.warning("[trade_events] no Location Info dialog after tapping the pin")
        return False

    moves = [e for e in els
             if (getattr(e, "label", "") or "").strip().lower() == "move"]
    if not moves:
        logger.warning("[trade_events] Location Info is open but has no Move button")
        return False
    # Prefer the gold one; a plain text match would happily take a label.
    gold = [e for e in moves if has_positive_background(frame, e)]
    btn = (gold or moves)[0]
    logger.info(f"[trade_events] Location Info open — tapping Move @ ({btn.cx},{btn.cy})"
                + ("" if gold else " (no gold background — taking the text match)"))
    tap_fn(btn.cx, btn.cy)
    _t.sleep(settle_s)
    return True


def sail_to_event(event, *, capture_fn=None, tap_fn=None, elements=None,
                  motion_wait_s: float = 45.0) -> dict:
    """Tap the event's location pin and confirm the fleet is genuinely MOVING.

    Returns {ok, reason}. `ok=False` with `moving=False` means the speed-0 bug: the tap was
    swallowed and the fleet is sitting still, which the caller must resolve by setting the
    destination again rather than by waiting.
    """
    if capture_fn is None:
        from capture.adb_capture import capture_screen
        capture_fn = capture_screen
    if tap_fn is None:
        from actions.adb_actions import tap as tap_fn

    frame = capture_fn()
    if elements is None:
        from vision.omniparser import parse_fast_cached
        elements = list(parse_fast_cached(frame))

    pin = find_event_pin(event, elements, getattr(frame, "width", 2400))
    if pin is None:
        return {"ok": False, "moving": False,
                "reason": f"no location pin on the {event.city} row"}

    logger.info(f"[trade_events] sailing to {event.city} for the {event.goods} "
                f"{event.kind.lower()} — tapping the row's location pin @ {pin}")
    tap_fn(*pin)

    # THE PIN ONLY OPENS A DIALOG. It raises **Location Info** — the city with a Nearby badge,
    # a mini-map, and a gold **Move** button — and nothing sails until Move is tapped (user,
    # 2026-08-23). Without this the fleet sat in port while the bot waited for motion that was
    # never going to start, and the bazaar window was missed.
    if not _tap_move(capture_fn, tap_fn):
        return {"ok": False, "moving": False,
                "reason": f"the Location Info dialog for {event.city} had no Move button"}

    from actions.route_execution import is_moving
    from actions.sail_actions import _confirm_making_way
    moving, hud = _confirm_making_way(motion_wait_s, is_moving)

    # A SHORT HOP CANNOT SHOW A FALLING ETA. The movement test wants the ETA to drop or the
    # day-at-sea to rise, and on a 1-day voyage neither can happen — there is no finer
    # granularity to fall through, and the whole trip is over in ~90 s. Live 2026-08-24 the
    # fleet sailed Bremen and ARRIVED while this call was still reporting `eta=None->1` as
    # the speed-0 bug, so the run aborted on a voyage that had actually succeeded.
    # An ETA that APPEARED is the evidence available here: it means the destination took and
    # the fleet is under way. Arrival is what confirms it, and the caller already waits for
    # that — so a missed hop still fails, just at the honest place.
    # A MEASURED 0.0 OUTRANKS THE ALLOWANCE. The short-hop case exists for "cannot tell" —
    # a 1-day ETA that has no room to fall — not for "we looked and the fleet is stopped".
    # Without this guard the allowance would wave through the exact speed-0 bug it sits
    # next to. None still means unknown, and unknown still gets the benefit of the doubt.
    eta = hud.get("eta_days")
    speed = hud.get("speed")
    if speed == 0.0:
        logger.warning(f"[trade_events] {event.city}: speed reads 0.0 kt — the fleet is "
                       "stopped, not on a short hop")
    elif not moving and eta is not None and eta <= _SHORT_HOP_MAX_DAYS:
        logger.info(f"[trade_events] {event.city} is a short hop (eta {eta}d) — an ETA this "
                    "small cannot be seen to fall, so arrival is what confirms it")
        return {"ok": True, "moving": True, "short_hop": True, "hud": hud,
                "eta_days": eta, "reason": f"sailing to {event.city} (short hop)"}

    if not moving:
        # NOT a wait-and-see: the game shows the destination as set while the fleet stands
        # still, and only setting it again clears it.
        logger.warning(f"[trade_events] tapped the pin for {event.city} but the fleet is NOT "
                       "moving — the speed-0 bug; the destination must be set again")
        return {"ok": False, "moving": False, "hud": hud,
                "reason": f"tapped {event.city} but the fleet never got under way"}

    logger.info(f"[trade_events] under way to {event.city} "
                f"(eta {hud.get('eta_days')}d) — on arrival the player walks to the market")
    return {"ok": True, "moving": True, "hud": hud,
            "eta_days": hud.get("eta_days"), "reason": f"sailing to {event.city}"}
