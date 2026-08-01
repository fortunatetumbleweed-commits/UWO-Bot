# memory/knowledge_base.py
# Structured knowledge store — what the bot has learned about ports, buildings,
# markets, screen layouts, and interactions over time.
#
# Record types:
#   ports/           — which buildings exist in each port
#   buildings/       — what a specific building does in a specific port
#   building_types/  — cross-port knowledge about a building category
#   markets/         — price history per port market (prices fluctuate)
#   screens/         — layout and structure of known screens (learned by agent)
#   interactions/    — "tapping X on screen Y does Z" (learned by agent)
#
# Access pattern:
#   kb = KnowledgeBase()
#   kb.get_building("Amsterdam", "castle")     → dict or None
#   kb.get_screen_structure("market")          → dict or None
#   kb.get_interaction("market", "purchase")   → dict or None

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KB_ROOT = Path("memory/knowledge")
PORTS_DIR        = KB_ROOT / "ports"
BUILDINGS_DIR    = KB_ROOT / "buildings"
TYPES_DIR        = KB_ROOT / "building_types"
MARKETS_DIR      = KB_ROOT / "markets"
SCREENS_DIR      = KB_ROOT / "screens"
INTERACTIONS_DIR = KB_ROOT / "interactions"


# ── helpers ───────────────────────────────────────────────────────────────────

