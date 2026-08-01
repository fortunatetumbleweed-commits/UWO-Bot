# Hug-shore navigation — test & simulation plan

A methodical test framework for the steering policy in
`brain/goals/hug_shore.py`.  Adapted from the PEGASUS / OpenSCENARIO
approach used in the autonomous-driving industry, sized down to our
specific problem (8-sector polar perception + simple ship dynamics).

## Why this exists

The steering policy has accumulated several behaviour-shaping
heuristics (intrusion discount, virtual wall, dead-end escape,
rotation penalty, etc.).  Each was added to fix a *specific* failure
mode observed in a live run.  Without a regression suite, new patches
can silently break old ones, and we don't have systematic coverage of
the failure-mode space.

This plan establishes:
1. A **regression test suite** of named failure scenarios from live
   logs — every patch must keep these green.
2. A **2D simulator** that synthesises the NavigationView from polygon
   shore + ship pose — lets us reproduce live failures offline at any
   speed.
3. A **scenario library** built from parametric factories (peninsula,
   bay, channel, …) — gives directional coverage of the structural
   failure-mode space.
4. **Property-based tests** that assert invariants the policy must
   always satisfy (no collision when escape exists, port↔starboard
   symmetry, bounded blind time).
5. **Coverage metrics + constant tuning** — grid-search the policy
   constants over the scenario library, score outcomes, justify every
   magic number with data.

## Inspiration / prior art

