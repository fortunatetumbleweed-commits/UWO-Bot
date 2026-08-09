"""Skill registry — capabilities + the many-to-many function↔building map.

A **skill** is one capability (recruit_crew, repair, sell, …). Functions and
buildings are MANY-TO-MANY: a function is offered at several buildings (with
different efficiency), and a building offers several functions. The registry
stores both directions and does **cost-aware location selection** — the top rule
is "prefer where you already are."

This module is the location model (the crew-shortage fix, unit-testable now);
wiring each skill's actual run/precondition/success-check to the existing
primitives (`brain/goals/*`, `market_actions`, `navigate_to_building`) is the
next step. See docs/bot_architecture_layers.md (Layer 3).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class SkillLocation:
    building:   str
    efficiency: float = 1.0     # higher = faster/better for this function here
    note:       str = ""        # e.g. "limited numbers" (Village crew)


@dataclass
class Skill:
    name:         str                          # "recruit_crew"
    available_at: List[SkillLocation] = field(default_factory=list)
    # run / precondition / success_check callables get wired to real primitives
    # later; the registry's value now is the location model.


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: Dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    # ── the two directions of the many-to-many map ───────────────────────────
    def buildings_for(self, function: str) -> List[str]:
        """Where can I do X? — ordered best-efficiency first."""
        sk = self._skills.get(function)
        if not sk:
            return []
        return [l.building for l in sorted(sk.available_at,
                                           key=lambda l: -l.efficiency)]

    def functions_at(self, building: str) -> List[str]:
        """What can I do here? — the affordance view of a building."""
        b = (building or "").lower()
        return [name for name, sk in self._skills.items()
                if any(l.building.lower() == b for l in sk.available_at)]

    # ── cost-aware location selection ────────────────────────────────────────
    def choose_location(
        self, function: str, current_building: Optional[str] = None,
    ) -> Optional[str]:
        """Pick where to perform `function`.

        1. **Prefer where you already are** — if the current building offers it,
           do it there (zero travel). This is the crew-shortage fix: at the
           Harbor needing crew → recruit at the Harbor, don't leave for the Inn.
        2. Otherwise pick the highest-efficiency location.
        (Travel-cost ranking can layer on once positions/distances are wired.)
        """
        sk = self._skills.get(function)
        if not sk or not sk.available_at:
            return None
        if current_building:
            cb = current_building.lower()
            if any(l.building.lower() == cb for l in sk.available_at):
                return current_building
        return max(sk.available_at, key=lambda l: l.efficiency).building


def default_registry() -> SkillRegistry:
    """Seed the known function↔building map (from the game-menu schema).

    Efficiency is a first cut: e.g. Harbor recruit is faster than Inn/Village.
    This map should eventually be data-driven from the affordance KB, learned
    as the bot plays.
    """
    r = SkillRegistry()
    r.register(Skill("recruit_crew", [
        SkillLocation("Harbor", 1.0),
        SkillLocation("Inn", 0.7),
        SkillLocation("Village", 0.4, "limited numbers"),
    ]))
    r.register(Skill("repair", [
        SkillLocation("Shipyard", 1.0),
        SkillLocation("Harbor", 0.8),
    ]))
    r.register(Skill("restore_ship", [SkillLocation("Shipyard", 1.0)]))
    r.register(Skill("resupply", [SkillLocation("Harbor", 1.0)]))
    r.register(Skill("buy_goods", [SkillLocation("Market", 1.0)]))
    r.register(Skill("sell_goods", [SkillLocation("Market", 1.0)]))
    r.register(Skill("hire_mate", [SkillLocation("Inn", 1.0)]))
    return r
