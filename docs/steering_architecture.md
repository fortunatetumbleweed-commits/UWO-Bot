# Steering architecture

Design captured 2026-06-01 from the §13.17 post-mortem and follow-up
discussion.  Drives the refactor that extracts collision avoidance out
of `HugShoreGoal`.

## Goal of the refactor

`HugShoreGoal` today owns too much — waypoint generation, driver-mode
dispatch, the picker (collision avoidance), FSM state, HUD plumbing,
action emission, and trace logging.  Conflating these:

- makes the picker's behaviour hard to reason about (multiple cost
  computations + override paths + fall-throughs in one function);
- makes algorithm swaps (VFH+ vs SND) require touching unrelated state;
- makes adding configuration (μ-weights, safety distance) a sprawling
  change because parameters thread through several layers.

The refactor pulls these into named modules with one direction of data
flow.

## Layering

```
┌─────────────────────────────────────────────────────┐
│ Mission                                             │   ← deferred until first
│ - sequences and interrupts Goals                    │     multi-step voyage YAML
│ - GoalStack pattern for "visit on discovery"        │
└────────────────────┬────────────────────────────────┘
                     │ provides current Goal
                     ▼
┌─────────────────────────────────────────────────────┐
│ Goal                                                │   ← HugShoreGoal lives here
│ = WaypointRule + TerminationCondition               │     (today)
│ - next_waypoint(nav, pose) → Waypoint               │
│ - is_done(nav, pose, history) → GoalOutcome?        │
└────────────────────┬────────────────────────────────┘
                     │ called by
                     ▼
┌─────────────────────────────────────────────────────┐
│ Steering Loop                                       │   ← run_hug_shore_loop
│ - per-tick perception + capture loop                │     today
│ - calls Goal for waypoint + termination check       │
│ - calls ConfigSelector for avoider config           │
│ - calls Avoider                                     │
│ - emits action                                      │
│ - owns FSM state, HUD state, calibration EMA,       │
│   trace logging                                     │
│ - owns operational safety cap (max_ticks,           │
│   stuck_for, signal kill) — separate from Goal      │
│   termination, which is plan semantics              │
└────────────────────┬────────────────────────────────┘
                     │ uses
                     ▼
┌─────────────────────────────────────────────────────┐
│ CollisionAvoider                                    │   ← VFHPlusAvoider etc.
│ select(nav, desired_heading, candidates, config)    │     pure-ish, config-driven
│   → AvoiderResult(chosen_sector, hdg, diagnostics)  │
└────────────────────┬────────────────────────────────┘
                     │ via
                     ▼
┌─────────────────────────────────────────────────────┐
│ Actuator                                            │   ← sea_actions today
│ hold_left / hold_right + jitter + ADB taps          │
└─────────────────────────────────────────────────────┘
```

The avoider doesn't know what a "hug-shore" is.  The goal doesn't know
what a histogram is.  The loop wires them.

## Goal = WaypointRule + TerminationCondition

A Goal is **one atomic navigation task** — bounded by a single
termination event (arrived, entered port, shore lost, ticks elapsed).
Two composable pieces:

```python
class WaypointRule(Protocol):
    def next_waypoint(self, nav, pose) -> Optional[Waypoint]: ...

class TerminationCondition(Protocol):
    def is_done(self, nav, pose, history) -> Optional[GoalOutcome]: ...
    # None = continue; GoalOutcome(reason, success=T/F) = done.
```

Concrete `TerminationCondition`s: `ArrivedAt(lat, lon, radius)`,
`ElapsedTicks(n)`, `ElapsedWallTime(seconds)`, `EnteredPort()`,
`DiscoveredAny(kind)`, `ShoreLost(side, after=N)`, `StuckFor(ticks)`.
Composition via `AnyOf(*conds)` / `AllOf(*conds)`.

Today's behaviour:

```python
Goal(
    waypoint  = HugShoreWaypointRule(side="port", dest=(5.71, 32.12)),
    terminate = AnyOf(ArrivedAt(5.71, 32.12, radius_deg=0.01),
                       ElapsedTicks(1000)),
    name      = "hug_to_luxor",
)
```

## On Avoider switching vs single-Avoider adaptive config

Initial design considered swapping Avoider algorithms based on a
narrow-passage detector.  Literature research (BARN Challenge 2022
winners; Marder-Eppstein 2010 Office Marathon; Macenski 2023 nav2;
Brock-Khatib 2002) consistently rejects Avoider-level switching:

- Marathon: ~60% of incidents within 2s of controller transition.
- BARN: single-controller adaptive params beat switched-controller
  entries in 7 of top 10.
- nav2 / Waymo / Cruise: regime selection at the deliberative /
  behaviour-tree layer, never at the reactive layer.

**Decision:** single `CollisionAvoider` implementation, varying
`AvoiderConfig` per tick via a `ConfigSelector`.  No instance swap, no
cold-state handoff, no boundary kinks.  If the data eventually shows
VFH+ truly cannot navigate a regime at any parameter setting, that
decision belongs at the Goal/Mission layer above the Steering Loop
(different Goal class for that regime), not inside the avoider stack.

## ConfigSelector with Schmitt-trigger hysteresis

```python
class ConfigSelector:
    """Picks an AvoiderConfig for this tick. Single Avoider class;
    parameters vary by situation."""
    def __init__(self, open_cfg, narrow_cfg=None,
                       enter_dense_count=5, exit_dense_count=3,
                       min_dwell_ticks=2):
        ...
    def pick(self, nav) -> AvoiderConfig: ...
```

