# memory/barter_kb.py
# Barter / village knowledge base — the shared substrate for the barter-nav domain.
#
# The KB is written by AI (the seasonal ingestion compiler, #28) and read by BOTH
# AI and the deterministic solvers (#19/#20/#22): solvers pull exact numbers
# (ratios, source ports, amity, rounds) for guaranteed math; AI pulls structure for
# the fuzzy strategic calls (which good/village/sell-port).  See the blueprint
# memory project_barter_kb_blueprint_2026-08-14.
#
# This module owns ONLY the data model + persistence.  Perception (populating these
# from the game) lives in the readers #12/#13/#14; per-good buy/sell PRICES + stock
# stay in memory/market_kb.py (not duplicated here).
#
# Storage (JSON, matching market_kb.py conventions):
#   memory/knowledge/barter/recipes.json      {good: BarterRecipe}   (seasonal)
#   memory/knowledge/barter/villages.json     {village: Village}
#   memory/knowledge/barter/preferences.json  {port: PortPreference}
#   memory/knowledge/state/fleet_state.json   FleetState (player-global)

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from loguru import logger

_BARTER_DIR = Path("memory/knowledge/barter")
_STATE_DIR = Path("memory/knowledge/state")

# Amity grades, low → high.  Each grade compounds (more barter rounds, higher
# per-round output, more goods unlocked).  Order is authoritative for gating
# (a recipe's amity_min is met when the village is at that grade OR higher).
AMITY_GRADES = ("Neutral", "Favorable", "Trusting", "Friendly")


def amity_at_least(current: Optional[str], required: Optional[str]) -> bool:
    """True if `current` amity grade meets or exceeds `required`.  Unknown current
    grade → False (don't assume eligibility); no requirement → True."""
    if not required:
        return True
    if current not in AMITY_GRADES or required not in AMITY_GRADES:
        return current == required
    return AMITY_GRADES.index(current) >= AMITY_GRADES.index(required)


# ── Record types ─────────────────────────────────────────────────────────────

@dataclass
class Preconditions:
    """Gates on whether the player can make a recipe at all."""
    amity_min: Optional[str] = None          # e.g. "Friendly" — village amity grade
    trade_level_min: Optional[int] = None    # merchant Trade skill level
    negotiation_expertise_min: Optional[int] = None
    guild: Optional[str] = None              # required guild membership, if any
    event: Optional[str] = None              # required active event/window, if any

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Preconditions":
        return cls(**{k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__})


@dataclass
class RecipeInput:
    """One material a recipe consumes, with where to source it."""
    material: str
    ratio: int = 1                           # nominal units consumed per round;
                                             # the game varies this daily (Stock Status)
    source_ports: list[str] = field(default_factory=list)  # from the panel's location pin
    # A VILLAGE IS A SOURCE TOO (user, 2026-09-08: "village is a legitimate source, some
    # materials are available at both villages and ports, like diamond. And some only at
    # villages or ports"). Kept apart from the ports because they are reached differently —
    # a port is a market to buy at, a village is a barter, and the world map lists them on
    # different tabs. Flattening the two is what sent a fleet looking for 'Chinook' in the
    # port list twenty times. Production sources are dropped at the reader: a crafting
    # recipe is not a place anything can sail to.
    source_villages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RecipeInput":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class BarterRecipe:
    """A seasonal barter recipe: output good ← input materials, offered at villages.

    output_per_round is amity-scaled ({grade: qty}) because per-round yield rises
    with amity (observed 709 Neutral → 744 Favorable → 813 Friendly for Camas)."""
    good: str
    season: str = ""                         # season label; recipes are re-scanned per season (#16)
    inputs: list[RecipeInput] = field(default_factory=list)   # UNION across villages
    village_inputs: dict = field(default_factory=dict)   # {village slug: [RecipeInput]}
    villages: list[str] = field(default_factory=list)      # villages offering this recipe
    output_per_round: dict[str, int] = field(default_factory=dict)  # {amity_grade: qty}
    preconditions: Preconditions = field(default_factory=Preconditions)
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["inputs"] = [i.to_dict() for i in self.inputs]
        d["village_inputs"] = {v: [i.to_dict() for i in ins]
                               for v, ins in (self.village_inputs or {}).items()}
        d["preconditions"] = self.preconditions.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BarterRecipe":
        inputs = [RecipeInput.from_dict(i) for i in d.get("inputs", [])]
        village_inputs = {v: [RecipeInput.from_dict(i) for i in ins]
                          for v, ins in (d.get("village_inputs") or {}).items()}
        pre = Preconditions.from_dict(d.get("preconditions", {}))
        obj = cls(**{k: v for k, v in d.items()
                     if k in cls.__dataclass_fields__
                     and k not in ("inputs", "village_inputs", "preconditions")})
        obj.inputs = inputs
        obj.village_inputs = village_inputs
        obj.preconditions = pre
        return obj

    def inputs_for(self, village: Optional[str]) -> list:
        """The materials THIS VILLAGE wants for the good.

        The same good takes different materials at different villages — Box of Nutmeg is
        Ebony+Coral+Textiles at Melanesian and Khmer, but Ebony+Coral+Dhaka Muslin at
        Malay (user 2026-08-21; the KB notes said so long before the model could).
        `inputs` is the UNION across villages and is only a fallback: planning a gather
        from it would buy a material this village does not accept."""
        if village:
            per = (self.village_inputs or {}).get(_slug(village))
            if per:
                return per
        return self.inputs


