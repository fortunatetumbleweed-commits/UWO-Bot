"""Where are we? — the first question, and the dispatcher's to answer.

A fresh run knows nothing. The phone may be locked, the daily news may be up, a perk banner
may be covering the port name, and any of those makes sea and port indistinguishable. Not
knowing is CORRECT; what matters is who resolves it and how.

Live 2026-08-28 it was resolved by a navigation primitive, badly. `open_world_map` — whose
job is one tap — was handed an OS lock screen, a daily-news popup and an Investment Season
banner. It waited for a `TransientActivity` it reached ZERO times, then read "Season" out of
the banner, accepted it as a port name, declared "Overworld confirmed" over a scene model
that had already said `sub_menu:perk`, and finally gave up after ten attempts. It did not
merely fail to recover — it manufactured a false answer about where the fleet was.

THE PROTOCOL (user, 2026-08-28):

    perceive                       (Pass 1 already dismisses named interruptors)
    a state we recognise and can work in?  -> done
    otherwise clear ONE layer — the lock, the notice, the unnameable chromed screen
    perceive again; there may be another layer beneath

Only then is a work order taken from the company task runner. The order used to come first,
and the preamble began navigating before position was established — which is how a map-opener
came to be holding a lock screen.

No new machinery: the clearing activities exist and work. What was missing is that the
BOOTSTRAP goes through the dispatcher at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from brain.dispatcher import ActivityResult, FINISHED

# States the task runner can be asked about. Anything else is a layer to clear first —
# the lock, a full-screen notice, a chromed screen we cannot name, or a frame still
# rendering. Note `world_map` counts: it is a place the bot works in, not an obstruction.
WORKABLE = (
    "port_overworld", "sea", "sea_cinematic", "village", "world_map",
    "building:market", "building:harbor", "building:harbour",
    "sub_menu:market", "sub_menu:purchase", "sub_menu:sell",
    "sub_menu:barter", "sub_menu:harbor", "sub_menu:harbour", "sub_menu:departure",
)


@dataclass(frozen=True)
class KnowWhereWeAre:
    """Establish position before any work is accepted. Carries nothing — there is nothing
    to carry, and a remembered position is the belief this exists to avoid."""

    def __str__(self) -> str:
        return "establish where we are"


class PositionKnownActivity:
    """The goal is met the moment perception lands on a state we can work in.

    NARROW BY CONSTRUCTION: `GOALS = (KnowWhereWeAre,)`, so goal-aware dispatch refuses to
    hand it anything else and every state it serves keeps behaving exactly as it does today
    for ordinary goals.

    It is the counterpart to the clearing activities, which serve ANY goal (they declare no
    GOALS) and so run in the blocking states — one layer per tick — until one of these
    states is reached.
    """

    name = "position_known"
    SERVES = WORKABLE

    # IT SERVES EVERY WORKABLE STATE, so it must claim NOTHING. Capabilities are unioned
    # across the activities serving a world, and this one is here to establish where the
    # fleet is — not to start transitions. A single non-empty entry here would afford that
    # transition in all fifteen worlds at once and undo the whole check.
    CAN_START = ()
    GOALS: tuple = (KnowWhereWeAre,)

    def work(self, goal: Any, state: Any) -> ActivityResult:
        where = getattr(state, "state", None) or getattr(state, "location", None)
        port = getattr(state, "port", None)
        logger.info(f"[bootstrap] position established: {where!r}"
                    f"{f' at {port}' if port else ''}")
        return ActivityResult(FINISHED,
                              {"state": where, "port": port,
                               "sub_menu": getattr(state, "sub_menu", None),
                               "scene_type": getattr(state, "scene_type", None)},
                              detail=f"at {port or where}")


def establish_position(*, max_ticks: int = 12, **kw) -> dict:
    """Tick until the bot knows where it is. Returns {ok, state, port}.

    The layers come off one per tick because that is what the clearing activities do — a
    swipe, a tap, an exit — and because there may be another beneath. `max_ticks` bounds a
    screen nothing can clear; it is a backstop, not a policy.
    """
    from brain.run_goal import run_goal
    res = run_goal(KnowWhereWeAre(), max_ticks=max_ticks, **kw)
    if res is not None and res.ok:
        obs = res.observed or {}
        return {"ok": True, "state": obs.get("state"), "port": obs.get("port"),
                "sub_menu": obs.get("sub_menu"), "scene_type": obs.get("scene_type")}
    logger.warning("[bootstrap] could not reach a state the task runner can work in — "
                   "not accepting a work order on an unknown position")
    return {"ok": False, "state": None, "port": None,
            "reason": "position could not be established"}
