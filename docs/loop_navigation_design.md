# Loop navigation — directed-loop + foothold tracking (design of record)

**Status:** principle validated in prototype (2026-07-31).  Prototype:
`tools/bankline_tracker_proto.py` (`LoopTracker`); new production layer
scaffolded at `brain/ai_nav/layers/loop_tactical.py` (`LoopTactical`,
`--tactical loop`).  Supersedes `docs/bankline_continuity_design.md`.
Regression target: `tests/tactical_scenarios/` (the xfail cases
`strait_t1525`, `channel_stuck_t1444`).

## 1. Purpose — why this exists

The goal of shore navigation is **to establish ONE continuous shoreline
across the whole trip**, and to **get close enough to the shore to
discover things on land** (map discoveries like Stonehenge require
proximity — roughly ≤30 px from the bank).

Everything below derives from that.  Navigation is not "reach a waypoint";
it is **extend a single continuous shoreline at its frontier, staying
close enough to discover the land beside it.**  The direction the ship
travels each tick is whatever keeps extending that continuous line.

## 2. Core insight — frame edge = land → one closed loop

The visible water is a **closed region**, bounded by real banks AND by the
frame edges (treat the frame boundary as a land wall).  So its boundary is
always a **closed directed loop**, and every situation collapses into one:
river channel = both banks + exit walls; open coast = coast + three walls;
strait / islands / dead-end = the same loop with a pinch or a lobe.  The
ship sails INSIDE the loop and hugs it; a directed loop never loses
"ahead."

## 3. Direction — decided by (foothold, hugging side) only

- **Foothold** = the ship's point on the loop — the ONLY thing carried
  across frames.  Shift it by `frame_shift`, snap to the nearest loop
  point.  It can only slide ~a frame_shift, so the forward sense can
  **never flip** (this is what killed the strait/cairo thrash).
- **Hugging side** = land on the LEFT for port; fixes which way around the
  loop is forward.
- **Reflex point** = +40 px ahead of the foothold along the loop (the
  immediate steering target); **tactical point** = +80 px.  Commit =
  ship→reflex.
- **No CLI compass seed.**  Cold-start attaches the foothold to the near
  shore; the hugging *distance* resolves direction (near east shore ⇒
  south, etc.).  Only a ship dead-centre in a wide body is ambiguous —
  handled by launch placement.  The goal/mission only **optimizes the
  route** (fork choice, destination), never the tick-to-tick direction.

## 4. Continuity IS the shoreline

The foothold stitches consecutive frames' loop pieces into one unbroken
curve; over the voyage that curve is the continuous shoreline.  A dead-end
fully in view is *already part of* that continuous line (it runs in,
around, and back out — all seen).  The loop + foothold are literally the
continuity-of-shoreline mechanism.

## 5. Frontier rule — extend the unknown, don't re-trace the known

Decide by **visibility + growth**, not a frame counter:

- **Fully-in-view dead-end (far end inside the frame, not clipped by an
  edge)** = an *established* piece of shoreline; there is no unknown there.
  - **narrow** (can't navigate into it, or it's noise) → **skip** — don't
    re-trace what's fully seen.
  - **wide** (navigable) → **enter and approach to ~30 px from the bank**
    to scan the land for discoveries, then continue.  (Not skipped — the
    point of getting close is discovery, not just shoreline shape.)
- **Edge-reaching or growing notch** = a **frontier**: the shoreline runs
  off the frame / keeps lengthening tick over tick.  **Follow it** — that
  IS extending the continuous shoreline into the unknown (the Y-tip
  branches grow because there is more shoreline to establish).

Growth (not a count) is the confirmation: a real channel lengthens toward /
through the edge; a fake edge-opening (t1444) never lengthens — it lands-in
(bottom-edge water 214→223→230 px over three ticks) — so it was never
shoreline-to-establish.  Real frontiers grow.

## 6. Noise — game-UI overlays on the minimap

Diagnosed 2026-07-31 (evidence: overlaying the mask's land on the raw
minimap).  The fake notches are **game-UI overlays segmented as LAND**.
The overlay colour depends on **whether the city has been visited**
(confirmed 2026-07-31 comparing Cairo vs Tunis):

- **Unvisited city** — anchor icon + "???" label are **YELLOW**.
- **Visited city** — anchor icon + the city NAME are **WHITE** (Cairo's
  white anchor sits at the east-bank edge where land notches into the
  water beside the ship; the white "Cairo" letters land on the water).
- Plus, in both: **white** diamond + star markers, and **yellow** NPC /
  pirate sprites.

So the filter must catch **BOTH colours**, not yellow alone:

- **YELLOW** — unvisited anchor + "???", NPC/pirate sprites (distinctive
  hue: R,G high, B low; nothing geographic is that saturated-yellow).
- **WHITE** — visited anchor + city name, diamonds, stars.  **Caveat:**
  white is close to the cloud land + the lat/lon HUD text, so the white
  branch must target *solid bright-white overlay shapes in / adjacent to
  water*, not just "bright" (which eats legitimate cloud land and the HUD).

