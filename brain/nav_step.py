"""The state machine's single-move API: take ONE step toward a requested UI state.

This is the half of `docs/one_loop_task_drives_state.md` that the state machine owns. It
answers exactly one question — "what is the next move from here to there?" — makes that one
move, and reports what happened. It owns no sequence, no retries and no recovery policy: the
caller's loop owns iteration, so the task is consulted between every move.

Contrast with what it replaces. `open_world_map`, `navigate_to_building` and friends each ran
a private loop that re-perceived, judged the state wrong, and navigated to force it. Those
loops could not know what the bot was trying to achieve, so they were locally reasonable and
globally wrong — one pressed Back until the fleet left the village a mission had just sailed
to (live 2026-08-22).

Route-finding is NOT reimplemented here. `brain.planner.find_path` already BFSes the FSM
graph between arbitrary states; this module executes a single edge of that path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger

# Outcomes of one step.
MOVED = "moved"              # a move was made; re-perceive and ask again
ARRIVED = "arrived"          # already at the requested state
BLOCKED = "blocked"          # something is in the way (see brain.unexpected)
NO_ROUTE = "no_route"        # the FSM knows no way from here to there
REFUSED = "refused"          # the move exists but would cost more than the caller may spend


@dataclass(frozen=True)
class StepResult:
    outcome: str
    state: Optional[str] = None          # where we were when the step was taken
    action: Optional[str] = None         # what the step did
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in (MOVED, ARRIVED)


# Leaving one of these costs POSITION the task may have sailed for, so it is never done to
# satisfy a state test. The caller must opt in explicitly.
_PLACES = ("port_overworld", "village")


def step_toward(target: str, *, frame=None, state: Optional[str] = None,
                may_leave_a_place: bool = False) -> StepResult:
    """Take at most ONE move from the current state toward `target`.

    Returns immediately after that move — it does not wait for the transition to land. A
    transition in progress is just another state (`loading`), which the caller's next tick
    perceives; that is what lets a task react mid-transition instead of blocking inside a
    poll.

    `may_leave_a_place` must be set for a step that would walk the fleet out of a settlement.
    From a village, leaving IS the only route to the world map — but whether the position can
    be spent is the task's call, not this module's.
    """
    if state is None:
        state = _current_state(frame)
    if state is None:
        return StepResult(BLOCKED, None, None, "state unreadable — re-perceive")
    if state == target:
        return StepResult(ARRIVED, state, None, f"already at {target!r}")

    # A STATE WHOSE ONE EXIT LEADS SOMEWHERE UNDECLARED IS NOT A DEAD END — it is a state you
    # leave by acting and then LOOKING. `idle_lock` and `loading` both declare `"to": null`:
    # the action is known (swipe up; wait) and the destination is not, so BFS can build no
    # path through them and `_find_path` correctly returns nothing.
    #
    # Reporting NO_ROUTE there is wrong twice over. It is not true — there IS a way out, and
    # the registry names it — and it strands the caller on a screen that would have cleared
    # with one gesture. Live 2026-08-26 at Svear Village the bot sat on the idle lock with a
    # full hold and the barter one tap away, correctly naming the screen and doing nothing.
    #
    # So: perform the declared action and report MOVED. Where it landed is the next
    # perceive's business, which is exactly how `loading` has always worked.
    if state not in _PLACES or may_leave_a_place:
        undeclared = [t for t in _exits_of(state) if t.to is None]
        if undeclared and _find_path(state, target) is None:
            action = undeclared[0].action
            logger.info(f"[nav_step] {state!r} exits by {action!r} to an undeclared "
                        "destination — performing it and re-perceiving")
            if _execute(action, state):
                return StepResult(MOVED, state, action,
                                  f"{action!r} out of {state!r}; destination discovered by "
                                  "the next perceive")

    edges = _find_path(state, target)
    if edges is None:
        return StepResult(NO_ROUTE, state, None, f"no known route {state!r} → {target!r}")
    if not edges:
        return StepResult(ARRIVED, state, None, f"already at {target!r}")

    first = edges[0]
    action = getattr(first, "action", None) or str(first)

    if state in _PLACES and not may_leave_a_place:
        return StepResult(
            REFUSED, state, action,
            f"the fleet is AT {state!r}; moving toward {target!r} would give up that "
            "position — the caller decides whether that is acceptable",
        )

    ok = _execute(action, state)
    if not ok:
        return StepResult(BLOCKED, state, action, f"{action!r} did not run")
    return StepResult(MOVED, state, action, f"{action!r} toward {target!r}")


# ── internals ────────────────────────────────────────────────────────────────


def _current_state(frame) -> Optional[str]:
    try:
        from actions.sail_actions import where_am_i
        if frame is None:
            from capture.adb_capture import capture_screen
            frame = capture_screen()
        return where_am_i(frame).get("location")
    except Exception as exc:
        logger.debug(f"[nav_step] state read failed: {exc}")
        return None


def _find_path(from_state: str, to_state: str):
    try:
        from brain.planner import get_planner
        return get_planner().find_path(from_state, to_state)
    except Exception as exc:
        logger.debug(f"[nav_step] path lookup failed: {exc}")
        return None


def finish_current_activity(frame=None) -> Optional[str]:
    """If this screen's activity ends by FINISHING, finish it. Returns the action performed.

    An activity has exactly two exits, as Android's do (user, 2026-08-26): `finish()`, which
    ends it and names no successor, and `startActivity(intent)`, which names one. In the FSM
    those are `"to": null` and `"to": "<state>"`.

    `idle_lock` and `loading` are the states whose ONLY exit is a finish — the lock's swipe
    names nothing, because nothing about the lock knows what lies beneath it. That is also
    why BFS cannot route through them: there is nothing to route TO, by construction, not
    because the graph is broken.

    Where the finish lands is established the same way every destination is — by perceiving
    afterwards. Returns None when this screen's activity does not end that way, so a caller
    can tell "the activity finished" from "the failure is real".

    INTERIM. Under the dispatcher this needs no separate entry point: an activity finishes,
    the dispatcher perceives, and the lock is no more special than a market. This exists for
    callers that do not yet run through the dispatcher, and should be deleted with them.
    """
    state = _current_state(frame)
    if state is None:
        return None
    undeclared = [t for t in _exits_of(state) if t.to is None]
    if not undeclared:
        return None
    action = undeclared[0].action
    logger.info(f"[nav_step] {state!r} ends by finishing — {action!r}")
    return action if _execute(action, state) else None


def _exits_of(state: str) -> list:
    """The registry's declared exits for `state`, or nothing if it does not know the state."""
    try:
        from brain.fsm_registry import get_fsm_registry
        st = get_fsm_registry().states.get(state)
        return list(getattr(st, "exits", []) or [])
    except Exception as exc:
        logger.debug(f"[nav_step] could not read exits for {state!r}: {exc}")
        return []


