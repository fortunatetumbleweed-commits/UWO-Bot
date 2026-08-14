# State-Change / Progress Detection

How the bot answers *"did my action make progress, or did nothing happen?"* —
the signal that drives the task executor's **attempt memory** (don't repeat a
no-op), **done-conditions** (did the task complete?), and **stuck detection**
(escalate). See `project_task_executor_done_and_attempt_memory` (memory) and
`docs/game_knowledge_base.md` (task layer).

## Why NOT a whole-frame pixel diff

Pixel-diffing before/after frames fails in **both** directions:

- **Over-fires (false positives):** the UI has constant ambient motion — a live
  countdown TIMER (`00:16:32`), NPC portraits, water, the 3D scene. A whole-frame
  diff reports "changed" on nearly every frame *without any real progress*.
- **Under-fires (false negatives):** after a buy, the purchased item stays in the
  right panel **grayed out** — the panel looks ~identical, so a pixel diff says
  "no progress" even though the purchase completed.

So pixel similarity is both too noisy *and* blind to the meaningful change. We
compare **structured / semantic** state, in layers.

## The layered detector (cheapest, most reliable first)

A change is "progress" if ANY of these fire. Ordered by reliability:

### 1. World-state delta — ground truth (primary)
The unambiguous signals live in the HUD numbers, not the pixels:
- buy done / progressed = **cargo ↑ AND ducats ↓**
- recruit = **crew ↑**
- sail = **location == destination**
Read a few HUD values each tick (cargo used/cap, ducats, crew cur/max) and diff
them. These *are* the done-conditions too, so the reader does double duty.
**Prereq:** the cargo/ducats/crew HUD readers (see Gap 1 below) — currently
missing/unwired.

### 2. Screen / dialog identity change (structural signature)
If the screen or dialog *type* changed (grid → Confirm → Negotiation → …), that's
progress. Already available:
- `brain/verify.py::_state_signature(perceive_result)` → `(state, detail[:80],
  flow, flow_step)`. `light_check()` already returns PROGRESS when the signature
  changes and NO_PROGRESS when it doesn't.
- family/dialog classifiers (`vision/family_classifier.py`,
  `vision/region_detectors/dialog.py`) + learned fingerprints.
Extend the signature with a few key element labels + the world values from (1).

### 3. Coarse region-diff — structural change *within* a screen
When the screen type is the *same* but content moved (an item loaded into the
cart), detect *which region* changed — cheaply and robust to the timer:

1. **Downscale** both frames hard (e.g. to a small grid of blocks). This is a
   **low-pass filter**: averaging many pixels into one block washes out the timer
   tick and sub-pixel jitter, while a large regional change (the horse image
   appearing in the right panel) survives.
2. **Per-block color diff** → threshold → the set of changed blocks.
3. **Mask ambient-motion areas** (3D scene, NPC portraits, water). Downscaling
   shrinks but doesn't kill large moving regions; use the OmniParser layout to
   keep only UI-panel regions and drop the animated background — else "background
   changed" every frame.
4. **Label the changed blocks via OmniParser's element layout:** map each changed
   block to the element/region it overlaps. `"block (5,3) changed"` becomes
   `"the right-panel / cart region changed"` — semantic, without reading the word
   "Horse."

Result: `horse-tile region changed` + `right-panel region changed` = *item moved
to cart* = progress. This is how a human eyeballs it — you see the image move, you
don't read the label.

**Limitation (by design):** the same downscaling that kills the timer also filters
*small* meaningful changes — the cargo count `708→885`, the discount `21%→25%` are
a few digits, too small to survive the low-pass. That's fine: coarse region-diff
is the **structural** signal; the **value** changes come from layer (1)'s numeric
readers. Division of labor:
- **coarse region-diff → structural progress** (item→cart, dialog appeared/closed).
- **HUD numeric readers → value progress + done** (cargo/ducats/crew/discount).

### 4. Element-state + region pHash (fine signals, last)
- **Grayed-out detection:** a disabled/purchased item is low-saturation / low-
  contrast. A quick check on its bbox (like `commit_button.yellow_fraction`, but
  measuring saturation) reads "enabled vs disabled" — handles the grayed-out case.
- **Region pHash:** perceptual hash of a specific (masked) region for subtle
  within-region changes. Last resort.

## Putting it together — `did_progress(before, after)`

```
def did_progress(before, after):        # after an action
    if world_delta(before, after):      # cargo/ducats/crew/location changed
        return True                     # (1) ground truth
    if signature(after) != signature(before):
        return True                     # (2) screen/dialog identity changed
    changed = coarse_region_diff(before.frame, after.frame,
                                 mask=ui_regions(after.elements))
    if changed:                         # (3) a UI region changed
        return True
    return False                        # no progress → attempt-memory marks it
```
`signature(state) = (screen/dialog id, key element labels, key world values)`.

## How the task executor uses it

Per step: execute an action → `did_progress(before, after)`.
- **No progress** → record `(signature(before), action)` as ineffective; the next
  decision at that signature EXCLUDES it and picks a different affordance.
- **Cycling / no untried options** → **stuck** → escalate.
- **done_condition(after)** true → task complete.
This is what breaks the "repeat the same non-working action forever" loop.

## Current code + gaps

- **Exists:** `verify.py` `_state_signature` + `light_check` (progress via
  signature, + optional Moondream visual tier) + `heavy_check` (done via per-goal
  predicate or Claude); `vision/frame_shift.py` (phase-correlation frame diff, but
  **nav/minimap only**). These feed the PLANNER, not the reasoning/task loop.
- **Gap 1 — HUD numeric readers** (cargo MISSING, ducats detection-only, crew
  exists-unwired) — the prerequisite for layer (1) and the done-conditions.
- **Gap 2 — coarse region-diff** (layer 3) is not built as a general task-layer
  helper (frame_shift is nav-specific).
- **Gap 3 — wiring:** `resolve()` (reasoning loop) doesn't call `light_check` /
  keep attempt memory / pass a `done_fn`. Connect the above into the task executor.

See `project_design_gap_analysis_2026-08-09` (memory) for the full gap map and
build order (HUD readers → task executor → done-conditions → decision cache).