Overworld bleed-through (the "Balboa" label, the Spanish flag) sits on open
water with no bank, so it causes no notch and is effectively handled.

Measured (Cairo east marker notch): true width ≈ 7 px (2×DT), depth 19 px,
142° kink — the endpoint chord (23 px) badly overstates the opening; the
**distance transform (2×DT = local water width)** is the right measure.

**Fix = colour-based UI-overlay filter (both hues) + DT complement:** before
tracing, restore bright-YELLOW and solid bright-WHITE overlay pixels to
WATER (they are overlays, not geography).  The DT-erosion (§7) is a good
complement — a ~7 px anchor sliver dies regardless of colour — and it
covers the white-vs-cloud ambiguity the colour test can't fully resolve.

## 7. Water-offset via the distance transform

The traceline must not follow the raw bank (its straight chords can cut
across land, and it dips into thin slivers).  Use the DT:

- **Navigable region = where `2×DT ≥ min_width` (~10–15 px)**; equivalently
  erode the water mask by ~half that before tracing.  One operation:
  removes thin marker slivers (≈7 px), offsets the polyline into the water
  by the hug distance, and eliminates cross-land chords.
- Calibrate `min_width` to kill ~7 px marker slivers while keeping real
  narrow channels (e.g. the strait pinch) open.

## 8. Validation

**Validate DIRECTION, not just flips.**  Flip-count (>60° tick-to-tick)
measures *stability*, not *correctness* — a stably-backward commit scores 0
flips.  Item #1 was validated on flips alone and shipped a ~180° channel
REVERSAL undetected (see #1 below); always compare the loop's commit to the
real lat/lon motion (and/or `expected_wp_bearing_range`) as well.

- **Production** `LoopTactical` (abeam foothold + continuity window +
  heading-seeded forward sense): direction now follows the ship on all
  channel/mixed scenarios; **4 residual flips** across the 10 (the y_tip
  U-turn, the strait pinch, lake) — driven through the real `AiNavPipeline`
  + `FileVisionSource`.  Only `cairo_start` still points against its
  recorded heading (degenerate wide-body departure, below).

Simplified-loop feasibility: min section length 10 px holds everywhere;
report in `data/loop_simplify_report.html`.

## 9. Open work

1. **[DONE 2026-07-31] Foothold + hug-side correctness.**
   - Hug-side sign (`_land_on_left`) VERIFIED correct — the apparent
     wrongness (med_hug_west hugged starboard 72 %) was **foothold drift**,
     not a sign bug.
   - **Foothold pinned to the beam**: it is now the loop point ABEAM the
     ship (nearest to the minimap centre), not a world-fixed `prev +
     frame_shift` point.  A world-fixed foothold scrolls away from centre
     on the ship-centred minimap, dragging the reflex backward and rotating
     the commit (the SW→E med drift, foot-dist ran to 212 px).  Abeam alone
     JUMPS across a pinch (strait: the two banks are ~equidistant), so the
     abeam pick is constrained to a **continuity window** (~30 px around
     `prev + frame_shift`) — it can only slide, never leap banks.  Forward
     sense is carried by continuity (the step whose reflex stays closest to
     the prev commit), robust to the contour re-indexing each tick.
   - **Forward-sense / direction (fixed 2026-07-31, after live run).**  The
     bug missed above: `_land_on_left` CANNOT set the forward sense in a
     CHANNEL — both banks are land, so it picks a bank arbitrarily and
     continuity locks the trace ~180° BACKWARD (nubia / y_fork / y_tip /
     nile_meander / strait all reversed; live: ship pointed south, loop
     drove north).  **Fix: at the INITIAL tick only, seed the forward sense
     from the ship's HEADING** (it is pointed where it wants to go) — pick
     the walk direction whose reflex matches `state.heading`.  Thereafter
     continuity carries it; open water self-corrects from its single bank.
     Heading is per-frame perception, not a goal/compass seed — this is what
     "launch placement" means (depart pointed the right way).
     - Tried and REVERTED: heading EVERY tick (flips 4→7, no gain) and
       hugging-side foothold every tick (flips 4→23) — both destabilise; the
       user's rule is heading at the *initial* tick only.
   - **Remaining — wide-body departure.**  `cairo_start` still points
     against its recorded heading: the ship heads WEST into open water with
     the only nearby bank on the STARBOARD side, so port-hug has no left
     bank to trace (`land_on_left`/hugging-side both degenerate).  Distinct
     from the channel reversal; live, pointing the ship at the channel (a
     real bank on the hug side) resolves it.  Earlier motion-seed cold-start
     was also REVERTED (tick 1 has no frame_shift).
2. Build: color UI-overlay filter → DT-offset navigable region → the
   frontier / notch rule (skip narrow fully-in-view dead-ends, enter wide
   ones to ~30 px, follow growing frontiers).

## 10. Wiring (later)

`LoopTactical` replaces `LookaheadTactical`'s anchor cascade.  The
mission/strategic layer supplies route + discovery goals (which frontier to
prefer, which wide dead-end to enter), not the tick-to-tick direction.
