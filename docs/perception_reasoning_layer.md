# Perception Reasoning Layer (L2.5)

> **STATUS — STILL CURRENT (2026-05-02).**  This document describes a
> perception primitive (the L2.5 Qwen layer for richer scene
> understanding) used by the planner architecture in
> [`planner_architecture.md`](planner_architecture.md).  No revision
> needed; the L2.5 layer feeds into both light and heavy verification
> as one of the perception inputs.

## Problem: Keyword-Based Perception Is Brittle

The current `perceive()` pipeline determines where the bot is by scanning OCR tokens
for a small set of hardcoded keywords.  This works for clearly distinct screens but
fails when two screens share surface text.

**Concrete example — Recruit Crew vs. Market Purchase:**

Both screens show:
- A composite action button at the bottom-right: `[amount] [action label]`
- A right panel with a scrollable list
- Ducat amounts throughout
- Sub-menu tabs at the top-left

The only reliable distinguishing signals are:
- The highlighted tab label ("Recruit Crew" vs. "Purchase")
- The right panel title ("Recruit Crew" vs. "Trade Goods on Sale")
- The action button label ("Recruit" vs. "Purchase")

Current keyword detection may match market keywords on the Recruit Crew screen
(numbers, "purchase" appearing elsewhere in the harbor tab list) and classify the
state incorrectly.  The bot then acts on the wrong perceived state.

**Root cause:** the keyword approach asks "does word X appear anywhere on screen?"
instead of "what is the highlighted sub-menu and what does the action button say?"
It ignores navigation context that makes screen identity unambiguous.

This problem is not limited to buildings.  Every navigation state has context that
keyword matching ignores:

| State | What keywords miss | What context provides |
|---|---|---|
| Building sub-menu | Which tab is highlighted | Active sub-menu id, available actions |
| Port overworld | Which overlays are active | Discovery notice text, player proximity, event banners |
| Sea | Which HUD elements are active | Region name, cinematic vs. active, encounter type |
| World map | What is selected | City name, route highlighted, zoom level |
| Main menu | Which item is focused | Current menu path |
| Loading | What destination | Transition target (city, sea, port) |

---

## Proposed Solution: L2.5 — Local LLM Reasoning

Add a reasoning layer between OCR (L2) and the KB cache (L3) that runs on **every
navigation state**, combining:

1. The full OCR token dump from the current frame
2. The coarse nav_state already identified by L0/L1 (sea / building / port_overworld /
   world_map / loading / unknown)
3. KB context relevant to that nav_state — building sub-menus, known port names,
   sea region names, menu structure

A **text-only local LLM** (Qwen-7B or 14B) reasons over this structured input and
returns a precise, structured description of the current state: where the bot is,
what is active, what action is available.

### Why text-only, not vision

This is a **context reasoning problem**, not a vision problem.  OCR already extracts
all the text on screen.  The LLM needs to answer questions like:
- "Given these OCR tokens and the Harbor sub-menu structure, which tab is active?"
- "Given these OCR tokens and the known sea region names, which region is the HUD showing?"
- "Given these OCR tokens and the known port list, which city is selected on the world map?"

Text-only reasoning over structured OCR output is faster, cheaper, and more reliable
than asking a vision model to interpret pixel positions.  Vision (Claude API) stays
as the fallback for truly novel screens the bot has never seen.

---

## Revised Vision Pipeline

```
Screenshot (2400×1080)
        │
        ▼
L0  MobileNetV3          ~0.1s  local   What scene type? (sea / building / port_map …)
        │
        ▼
L1  Chrome detector      ~0.1s  local   Which chrome buttons are visible?
                                         → confirms nav_state, refines L0
        │
        ▼
L1.5 OmniParser          ~2s    local   Where is every tappable element?
                                         → bounding boxes + labels
        │
        ▼
L2  OCR (PaddleOCR)      ~0.5s  local   Full text token dump from entire frame
                                         (titles, tabs, labels, prices, notices,
                                          HUD elements, player names, overlays)
        │
        ▼
L2.5 Qwen LLM  ◄── NEW   ~1-3s  local   Context reasoning over ALL nav states
        │                                Input:  OCR tokens
        │                                      + coarse nav_state from L0/L1
        │                                      + KB context for this nav_state
        │                                Output: structured PerceiveResult fields
        │                                        { detail, sub_menu, flow_hint,
        │                                          overlays, confidence }
        │
        ▼
L3  KB cache             instant free   Have we seen this exact state before?
        │                                Hit  → return cached SceneInventory
        │ miss only                       Miss → escalate to L4
        ▼
L4  Claude Vision API    ~5s    paid    First visit only → SceneInventory cached
```

