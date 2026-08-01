# Dialog & Event/Perk Models — typed overlay perception

Status: design draft 2026-05-18.  No code yet.

## Why this exists

The bot's current obstruction handling is a *bag-of-keywords pattern
matcher* (`memory/knowledge/fsm/interruptors.json`) plus a dispatch
table of one-off `_dismiss_*` functions in `brain/perceive.py`.  It
works, but three drags become more painful as the bot grows:

1. **No structural awareness.**  An interruptor pattern like
   `["recruit", "1000"]` fires on any frame containing those tokens
   — it can't distinguish "recruit confirmation dialog with X close"
   from "the word *Recruit* visible in a building list".  Same
   class of problem as the fingerprint registry.

2. **Dismissal logic scattered.**  `_dismiss_tap_ok`,
   `_dismiss_tap_decline`, `_dismiss_close_button`,
   `_dismiss_story_event`, `_dismiss_chained_dismissal` each
   re-derive what the obstruction is and where its buttons live.
   No single source of truth for "what is this popup, and how do I
   resolve it".

3. **Cannot compose with SceneModel.**  The interruptor pipeline
   and SceneModel run in parallel, neither aware of the other.  The
   calibration work surfaced this directly: overlays and
   interruptors are the *same thing* — UI layers that sit on top of
   a base scene — but the codebase models them as two separate
   pipelines with two separate registries.

The fix is the same shape as the SceneModel fix for scenes:
**typed component models with structural detection**, in place of
keyword-pattern matching.

Two models — because the shapes are genuinely different:

  - `DialogModel`  for bounded card overlays.
  - `EventPerkModel` for full-screen reward-collection takeovers.

## DialogModel — bounded card overlays

Every UWO dialog uses the same frame:

```
┌──────────────────────────────────────┐
│  ░░░ Title bar (dark brown) ░░░  [X] │  ← title_bar + close_button
├──────────────────────────────────────┤
│                                      │
│   Body text                          │  ← body_text
│   (optional reward icons, NPC bust)  │  ← optional regions
│                                      │
│   [ Confirm ]      [ Cancel ]        │  ← action_buttons (0..N)
└──────────────────────────────────────┘
```

The variants differ only in WHICH parts are present:

| Kind             | Title bar | X close | Body | Actions    | NPC bust | Dismiss action |
|------------------|-----------|---------|------|------------|----------|----------------|
| `informational`  | yes       | yes     | yes  | none       | no       | tap X          |
| `confirmation`   | yes       | yes     | yes  | Conf+Canc  | no       | caller decides |
| `system`         | yes       | yes     | yes  | OK         | no       | tap OK         |
| `reward`         | yes       | yes     | yes  | Claim/Cont | no       | tap Claim      |
| `quest_offer`    | yes       | yes     | yes  | Accept/Dec | sometimes | caller decides |

(Building NPC overlays are a sibling overlay type, not a Dialog kind —
see *Building NPC overlay* section below.)

### Data shape

```python
@dataclass(frozen=True)
class TitleBar:
    bbox:       tuple[int, int, int, int]
    text:       Optional[str]                # title text (single line)

@dataclass(frozen=True)
class DialogAction:
    label:      str                          # "Confirm", "OK", "Cancel"
    bbox:       tuple[int, int, int, int]
    is_positive: bool                        # gold-styled vs grey

@dataclass(frozen=True)
class DialogModel:
    bbox:           tuple[int, int, int, int]   # whole card
    title_bar:      Optional[TitleBar]
    close_button:   Optional[tuple[int, int, int, int]]  # X icon
    body_text:      tuple[str, ...]
    actions:        tuple[DialogAction, ...]
    npc_portrait:   Optional[tuple[int, int, int, int]]
    raw_bbox_density: float                  # cluster signal — for debugging

    def kind(self) -> DialogKind: ...
    def dismiss_action(self) -> Action: ...
```

### Detection signature

A DialogModel fires when ALL of:

1. A dense cluster of elements occupies a centred bounded region
   (convex hull < ~60% of the frame, density inside ≫ density
   outside).
2. AT LEAST ONE of these anchors is present:
   - X close icon in the cluster's top-right corner (the keystone
     signal — OmniParser detects corner icons reliably).
   - One or more action-verb buttons (`Confirm`, `OK`, `Cancel`,
     `Continue`, `Claim`, `Accept`, `Decline`, ...) inside the
     cluster.
   (Building NPC overlays are NOT a DialogModel anchor — they are a
  separate model.  See *Building NPC overlay* below.)

