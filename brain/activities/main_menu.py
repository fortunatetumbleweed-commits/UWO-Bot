"""The main menu: one exit, and it is NOT Back.

The menu is the overlay of Ship / Fleet / Assign / Build / Storage / Mates / ... over
whatever the fleet was looking at, closed by an X in its top-right corner. It has a
repertoire of exactly one action and an unknown destination — the same shape as the idle lock
and the unnameable chromed screen next door, which is why it is an ordinary activity here
rather than anything special.

WHY IT NEEDED ITS OWN. Nothing served `main_menu`, so the dispatcher said "no activity for
state 'main_menu' — asking for a goal", found nothing to do, and stopped after three
identical ticks. Live 2026-09-07 it blocked two runs in a row: the menu was sitting over an
arrival gate at Madeira, and the mission could not start because the bot could not put the
menu away.

AND IT COULD NOT BORROW ONE. `UnrecognizedChromedActivity` prefers Home and falls back to
BACK — and Back on the main menu raises "Exit Game?" (`brain/intents._NEVER_BACK_FROM` lists
it for exactly that reason). Measured on the live screen, the chrome detector finds no home,
no back and no hamburger, so borrowing that activity would have pressed Back every time.
`actions.screen_exit.exit_current_screen` refuses here too — it finds no on-screen exit.

THE X IS THE RIGHTMOST ICON IN THE TOP-RIGHT, and that rule is safe only because the SCREEN
is already identified: `classify_nav_state` names this menu by its tile bar (auction, friend,
guild, rank). A general "an unlabelled corner icon is a close" would tap the chat bubble
beside it — measured 2026-09-07, chat at (2078,2)-(2156,93) and the X at (2173,4)-(2259,96),
adjacent and both unlabelled. Knowing WHICH screen this is turns a guess into a lookup.
"""
from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from brain.dispatcher import BLOCKED, FINISHED, ActivityResult

STATE = "main_menu"

# The corner the close X lives in, as fractions of the frame. Generous — it is narrowed by
# taking the RIGHTMOST icon inside it, not by the box being tight.
_CORNER = (0.80, 0.0, 1.0, 0.16)
# A close X is a small square-ish icon. The tile-bar buttons below are far larger, and this
# keeps a stray banner out of the corner search.
_MAX_ICON = 140


class MainMenuActivity:
    """Close the menu, then finish. Where it lands is the dispatcher's next look."""

    # THE MAIN MENU IS DRAWN OVER THE WORLD, NOT A WORLD OF ITS OWN (user, 2026-09-09:
    # "mainmenu is an overlay, it does not have anything to do with the task. It should not
    # affect the task status"). The same marker the idle lock, a full-screen notice and an
    # unnameable chromed screen carry, and it is what stops this activity's FINISHED being
    # read as the GOAL's.
    CLEARS_SCREEN = True


    name = "main_menu"
    SERVES = (STATE,)
    CAN_START = ()

    def __init__(self, *, capture_fn=None, tap_fn=None, omni_fn=None) -> None:
        self._capture, self._tap, self._omni = capture_fn, tap_fn, omni_fn

    def work(self, goal: Any, state: Any) -> ActivityResult:
        """`goal` is ignored on purpose, as in the idle lock: the screen affords one gesture
        and no goal makes a different one correct."""
        frame = getattr(state, "frame", None)
        if frame is None:
            capture = self._capture
            if capture is None:
                from capture.adb_capture import capture_screen as capture
            frame = capture()

        spot = self._close_x(frame)
        if spot is None:
            # NEVER BACK FROM HERE — it raises "Exit Game?". Reporting is the honest answer;
            # the dispatcher looks again, and a human can close it if it truly will not.
            logger.warning("[main_menu] no close X in the top-right — reporting rather than "
                           "pressing Back, which would offer to quit the game")
            return ActivityResult(BLOCKED, {"state": STATE},
                                  detail="the main menu has no readable close X")

        tap = self._tap
        if tap is None:
            from actions.adb_actions import tap as tap
        logger.info(f"[main_menu] closing via the X @ {spot}")
        tap(*spot)
        try:
            from actions.ui import settle
            settle("transition")
        except Exception as exc:              # noqa: BLE001 — a slower close, not a failure
            logger.debug(f"[main_menu] could not settle: {exc}")
        # WHERE THIS LANDS IS NOT REPORTED. The menu covers whatever was underneath, and the
        # dispatcher re-perceives after every activity — which is where the answer comes from.
        return ActivityResult(FINISHED, {"exit": "close_x", "at": spot}, detail="menu closed")

    def _close_x(self, frame) -> Optional[tuple]:
        """The rightmost small icon in the top-right corner, or None."""
        omni = self._omni
        try:
            if omni is None:
                from vision.omniparser import parse_fast_cached as omni
            els = list(omni(frame))
        except Exception as exc:              # noqa: BLE001 — blind, not broken
            logger.debug(f"[main_menu] could not read the screen: {exc}")
            return None
        w, h = frame.width, frame.height
        x0, y0, x1, y1 = _CORNER
        best = None
        for e in els:
            if (getattr(e, "element_type", "") or "") != "icon":
                continue
            if not (x0 * w <= e.cx <= x1 * w and y0 * h <= e.cy <= y1 * h):
                continue
            if (e.x2 - e.x1) > _MAX_ICON or (e.y2 - e.y1) > _MAX_ICON:
                continue
            if best is None or e.cx > best.cx:
                best = e
        return (int(best.cx), int(best.cy)) if best is not None else None
