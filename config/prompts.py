# config/prompts.py
# LLM prompt templates and seed game knowledge.
# All prompts live here so they can be tuned without touching logic.

# ── Seed knowledge (system prompt fed to every LLM call) ─────────────────────

SEED_KNOWLEDGE = """
You are the AI brain of a bot playing Uncharted Waters Origin (UWO),
an Age of Sail trading and exploration game on Android.

## Game Overview
- You run a trading company with ships and crew
- Primary currency: ducats. Goal: grow wealth and expand the fleet
- Core loop: sail between ports → buy goods cheap → sell elsewhere for profit

## Scene Types
There are three main scenes:

1. PORT OVERWORLD / PORT MAP
   - You are inside a port, walking between buildings
   - Right panel: tab bar (tasks / buildings / people / location), mini map, building list
   - Tap the mini map centre → opens port map (clean grayscale overhead view,
     all buildings shown as labeled icons)
   - Tap the globe icon (bottom-right corner of the mini map) → opens world map
   - On port map: tap a building label → character walks there and enters

2. BUILDING INTERIOR
   - You are inside a specific building (market, inn, castle, etc.)
   - Screen title (top-left) shows the building name
   - Press back to return to port overworld

3. SEA / WORLD MAP
   - Your ship is sailing between ports
   - Tap mini map → opens world map (shows all ports + current ship position)
   - Tap a port on world map → character walks to harbour → ship departs
   - Before departing: game checks supplies and crew

## Navigation Pattern (identical at both scales)
  open map → find destination label → tap it → wait for arrival
  Port scale : port map → tap building → character enters building
  Sea scale  : world map → tap port → ship sails there

## Common Building Types
- Market     : buy and sell trade goods (prices differ by port)
- Inn        : rest crew, hire sailors
- Castle     : government quests, daily rewards
- Harbor     : manage ships, supplies
- Bank       : loans, money storage
- Union      : trade missions and requests
- Shipyard   : repair and upgrade ships
- Item Shop  : tools and equipment
- Bureau     : official documents and licences
- Cathedral  : blessings and religious quests
- Fortune Teller : predictions and special information

## Key Facts
- Profit = buy goods low at one port, sell high at another
- Prices fluctuate — always read current prices before trading
- Need sufficient crew and supplies before sailing
- Building types work the same way across all ports

## Behaviour Guidelines
- Check the knowledge base before acting — if you have seen this before, use it
- On a new screen: observe first, then probe with the smallest safe action
- Do not spend ducats or take irreversible actions until you understand the situation
- Learn structure, not every detail — "this is a goods grid" is enough;
  you do not need to memorise every price
"""

# ── Screen perception ─────────────────────────────────────────────────────────

PERCEIVE_PROMPT = """\
Look at this screenshot from Uncharted Waters Origin.
Scene classification has already been handled separately — focus on describing
the CONTENT of the screen: what is visible, readable, and interactive.

Respond ONLY with a JSON object — no explanation text, no markdown fences.

{
  "scene_type": "port_overworld" | "port_map" | "building_interior" | "sub_menu" | "world_map" | "sea" | "dialog" | "loading" | "unknown",
  "screen_title": "text shown in the top-left area (port name, building name, or menu item), or null",
  "visible_elements": ["list of distinct UI elements or game objects visible"],
  "text_labels": ["list of readable text strings on screen"],
  "interactive_hints": ["elements that look tappable or interactive"],
  "description": "one sentence summary of what is on screen and what can be done"
}
"""

# ── Reasoning / planning ──────────────────────────────────────────────────────

REASON_PROMPT_TEMPLATE = """\
Current game state:
{state_summary}

Current goal: {goal}

Knowledge base context (what you already know about this situation):
{kb_context}

Decide the single best next action to make progress toward the goal.
If the goal is already achieved or impossible, say so.

Respond ONLY with a JSON object — no explanation text, no markdown fences.

{{
  "action_type": "tap" | "swipe" | "press_back" | "wait" | "open_port_map" | "open_world_map" | "done" | "stuck",
  "target_description": "plain English description of what to tap or interact with",
  "coords": [x, y] or null,
  "reasoning": "why this action makes sense",
  "hypothesis": "what you expect to happen after this action",
  "confidence": 0.0 to 1.0
}}
"""

# ── Learning ──────────────────────────────────────────────────────────────────

LEARN_PROMPT_TEMPLATE = """\
I took an action in a game and want to record what I learned.

Before the action:
{before_description}

Action taken: {action_description}

After the action:
{after_description}

What can be learned from this? Extract structured knowledge to save.

Respond ONLY with a JSON object — no explanation text, no markdown fences.

{{
  "confirmed": "hypothesis that was confirmed, or null",
  "refuted": "hypothesis that was refuted, or null",
  "new_knowledge": [
    {{
      "type": "screen_structure" | "interaction" | "icon_meaning" | "building_purpose" | "navigation",
      "key": "short identifier string",
      "value": "what was learned",
      "confidence": 0.0 to 1.0
    }}
  ],
  "summary": "one sentence summary of what happened"
}}
"""

# ── Building classification ───────────────────────────────────────────────────

CLASSIFY_BUILDING_PROMPT_TEMPLATE = """\
I am inside a building in Uncharted Waters Origin and I want to understand what it does.

Current screen state:
{state_summary}

Based on everything visible — the screen title, text labels, NPC presence, UI elements,
and any interactive hints — answer the following:

1. What *type* of building is this?  (e.g. market, inn, castle, harbor, bank, union,
   shipyard, item_shop, bureau, cathedral, fortune_teller, guild, tavern, …)
   Use a known type if it matches; invent a snake_case slug only if it is genuinely novel.

2. What is the primary purpose of this building?

3. What actions or services are visibly available?

4. What UI elements indicate those functions?

5. Where does this fit in the knowledge base?
   - Is this the same as a well-known building type (e.g. all Castles give daily rewards)?
   - Or is it something new that has not been seen before?

Respond ONLY with a JSON object — no explanation text, no markdown fences.

{{
  "building_type": "slug of the type (e.g. 'market', 'castle')",
  "purpose": "one-sentence description of what this building does",
  "available_actions": ["list of things you can do here"],
  "ui_elements": ["list of visible UI elements that signal the building's function"],
  "is_known_type": true or false,
  "confidence": 0.0 to 1.0,
  "notes": "anything unusual, or empty string if nothing to note"
}}
"""

# ── Port map building filter ──────────────────────────────────────────────────

FILTER_BUILDINGS_PROMPT_TEMPLATE = """\
I am looking at a port map — a grayscale overhead view of a port in a trading game.
My OCR scanner found these text labels on the screen: {ocr_labels}

The port map is rendered as a grayscale overlay, but the live port scene is still
visible underneath it. This means two types of text can bleed through:
  1. Building labels — fixed icons on the map with names like "Market", "Inn", "Castle"
  2. Player appellation text — floating titles above other players' characters,
     such as "Clan Contributor", "Grand Admiral", "Master Trader"

Numbers, single characters, and fragments are noise from icon graphics.

Which of these labels are actual building names on the map?

Respond ONLY with a JSON object — no explanation text, no markdown fences.

{{
  "buildings": ["list of building name labels"],
  "noise": ["list of non-building labels"],
  "reasoning": "brief explanation of how you decided"
}}
"""