def _slug(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", "_", text)
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── KnowledgeBase ─────────────────────────────────────────────────────────────

class KnowledgeBase:
    """Read/write interface to the structured knowledge store."""

    # ── ports ────────────────────────────────────────────────────────────────

    def known_ports(self) -> list[str]:
        """Return names of all ports with at least one record."""
        if not PORTS_DIR.exists():
            return []
        return sorted(
            json.load(open(p))["port"]
            for p in PORTS_DIR.glob("*.json")
        )

    def get_port(self, port: str) -> dict | None:
        return _load(PORTS_DIR / f"{_slug(port)}.json")

    def record_port_buildings(self, port: str, buildings: list[str]) -> None:
        """Update the list of buildings known to exist in *port*."""
        path = PORTS_DIR / f"{_slug(port)}.json"
        existing = _load(path) or {"port": port, "buildings": [], "first_seen": _now()}
        # Merge — never remove buildings already recorded
        known = set(existing["buildings"])
        known.update(b.lower() for b in buildings)
        existing["buildings"] = sorted(known)
        existing["last_explored"] = _now()
        _save(path, existing)

    # ── buildings ────────────────────────────────────────────────────────────

    def get_building(self, port: str, building: str) -> dict | None:
        return _load(BUILDINGS_DIR / f"{_slug(port)}__{_slug(building)}.json")

    def save_building(
        self,
        port: str,
        building: str,
        *,
        building_type: str = "",
        purpose: str = "",
        actions: list[str] | None = None,
        ui_elements: list[str] | None = None,
        screenshot_path: str = "",
        ocr_texts: list[str] | None = None,
    ) -> dict:
        """
        Create or update the knowledge record for (port, building).
        Increments visit_count on each call.
        """
        path = BUILDINGS_DIR / f"{_slug(port)}__{_slug(building)}.json"
        existing = _load(path)
        ts = _now()

        if existing is None:
            record: dict[str, Any] = {
                "port": port,
                "building_name": building,
                "building_type": building_type or _slug(building),
                "purpose": purpose,
                "actions": actions or [],
                "ui_elements": ui_elements or [],
                "visit_count": 1,
                "first_visited_at": ts,
                "last_visited_at": ts,
                "screenshots": [screenshot_path] if screenshot_path else [],
                "raw_ocr_snapshots": [{"ts": ts, "texts": ocr_texts or []}],
            }
        else:
            record = existing
            record["visit_count"] = record.get("visit_count", 0) + 1
            record["last_visited_at"] = ts
            if purpose:
                record["purpose"] = purpose
            if actions:
                record["actions"] = actions
            if ui_elements:
                record["ui_elements"] = ui_elements
            if building_type:
                record["building_type"] = building_type
            if screenshot_path:
                record.setdefault("screenshots", []).append(screenshot_path)
            if ocr_texts:
                record.setdefault("raw_ocr_snapshots", []).append(
                    {"ts": ts, "texts": ocr_texts}
                )

        _save(path, record)
        # Keep the port index up to date
        self.record_port_buildings(port, [building])
        return record

    def all_buildings_for_port(self, port: str) -> list[dict]:
        """Return all building records for a given port."""
        prefix = f"{_slug(port)}__"
        records = []
        if not BUILDINGS_DIR.exists():
            return records
        for path in sorted(BUILDINGS_DIR.glob(f"{prefix}*.json")):
            r = _load(path)
            if r:
                records.append(r)
        return records

    # ── building types ────────────────────────────────────────────────────────

    def get_building_type(self, building_type: str) -> dict | None:
        return _load(TYPES_DIR / f"{_slug(building_type)}.json")

    def save_building_type(
        self,
        building_type: str,
        *,
        description: str = "",
        common_actions: list[str] | None = None,
        port: str = "",
        notes: str = "",
    ) -> dict:
        path = TYPES_DIR / f"{_slug(building_type)}.json"
        existing = _load(path)
        ts = _now()

        if existing is None:
            record: dict[str, Any] = {
                "building_type": building_type,
                "description": description,
                "common_actions": common_actions or [],
                "seen_in_ports": [port] if port else [],
                "notes": notes,
                "first_recorded": ts,
                "last_updated": ts,
            }
        else:
            record = existing
            if description:
                record["description"] = description
            if common_actions:
                record["common_actions"] = common_actions
            if port and port not in record.get("seen_in_ports", []):
                record.setdefault("seen_in_ports", []).append(port)
            if notes:
                record["notes"] = notes
            record["last_updated"] = ts

        _save(path, record)
        return record

    def all_building_types(self) -> list[dict]:
        if not TYPES_DIR.exists():
            return []
        return [_load(p) for p in sorted(TYPES_DIR.glob("*.json")) if _load(p)]

    # ── markets ───────────────────────────────────────────────────────────────

    def get_market_prices(self, port: str) -> list[dict]:
        """Return full price history for *port*'s market (newest first)."""
        record = _load(MARKETS_DIR / f"{_slug(port)}__market.json")
        if not record:
            return []
        return list(reversed(record.get("price_history", [])))

    def append_market_prices(self, port: str, goods: list[dict]) -> None:
        """
        Append a price snapshot for *port*.
        *goods* is a list of {"name": str, "buy": int, "sell": int}.
        """
        path = MARKETS_DIR / f"{_slug(port)}__market.json"
        record = _load(path) or {"port": port, "price_history": []}
        record["price_history"].append({"timestamp": _now(), "goods": goods})
        _save(path, record)

    def latest_market_prices(self, port: str) -> list[dict]:
        """Return the most recent goods list for *port*, or []."""
        history = self.get_market_prices(port)
        return history[0]["goods"] if history else []

    # ── screen structures (learned by agent) ─────────────────────────────────

    def get_screen_structure(self, screen_title: str) -> dict | None:
        """Return the learned layout/structure record for a screen, or None."""
        return _load(SCREENS_DIR / f"{_slug(screen_title)}.json")

    def save_screen_structure(
        self,
        screen_title: str,
        structure: str,
        confidence: float = 0.5,
    ) -> dict:
        """
        Save or update the layout understanding for *screen_title*.
        Only overwrites existing knowledge if the new confidence is higher.
        """
        path = SCREENS_DIR / f"{_slug(screen_title)}.json"
        existing = _load(path)
        ts = _now()
        if existing is None:
            record: dict[str, Any] = {
                "screen_title": screen_title,
                "structure": structure,
                "confidence": confidence,
                "first_learned": ts,
                "last_updated": ts,
            }
        else:
            record = existing
            record["last_updated"] = ts
            if confidence >= record.get("confidence", 0.0):
                record["structure"] = structure
                record["confidence"] = confidence
        _save(path, record)
        return record

    # ── interactions (learned by agent) ──────────────────────────────────────

    def get_interaction(self, screen: str, element: str) -> dict | None:
        """Return the learned outcome of tapping *element* on *screen*, or None."""
        return _load(
            INTERACTIONS_DIR / f"{_slug(screen)}__{_slug(element)}.json"
        )

    def save_interaction(
        self,
        screen: str,
        element: str,
        outcome: str,
        confidence: float = 0.5,
    ) -> None:
        """
        Record that tapping *element* on *screen* produces *outcome*.
        Only overwrites if the new confidence is higher.
        """
        path = INTERACTIONS_DIR / f"{_slug(screen)}__{_slug(element)}.json"
        existing = _load(path)
        ts = _now()
        if existing is None:
            record: dict[str, Any] = {
                "screen": screen,
                "element": element,
                "outcome": outcome,
                "confidence": confidence,
                "first_learned": ts,
                "last_updated": ts,
            }
        else:
            record = existing
            record["last_updated"] = ts
            if confidence >= record.get("confidence", 0.0):
                record["outcome"] = outcome
                record["confidence"] = confidence
        _save(path, record)


# ── display helpers (used by the chat kb command) ─────────────────────────────

def _fmt_building(r: dict, detail: bool = False) -> str:
    name = r.get("building_name", "?").title()
    purpose = r.get("purpose") or "(not yet analysed)"
    visits = r.get("visit_count", 0)
    lines = [f"  {name}  [{visits} visit{'s' if visits != 1 else ''}]"]
    lines.append(f"    {purpose}")
    if detail:
        actions = r.get("actions") or []
        if actions:
            lines.append(f"    Actions: {', '.join(actions)}")
        elements = r.get("ui_elements") or []
        if elements:
            lines.append(f"    UI:      {', '.join(elements)}")
    return "\n".join(lines)


def show_kb(args: list[str]) -> None:
    """
    Pretty-print knowledge base contents.

    Usage patterns (args = words after "kb"):
      kb                        list all known ports
      kb <port>                 list buildings in that port
      kb <port> <building>      details for one building
      kb type <type>            cross-port knowledge for a building type
      kb market <port>          latest market prices for a port
    """
    kb = KnowledgeBase()

    if not args:
        # List all known ports
        ports = kb.known_ports()
        if not ports:
            print("  Knowledge base is empty — run 'explore' to start learning.")
            return
        print(f"\n  Known ports ({len(ports)}):")
        for port in ports:
            rec = kb.get_port(port)
            buildings = rec.get("buildings", []) if rec else []
            print(f"    {port}  —  {len(buildings)} building(s): {', '.join(buildings)}")
        print()
        return

    if args[0].lower() == "type":
        # Building type query
        type_name = " ".join(args[1:]) if len(args) > 1 else ""
        if not type_name:
            types = kb.all_building_types()
            if not types:
                print("  No building-type knowledge yet.")
                return
            print(f"\n  Known building types ({len(types)}):")
            for t in types:
                ports = t.get("seen_in_ports", [])
                print(f"    {t['building_type'].title()}  —  seen in: {', '.join(ports) or 'none yet'}")
                print(f"      {t.get('description') or '(no description)'}")
            print()
        else:
            t = kb.get_building_type(type_name)
            if not t:
                print(f"  No type knowledge for '{type_name}'.")
            else:
                print(f"\n  Building type: {t['building_type'].title()}")
                print(f"  Description:   {t.get('description') or '—'}")
                print(f"  Actions:       {', '.join(t.get('common_actions') or []) or '—'}")
                print(f"  Seen in:       {', '.join(t.get('seen_in_ports') or []) or '—'}")
                if t.get("notes"):
                    print(f"  Notes:         {t['notes']}")
            print()
        return

    if args[0].lower() == "market":
        port = " ".join(args[1:]) if len(args) > 1 else ""
        if not port:
            print("  Usage: kb market <port name>")
            return
        goods = kb.latest_market_prices(port)
        if not goods:
            print(f"  No market price data for '{port}' yet.")
        else:
            print(f"\n  {port.title()} — latest market prices:")
            print(f"  {'Good':<25} {'Buy':>6}  {'Sell':>6}")
            print(f"  {'-'*25} {'-'*6}  {'-'*6}")
            for g in sorted(goods, key=lambda x: x.get("name", "")):
                print(f"  {g.get('name','?'):<25} {g.get('buy',0):>6}  {g.get('sell',0):>6}")
        print()
        return

    # Port query (1 or 2 args)
    port = args[0].title()
    if len(args) == 1:
        buildings = kb.all_buildings_for_port(port)
        if not buildings:
            print(f"  No knowledge about '{port}' yet.")
            return
        print(f"\n  {port} — {len(buildings)} building(s):")
        for r in buildings:
            print(_fmt_building(r))
        print()
        return

    # Specific building
    building = " ".join(args[1:])
    r = kb.get_building(port, building)
    if not r:
        print(f"  No knowledge about '{building}' in {port} yet.")
    else:
        print(f"\n  {port} › {building.title()}")
        print(_fmt_building(r, detail=True))
        last = r.get("last_visited_at", "")[:10]
        if last:
            print(f"    Last visited: {last}")
    print()