@dataclass
class Village:
    """A barter village's observed state (not the seasonal recipes, which live in
    BarterRecipe).  amity gates rounds + which goods unlock; rounds_remaining==0 is
    what shows the red 'Cannot Exchange' note, NOT amity directly."""
    name: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    amity: Optional[str] = None              # current amity grade
    amity_points: Optional[int] = None       # numeric amity (e.g. 744)
    barter_rounds_total: Optional[int] = None
    barter_rounds_remaining: Optional[int] = None
    eligible_goods: list[str] = field(default_factory=list)  # currently barterable
    locked_goods: list[str] = field(default_factory=list)    # shown but amity-locked

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Village":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class PortPreference:
    """A port's seasonal sell preferences BY CATEGORY (always readable on the
    Preference tab, regardless of trade-level visibility).  Actual per-good prices
    live in market_kb; this is the category markup that drives sell-port choice."""
    port: str
    season: str = ""
    preferences: dict[str, int] = field(default_factory=dict)  # {category: markup_pct}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PortPreference":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class FleetState:
    """Player-global state that gates barter/trade + drives growth strategy.  Per-
    village amity lives on the Village records; this holds merchant-wide skills,
    guild, currencies, and company progression (the LV bar on every overworld)."""
    trade_level: Optional[int] = None
    negotiation_expertise: Optional[int] = None
    guild: Optional[str] = None
    currencies: dict[str, int] = field(default_factory=dict)  # {"ducat","blue_gem","red_gem",...}
    gift_tokens: Optional[int] = None
    company_level: Optional[int] = None          # "LV 92" bottom-centre bar
    company_xp_pct: Optional[float] = None        # "68.81%" progress to next level

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FleetState":
        return cls(**{k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__})


# ── Storage ──────────────────────────────────────────────────────────────────

def _slug(s: str) -> str:
    return s.lower().replace(" ", "_").replace("/", "_")


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        logger.warning(f"Failed to load barter KB {path}: {exc}")
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# Recipes — keyed by good slug -------------------------------------------------

_RECIPES_PATH = _BARTER_DIR / "recipes.json"


def save_recipe(recipe: BarterRecipe) -> None:
    data = _load_json(_RECIPES_PATH)
    data[_slug(recipe.good)] = recipe.to_dict()
    _write_json(_RECIPES_PATH, data)
    logger.info(f"Barter recipe saved: {recipe.good} ({len(recipe.inputs)} inputs)")


def load_recipe(good: str) -> Optional[BarterRecipe]:
    d = _load_json(_RECIPES_PATH).get(_slug(good))
    return BarterRecipe.from_dict(d) if d else None


def all_recipes() -> list[BarterRecipe]:
    return [BarterRecipe.from_dict(d) for d in _load_json(_RECIPES_PATH).values()]


# Villages — keyed by village slug --------------------------------------------

_VILLAGES_PATH = _BARTER_DIR / "villages.json"


def save_village(village: Village) -> None:
    data = _load_json(_VILLAGES_PATH)
    data[_slug(village.name)] = village.to_dict()
    _write_json(_VILLAGES_PATH, data)
    logger.info(f"Village saved: {village.name} (amity={village.amity})")


def load_village(name: str) -> Optional[Village]:
    d = _load_json(_VILLAGES_PATH).get(_slug(name))
    return Village.from_dict(d) if d else None


# Port preferences — keyed by port slug ---------------------------------------

_PREFERENCES_PATH = _BARTER_DIR / "preferences.json"


def save_preference(pref: PortPreference) -> None:
    data = _load_json(_PREFERENCES_PATH)
    data[_slug(pref.port)] = pref.to_dict()
    _write_json(_PREFERENCES_PATH, data)


def load_preference(port: str) -> Optional[PortPreference]:
    d = _load_json(_PREFERENCES_PATH).get(_slug(port))
    return PortPreference.from_dict(d) if d else None


def all_preferences() -> list[PortPreference]:
    return [PortPreference.from_dict(d) for d in _load_json(_PREFERENCES_PATH).values()]


# Fleet state — single record --------------------------------------------------

_FLEET_STATE_PATH = _STATE_DIR / "fleet_state.json"


def save_fleet_state(state: FleetState) -> None:
    _write_json(_FLEET_STATE_PATH, state.to_dict())


def load_fleet_state() -> FleetState:
    """Fleet state always exists conceptually; returns an empty FleetState if unseen."""
    return FleetState.from_dict(_load_json(_FLEET_STATE_PATH))