---

## L2.5 Context Per Nav State

L2.5 receives different KB context depending on what L0/L1 determined.
Each nav_state has a corresponding context type:

### Building
```
Nav state: building
Building type: harbor
Known sub-menus: Supply, Repair, Recruit Crew, Departure
Known flows: departure → harbor_departure, recruit_crew → harbor_recruit_crew

OCR tokens: [...]

What sub-menu is active? What action is available? Are any dialogs open?
```

### Port overworld
```
Nav state: port_overworld
Current port: London (from top-left OCR)
Known buildings: Harbor, Market, Castle, Bank, Cathedral, Bureau

OCR tokens: [...]

Are any overlay notices visible (discovery, achievement, level-up)?
Is the bot near a building entrance? Which building?
Are there any blocking dialogs?
```

### Sea
```
Nav state: sea
Known sea regions: Mediterranean, North Sea, Indian Ocean, ...
Known ports reachable from current position: [from routes KB]

OCR tokens: [...]

Is this active sailing or cinematic (idle)?
Is a sea region name or arrival notice visible?
Is there any encounter or event banner?
```

### World map
```
Nav state: world_map
Known ports: [list from KB]
Known sea regions: [list from KB]

OCR tokens: [...]

Is a city selected? Which one? Is a route highlighted?
Is a City Info panel open?
```

### Main menu
```
Nav state: main_menu
Known menu items: [from KB]

OCR tokens: [...]

Which menu item is focused or highlighted?
```

### Loading
```
Nav state: loading

OCR tokens: [...]

What is the loading target? (city name, "Preparing for Voyage", etc.)
```

---

## L2.5 Output Contract (unified across all states)

```json
{
  "detail": "building: harbor — recruit crew (953 crew, 218,714 gold)",
  "sub_menu": "recruit_crew",
  "flow_hint": null,
  "overlays": [],
  "confidence": "high"
}
```

| Field | Meaning | Used by |
|---|---|---|
| `detail` | Replaces `where_am_i()` detail string; human-readable | `PerceiveResult.detail`, logging |
| `sub_menu` | Active sub-menu id within current building/state | `_detect_active_flow`, blocking signal resolution |
| `flow_hint` | Suggested flow id if L2.5 thinks a transaction is in progress | `_detect_active_flow` cross-check |
| `overlays` | List of active overlay ids (notices, popups) that L2.5 can read from OCR | interruptor detection |
| `confidence` | `"high"` or `"low"` — low triggers L4 Claude reclassification | `recover_to_port_overworld` |

The `detail` and `confidence` fields drop directly into `PerceiveResult`.
`sub_menu`, `flow_hint`, and `overlays` are consumed by `_detect_active_flow` and
the interruptor detection pass.

---

## Relationship to Existing Components

| Component | Before L2.5 | After L2.5 |
|---|---|---|
| `where_am_i()` | Keyword scan → `detail` string | L2.5 `detail` (richer, context-aware) |
| `_detect_active_flow` | Keywords per step in `flow_detection_order` | L2.5 `sub_menu` + `flow_hint` → KB flow map |
| `_detect_interruptors` | Keywords in OCR centre strip | L2.5 `overlays` (reads full screen in context) |
| `reclassify_with_claude` | Fallback when stalled | Fallback when L2.5 `confidence=low` |
| Claude API (L4) | First-visit scene understanding | Still first-visit only; L2.5 handles all repeat visits |
| KB context | Not used by perceive | L2.5 primary input for every call |

