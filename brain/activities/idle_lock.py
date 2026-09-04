"""The idle standby lock: one action, unknown destination.

The game drops into this after inactivity, and it takes over the WHOLE screen — nothing of the
previous world survives. That is what makes it a state rather than a popup, and why it lives
here as an activity instead of being swept aside during perception.

It has exactly two properties, and they are usually confused with each other:

  * Its exit action is KNOWN, because the screen says it: "Slide up to unlock".
  * Its destination is NOT known. The swipe returns to whatever world the bot was in, and the
    bot must not assume that. It may remember a previous state, but a remembered state is a
    belief, and beliefs are what go stale.

So this activity swipes and FINISHES. It does not report where the bot now is, because it
cannot know — the dispatcher re-perceives after every activity anyway, which is how the
destination is established. That generality is the point: no caller anywhere needs to learn
what a lock is. `brain/barter_mission_live.py` gains no line about locks; neither does the
market, the harbour or the world map.

Why this exists (2026-08-26): at Svear Village, with 445 Iron, 146 Matchlock Gun and 438 Candle
aboard and the barter one tap away, the game locked while the panel was opening. Perception
named the screen correctly on every look — as `learned_on_standby_at_sea_slide_up_to_unlock`,
a LEARNED fingerprint the FSM had no node for — so nothing could route out of it. The mission
re-perceived a screen it could not act through for 16 minutes, retried, re-perceived for
another 17, and aborted at the destination it had sailed an hour to reach.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from brain.dispatcher import ActivityResult, BLOCKED, FINISHED

# The swipe that dismisses it, in normalised coordinates so it rides resolution and the
# camera-notch shift. Bottom-centre to mid-screen: the gesture the prompt asks for.
_FROM_NORM = (0.50, 0.82)
_TO_NORM = (0.50, 0.30)
_DURATION_MS = 500


class IdleLockActivity:
    """Swipe up, then finish. The whole repertoire."""

    name = "idle_lock"

    # THE STATE IT SERVES, DECLARED HERE like every other activity. It used to be assigned in

    # ENDS BY FINISHING, so it starts nothing. The lock answers one gesture — the swipe its
    # own prompt names — and Back does nothing to it. Live 2026-08-26 at Stockholm the
    # leave-the-village loop pressed Back at the lock and would have spent all four attempts
    # that way. `_ends_by_finishing` already says this; declaring it here is the same fact
    # where the dispatcher can act on it.
    CAN_START = ()
    # `default_activities()` as `registry["idle_lock"] = [...]` — a second place that knew the
    # state's name, and worse, an ASSIGNMENT where the others append: an activity declaring
    # SERVES = ("idle_lock",) would have been silently dropped, which is the very overwrite bug
    # the setdefault/append beside it exists to prevent.
    SERVES = ("idle_lock",)

    # This activity clears a SCREEN; it never fills an order. Its FINISHED means "the
    # swipe is done", not "the goal is served" — see Dispatcher.tick.
    CLEARS_SCREEN = True

    def __init__(self, *, swipe=None, settle=None) -> None:
        self._swipe = swipe
        self._settle = settle

    def work(self, goal: Any, state: Any) -> ActivityResult:
        """Perform the one action this world affords.

        `goal` is ignored on purpose. There is nothing to choose here — the screen affords a
        single gesture, and no goal makes a different one correct. An activity with one action
        does not need to be told which.
        """
        swipe, settle = self._swipe, self._settle
        if swipe is None:
            from actions.adb_actions import swipe as swipe          # type: ignore[no-redef]
        if settle is None:
            from actions.ui import settle as settle                 # type: ignore[no-redef]

        w, h = _frame_size(state)
        x1, y1 = int(_FROM_NORM[0] * w), int(_FROM_NORM[1] * h)
        x2, y2 = int(_TO_NORM[0] * w), int(_TO_NORM[1] * h)

        logger.info(f"[idle_lock] slide up to unlock: ({x1},{y1}) -> ({x2},{y2})")
        try:
            swipe(x1, y1, x2, y2, _DURATION_MS)
        except Exception as exc:
            # Report, do not retry here. A swipe that cannot be issued is not a thing this
            # activity can reason about, and the task runner may well decide to stop.
            logger.error(f"[idle_lock] the unlock swipe failed: {exc}")
            return ActivityResult(BLOCKED, {"gesture": "swipe_up"}, detail=str(exc))

        settle("transition")

        # NOTE WHAT IS *NOT* REPORTED: where the bot now is. This activity has no idea, and
        # guessing would hand the task runner a belief dressed as an observation. The
        # dispatcher re-perceives after every activity; that is where the destination comes
        # from.
        return ActivityResult(FINISHED, {"gesture": "swipe_up"}, detail="unlock swiped")


def _frame_size(state: Any) -> tuple[int, int]:
    """The frame's dimensions, falling back to the phone's fixed landscape resolution."""
    frame = getattr(state, "frame", None)
    if frame is not None and getattr(frame, "width", None):
        return frame.width, frame.height
    return 2400, 1080
