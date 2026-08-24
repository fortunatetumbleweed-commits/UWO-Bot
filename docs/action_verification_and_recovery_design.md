# Action verification & recovery — design

Status: **design** (2026-08-17). Consolidates the recovery/verification principles the
user laid out while debugging the gather run. Foundational: it changes how the bot
*verifies* actions and *recovers*, and it underpins learning the UI.

## 1. Motivation — AI perception is non-deterministic

The bot's perception stack (OmniParser, Moondream, Qwen, the CNN classifiers) is
**model-based and non-deterministic**: the *same* screen can yield *different* results
run to run — OmniParser may detect a different set of boxes on two near-identical
frames; a classifier may waver. So you **cannot** reliably answer "did my action work?"
by simply re-running perception and comparing labels — the labels themselves are noisy.

The reliable signal is **deterministic**: did the action cause a **tangible change in a
screen region**? Pixels don't lie the way model outputs do. So the verification layer
is a cheap, ML-free **frame-area diff**, sitting *between* the action and any re-perceive
or recovery.

## 2. What counts as a "tangible change"

Every meaningful game action produces a **layout change** in some region:

| action | region that changes |
|---|---|
| enter / exit a building | whole content area |
| a dialog appears / dismisses | **centre** region (a modal over the middle) |
| right panel shows / hides (building list, cart, destination list) | **right** region |
| select a destination → "Go to Port/Village" button appears | **bottom** button row (at minimum) |
| tap a market tile → cart tile added | right cart region |

What is **NOT** a real change: the **clock ticking**, a weather shimmer, an NPC speech
bubble — these are tiny, localised, and involve **no layout change**. The whole point is
to separate "the game state actually moved" from "time passed / cosmetic noise."

So the rule: **a successful game action shows a layout change in the expected region;
if the only delta is time/cosmetic, the action did not register.**

## 3. The core primitive — `classify_action_outcome`

Cheap, ML-free, runs on downscaled frames:

```python
classify_action_outcome(prev_frame, cur_frame) -> Outcome
# Downscale both; abs-diff. Compute mean delta per REGION (centre / periphery /
# right-panel / bottom-button-row / top-HUD). Ignore the top-HUD clock area.
#   all regions ~tiny            → "unchanged"        (tap didn't register)
#   centre ≫ periphery           → "central_dialog"   (overlay popped up)
#   right / bottom region jumps  → "panel_or_button"  (expected UI reaction)
#   whole frame large            → "state_change"     (enter/exit / transition)
```

Regions (fractions of the 2400×1080 frame, to calibrate): TOP-HUD strip (ignore — clock),
CENTRE modal zone, RIGHT panel, BOTTOM button row, and the residual periphery. Keep it
coarse and fast — it's a pre-perception gate, not a detector.

## 4. The recovery ladder — full reset is the LAST resort

`recover_to_port_overworld` throws away all flow progress, restarts from overworld, and
frequently just re-hits the same wall. It must be the final fallback, not the reflex.

```
 perceive → decide → do ONE action
      │
      ▼
 classify_action_outcome(prev, cur)
   ├─ expected change (panel_or_button / state_change we wanted) ─▶ proceed
   ├─ "state_change" but UNEXPECTED ───────────────────────────────▶ central perceive → decide (re-route)
   ├─ "central_dialog" ────────────────────────────────────────────▶ detect + handle the dialog (its own close-X / Back / interact) → retry the intended action
   └─ "unchanged" (tap didn't register) ───────────────────────────▶ our UI model ≠ real UI: RE-LOCATE the control (perceive it afresh), retry a DIFFERENT way — never blind-retry the same coord  (see feedback_stuck_vary_the_action)
      │  (only when the above is exhausted, or the state is truly unrecognised)
      ▼
 escalate to Claude / human  →  recover_to_port_overworld   ← LAST RESORT
```

Everything above the last line is cheap and non-destructive.

## 5. Subloops & unknown states (the routing rule)

Two rules that govern who handles what (see feedback_subloops_only_known_states):

- **Avoid subloops.** Prefer the central `perceive → decide → one action → re-perceive`
  tick loop. A subloop = code that runs its own inner perceive/act loop and won't yield
  to the central loop until it finishes or times out.
- **A subloop, if truly necessary, handles ONLY known states** (a deterministic sequence
  over states it fully recognises). **Any unknown state bubbles UP to the central
  perceive→decide loop**, which routes it (or escalates). An unknown state is *never*
  blindly acted on inside a subloop.

### `_handle_unknown` redesign
Today `brain/goals/sail_to._handle_unknown` sends everything not in its dispatch table
(incl. `sub_menu`, which is a *known* screen) into `plan_to("port_overworld")` →
`recover_to_port_overworld` — a blocking subloop that flailed for ~4 min on the gather
run. Split it:

- **Known-but-unhandled** (`sub_menu:*`, a dialog, a building sub-screen — the bot knows
  exactly where it is): exit deterministically, **one Back/close per tick**, then
  re-perceive via the central loop. (`navigate_to_building` already does exactly this
  for `sub_menu`: `press_back(); continue`.)
- **Truly unknown** (novel / garbled screen): re-perceive once; if still unknown →
  escalate. Do not blind-recover.

## 6. Clean hand-off (upstream cause)
Every task/executor must **leave the world in a known state** (`port_overworld`) when it
finishes, so downstream goals never inherit a deep/odd state. The gather executor must
`_exit_to_overworld()` after buying — the frame-19 stall was `SailToGoal` inheriting a
market it should never have seen. This is the flow-completeness discipline applied to
sub-task boundaries.

## 7. Why this is also how gameplay is LEARNED
The same primitive teaches the bot the UI's cause→effect. Learning "what does tapping
here do?" = **observe the tangible screen-area change an action produces**. Because model
perception is noisy, the *deterministic* action→region-change signal is the trustworthy
teacher:
- tapping a building nameplate → whole-content change ⇒ "that enters a building";
- selecting a destination → bottom-button change ⇒ "Go to Port appeared";
- a commit tap → central_dialog ⇒ "a confirm dialog follows a purchase".

So `classify_action_outcome` is both the **verification gate** (did it work?) and a
**learning signal** (what did it do?) — feeding the growing game KB
([[project_game_knowledge_base_vision]]).

## 8. Implementation plan
1. **`classify_action_outcome(prev, cur)`** — region frame-diff primitive + unit tests
   on captured before/after frame pairs (enter/exit, dialog, panel, clock-only). ML-free.
2. **Wire it into the act→verify step** of the nav loops (`navigate_to_building`,
   `SailToGoal`, market actions): after an action, classify → route per §4 before any
   perceive/recovery.
3. **`_handle_unknown` split** (§5) + **gather clean hand-off** (§6).
4. **Demote `recover_to_port_overworld`** to last-resort; make it bail to the central
   loop on any state it doesn't deterministically recognise.
5. (Learning) log action→outcome pairs to grow the UI cause-effect KB.

## 9. Reuse
Frame-diff already exists in pieces to build on: `_overworld_signature`
(navigate_to_building), the bulk-load cart-tile pixel-diff, the no-obstruction
perceptual-hash cache, and the speed/bounce detector. Generalise into the one region
classifier.

## Related
[[feedback_action_outcome_frame_diff_triage]] · [[feedback_subloops_only_known_states]] ·
[[feedback_no_subloops_perceive_act_perceive]] · [[feedback_stuck_vary_the_action]] ·
[[project_home_button_is_chromed_only_escape]] · [[project_game_knowledge_base_vision]] ·
`docs/opportunity_driven_architecture.md`.