Flow detection shifts from keyword scanning to:
1. L2.5 identifies active sub-menu + flow_hint from OCR + context
2. KB maps sub-menu → flow id
3. Step-level keyword detection (basket/confirm/negotiate/result) still applies for
   overlay dialog states — those are on top of the sub-menu, not replacing it

---

## Cost Model

| Scenario | Current | With L2.5 |
|---|---|---|
| Any state — known | L0+L1+keywords: ~0.3s | L0+L1+OCR+Qwen: ~4s |
| Any state — unknown | Claude API: ~5s + cost | L2.5: ~3s free; Claude only on L3 miss |
| Any state — stalled | Claude API: ~5s + cost | L2.5 confidence=low → Claude; usually avoided |
| Building sub-menu confusion | Claude API each stall | L2.5 resolves without API |
| Sea region / overlay notice | Keyword miss → wrong state | L2.5 reads full OCR in context |

The per-call cost is ~3–4s of local Qwen inference per perceive cycle.  The perceive
cycle already takes ~3s due to OmniParser, so the wall-clock increase per tick is
modest.  The more significant gain is in correctness: states that currently require
a Claude API call (stalls, misclassifications) are handled locally.

---

## KB Context Structure

For L2.5 to provide context for every nav_state, the KB must supply:

**Buildings** (`building_types/<type>.json`) — sub-menus and flow map:
```json
{
  "building_type": "harbor",
  "sub_menus": [
    { "id": "supply",       "labels": ["supply", "supplies"] },
    { "id": "repair",       "labels": ["repair"] },
    { "id": "recruit_crew", "labels": ["recruit crew", "recruit"] },
    { "id": "departure",    "labels": ["departure", "depart"] }
  ],
  "flows": {
    "departure":    "harbor_departure",
    "recruit_crew": "harbor_recruit_crew"
  }
}
```

**Ports** (`ports/<slug>.json`) — buildings list already exists; no change needed.

**Sea regions** (`strategy/growth.json`) — `sea_region_progression` already lists
region names; L2.5 uses these to recognise region announcements in HUD.

**World map** (`control/ui_signals.json`) — city/port name list for selected-city
recognition; to be added.

**Menu structure** (`control/ui_signals.json`) — `main_menu_keywords` already
exists; extend with menu item ids.

---

## Data Flywheel Integration

Every L2.5 call where `confidence=low` (or later corrected by L4) is saved to
`data/training/<nav_state>/` alongside L4's authoritative label.

- `data/training/building_sub_menu/` — building sub-menu classification
- `data/training/sea_state/` — sea region, cinematic vs. active, encounters
- `data/training/port_overworld/` — overlay notices, building proximity
- `data/training/world_map/` — selected city, panel state

When any category accumulates enough examples, Qwen can be fine-tuned on
game-specific labels, reducing L4 call rates further.

---

## Implementation Plan

1. **KB extension**: add `sub_menus`/`flows` to `building_types/*.json`; add port/sea
   region/menu context to `ui_signals.json` and `growth.json` where missing.

2. **Qwen integration** (`vision/qwen_perception.py`): thin wrapper around a local
   Qwen model (llama.cpp / Ollama).  Accepts `(nav_state, ocr_tokens, kb_context)`
   and returns the unified output dict.  Returns `None` gracefully if model
   unavailable so the pipeline falls back to current keyword behaviour.

3. **`perceive()` update**: after OCR (L2), call L2.5 for every nav_state.
   Use L2.5 `detail`, `sub_menu`, `overlays`, `flow_hint` to populate
   `PerceiveResult` instead of keyword scanning.  Fall back to keyword scan if
   L2.5 returns `confidence=low` or is unavailable.

4. **`_detect_active_flow` update**: use L2.5 `sub_menu` → KB `flows` map to
   identify the active flow; keep step-level keyword detection for overlay dialog
   states (basket / confirm / negotiate / result).

5. **`_detect_interruptors` update**: supplement keyword scan with L2.5 `overlays`
   field which reads the full screen in context.

6. **Blocking-signal resolution** (see `blocking_signals_design.md`): once L2.5
   correctly identifies sub-menus across all states, the bot can navigate
   autonomously within a building to resolve prerequisites.
