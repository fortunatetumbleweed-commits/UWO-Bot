# port_overworld

The port-side 3D scene with the right-side info panel and building list
visible.  This is where the bot starts most navigation decisions when
docked at a port.

## Layout

### Fixed UI chrome (positions are deterministic)

- **Top-left (cx ≈ 400, cy ≈ 55):** port name.  Read it from the
  actual OCR tokens — DO NOT assume a name; every port that exists
  in UWO may appear here, including ones not seen before.
- **Top-right cluster (x > 1700, y < 100):** chrome row.  Pass
  button at `(1775, 49)` followed by 5–6 icons (mail / mission /
  settings / hamburger and others) at evenly-spaced positions
  ~80–100 px apart.
- **Right edge (x > 1880, y 130–1000):** a **tabbed info panel**
  (similar in concept to the world_map's City Info panel but visually
  different).  Structure:
  - **Tab bar at the top:** 4 icons, left-to-right:
    1. **Tasks icon** — when selected, the panel below shows the
       active task list.
    2. **Buildings icon** — when selected, the panel shows the
       vertical list of this port's buildings (`"Harbor"`,
       `"Market"`, `"Shipyard"`, `"Bank"`, `"Inn"`, `"Sanctuary"`,
       `"Item Shop"`, `"Bureau"`, etc.).  Each row is a tappable
       `[button]`.
    3. **Nearby player icon** — when selected, the panel shows other
       players currently near this port.
    4. **Location icon** — this one is a **toggle**, not a tab.
       Tapping toggles the mini-map on/off (rather than swapping
       what's shown below).
  - **Count badges:** the first three icons (tasks / buildings /
    nearby players) may show a small number badge in their top-right
    corner indicating how many items the tab has — e.g. `3` on the
    tasks icon means 3 active tasks, `8` on the buildings icon means
    8 buildings in this port.  The location icon does not show a
    badge.
  - **Date / season / time bar (cy ≈ 350–460):** sits between the
    tab bar and the active tab's content.  Contains tokens like
    `"Spring"`, `"Mar"`, `"09.48"`, `"Wet Season"`.
  - **Panel content area (cy 440–1000):** shows whichever tab is
    active — the task list, building list, or nearby player list.
    Only ONE of these is visible at a time.

### Player labels (fixed positions; present when applicable)

- **Appellation (cx ≈ 1196, cy ≈ 375):** the player's equipped title.
  Examples: `"Eastern Explorer"`.  Always at this position when an
  appellation is equipped.
- **Player nameplate (cx ≈ 1235, cy ≈ 507):** the player character's
  name (e.g. `"TaylorFP"`) above their sprite.

### Variable content in the 3D gameplay area (x 50–1860, y 0–1040)

- **Building name plates:** float above building entrances when the
  player is near.  Large font (~115 px bbox height), single proper-noun
  label matching a building name (Harbor, Market, Inn, Bank, etc.).
  Typical sizes ~375–426 × ~114–115 px.
- **NPC text bubbles:** speech from NPCs in the 3D area.  Smaller font
  (~30–42 px per line).  Sentence-like content — multiple words, mixed
  case, often with articles ("the", "a") and punctuation.  May be
  single-line OR multi-line.  Typical sizes ~322 × 37 (single-line) or
  ~322 × 132 (multi-line, ~3 lines).
  - Position is **anywhere in the gameplay area** — top, middle, sides.
- **Other player characters and their guild tags:** can appear in the
  3D area at variable positions.

### Event banners (any 3D-overworld screen)

- **Top-center, cy ≈ 100–150:** server-wide event tickers.  Examples:
  `"Maca Boom occurred in Yeongil"`, `"Festival occurred in <port>"`.
  These mention port names but are NOT located at those ports.
  Verbs to recognise: `occurred`, `ongoing`, `ended`, `boom`, with
  event-type names like `festival`, `plague`, `flood`, `war`.

## Element roles you may see

- `port_name` — top-left port label
- `chrome_icon` — top-right icons + the Pass button
- `right_panel_tab` — the 4 tab-bar icons above the building list
- `date_time` — season / month / time / weather tokens
- `right_panel_row` — building list entries on the right edge
- `appellation` — player's equipped title at the fixed appellation
  position
- `player_nameplate` — player character name
- `building_nameplate` — building name floating above an entrance
  (in 3D world, not in the right panel)
- `npc_bubble` — NPC speech bubble (NOISE — ignore for scene state)
- `phone_os` — phone status bar at the bottom (NOISE)
- `build_info` — bottom-right version string (NOISE)
- `event_banner` — top-bar event ticker (NOISE for scene state, but
  may carry economy-relevant info)
- `button`, `text`, `icon` — fallback for unidentified elements

## Hints

- The active right-panel tab has a gold/yellow left-edge glow.
- Tapping a right-panel building row navigates the character TO that
  building.  This is navigation, not a transaction.
- A building name plate floating in the 3D area indicates the player
  is **physically near** that building's entrance.  Tapping the name
  plate enters the building (same effect as tapping it in the right
  panel list, but with a different navigation path).
- When describing this scene to summarise game state, **IGNORE
  elements tagged `npc_bubble`, `phone_os`, `build_info`,
  `event_banner`** — they're noise.
- If you see two `building_nameplate` elements in the 3D area, the
  player is between two buildings.  Both are tappable; the player can
  go to either.

## Source frames

Authored from a representative sample of port_overworld captures
covering: day/night cycles, with/without player appellation, with/
without active NPC bubbles, and with/without building name plates
expanded.  Specific port names omitted from this section to prevent
the small Qwen model from parroting them as the live port.
