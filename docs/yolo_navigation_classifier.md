# YOLO Navigation Classifier — UWO-specific element detection

Status: design 2026-05-19.  No code yet.

## Why this exists

The bot today has two perception problems that share a structural root:

1. **State classification is slow on ambiguous frames.**
   When chrome is all-False (transition frames, in-world overlays,
   sea-after-depart), the classifier falls back to Moondream
   (~5–7 s per call) or commits to the wrong branch via single-word
   OCR.  Phase 3.3 mitigated this with a TTL+event cache, but the
   underlying cost is still high.

2. **Navigation is blind to proximity events.**
   When sailing near a port, a Quick Supply prompt appears for a few
   seconds — the bot misses it because perceive() polls every ~30 s
   while sailing.  When discovering a new port or village, OCR can't
   recognise the name (it's not in the KB), so the bot can't act on
   the first-visit signal.  These are not weakness of the existing
   detectors; they're *outside their problem domain*.

Both problems reduce to the same question: "is element X visible at
position Y right now?"  That's the question YOLO is designed to
answer, in ~50 ms.  A UWO-specific YOLO trained on a small set of
keystone visual classes is the missing tool.

## Two distinct uses of UWO-YOLO

| Use | Question answered | Trigger frequency |
|---|---|---|
| **Family classifier** | What kind of screen is this? | Every perceive() tick |
| **Navigation classifier** | What's near the player/ship right now? | Every tick while sailing or walking in port |

The two share a model and a labeling pipeline (one trained YOLO can
detect both class sets); they differ in *how the bot consumes the
output*.  Family classes feed `_classify_nav_state`.  Navigation
classes feed proximity-driven actions (Quick Supply, discovery,
nameplate tap-to-enter).

## Why YOLO is the right tool

| Bot needs to know… | Best tool | Why |
|---|---|---|
| What family is this screen? | YOLO | Fixed visual keystones (lighthouse / ship / mode_tabs / back_arrow) |
| What's near me right now? | YOLO | Same — proximity badges have fixed appearance regardless of port name |
| Is there a dialog visible? | YOLO | Static visual frame shape |
| What does the dialog say? | OmniParser + OCR | Dynamic text content |
| What good is in this tile and at what price? | OmniParser + OCR | Dynamic numbers |
| Is this tile recommended / soaring? | YOLO | Visual state badge, fixed appearance |
| Is the bot near a port / village / landmark? | YOLO | Proximity glyphs are visually distinct |
| What's the name of the port? | OCR | Reads the text inside the YOLO-detected nameplate |

YOLO does "WHERE / WHAT TYPE"; OmniParser does "WHAT IT SAYS"; they
compose.  Most bot decisions need both.

## Class taxonomy

### Family classes (Phase 4a — separate doc; sketched here)

  - `lighthouse_icon` — top-left, port_overworld
  - `ship_icon` — top-left, sea
  - `world_map_mode_tabs` — top centre of world_map (Explore / Route / Trade / Port)
  - `back_arrow_icon` — chromed screens

### Navigation classes (Phase 4b — this doc)

  - `port_nameplate_floating`
        A port's name floating above the horizon when the ship is
        within proximity range.  Visually a labelled banner above a
        port silhouette.
  - `village_nameplate_floating`
        Same shape, smaller settlement.  Distinct visual style
        (different icon / colour from port nameplates).
  - `landmark_indicator`
        Lighthouse, monument, ruin — anything that produces a
        Discovery event when the ship sails close.
  - `quick_supply_button`
        The standalone "Quick Supply" prompt that appears beside
        the port nameplate at sea.  Lets the bot resupply without
        docking.
  - `building_nameplate_proximity`
        The floating nameplate above a building when the character
        walks close to it on port_overworld.  Already used by
        `actions.sail_actions._tap_nameplate_if_visible`; YOLO
        version would be faster and not text-dependent.
  - `enemy_fleet_indicator`
        Hostile sail.  Triggers avoidance or engagement decision.
  - `player_fleet_indicator`
        Other player's fleet visible.  Mostly informational.
  - `discovery_overlay_banner`
        First-visit notification.  Replaces today's keyword
        interruptor with a structural detection.

The navigation classifier MAY also include the family classes in
the same model — they're complementary, share frames, and the same
training pipeline produces both.

## Training data pipeline

Same data-flywheel pattern as the rest of the bot: Claude is the
teacher; YOLO is the student.

### Bootstrap labeling

`tools/label_yolo_via_claude.py` (to write):

  1. Read all frames in `data/labels.jsonl`.  Filter to families
     where the navigation classes are relevant (port_overworld,
     sea, sea_cinematic, port_loading, port_arrival_overlay).
  2. For each frame, send to Claude Vision with the class list and
     ask for bounding boxes in YOLO format
     (`<class_id> <cx_norm> <cy_norm> <w_norm> <h_norm>` per line).
  3. Save labels to `data/training/yolo_uwo/<session>/<frame>.txt`.
  4. Copy each frame to `data/training/yolo_uwo/images/`.

Expected output: ~400 frames × ~3-8 boxes per frame = 1200-3200
labeled boxes.  Claude API cost: ~$5-15 for the whole batch.

### Additional capture pass (deferred — Phase 4b post-ship)

Real-time-only events (Quick Supply prompt, discovery overlay) need
deliberate capture.  A `tools/capture_proximity_events.py` script
would:

  - Run the bot on a known route (London → Lisbon → Las Palmas → …)
  - Record every screenshot while sailing
  - Tag frames where Quick Supply / discovery overlay appeared
  - Add to training set

This is deferred until the base navigation classifier works on the
existing corpus.  Initial training relies on whatever proximity
events were captured incidentally during prior sessions.

### Training

YOLO11n (smallest variant) on Apple Silicon:

  - Input: 640×640 (resized from 2400×1080)
  - 4–9 classes
  - 200–400 training images, 20% held out for validation
  - ~30 min training on M-series GPU
  - Expected mAP: > 0.8 for keystone classes (they're visually
    distinctive); proximity classes ≥ 0.7 with the modest dataset.

### Distillation loop

When YOLO is uncertain (confidence < 0.5) on an element the bot
needed, fall back to Claude.  Save the frame + Claude's answer to
`data/training/yolo_uwo/uncertain/`.  Retrain monthly.  Same
flywheel as market_tile.

## Integration

### Phase 4a — family classifier

In `_classify_nav_state`, before the chrome detector:

```python
yolo_family = vision.yolo_uwo.classify_family(frame)
if yolo_family.confidence >= 0.7:
    return mapping[yolo_family.label]    # port_overworld / sea / world_map / chromed
# else: fall through to rule-based + Moondream cache (today's pipeline)
```

This collapses the chrome / port_name / sea_HUD / Moondream chain
to a single fast detector for most frames.  Rule-based path stays
as the fallback for low-confidence YOLO outputs.

### Phase 4b — navigation classifier (this doc's focus)

Two integration sites:

  1. **`actions.sail_actions._wait_for_arrival`** — during sailing:
     each tick (slow polling fine for now), call
     `vision.yolo_uwo.detect_navigation(frame)`.  When
     `port_nameplate_floating` fires AND `quick_supply_button` fires:
     evaluate ducat cost vs supply level, tap if appropriate.
     **Deferred for now**: ship logs only; the bot keeps using the
     current dock-and-resupply path.  Once telemetry shows YOLO
     reliably detects Quick Supply, switch to active tapping.

  2. **`actions.sail_actions.navigate_to_building`** — already uses
     `_tap_nameplate_if_visible` which relies on building name OCR.
     Add a YOLO `building_nameplate_proximity` detection as a
     supplement: when the YOLO class fires AND the OCR'd name
     fuzzy-matches the target building, prefer the YOLO bbox
     (more accurate centre point for tap).

### Phase 4b deferred items (do NOT block initial ship)

  - Real-time Quick Supply tap during sailing (capture data first,
    decide cost/supply heuristic, then enable).
  - Active discovery-driven navigation (sail toward unknown
    nameplate to gain Adventure EXP).
  - Adaptive polling during sailing (drop from 30 s to 3 s) — this
    is a separate concern from YOLO itself and can wait.

For the initial ship, the navigation YOLO runs at the current
polling rate (~30 s), logs detections to telemetry, and the bot's
existing slower path keeps working.  Validation question: does
YOLO reliably detect proximity events when the polls happen to
catch them?  If yes, the real-time work in a follow-up phase is
justified.

## Architecture

```
brain/perceive.py:
   _classify_nav_state(frame)
       ↓
   vision/yolo_uwo.py
       ↓ classify_family() / detect_navigation()
   ┌────────────────────────────────┐
   │ trained YOLO model             │
   │ (data/models/yolo_uwo.pt)      │
   └────────────────────────────────┘
       ↓
   YoloUwoResult:
     family: { label, confidence }
     navigation: [
       { class, bbox, confidence } …
     ]
```

The YOLO model lives at `data/models/yolo_uwo.pt`.  At import time
the module loads it onto the same device OmniParser uses (mps).
First call is warm (model already in memory).  Subsequent calls
~50 ms.

The `vision/yolo_uwo.py` module exposes:

  - `classify_family(frame) → FamilyVerdict`
  - `detect_navigation(frame) → list[NavigationDetection]`
  - `detect_all(frame) → YoloUwoResult` (combines both)

Cached per `id(frame)` like OmniParser.

## Open questions

  - **Polling cadence** during sailing.  At 30 s, Quick Supply
    prompts that appear for only a few seconds will mostly be
    missed.  Adaptive polling is a separate phase.  For the
    initial ship, accept the miss rate and log it.

  - **Class set evolution**.  Bot will encounter new visual
    elements as it sees more of the game (combat screens,
    auction house, guild hall).  Plan: keep the class taxonomy
    flat and additive — new classes ship as YOLO retrains.

  - **Discovery semantics**.  When YOLO detects a brand-new
    nameplate the bot has never visited, what should it do?  This
    is a *policy* question, not a *perception* question, and it
    belongs in the bot's goal layer (`actions/self_grow.py`).
    Initially: just log it.  Later: route toward it when no
    higher-priority goal is active.

## What this doc does NOT cover

  - Real-time Quick Supply tap-while-sailing — deferred to a
    Phase 4b follow-up once data confirms YOLO is reliable here.
  - Adaptive polling during sailing — separate concern.
  - MarketTileModel state classes — separate concern (Phase 4d).
  - Combat / auction / guild perception — out of scope.

## Phasing recap

```
Phase 4a — family classifier YOLO (4 classes)         1 week
Phase 4b — navigation classifier YOLO (this doc)      1 week
Phase 4b.1 (deferred) — real-time Quick Supply tap    later
Phase 4b.2 (deferred) — discovery-driven nav          later
Phase 4c (deferred) — adaptive polling during sail    later
Phase 4d (deferred) — market-tile state classes       later
```

For now, ship: trained model + read-only detection + telemetry
logging.  Speed/realtime optimisations are explicitly OUT of scope
for the initial Phase 4b ship.  Correctness first.

## See also

  - `docs/temporal_scene_classifier.md` — earlier proposal for
    Moondream Stage-1 family arbiter, which YOLO supersedes.
  - `docs/scene_model_design.md` — SceneModel as the deterministic
    Stage-2; YOLO becomes its primary structural input.
  - `docs/dialog_and_event_models.md` — the typed overlay models
    that YOLO can accelerate (dialog frame detection, NPC bust).
  - `brain/moondream_family_cache.py` — current TTL+event cache
    for Moondream that YOLO supersedes for the common case.