The X close anchor catches the informational dialogs that today
fall through (frame `0005_2156116746.png` — the calibration miss
that prompted this doc).

## Building NPC overlay  *(sibling model, not a Dialog kind)*

The label was renamed from `npc_dialogue` → `building_npc_overlay` on
2026-05-18 because the original name conflated two unrelated things.
The distinction:

  - **Building NPC initial scene** — when the player first enters
    a building, the NPC is *part of the scene*, not an overlay.
    The sub_menu UI is rendered around them and they don't block
    input.  This is NOT a dialog and should NOT fire DialogModel.
  - **Building NPC overlay** — transient overlay that appears
    AFTER a transaction completes (e.g. "The crew is ready" at the
    harbor; "Hmm; never mind." at the market sell screen).  The
    NPC art is drawn ON TOP of the sub_menu screen with speech
    text and (sometimes) a speaker-name button.  This IS a dialog
    that needs dismissal.

Real frame inspection (`0050_2203595297.png`, Market Sell screen
with NPC overlay):

  - Large central icon  cx=1181  cy=655  w=1124  h=612  (NPC art)
  - "Market Owner" button (speaker name)  cx=990  cy=705
  - Quoted speech text  "Hmm; never mind."  cx=1023  cy=768
  - "Let's not bring this up again."  cx=1105  cy=815

Structural anchors for `building_npc_overlay`:

  1. A large icon-typed element covering ≥ 20% of the screen
     area, biased toward the centre or right.
  2. Quoted-speech text (label contains terminal punctuation like
     `."`, `?` or starts with a capitalised conversational
     phrase) within the same vertical band as the NPC art.
  3. OR a speaker-name button — short label (≤ 3 words) ending
     in "Owner", "Official", "Captain", "Innkeeper", etc.

### Transient reward notification  *(not a DialogModel target)*

A small floating reward notification can appear briefly on top of
*any* building NPC overlay (or other base scenes).  It is **not**
specific to Mercator's Estate — that label was the one captured in
labels.jsonl, but the same notification can fire at any building.

Characteristics:

  - Non-blocking — does NOT prevent player input on the underlying
    scene.
  - Auto-dismisses after a couple of seconds.
  - The player (and the bot) has nothing to do — no tap required.

This matches the *overlay notice* category already documented in
`CLAUDE.md` (translucent banners that disappear on their own or
clear on tap).  DialogModel should NOT fire on this — it is not a
modal dialog and the bot should treat it as ambient signal, not an
interruptor.

Frame `2026-04-15_16-05-29/0045_1611495973.png` (labelled
`building_npc_overlay` with `notes: "Mercator Estate"`) shows the
underlying NPC overlay with the transient notification active.

If the DialogModel detector picks up the underlying NPC overlay
correctly when the transient notification is also present, that's
the right behaviour — the notification is noise that doesn't change
what the bot should do.

## Other overlay categories — new typed models needed

The Phase 1 spotcheck (2026-05-18) surfaced four additional overlay
shapes in the labelled corpus that are neither bounded-card Dialogs
nor Building NPC overlays.  Each needs its own typed model so the
existing detectors can stay strict.

### Story NPC conversation

UWO is an MMORPG with a story system.  Story conversations show an
NPC and dialogue text, but they are **not** Building NPC overlays:
they have a distinctive **top-right control bar** with
`Auto / Dialog History / Skip`.  Sometimes the NPC is a building
NPC (e.g. Union request dialogues), but the wrapping is the story
system, not the post-transaction overlay.

Structural signature:

  - Top-right control bar with three controls — `Auto`, `Dialog
    History`, `Skip` (the keystone signal).
  - NPC art + dialogue text — same as Building NPC overlay.
  - Tap-anywhere or tap-Skip to advance.

Suggested label: `story_npc_conversation`.  Three frames currently
labelled `building_npc_overlay` should be relabelled (notes:
"Story NPC conversation"); see Phase 1 spotcheck trace.

### Negotiation panel

Trade negotiation shows a **non-dismissable** info card in the
middle of the screen and **large action buttons on the right
side** (much bigger than regular dialog buttons).  The card has
no X close and no buttons inside it — the action buttons live
in a vertical column to the right.

