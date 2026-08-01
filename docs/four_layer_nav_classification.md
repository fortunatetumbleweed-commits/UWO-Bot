# Four-Layer Nav-State Classification

Companion to `four_layer_perception.md`.  That doc applied the L1–L4
framework to **goal predicates** (`has_enough_crew`).  This doc applies
the same framework to **nav-state classification** — the question
`_classify_nav_state` answers on every frame: *port_overworld? sea?
building? world_map? loading?*.

Motivation: the 2026-05-12 15:44 stuck-state run.  After a game reload,
`_classify_nav_state` produced two consecutive wrong answers
(`sea_cinematic` and then `building/harbor`), each triggered by a
different detector firing alone with weak evidence.  The cascade burned
the replan budget and ended in the teaching loop.  Root cause was not a
single detector bug — it was that the classifier has no explicit notion
of *layer*, so any detector that returns a non-null answer wins, even
when its question is the wrong question for the frame.

## The two overworlds

UWO has exactly **two 3D worlds**.  Everything else (buildings, menus,
dialogs, maps) is 2D UI layered on top of one of them.

| World | When | UI chrome that uniquely identifies it |
|---|---|---|
| **Port overworld** | Player is at a port (walking around, ship docked nearby) | Port name in top-left + right panel (tabs + mini map) + day/season/time bar |
| **Sea overworld** | Player is sailing | Sailing HUD (sail/anchor controls + ETA + day counter) |

Both worlds contain a ship and water in their 3D content.  The arrival
animation after a reload at a port is **not a separate state** — it is
the port-overworld scene with the camera positioned at the dock.  The
3D content is shared; **only the UI chrome distinguishes the two worlds.**

The reload bug was that perception ran while chrome was still drawing.
With chrome absent on both worlds, the classifier asked a visual-content
question ("does the frame look like sea?"), saw ship-on-water, and
answered yes — even though state was port_overworld.  No amount of
visual reasoning fixes this: the worlds are visually overlapping by
design, and only chrome resolves the ambiguity.

## The four layers (applied to nav classification)

| Layer | Input | What it computes | What it answers |
|---|---|---|---|
| **L1 — Fingerprint** | OmniParser elements (label, region) | Signature match against `fingerprints/*.json` | "Is this an exact known node?" |
| **L2 — Structural signals** | Chrome detector + element-shape summary | Which UI-chrome signature is present | "Which kind of screen is this?" |
| **L3 — Extracted text** | OCR on bounded crops | Port name, building title, screen title | "Confirm/refine L2's guess" |
| **L4 — Visual predicate** | Moondream / Claude Vision binary queries | Yes/no on narrow visual concept | "Tiebreak when L2+L3 are inconclusive" |

The discipline that was missing: **each layer must abstain when its
evidence is weak**, and the cascade must respect that.  A layer with
weak evidence does not get to win.

### L1 — exact-node match (high precision, conservative)

A fingerprint match commits only when corroborated by **≥2 independent
signals**.  Today the harbor fingerprint fired on a single signal
(`building_title=matched(['harbor'])`) from the overworld's harbor-
entrance label — that's an L1 rule behaving like an L2 heuristic.

**Rule:** L1 fingerprint rules for "inside building" must require
building-class chrome (Home button or back arrow) alongside any
title-text signal.  Single-signal matches are downgraded to L2-style
hypotheses, not L1 commits.

### L2 — structural signals (the load-bearing layer)

L2 owns the port-vs-sea-vs-building decision.  The decisive signals
are **UI chrome**, not 3D content:

| Decision | Required signals (≥2) |
|---|---|
| `port_overworld` | port-name OCR readable AND right_panel present |
| `sea` | sailing HUD tokens present (≥2 distinct tokens) |
| `building` | Home button OR back arrow + screen title readable |
| `world_map` | "World Map" title text present |
| `loading` | loading-pattern detector fires |
| `pending` | none of the above; chrome flags all False AND OmniParser yielded < N elements |

The new state `pending` is the key addition.  It means *"the 3D world
is still rendering, the chrome hasn't drawn yet, I will not guess"* —
the classifier's *honest "not yet"*.  Callers respond by polling (capture
again, re-perceive) up to a timeout.

Without `pending`, the classifier was forced to pick one of the named
states from a frame that had no chrome at all, which is when Moondream
got the deciding vote and got it wrong.

### L3 — extracted text (confirm L2)

Once L2 has a candidate, L3 reads the bounded crops L2 said should
contain text and either confirms or invalidates:

- L2 says `port_overworld` ⇒ L3 reads the top-left crop, must return a
  port name that is **not** a zone-label keyword.  Success ⇒ commit
  with `port` field populated.  Failure ⇒ demote L2 to `unknown`.
- L2 says `building` ⇒ L3 reads the title crop, returns building name
  for the `detail` field.

