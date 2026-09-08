"""Place names, resolved against the baked world-map catalogue.

WHERE THIS LIVES IS THE POINT. A port name is KB data, not a UI concern — and the task
layer has to be able to ask "is this a real port?" before it routes a fleet there. It sat in
`actions/` until 2026-09-08, when `mission_runner` needed it and the layering test caught the
import: a task module reaching into actions/ has stepped around the dispatcher. The check is
pure data, so it moves to the layer that owns the data rather than the rule being bent.

Both catalogues are BAKED, complete and offline — 224 ports and 69 villages.
"""

from __future__ import annotations

def _catalogues():
    """({stripped port name: canonical}, {stripped village name: canonical}).

    Both are BAKED, complete and offline — 224 ports and 69 villages from the world-map
    catalogue. Cached on the function so a per-material read does not reload them.
    """
    if getattr(_catalogues, "_cache", None) is None:
        import json
        from pathlib import Path
        base = Path("memory/knowledge/world_map")

        def _table(fname, key):
            try:
                raw = json.loads((base / fname).read_text(encoding="utf-8")).get(key) or {}
            except Exception:                          # noqa: BLE001 — absent is not fatal
                return {}
            return {_strip_name(r.get("name")): r.get("name")
                    for r in raw.values() if isinstance(r, dict) and r.get("name")}

        _catalogues._cache = (_table("port_coordinates.json", "ports"),
                              _table("village_coordinates.json", "villages"))
    return _catalogues._cache


def _strip_name(s) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", str(s or ""))
                   if unicodedata.category(c) != "Mn").lower().strip()


def resolve_source_port(raw: str):
    """The canonical PORT this Source-panel line names, or None.

    THE PANEL LISTS MORE THAN PORTS, and everything it lists reads as a line of text. A
    material's Source panel carries producing VILLAGES and section headings beside the
    market ports, and the reader below took the lot. What that produced, live:

        Matchlock Gun (13): Barcelona, Chinook, Village, Seville, Ciguayo Village, ...
        Iron (16):          ... Production, Smelting Handbook: Uncut Ore, ...
        Candle (14):        ... Sundry Goods Company Directory_, ...

    against a truth of two ports for Matchlock and four for Iron. The missions only worked
    because the real ports sorted to the front.

    The world-map catalogue is the cross-check, and it is already baked: 224 ports and 69
    villages, offline and complete. Accent-stripped EXACT matching does the work — 'Gijon'
    is Gijón, 'Malaga' is Málaga, 'Male' is Malé — with no fuzzy threshold to tune.

    A VILLAGE IS A REJECTION, NOT A MATCH, and it must be tested BEFORE any fuzziness: a
    village has no market to buy from, and `correct_port_name` scores 'Sioux Village'
    against 'Seville' at 0.60, which would file a port that does not sell the material at
    all. That is worse than the heading it replaces — a heading sends nobody anywhere.
    """
    key = _strip_name(raw)
    if not key:
        return None
    ports, villages = _catalogues()
    if key in ports:
        return ports[key]
    if key in villages:
        return None                    # a real place, and not one with a market
    return None


def resolve_source_village(raw: str):
    """The canonical VILLAGE this Source-panel line names, or None.

    A VILLAGE IS A SOURCE, NOT A REJECTION (user, 2026-09-08: "village is a legitimate
    source, some materials are available at both villages and ports, like diamond. And
    some only at villages or ports"). `resolve_source_port` above rejects villages, and it
    is right to — a village has no market — but the caller then dropped them on the floor,
    so a material sold ONLY at a village had no recorded source at all. That is the same
    good the barter chains are built on: Birch Tree is a barter good and an input to
    Naverslojd.
    """
    key = _strip_name(raw)
    if not key:
        return None
    _ports, villages = _catalogues()
    return villages.get(key)
