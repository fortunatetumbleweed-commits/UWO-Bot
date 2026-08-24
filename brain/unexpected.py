"""What to do when the bot perceives something it was not expecting.

There are exactly four cases (user, 2026-08-23), and they are handled in ONE place so the
policy can be reasoned about. Four private recovery policies scattered through the action
layer is what let one loop's Back undo another loop's progress.

  1. STATE_CHANGED   — an unexpected but RECOGNISED state. The screen is ground truth: adopt
                       it and let the task re-derive its next step. Never force the screen
                       back to match a stale expectation.
  2. ACTION_DIALOG   — a dialog raised by the bot's own action, offering a decision
                       (Ok/Cancel). Whether to accept is a TASK question, sometimes needing
                       knowledge of the game's rules, so this module surfaces the choice and
                       does not make it.
  3. SYSTEM_POPUP    — daily news, event, promo. Unsolicited, no decision to make: dismiss.
  4. UNKNOWN_SCREEN  — a chromed screen the bot has not learned, nothing blocking. Leave via
                       Home to an overworld and re-derive.

The split between 2 and 3 is structural, not historical: a popup offering ACTIONS is a
decision; a popup offering only a close-X is noise. That is testable from the frame, whereas
"did we cause it?" depends on bookkeeping the bot does not reliably keep.

See docs/one_loop_task_drives_state.md — "the state machine owns the HOW, the task owns the
WHETHER, advised by a reasoning layer that knows the GAME".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence, Tuple

from loguru import logger

STATE_CHANGED = "state_changed"
ACTION_DIALOG = "action_dialog"
SYSTEM_POPUP = "system_popup"
UNKNOWN_SCREEN = "unknown_screen"
CLEAR = "clear"


@dataclass(frozen=True)
class Unexpected:
    """What perceive found in the way, and what can be done about it."""

    case: str
    state: Optional[str] = None                 # the perceived nav state
    text: Tuple[str, ...] = ()                  # dialog/popup body, for a decision
    options: Tuple[str, ...] = ()               # action labels offered, e.g. ('Ok', 'Cancel')
    positive: Optional[str] = None              # the label that commits, when there is one
    bbox: Optional[Tuple[int, int, int, int]] = None
    signals: Sequence[str] = field(default_factory=tuple)

    @property
    def blocks(self) -> bool:
        """True when something must be resolved before the task can act."""
        return self.case in (ACTION_DIALOG, SYSTEM_POPUP, UNKNOWN_SCREEN)

    @property
    def needs_decision(self) -> bool:
        """True when only the task (or the game's rules) can say what to do."""
        return self.case == ACTION_DIALOG


# States that are PLACES, not screens laid over one. Being in one unexpectedly is case 1:
# the task adopts it. It is never a thing to "escape".
_PLACES = ("port_overworld", "village", "sea", "sea_cinematic", "world_map")


def look(frame=None, *, expected_state: Optional[str] = None) -> Unexpected:
    """Classify what is in front of the bot, from one frame. Never taps.

    `expected_state` is what the caller believed it was looking at; a mismatch with no
    obstruction is case 1.
    """
    if frame is None:
        from capture.adb_capture import capture_screen
        frame = capture_screen()

    state = None
    try:
        from actions.sail_actions import where_am_i
        state = where_am_i(frame).get("location")
    except Exception as exc:
        logger.debug(f"[unexpected] could not read the state: {exc}")

    dialog = _read_dialog(frame)
    if dialog is not None:
        actions = tuple(a.label for a in dialog.actions)
        positive = next((a.label for a in dialog.actions if a.is_positive), None)
        # A decision is offered → the task must make it. Only a close-X → noise.
        case = ACTION_DIALOG if actions else SYSTEM_POPUP
        return Unexpected(case=case, state=state, text=tuple(dialog.body_text),
                          options=actions, positive=positive, bbox=dialog.bbox,
                          signals=("dialog",))

    obstruction = _read_obstruction(frame)
    if obstruction is not None and obstruction.is_obstructed:
        # Popups and overlays with no action row carry no decision — they are in the way.
        return Unexpected(case=SYSTEM_POPUP, state=state, bbox=obstruction.bbox,
                          signals=tuple(obstruction.signals))

    if state is not None and state not in _PLACES and state != "building":
        # A chromed screen the bot has no route out of, with nothing blocking it.
        return Unexpected(case=UNKNOWN_SCREEN, state=state, signals=("unrouted_screen",))

    if expected_state and state and state != expected_state:
        return Unexpected(case=STATE_CHANGED, state=state,
                          signals=(f"expected={expected_state}",))

    return Unexpected(case=CLEAR, state=state)


def resolve(u: Unexpected, *, decide: Optional[Callable[[Unexpected], Optional[str]]] = None,
            tap_label: Optional[Callable[[str], bool]] = None) -> dict:
    """Act on what `look` found. Returns {handled, action, reason}.

    `decide` is the TASK's (or reasoning layer's) answer for an ACTION_DIALOG — given the
    Unexpected, return the label to tap, or None to leave it alone. Without it this refuses
    rather than guessing: the whole point of case 2 is that the decision is not the state
    machine's to make.
    """
    if u.case == CLEAR:
        return {"handled": True, "action": None, "reason": "nothing in the way"}

    if u.case == STATE_CHANGED:
        # Nothing to DO — the correction is that the caller updates its expectation.
        return {"handled": True, "action": None,
                "reason": f"state is {u.state!r} — adopt it and re-derive"}

    if u.case == ACTION_DIALOG:
        if decide is None:
            return {"handled": False, "action": None, "needs_decision": True,
                    "reason": f"dialog offers {list(u.options)} — the task must choose"}
        choice = decide(u)
        if not choice:
            return {"handled": False, "action": None,
                    "reason": "the task declined to answer the dialog"}
        ok = _tap_label(choice, tap_label)
        return {"handled": bool(ok), "action": f"tap:{choice}",
                "reason": f"tapped {choice!r}" if ok else f"could not find {choice!r}"}

    if u.case == SYSTEM_POPUP:
        from brain.unexpected_dialog import clear_blockers
        from capture.adb_capture import capture_screen
        try:
            res = clear_blockers(capture_screen())
        except Exception as exc:
            return {"handled": False, "action": None, "reason": f"dismiss failed: {exc}"}
        return {"handled": bool(res.get("cleared")), "action": "dismiss",
                "reason": res.get("reason", "")}

    if u.case == UNKNOWN_SCREEN:
        from actions.screen_exit import exit_current_screen
        res = exit_current_screen()
        method = getattr(res, "method", None)
        return {"handled": method not in (None, "refused"), "action": f"exit:{method}",
                "reason": f"unlearned screen {u.state!r} — left via {method}"}

    return {"handled": False, "action": None, "reason": f"unhandled case {u.case!r}"}


# ── internals ────────────────────────────────────────────────────────────────


def _read_dialog(frame):
    try:
        from vision.omniparser import parse_fast_cached
        from vision.region_detectors.dialog import detect_dialog
        return detect_dialog(list(parse_fast_cached(frame)), frame.width, frame.height)
    except Exception as exc:
        logger.debug(f"[unexpected] dialog read failed: {exc}")
        return None


def _read_obstruction(frame):
    try:
        from vision.obstruction_classifier import classify_obstruction
        from vision.screen_perception import parse_screen
        return classify_obstruction(parse_screen(frame))
    except Exception as exc:
        logger.debug(f"[unexpected] obstruction read failed: {exc}")
        return None


def _tap_label(label: str, tap_label: Optional[Callable[[str], bool]]) -> bool:
    if tap_label is not None:
        return bool(tap_label(label))
    from actions import ui
    from capture.adb_capture import capture_screen
    return bool(ui.tap_text(capture_screen(), label.lower(), why=f"dialog → {label}"))