L3 disagreement demotes L2; L3 silence (couldn't read text) leaves L2
intact but logs the gap.

### L4 — visual predicate (tiebreaker only)

Moondream and Claude Vision binary queries answer **narrow visual
predicates**, not state classes.  Useful as tiebreakers when L2 is
ambiguous — for example when an in-world overlay (NPC speech bubble,
name-plate) obscures the right panel and L2 sees only the port-name
signal.

**Rule:** L4 is only invoked when L2+L3 returned an ambiguous result
*with at least one positive signal*.  L4 is **never** invoked on a
frame where L2 returned `pending` (chrome not yet drawn) — the right
response there is to wait, not to ask a visual-content model.

The 2026-05-12 bug was an L4 call on a frame where L2 should have
returned `pending`.  Moondream's "at sea?" answers "yes" whenever it
sees ship-on-water, which is true on both overworlds and on the port-
arrival animation — so the predicate is the wrong predicate for the
question "which overworld is this."

## Decision matrix

| Frame property | L1 | L2 | L3 | L4 |
|---|---|---|---|---|
| Fingerprint match with ≥2 signals | **commit** | — | — | — |
| Chrome flags + structural signals ≥2 | — | **commit** | confirm | — |
| Only one chrome signal | — | tentative | **deciding** | tiebreak |
| All chrome absent, sparse elements | — | `pending` | — | **skip** |
| L2 ambiguous with positive signal | — | tentative | confirm | **tiebreak** |

## What stays vs what changes

### Stays
- OmniParser, chrome detector, OCR, fingerprint registry — all still in use.
- Moondream "in town?" / "at sea?" — kept as L4 tiebreakers, not as primary signals.
- Existing detection chain order (cheap-first).

### Changes
- **Fingerprint rules tightened.**  "Inside building" fingerprints
  require chrome-class corroboration alongside title text.  Single-
  signal matches are downgraded to L2 candidates.
- **New `pending` state.**  When chrome flags all False AND OmniParser
  yielded fewer than ~5 elements, classifier returns `pending` instead
  of guessing.  Callers (perceive, plan_loop) poll up to a timeout
  (~15 s) before escalating to L4 or Claude.
- **L4 invocation gated.**  Moondream binary calls only fire when L2
  returned a positive-but-incomplete signal (e.g. port name read but
  right panel not detected).  Not called when L2 returned `pending` or
  when no L2 signal fired at all.
- **L3 explicitly demotes L2 on disagreement.**  Currently the
  classifier returns `port_overworld` with `(right panel unconfirmed)`
  even when port-name reads suspiciously like a zone label — that
  becomes an explicit L3 invalidation step instead.

## Path forward

Three slices, shippable independently.  Ordered by impact-per-day:

### Slice 1 — `pending` state + L4 gating  *(start here)*

- Add `pending` as a valid `_classify_nav_state` return value.  Return
  it when all chrome flags are False AND OmniParser yielded < 5
  elements AND no port-name / sea-HUD / building-title text was read.
- In `perceive()`, when classifier returns `pending`, sleep ~1.5 s and
  re-capture/re-classify up to a timeout (15 s).  After timeout,
  escalate as today.
- Gate Moondream `_confirm_at_sea` / `_confirm_in_town` calls: only
  fire when at least one positive structural signal was present.

Expected impact: post-reload misperception cascade prevented.  No more
false `sea_cinematic` on port-arrival frames.

### Slice 2 — Fingerprint corroboration rules

- Add a `min_signals` / `required_chrome` field to fingerprint records
  for any state representing "inside building X".  Default 1 for
  backward compat; building-class entries set to 2 + require Home or
  back arrow.
- Audit existing fingerprints; tighten any whose primary signal is a
  text token that could appear on the overworld (harbor, market, inn,
  bank, …).

Expected impact: the secondary misperception (`building/harbor` while
on overworld) prevented.  Building-class fingerprints become true L1
commits, not L2 heuristics in disguise.

### Slice 3 — Explicit L3 confirm/demote step

- Refactor `_classify_nav_state` so each L2 candidate explicitly
  triggers an L3 read of the bounded crop it expects to contain text,
  and L3 disagreement demotes the candidate to `unknown`.
- Same shape as `extract_state` in the goal-predicate L3 — anchored
  extraction with abstain semantics.

Expected impact: cleaner classifier, fewer false commits when one
signal fires alone, better diagnostics (every commit comes with the L3
evidence that confirmed it).

## Why this is the right shape

Same three properties as the goal-predicate doc:

1. **Defence in depth.**  No single detector is load-bearing.  L4 is
   reduced to tiebreaker; L2 chrome is the decisive layer; L1 is
   conservative; L3 confirms.
2. **Honest "not yet."**  `pending` is a positive output, not a
   fallback.  Callers know to poll rather than guess.
3. **Each layer has a clear contract.**  L1 commits when ≥2 signals
   agree; L2 decides on chrome; L3 confirms; L4 tiebreaks only.  When
   the bot is wrong, the layer at fault is identifiable from a
   structured log — not from re-running perception under a debugger.
