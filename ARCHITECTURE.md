# UWO Bot — Architecture Plan

## Vision

Build a bot that plays Uncharted Waters Origin (UWO) autonomously and **improves over time**.
The bot should handle the unpredictability of a live game UI — NPC overlays, dynamic content,
unexpected screens — without becoming brittle. It does this by combining specialized perception
models with a reasoning brain that accumulates knowledge from every session.

The human owner acts as a **supervisor and trainer**, not a programmer. A UI lets them review
what the bot found, answer questions it couldn't resolve, and edit the game knowledge it
reasons from — no code changes needed.

---

## Why Not a Simple FSM or Rule-Based Agent

A Finite State Machine (deterministic rules for each screen) breaks on edge cases the rules
didn't anticipate — NPC overlaps, unexpected dialogs, game updates, anti-cheat changes.
There are always new edge cases.

A pure LLM agent (ask Claude what to do every step) is flexible but slow, expensive, and
still gets stuck in loops without a feedback mechanism.

The right architecture is a **hybrid**:
- Specialized fast models for perception (what am I looking at?)
- A reasoning brain for decisions (what should I do?)
- A human-in-the-loop UI for anything the bot can't resolve confidently
- A learning loop so the bot improves with each session

---

## Overall System Map

```
┌─────────────────────────────────────────────────────────────────┐
│                         GAME (Android Phone)                    │
│                    ADB screenshots + input                      │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│                      CAPTURE PIPELINE                           │
│   continuous screenshot capture (manual play or bot mode)       │
│   deduplication (skip identical frames)                         │
│   timestamp + session tagging                                   │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│                    PERCEPTION PIPELINE                          │
│                                                                 │
│   Screen Classifier  →  screen_type + confidence score         │
│   OmniParser         →  detected elements + bounding boxes     │
│   OCR (PaddleOCR)    →  text labels, building names, prices    │
│                                                                 │
│   Output per frame:                                             │
│   { screen_type, confidence, elements[], text[], raw_image }   │
└──────────────┬────────────────────────────┬─────────────────────┘
               │ high confidence            │ low confidence /
               │                            │ unknown
               ▼                            ▼
┌──────────────────────┐        ┌───────────────────────────────┐
│   KNOWLEDGE BASE     │        │       REVIEW QUEUE            │
│                      │        │                               │
│  screen_types        │        │  frames needing human input   │
│  building_types      │        │  or Claude API answer         │
│  port records        │        │                               │
│  action outcomes     │        └──────────────┬────────────────┘
│  trade prices        │                       │
│  game rules          │                       │
└──────────────────────┘                       │
                                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                      SUPERVISOR UI                              │
│                                                                 │
│  ┌─────────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │  KNOWLEDGE      │  │  REVIEW      │  │  SESSION          │  │
│  │  EDITOR         │  │  QUEUE       │  │  BROWSER          │  │
│  │                 │  │              │  │                   │  │
│  │  add/edit/delete│  │  screenshot  │  │  timeline of      │  │
│  │  game rules     │  │  + question  │  │  captured frames  │  │
│  │  screen types   │  │              │  │                   │  │
│  │  building types │  │  you answer  │  │  filter by:       │  │
│  │  action policies│  │  or send to  │  │  · high conf ✓    │  │
│  │                 │  │  Claude API  │  │  · low conf  ⚠    │  │
│  └─────────────────┘  └──────────────┘  │  · unclassified ? │  │
│                                         └───────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  BOT CONTROLS                                            │   │
│  │  [ Manual + Capture ]  [ Bot Active ]  [ Capture Only ]  │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                            │
               answered questions + corrections
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                       BRAIN                                     │
│                                                                 │
│   Fine-tuned local LLM (e.g. Llama 3.1 8B / Phi-3)             │
│                                                                 │
│   Routine decisions  →  local model (fast, free)                │
│   Novel / uncertain  →  Review Queue or Claude API              │
│                                                                 │
│   Periodically retrained on accumulated labeled data            │
└─────────────────────────────────────────────────────────────────┘
```

---

## Components

### 1. Capture Pipeline

Runs continuously whenever the bot is active (any mode).

- Captures ADB screenshots at a configurable interval
- Deduplicates frames (skip if pixel diff < threshold — character animation, not a new screen)
- Tags each frame with: timestamp, session ID, operating mode
- Stores raw frames for later review and training

