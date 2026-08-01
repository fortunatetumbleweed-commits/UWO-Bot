# Four-Layer Perception Architecture

Findings from empirical analysis of 19 labelled frames (Port Royal + Southside
recruit-crew capture sessions, 2026-05-12).  Analysis script:
`tools/tree_simulator.py`.

The single-hash "screen fingerprint" was being asked to answer four different
questions at once.  The data shows each question has a different right answer.

## The four layers

| Layer | Input | What it computes | What decision it answers |
|---|---|---|---|
| **L1 — Fingerprint** | OmniParser elements (button+icon+text, position-binned, numerics stripped) | SHA1 hash of `(label, region)` tuples (Recipe D) | "Am I at the exact same node I came from?" — cycle detection |
| **L2 — Component summary** | OmniParser elements, grouped into 3×3 grid × `element_type` | `Counter[(etype, region)]` — multi-set | "Is this the same *kind* of screen?" — screen-type matching, plan-context lookup |
| **L3 — Structured state** | OCR tokens | Regex extraction → `{crew_capacities, min_crew, standby_crew, dialog_text_cues, blocker_phrases}` | "What is the actual state right now?" — state snapshot, ΔL3 between taps reveals progress |
| **L4 — Goal predicate** | L3 state + goal definition | Deterministic predicate, returns `True / False / None` | "Is the goal achieved?" — loop termination, success detection |

Each layer is **strictly more abstract than the one below it**.  L1 is
position-of-pixels.  L4 is yes/no on a game-state predicate.

## Empirical evidence (19 frames)

The numbers that anchor the design choices:

- **L1 (Recipe D)** — catches the cycle case `s1.01 ≡ s1.03` (same fingerprint after Cancel returns to recruit screen); 7/7 expected screen distinctions pass; doesn't catch before/after-recruit (correct: they really are different states).
- **L2 (count-tolerance ±1, threshold 0.85)** — 5/5 expected matches at similarity 1.00 (before/after recruit at harbour and inn, overlay captured twice, cycle, cross-port); correctly rejects Recipe-D collision at 0.75; correctly rejects every screen-type-difference pair below 0.85.  One ambiguity: harbour-recruit ≈ inn-recruit at 0.95 — same screen type, different parent, which L1 distinguishes via the left menu.
- **L3** — extracts fleet capacities + `min_crew` on every sub-menu / dialog frame in the test set; dialog presence flagged via `cancel` keyword on every confirm dialog.
- **L4 (`current_crew >= min_crew`)** — 100% correct on every relevant frame: ✓ on every post-recruit frame, ✗ on every pre-recruit frame, abstains (`?`) on screens with no extractable state.

## Decision matrix — which layer answers each runtime question

| Runtime question | Use layer | Why |
|---|---|---|
| Did the bot just tap and return to the parent screen? (cycle) | L1 exact match | Bit-precise; matches `commit_via_positive_taps` cycle-close semantics |
| Is this the same kind of screen as ones I've seen before? | L2 similarity ≥ 0.85 | Tolerates OCR noise and YOLO confidence flicker |
| Did anything change from the previous frame? | ΔL3 (diff state dicts) | Captures real game-state shifts |
| Was the tap a no-op? | L1 same + L3 empty diff | Two independent signals must agree |
| Has the goal been achieved? | L4 predicate | Deterministic, zero-cost, more reliable than Claude `heavy_check` |
| What kind of screen am I on at all? | Fingerprint registry (existing) | Falls back to L2 when registry misses |
| What should I tap next? | Context-aware button picker (existing); LoRA later | LLM-shaped problem; L1-L4 produce richer prompt context |

## What stays vs what changes

### Stays
- All existing perception layers — OmniParser, EasyOCR, chrome detector, fingerprint registry, interruptor detector.
- Existing `commit_via_positive_taps` cycle-close detection — L1 powers this.
- Existing flow-completeness, no-op detection, plan-runtime confidence/demotion.
- Claude as the strategic-reasoning tier — scene analysis, plan generation, plan revamp, teaching loop.
- Moondream/llava and Qwen as middle-tier yes/no and detail-enrichment helpers.

### Changes
- **L4 replaces Claude `heavy_check` for any goal that has a defined local
  predicate.**  Other goals keep escalating to Claude until predicates are
  added.  Per-recruit Claude calls drop from ~7-10 to ~1-2 with strictly
  better accuracy (no more `heavy_uncertain` burning replan budget).
- **L3 state snapshot at every tap** lets the no-op detector and progress
  detector use richer signals than the current OCR-signature compare.
- **L2 component summary** becomes the screen-type matcher for plan
  lookup, supplementing the exact-fingerprint registry.

## Path forward

Three independent slices, each shippable on its own.  Order is priority by
impact-per-day:

### Slice 1 — L4 goal predicate framework  *(start now)*

- New `local_predicate` field on goal definitions
  (`memory/knowledge/goals/<goal>.json`).
- `brain/predicate.py`: a minimal evaluator that reads L3 state and returns
  `True / False / None`.
- `brain/plan_loop.heavy_check` calls the local predicate first; falls back
  to Claude only when no predicate is defined or the predicate returns
  `None`.
- First predicate authored: `has_enough_crew = crew_capacities[0][0] >= min_crew`.

Expected impact: removes 5-7 Claude calls per recruit cycle, eliminates the
`heavy_uncertain` failure mode for the trained goal.

### Slice 2 — L3 state extractor + per-step ΔL3

- New `vision/state_extractor.py`: the L3 regex extraction from this
  morning's analysis, exposed as a function over OCR tokens.
- New utility in `brain/perceive.py`: `state_diff(before, after)` returns
  the set of changed keys.
- Wire into `_drain_positive_button_chain`: instead of (or alongside) OCR
  signature comparison, use `state_diff` to detect no-ops.  More
  informative — also tells us *what* changed when there is progress.

Expected impact: better no-op detection on screens where pixel signature
is noisy (animation, ship counter drift), more informative logs for
debugging.

### Slice 3 — L2 screen-type matcher for plan lookup

- New `vision/component_summary.py`: the L2 `Counter[(etype, region)]`
  computation.
- Each plan in `memory/knowledge/plans/<goal>/<plan>.json` gains an
  `applies_when` field — an L2 component summary representing the screen
  type the plan starts from.
- `PlanRuntime.lookup(goal, current_state)` ranks plans by L2 similarity to
  current screen, in addition to historical success rate.

Expected impact: cross-context plan reuse (a recruit plan learned at harbour
can be considered for the inn's recruit screen because the L2 similarity is
high); planner picks the most-applicable plan rather than just the
highest-confidence one.

## Why this is the right shape

Three properties the architecture has that the single-hash design didn't:

1. **Defence in depth on every decision.**  No single layer is load-bearing.
   Goal-met can be confirmed by L4 alone; no-op detection cross-checks L1
   equality with L3 emptiness; screen identity uses L2 similarity with L1 as
   a tiebreaker.  When the bot is wrong, multiple layers go wrong together,
   which is much rarer.

2. **Each layer is locally observable.**  L1 is a hash, L2 is a dict of
   counts, L3 is a dict of structured fields, L4 is a boolean.  All four are
   printable.  Diagnosing a stuck bot becomes reading a four-line summary,
   not re-running perception.

3. **Claude gets promoted, not replaced.**  Lower tiers handle the common
   cases; Claude is only invoked for novelty / open-ended reasoning, and
   when it *is* invoked it gets a structured-state preamble that
   dramatically improves prompt quality.  API spend trends down while
   per-call value goes up.