Structural signature:

  - Centred info card (text-only, no X, no buttons inside).
  - Vertical column of LARGE action buttons on the right edge —
    significantly larger than the standard ~70 px dialog button.

Suggested label: `negotiation`.  Multiple frames currently labelled
`dialog_gameplay` belong here (notes: "negotiation"); see Phase 1
spotcheck trace.

### Level-up screen  *(own model, simpler than EventPerkModel)*

UWO has three level-up events that share a full-screen
celebratory layout but are structurally simpler than event/perk
popups — they have no tabs and no claim-action grid.  Tapping
anywhere outside the optional mate list dismisses the screen.

Variants:

  - **Company level up** — the company itself levels up.  Single
    headline message, no mate list.
  - **Company expertise level up** — one of Adventure / Trade /
    Combat expertises levels up.  Includes a mate list showing
    which mates also levelled up on the same expertise.
  - **Mate skill unlocked** — a mate reaches a level that unlocks
    a new effect / skill.  Single screen showing the unlocked
    capability.

Structural signature:

  - Full-screen takeover (no underlying chrome visible).
  - Headline text matching one of: `Level Up`, `Adventure Up`,
    `Trade Up`, `Combat Up`, `Skill Unlocked` (subtype switch).
  - Optional mate list (expertise variant) — a vertical list of
    mate entries occupying a bounded region.
  - **No tabs, no multi-item claim row** — that distinguishes it
    from `EventPerkModel`.

Dismissal — tap anywhere **outside the mate list bbox**.  The
runtime computes a tap-safe zone from the mate list bbox; if no
mate list is present, any tap dismisses.

Suggested model: `LevelUpScreen` (own file, sibling to DialogModel
and BuildingNpcOverlay).  Subtype enum is small and closed (the
three variants above), so a `Literal["company"|"expertise"|"mate_skill"]`
field is enough — no registry needed.

Why NOT EventPerkModel:

| | LevelUpScreen | EventPerkModel |
|---|---|---|
| Layout | Single message + optional list | Tabs + multi-item claim grid |
| Dismiss | Tap anywhere outside | Collect plan (claim each item) |
| Episodic record | Single event ("levelled up X") | Multi-item collection log |
| Subtypes | Fixed (3) | Open registry (new event types ship independently) |

Several frames currently labelled in mixed `dialog_*` categories
belong to LevelUpScreen; corpus cleanup deferred along with the
`dialog_reward` mixed-bag cleanup.

### Reward-collection labelling cleanup  *(deferred)*

The `dialog_reward` label in `data/labels.jsonl` is a mixed bag
— some frames are perk/event popups, some are sub-screens of
those, some are unaddressed popups, some are obsolete.  This will
be cleaned up when EventPerkModel ships in Phase 4.  Phase 1
DialogModel's `reward` kind classification is therefore evaluated
loosely against this label until then.

### Kind classification

`DialogModel.kind()` is a switch on which fields are populated:

```
no actions          ─► informational
actions={OK}        ─► system
actions={Confirm,Cancel}            ─► confirmation
actions={Claim}|{Continue+reward}   ─► reward
npc_portrait present                ─► building_npc_overlay
actions={Accept,Decline}            ─► quest_offer
```

When the same dialog could classify multiple ways (e.g. reward
with NPC bust), priority is: `building_npc_overlay > reward > … >
informational`.

### Dismiss policy

`DialogModel.dismiss_action()` returns the action the runtime
should take when the goal is "clear this dialog and continue":

```
informational  ─► tap close_button
system         ─► tap OK
reward         ─► tap Claim / Continue
building_npc_overlay   ─► tap Continue
confirmation   ─► raise — caller must decide Confirm vs Cancel
quest_offer    ─► raise — caller must decide Accept vs Decline
```

