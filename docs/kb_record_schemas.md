# Knowledge Base — Record Schemas

Records live under `memory/knowledge/` and are organised by topic.  See
also [`docs/kb_design.md`](kb_design.md) for the broader design
discussion.

## Port records — `memory/knowledge/ports/<port_slug>.json`
Which buildings exist in this port, indexed by building name.

```json
{
  "port": "Amsterdam",
  "buildings": ["harbor", "market", "castle", "..."],
  "last_explored": "..."
}
```

## Building records — `memory/knowledge/buildings/<port_slug>__<building_slug>.json`
What is known about a specific building in a specific port.

```json
{
  "port": "Amsterdam",
  "building_name": "castle",
  "building_type": "castle",
  "purpose": "Collect daily government rewards and check official quests",
  "actions": ["collect_daily_reward", "view_quests"],
  "ui_elements": ["Daily Reward button", "Quest Board"],
  "visit_count": 3,
  "first_visited_at": "...",
  "last_visited_at": "...",
  "screenshots": ["..."],
  "raw_ocr_snapshots": ["..."]
}
```

## Building-type records — `memory/knowledge/building_types/<type_slug>.json`
Cross-port knowledge about a building category.  Castles work the same
everywhere; this record captures the shared understanding so L1 can
answer "what does a Castle do?" without needing a port-specific record
for every city.

```json
{
  "building_type": "castle",
  "description": "Government building — daily rewards and official quests",
  "common_actions": ["collect_daily_reward", "view_quests"],
  "seen_in_ports": ["Amsterdam", "Lisbon"],
  "notes": "Function is identical across all ports visited so far"
}
```

## Market price records — `memory/knowledge/markets/<port_slug>__market.json`
Markets differ port-to-port and prices fluctuate.  Each visit appends a
snapshot.

```json
{
  "port": "Amsterdam",
  "price_history": [
    {
      "timestamp": "...",
      "goods": [{"name": "Wool", "buy": 120, "sell": 95}, "..."]
    }
  ]
}
```

## Scene inventories — `memory/knowledge/scenes/<type>__<title>.json`
Full element map for a (scene_type, title) pair, populated by Claude
Vision on first visit and reused forever.  Detailed schema lives in the
KB design doc.