### 2. Perception Pipeline

Three specialized models run on each captured frame:

| Model | Task | Why specialized |
|---|---|---|
| **Screen Classifier** | Identify which screen type (port_map, building_interior, etc.) | Small fine-tuned CNN, <100ms, trained on UWO screenshots specifically |
| **OmniParser** (Microsoft) | Detect all interactable UI elements with bounding boxes | Pre-trained on UI grounding across apps/games; handles icons, buttons, text+icon combos |
| **PaddleOCR** | Read text labels (building names, prices, port names) | Purpose-built OCR, more reliable than general LLMs for text extraction |

Output per frame:
```json
{
  "screen_type": "port_map",
  "confidence": 0.94,
  "elements": [
    { "type": "building_icon", "label": "harbor",   "bbox": [880, 390, 950, 460] },
    { "type": "building_icon", "label": "market",   "bbox": [1060, 350, 1130, 420] },
    { "type": "button",        "label": "back",     "bbox": [30, 20, 90, 60] },
    { "type": "button",        "label": "world map","bbox": [60, 980, 200, 1040] }
  ],
  "text": ["harbor", "market", "bureau", "Narvik"]
}
```

### 3. Knowledge Base

Persistent structured storage of everything the bot knows. Three categories:

**Static game knowledge** — rules, screen types, building behaviours
```
game_rules/         game world, goals, activities, resources
screen_types/       what each screen type looks like and means
building_types/     what each building category does (cross-port)
action_policies/    which actions work in which situations
```

**Accumulated port knowledge** — discovered through play
```
ports/<slug>.json           buildings known in this port
buildings/<port>__<name>.json   what was found inside
markets/<port>__market.json     price history snapshots
```

**Action outcomes** — what worked and what didn't
```
outcomes/           (screen_type, action, result) tuples
                    feed into brain retraining
```

### 4. Review Queue

Frames the perception pipeline could not classify with sufficient confidence,
and frames where the brain could not decide what to do.

Each queue item contains:
- The raw screenshot
- What the perception pipeline guessed (and confidence)
- A specific question: "What screen type is this?" / "What should I do here?"

Resolution options:
- **Answer yourself** — pick from suggestions or type a new answer
- **Ask Claude API** — send screenshot + question to Claude, review answer before accepting
- **Skip** — defer to a later session

Accepted answers are written back to the KB and added to the training dataset.

### 5. Brain

The reasoning layer — takes structured perception output + KB context → decides action.

**Input at each step:**
```
[World knowledge]     internalized via fine-tuning — game rules, goals,
                      screen types, building behaviours, action space

[Current state]       injected fresh — screen_type, detected elements,
                      port, resources, visited buildings

[Episodic memory]     last N actions and their outcomes

[Retrieved KB]        relevant records for current screen/port/building
```

**Output:**
```json
{
  "action": "enter_building",
  "target": "cathedral",
  "method": "building_list",
  "reasoning": "Port map tap navigates only; building list triggers direct entry",
  "confidence": 0.91
}
```

**Model strategy:**
- Start: Claude API as brain (no training data yet, maximum capability)
- As labeled data accumulates: fine-tune a local LLM (Llama 3.1 8B or Phi-3) with LoRA
- End state: local model handles routine decisions; Claude API only for genuinely novel situations
- Fine-tuning cadence: after each session batch of ~500 new labeled frames

### 6. Supervisor UI

Three panels accessible from a local web UI:

**Session Browser**
- Timeline view of all captured frames in a session
- Filter by: classified (✓), low confidence (⚠), unclassified (?)
- Click any frame to see what the perception pipeline detected

**Review Queue**
- One frame at a time with the bot's question
- Quick-answer UI: pick from suggestions, type new category, or send to Claude
- Progress indicator: N items waiting

**Knowledge Editor**
- Add / edit / delete entries in all KB categories
- Screen types, building types, game rules, action policies
- Changes take effect immediately — no code deploy needed

---

## Operating Modes

| Mode | You do | Bot does |
|---|---|---|
| **Manual + Capture** | Play normally | Silently captures and classifies every frame |
| **Capture Only** | Not playing | Captures live screen, no input sent |
| **Bot Active** | Watch and supervise | Bot plays autonomously; you can intervene |

Manual + Capture is the primary way to build the initial training dataset.

---

## Knowledge Types and How They're Fed In

