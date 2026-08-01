# Scene Model Design — deterministic perception with family hierarchy

Status: design-locked 2026-05-17.  Slice 1 in progress.

## Why this exists

The bot has been classifying screens through three increasingly tangled
layers — a chrome detector (template + element-position flags), a fingerprint
registry (bag-of-words from Claude scene analyses), and Qwen (a text-only
LLM reasoning over OCR tokens).  Each layer was added to plug a specific
hole in the previous one.  The compound system has these chronic problems:

1. **Bag-of-strings fingerprints poison the registry.**  Learning hook
   saves keywords like `['gear', 'tool']` from a Claude analysis of the
   Item Shop / Tool sub-menu.  The same keywords fire on `port_overworld`
   on later runs (gear icons + tool-named buildings in the building list),
   blocking navigation for 60-second timeouts.
2. **No deterministic structural signals.**  A single ambiguous Claude
   token can override all the structural evidence.  When the registry
   says `learned_tool_all` with high confidence, the bot believes it.
3. **LLM-on-hot-path costs without LLM-shaped value.**  Qwen runs every
   tick.  It hallucinates Wi-Fi percentages, narrates chrome counters,
   confabulates port names.  Its output decorates `perceive` but rarely
   changes a decision.  For navigation, an LLM is the wrong tool.
4. **No way to introspect "what is the bot looking at right now."**
   Wrong decisions are debugged by re-reading prose paragraphs from the
   Qwen log.  No region-level breakdown of why the classifier chose what
   it did.

The architectural fix is to move the spatial reasoning OUT of the LLM
and INTO deterministic perception code.  OmniParser gives us pixel-
accurate detections; we know what each screen region of UWO means;
reducing pixel positions to semantic regions is rules-driven, debuggable,
fast, and free.  The LLM is reserved for genuinely open-ended semantic
work (NPC dialogue, novel screens), not state classification.

## Core idea: a Scene Model

`perceive()` produces a `SceneModel` — an explicit structured picture
of the current screen, populated by small region detectors that each
own one rectangle of the screen.  Downstream code reads the SceneModel
to make navigation decisions.  No bag-of-words signals, no LLM on the
hot path, no hidden state.

```
┌─────────────────────────────────────────────────────────────────┐
│ Screenshot (2400×1080)                                          │
│       │                                                          │
│       ▼                                                          │
│ OmniParser → list[DetectedElement]  (pixel-accurate)            │
│       │                                                          │
│       ▼                                                          │
│ ┌─── Region detectors (deterministic) ──────────────────────┐    │
│ │  top_left:        lighthouse | ship | back_arrow | …      │    │
│ │  top_right:       currency strip | hamburger | home | …   │    │
│ │  bottom_chrome:   wifi/uid/server (always present)        │    │
│ │  left_menu:       vertical menu items                     │    │
│ │  right_panel:     building list | Cart | Hire | Sales …   │    │
│ │  action_buttons:  Purchase | Sell | Confirm | …           │    │
│ │  …                                                          │    │
│ └──────────────────────────────────────────────────────────┘    │
│       │                                                          │
│       ▼                                                          │
│ Scene classifier (rules over regions) → SceneModel              │
│       │                                                          │
│       ▼                                                          │
│ Consumers: _assert_at_port, navigate_to_building, flows, …       │
└─────────────────────────────────────────────────────────────────┘
```

LLMs (Claude for novel screens, Qwen as future fallback) are consulted
only when the deterministic layer reports low confidence or genuine
ambiguity — they are emergency reasoners, not the primary classifier.

## Scene families

Two families of UWO screens fall out naturally:

### Overworld family

- `port_overworld` — town view with 3D characters, NPCs, buildings.
- `sea` — sea view with 3D weather, ship animation.

Shared traits:
- 3D world content that changes continuously.
- Top-left **compound region** containing a big icon (lighthouse/ship)
  plus a row of small UI (flag + title + icon-bar).
- A right-side panel with tabbed lists (building / character / task / map).
- A Row 2 icon-bar with shield (always) + dynamic buffs.
- Level/progress strip bottom-centre.

### Chromed family

- `building` — interior of a building (Harbor, Market, Inn, …).
- `sub_menu` — a sub-screen inside a building (Purchase, Sell, …).
- `port_map` — the port map overlay.
- `world_map` — the world map screen.

Shared traits:
- Stable layout: top-left back-arrow + title + `?` tutorial icon.
- Top-right currency strip (in buildings/sub-menus) or home/hamburger
  variants.
- Left-side vertical menu of clickable items (buildings/sub-menus).
- Centre panel with main content (item grid, NPC dialog, settings, …).
- Right-side contextual panel (Cart / Hire / Sales List / City Info).
- Bottom action buttons row (Purchase / Sell / Confirm / Set Sail).

### Special cases

- **Main menu** is structurally distinct (left-side currency + supply +
  load panel) but UX-wise an *overlay* on an overworld scene.  Modeled
  as a `main_menu_overlay` mode-flag on overworld SceneModels, not a
  separate scene_kind.
- **Battle** scenes change but are much simpler than overworlds.
  Deferred for now.