Hysteresis pattern from Quinlan-Khatib 2003 / Marder-Eppstein 2010:
20% threshold gap (5 dense to enter "narrow", 3 to exit) + 1 s min
dwell.  Prevents meta-oscillation at the regime boundary.

## CollisionAvoider — single algorithm, multiple configs

```python
class CollisionAvoider(Protocol):
    def select(
        self,
        nav: NavigationView,
        desired_heading_deg: float,
        candidate_sectors: tuple[int, ...],
        config: AvoiderConfig,
    ) -> AvoiderResult: ...

@dataclass(frozen=True)
class VFHPlusConfig:
    safety_dist:       float = 0.12
    target_weight:     float = 5.0    # μ1 (Ulrich-Borenstein 1998)
    inertia_weight:    float = 0.0    # μ2 — bumped after phase 2
    smoothness_weight: float = 0.0    # μ3 — deferred (needs cross-tick state)
```

`VFHPlusAvoider` implements the canonical 3-cost formula from VFH+
(Ulrich & Borenstein 1998).  Initial config defaults reproduce today's
behaviour (μ2 = μ3 = 0); phase 2 adds μ2 = 2 (the canonical 2/5 ratio
the paper credits with eliminating narrow-corridor oscillation).

Backwards-compatible `ArgminAvoider` exists as a thin wrapper around
today's `_point_pursuit_pick_sector` to make the seam introduction a
no-op behaviour change.

## Failure handling

On `terminate.is_done(...)` returning `GoalOutcome(success=False)`:
- Steering Loop calls `sail_stop`.
- Logs failure reason + last 5 ticks of (pose, heading, commanded
  action, sector summary, waypoint) to trace.jsonl.
- Returns the `GoalOutcome` to the caller.

No recovery / fallback-goal logic until we have failure-trace data.
Hard-coding recovery now would be guessing.  See
`memory/project_goal_failure_handling.md`.

## Cross-cutting safety vs Goal semantics — separate them

- **Steering Loop** owns operational safety: absolute `max_ticks`,
  `stuck_for(N)`, signal kill.  Not part of plan semantics; the bot
  can't run forever or thrash forever.
- **Goal** owns plan semantics: "hug for 10 min" is
  `ElapsedWallTime(600)` inside the Goal's TerminationCondition.

Mixing them up produces the "max-ticks ran out so the goal thinks it
succeeded" bug class.

## Migration phases

1. **Extract `CollisionAvoider` Protocol + `ArgminAvoider`**: thin
   wrapper around today's `_point_pursuit_pick_sector`.  No behaviour
   change.  Tests pass unchanged.  Goal: prove the interface shape.
2. **Add `VFHPlusAvoider` with μ2 inertia**: canonical 3-cost formula
   from VFH+ paper, defaults μ1=5, μ2=2, μ3=0.  Plug into new driver
   mode `vfh_plus`.  Re-test in Nile — single highest-impact fix per
   the research.
3. **Extract `WaypointGenerator`** and `LyapunovWaypointGenerator`.  No
   behaviour change.  Seam is fully clean.
4. **`ConfigSelector` + narrow-passage detector + adaptive
   `safety_dist`**: 0.12 → 0.06 when ≥5 dense sectors observed.
   Hysteresis on detector (5 enter / 3 exit / 2-tick dwell).
5. **Goal layer split**: extract `WaypointRule` /
   `TerminationCondition` protocols + concrete conditions
   (`ArrivedAt`, `ElapsedTicks`, etc.) + `AnyOf`/`AllOf` composition.
   HugShoreGoal becomes a thin assembly of rule + termination.

Deferred:
- **`Mission` (sequential goal list)** — when first multi-step voyage
  YAML lands.
- **`GoalStack` + `InterruptOn`** for "visit on discovery" — when
  ExploreGoal is designed.  This is a higher-level task that contains
  multiple navigation tasks; see
  `memory/project_steering_architecture_layering.md` for the rationale.
- **`SmoothNDAvoider`** — only if empirical data shows VFH+ cannot
  handle a regime at any parameter setting.  Even then, surface the
  decision at the Goal layer (different Goal class), not at the
  Avoider layer.
- **8 → 16 sectors** — orthogonal migration touching the
  NavigationView protocol; significant.  Bigger impact than any
  Avoider tweak but larger blast radius.

## References

- Ulrich & Borenstein 1998 — *VFH+: Reliable Obstacle Avoidance for
  Fast Mobile Robots*, ICRA.
- Durham & Bullo 2008 — *Smooth Nearness-Diagram Navigation*, IROS.
- Minguez & Montano 2004 — *Nearness Diagram (ND) Navigation*, IEEE T-RA.
- Marder-Eppstein et al. 2010 — *The Office Marathon*, ICRA.
- Macenski et al. 2023 — *The Marathon 2: nav2*, JFR.
- Brock & Khatib 2002 — *Elastic Strips*, IJRR.
- Quinlan & Khatib 2003 — *Elastic Bands*, ICRA.
- Xiao et al. 2022 — *Autonomous Ground Navigation in Highly
  Constrained Spaces* (BARN Challenge), IEEE RAM.
- Gat 1998 — *On Three-Layer Architectures*, AAAI Press.

See also: `docs/lyapunov_point_pursuit_design.md` (the §13.17 waypoint
generator design); `memory/feedback_math_bugs_no_patches.md` (process
lesson from the degrees-as-radians bug); the two project memories on
this design above.
