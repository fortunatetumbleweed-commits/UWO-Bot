"""A full-screen notice: one tap, unknown destination.

The game interrupts with these constantly — a mate finishing promotion, a level-up, an
announcement, a reward. They take over the WHOLE screen, they carry no close-X and no OK, and
they are cleared by tapping anywhere. That combination is what makes them a state rather than
a popup, and it is the same shape as the idle lock: one action, and a destination this
activity has no business guessing.

WHAT THIS IS NOT. It is not a dialog. A dialog offers a CHOICE and its buttons are the answer
space (`brain.dialog_question`); tapping one blindly is exactly the mistake this bot has made
before. `perceive` only returns `transient` when no dialog card is present, so by the time
this runs the choice has already been ruled out.

It is also not loading. Loading is a transient the CNN groups here too, but a loading screen
has no activity — nothing is resumed on it, so the dispatcher asks the task runner and ticks
again. A tap on a loading screen is harmless; a tap that navigates is not, which is why this
taps the DEAD ZONE rather than the centre.

Why this exists (2026-08-26): mates finished promotion mid-session and the "Effect Unlocked"
screen appeared. The family CNN identified it as transient at 0.9999 and the classifier ignored
that, spending 23 seconds in the legacy cascade — omniparser, chrome flags, Moondream three
times, read_port_name — to arrive at "Unknown blocking screen", from which the escalation
machinery starts guessing. Every question in that cascade asks WHERE THE FLEET IS, and nothing
about a notice covering the whole screen can answer it.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from brain.dispatcher import ActivityResult, BLOCKED, FINISHED

# WHERE TO TAP, normalised so it rides resolution and the notch.
#
# NOT the centre. A result screen puts its content there — reward tiles, effect icons, a
# portrait — and some of those are themselves tappable, which turns "dismiss this" into
# "navigate somewhere". The upper-left quadrant is background on every notice seen so far:
# the promotion screen has sky there, and the bottom-right "Other Mates (1)" button — the one
# control that would take the bot somewhere — is nowhere near it.
_TAP_NORM = (0.18, 0.16)


class TransientActivity:
    """Tap the notice away, then finish. The whole repertoire."""

    name = "transient"

    # THE STATE IT SERVES, DECLARED HERE like every other activity. It used to be assigned in

    # A full-screen notice ends by being dismissed. Nothing is reachable THROUGH it.
    CAN_START = ()
    # `default_activities()` as `registry["transient"] = [...]` — a second place that knew the
    # state's name, and worse, an ASSIGNMENT where the others append: an activity declaring
    # SERVES = ("transient",) would have been silently dropped, which is the very overwrite bug
    # the setdefault/append beside it exists to prevent.
    SERVES = ("transient",)

    # Clears a SCREEN, never fills an order. Live 2026-08-27 its FINISHED retired a
    # Hold order for 253 Matchlock Gun that nobody had bought — see Dispatcher.tick.
    CLEARS_SCREEN = True

    # TAPPED ON SIGHT, AND THAT IS THE RIGHT ANSWER (user, 2026-09-01). A transient is either
    # a NOTICE waiting for a gesture or a TRANSITION that resolves itself, and the CNN cannot
    # tell them apart — but the choice does not need it to:
    #
    #   * on a notice, the tap is the whole job;
    #   * on the arrival transition, the tap SPEEDS IT UP, and doing nothing still arrives.
    #
    # So there is no case where waiting wins. A "look again first" was tried here and removed:
    # it spent a couple of seconds asking a question whose two answers both end in a tap.
    #
    # The tap is aimed at the DEAD ZONE rather than the centre, which is what keeps "dismiss
    # this" from becoming "navigate somewhere" — see `_TAP_NORM`.

    def __init__(self, *, tap=None, settle=None) -> None:
        self._tap = tap
        self._settle = settle

    def work(self, goal: Any, state: Any) -> ActivityResult:
        """Perform the one action this world affords.

        `goal` is ignored on purpose: the screen affords a single gesture and no goal makes a
        different one correct. An activity with one action does not need to be told which.
        """
        tap, settle = self._tap, self._settle
        if tap is None:
            from actions.adb_actions import tap as tap              # type: ignore[no-redef]
        if settle is None:
            from actions.ui import settle as settle                 # type: ignore[no-redef]

        w, h = _frame_size(state)
        x, y = int(_TAP_NORM[0] * w), int(_TAP_NORM[1] * h)

        logger.info(f"[transient] tapping the notice away @ ({x},{y})")
        try:
            tap(x, y)
        except Exception as exc:
            # Report, do not retry. A tap that cannot be issued is not something this activity
            # can reason about.
            logger.error(f"[transient] the dismissing tap failed: {exc}")
            return ActivityResult(BLOCKED, {"gesture": "tap"}, detail=str(exc))

        settle("transition")

        # NOT REPORTED: where the bot now is. A notice can cover any world and this activity
        # cannot see through it. The dispatcher re-perceives after every activity; that is
        # where the destination comes from. Several notices can also queue — the promotion
        # screen says "Other Mates (1)" — and that needs nothing special either: the next tick
        # perceives another transient and this runs again.
        return ActivityResult(FINISHED, {"gesture": "tap"}, detail="notice tapped away")


def _frame_size(state: Any) -> tuple[int, int]:
    frame = getattr(state, "frame", None)
    if frame is not None and getattr(frame, "width", None):
        return frame.width, frame.height
    return 2400, 1080
