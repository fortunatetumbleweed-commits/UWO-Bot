# memory/trade_priors.py
#
# Static game-knowledge priors used to estimate expected profit on any trade leg
# before the bot has enough real voyage data.
#
# Three signals, in decreasing reliability order:
#
#   1. Specialties   — goods that earn a bonus multiplier at distant ports.
#                      Reliable: the specialty system is hard-coded by the game.
#
#   2. Distance tier — trading across sea regions is more profitable than
#                      within one region.  Reliable: game design principle.
#
#   3. Port preferences — cultural/geographic demand biases.
#                      Approximate: guides which goods to bring, filters out
#                      zero-value trades (e.g. wine in Islamic cities).
#
# The function `leg_profit_score(from_port, to_port)` returns a float ≥ 0:
#   - higher = more likely to be profitable
#   - used as the "uninformed prior" edge weight in the route planner
#   - real observed ducats/hour overrides this once enough data exists
#
# All port names here are lowercase to match PortGraph normalisation.

from __future__ import annotations

# ── 1. Sea regions ─────────────────────────────────────────────────────────────
# Ports are grouped into sea regions.  Trade across regions earns more.
# Add ports as the bot discovers them — unknown ports default to "unknown".

SEA_REGIONS: dict[str, str] = {
    # North / West Europe
    "london":        "north_europe",
    "amsterdam":     "north_europe",
    "hamburg":       "north_europe",
    "antwerp":       "north_europe",
    "bristol":       "north_europe",
    "edinburgh":     "north_europe",

    # Iberian / West Mediterranean
    "lisboa":        "west_mediterranean",
    "lisbon":        "west_mediterranean",
    "seville":       "west_mediterranean",
    "cadiz":         "west_mediterranean",
    "barcelona":     "west_mediterranean",
    "marseille":     "west_mediterranean",
    "genoa":         "west_mediterranean",
    "venice":        "west_mediterranean",
    "naples":        "west_mediterranean",
    "palermo":       "west_mediterranean",

    # Atlantic Islands
    "las palmas":    "atlantic_islands",
    "funchal":       "atlantic_islands",
    "azores":        "atlantic_islands",
    "porto santo":   "atlantic_islands",
    "tenerife":      "atlantic_islands",

    # West Africa
    "dakar":         "west_africa",
    "elmina":        "west_africa",
    "luanda":        "west_africa",
    "cape verde":    "west_africa",
    "sao tome":      "west_africa",
    "douala":        "west_africa",

    # East Mediterranean / Middle East
    "istanbul":      "east_mediterranean",
    "alexandria":    "east_mediterranean",
    "beirut":        "east_mediterranean",
    "tripoli":       "east_mediterranean",
    "tunis":         "east_mediterranean",
    "algiers":       "east_mediterranean",
    "athens":        "east_mediterranean",

    # East Africa / Red Sea
    "aden":          "east_africa",
    "mombasa":       "east_africa",
    "mozambique":    "east_africa",
    "zanzibar":      "east_africa",
    "socotra":       "east_africa",

    # Caribbean / Americas
    "port royal":    "caribbean",
    "havana":        "caribbean",
    "cartagena":     "caribbean",
    "santo domingo": "caribbean",
    "veracruz":      "caribbean",
    "san juan":      "caribbean",
    "bridgetown":    "caribbean",

    # South America
    "recife":        "south_america",
    "salvador":      "south_america",
    "buenos aires":  "south_america",
    "lima":          "south_america",

    # Indian Ocean
    "calicut":       "indian_ocean",
    "goa":           "indian_ocean",
    "diu":           "indian_ocean",
    "cochin":        "indian_ocean",
    "ceylon":        "indian_ocean",
    "colombo":       "indian_ocean",
    "aceh":          "indian_ocean",
    "malacca":       "indian_ocean",
    "hormuz":        "indian_ocean",

    # East Asia
    "guangzhou":     "east_asia",
    "nagasaki":      "east_asia",
    "hangzhou":      "east_asia",
    "quanzhou":      "east_asia",
    "macau":         "east_asia",
    "manila":        "east_asia",

    # Pacific
    "guam":          "pacific",
    "cebu":          "pacific",
}

