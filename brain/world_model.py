"""World model — the bot's belief about the Company (Company → Fleets → Ships).

The situational-awareness layer: the things perception (`PerceivedState`) can't
read off a single screen — company currencies, fleet position/supply/crew/cargo,
ship life, discovered ports. The reasoning layer reads this to decide what to do;
skill location-selection reads `current_building`; monitors read supply/crew/ship
life; and `to_prompt()` serializes it into the reasoning-layer LLM context.

See docs/bot_architecture_layers.md (Layer 2 — "World model = the Company
object"). This is the data model + departure diagnosis + serializer; wiring an
updater from `PerceivedState`/observations is the next step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ── currency spending discipline ──────────────────────────────────────────────
# Free = auto-spendable by the bot. Confirm = REAL MONEY, always ask the user
# before spending (the standing red-gem rule).
FREE_CURRENCIES    = frozenset({"ducat", "blue_gem", "guild_coin"})
CONFIRM_CURRENCIES = frozenset({"red_gem"})

# A ship with life below this cannot sail — and one blocked ship blocks the fleet.
SHIP_LIFE_MIN_TO_SAIL = 20


@dataclass
class Ship:
    name: str = ""
    life: int = 100                 # 0–100 durability; drops while sailing

    @property
    def can_sail(self) -> bool:
        return self.life >= SHIP_LIFE_MIN_TO_SAIL


@dataclass
class Fleet:
    location:         str = "unknown"           # port name | "at_sea"
    current_building: Optional[str] = None      # which building we're inside, if any
    supply_days:      Optional[int] = None
    crew_current:     Optional[int] = None
    crew_capacity:    Optional[int] = None
    cargo:            dict = field(default_factory=dict)   # good -> qty
    cargo_used:       Optional[int] = None
    cargo_capacity:   Optional[int] = None
    ships:            List[Ship] = field(default_factory=list)

    @property
    def crew_short(self) -> bool:
        if self.crew_current is None or self.crew_capacity is None:
            return False
        return self.crew_current < self.crew_capacity

    @property
    def blocked_ships(self) -> List[Ship]:
        return [s for s in self.ships if not s.can_sail]

    def can_depart(self) -> Tuple[bool, List[str]]:
        """The departure precondition, as READABLE facts.

        Returns (ok, reasons) — `reasons` is the list of concrete blockers
        ("crew short (42/60)", "ship life<20: Sloop"). This is what turns the
        opaque "fleet cannot depart" into a diagnosis the reasoning layer / a
        monitor can act on. Unknown fields are not treated as blockers.
        """
        reasons: List[str] = []
        blocked = self.blocked_ships
        if blocked:
            reasons.append(
                f"ship life < {SHIP_LIFE_MIN_TO_SAIL}: "
                + ", ".join(s.name or "?" for s in blocked)
            )
        if self.crew_short:
            reasons.append(f"crew short ({self.crew_current}/{self.crew_capacity})")
        if self.supply_days is not None and self.supply_days < 1:
            reasons.append("out of supplies")
        return (not reasons, reasons)


@dataclass
class WorldModel:
    """The Company — root of the belief state."""
    currencies:       dict = field(default_factory=dict)   # name -> amount
    fleets:           List[Fleet] = field(default_factory=list)
    active_fleet_idx: int = 0
    discovered_ports: List[str] = field(default_factory=list)

    @property
    def fleet(self) -> Optional[Fleet]:
        """The active fleet (the one the bot is driving)."""
        if 0 <= self.active_fleet_idx < len(self.fleets):
            return self.fleets[self.active_fleet_idx]
        return None

    def needs_confirmation_to_spend(self, currency: str) -> bool:
        """True for real-money currencies (red gem) — never auto-spend these."""
        return (currency or "").lower() in CONFIRM_CURRENCIES

    def to_prompt(self) -> str:
        """Compact text serialization for the reasoning-layer LLM context.

        Deterministic and small — this is exactly what the LLM receives about the
        company/fleet/ship state, alongside the PerceivedState of the screen.
        """
        cur = "  ".join(f"{k}={v:,}" if isinstance(v, int) else f"{k}={v}"
                        for k, v in self.currencies.items()) or "(unknown)"
        lines = [f"COMPANY: {cur}"]
        f = self.fleet
        if f is None:
            lines.append("FLEET: (none)")
        else:
            where = f.location + (f", inside {f.current_building}"
                                  if f.current_building else "")
            crew = ("?" if f.crew_current is None
                    else f"{f.crew_current}/{f.crew_capacity}"
                         + (" SHORT" if f.crew_short else ""))
            cargo = ("?" if f.cargo_used is None
                     else f"{f.cargo_used}/{f.cargo_capacity}")
            supply = "?" if f.supply_days is None else f"{f.supply_days}d"
            lines.append(
                f"FLEET(active): at {where} | supply={supply} | "
                f"crew={crew} | cargo={cargo}"
            )
            if f.ships:
                ships = ", ".join(
                    f"{s.name or '?'}(life {s.life}"
                    + ("" if s.can_sail else " CANNOT SAIL") + ")"
                    for s in f.ships
                )
                lines.append(f"  ships: {ships}")
            ok, reasons = f.can_depart()
            unread = []
            if f.crew_current is None: unread.append("crew")
            if not f.ships:            unread.append("ship life")
            if f.supply_days is None:  unread.append("supply")
            if reasons:
                lines.append("  can_depart: NO — " + "; ".join(reasons))
            elif unread:
                # don't assert YES when the preconditions haven't been read
                lines.append(f"  can_depart: unknown (not read: {', '.join(unread)})")
            else:
                lines.append("  can_depart: YES")
        if self.discovered_ports:
            head = ", ".join(self.discovered_ports[:8])
            more = f", … (+{len(self.discovered_ports) - 8})" if len(self.discovered_ports) > 8 else ""
            lines.append(f"DISCOVERED PORTS: {head}{more}")
        return "\n".join(lines)
