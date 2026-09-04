"""A chromed screen we have no support for: two actions, unknown destination.

This is a STATE, in the same sense that `market` is a state — not an error, and not a
recovery. The only thing that distinguishes them is the repertoire:

    market                        buy, sell, exit
    unrecognized_chromed_screen   back (the title bar), home

Every chromed screen in UWO affords those two, which is why this activity needs to know
nothing about WHICH screen it is on. It performs one of them and FINISHES, exactly as the
market finishes when its buying is done. The dispatcher re-perceives afterwards and routes
from what is actually there.

That is the whole design, and it is what `recover_to_port_overworld` got wrong. That
function owns a 20-attempt internal loop and treats `port_overworld` as the only success,
so it FORCES a destination and can outlive the world it was launched in. Live 2026-08-27:
the daily news knocked the bot to the main menu just after it had committed a departure;
recovery closed the menu correctly, perception then read `sea` at confidence 1.00 — the
fleet was sailing to Tripoli exactly as intended — and recovery, still insisting on
`port_overworld`, declared "Failed to reach port_overworld after 3 attempts" and killed the
run. Had the timeout not fired first, its nested `_recover_from_sea` would have tried to
sail the fleet back to `home_port`, undoing the voyage.

A single-step activity cannot fail that way. It has no destination to insist on and no loop
to outlive a world change (memory: no-subloops-task-drives-state).

BOTH EXITS ARE CONFIRMED BEFORE USE, never assumed. The Home icon is LOCATED by template
match rather than taken from a constant: on the Placement Setting screen it matched at
(2219,44) while `MENU_TILE_COORDS["home"]` says (2350,40) — 131px away, and a tap there
would have hit nothing.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from brain.dispatcher import ActivityResult, BLOCKED, FINISHED

STATE = "unrecognized_chromed_screen"


class UnrecognizedChromedActivity:
    """Leave, by whichever of the two exits this screen actually shows. Then finish."""

    name = STATE
    SERVES = (STATE,)

    # A chromed screen nobody can name. Its whole repertoire is back and home — an activity
    # that FINISHES, not a recovery that forces a destination. We cannot claim it affords a
    # globe or a building list, because we do not know what it is.
    CAN_START = ()

    # Clears a SCREEN, never fills an order — see Dispatcher.tick.
    CLEARS_SCREEN = True

    def __init__(self, *, capture=None, tap=None, back=None, chrome=None, settle=None) -> None:
        self._capture, self._tap, self._back = capture, tap, back
        self._chrome, self._settle = chrome, settle

    def work(self, goal: Any, state: Any) -> ActivityResult:
        """One exit, then finish.

        `goal` is ignored on purpose, as in the idle lock: the screen affords two gestures
        and no goal makes a different one correct. There is nothing here to decide.
        """
        capture, tap, back = self._capture, self._tap, self._back
        chrome, settle = self._chrome, self._settle
        if capture is None:
            from capture.adb_capture import capture_screen as capture   # type: ignore[no-redef]
        if tap is None:
            from actions.adb_actions import tap as tap                  # type: ignore[no-redef]
        if back is None:
            from actions.adb_actions import press_back as back          # type: ignore[no-redef]
        if chrome is None:
            from vision.chrome_detector import ChromeDetector
            chrome = ChromeDetector().detect
        if settle is None:
            from actions.ui import settle as settle                     # type: ignore[no-redef]

        try:
            frame = getattr(state, "frame", None) or capture()
            found = chrome(frame)
        except Exception as exc:
            logger.error(f"[{STATE}] could not read the screen's chrome: {exc}")
            return ActivityResult(BLOCKED, {}, detail=str(exc))

        home = (getattr(found, "positions", None) or {}).get("home")
        if getattr(found, "has_home", False) and home:
            score = (getattr(found, "scores", None) or {}).get("home", 0.0)
            logger.info(f"[{STATE}] Home at {home} (matched {score:.2f}) — tapping")
            try:
                tap(*home)
            except Exception as exc:
                logger.error(f"[{STATE}] the Home tap failed: {exc}")
                return ActivityResult(BLOCKED, {"exit": "home"}, detail=str(exc))
            settle("transition")
            # WHERE THIS LANDS IS NOT REPORTED. Home leads to the port overworld from a port
            # screen and elsewhere from elsewhere, and the dispatcher re-perceives after
            # every activity — which is where the destination comes from, not from here.
            return ActivityResult(FINISHED, {"exit": "home", "at": home}, detail="home tapped")

        if getattr(found, "has_home", False):
            logger.warning(f"[{STATE}] Home detected but its position was not recorded — "
                           "backing out rather than tapping a remembered point")

        logger.info(f"[{STATE}] no usable Home — pressing back")
        try:
            back()
        except Exception as exc:
            logger.error(f"[{STATE}] the back press failed: {exc}")
            return ActivityResult(BLOCKED, {"exit": "back"}, detail=str(exc))
        settle("transition")
        return ActivityResult(FINISHED, {"exit": "back"}, detail="back pressed")
