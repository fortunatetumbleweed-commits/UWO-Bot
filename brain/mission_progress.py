"""How far the current mission has got, so finished steps are never re-run.

Every run before 2026-08-22 restarted from step one. That is why a fleet standing IN
Melanesian Village — its own destination, materials aboard — opened the world map to run a
REMOTE check on the village it was inside, could not open the map from a village, pressed
Back three times, and ended up at sea. Nothing in the bot could say "that part is done".

Once the fleet has committed to the voyage, the gathering questions are settled: the
materials are aboard or they are not, and no amount of re-checking at sea changes it (user
2026-08-22: "if it has started sailing to the village, then no check for material anymore,
just arrive and barter"). Re-deriving them is not merely wasted time — it is what walks the
fleet out of the place it spent a voyage to reach.

Phases are ordered. `at_least(phase)` asks whether a step is behind us; steps guard
themselves with it rather than each caller re-deciding.

Persisted with a timestamp (see `memory.observed_facts`) so a mission survives a restart
mid-voyage, and so a stale record can be recognised as stale rather than trusted blindly.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

from memory.observed_facts import forget, recall, remember

_KEY = "mission_progress"

# The task has THREE phases, and each one closes a question for good (user, 2026-08-22):
#
#   planning   — remote-check the village ONCE and size the materials needed.
#                Leaving this phase ends remote checking for the rest of the task.
#   gathering  — buy what is missing, sell the surplus.
#                Leaving this phase ends cargo checking for the rest of the task.
#   bartering  — sail to the village and barter. No village check, no cargo check.
#                Leaving this phase means the goods are aboard; the barter is not re-run.
#   sailing_route — take the tail: run the planned route (or sail to the sell port) and
#                sell the output. Nothing upstream is revisited.
#
# Ordered: later phases imply every earlier one.
PHASES = ("planning", "gathering", "bartering", "sailing_route", "done")

# A mission left half-finished for this long is not evidence about now — the ship may have
# been sailed by hand, the day may have rolled over, the barter window may have re-rolled.
_MAX_AGE_S = 6 * 3600


def start(village: str, good: str, rounds: int, *,
          recipe: Optional[dict] = None, rounds_remaining: Optional[int] = None) -> None:
    """Begin tracking a mission. Replaces any earlier one.

    `recipe` is pinned HERE and reused for the rest of the task. The village's quantities
    re-roll roughly every six hours, and a mission that re-reads them every run pays a trip
    to the world map each time — which is what sailed the fleet out of the village it had
    already reached. A stale ratio just means less output, and that trade is worth making.
    """
    remember(_KEY, {"village": village, "good": good, "rounds": int(rounds),
                    "recipe": recipe, "rounds_remaining": rounds_remaining,
                    "phase": "planning"})


def advance(phase: str, **extra) -> None:
    """Record that `phase` has been reached. Never moves backwards."""
    if phase not in PHASES:
        raise ValueError(f"unknown mission phase {phase!r}")
    cur = current()
    if cur is None:
        logger.debug(f"[progress] no mission in flight — not recording {phase!r}")
        return
    if PHASES.index(phase) <= PHASES.index(cur.get("phase", PHASES[0])):
        return                                  # already at or past it
    # `current()` decorates its return with a COMPUTED age; writing that straight back would
    # persist a derived value as if it were data, and the stored age would then be wrong the
    # moment it was written.
    cur.pop("age_s", None)
    remember(_KEY, {**cur, **extra, "phase": phase})
    logger.info(f"[progress] {cur.get('village')}: {phase}")


def current() -> Optional[dict]:
    """The mission in flight, or None when there is none recent enough to trust."""
    seen = recall(_KEY, max_age_s=_MAX_AGE_S)
    if seen is None:
        return None
    value, age = seen
    if not isinstance(value, dict) or "phase" not in value:
        return None
    return {**value, "age_s": age}


def at_least(phase: str, *, village: str = "") -> bool:
    """True when the mission in flight has reached `phase` (or beyond).

    `village` guards against answering about a DIFFERENT mission: progress toward one
    village says nothing about another.
    """
    if phase not in PHASES:
        raise ValueError(f"unknown mission phase {phase!r}")
    cur = current()
    if cur is None:
        return False
    if village and (cur.get("village") or "").lower() != village.lower():
        return False
    return PHASES.index(cur["phase"]) >= PHASES.index(phase)


def record_rounds(n: int) -> None:
    """Add `n` committed barter rounds to the mission's running total.

    The total is what distinguishes "this mission has bartered and the materials are now
    spent" from "the materials never arrived". A single run cannot tell them apart — it sees
    only an empty panel either way — and the difference decides whether the tail should run
    or the mission should report a failure (live 2026-08-23: a mission that HAD bartered
    three times reported "bartered 0 round(s)" on the run that found nothing left, and the
    route never ran).
    """
    cur = current()
    if cur is None or n <= 0:
        return
    cur.pop("age_s", None)
    remember(_KEY, {**cur, "committed_total": int(cur.get("committed_total") or 0) + int(n)})


def committed_total() -> int:
    """How many barter rounds this mission has committed, across runs."""
    return int((current() or {}).get("committed_total") or 0)


def finish() -> None:
    """Mission over — done, abandoned, or superseded."""
    forget(_KEY)