| Type | How encoded | When used |
|---|---|---|
| **Static world knowledge** | Included in every fine-tuning example — baked into model weights | Always available, no injection needed after training |
| **Dynamic game state** | Injected fresh each step from perception output + KB | Changes every step |
| **Episodic memory** | Last N (action, outcome) pairs injected as context | Prevents repeating failed actions |
| **Retrieved KB** | Relevant records queried from KB and injected | Port/building specific context |

---

## The Learning Loop

```
1.  Play manually with capture mode on
         ↓
2.  Perception pipeline classifies frames
    High confidence → auto-labeled, into KB
    Low confidence  → Review Queue
         ↓
3.  Review Queue: you answer in 1 click per item
    (or delegate to Claude API for harder ones)
         ↓
4.  Answers added to KB + training dataset
         ↓
5.  Periodic LoRA fine-tune on accumulated data
         ↓
6.  Brain handles more situations locally
    Review Queue gets shorter each session
         ↓
7.  Bot plays autonomously → captures more edge cases
    → back to step 2
```

The bot gets cheaper and more reliable over time.
New situations always have a resolution path: Review Queue → human or Claude answer → training data.

---

## Device Independence

A core design requirement: the bot must work on any device without code changes.
Switching from a phone to a tablet, or to a device with a different screen size or
aspect ratio, should require only a single config update.

**Why hard-coded coordinates fail:**
- Phone → tablet: resolution changes (e.g. 1080×2400 → 2560×1600)
- Different aspect ratios shift the position of every UI element
- Game UI scaling varies across screen densities
- Even on the same device, a system font size change can shift button positions

**How this design avoids hard coordinates:**

All element positions are **detected dynamically by OmniParser** at runtime.
The brain never has a hardcoded (x, y). Instead:

```
OmniParser detects:  { type: "minimap", bbox: [2100, 180, 2380, 320] }
Brain decides:       tap the minimap element
Executor computes:   tap centre of that bbox → (2240, 250)
```

The coordinate is derived from what is actually on screen, not assumed from a
previous device. If the minimap moves because the screen is larger, OmniParser
finds it in its new position automatically.

**Screen classifier is resolution-agnostic:**
A port map looks like a port map on any device. The classifier learns visual
patterns and relative layouts, not absolute pixel positions.

**OCR finds text wherever it appears:**
Building names and labels are found by reading the screen, not by cropping a
fixed region that may be wrong on a different device.

**The one thing that needs care — capture normalization:**

Perception models (screen classifier, OmniParser) are trained on screenshots at
a specific resolution. Raw captures from different devices will have different
pixel dimensions. The capture pipeline handles this with a single normalization
step:

```
Raw capture (any resolution)
    ↓
Resize to canonical resolution (configured per device, e.g. 1080×2400)
    ↓
Perception pipeline (always sees consistent input)
```

Switching devices means updating one config value — `CANONICAL_RESOLUTION` —
and recapturing a small number of labeled screenshots for any UI elements that
look visually different on the new device (e.g. different icon sizes).

**Design rule:** No (x, y) coordinate may appear in the brain or decision layer.
Coordinates only exist in the executor, and only after being computed from a
detected element's bounding box.

---

## Build Order

| Phase | What | Outcome |
|---|---|---|
| **1** | Capture pipeline + session browser | Can capture and review gameplay footage |
| **2** | Screen classifier (fine-tuned on manual captures) | Reliable screen type detection |
| **3** | OmniParser + PaddleOCR integration | Reliable element + text detection |
| **4** | Review Queue UI | Human-in-the-loop for unknowns |
| **5** | Knowledge Editor UI | Edit game rules without code changes |
| **6** | Brain v1 (Claude API) | Bot can reason and act |
| **7** | Action outcome recording | Data for brain training |
| **8** | Local LLM fine-tune pipeline | Brain runs locally, cheaper per step |
| **9** | Full autonomous mode + supervision UI | Bot plays, you supervise |

---

## Natural Language Interface

### Current Use: You Commanding the Bot

Rather than configuring tasks in code or a UI, you talk to the bot in plain language:

```
"go to Ceuta and buy 100 wheat"
"explore all buildings in this port"
"find the cheapest place to buy cloth and go there"
"tell me what the market here is selling"
```

The bot parses your intent, plans the steps, executes them, and reports back —
exactly like giving instructions to a human assistant.

