# Building an Autonomous Game Bot — Journey to Milestone 1: Port Discovery

*This is the first in a series of articles documenting the development of an autonomous bot
for the mobile game Uncharted Waters Origin (UWO). Each article covers one milestone:
the problems encountered, the design pivots, and what was ultimately achieved.*

---

## Where It Started — OCR and Rules

The project began with the simplest possible approach: **ADB screencap → EasyOCR → hardcoded coordinates**. The bot could read the port name from the top-left corner and tap fixed pixel positions to navigate buildings. Rules like "if screen title is 'Market', tap (70, 145) for Purchase" were written by hand after manually inspecting screenshots.

This worked for exactly the screens it was built for, and broke everywhere else.

---

## The First Wall — Navigation Fragility

The earliest problems were all variations of the same theme: **the real world doesn't match hardcoded assumptions**.

- **OCR returned garbage** on many screens. "Socotra" came back as "ccbaccot", "Cohasset" as "cohasset" (fine) or "cohasct" (not fine). A vowel-ratio heuristic had to be added just to filter obvious OCR noise.
- **Building list navigation** required scrolling, but the scroll amount was unpredictable — some ports have 4 buildings, some have 12. The bot would scroll past targets or stop short.
- **Port map navigation** was attempted as a cleaner alternative (tap the mini-map, get a clean overhead view, OCR building labels there). But the port map template matcher kept failing because the `world_map_btn.png` template included a clock ("21:02") in the corner that changed every minute. The template never matched. This went unnoticed for a long time because a saturation heuristic was masking the failure with a silent fallback.
- **Icon tap offset** was calibrated wrong — the bot was tapping building text labels instead of the icons 70px above them on the port map.
- **90-second steps** — every navigation poll called llava (a 7B vision model running locally) for scene classification. ~30 seconds per poll × 3 polls = a minute and a half to take one step. The bot was essentially unusable.

---

## The Second Wall — Understanding Screens

Once navigation became somewhat reliable, the bot could enter buildings — but it didn't know what it had found. The initial approach was to ask **llava:7b** ("describe this screen") and parse the answer.

llava hallucinated confidently and consistently. A Sanctuary was described as "a place to rest and hire sailors" (that's the Inn). An Item Shop was described as "buy/sell items from other ports" (that's the Market). The descriptions were plausible-sounding but wrong — the model was generating generic game text rather than reading the actual screen.

This exposed a fundamental design problem: **a general-purpose 7B model asked to understand a specific mobile game's UI will make things up**.

---

## The Pivots

### Pivot 1 — Speed: `_quick_classify`

The 90-second step problem was solved by separating polling from understanding. A lightweight `_quick_classify` function using only MobileNetV3 (L0, ~0.1s) and template matching (L1, ~0.1s) replaced llava for navigation polling. llava was only called once per significant state change. Steps dropped from 90s to ~5s.

### Pivot 2 — Accuracy: Sub-menu OCR

Instead of asking an LLM what a building does, the bot was taught to **read the left panel inside the building**. Every building in UWO shows a vertical list of its own service buttons (e.g. Bank shows "Exchange", "Deposit", "Withdraw"). OCR on that specific region gave ground-truth building descriptions with zero hallucination risk. The LLM was demoted to a last-resort fallback.

### Pivot 3 — Architecture: The Tiered Vision Pipeline

The biggest design pivot came from stepping back and asking: why is one model doing everything? The answer was a layered pipeline where each model answers only the question it's good at:

```
L0  MobileNetV3       → What scene type is this?          (0.1s, always)
L1  Chrome detector   → Which stable buttons are present?  (0.1s, always)
L1.5 OmniParser v2   → Where is every tappable element?   (16s, local)
L2  EasyOCR           → What does the text say?            (0.5s, on demand)
L3  Knowledge Base    → Have we seen this before?          (instant, free)
L4  Claude Vision API → What does it all mean?             (5s, once only)
```

**OmniParser** (Microsoft, 2024) — a YOLOv8 + Florence-2 combination fine-tuned on UI screenshots — was the key discovery. Unlike llava which described scenes narratively, OmniParser produces a structured list of every interactive element with pixel-accurate bounding boxes. It doesn't know what game this is or what anything means. It just finds things.

**Claude Vision API** receives OmniParser's element list as a text table (not the raw image) and provides game-context understanding: "the right panel list items are direct building entry buttons; tapping 'Market' at (2091, 516) opens the market". This result is cached forever — the same scene never triggers another API call.

The division of labour: **OmniParser detects, Claude understands, the brain executes**.

The market screen illustrated why this matters. Pure OCR on a screen full of tiles, timers, cargo panels, and price indices returned nonsense: "on Sale 00.21.04 Purchase", "05/11/2026/00.00.00 — index 107%". Claude, given OmniParser's element positions and a thumbnail of the screen, correctly identified: 6 trade goods, their buy prices, which were sold out (restock timers vs prices), the cargo panel on the right to ignore, and the global restock date to discard.

---

## What Was Achieved

### Infrastructure

- Full ADB capture → tiered vision → knowledge base → action loop
- MobileNetV3 scene classifier (19 classes) trained on labeled game screenshots
- Chrome template matcher for deterministic detection of stable UI elements
- OmniParser v2 running locally on Apple Silicon MPS, ~16s per frame
- Claude Vision API integration with JSON parsing and `SceneInventory` caching
- EasyOCR + PaddleOCR for text (Latin and CJK/Cyrillic for player names)

### Knowledge about Socotra

- All 8 buildings discovered and recorded with their services
- `SceneInventory` for the port overworld: 44 elements, all buildings with exact tap coordinates
- Market fully read: 6 purchase goods with prices, price indices, and sold-out status; 3 sell goods from current cargo; timestamped and stored in the market knowledge base

| Good | Buy Price | Index | Status |
|---|---|---|---|
| Date Palm | 499₫ | 96% | Specialty (sold out) |
| Dracaena | 889₫ | 108% | Specialty (sold out) |
| Magnetic Iron Ore | 753₫ | 95% | Specialty (sold out) |
| Ambergris | 1284₫ | 102% | Available |
| Pomegranate | 255₫ | 103% | Available |
| Arak | — | — | Sold out (no restock) |

### The "Learn Once, Reuse Forever" Principle

- First visit to any scene → Claude API called once → `SceneInventory` written to disk
- Every subsequent visit → instant cache hit, no API call, exact tap coordinates available
- Building-type understanding transfers across ports: a Bank encountered in a new city is immediately understood from the Bank record, without re-calling Claude
- Market prices are re-read on every visit (they fluctuate), but structural understanding is permanent

---

## Where It Stands

The system has gone from "tap hardcoded coordinates and hope" to a self-teaching pipeline that genuinely understands game UI. The knowledge base grows with every port visited, and the API call rate trends toward zero as more scenes are learned.

The first price data is in. Socotra's specialties — Date Palm, Dracaena, Magnetic Iron Ore — are goods produced here and sold cheaply back to Socotra. The trading thesis: buy them here, sail far, sell where demand is high. Longer distance means higher profit.

**Milestone 2** is sea navigation: open the world map, sail to a new port, read its market, and compute the first real profitable trade route. That is where the bot stops exploring and starts earning.
