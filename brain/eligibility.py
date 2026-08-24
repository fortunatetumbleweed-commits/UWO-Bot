"""Barter eligibility filter (#18) — can we barter this recipe at this village NOW?

Combines the recipe's static preconditions (amity grade, trade level, negotiation
expertise, guild, active event) with the village's live state (good locked?,
rounds remaining?).  Pure over the KB records; the strategy layer (#29) uses it to
prune the candidate set before ranking.

KEY game fact: the red "Cannot Exchange" note ⇔ rounds_remaining == 0, NOT amity
directly — higher amity UNLOCKS more rounds/goods, but a good you're amity-eligible
for is still un-barterable when the day's rounds are used up.  Both gates apply.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from memory.barter_kb import BarterRecipe, Village, FleetState, amity_at_least


@dataclass
class Eligibility:
    good: str
    eligible: bool
    reasons: list = field(default_factory=list)   # blocking reasons (empty ⇒ eligible)


def evaluate_eligibility(recipe: BarterRecipe, village: Village, fleet: FleetState,
                         active_events: Sequence[str] = ()) -> Eligibility:
    """Return an Eligibility verdict for one recipe at one village."""
    reasons: list = []
    pre = recipe.preconditions

    if not amity_at_least(village.amity, pre.amity_min):
        reasons.append(f"amity {village.amity!r} < required {pre.amity_min!r}")

    if pre.trade_level_min is not None and (
            fleet.trade_level is None or fleet.trade_level < pre.trade_level_min):
        reasons.append(f"trade_level {fleet.trade_level} < {pre.trade_level_min}")

    if pre.negotiation_expertise_min is not None and (
            fleet.negotiation_expertise is None
            or fleet.negotiation_expertise < pre.negotiation_expertise_min):
        reasons.append(
            f"negotiation_expertise {fleet.negotiation_expertise} < {pre.negotiation_expertise_min}")

    if pre.guild and fleet.guild != pre.guild:
        reasons.append(f"guild {fleet.guild!r} != required {pre.guild!r}")

    if pre.event and pre.event not in active_events:
        reasons.append(f"event {pre.event!r} not active")

    if recipe.good in (village.locked_goods or []):
        reasons.append("good locked at village")

    # rounds_remaining == 0 → the red 'Cannot Exchange' note (independent of amity).
    if village.barter_rounds_remaining is not None and village.barter_rounds_remaining <= 0:
        reasons.append("no barter rounds remaining")

    return Eligibility(recipe.good, not reasons, reasons)


def eligible_recipes(recipes: Sequence[BarterRecipe], village: Village,
                     fleet: FleetState, active_events: Sequence[str] = ()) -> list:
    """The subset of `recipes` currently barterable at `village`."""
    return [r for r in recipes
            if evaluate_eligibility(r, village, fleet, active_events).eligible]
