# Vision Pipeline — Full Architecture

Every tick the bot takes a screenshot and passes it through a pipeline of
models.  Each layer answers a different question and has a different cost
profile.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Screenshot (2400×1080, captured via ADB)                               │
└────────────────────────────┬────────────────────────────────────────────┘
                             │
          ┌──────────────────▼──────────────────┐
          │  L0 — MobileNetV3-small classifier   │  ~0.1s, local, free
          │  "What scene type is this?"          │
          │  19 classes: port_overworld,         │
          │  port_map, building_interior,        │
          │  sub_menu, sea, market, …            │
          │  Trained on labeled screenshots.     │
          │  Confidence ≥ 70% → use result.      │
          └──────────────────┬──────────────────┘
                             │  scene_type
          ┌──────────────────▼──────────────────┐
          │  L1 — Chrome detector               │  ~0.1s, local, free
          │  "Which stable UI chrome elements   │
          │   are present?"                     │
          │  Template-matches fixed buttons:    │
          │  hamburger, home, back_arrow,        │
          │  world_map_btn, right_panel.         │
          │  Overrides L0 when it fires —        │
          │  deterministic and high-precision.   │
          └──────────────────┬──────────────────┘
                             │  chrome flags + refined scene_type
          ┌──────────────────▼──────────────────┐
          │  L1.5 — OmniParser v2               │  ~2s, local, free
          │  "What interactive elements are     │
          │   present right now, and where?"    │
          │  Fine-tuned Florence-2 detects ALL  │
          │  buttons, icons, tabs, list items   │
          │  with pixel-accurate bounding boxes │
          │  and auto-generated descriptions.   │
          │  Runs every frame — positions shift │
          │  as content changes.                │
          │  Output: [{label, bbox, type}, …]   │
          └──────────────────┬──────────────────┘
                             │  element list with exact coordinates
          ┌──────────────────▼──────────────────┐
          │  L2 — OCR (EasyOCR / PaddleOCR)    │  ~0.5s, local, free
          │  "What does the text say?"          │
          │  Reads port name (top-left),        │
          │  building labels (port map),        │
          │  prices (market), screen title.     │
          │  PaddleOCR for player-generated     │
          │  text (CJK, Cyrillic, Arabic).      │
          └──────────────────┬──────────────────┘
                             │  text content at known/detected positions
          ┌──────────────────▼──────────────────┐
          │  L3 — Knowledge Base (cache)        │  instant, free
          │  "Have we understood this scene     │
          │   before?"                          │
          │  Keyed by (scene_type, title).      │
          │  Hit → return cached SceneInventory │
          │  (layout, element purposes,         │
          │   navigation hints).                │
          │  Miss → escalate to L4.             │
          └──────────────────┬──────────────────┘
                             │  cache miss only
          ┌──────────────────▼──────────────────┐
          │  L4 — Claude Vision API             │  ~5s, API cost, ONCE only
          │  "What does this scene mean and     │
          │   what should the bot do here?"     │
          │                                     │
          │  Input: OmniParser element list     │
          │  (text, not full image) + small     │
          │  thumbnail for visual context.      │
          │                                     │
          │  Output: structured SceneInventory  │
          │  — layout description               │
          │  — navigation hints                 │
          │  — purpose of each element          │
          │  — game-context understanding       │
          │                                     │
          │  Result cached forever in KB.       │
          │  Same scene never calls API again.  │
          └─────────────────────────────────────┘