### Overlays are not independent scenes  *(added 2026-05-18)*

Dialogs, popups, city-info panels, reward screens, transaction
confirmations, NPC dialogues — these are *not* separate base scenes.
They are events that occur on top of a base scene.  The City Info
panel sits on top of `world_map`.  Transaction confirmation dialogs
sit on top of `sub_menu:purchase`.  An NPC dialogue sits on top of
`port_overworld`.  Their meaning is bound to where the bot was when
they fired.

The right model is a tuple:

```
BotState = (base_scene, optional_overlay)
   base_scene  ∈ overworld | chromed family members
   overlay     ∈ None | city_info | transaction_dialog | reward |
                 building_npc_overlay | discovery_notice | system_notice | …
```

This collapses the taxonomy.  Instead of 14 `dialog_*` scene types,
there are ~5 base scenes × a small overlay vocabulary, and the
planner's logic becomes "if overlay present, dismiss / resolve it
first; else act on base".  The FSM transition graph stays small —
overlays don't multiply the state space.

**Detection — OmniParser is the better tool.**

An overlay is structurally a dense cluster of elements inside a
central rectangle, with the surrounding area sparse.  OmniParser
gives every element's bbox, so the cluster is detectable in code:

  - N≥3 buttons/text inside a bounded region
  - convex hull < ~60% of the screen
  - element density inside the hull is much higher than outside

That returns the overlay bbox deterministically, every frame, free.

The translucent backdrop is itself a useful signal: OmniParser will
still detect elements *outside* the overlay rectangle (port building
plates, NPC speech bubbles).  Those elements identify the base
scene even when the overlay obscures the title region.

**Stage-2 pipeline becomes:**

  1. OmniParser elements → overlay-cluster detector → optional
     overlay bbox + content tokens.
  2. Elements *outside* the overlay bbox → SceneModel — recovers the
     base scene.
  3. Moondream fallback only when OmniParser is too sparse to
     decide (the empirical 32% no-zone-elements case from the
     big-icon audit).

**Consequence — base scene memory.**

