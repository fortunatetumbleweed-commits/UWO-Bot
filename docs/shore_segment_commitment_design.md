# §13.23 — Shore segment commitment + LLM-on-HOLD

## Problem

The current `_shore_tangent_angle` does a weighted-LSQ line fit through
**three** port-side range samples (sec 14, 12, 10 at body angles −45°,
−90°, −135°) every tick.  At any topology change — Y/T junctions,
narrow inlet mouths, dead-end pockets — these three samples come from
**different physical shore segments** and the LSQ fits a single line
through non-collinear points, producing a tangent that describes no
real shore.  The bad tangent propagates through the §13.21
destination-anchored disambiguation and `_hug_line_waypoint`, landing
the waypoint in shore on the wrong side of the bot.

### Reference incident — `hug_debug_20260601_164805` t338 → t341

- t338: bow=234° (SW), LSQ θ_err=+12.4°, world-tangent 246°. Healthy.
  Waypoint = 196° (SSW, toward dest). Bot acts.
- t339, t340: heading detector falls back to `bow_wedge` (conf 0.5),
  rejected.  Policy skipped; no state update.  Bot continues to rotate
  through the U-turn under prior momentum.
- t341: bow=328° (NW), LSQ θ_err=−20.6°.  Pre-flip world-tangent 307°
  (WNW) — opposite the LOS-to-dest at 197° (SSW).  §13.21 flip picks
  the alternate 127° (ESE); the §13.21 side-bug (known limitation —
  see `project_destination_anchored_tangent_validated.md`) then pushes
  the perpendicular term east-of-bow.  Waypoint = 136° (SE), into the
  south bank.  The avoider obediently chases the SE bearing and picks
  sec 2 (compass 13°, frac=0.75, dist=0.069), turning the bot into the
  wrong branch of the Y.

The three samples at t341 — distances 0.256, 0.144, 0.117 — are from
three different walls (outer bank, junction corner, inside of
branch).  They're not on a single line.  No per-tick fit-quality
metric reliably separates this case from t338's clean fit
(verified empirically — see `/tmp/sim_step1.py`,
`/tmp/sim_option1.py`).

## Research summary

(Survey output is reproduced in conversation; full prompt + result are
in the chat transcript.  Key citations:)

- **Nguyen, Martinelli, Tomatis & Siegwart (2007)**:  single-tick
  residual gates assume n ≥ 10 supporting points; below n ≈ 5 the
  residual is "not statistically meaningful" (Arras & Siegwart 1997).
  Our 3-sample regime is below this threshold.
- **Choset & Burdick (2000)** and **Antonelli et al. (2007)**: pure
  world-frame-memory + delta-gate approaches **fail at river mouths /
  T-junctions** in published evaluation — the events look identical to
  legitimate fast curvature.
- **Lumelsky & Stepanov (1987), Kamon-Rimon-Rivlin (1998)**: the
  canonical pattern for "I'm hugging *that* wall and a U-turn or
  junction shouldn't change which wall" is **world-frame segment
  identity** (Bug2 hit-point, TangentBug segment ID, ILOS world-frame
  parameter).

The mature path forward is a **world-frame segment-identity** layer
combined with a **second independent signal** at ambiguous moments.
In our codebase the cheapest second signal is an LLM consult against
the mini-map, matching the existing Claude-consult pattern for unknown
scenes.

## Goals

1. **Catch the t341-class failure** — the algorithm must not commit to
   a hug-line waypoint derived from an LSQ tangent that describes no
   real shore.
2. **Preserve t338-class behaviour** — at ticks where the LSQ produces
   a useful tangent (even if not "perfectly clean" by classical
   metrics), the algorithm should still produce a sensible waypoint.
3. **Resolve genuine ambiguity** — when the algorithm cannot decide
   from local sensing alone, escalate to a mini-map-aware reasoner
   that returns a goal-direction bearing.  The bot acts on the
   reasoner's answer for a bounded horizon, then resumes local control.
4. **No regression on simple-channel ticks** — t280, t281 type ticks
   continue exactly as today.

## Non-goals

