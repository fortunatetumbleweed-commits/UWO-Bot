"""The task runner for one sea leg. PASSIVE — it is consulted, it does not drive.

Guiding Principle #7. It answers one question per consult: given what the sea just reported,
what should the dispatcher do next?

  under way          -> keep the same goal; the voyage is time passing
  not moving         -> ask for the destination to be set again
  ashore             -> nothing more to ask

WHY THIS EXISTS AT ALL. `SeaActivity` can tell that the ship is not moving — the ETA stops
falling — but not what to do about it, because the answer is the destination, and that is the
mission's. A fixed `run_goal(ArriveAshore(...))` had nowhere to put that decision: a BLOCKED
result simply ended the leg with "not moving" and the fleet sat there. This is the smallest
thing that can hear the report and act on it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from brain.activities.sea import ArriveAshore
from brain.activities.world_map import ChooseDestination
from brain.dispatcher import BLOCKED, FINISHED, UNRECOGNISED, WORKING

UNDER_WAY = "under-way"
ARRIVED = "arrived"
ADRIFT = "adrift"          # not moving, and no destination to re-set


@dataclass
class VoyageRunner:
    """One sea leg. `destination` is what to re-select if the ship turns out to be adrift."""

    what: str = "the voyage"
    destination: Optional[str] = None
    kind: str = "port"                     # 'port', 'village' or 'route'

    status: str = UNDER_WAY
    reason: str = ""
    port: Optional[str] = None

    # How many times a destination may be re-set before giving up. A course that will not
    # take is not fixed by asking a third time, and re-selecting forever is the sub-loop this
    # architecture removes.
    _MAX_RESETS: int = 2
    _resets: int = field(default=0)
    _pending: Any = None

    def next_goal(self, result: Optional[Any], state: Any) -> Optional[Any]:
        if result is not None:
            done = self._absorb(result)
            if done:
                return None

        if isinstance(self._pending, ChooseDestination):
            return self._pending

        self._pending = ArriveAshore(self.what)
        return self._pending

    def _absorb(self, result: Any) -> bool:
        """Update status from what came back. True when there is nothing more to ask."""
        status = getattr(result, "status", None)
        observed = getattr(result, "observed", None) or {}

        # ONLY A TERMINAL RESULT ENDS AN ATTEMPT — see the note in brain/sail_runner.py.
        # UNRECOGNISED is the dispatcher routing, not a refusal.
        if status in (WORKING, UNRECOGNISED):
            return False

        # WHICH GOAL PRODUCED THIS? A result means nothing on its own — FINISHED from
        # `ChooseDestination` says the course was set, and reading it as "arrived" ends the
        # voyage the moment it is rescued. So the outstanding goal is consulted before the
        # status is.
        if isinstance(self._pending, ChooseDestination):
            # The course was re-set (or refused). Either way, go back to watching: whether it
            # took is a question for the ETA, not for the tap that was just made.
            self._pending = None
            return False

        if status == FINISHED:
            # ASHORE. `PortActivity` (or `VillageActivity`) says so the moment the state
            # stops being sea — whichever owns the screen the voyage ended on; the
            # committed destination was set when the departure was, and repeating it here
            # would be a second copy of a decision already made.
            self.status, self.port = ARRIVED, observed.get("port")
            self._pending = None
            return True

        if status == BLOCKED and observed.get("moving") is False:
            if not self.destination:
                self.status = ADRIFT
                self.reason = (f"{self.what} is not moving and no destination is known — "
                               f"the course cannot be re-set from here")
                logger.warning(f"[voyage] {self.reason}")
                self._pending = None
                return True
            if self._resets >= self._MAX_RESETS:
                self.status = ADRIFT
                self.reason = (f"{self.what} did not get under way after {self._resets} "
                               f"attempts to set {self.destination!r}")
                logger.warning(f"[voyage] {self.reason}")
                self._pending = None
                return True
            self._resets += 1
            self._pending = ChooseDestination(where=self.destination, kind=self.kind)
            logger.info(f"[voyage] {self.what} is not moving — setting the course to "
                        f"{self.destination!r} again ({self._resets}/{self._MAX_RESETS})")
            return False

        if status == BLOCKED:
            # Some other refusal — reported, not interpreted. Supply is the usual one, and
            # what to do about it needs the mission.
            self.status, self.reason = ADRIFT, getattr(result, "detail", "") or "blocked"
            self._pending = None
            return True

        return False