# Profit multiplier for cross-region vs within-region trade.
# Indexed by (from_region, to_region) — symmetric: both orderings are listed.
_SAME_REGION_MULT = 1.0

_CROSS_REGION_MULT: dict[frozenset, float] = {
    # Short hops — modest bonus
    frozenset({"north_europe",      "west_mediterranean"}): 1.4,
    frozenset({"north_europe",      "atlantic_islands"}):   1.3,
    frozenset({"west_mediterranean","atlantic_islands"}):   1.2,
    frozenset({"west_mediterranean","east_mediterranean"}): 1.3,
    frozenset({"east_mediterranean","east_africa"}):        1.5,

    # Medium hops
    frozenset({"north_europe",      "west_africa"}):        1.6,
    frozenset({"north_europe",      "caribbean"}):          1.8,
    frozenset({"west_mediterranean","west_africa"}):        1.5,
    frozenset({"west_mediterranean","caribbean"}):          1.7,
    frozenset({"atlantic_islands",  "caribbean"}):          1.5,
    frozenset({"atlantic_islands",  "west_africa"}):        1.4,
    frozenset({"west_africa",       "south_america"}):      1.6,
    frozenset({"caribbean",         "south_america"}):      1.3,

    # Long hops — big bonus
    frozenset({"north_europe",      "indian_ocean"}):       2.2,
    frozenset({"north_europe",      "east_asia"}):          2.5,
    frozenset({"west_mediterranean","indian_ocean"}):       2.0,
    frozenset({"west_mediterranean","east_asia"}):          2.3,
    frozenset({"caribbean",         "indian_ocean"}):       2.1,
    frozenset({"west_africa",       "indian_ocean"}):       1.8,
    frozenset({"indian_ocean",      "east_asia"}):          1.6,
    frozenset({"east_africa",       "indian_ocean"}):       1.4,
    frozenset({"indian_ocean",      "pacific"}):            1.8,
    frozenset({"east_asia",         "pacific"}):            1.5,
}


def cross_region_multiplier(port_a: str, port_b: str) -> float:
    """Return the expected profit multiplier for trading between two ports."""
    ra = SEA_REGIONS.get(port_a.lower(), "unknown")
    rb = SEA_REGIONS.get(port_b.lower(), "unknown")
    if ra == "unknown" or rb == "unknown":
        return 1.2   # unknown port — assume a small bonus (likely a new region)
    if ra == rb:
        return _SAME_REGION_MULT
    key = frozenset({ra, rb})
    return _CROSS_REGION_MULT.get(key, 1.5)   # default for unspecified cross-region pair


# ── 2. Specialties ─────────────────────────────────────────────────────────────
# Specialty goods earn a bonus multiplier at the destination.
# Value here is the estimated multiplier over base price when sold far away.
# These are confirmed UWO specialty rules.

SPECIALTY_GOODS: dict[str, list[str]] = {
    "north_europe":      ["wool", "cloth", "amber", "linen", "beer"],
    "west_mediterranean":["wine", "olive oil", "marble", "pottery", "cork"],
    "east_mediterranean":["cotton", "glass", "carpet"],
    "west_africa":       ["gold dust", "ivory", "palm oil", "ebony"],
    "east_africa":       ["myrrh", "frankincense", "tortoiseshell"],
    "caribbean":         ["sugar", "tobacco", "rum", "cotton", "logwood"],
    "south_america":     ["silver", "cochineal", "indigo", "cacao"],
    "indian_ocean":      ["pepper", "cinnamon", "ginger", "muslin", "indigo"],
    "east_asia":         ["tea", "porcelain", "silk", "lacquerware"],
    "pacific":           ["sea cucumber", "sandalwood"],
}

# Bonus multiplier for selling a specialty good outside its home region
SPECIALTY_SELL_BONUS = 1.5


