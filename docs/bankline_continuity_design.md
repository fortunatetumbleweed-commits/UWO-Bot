# Bankline continuity — the directed-vector tracker

> **[SUPERSEDED 2026-07-31 by `docs/loop_navigation_design.md`.]** The right
> instinct — persist the bankline, carry direction by continuity — but it
> hung continuity on the open traceline's endpoint/tail (which jumps).  The
> loop-navigation design fixes that: close the region into a directed loop
> (frame edges = walls) and carry a single foothold.  Kept for history.


**Status:** design agreed 2026-07-30; replaces the anchor policy in
`LookaheadTactical` (sticky / migrate / hold / hug-side).  Regression
target: `tests/tactical_scenarios/` (15 scenarios; `strait_t1525` and
`channel_stuck_t1444` are the currently-xfail cases this must fix).

## The problem

The tactical today re-traces the hug-side bank **fresh every tick** and
then uses an **anchor** (the traced endpoint, world-locked via
`frame_shift`) to remember which way it was going.  At ambiguous geometry
— a strait, between islands, a dead-end coming into view — a fresh trace
has no memory, so the anchor **re-picks and flips**: the commit oscillates
W↔E / N↔S and the ship stalls (see `strait_t1525`, `channel_stuck_t1444`).
Every downstream patch (sticky, migrate, hold(pocket), artifact_reversal,
hug-side re-pick) is a band-aid over the fact that **the endpoint is the
wrong thing to hang continuity on.**

## The insight

The bankline is **one continuous curve in the world**.  The minimap is a
**shifting window** onto it.  As the ship moves the window scrolls: the
bankline **grows on the leading side and trims on the trailing side**, but
any section visible in both frames is the **same shape, just shifted** by
`frame_shift`.  So if we follow the *same continuous line* in the *same
direction*, there is **no anchor at all** — direction is a property of the
line, not a remembered point.

Two constraints keep it honest:

1. **Don't shift-integrate a remembered line** — that accumulates error
   (the drift the code already warns about, session 2026-07-27 t174).  We
   **re-trace fresh every frame** (perception, no drift).
2. **But don't throw the old trace away** — register the fresh trace
   against it to find where it *grew*, where it *trimmed*, and — crucially
   — **which way along it is forward.**

## The directed vector (head → tail)

Model the bankline as a **directed polyline**: a **head** and a **tail**.

| end | meaning | per-tick change |
|---|---|---|
| **head** | forward / leading (travel direction) | **unbounded** — a dead-end tip or sharp turn reveals and it grows far, possibly in a new direction |
| **tail** | trailing / behind the ship | **bounded by `frame_shift`** — trims ~one shift as the ship advances; **regrows ~one shift if the ship goes backward** |

**Continuity lives in the tail, not the head.**  Because the tail's change
is frame-shift-bounded, we can register it reliably frame-to-frame; that
registration fixes the head/tail assignment on the fresh trace, i.e. the
**traversal direction is carried over**.  The head is then free to explode
with new geometry — its jumps are legitimate and no longer decide
direction.

**Wrong-direction is a bounded, detectable signal:** if the tail *regrows*
instead of trimming, the ship traveled the wrong way (e.g. a bounce spun
it) — caught within a frame-shift, not a wild flip.

## Per-tick algorithm

1. **Fresh-trace** the hug-side bank → `new_line` (as today; no drift).
2. `prev_shifted = prev_line + frame_shift`.
3. **Register** `new_line` against `prev_shifted` over the **tail overlap**
   (near/behind the ship; tolerance ≈ shift error).  This assigns the
   head/tail of `new_line` so the **direction carries over**, and flags the
   head **growth** and tail **trim** (or tail **regrow** ⇒ wrong-way).
4. If the tail does **not** register (no overlap within ~a shift) → suspect
   trace (artifact / wrong bank) → hold prior one tick.
5. **Commit** = along `new_line` toward the **head** (tangent at the ship).
   The head endpoint may jump — allowed.
6. `prev_line = new_line`.  **No** sticky / migrate / hold / hug-side —
   replaced by registration.

## Direction seeding (bootstrap only, tick ~1)

Direction and the hug rule are **interdependent** and must be resolved once
at the start:

- **Straight channel (Nile off Cairo):** ambiguous — with the ship pointing
  E/W, "hug port" doesn't say which bank.  The initial direction is
  **given** (`--commit-bearing` N/S).
- **One-sided coast (off Tripoli):** geometry resolves it — land is on one
  side, so "hug port" can only mean turning **west** (east would put land on
  starboard).  No seed needed; the hug side *is* the direction.

After tick ~2 the tail-registration carries the direction; neither seed is
consulted again.

## Why it fixes the hard cases

- **Strait / islands:** the shore is continuous; the tail registers every
  tick, so we keep following the *same* line the *same* way through the
  gap.  No re-pick → no W↔E / N↔S span.
- **Dead-end into view:** the head grows far (legitimate); the tail still
  registers, so we know it's the same line — approach the tip, then the
  head/tail flip as we turn around is itself a bounded, registered event.
- **Bounce-180 in a channel:** the map is world-oriented, so the line is
  unchanged; the tail registers and the head is still ahead → the commit
  keeps pointing forward, the ship recovers.

## Open implementation detail

**Registration method** for step 3 (robust to head growth): resample both
polylines to arc-length and align the tail overlap, or a lighter
shift-and-correlate near the ship.  To be chosen at build time.