def _execute(action: str, state: str) -> bool:
    """Run one FSM edge. Unknown actions are reported, never improvised."""
    from actions import ui
    # THE NAMES MUST BE THE REGISTRY'S, NOT PLAUSIBLE ONES. These were originally written as
    # 'exit_screen'/'home'/'open_world_map'; the FSM's states.json actually says `tap_home`
    # and `open_world_map_from_sea`, so every edge but `press_back` fell through to "no
    # executor" and reported BLOCKED. Live 2026-08-24: reorienting out of a market backed out
    # to `building`, then could not run `tap_home` and gave up one move from the overworld.
    try:
        if action in ("press_back", "back"):
            ui.back(why=f"one step out of {state!r}")
            return True
        if action in ("tap_home", "exit_screen", "home", "tap_x_button"):
            from actions.screen_exit import exit_current_screen
            return getattr(exit_current_screen(), "method", None) not in (None, "refused")
        if action in ("open_world_map_from_sea", "open_world_map"):
            from actions.sail_actions import open_world_map
            return bool(open_world_map())
        if action == "wait":
            import time as _t
            _t.sleep(2.0)                      # a transition in flight — let it land
            return True
        if action == "swipe_up":
            # The idle lock's one exit. Shares the activity's implementation rather than
            # re-rolling the gesture, so there is a single answer to "how do we unlock".
            # Like `wait`, this edge has no declared destination: it succeeds by being
            # performed, and where it lands is established by re-perceiving afterwards.
            from brain.activities.idle_lock import IdleLockActivity
            return IdleLockActivity().work(goal=None, state=None).ok
        # tap_building / tap_sub_menu_item / tap_mini_map / tap_hamburger / tap_globe_icon /
        # tap_city_go_to_city / tap_centre all need a TARGET the state machine does not carry
        # (which building? which menu item?). They are ENTERING moves, and reorienting is
        # about getting OUT, so they are reported rather than improvised.
        logger.warning(f"[nav_step] no executor for FSM action {action!r} — reporting")
        return False
    except Exception as exc:
        logger.warning(f"[nav_step] {action!r} raised: {exc}")
        return False


