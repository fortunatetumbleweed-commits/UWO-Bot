# Milestone Status — 2026-05-25

One-line snapshot of where each milestone stands.  CLAUDE.md has a
shorter summary; this doc keeps the historical detail.

## Milestone 1 — Core Infrastructure ✅ DONE
ADB capture, top-left port-name OCR, port-map navigation, chrome
detector, `where_am_i()` location oracle, anti-cheat-aware tap timings,
interactive chat loop + named routines.

## Milestone 2 — Sea Navigation & Basic Trading ✅ DONE
World-map open from sea, OCR port labels, City Info → Go To City,
arrival detection, supply departure, auto-sell / auto-buy at market,
full autonomous trade loop via `run_task.py` + YAML task files, P/L
reporting.

## Milestone 2.5 — Data Flywheel ✅ DONE
`training/collector.py`, `training/claude_fallback.py`, supervisor
labeler's `/training` report, market-tile fuzzy matching.
Distillation triggers: 50+ examples for `market_tile`, 100+ per class
for `scene_type`.

## Milestone 3 — FSM & Robust Recovery ✅ MOSTLY DONE
What shipped (from the original Planner / Phase 5 / Phase 6 work in the
task list):
- **Planner architecture** (tasks #21–#24) — edge metadata, sail_to
  migrated, removed direct `where_am_i` call sites, Layer 4 replan
  wiring.
- **Phase 5c–5e** — OmniParser-backed chrome + button detection +
  per-frame cache; world-map exploration via OmniParser; OmniParser
  primary screen classifier; commit_via_positive_taps fallback;
  menu-item vs positive-button distinction.
- **Phase 6** — fingerprint registry (foundational + learned), agent
  perceive consults registry, defeat_dialog learning, learning hook
  for Claude scene analyses.
- **Flow completeness** (tasks #46–#52) — schema migration, runner
  bookkeeping, perceive guard, learning re-trigger, authoring
  validation.  See `docs/flow_and_plan_completeness.md`.
- **Plan completeness** (tasks #57–#65) — transaction tracking,
  positive-button fallback, Claude revamp on miss, gate learned-recovery
  on transaction count.
- **Scene Model** (tasks #107–#113) — `left_menu`, `action_buttons`,
  `composite_buttons`, `panels` detectors; `_assert_at_port` and
  `exit_to_overworld` migrated to use it.
- **Typed detectors** (tasks #120–#134) — `DialogModel`,
  `BuildingNpcOverlay`, structural `obstruction classifier`; substring
  interruptor loop replaced.  See `docs/dialog_and_event_models.md`.
- **explore_port + iterate_menu_items** (tasks #80–#101) — autonomous
  port exploration.

What's still open in this milestone:
- Task #25 — Cleanup recovery.py and unify `learn_edge`.
- Task #173 — Phase 4a v2: split `chromed` family into `port_interior`
  + `village_interior` (deferred).

## Milestone 4 — World Map Intelligence ✅ PARTIALLY DONE
Shipped:
- Pan / zoom world map (tasks #136–#154) — including stuck detection,
  scale calibration persistence, odometry-based recalibration, dead
  reckoning when no ports visible.
- Village support (tasks #157–#160) — `village_coordinates.json` baked
  from voyage.tw, `pan_to_village`, end-to-end sail to Berber etc.
- Lat / lon localisation (tasks #162–#166) — `read_latlon` primitive,
  affine calibration, water-tap localisation in `pan_to_port`, village
  arrival recognition.

Open:
- Read City Info panel (facilities, available goods, trade preferences,
  tax rates) and persist to port KB.
- Culture-aware trading (skip zero-value goods).
- Multi-hop route planning with resupply stops.

## Milestone 5 — Route Optimisation
Not started.  Query market KB across ports, score by ducats-per-minute,
re-explore stale markets, exploit Major Trends.

## Milestone 6 — Port Knowledge Base & Discovery ✅ PARTIALLY DONE
Shipped:
- `explore_port` action + `iterate_menu_items` primitive (tasks
  #80–#101).
- First-visit discovery overlay handling.
- Claude consult for unknown sub-menus + KB expansion.

Open: KB viewer in chat loop, building-type cross-port records
populated systematically.

## Milestone 7 — Combat & Sea Exploration
Not started.  See `docs/sea_capture_and_steering.md` for the planned
real-time sea-control architecture (scrcpy video tap-in + YOLO + async
loop).

## Milestone 8 — Autonomous Company Growth
Not started.  Adaptive strategy, fleet management, progression-ladder
awareness.

## Cross-cutting work that doesn't fit a milestone
- **Goal-aware perception design** (memory:
  `feedback_goal_aware_perception_design.md`) — layered redesign;
  partly implemented.
- **Temporal scene classifier** (task #114, #116; doc:
  `docs/temporal_scene_classifier.md`).
- **BotObservation v1** (tasks #117–#118) — singleton state with decay.
- **Family classifier** (tasks #171–#172) — MobileNetV3 trained on
  5 families; short-circuits classify_nav_state on high confidence.