| Source | What we borrow |
|---|---|
| **[PEGASUS](https://www.researchgate.net/publication/330482709_Introduction_to_PEGASUS)** (German AV project) | 4-level scenario abstraction: functional → abstract → logical → concrete |
| **[OpenSCENARIO](https://www.appliedintuition.com/blog/asam-openscenario-v2)** | Idea of scenario-as-data, version-controllable, language-agnostic |
| **[Arena-Bench](https://arxiv.org/pdf/2206.05728)** | Category-based scenario factories + outcome metrics |
| **[Borenstein VFH evaluation papers](https://www.researchgate.net/publication/228523169_Steps_towards_the_automatic_evaluation_of_robot_obstacle_avoidance_algorithms)** | Parametric obstacle-density sweeps + path-quality metrics |
| **Hypothesis** Python library | Property-based testing for invariants |

What we DON'T borrow: the heavyweight USV simulators (Aeolus Ocean,
usv_sim_lsa) — those model 6-DOF dynamics, sensor fusion, COLREGs.
Our problem is *much* smaller (8-sector polar perception, discrete
steering, no global path planning), so we build a 200-line raycast
sim instead.

## Scenario abstraction levels

Borrowed verbatim from PEGASUS:

```
Functional:  natural language        "Ship hugs starboard along a
                                      straight south coast going east"
Abstract:    category + dimensions   straight_shore + (side, dist, length)
Logical:     parameterised template  straight_shore(side="starboard",
                                                    dist_range=[0.10, 0.25],
                                                    heading_offset_range=[-15, 15])
Concrete:    one instance            straight_shore("starboard", dist=0.12,
                                                    heading_offset=+5°)
```

Each test case lives at one level.  Regression tests are *concrete*
(specific failure logs).  Coverage tests are *logical* (parameterised
templates we sample from).  Property tests are *abstract* (work
across the whole space).

## Coverage taxonomy

### Dimensions

| Dimension | Values |
|---|---|
| **Shore shape** | open-water / straight / convex-corner (peninsula) / concave-corner (cove) / 90°-corner / channel / harbor-mouth / dead-end / archipelago |
| **Shore side relative to hug-target** | target / opposite / ahead / behind / both-sides / surrounding |
| **Hug-target distance** | touching (0–0.05) / tight (0.05–0.15) / corridor (0.15–0.25) / drifting (0.25–0.40) / lost (>0.40) |
| **Ship heading vs shore tangent** | parallel / oblique-toward / oblique-away / perpendicular-toward / perpendicular-away |
| **Ahead state** | clear / partial / blocked-frac / blocked-dist / collision-imminent / wall |
| **Hug side** | port / starboard (symmetry test) |
| **Memory state** | fresh / mid-recovery / just-collided / been-blocked-N-ticks |

### Coverage targets

| Level | Cases | What it catches |
|---|---|---|
| **Singleton** | each value of each dimension appears in ≥1 test | ~50 cases |
| **Pairwise** | every (dim1=v1, dim2=v2) pair appears together | ~150 cases — industry standard for "well-tested" |
| **Critical triplets** | failure-prone dim-triples (shore × ahead × distance) | ~80 cases |

Total: ~150–300 scenarios.  Each runs in <2 seconds.  Full suite in
under 10 minutes.

## Property assertions (universal invariants)

Properties hold across the *whole* scenario space, not any specific
test.  Verified with [Hypothesis](https://hypothesis.readthedocs.io/)
by random scenario sampling.

| # | Property |
|---|---|
| P1 | **No-collision-when-escape-exists** — if any sector has obstacle cost < 0.5, the bot does not pick a sector with cost ≥ 0.8 |
| P2 | **Symmetry** — mirroring the scenario (and hug-side) produces the mirrored action |
| P3 | **Eventual progress** — in any scenario with a free path of length > 5 ship-lengths, the bot reaches it within N ticks |
| P4 | **Bounded blind time** — no single hold gesture exceeds 1.5 s |
| P5 | **Calibration safety** — `wall_distance` never drops below `WALL_DIST_MIN` regardless of input sequence |
| P6 | **Sector validity** — `last_chosen_sector` is always in `CANDIDATE_SECTORS` |

Property violations are *always* bugs.

## File layout

```
uwo_v2/
├── sim/                                ← simulator package
│   ├── __init__.py
│   ├── world.py                        # Shore polygons + ship state
│   ├── perception.py                   # Raycast → 8 NavigationView sectors
│   ├── dynamics.py                     # Apply actions, advance ship, collide
│   ├── factories.py                    # peninsula(), bay(), channel(), …
│   ├── render.py                       # Matplotlib animation / PNG
│   └── scenarios.py                    # Named regression scenarios from logs
│
├── tests/                              ← flat pytest layout (existing convention)
│   ├── test_hug_shore_regression.py    # named-from-log scenarios
│   ├── test_hug_shore_categories.py    # parameterised over factories
│   └── test_hug_shore_properties.py    # Hypothesis property tests
│
├── tools/                              ← flat scripts (existing convention)
│   ├── replay_hug_shore.py             # load scenario → run → render PNG/GIF
│   └── tune_hug_shore_constants.py     # grid search over policy constants
│
└── docs/
    └── hug_shore_scenarios.md          # THIS DOC
```

Rationale:
- `sim/` is a top-level package (peer of `vision/`, `capture/`) because
  it's an importable library, not a script.  It imports the
  `NavigationView` Protocol and the `HugShoreGoal` — nothing else.
- `tests/` stays flat per existing convention (no subdirs).
- `tools/` houses the one-shot scripts (matching `tools/label_minimap.py`,
  `tools/calibrate_*.py`, etc.).
- Nothing in the runtime ever imports from `sim/` — it's a leaf
  dependency, safe to evolve independently.

## Phased delivery

| Phase | Deliverable | Est. | Status |
|---|---|---|---|
| **0** | This doc | 0.5 d | ⏳ in progress |
| **1** | `sim/world.py`, `sim/perception.py`, `sim/dynamics.py` minimum-viable | 1 d | next |
| **2** | `sim/scenarios.py` with first 5 named scenarios from logs + `tests/test_hug_shore_regression.py` | 1 d | after 1 |
| **3** | `sim/factories.py` with parametric shapes + `tests/test_hug_shore_categories.py` | 1 d | after 2 |
| **4** | `tests/test_hug_shore_properties.py` (Hypothesis-based) | 0.5 d | after 2 |
| **5** | `sim/render.py` + `tools/replay_hug_shore.py` | 0.5 d | independent |
| **6** | `tools/tune_hug_shore_constants.py` (grid search) | 0.5 d | after 3 |

**Minimum useful set: phases 0+1+2** — ~2.5 days, gives us regression
safety AND the ability to reproduce any future live failure offline.

The remaining phases can land incrementally — each one independently
adds value.

## Coordinates & units (sim convention)

- World is the **unit square** `[0, 1] × [0, 1]` (or larger if needed).
- Distances are normalised to `[0, 1]`, matching the live NavigationView.
- `nearest_dist = 0` means "land touching ship", `1.0` means "max view".
- `VIEW_RADIUS = 0.5` of world units — matches mini-map perception
  range.
- Headings follow the compass convention: **0° = North, 90° = East**
  (positive y = north, positive x = east).  Matches the live
  `nav.ship_heading_deg`.
- Sectors are ship-relative: 0=ahead, 1=ahead-stbd (+45°), 2=stbd
  (+90°), … 7=ahead-port (−45°).  Matches `vision/navigation_view.py`.

## Test selection guide

When a new live failure is observed:

1. **Add a concrete regression test** to `sim/scenarios.py` — pin the
   inputs and expected behavior exactly as observed.
2. **If the failure pattern recurs** in different geometries, promote
   to a logical scenario in `sim/factories.py` covering the pattern.
3. **If the pattern is "this property should always hold,"** add a
   property assertion to `tests/test_hug_shore_properties.py`.

## Maintenance discipline

- **Don't delete regression tests** — each one represents a specific
  bug that was fixed and shouldn't come back.  If a test no longer
  makes sense after a refactor, document why and update it; don't
  silently drop it.
- **Every new heuristic constant** in `hug_shore.py` should have
  ≥1 scenario test that's sensitive to it — flipping the constant
  should make that test fail.
- **Constant tuning output** from `tune_hug_shore_constants.py`
  goes into the constants file with a comment naming the tuning run
  + the scenarios that drove the choice.