# ── Reorienting a lost caller ────────────────────────────────────────────────
#
# THE SCREEN IS THE TRUTH; THE BOT'S BELIEF IS WHAT GOES STALE (user, 2026-08-24).
#
# When a primitive cannot see the control it needs, the usual cause is not a bad read — it
# is that the bot is somewhere else than it thinks. `read_fleet_status` hunting the ☰ inside
# a MARKET is the shape of it: the ☰ exists only on the port overworld, so the fleet was
# never going to be readable from there. Its answer was to re-perceive three times in place,
# which only re-confirms the same true screen; what was missing was the TRANSITION.
#
# So a lost caller does three things, in order: hand back, RE-PERCEIVE (`where_am_i` — the
# screen decides, not the remembered state), and MOVE to the state the work requires.
#
# Iteration lives here rather than in `step_toward` because a single step is all the state
# machine should own. This is the bounded loop for callers that are not yet inside
# `task_loop.run_task`; the task loop remains the real owner of the sequence, and asks the
# task what to do between every move rather than driving to a fixed target like this.
_REORIENT_MAX_STEPS = 8


def reorient_to(target: str, *, max_steps: int = _REORIENT_MAX_STEPS,
                capture=None, may_leave_a_place: bool = False) -> StepResult:
    """Move until the screen really IS `target`. Returns the final StepResult.

    `ARRIVED` means the screen was re-perceived and agrees. Anything else is a refusal the
    caller must act on — never a reason to carry on as if the state had been reached.
    """
    if capture is None:
        from capture.adb_capture import capture_screen as capture
    last = StepResult(outcome=NO_ROUTE, reason="never stepped")
    for i in range(max_steps):
        last = step_toward(target, frame=capture(),
                           may_leave_a_place=may_leave_a_place)
        if last.outcome == ARRIVED:
            if i:
                logger.info(f"[reorient] reached {target!r} after {i} move(s)")
            return last
        if last.outcome != MOVED:
            # BEFORE DECLARING FAILURE, ASK WHERE WE ACTUALLY ARE. A step can fail precisely
            # BECAUSE it already arrived: live 2026-08-24, nav_step read 'building' and asked
            # for `tap_home`, while `exit_current_screen` looked at the same screen, saw
            # port_overworld and refused (Back there opens "Exit Game?"). The refusal was
            # reported as BLOCKED and the mission died one step after succeeding.
            # Two readings of one screen disagreed; the newest reading decides.
            again = step_toward(target, frame=capture(),
                                may_leave_a_place=may_leave_a_place)
            if again.outcome == ARRIVED:
                logger.info(f"[reorient] {last.outcome} from {last.state!r}, but the screen "
                            f"now reads {target!r} — already there")
                return again
            logger.warning(f"[reorient] cannot reach {target!r} from {last.state!r}: "
                           f"{last.outcome} — {last.reason}")
            return last
        logger.info(f"[reorient] {last.state!r} → {target!r}: {last.action!r}")
    logger.warning(f"[reorient] still not at {target!r} after {max_steps} moves")
    return last