When an overlay fully obscures the base scene's identifying chrome,
the bot must *remember* the base from the previous tick rather than
re-derive it.  This is the persistent-state requirement from
`docs/memory_and_agent_architecture.md` ("Bridge 1 — observation
contract"); it shows up here as a concrete need, not a planning
nicety.  The `BotObservation` record (see that doc) is the
canonical home for `last_known_base_scene`.

## Data shape

`SceneModel` uses **composition with optional fields** rather than class
inheritance.  Each region detector populates its slot; family-specific
slots stay `None` when not applicable.

```python
@dataclass(frozen=True)
class SceneModel:
    scene_family:  Literal["overworld", "chromed", "world_map", "unknown"]
    scene_kind:    str                # e.g. "port_overworld", "building:market"
    confidence:    Literal["high", "medium", "low"]
    signals_matched:   list[str]      # ["top_left_lighthouse", "flag_present"]
    signals_disagreed: list[str]      # populated only when detectors conflict

    # Universal (always populated when detected; None otherwise)
    bottom_chrome:     BottomChromeRegion  | None
    top_left:          TopLeftRegion       | None
    obstructions:      list                = field(default_factory=list)

    # Overworld-only
    overworld_world:   OverworldWorldRegion  | None = None
    row_2_buffs:       Row2BuffsRegion       | None = None
    level_progress:    LevelProgressRegion   | None = None
    main_menu_overlay: MainMenuOverlay       | None = None
    overworld_panel:   OverworldRightPanel   | None = None

    # Chromed-only
    top_right_chrome:  TopRightChromeRegion  | None = None
    left_menu:         LeftMenuRegion        | None = None
    center_panel:      CenterPanelRegion     | None = None
    right_panel:       RightContextPanel     | None = None
    action_buttons:    ActionButtonsRegion   | None = None

    # World-map-only
    world_map:         WorldMapRegion        | None = None
```

`TopLeftRegion` itself is family-aware — same slot, different content
per family:

```python
@dataclass(frozen=True)
class TopLeftRegion:
    family:        Literal["overworld", "chromed", "world_map", "unknown"]
    # Overworld:
    big_icon:      Literal["lighthouse", "ship"] | None = None
    flag_present:  bool = False
    flag_nation:   str | None = None
    # Chromed:
    back_arrow:    bool = False
    tutorial_q:    bool = False     # the '?' in a circle next to title
    # Universal:
    title:         TitleField | None = None     # {text, bbox, raw_ocr}
```

## Scene classification rules

The classifier is small and deterministic:

| `top_left.big_icon` | `top_left.back_arrow` | `top_left.title` | → scene_kind |
|---------------------|------------------------|------------------|--------------|
| `lighthouse` | — | `<port name>` | `port_overworld` |
| `ship` | — | `<waters name>` | `sea` |
| — | True | `"World Map"` | `world_map` |
| — | True | `<building name>` | `building:<name>` |
| — | True | `<sub-menu name>` | `sub_menu:<name>` |
| None | None | — | `unknown` |

When two top-level signals agree, `confidence=high`.  When only one
signal is present, `confidence=medium`.  When detectors conflict (e.g.
lighthouse detected AND back-arrow detected), `signals_disagreed` is
populated and `confidence=low` — the caller may invoke an LLM to
disambiguate or fall through to the legacy classifier.

## Build order (slices)

Each slice is a single PR with its own tests and validation against the
labelled-frame corpus.

### Slice 1 — Substrate + universal anchors (this slice)

- `vision/scene_model.py`: dataclass hierarchy.
- `vision/region_detectors/__init__.py`: package init.
- `vision/region_detectors/bottom_chrome.py`: wifi/uid/server detector.
- `vision/region_detectors/top_left.py`: family-aware top-left detector.
- `vision/scene_classifier_v2.py`: combines regions → SceneModel.
- `tests/test_scene_model.py`: tests against 4 labelled frames.

Outcome: SceneModel produced alongside existing `classify_screen` output.
Both are logged; consumers untouched.

### Slice 2 — Left menu detector

Enables reliable reading of menu items in chromed scenes (Purchase, Sell,
Recruit Crew, Supply, …).  Replaces `_collect_menu_candidates` in
explore_actions.

### Slice 3 — Action buttons detector

Reads the bottom-row positive action buttons (Purchase, Sell, Confirm,
Set Sail, Recruit, …).  After this slice, every flow you can name has
its three required region detectors (`top_left`, `left_menu`,
`action_buttons`) and can be driven end-to-end without learned
fingerprints.

### Slice 4 — Right panel detector

Building list (overworld), Cart (Market/Item Shop), Hire panel (Inn),
Sales List (Sell sub-menu), City Info (World Map), Insurance Info (Bank).
Variant-aware: detector returns one of several shapes based on parent
scene_kind.

### Slice 5 — Consumer migration

Migrate `_assert_at_port`, `exit_to_overworld`, `navigate_to_building`,
and the explore_port orchestrator to consume SceneModel.  Legacy
classifier path becomes a fast-path cache for already-validated screens.

### Slice 6+ — Overworld content

`OverworldWorldRegion` (NPC bubbles, characters), `Row2BuffsRegion`
(shield + buffs), `LevelProgressRegion`, `MainMenuOverlay` mode flag.
Lower priority — these enable richer reasoning but are not load-bearing
for the trade/recruit/supply/sail flows.

## Flow → region mapping

The slice priorities are calibrated against the key flows.  Each flow
needs a specific subset of regions:

| Flow | Required regions |
|------|------------------|
| **Trade — purchase** | top_left, left_menu, center_panel, right_panel (Cart), action_buttons |
| **Trade — sell** | top_left, left_menu, center_panel, right_panel (Sales List), action_buttons |
| **Recruit (Inn)** | top_left, left_menu, right_panel (count input), action_buttons |
| **Supply (Harbor)** | top_left, left_menu, action_buttons |
| **Sail (World Map)** | top_left, world_map, right_panel (City Info), action_buttons |
| **Repair (Shipyard)** | top_left, left_menu, action_buttons |
| **Restore (Shipyard)** | top_left, left_menu, action_buttons |

The three highest-leverage detectors are `top_left`, `left_menu`,
`action_buttons` — they appear in every flow.  `right_panel` and
`center_panel` are secondary (used for verification + value reading,
but the bot can tap reasonable defaults when they're absent).

## Co-existence with the existing classifier

Slice 1 ships as **additive**: SceneModel is produced alongside the
existing `classify_screen` output.  Both are logged so we can compare
in real runs.  Consumers (perceive, navigate_to_building, …) continue
using the existing classifier.  This is a deliberate phase-gate to
build empirical confidence before any migration.

Migration begins at slice 5 once the labelled-corpus test pass rate is
high and live-run agreement-rate is observed.

## What this changes about the legacy stack

After all slices land and migration completes:

- **Fingerprint registry** becomes a fast-path cache, not authoritative.
  Bag-of-words signals are deprecated in favour of structural region
  detectors.
- **Qwen on the perceive hot path** is removed.  Qwen may still be
  invoked for novel-screen analysis as a fallback to Claude when the
  scene model reports low confidence.
- **Chrome detector** (template + position checks) is replaced by the
  structural region detectors.  Templates remain in `vision/assets/`
  as inputs to the detectors, not as primary classifiers.
- **`where_am_i` style location queries** read from the SceneModel,
  not from the union of chrome flags + Qwen prose.

## Open questions deferred to specific slices

- Sea-region info dialog (tapping the ship icon): not yet captured; new
  scene_kind to be added when we have a frame.
- Main menu opened over overworld: mode flag + detector to be sized in
  slice 6 once we have labelled main-menu-over-overworld frames.
- Battle scenes: deferred indefinitely.

## Decision: composition over inheritance

We use a flat dataclass with optional fields rather than a class
hierarchy because:

- Many regions are shared across families (top_left, bottom_chrome,
  obstructions, right_panel — though the right_panel content differs).
- Adding a new family later would require multiple inheritance or
  awkward overrides.
- Consumers that only need one region (e.g. `_assert_at_port` only
  needs `top_left`) shouldn't have to know about family hierarchies.
- Composition makes "is this region present?" a clean `is not None`
  check.