Callers that need to make a decision (e.g. "should we confirm this
recruit?") read `DialogModel.kind()` and the action labels
directly instead of asking a separate `_dismiss_*` function.

## EventPerkModel — reward-collection takeovers

This is **not a dismissal target**.  Event/perk popups are how
UWO delivers most goodies — login rewards, daily attendance, limited-
time events, trade-point rewards, achievement chests.  The bot's job
is to **collect**, not to skip.  Modelling these as "things to
dismiss" loses information; the bot should know what it received,
which is the foundation of episodic memory (Bridge layer in
`docs/memory_and_agent_architecture.md`).

### Visual signature (different from dialogs)

```
╔══════════════════════════════════════════╗
║  ┌──────────────────────────────────┐    ║
║  │ EVENT / PERK CARD                │ X  ║  ← takeover, often no
║  │  (large image, slogan, timer)    │    ║    bounded "card" — fills
║  ├──────────────────────────────────┤    ║    most of screen
║  │  [ tabs: Daily | Weekly | … ]    │    ║
║  ├──────────────────────────────────┤    ║
║  │  Reward 1: 5x Blue Gem  [Claim]  │    ║  ← multiple
║  │  Reward 2: 10x Repair  [Claim]   │    ║    claimable rows
║  │  Reward 3: 1k Ducats   [Open]    │    ║
║  └──────────────────────────────────┘    ║
║                                          ║
╚══════════════════════════════════════════╝
```

Common variants (initial taxonomy — designed to grow):

  - **Login reward** — one card, single Claim
  - **Daily attendance** — calendar grid, one Claim/day, ≥1 ready
  - **Trade Point reward** — random-roll animation, one Claim
  - **Achievement chest** — grid of chests, multiple Claims
  - **Event perk** — multi-tab, multiple sub-events, per-tier rewards
  - **Limited-time offer** — IAP-styled, often with X to decline
  - **Anniversary / seasonal** — bespoke layouts

The base recognition signature is intentionally broad:

1. Large card filling ≥40% of the screen.
2. ≥1 element labelled with a reward-collection action verb
   (`Claim`, `Open`, `Receive`, `Collect`, `Get`, `Free`).
3. OR a tab bar at the top of the card with multiple tab labels.

The runtime then picks the matching *sub-type* for richer
extraction.

### Data shape — designed for growth

```python
@dataclass(frozen=True)
class RewardItem:
    """A single goodie the popup is offering."""
    label:       str                # "Blue Gem ×5", "Repair Kit ×10"
    quantity:    Optional[int]
    icon_bbox:   Optional[tuple]
    claim_btn:   Optional[tuple]    # the Claim button for THIS row
    claimed:     bool               # greyed-out / already-claimed state

@dataclass(frozen=True)
class EventPerkModel:
    bbox:            tuple
    subtype:         EventSubtype    # see below — enum-like, growable
    title:           Optional[str]   # "Daily Attendance", "Anniversary"
    close_button:    Optional[tuple]
    tabs:            tuple[str, ...] # tab labels, empty when no tabs
    active_tab:      Optional[str]
    items:           tuple[RewardItem, ...]
    primary_action:  Optional[DialogAction]  # the big "Open" / "Skip"
    metadata:        dict            # subtype-specific extras
```

`EventSubtype` is a Literal string (not a closed enum) so adding new
event types doesn't break the model:

```python
EventSubtype = Literal[
    "login_reward",
    "daily_attendance",
    "trade_point_reward",
    "achievement_chest",
    "event_perk",
    "limited_offer",
    "anniversary",
    "unknown",       # base detector fired, subtype detector hasn't
]
```

The base detector identifies the model fires; subtype detectors
each examine `EventPerkModel` candidates and refine.  Subtype
detectors are registered, not hard-coded:

```python
register_event_subtype(
    "trade_point_reward",
    detect_fn=lambda epm: ("trade point" in (epm.title or "").lower()
                            or epm.title == "Reward Roll"),
    extract_fn=_extract_trade_point_specifics,
)
```

This is the **expandability hook**.  Adding a new event type is a
single registration: a detect fn (does this popup match the
subtype) and an extract fn (pull subtype-specific fields into
`metadata`).  No edits to existing code; the registry composes.

### Collection action — not just dismissal

`EventPerkModel.collect_plan()` returns an ordered list of actions
to harvest the popup:

```
[ tap Claim for item 1,
  tap Claim for item 2,
  switch to tab "Weekly",
  tap Claim for item 3,
  tap Close ]
```

This is closer to a *flow* than a single dismissal.  The runtime
runs this plan and writes a structured episodic record:

```json
{
  "kind": "event_collection",
  "subtype": "daily_attendance",
  "timestamp": "...",
  "items_collected": [
    {"label": "Blue Gem", "quantity": 5},
    {"label": "Repair Kit", "quantity": 10}
  ],
  "duration_s": 4.2
}
```

This is *the* mechanism by which the bot will learn what it owns
and what events it has claimed today — a layer-(c) episodic memory
write, populated automatically by perception, no separate logging
required.

## How these fit into perception

```
OmniParser elements ─┐
                     │
                     ├─► DialogModel detector  ─┐
                     │                          │
                     ├─► EventPerkModel detect ─┼─► overlay slot
                     │                          │
                     ├─► (future overlay types) ┘
                     │
                     └─► SceneModel base detector
                            (runs on elements outside overlay bbox)
```

`SceneModel.overlay` becomes a tagged union:

```python
SceneModel.overlay: Optional[Union[DialogModel, EventPerkModel,
                                    OtherOverlay]] = None
```

Downstream consumers branch on the model type:

```python
if isinstance(scene.overlay, DialogModel):
    runtime.handle_dialog(scene.overlay)
elif isinstance(scene.overlay, EventPerkModel):
    runtime.collect_event(scene.overlay)
```

The current `Overlay` placeholder in
`vision/region_detectors/overlay.py` becomes the structural
detector that returns the typed model.

## Migration plan

Phased, no big-bang rewrite.  Each phase is independently shippable
and reverts cleanly.

### Phase 1 — Read-only DialogModel
Compute `DialogModel` for every frame alongside the existing
interruptor pipeline.  Log when both fire to see whether they agree
on detection.  No behaviour change.  Acceptance bar: on the
labelled corpus, DialogModel fires on ≥95% of `dialog_*` frames and
disagrees with `_detect_interruptors` < 10% of the time.

### Phase 2 — DialogModel replaces overlay slot in SceneModel
Wire DialogModel into `vision/scene_model.py` as the source for
`SceneModel.overlay` whenever a dialog is detected.  The
calibration re-run measures the lift.  At this point the bot is
still using the old interruptor pipeline for dismissals — we have a
clean structural overlay signal but haven't migrated the runtime.

### Phase 3 — Migrate one `_dismiss_*` function at a time
For each existing dismissal handler, replace its keyword-pattern
match with `DialogModel.kind() == "..."` and its action logic with
`DialogModel.dismiss_action()`.  Delete the corresponding
`interruptors.json` entry once green.  Catches accidental
regressions because the rest of the pipeline still runs.

### Phase 4 — EventPerkModel + collection runtime
Build EventPerkModel base + the first two subtype detectors
(`login_reward`, `daily_attendance`).  Wire the collect_plan into
the runtime.  Write the episodic record on each collection.
Following subtypes (`trade_point_reward`, `achievement_chest`,
`event_perk`, …) ship as needed in subsequent phases — no schema
change required.

### Phase 5 — Retire keyword interruptors
Anything `DialogModel` / `EventPerkModel` covers gets deleted from
`interruptors.json`.  What's left is the genuine long tail
(unstructured overlays the typed models don't yet handle); those
keep using the learning hook but populate the typed-model
registries instead of the flat JSON blob.

## Open questions

- **NPC dialogue in port vs in building.**  The street NPC bubble
  taxonomy (`project_building_npc_overlay_taxonomy.md` memory) makes a
  distinction; should bubble-only NPC speech be a third model, or a
  `DialogModel` subtype, or stay outside the overlay system since
  bubbles don't actually block input?  Lean: keep bubbles out (they
  are non-blocking signs of NPC presence, not interruptors).

- **Confirmation dialog *with goal context*.**  A
  `confirmation`-kind dialog is ambiguous on its own — the bot
  needs to know whether to Confirm or Cancel based on what flow
  it was running.  This is the `GoalContext` integration; the
  dialog model itself stays neutral, but `runtime.handle_dialog`
  reads goal context to make the call.

- **Multi-step event collection failures.**  If the bot's
  collect_plan stalls mid-sequence (e.g. one Claim button doesn't
  respond), what's the partial-progress semantic?  Episodic
  record needs `collected_items` to reflect what actually
  succeeded, not the plan.

## See also

  - `docs/scene_model_design.md` — base scene perception, overlay model
  - `docs/temporal_scene_classifier.md` — Stage-1 family + temporal aggregator
  - `docs/memory_and_agent_architecture.md` — Bridge 1 (observation contract)
    and layer-(c) episodic memory; EventPerkModel collection is the first
    real producer of structured episodic records
  - `brain/perceive.py` — current interruptor pipeline (to be migrated)
  - `memory/knowledge/fsm/interruptors.json` — the keyword registry being
    progressively retired
