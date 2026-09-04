"""The harbour: depart, and hire the crew that departing needs.

A BLOCKER IS NOT A RECOVERY — IT IS THE NEXT GOAL (user, 2026-08-26). "Not enough crew" is
not an exception raised by sailing; it is the world saying the fleet is not ready, and the
remedy is an ordinary piece of work: go somewhere, hire crew, come back. Modelled that way it
needs no ladder, no escalation and no teaching.

What it replaced is worth keeping in view. `memory/knowledge/fsm/flows.json` answers this
blocker with a six-step UI script — exit to the overworld, navigate to the inn, tap the
primary action, exit again, navigate back to the harbour — and when that script missed,
`resolve_fleet_blocker` climbed a four-rung ladder (KB → learned recovery → Claude Vision →
wake the human). Eleven of the eighteen entries in learned_recoveries.json are this one
situation, taught over and over, one of them keyed on the literal string '181,224'.

Under the model there is no ladder. The activity reports what blocks it; the task runner
hands back a goal; this same activity serves that goal. `Depart` and `RecruitCrew` are the
same building doing two of its jobs.

THIS ACTIVITY DOES NOT NAVIGATE. It never walks to the inn and never sails. If the crew
cannot be hired here, it says so and the dispatcher decides where to go — because entering a
building is a transition, and transitions are the dispatcher's alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from loguru import logger

from brain.dispatcher import ActivityResult, BLOCKED, FINISHED, UNRECOGNISED


# ── The goals ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Depart:
    """Put to sea, bound for `destination`. Stops when the fleet is at sea, or when
    something the harbour can name is preventing it."""
    destination: str = ""

    def __str__(self) -> str:
        return f"depart for {self.destination}" if self.destination else "depart"


@dataclass(frozen=True)
class RecruitCrew:
    """Have enough crew to sail. No number: the game knows the minimum and says when it is
    met, and a quantity here would be an estimate pretending to be a contract."""

    def __str__(self) -> str:
        return "recruit crew"


# The blockers this port knows how to answer, and what to do about each. A blocker with no
# goal is NOT a failure of this table — it is a situation nobody has designed yet, and
# saying so beats inventing a recovery for it.
def goal_for_blocker(blocker: Optional[str]) -> Optional[Any]:
    """The named blocker → the goal that clears it. None when nothing here can."""
    said = (blocker or "").strip().lower()
    if not said:
        return None
    if "crew" in said:
        return RecruitCrew()
    # "not enough supply" resolves at the MARKET, not here, and "not enough ship life" at the
    # shipyard. Both are goals too — they are simply not this activity's to serve, so this
    # returns None and the task runner routes them.
    return None


class HarborActivity:
    """Depart, and hire crew. Nothing else."""

    name = "harbor"

    SERVES = ("building:harbor", "building:harbour",
              "sub_menu:harbor", "sub_menu:harbour", "sub_menu:departure")

    # A building, like the market: Back out, nothing else. Departing is WORK done here, not a
    # transition this dispatcher starts.
    CAN_START = ("EXIT_BUILDING",)

    # Out of a building is onto the overworld it stands in.
    LEADS_TO = {"EXIT_BUILDING": "port_overworld"}

    # WHICH ORDERS IT SERVES. Declaring none does not mean "none": the dispatcher reads an
    # activity with no GOALS as a state-CLEARING one, in the way of every goal and obliged to
    # run whatever the order is. So this absorbed every goal at the harbour and answered for
    # orders it cannot fill — the same latent bug VillageActivity had, and the reason
    # AshoreActivity was given GOALS explicitly from the start.
    GOALS: tuple = (Depart, RecruitCrew)

    def __init__(self, *, readiness_fn=None, depart_fn=None, recruit_fn=None,
                 look_fn=None, resolve_fn=None, ask=None) -> None:
        self._readiness = readiness_fn
        self._depart = depart_fn
        self._recruit = recruit_fn
        self._look = look_fn
        self._resolve = resolve_fn
        self._ask = ask

    # ── the one entry point ──────────────────────────────────────────────────
    def work(self, goal: Any, state: Any) -> ActivityResult:
        where = getattr(state, "state", None) or getattr(state, "location", None)
        if where is not None and where not in self.SERVES:
            return ActivityResult(UNRECOGNISED, {"state": where},
                                  detail=f"not in a harbour ({where!r})")

        if isinstance(goal, Depart):
            return self._try_depart(goal)
        if isinstance(goal, RecruitCrew):
            return self._recruit_crew(goal)
        return ActivityResult(BLOCKED, {}, detail=f"the harbour cannot serve {goal!r}")

    # ── depart ───────────────────────────────────────────────────────────────
    def _try_depart(self, goal: Depart) -> ActivityResult:
        readiness = self._readiness or _default_readiness
        reading = readiness() or {}

        if not reading.get("ready") and not reading.get("on_departure_panel"):
            # Looking at the wrong screen is not a blocker — it is being lost, and being lost
            # is not something an activity can fix by looking again.
            return ActivityResult(UNRECOGNISED, {"state": "harbor"},
                                  detail="not on the departure panel")

        if not reading.get("ready"):
            blocker = reading.get("blocker") or {}
            named = blocker.get("text") if isinstance(blocker, dict) else str(blocker)
            logger.info(f"[harbor] cannot depart: {reading.get('detail')}")
            # BLOCKED, with the blocker NAMED. The next goal follows from the name, and this
            # activity does not decide it — reporting is the whole job here.
            return ActivityResult(
                BLOCKED,
                {"blocker": named, "next_goal": goal_for_blocker(named),
                 "stopped_because": reading.get("detail") or "not ready"},
                detail=str(goal))

        depart = self._depart or _default_depart
        which = depart()
        if which == "not_found":
            # The panel says ready and yet there is no button — that is a disagreement, not
            # a blocker, and it hands back rather than guessing at a coordinate.
            return ActivityResult(UNRECOGNISED, {"state": "harbor"},
                                  detail="ready, but no departure button found")
        logger.info(f"[harbor] departed for {goal.destination or 'sea'} via {which}")
        # An INTENT, not a finish: this names its successor. The dispatcher perceives what
        # actually happened, so nothing here waits for the sea to appear.
        return ActivityResult(FINISHED, {"departed": True, "for": goal.destination,
                                         "via": which, "stopped_because": "under way"},
                              detail=str(goal))

    # ── recruit ──────────────────────────────────────────────────────────────
    def _recruit_crew(self, goal: RecruitCrew) -> ActivityResult:
        recruit = self._recruit or _default_tap_recruit
        if not recruit():
            return ActivityResult(BLOCKED, {"stopped_because": "no Recruit Crew control here"},
                                  detail=str(goal))

        answered = self._answer_any_dialog(goal)
        # ONE RUNG, NOT A LADDER. Whether the crew is now sufficient is not asked here — the
        # dispatcher re-perceives and the next Depart reads the panel, which is the game's
        # own answer and outranks anything this could compute.
        return ActivityResult(FINISHED,
                              {"recruited": True, "dialog": answered,
                               "stopped_because": "recruit attempted"},
                              detail=str(goal))

    def _answer_any_dialog(self, goal: Any) -> Optional[str]:
        """A confirmation is expected here — recruiting costs ducats. Answer it by DESCRIBING
        it and letting its buttons be the answer space."""
        from brain.dialog_question import Situation, decider
        from brain.unexpected import ACTION_DIALOG

        look = self._look
        if look is None:
            from brain.unexpected import look
        resolve = self._resolve
        if resolve is None:
            from brain.unexpected import resolve

        u = look()
        if getattr(u, "case", None) != ACTION_DIALOG:
            return None

        situation = Situation(
            place="the HARBOR",
            goal="set sail",
            tried=("The fleet could not depart for want of crew, so I tapped Recruit Crew.",))
        res = resolve(u, decide=decider(situation, ask=self._ask)) or {}
        logger.info(f"[harbor] dialog: {res.get('reason')}")
        return res.get("action")


# ── the defaults, which touch the device ─────────────────────────────────────

def _default_readiness() -> dict:
    from actions.sail_actions import read_fleet_readiness
    return read_fleet_readiness()


def _default_depart() -> str:
    """Returns which button was tapped: 'supply_depart' | 'depart_now' | 'not_found'."""
    from actions.sail_actions import _tap_depart_button
    from capture.adb_capture import capture_screen
    return _tap_depart_button(capture_screen())


def _default_tap_recruit() -> bool:
    from actions import ui
    from capture.adb_capture import capture_screen
    return bool(ui.tap_text(capture_screen(), "recruit crew", why="goal: recruit crew"))