### The Separation That Makes This Work

The natural language layer and the execution layer are kept strictly separate:

```
User input (natural language — never changes)
         ↓
Intent Parser
  Converts natural language to structured intent:
  { action: trade, destination: Ceuta,
    buy: [{ item: wheat, qty: 100 }] }
         ↓
Task Planner (brain)
  Breaks intent into executable steps:
  1. navigate_to_port("Ceuta")
  2. enter_building("market")
  3. buy_item("wheat", 100)
  4. verify_purchase("wheat", 100)
         ↓
Execution Adapter  ←── only this layer changes per game / platform
         │
         ├── UWO today         vision + ADB taps (current)
         ├── UWO with API      HTTP/REST calls
         ├── MUD               text commands ("go south", "buy wheat 100")
         ├── PC game           keyboard/mouse simulation
         └── Future NL game    send natural language directly to game
         ↓
Feedback to user: "Bought 100 wheat for 4,800 ducats in Ceuta"
```

The intent, the task plan, and the feedback are identical regardless of which
game is being played or how it is controlled. Only the adapter changes.

### Execution Adapter Pattern

Each adapter implements the same interface — a set of abstract game actions:

```
navigate_to_port(port_name)
enter_building(building_type)
buy_item(item, quantity)
sell_item(item, quantity)
check_prices()
sail_to(destination)
...
```

How each action is carried out is the adapter's concern:

| Game / Platform | Adapter mechanism |
|---|---|
| UWO mobile (current) | Vision detects elements → ADB tap on bounding box |
| PC game | Keyboard/mouse simulation via OS |
| MUD / text game | Exact text commands (`buy wheat 100`, `go north`) |
| Game with data API | Read game state via HTTP; control still via UI simulation |
| Future NL-native game | Forward natural language directly to game's AI layer |

Adding support for a new game means writing a new adapter — the brain,
task planner, knowledge base, and natural language interface are untouched.

### What Existing Games Actually Support

It is worth being precise here, because the reality is more limited than it
might seem.

| Game | What is exposed | What it actually means |
|---|---|---|
| **EVE Online API** | REST endpoints | Read-only data — market prices, character stats, corporation info. Cannot control character actions through the API. |
| **MUDs** | Text input | Exact command syntax — `go north`, `buy wheat 100`. Flexible phrasing like "head northward" fails. Closer to a CLI than natural language. |
| **WoW / FFXIV addons** | Lua API | Read game state and trigger specific game functions. Exact calls, not language. |
| **Roblox** | Lua scripting | For building game worlds, not for controlling a player character. |

**None of these accept true natural language as game input.** The full
spectrum from most rigid to most flexible looks like:

```
Most rigid                                          Most flexible
     │                                                    │
     ▼                                                    ▼
Read-only        Exact text       Macro/script     Natural language
data API         commands         syntax           (does not exist
(EVE)            (MUDs)           (WoW macros)      in any mainstream
                                                    game today)
```

The translation layer is therefore never optional. The bot always speaks
the game's language; your natural language is always translated first.

### Longer-Term Vision

The translation layer will remain necessary for any game that does not
natively accept natural language. What will change over time is the
sophistication of what games expose:

- More games will offer **control APIs** (not just read APIs), making
  adapters more reliable than vision-based tap simulation
- AI-native games will accept **structured intent** directly, reducing
  the adapter to a thin format conversion
- Some experimental games already use LLMs for NPC dialogue — extending
  that to player commands is a small step

When that happens, the adapter for those games becomes trivial. Everything
above it — the brain, task planner, KB, and natural language interface —
stays exactly the same.

The bot is therefore not a UWO-specific tool but a **general game assistant
framework**: same brain, same natural language interface, same knowledge
accumulation loop — plugged into different games via swappable adapters.

---

## Current State (April 2026)

The codebase has early implementations of several components:

- `capture/` — ADB screenshot capture working
- `vision/` — EasyOCR + llava 7B (to be replaced by PaddleOCR + OmniParser + screen classifier)
- `brain/agent.py` — LLM-based agent loop (to be replaced by structured brain)
- `brain/states/port_map.py` — port map navigation (reusable)
- `memory/knowledge/` — KB structure in place (ports, building_types, buildings, markets)
- `actions/` — ADB tap/swipe working

The existing code is a useful prototype for understanding game mechanics and edge cases.
The architecture above is the target to build toward.