def has_specialty_opportunity(from_port: str, to_port: str) -> bool:
    """True if the destination region can sell specialties from the origin region."""
    from_region = SEA_REGIONS.get(from_port.lower(), "unknown")
    to_region   = SEA_REGIONS.get(to_port.lower(), "unknown")
    if from_region == "unknown" or to_region == "unknown":
        return False
    return (
        from_region != to_region
        and bool(SPECIALTY_GOODS.get(from_region))
    )


# ── 3. Port preferences ────────────────────────────────────────────────────────
# Cultural and geographic demand biases.
# "forbidden" goods earn zero at this port — don't buy them to sell here.
# "preferred" good categories earn extra — prioritise loading them.

PORT_PREFERENCES: dict[str, dict] = {
    # Islamic ports — no alcohol, no pork products
    "aceh":      {"forbidden": ["wine", "beer", "rum", "pork", "lard"]},
    "aden":      {"forbidden": ["wine", "beer", "rum", "pork"]},
    "hormuz":    {"forbidden": ["wine", "beer", "rum", "pork"]},
    "alexandria":{"forbidden": ["wine", "pork"]},
    "istanbul":  {"forbidden": ["pork"]},
    "dakar":     {"forbidden": ["wine", "beer", "rum", "pork"]},

    # East Asia — high demand for European luxury goods
    "guangzhou": {"preferred": ["wool", "cloth", "glass", "wine", "coral", "amber"]},
    "nagasaki":  {"preferred": ["silk", "sugar", "glassware", "wool"]},
    "hangzhou":  {"preferred": ["wool", "coral", "glass"]},

    # Caribbean — European goods at a premium
    "port royal":{"preferred": ["wool", "cloth", "linen", "tools", "firearms"]},
    "havana":    {"preferred": ["cloth", "iron", "tools"]},

    # Indian Ocean — European metals and textiles valued
    "calicut":   {"preferred": ["silver", "copper", "cloth", "coral"]},
    "goa":       {"preferred": ["cloth", "metals", "glass"]},
    "diu":       {"preferred": ["coral", "cloth", "metals"]},

    # North Europe — eastern luxury goods valued
    "london":    {"preferred": ["pepper", "cinnamon", "silk", "porcelain", "sugar",
                                "tobacco", "indigo", "cotton"]},
    "amsterdam": {"preferred": ["pepper", "spices", "silk", "porcelain", "cotton"]},
}


def forbidden_goods(port: str) -> list[str]:
    """Return goods that earn zero at this port (don't buy to sell here)."""
    return PORT_PREFERENCES.get(port.lower(), {}).get("forbidden", [])


def preferred_goods(port: str) -> list[str]:
    """Return good names/categories that earn a premium at this port."""
    return PORT_PREFERENCES.get(port.lower(), {}).get("preferred", [])


def good_is_forbidden(good_name: str, destination_port: str) -> bool:
    """True if this good should not be carried to this destination."""
    forbidden = forbidden_goods(destination_port)
    name_lower = good_name.lower()
    return any(f in name_lower for f in forbidden)


# ── Combined prior score ───────────────────────────────────────────────────────

def leg_profit_score(from_port: str, to_port: str) -> float:
    """
    Estimated relative profit score for a trade leg from_port → to_port.

    Higher = more likely to be profitable.
    Scale is arbitrary — used only for comparison between legs.

    Components:
      - cross-region multiplier (distance proxy)
      - specialty bonus if the origin has specialties valuable at destination
      - small penalty if destination has many forbidden goods (fewer options)
    """
    score = cross_region_multiplier(from_port, to_port)

    if has_specialty_opportunity(from_port, to_port):
        score *= SPECIALTY_SELL_BONUS

    # Penalty proportional to forbidden goods count at destination
    # (fewer sellable goods = less cargo capacity used productively)
    n_forbidden = len(forbidden_goods(to_port))
    if n_forbidden:
        score *= max(0.7, 1.0 - n_forbidden * 0.05)

    return score
