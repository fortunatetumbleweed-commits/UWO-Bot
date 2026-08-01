# Phase 6 — Fingerprint Coverage Report

*Generated 2026-05-03 from `python -m vision.fingerprint_survey` against
`data/labels.jsonl` (793 labelled frames across 24 screen types).*

The OmniParser-driven fingerprint registry replaces the chrome-template +
fixed-crop-OCR + Moondream chain as the primary state classifier.  Each
fingerprint is data-derived: signals come from running OmniParser
`parse_fast` on labelled frames and extracting cross-frame-stable
region-keyed label sets.

## Validation against labelled corpus

Sample of up to 10 labelled frames per state, classified through
`classify_via_registry`:

| State              | Total labelled | Accuracy | Notes |
|--------------------|----------------|----------|-------|
| `main_menu`        | 26             | **10/10 (100%)** | 4-region fingerprint; bottom-right tile bar alone is sufficient |
| `world_map`        | 57             | **10/10 (100%)** | Mode tabs + title + Go-to-City |
| `port_overworld`   | 39             | **10/10 (100%)** | Right-edge cluster + 6 negative signals |
| `building`         | 50             | 9/10 (90%)       | 1 no-match (likely title OCR drift) |
| `port_map`         | 31             | 9/10 (90%)       | 1 → port_overworld |
| `sub_menu`         | 68             | **10/10 (100%)** | 30+ KB-driven sub-menu titles |
| `sea`              | 45             | **10/10 (100%)** | Sea HUD tokens + 5 negative signals |
| `port_loading`     | 27             | **10/10 (100%)** | Private-rank / nation-rank panel |
| `loading`          | 35             | 1/10 (10%)       | Only 'sailing condition' variant matches; 8 no-match |

**`sailing_idle`** (21 labelled frames) has no registered fingerprint:
the survey found no cross-frame-stable label-set signals — sailing_idle
is the cinematic view with HUD hidden, by design containing minimal UI
elements.  Detection lives in `actions/sail_actions._confirm_at_sea_moondream`
which asks Moondream a yes/no scene-class question.

The Moondream prompt is now formulated as **"is a ship travelling on
water?"** — including open ocean, coastal, AND river passages (where
land banks may occupy significant screen area).  The previous
"open ocean only" phrasing incorrectly returned `no` for river-passage
sailing, which is also a valid sailing state.

Within the registry architecture, sailing_idle could later become a
fingerprint with a `MoondreamSignal` type added to the evaluator —
but that requires extending the signal taxonomy.  For now, perceive's
fallback chain handles it.

## States covered well (registry-only, no labelling needed)

These are at production quality.  No additional labels needed:

- **`main_menu`** — 4 region signals (top-left company panel, right icon
  grid, bottom-left utility, bottom-right tile bar).  The user's
  invariant "tile bar alone identifies main_menu" is encoded.
- **`world_map`** — 4 signals (mode tabs, World Map title,
  bottom-left controls, Go to City button).
- **`port_overworld`** — Right-edge tab/minimap cluster as positive,
  plus 6 negative signals against more specific states.  The "default
  at-port screen" semantic is now structurally correct.
- **`port_loading`** — Private/nation rank ranking panel.
- **`sub_menu`** — 30+ KB-known sub-menu titles cover all 68 labelled
  frames.

## States needing more labels

### `loading` — 35 frames labelled, only 10% accuracy

Most loading screens don't share visible elements.  The "sailing
condition" / "there are no" labels appear on 4/5 surveyed frames but
the other ~28 labelled loading frames have different content.

**What to label:**
- 5–10 frames of each distinct loading variant (between port and sea,
  battle entry, tutorial transition, etc.).
- For each, note in the `notes` field which loading sub-type it is
  (e.g. `"voyage_start_loading"`, `"battle_entry_loading"`).

Once labelled, the fingerprint can branch on sub-type or use a
"low-element-count + no specific state matched" heuristic.

### `sailing_idle` — 21 frames labelled, 0% (no fingerprint)

The cinematic sea view has too few elements to fingerprint by labels
alone.  Detection currently relies on Moondream's "is open ocean?"
yes/no.  Long-term, a structural signal could be `len(elements) < 5`
combined with "no land visible" via Moondream — but this is
architecturally separate from the label-based registry.

**No additional labels strictly needed** for this state in the
current architecture.  Detection lives in the legacy chain.

## States not yet labelled (interruptors / overlays / dialogs)

These 14 screen types are out of scope for the **navigation state**
registry — they're interruptors / popups that fire on top of any nav
state.  The next phase (Phase 7) will build a SECOND registry with the
same architecture (Fingerprint + signals + evaluator) for these,
populated from labels:

| Screen type        | Total labelled |
|--------------------|----------------|
| `dialog_transaction` | 75 |
| `dialog_event`     | 64 |
| `dialog_gameplay`  | 43 |
| `dialog_overlay`   | 41 |
| `dialog_reward`    | 23 |
| `dialog_system`    | 21 |
| `dialog_game_notice` | 16 |
| `dialog_shop`      | 9 |
| `dialog_android`   | 2 |
| `building_npc_overlay`     | 24 |
| `port_arrival_overlay` | 21 |
| `result_screen`    | 26 |
| `announcement`     | 13 |
| `other`            | 16 |

Total: **394 labelled interruptor frames** ready to fingerprint when
Phase 7 starts.

## What I'd ask the user to label next

Two narrow gaps in the navigation-state coverage:

1. **`loading` sub-types** — capture and label 5–10 frames of each
   distinct loading screen variant (voyage start, battle entry, port
   arrival, tutorial transition).  Tag each with a `notes` field
   identifying the sub-type.  Goal: a fingerprint branch per variant.

2. **A handful more `port_map` and `building_interior` frames** for
   the 90%-accurate fingerprints.  Notes on what the misses likely
   are (corrected from user clarifications):
   - **Estate variants** are tied to named NPCs (Mercator Estate,
     Franco Estate, etc.) and exist only at certain ports — building
     function is identical across all of them.  The current fingerprint
     accepts any TL text containing the substring `"estate"`, so
     `Franco Estate` should already classify as `building`; a frame
     would just confirm that.
   - **Bureau** exists at EVERY port but may require scrolling the
     building list to find at busy ports — the navigation concern is
     separate from the classification (the Bureau screen itself has
     `bureau` in its TL title and classifies cleanly).
   - **Port-map variants** at ports with fewer building icons or
     different layouts could shake out the remaining 1/10 miss.

Everything else in the **navigation-state vocabulary** is fully
covered by the existing labelled corpus.  Phase 7's interruptor
registry will use the same procedure on the 394 labelled
dialog/overlay frames.
