# memory/knowledge/culture_rules.py
#
# Cultural restrictions on trade goods.
#
# Each culture has goods that are prohibited (sell price = 0, never listed for
# purchase) or simply very low-value.  Buying these goods at a port and trying
# to sell them at a culturally-restricted port wastes cargo space and time.
#
# Rules are applied in auto_buy() when a destination is known:
#   filtered = get_sellable_goods(goods_available, destination_port)
#
# Data is loaded from memory/knowledge/config/culture_rules.json.
# To add a new port culture or restriction: edit that file — no code change needed.

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


# ── Load from JSON ─────────────────────────────────────────────────────────────

_CULTURE_RULES_FILE = Path("memory/knowledge/config/culture_rules.json")


def _load_culture_rules() -> tuple[
    dict[str, set[str]],
    dict[str, str],
    dict[str, set[str]],
]:
    try:
        raw = json.loads(_CULTURE_RULES_FILE.read_text())
        restrictions = {
            culture: set(goods)
            for culture, goods in raw.get("restrictions", {}).items()
        }
        port_cultures: dict[str, str] = raw.get("port_cultures", {})
        category_restrictions = {
            culture: set(cats)
            for culture, cats in raw.get("category_restrictions", {}).items()
        }
        return restrictions, port_cultures, category_restrictions
    except Exception as exc:
        from loguru import logger
        logger.error(f"Failed to load culture_rules.json: {exc}")
        return {}, {}, {}


CULTURE_RESTRICTIONS, PORT_CULTURES, CATEGORY_RESTRICTIONS = _load_culture_rules()


def reload_culture_rules() -> None:
    """Reload all culture rule tables from disk."""
    global CULTURE_RESTRICTIONS, PORT_CULTURES, CATEGORY_RESTRICTIONS
    CULTURE_RESTRICTIONS, PORT_CULTURES, CATEGORY_RESTRICTIONS = _load_culture_rules()
    from loguru import logger
    logger.info(
        f"Reloaded culture rules: {len(CULTURE_RESTRICTIONS)} cultures, "
        f"{len(PORT_CULTURES)} port mappings"
    )


def get_culture_for_port(port_name: str) -> Optional[str]:
    """
    Return the culture name for a given port, or None if unknown.
    First checks the hardcoded map, then the KB (City Info records).
    """
    slug = port_name.lower().strip().replace(" ", "_")
    # Direct lookup
    if slug in PORT_CULTURES:
        return PORT_CULTURES[slug]
    # Partial match
    for key, culture in PORT_CULTURES.items():
        if key in slug or slug in key:
            return culture
    # Try KB
    try:
        from actions.world_map import load_city_info_from_kb
        info = load_city_info_from_kb(port_name)
        if info and info.get("culture"):
            culture_str = info["culture"].lower()
            for known_culture in CULTURE_RESTRICTIONS:
                if known_culture in culture_str:
                    return known_culture
    except Exception:
        pass
    return None


def get_restricted_goods(destination_port: str) -> set[str]:
    """
    Return the set of good names (lowercase) that cannot be sold at
    *destination_port* due to cultural restrictions.

    Returns an empty set if the port's culture is unknown or has no restrictions.
    """
    culture = get_culture_for_port(destination_port)
    if culture is None:
        return set()
    return CULTURE_RESTRICTIONS.get(culture, set())


def get_restricted_categories(destination_port: str) -> set[str]:
    """
    Return the set of category substrings that are restricted at the destination.
    """
    culture = get_culture_for_port(destination_port)
    if culture is None:
        return set()
    return CATEGORY_RESTRICTIONS.get(culture, set())


def is_good_sellable_at(good_name: str, destination_port: str,
                         category: Optional[str] = None) -> bool:
    """
    Return True if *good_name* can be sold at *destination_port*.

    Checks:
      1. Good name against the culture's restricted goods list.
      2. Category substring against restricted category list (if category provided).
    """
    name_lower = good_name.lower().strip()
    restricted = get_restricted_goods(destination_port)
    if any(r in name_lower for r in restricted):
        return False

    if category:
        cat_lower = category.lower()
        cat_restricted = get_restricted_categories(destination_port)
        if any(r in cat_lower for r in cat_restricted):
            return False

    return True


def update_port_culture(port_name: str, culture: str) -> None:
    """
    Record a port→culture mapping discovered during play.
    Updates the in-memory dict and persists it to the KB.
    """
    slug = port_name.lower().strip().replace(" ", "_")
    PORT_CULTURES[slug] = culture.lower()

    # Persist to KB so it survives restarts
    try:
        from actions.world_map import load_city_info_from_kb, save_city_info_to_kb, CityInfoPanel
        info_dict = load_city_info_from_kb(port_name) or {}
        info = CityInfoPanel(
            port=port_name,
            culture=culture,
            goods=info_dict.get("goods", []),
            facilities=info_dict.get("facilities", []),
            tax_info=info_dict.get("tax_info", ""),
        )
        save_city_info_to_kb(port_name, info)
    except Exception:
        pass
