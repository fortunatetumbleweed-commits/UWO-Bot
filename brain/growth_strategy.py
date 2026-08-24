"""Company-growth strategy + ducat guardrail (#30) — the top-level objective brain.

Goal (user 2026-08-14): maximize COMPANY XP — every sail / trade / explore / combat
earns company experience — subject to a HARD guardrail: never run out of free
cash-flow ducats. Sits ABOVE the barter/trade/explore strategies: it picks WHICH
activity to pursue next; the sub-strategies (e.g. #29) fill in the details.

Policy:
  • If ducats are already below the floor → RECOVER: pick the most ducat-positive
    activity (earn cash before anything else).
  • Otherwise → maximize estimated company XP among activities that keep ducats at
    or above the floor after their (estimated) ducat cost. If none stay above the
    floor, recover instead.

Uses vision.hud_readers.read_company_level (#14) for XP progress + read_currencies /
read_ducats for the ducat balance. Extensible later to blue-gem / token goals.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass
class Activity:
    name: str                 # 'barter' | 'trade' | 'explore' | 'combat' | ...
    est_xp: float             # estimated company XP gained
    est_ducat_delta: float    # estimated net ducats (earn +, spend −)


@dataclass
class GrowthDecision:
    activity: Activity
    reason: str


def guardrail_ok(activity: Activity, ducats: float, ducat_floor: float) -> bool:
    """True if doing `activity` keeps ducats at/above the floor afterwards."""
    return ducats + activity.est_ducat_delta >= ducat_floor


def choose_growth_activity(activities: Sequence[Activity], ducats: float,
                           ducat_floor: float) -> Optional[GrowthDecision]:
    """Pick the next activity: maximize company XP subject to the ducat guardrail;
    recover cash when below the floor or when nothing keeps us above it."""
    if not activities:
        return None

    most_positive = max(activities, key=lambda a: a.est_ducat_delta)

    if ducats < ducat_floor:
        return GrowthDecision(
            most_positive,
            f"ducats {ducats:.0f} below floor {ducat_floor:.0f} — recover cash")

    safe = [a for a in activities if guardrail_ok(a, ducats, ducat_floor)]
    if not safe:
        return GrowthDecision(
            most_positive,
            "no XP activity keeps ducats above the floor — recover cash")

    best = max(safe, key=lambda a: a.est_xp)
    return GrowthDecision(best, f"maximize company XP within the ducat guardrail")
