"""The task runner for a barter command. PASSIVE — it is consulted, it does not drive.

Guiding Principle #7. The dispatcher settles to a world, consults this for a work order, and
dispatches it. This module does exactly two things: **hand back the next work order, and
update task status.** It calls nothing.

  - It does not CALL DOWN. There is no import of `actions`, `vision` or `capture` here, and
    there never will be. When the mission needs something read, it does not read it — it
    returns the work order that asks an activity to read it, and is handed the answer on the
    next consult. `tests/test_the_layering_is_enforced.py` holds this module to 0/0.
  - It does not CALL UP. It never invokes the dispatcher or the goal loop.

WHY THIS IS A SEPARATE MODULE. `brain/barter_command.py` is both the CLI entry point and the
task logic, fused into one linear function that perceives 12 times. An entry point legitimately
drives; task logic legitimately does not. Splitting them is what lets either one be enforced.
Steps migrate here one at a time, and each migration lowers that module's budget.

Migrated so far:
  - the RECIPE step: what the village trades, and how many rounds the day still allows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from loguru import logger

from brain.activities.sea import ClearOfTheVillage, ReadHold
from brain.activities.village import ReadBarterPanel
from brain.activities.world_map import RemoteCheck
from brain.barter_quantity import MAX_DAILY_BARTER_ROUNDS
from brain.dispatcher import BLOCKED, FINISHED, still_working

# Task status. The mission's own vocabulary, not the screen's.
NEED_RECIPE = "need-recipe"      # what does this village trade, and how many rounds are left?
HAVE_RECIPE = "have-recipe"      # the reading is in hand and complete
NEED_HOLD = "need-hold"          # what can the ship carry, and what is aboard?
HAVE_HOLD = "have-hold"          # the two numbers are in hand
NEED_PANEL = "need-panel"        # standing in the village: what does the panel say?
HAVE_PANEL = "have-panel"        # the reading is in hand
NEED_CLEAR = "need-clear"        # the tail cannot open the world map from inside a village
HAVE_CLEAR = "have-clear"        # somewhere the map opens from
FAILED = "failed"                # the mission cannot proceed, with a reason


@dataclass
class BarterTaskRunner:
    """One barter command, consulted step by step.

    `next_goal(result, state)` is the whole interface the dispatcher needs. `result` is what
    the last activity handed back (None on the first consult); `state` is what the dispatcher
    perceived. Neither is fetched here.
    """

    village: str
    good: str

    status: str = NEED_RECIPE
    reason: str = ""
    trades: list = field(default_factory=list)
    base: dict = field(default_factory=dict)
    capacity: Optional[int] = None
    used: Optional[int] = None
    panel: Any = None

    # The work order currently outstanding, so a repeated consult does not re-issue it.
    _pending: Any = None

    # ── the interface the dispatcher uses ────────────────────────────────────

    def next_goal(self, result: Optional[Any], state: Any) -> Optional[Any]:
        """The next work order, or None when there is nothing left to ask for."""
        if result is not None:
            self._absorb(result)

        if self.status == NEED_RECIPE:
            if self._pending is None:
                self._pending = RemoteCheck(village=self.village, good=self.good)
                logger.info(f"[barter_runner] asking for a remote check of {self.village}")
            return self._pending

        if self.status == NEED_CLEAR:
            # DECIDED FROM `state`, NOT BY LOOKING. Being clear is a fact about where the
            # fleet is, and the dispatcher perceived it before this consult. The old loop
            # captured a screen and ran `where_am_i` itself, once per attempt, to learn what
            # it was already about to be told.
            where = getattr(state, "state", None) or getattr(state, "location", None)
            if where in _CLEAR_OF_THE_VILLAGE:
                self._pending = None
                self.status = HAVE_CLEAR
                logger.info(f"[barter_runner] at {where!r} — the tail can open the map")
                return None
            if where in _BACK_WOULD_QUIT:
                # Back raises "Exit Game?" here and the tail does not need it. Report rather
                # than press: reaching this means the premise that we were in a village was
                # wrong (live 2026-08-26 the fleet was at Stockholm, moved there for supply).
                self._pending = None
                self.status = FAILED
                self.reason = f"at {where!r} — Back would raise 'Exit Game?' here"
                logger.warning(f"[barter_runner] {self.reason}")
                return None
            if self._pending is None:
                self._pending = ClearOfTheVillage()
                logger.info(f"[barter_runner] leaving the village from {where!r}")
            return self._pending

        if self.status == NEED_PANEL:
            if self._pending is None:
                self._pending = ReadBarterPanel(good=self.good, village=self.village)
                logger.info(f"[barter_runner] asking the panel about {self.good!r}")
            return self._pending

        if self.status == NEED_HOLD:
            if self._pending is None:
                self._pending = ReadHold()
                logger.info("[barter_runner] asking for the hold")
            return self._pending

        return None

    # ── updating task status ─────────────────────────────────────────────────

    def _absorb(self, result: Any) -> None:
        """Read an activity's result into task status. Never a screen fact — a task fact."""
        if isinstance(self._pending, ReadBarterPanel):
            status = getattr(result, "status", None)
            # WORKING IS NOT AN ANSWER, AND IT IS NOT A REFUSAL EITHER (live 2026-09-04).
            #
            # An activity does ONE action per tick and says WORKING while it is still in the
            # context — that is the dispatcher's own contract. The barter panel opens with
            # NOTHING selected, so the first tick selects the good and reports
            # `{'did': 'selected Birch Tree'}`; the panel is readable on the NEXT one.
            #
            # Treating that first in-progress tick as failure abandoned a barter that was
            # working: at Svear the good was selected and the panel then read
            # `good=Birch Tree out=564 materials=[...]` — an answer, one tick too late to be
            # heard. The runner had already said "the panel did not answer here", the command
            # replanned the WHOLE mission (re-gathering three ports whose materials were
            # already aboard), and it died on `sea cannot start ENTER_BUILDING` because a
            # village has no market.
            #
            # Only a TERMINAL status decides. Keep the order pending and ask again.
            if still_working(result):
                logger.info("[barter_runner] the panel is still being opened "
                            f"({(getattr(result, 'observed', None) or {}).get('did')!r}) — "
                            "asking again rather than calling it a refusal")
                return
            self._pending = None
            if status == FINISHED:
                self.panel = (getattr(result, "observed", None) or {}).get("panel")
                self.status = HAVE_PANEL
            else:
                # NOT A FAILURE — a fall-through. The panel not being readable here means the
                # ordinary check-and-plan path is the one to take, which is exactly what the
                # probe exists to decide.
                self.status = FAILED
                self.reason = getattr(result, "detail", "") or "the panel did not read here"
            return
        if isinstance(self._pending, ReadHold):
            self._absorb_hold(result)
            return
        if not isinstance(self._pending, RemoteCheck):
            return

        observed = getattr(result, "observed", None) or {}
        status = getattr(result, "status", None)

        if status == FINISHED:
            self._pending = None
            self.trades = list(observed.get("trades") or [])
            self.base = dict(observed.get("base") or {})
            self.status = HAVE_RECIPE
            logger.info(f"[barter_runner] {self.village} reads {len(self.trades)} trade(s)")
            return

        if status == BLOCKED and observed.get("partial"):
            # A PARTIAL READ IS NOT A RECIPE. Materials are invariant, so fewer than the
            # village actually trades means the read fell short — and planning from it is how
            # a fleet reached Svear with two of three materials and could not barter at all.
            # The activity already refuses to certify it; the task runner refuses to use it.
            self._pending = None
            self.status = FAILED
            self.reason = (f"the trade list at {self.village} did not read completely — "
                           f"{getattr(result, 'detail', '') or 'partial'}")
            logger.warning(f"[barter_runner] {self.reason}")
            return

        if status == BLOCKED:
            self._pending = None
            self.status = FAILED
            self.reason = getattr(result, "detail", "") or f"could not read {self.village}"

    def _absorb_hold(self, result: Any) -> None:
        """The two numbers, or a refusal to guess.

        NEITHER GUESS IS SAFE. Assuming an empty hold over-plans the gather; assuming a full
        one abandons a good mission. So an unread hold is a FAILED task, not a default.
        """
        observed = getattr(result, "observed", None) or {}
        status = getattr(result, "status", None)

        if status == FINISHED:
            self._pending = None
            self.capacity = observed.get("cargo_capacity")
            self.used = observed.get("cargo_used")
            self.status = HAVE_HOLD
            logger.info(f"[barter_runner] the hold reads {self.used}/{self.capacity}")
            return

        if status == BLOCKED:
            self._pending = None
            self.status = FAILED
            self.reason = (observed.get("reason")
                           or getattr(result, "detail", "") or "the hold did not read")

    # ── what the mission asks of the reading ─────────────────────────────────

    def trade_for(self, good: str) -> Optional[Any]:
        """The village's trade row for `good`, or None."""
        want = _norm(good)
        for t in self.trades:
            if _norm(getattr(t, "good", "")) == want:
                return t
        return None

    @property
    def barters_used(self) -> Optional[int]:
        return self.base.get("barters_used")

    @property
    def barters_total(self) -> Optional[int]:
        return self.base.get("barters_total")

    @property
    def rounds_remaining(self) -> Optional[int]:
        """Rounds the day still allows, or None if the Base tab was unreadable."""
        used, total = self.base.get("barters_used"), self.base.get("barters_total")
        if used is None or total is None:
            return None
        # The day's CEILING, not the arrival total: every amity grade opens one more barter,
        # and amity climbs while trading. Planning on the arrival number guarantees coming up
        # short. See VillageCheck.rounds_remaining, which this mirrors deliberately.
        ceiling = max(int(total), MAX_DAILY_BARTER_ROUNDS)
        return max(0, ceiling - int(used))


# Where the world map can be opened from, and where Back must never be pressed. The same
# facts `brain/intents.py` routes on — asserted equal in the tests, so a change to one that
# is not made to the other is caught rather than discovered by a fleet that quit the game.
_CLEAR_OF_THE_VILLAGE = ("sea", "sea_cinematic", "world_map", "port_overworld", "port_map")
_BACK_WOULD_QUIT = ("main_menu",)


def _norm(name: str) -> str:
    return "".join((name or "").lower().split())