```

## Division of labour

| Model | Question answered | Runs when | Cost |
|---|---|---|---|
| MobileNetV3 | What scene type? | Every frame | ~0.1s, free |
| Chrome detector | Which chrome buttons visible? | Every frame | ~0.1s, free |
| OmniParser | Where is every tappable element? | Every frame | ~2s, local |
| EasyOCR / PaddleOCR | What does the text say? | On demand | ~0.5s, free |
| Knowledge Base | Have we seen this before? | Every frame | Instant, free |
| Claude Vision API | What does it all mean? | First visit only | ~5s, API call |
| Claude Fallback | Local model failed — what is this? | On failure only | ~2s, API call → training data |

**Key design principle — separation of detection from reasoning:**
- OmniParser detects *what* is on screen with pixel-accurate bounding
  boxes.  It never hallucinates positions.
- Claude reasons about *why* elements exist and *what they do* in game
  context — from OmniParser's text output, not from the raw image.  This
  makes Claude faster, cheaper, and more accurate because it is reasoning
  over structured data instead of trying to locate pixels.
- On every subsequent visit OmniParser re-detects current positions (UI
  shifts as content changes); Claude's cached understanding provides the
  meaning.  The two together = complete, accurate, up-to-date picture.

## "Learn once, reuse forever" + Data Flywheel
Two complementary learning mechanisms:

**L3/L4 cache (scene understanding):** the API call rate drops toward
zero as the KB grows.  By the time the bot has visited every building
type once, L4 never fires again.

**Data flywheel (distillation):** when a local model fails, Claude
labels the example and the result is saved to `data/training/<category>/`.
Over time these accumulate into a per-category training dataset.  When a
category has enough labeled examples, a local model is retrained on
Claude's labels (knowledge distillation) and stops needing the fallback.
The bot gets smarter the more it runs — without requiring manual labeling.

## Stored knowledge
```
memory/knowledge/
  ports/<slug>.json              ← which buildings exist in this port
  buildings/<port>__<bld>.json   ← visit history, purpose, actions per building
  building_types/<type>.json     ← cross-port: what a Castle/Bank/Inn does everywhere
  markets/<port>__market.json    ← price snapshots, appended each visit
  scenes/<type>__<title>.json    ← SceneInventory: full element map from Claude+OmniParser
```

Schemas: see [`docs/kb_record_schemas.md`](kb_record_schemas.md).

## UI Component Registry (stable cross-screen elements)
Some elements appear on nearly every screen and always mean the same
thing.  These are tracked separately from per-scene inventories:

| Component | Appears on | Description |
|---|---|---|
| `home_button` ⌂ | building_interior, sub_menu, port_map | Exits directly to port overworld |
| `back_arrow` ← | building_interior, sub_menu, port_map | Goes one level back |
| `hamburger` ≡ | port_overworld | Opens main menu |
| `tutorial_button` ? | any screen with a `?` after the title | Starts guided tutorial — highlights every interactive element with explanation |
| `world_map_btn` | port_map (icon + text), mini_map (icon only) | Opens world map |
| `building_name_panel` | port_overworld | Floating panel above entrance; expands on approach with multi-line description; tapping it enters the building |
| `right_panel` | port_overworld | Tab bar + mini map on right side |

The `?` tutorial button is especially valuable for first-time building
exploration — the game itself narrates every interactive element.

## Subsequent additions (post-original-design)

Layers added since the original pipeline:

- **Family classifier (L0.5 — MobileNetV3 on labelled families)** — fast
  short-circuit that returns one of `port_overworld`, `world_map`, `sea`,
  `chromed`, `transient` at very high confidence.  When confidence is
  high we skip the heavier L0.1–L2 stages.  Trained on labelled corpus
  from the supervisor labeler.  See `vision/family_classifier.py`.
- **Typed detectors over keyword matching** — `DialogModel` (typed dialog
  detection), `BuildingNpcOverlay`, structural `obstruction classifier`
  replace the older substring-matching interruptor loop.  See
  `vision/region_detectors/` and `docs/dialog_and_event_models.md`.
- **Fingerprint registry** — `(scene_type, title) → fingerprint` lookup
  with foundational + learned entries.  See
  `docs/phase6_fingerprint_coverage.md`.
- **Goal-aware perception** — perception is parameterised on the active
  goal so Qwen / Claude see what the bot is *trying to do*, not just the
  raw screen.  See
  `memory/feedback_goal_aware_perception_design.md`.
- **Sea HUD lat/lon read** — the position-truth signal lives below the
  mini-map and is parsed via OmniParser + EasyOCR fallback with a
  lenient parser for the ship-action-radius noise.  See
  `docs/sea_capture_and_steering.md` and
  `memory/project_sea_hud_latlon.md`.