- Multi-segment line extraction (IEPF / split-and-merge).  Future
  work.  This design tracks **one** segment at a time, which is
  sufficient because the bot hugs one shore at a time.
- A global path planner / world map.  Future work; eventually wanted
  for routing but orthogonal to the local-control failure.
- Kalman tracking of segment covariance.  Use a simple EWMA on the
  committed tangent; revisit if instability shows up in voyages.

## Design

Two coordinated pieces:

1. **`ShoreSegmentMemory`** — a state object holding the currently
   committed shore segment in world frame.
2. **`HugShoreGoal` tick integration** — at each tick: extract fresh
   LSQ, associate against memory, decide ACT / HOLD, and on HOLD
   either fall back to memory or escalate to the LLM consult.

### `ShoreSegmentMemory`

```
@dataclass
class ShoreSegmentMemory:
    tangent_world_deg:    float          # compass bearing of shore tangent
    anchor_lat:           float          # point on the shore line (world)
    anchor_lon:           float          # point on the shore line (world)
    committed_tick:       int            # when first committed
    last_update_tick:     int            # last EWMA refresh
    side_at_commit:       Side           # which side of bow ("port" / "stbd")
                                         # at commit time — used for §13.21 fix
    consecutive_misses:   int = 0        # how many ticks fresh has disagreed
```

Single instance; `None` when no segment is committed yet (bootstrap).

### Tick state machine

At each acted tick (heading not rejected):

```
fresh_theta_err = _shore_tangent_angle(nav, side)   # existing
fresh_tangent_world = (heading + fresh_theta_err) % 360
fresh_tangent_world = destination_anchor_flip(fresh_tangent_world, los)
                                             # existing §13.21 logic

fresh_signals = {
    "samples_with_frac_ge_0.5":    count of (14, 12, 10) with frac ≥ 0.5,
    "high_frac_dist_ratio":         max/min dist over those samples,
    "fresh_tangent_world":          (computed above),
}

if memory is None:
    if fresh_signals["samples_with_frac_ge_0.5"] >= 2:
        commit(fresh)
        action = ACT(use_tangent = fresh)
    else:
        action = ACT(use_tangent = fresh)   # bootstrap, no choice

else:
    delta = angle_diff(fresh_tangent_world, memory.tangent_world_deg)

    if delta < ASSOC_THRESH_DEG:           # 30°
        # Same segment — refresh memory by EWMA.
        memory.tangent_world_deg = ewma(memory, fresh, alpha=0.3)
        memory.last_update_tick = tick
        memory.consecutive_misses = 0
        action = ACT(use_tangent = memory.tangent_world_deg)

    else:
        # Segment-identity disagreement.
        memory.consecutive_misses += 1
        if fresh_signals["samples_with_frac_ge_0.5"] >= 2 and \
           memory.consecutive_misses >= RECOMMIT_AFTER_TICKS:    # 2
            # Bot has clearly moved to a new shore segment.
            commit_new(fresh)
            action = ACT(use_tangent = fresh)
        else:
            # Either fresh is unclean OR we haven't yet seen N
            # consecutive disagreements — don't trust fresh.
            action = HOLD_AMBIGUOUS
```

### HOLD escalation

When `action == HOLD_AMBIGUOUS`, the goal does **not** emit a steering
action.  Instead it:

1. Checks `llm_cache` for a recent decision (≤ `LLM_CACHE_TICKS`).
2. On cache hit: use the cached bearing as the waypoint.
3. On cache miss: enqueue an LLM consult.
   - **Synchronous variant (initial)**: block for the consult
     (≤ 3 seconds typical), use the answer this tick.
   - **Async variant (later)**: emit "hold" this tick, fire the
     consult, use the answer when next tick lands.

Cache key: `(round(lat, 2), round(lon, 2), round(heading, 1))`.
Cache horizon: 5 ticks (~3 seconds at our cadence).

### LLM prompt template

The mini-map crop is small and OCR-clean.  The prompt is short:

```
You are looking at a navigation mini-map of a sailing game.

Boat state:
  - Current position: lat={lat:.2f}, lon={lon:.2f}
  - Current bow heading: {heading:.0f}° compass
  - Destination bearing: {dest_bearing:.0f}° compass
  - Current speed: {speed:.1f} knots

Local observation:
  - Around the boat, 16 sectors of 22.5° each report (land-fraction,
    nearest-distance).  Land-fraction near 1 = solid shore in that
    direction; near 0 = open water.  The forward arc (sectors 12, 13,
    14, 15, 0, 1, 2, 3, 4) is the bot's reachable swing.
  - {sector_readings_table}

The local shore-following algorithm is reporting an ambiguous fit
(unable to decide which physical shore segment to follow).  Look at
the mini-map and pick a compass bearing the boat should steer toward
to make progress toward the destination via navigable water.

Respond with one number — the recommended compass bearing in
degrees (0-359).  Bearings to avoid pointing into solid land.
```

Plus the mini-map PNG as image input.

Cost estimate: ~1500 tokens prompt + image + ~5 tokens reply.  At
current pricing this is sub-cent per call.  Bounded by cache so even
a 1-hour voyage stuck near junctions wouldn't see > 50 calls.

### Tuning constants

| Constant | Value | Notes |
|---|---|---|
| `ASSOC_THRESH_DEG` | 30° | World-frame angular tolerance for "same segment" |
| `RECOMMIT_AFTER_TICKS` | 2 | Consecutive disagreements before re-acquiring |
| `EWMA_ALPHA` | 0.3 | Fresh tangent's weight in memory update |
| `LLM_CACHE_TICKS` | 5 | Reuse last LLM answer for this many ticks |
| `HEALTHY_FRAC_MIN` | 0.5 | Sample frac threshold for "commit-worthy" |
| `LLM_CONSULT_BUDGET_PER_VOYAGE` | 50 | Hard cap; beyond it, fall back to "hold" |

## Failure-mode mapping

Traced against `hug_debug_20260601_164805`:

| tick | observation | state machine path | outcome under new design |
|---|---|---|---|
| t275 | first acted tick, healthy fit | INIT_COMMIT | as today |
| t280-t281 | clean channel, slight drift | ASSOC same-segment, EWMA | as today |
| t290-t320 | U-turn region, heading often rejected | skip-rej many ticks; occasional EWMA | as today (drift is contained by EWMA's smoothing) |
| **t338** | fresh tangent 85° from memory; samples 2+ with frac ≥ 0.5; bot has been rejected for many ticks before | first DISAGREE → `consecutive_misses=1` → HOLD-AMBIGUOUS → **LLM consult → returns ~200° (SSW)** → waypoint ≈ trace's 196° | matches today's good behaviour via LLM rather than coincidence |
| **t341** | fresh tangent 34° from memory after t338 didn't update; samples 2+ with frac ≥ 0.5 | DISAGREE again; `consecutive_misses=2` triggers RECOMMIT path **OR** still HOLD if fresh is judged unclean (depends on tuning) | needs the unclean-detector to fire; if it does, LLM consult → **returns ~210° (SW)** → bot turns left into correct branch |
| t342+ | post-junction, fits stabilise | EWMA tracking | new committed tangent reflects the SW-bound channel |

### Sensitivity / what could go wrong

- **HOLD too often**: if `ASSOC_THRESH_DEG` is too tight, every minor
  channel bend triggers HOLD → many LLM calls.  Tune up to 45° if
  voyage logs show this.
- **HOLD too rarely**: if `RECOMMIT_AFTER_TICKS` is 1, the algorithm
  recommits immediately on a single bad fresh fit (the original
  failure mode).  Keep at 2 minimum.
- **LLM returns bad bearing**: cache + LLM_CONSULT_BUDGET_PER_VOYAGE
  prevent unbounded damage.  If a returned bearing points into land
  (per current 16-sector observation), discard and fall back to
  memory's projected tangent.

## Implementation plan

**Phase A — Memory + state machine, no LLM** (~½ day):
1. Add `ShoreSegmentMemory` dataclass.
2. In `_lyapunov_state` (or a new wrapper), implement the state
   machine.  Return either the regulator state or a `HOLD_AMBIGUOUS`
   sentinel.
3. In `HugShoreGoal.tick()`, when state machine returns HOLD:
   skip steering for that tick (emit `action="hold"`).
4. Wire diagnostics into the trace (memory state, fresh vs committed
   tangent, association decision).
5. Tests: replay t338/t341 sectors; assert HOLD-AMBIGUOUS at t341,
   ACT at t280/t281.

This gets us a **safer-but-stallier** bot — won't crash at junctions
but may idle until the perception improves.

**Phase B — LLM consult on HOLD** (~½ day):
1. Add `vision/llm_navigation_consult.py` that takes the mini-map,
   bot state, and sector observation; returns a compass bearing.
2. Add a frame-keyed cache.
3. Wire into the HOLD branch.
4. Tests: mock the LLM, assert cache reuse, assert fallback when LLM
   returns garbage or budget is exhausted.

After Phase B, deferred-A and §13.21 known limitations become much
less impactful — the LLM's bearing supersedes the buggy lateral term
on the rare ambiguous ticks where it matters.

**Phase C — Live voyage validation** (~1-2 voyages):
1. Re-run the East-Med voyage that produced
   `hug_debug_20260601_164805`.
2. Check trace for HOLD frequency and LLM call count.
3. Inspect the Y-junction transition: does the bot now route correctly?
4. Tune `ASSOC_THRESH_DEG` / `RECOMMIT_AFTER_TICKS` if needed.

## Tests

| Test | Where | Purpose |
|---|---|---|
| `test_segment_memory_commit_on_clean_fit` | `tests/test_shore_segment.py` | Initial commit fires on 2+ high-frac samples |
| `test_segment_memory_ewma_on_match` | same | Tangent updates by 30% of fresh delta |
| `test_segment_memory_hold_on_t341_replay` | same | t341 sector fixture → HOLD-AMBIGUOUS |
| `test_segment_memory_ewma_on_t280_replay` | same | t280 sector fixture → EWMA-UPDATE |
| `test_segment_memory_recommit_after_n_misses` | same | After 2 consecutive disagreements on healthy samples, recommit |
| `test_llm_consult_cache_hit_skips_call` | `tests/test_llm_navigation_consult.py` | Cache reuse |
| `test_llm_consult_budget_exceeded_falls_back` | same | Hard cap |
| `test_llm_consult_bad_bearing_rejected` | same | Returned bearing into land → discard |

## Open questions

1. **Should the LLM consult also fire on §13.21's side-bug regime**
   (independent of the LSQ ambiguity, when the dest-anchored flip
   would have produced a wrong-side waypoint)? Probably yes — same
   pattern, same cost.  Decide after Phase A is live.
2. **Async LLM call vs synchronous block?**  Sync is simpler.  Latency
   per call is acceptable (we already pay 2-3s for capture+OmniParser
   each tick).  Start sync, revisit if voyage cadence suffers.
3. **What does the LLM see for the sector observation?**  A 16-row
   table, or a rendered diagram?  Start with the table; rendered
   diagram is a future optimisation.
4. **Does `consecutive_misses` reset on heading-rejection ticks?**
   No — heading-rejection means no fresh fit at all, so neither
   match nor miss.  Counter stays where it was.

## Related work / cross-references

- `docs/shore_following_design.md` — original LSQ tangent estimator
- `docs/lyapunov_point_pursuit_design.md` — waypoint generation
- `docs/steering_architecture.md` — Goal / Avoider / Loop layering
- `memory/project_destination_anchored_tangent_validated.md` — §13.21
- `memory/project_avoider_density_gate.md` — §13.22 (Fix B)
- `memory/project_hug_shore_local_vs_global_goals.md` — flags
  exactly this U-turn / wrong-side-hugging failure mode

## Tracking

- `§13.23 Phase A` — implement `ShoreSegmentMemory` + state machine
- `§13.23 Phase B` — LLM consult on HOLD
- `§13.23 Phase C` — live voyage validation + tuning

Move CLAUDE.md milestone tracking once Phase A lands.
