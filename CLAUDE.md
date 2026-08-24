# UWO Bot — Claude Code Context

## Project Overview
A Python bot that plays **Uncharted Waters Origin (UWO)** autonomously
on an Android phone, mirrored to Mac via scrcpy.  Goal: grow the
in-game company across **trade**, **exploration**, and **combat** — all
three in parallel.  Navigation supports them, not the other way around.
Long-term goal is self-learning: earn ducats → buy better gear / ships
→ unlock new routes → discover more → earn more.

## Knowledge Map — where to find / put things
The project's knowledge lives in four places, each with different
freshness guarantees:

| Where | What | Authoritative? | Updates how |
|---|---|---|---|
| **Code + tests** | Bot's actual behaviour | ✓ ground truth | Every commit |
| **`memory/`** (`~/.claude/projects/.../memory/`) — index in `MEMORY.md` | Per-incident lessons, user preferences, gotchas | Notes about truth | Auto, when I learn something new or you correct me |
| **`CLAUDE.md`** (this file) | Concise project-level rules, current milestone, knowledge map | Project intent | Manual; **must be updated** when major decisions or phase shifts happen — see `memory/feedback_claudemd_update_on_decisions.md` |
| **`docs/`** | Long-form designs, schemas, milestone detail, game mechanics | Authoritative when current; can drift | Manual; updated alongside code |

When in doubt about where to put something new:
- A behaviour rule that applies to every future change → memory `feedback_*` entry
- A non-obvious project fact (e.g. game quirk) → memory `project_*` entry
- A pointer to an external system (Linear, Slack, dashboard, etc.) → memory `reference_*` entry
- A design decision that affects how features should be built → CLAUDE.md (here) + a doc in `docs/`
- A long explanation, diagram, table, or schema → `docs/`

## Trace Viewer Reports
Every live run writes an action trace to `data/sessions/trace_<name>_<ts>/`.  To review it:

```bash
python -m tools.trace_viewer data/sessions/trace_<name>_<ts>/
open -a Safari "file://$PWD/data/sessions/trace_<name>_<ts>/viewer.html"
```

**Always open the report in Safari as part of generating it** — not as a separate step to be
asked for.  The report exists to be looked at frame by frame; left as a path on disk the
review does not happen.  When summarising, name the frames that matter ("frames 8-12 are the
repeated taps at the hardcoded close-X"), because the review navigates to specific ticks.

`--qwen` adds a Qwen read per frame (slow); `--rebuild` ignores cached component data.
Generation reuses perception the bot already recorded where available; see
`actions/action_trace.py::_capture_with_perception`.

## Voyage Evaluation Procedure
Standard, single-command voyage evaluation against the canonical
Cairo→Y-tip reference path:

```bash
python -m tools.evaluate_voyage data/sessions/explore_port_<ts>/
```

The procedure:
1. Loads the standard reference from `data/reference/standard_nile.json`
   (canonical session, outbound clip, tolerance, lookahead).
2. Runs `tools/compare_paths` to identify deviation segments vs
   the reference path.
3. Drills into each segment's tick data to surface *major issue
   ticks* — rejection bursts, OCR jumps, picker fires, §13.21 commit
   resets — and infers the likely root cause.
4. Emits VERDICT (PASS / WARN / FAIL) + per-segment narrative +
   ranked issue-tick list to `voyage_eval.md` and `voyage_eval.json`
   in the session dir.
5. Exit code reflects the verdict so this can gate CI / scripts.

Use this every voyage.  Jump straight to the surfaced issue ticks in
`tick_viewer.py` (press `g`, type the tick number, Enter) to inspect
the frame + trace context.

## Active Rules
- **Never act blind — always know WHERE and WHAT STATE, else re-perceive** —
  before taking any action the bot must know both (1) its **location**
  (port_overworld / market / building / sub_menu / sea / village / world_map …)
  and (2) its **interaction state** (mid-transaction with the bot's own dialogs,
  vs. blocked by an unsolicited popup like daily-news / promo / event).  If either
  is unknown it must **re-perceive** — never guess or tap.  If still unknown after
  re-perceiving, **escalate / learn**, don't flail.  Some facts are **invariants**:
  a `port_overworld` ALWAYS has a port name — failing to read one there is an
  anomaly to flag + retry (re-capture, stronger read), NOT a silent `None`.  This
  is the foundation for the action-verification/recovery ladder
  (`docs/action_verification_and_recovery_design.md`).
  - **Dialog / popup handling — who caused it decides how to clear it:**
    - **From the bot's OWN action** (a Confirm / Result / Negotiation after a
      buy / sell / commit / recruit): **complete the action** by clicking through
      its action buttons (Confirm / OK / Purchase / …), never dismiss it.
    - **System / unsolicited** (daily news, promo, event, announcement — NOT
      triggered by the bot): **dismiss** it — the Back button, or the dialog's own
      **close X** (an X-shaped icon in or near the **top-right** of the popup).
      Never the screen-corner (at sea/port_overworld that is the ☰ hamburger —
      see `memory/project_home_button_is_chromed_only_escape`).
- **Surgical changes** — touch only what the task requires.  Don't refactor
  adjacent code, don't add unrequested features, don't introduce
  abstractions for hypothetical use.
- **Scoped test runs** — run only the tests for modules you edited;
  escalate to the full suite when changes touch ≥3 subdirs or critical-
  path modules (perception, recovery, planner, FSM core).
- **Flow completeness** — a flow is complete only when it (1) ends at a
  recognised state AND (2) contains at least one positive transaction
  (confirm / buy / sell / recruit / set-sail).  Back / Home = Cancel,
  not progress.  See `docs/flow_and_plan_completeness.md`.
- **Plan completeness** — Claude-generated plans are recommendations
  bound by the same rule.  On step-miss, refine via
  `commit_via_positive_taps` or revamp via Claude.  Plans with zero
  transactions must NOT be persisted as learned recoveries.  See
  `docs/flow_and_plan_completeness.md`.
- **CLAUDE.md stays current** — when a major decision is made or a phase
  status changes, propose a CLAUDE.md edit in the same session.  See
  `memory/feedback_claudemd_update_on_decisions.md`.
- **Per-frame perception sharing** — capture the screenshot once per tick,
  pass the same `Image` and reuse `parse_fast_cached(img)` /
  `_ocr_frame(img)` results across detectors.  Don't re-shoot or re-parse
  inside helpers — each capture+OmniParser pair costs ~2-3s.  When a
  function needs vision, accept `frame` as an argument; only call
  `capture_screen()` at the top of the loop.
- **Transactional primitives gate on location** — any function that taps
  at a fixed sea / port HUD coordinate (rudder, L/R arrows, harbor
  depart, market buttons) MUST call `_assert_on_sea` / `_assert_at_port`
  before tapping.  A rudder tap fired on the wrong screen (post-arrival
  port_overworld) opened an unrelated quest dialog once — gating is the
  fix.  See `actions/sea_actions.py::_assert_on_sea`.
- **All screen input goes through `actions/ui.py`** — never hand-roll taps, swipes,
  key presses or waits in new code.  `ui.tap_element` / `ui.tap_text` take a DETECTED
  element or a label, `ui.scroll` / `ui.back` / `ui.settle(kind)` carry the jitter, and
  `ui.tap_at(x, y, why=…)` is the loud, logged exception for genuinely calibrated HUD
  controls.  This exists because the two habits it replaces each caused a live failure:
  a `time.sleep(1.0)` scroll loop tripped the ANTI-CHEAT and terminated the game
  (2026-08-21), and hardcoded coordinates silently mis-tap whenever the game re-bakes
  its camera-cutout offset — the same Village Info panel rendered ~110px apart between
  two sessions, so every absolute band in the trade-list parser rejected every row.
  Rule of thumb: **if you are about to write a number that means "where on the screen",
  find the element instead.**  Layout is stable; position is not.
- **Unit tests never touch the phone** — `tests/conftest.py` blocks any `adb`
  subprocess and hands tests offline stand-ins (a blank 2400x1080 frame, a canned
  `dumpsys` rotation, recording stubs for `adb_actions` input and the sea-steering
  press-and-holds).  A test that genuinely needs hardware must be marked
  `@pytest.mark.device` and is skipped unless you pass `--device`.
  This is not hygiene for its own sake: before the guard existed the suite fired 6
  `input swipe`s and 2 `keyevent 4`s at the live game — `test_hug_shore_bug2_wall_ahead`
  drove `HugShoreGoal.tick()` into the real steering primitives, and
  `test_village_recognition` drove `SailToGoal` into a real `press_back()`, which on the
  port overworld raises "Exit Game?".  Running the unit suite steered the ship and
  panned the map.
  Two corollaries when writing tests:
  - **Reset session state you depend on.**  Production caches are keyed by frame bytes,
    and every test builds the same blank frame, so they collide.  `_OBSTRUCTION_NONE_CACHE`
    (5s TTL) made four interruptor tests order-dependent — each passed alone and failed in
    the file.  conftest clears the known frame-keyed caches, the daily-news suppression
    global, and the Moondream family cache; add new ones there as they appear.
  - **State every attribute a `MagicMock` stands in for.**  MagicMock auto-vivifies
    unknown attributes as TRUTHY mocks, so when `has_hamburger` was added to the chrome
    detector every existing chrome mock silently began reporting "this is an overworld"
    and the Home-button branch became unreachable in tests — in two separate files.

- **Expected screens are multi-anchored; positive-button search is for the UNEXPECTED** —
  on a screen the bot MEANT to be on, an action must be anchored by several things at once:
  the screen's identity, the specific control (detected, not remembered), and the expected
  post-condition.  Searching for "the positive button" there is a guess dressed as a plan.
  `find_positive_button_for_context` is for the other case only: something unexpected
  interrupted a goal the bot was PURSUING and had already COMMITTED an action toward, so a
  transaction is genuinely half-finished and the goal says which button closes it.  With no
  goal there is nothing to finish and the search degrades to "tap whatever looks positive" —
  live 2026-08-22 that tapped `Trade Info` and a panel title on the Market landing page,
  because POSITIVE_LABELS contains the word "trade" and matching is by substring.
  In this game a POSITIVE button is identified by its **yellow/gold background**, not its
  wording (measured: the dialog OK is 64-91% gold pixels; Trade Info, Trade Points and
  Requested Trade Goods are 0%).  `brain.commit_actions.has_positive_background` enforces
  that; the word lists are a weak secondary signal and should eventually be LEARNED from
  observed real commit buttons rather than hand-listed.
  Today FOUR different notions of "positive" coexist — `POSITIVE_LABELS` (substring),
  goal keywords + `UNIVERSAL_COMMIT_LABELS`, `detect_commit_buttons` (colour+layout),
  `_DIALOG_OK_WORDS`, plus `_tap_depart_button`'s own "depart"/"supply" scan.  That is the
  "one canonical implementation per concern" rule being violated, and it is why fixing the
  trade false positive in one place left the escalation path exposed.

- **ONE sell flow, several goals** — selling at a market is a single flow: switch to the
  Sell tab → stage goods → tap Sell → handle Confirm/Result/Negotiation.  What differs
  between callers is only WHICH goods and HOW MANY of each:
    - `goal="profit"`  — every profitable good            [trade runs]
    - `goal="clear"`   — every non-kept good              [free cargo for a barter]
    - `goal="down_to"` — trim each good to a target qty   [surplus of a MATERIAL]
  Today `sell_goods` and `sell_down_to` are two implementations of that same flow, and the
  cost is concrete: the "N / M" Cargo-bar confusion had to be found and fixed TWICE (once
  when opening the quantity dialog, once when verifying it closed), and only one of the two
  paths backs out of the market on abort — so a false abort there DISCARDS a basket that
  was correctly staged (live 2026-08-22: 981 Ebony loaded, Sell one tap away, thrown away).
  Selection is the part that should vary; staging, committing and the dialog chain are the
  part that must not.  Same rule as "one canonical implementation per concern" — when a new
  selling need appears, add a GOAL, do not fork the flow.

- **ONE loop — the task drives the state machine** → `docs/one_loop_task_drives_state.md`.
  Two things are going on when the bot acts: a TASK (which owns the *sequence*: plan →
  gather → barter → route+sell, tracked in `brain/mission_progress.py`) and a UI STATE
  MACHINE (port_overworld / harbor / market / sea / village — which owns *no* sequence,
  only "where am I and what can I do here").  Today the task is chopped into sub-loops
  living INSIDE actions, each with its own private recovery policy — that inverts the
  hierarchy, and a low-level action ends up deciding the fleet's position in the world
  without knowing what the bot is trying to achieve.
  Measured cost in one day (2026-08-22): `open_world_map`'s loop judged `village` "not
  port/sea" and pressed Back until the fleet was AT SEA — leaving the village the mission
  had just sailed to; `navigate_to_building("harbor")` saw "sailing to Melanesian Village",
  judged it "not the harbour", and pressed Back, cancelling a departure that had SUCCEEDED
  — four times, 18.5 minutes; `_read_owned_via_sell` tapped a label matching the page title
  and left the market it was standing in.  Each was locally reasonable and globally wrong,
  because locally was the only scope it had.
  Rules: a primitive **never** presses Back to make the screen match its expectation (the
  screen is ground truth; the stale expectation is what updates) and **never** walks the
  fleet out of a settlement.  A loop inside a primitive may only wait for that primitive's
  OWN effect ("did the map open?" ✓ / "am I somewhere else now?" ✗ — that is the task
  runner's question).  After ANY correction — state adopted, dialog cleared, popup
  dismissed — the loop **re-asks the task what to do next** rather than resuming a
  half-finished intention.
- **Anti-cheat tap discipline** — the game fingerprints automated input:
  - **Never burst-tap** (≥ 3 taps in < 1 s).  Burst-tapping the L/R
    arrows caused the spiral that fired the anti-cheat popup.  Use
    `turn_left/turn_right(taps=N)`'s built-in inter-tap jitter; do not
    loop tap manually.
  - **Never run a fixed-cadence action loop** — anti-cheat detects
    regular intervals.  Always jitter sleeps (e.g.
    `random.uniform(low, high)`).
  - **Every ADB tap** already includes the 0.3-0.8s post-tap delay
    — keep that.
- **Decisions at the right level — escalate, don't absorb** — a primitive's
  job is to *do one thing* and *report what happened*, not to decide what
  to do next.  When a primitive can't complete (button missing, wrong
  state, unexpected popup), it must return a structured failure and let
  the caller — the FSM / planner / goal — decide whether to retry,
  fall back, escalate to Claude, or abort.  Symptoms of this rule being
  violated:
  - inner loop retries forever and the bot gets stuck;
  - "I'll dismiss any dialog I find" logic inside a market function
    (dialog handling belongs in `perceive` / interruptor layer, not
    inside `auto_buy`);
  - recovery path embedded in a building helper instead of in
    `brain/recovery.py`.

  Quick test: *"if this function fails, does anything above it learn
  about the failure?"*  If no, the level is wrong.
- **One canonical implementation per concern** — when you find yourself
  writing logic that looks like something else (dialog detection, state
  assertion, button-finding, back-to-overworld navigation), find the
  existing implementation and use or extend it.  If two have already
  diverged, figure out which is correct and consolidate.  Examples of
  shared concerns that have ONE home today:
  - dialog detection → `vision/region_detectors/DialogModel`
  - obstruction classification → `vision/obstruction_classifier`
  - state classification → `brain/perceive.classify_nav_state` +
    fingerprint registry
  - "am I at the right place to tap?" → `_assert_on_sea` /
    `_assert_at_port`
  - button finding → `actions/sail_actions._find_button` (OmniParser-backed)

  When the existing one doesn't fit your case, generalise it — don't
  fork a parallel version.
- **When refactoring existing offenders** — these rules are most useful
  when written, not when discovered after the fact.  If during a task
  I notice buried-high-level logic or duplicated concerns nearby:
  - **don't bundle the refactor with the current change** (surgical
    changes rule applies);
  - **flag it inline** — note the smell in the response and propose a
    follow-up cleanup commit;
  - **the user picks the priority** — sometimes the offender is fine
    to leave until it bites again.

## Architecture (skeleton)
```
Android Phone (game)
        │ ADB screencap
        ▼
   capture/          raw frame (2400×1080 PNG)
        │
        ▼
   vision/           L0–L4 perception pipeline → SceneInventory
        │            (family_classifier, chrome, OmniParser, OCR, KB cache, Claude Vision)
        ▼
   memory/knowledge/ port / building / market / scene records
        │
        ▼
   brain/            perceive → reason → act → learn
                     (FSM, planner, goal stack, plan completeness)
        │
        ▼
   actions/          tap / swipe / press_back, sail / market / world-map primitives
        │ ADB input
        ▼
Android Phone (game)
```

Detail:
- **★ NEXT-PHASE FOUNDATION (read first, 2026-08-09)** →
  `docs/next_phase_architecture_2026-08-09.md`.  Consolidated design for the next phase:
  the THREE knowledge layers (strategy → task → control), the self-correcting
  TASK EXECUTOR (done-conditions + attempt memory: remember what worked / didn't →
  no-progress→try-different→stuck→escalate), progress/change detection
  (`docs/state_change_detection.md` — structured, not pixel-diff), the tiered
  reasoning cascade (cache → Qwen → Claude, learn-once), the knowledge base
  (control/task/strategy KBs; play-learned + human text/URL), and the current-code
  gap map + build order (HUD readers → task executor → done-conditions → decision
  cache).  Supersedes/consolidates the earlier scattered design notes.
- **★ Perceive→Act→Verify act-layer substrate (PROPOSED, 2026-08-19)** →
  `docs/perceive_act_verify_substrate.md`.  The primitive-level realization of the
  self-correcting executor: every action declares an EXPECTED post-condition, then
  re-perceives and compares expected-vs-actual — no assumed-state blind actions.
  L0-L3 verification (frame_diff → verify.py → targeted → perceive), an outcome
  taxonomy (match / no-change / wrong-state / refused), and a 4-phase migration.
  Motivated by the blind-`press_back` "Exit Game?" loop; see
  `memory/feedback_perceive_act_verify_expected_vs_actual.md`.
- **★ Barter command flow — BUILT 2026-08-20, NOT YET RUN LIVE** →
  `docs/barter_command_flow.md` (full gap table + build order + what each piece does).
  One typed command drives the mission:
  `python run_barter.py "barter <good> at <village>[, then take the route <name> |
  and sail to <port>]"` (`--dry-run` plans without sailing).  Recipe = invariant
  materials + 6h-volatile quantities, so the plan CONSTRUCTS the live recipe
  REMOTELY from port before gathering — `actions/village_check.read_village_barter_remote`
  (Base + Barter tabs, scroll-accumulated, source pins learned, KB invariants written
  back).  The CHECK runs BEFORE the graph is built, because every node is
  parameterised by it.  Plan: `brain/barter_quantity.plan_barter_rounds` with
  `free_space = capacity − cargo − 7-day supply reserve`, bounded by the PEAK hold
  (`max(Σ needs, output)` per round — the gather must be carryable, not just the
  output).  Graph: gathers → **supply_verify** → village → barter → route/sail tail →
  sell (`brain/mission.MissionTail`).  First live mission completed 2026-08-20
  (+66.6M ducats); see `memory/project_village_find_worldmap_2026-08-20.md`.
  ⚠️ Unvalidated live surface: the Village Info panel navigation and the main-menu
  fleet read (`actions/fleet_status.py`) — supervise the first run.
- Full vision pipeline → `docs/vision_pipeline.md`
- `where_am_i()` design + location vocabulary → `docs/where_am_i_design.md`
- Architecture overview (broader) → `docs/architecture_overview.md`
- **Layered bot architecture (task / reasoning / skills / perception) — TARGET
  design (2026-08-07)** → `docs/bot_architecture_layers.md`.  Task layer takes
  natural-language tasks (goals + schedule + concurrent monitors); reasoning/
  mission layer below owns "where am I + how to achieve the goal + recover";
  skills layer wraps the existing goals/actions; perception is the structured
  `PerceivedState` substrate.  The **reasoning fallback** (LLM over structured
  state + intent, whitelisted actions; fixes wrong-place + headless-escalation)
  → `docs/reasoning_fallback_layer_design.md`.
- **Perception consolidation (A0–A3, A2 structured PerceivedState) — IN PROGRESS
  (2026-08)** → `docs/perception_backlog.md` + `docs/a2_perceived_state_implementation_plan.md`.
  Family CNN owns coarse structure (chromed ≠ overworld); panel identity from the
  left-menu vocab; general `GridDetector` reads same-layout grids/lists; market
  goods reader is OmniParser-detected (no hardcoded grid).  Principle: **detect,
  don't hardcode; nav fast, buildings/port precise via OmniParser.**
- **Affordance learning & curiosity — grow the KB from play (design, 2026-08-07)**
  → `docs/affordance_learning_and_curiosity.md`.  OmniParser sees elements the KB
  doesn't (`Trade Points`); learn them by reasoning + **safe** experimentation
  (open info → Back), detect operation-completion signatures, record into an
  affordance KB.  **Safety-gated**: probe only reversible/info affordances; never
  experiment on spend/confirm/red-gem actions.  Extends the Data Flywheel +
  learn-once.
- KB record schemas → `docs/kb_record_schemas.md`
- Scene model substrate → `docs/scene_model_design.md`
- Dialog / event typed detectors → `docs/dialog_and_event_models.md`
- Sea capture loop + real-time plan → `docs/sea_capture_and_steering.md`
- Navigation models (mini-map + shoreline) — corpus, training, wire-in → `docs/navigation_models_status.md`
- **River/shore exploration — perception/mapping/planning split** → `docs/exploration_navigation_layers.md`
  (Read this before touching `JunctionDetector`, `JunctionGraph`,
  `TremauxPicker`, or `water_skeleton.py`.  Defines what each layer
  owns and what crosses between them.  The "perception is stateless,
  mapping owns memory, planning consumes both" principle is load-
  bearing for the replay-test framework.)
- **Tactical substrate — CURRENT (single hugging-side bankline, 2026-07-31)**
  → `brain/ai_nav/layers/loop_tactical.py` (`LoopTactical`, `--tactical
  loop`).  Design doc `docs/loop_navigation_design.md`; milestone
  `memory/project_loop_bankline_roundtrip_2026-07-31.md`.  The live
  substrate is ONE hugging-side bankline, not a whole loop:
  1. **LOOP = raw ship-water-CC contour** — full-coverage invariant (the
     water body the ship is on must be entirely in the loop; no erosion,
     which distorts/splits complex shapes like the strait).
  2. **Desired direction = ship HEADING** (perception, not a compass seed).
  3. **FOOTHOLD = forward-port arc scan** — sweep abeam→straight-ahead on
     the hug side, nearest LAND hit (reacts to land on the bow, not just
     abeam; excluding "behind" is load-bearing).
  4. **NAV LINE = bank forward-trace to the frame-edge exit / dead-end**,
     offset inward per-point by `min(hug≈14, DT)`, then pocket-cut + light
     DP.  Reflex 40px / tactical 80px along it; commit = ship→reflex.
  Dead-end/lake "falls out free" (the one bank curves around and back).
  **Validated end-to-end**: Cairo→lake→Cairo round trip, 734 ticks, 4
  flips, zero thrash — beats the prior best (861/13).  Steering: PID is now
  the live default (`--no-pid` for the sim).

  **[SUPERSEDED 2026-07-31] anchor substrate (bank tracer)** →
  `tools/bank_tracer.py` (`trace_bank_to_edge`) +
  `brain/ai_nav/layers/tactical.py` (`LookaheadTactical`, `--tactical
  lookahead`) — anchor policy (sticky / migrate / hold(pocket_ahead) /
  reach-gate) over a traced polyline ENDPOINT, which flipped at
  straits/islands/dead-ends.  Kept for the pytest suite + history; the
  loop substrate replaces it as the default going forward.  See
  `memory/project_reflex_wp_arclength_lookahead_2026-07-28.md`.
  Regression suite: `tests/tactical_scenarios/` — **perceive-from-frames
  replays** that drive the real `AiNavPipeline` over recorded frames via
  `FileVisionSource` (ensemble heading, dead-reckoned position), NOT
  injected trace values.
- **[OBSOLETE 2026-07-28] Edge-anchored centerline extraction** →
  `docs/edge_anchored_centerline_design.md` (was proposed to retire the
  skeleton substrate; itself superseded by the bank tracer above and
  never wired.  Prototype `tools/edge_centerline_prototype.py`.  Kept
  for history.)
- **[OBSOLETE substrate] LookaheadTactical SEARCH-mode tracker** →
  `docs/tactical_search_tracker.md` (frame-edge dest-continuity logic;
  its skeleton/centerline upstream is retired.  The "keep the dest if
  it's still on a water frame edge" idea survives in the bank tracer's
  frame-edge exits.)
- **[OBSOLETE substrate 2026-07-24] LookaheadTactical original walker** →
  `docs/lookahead_tactical_design.md` (skeleton-walk lookahead; kept for
  historical context).
- **Tactical LOCK → mission-layer refactor sketch** →
  `docs/tactical_lock_mission_refactor.md` (design draft, not yet
  implemented — LOCK decisions should live in mission not tactical).

## Coding Conventions
- Modules independently testable with mock ADB.
- Every ADB tap includes a small random delay
  (`time.sleep(random.uniform(0.3, 0.8))`).
- Screen templates live in `vision/assets/` as PNG.
- Game state is single source of truth — updated from vision output each tick.
- Log every action with timestamp, state before, action, outcome.
- Type hints throughout.
- FSM states in separate files under `brain/states/`.

## ADB Setup
```bash
adb devices
adb exec-out screencap -p > screen.png
adb shell input tap 540 960
adb shell input swipe 540 800 540 400 300
```
Phone resolution is fixed at **1080×2400 landscape**; templates break
if it changes.

## Key Design Decisions
- **scrcpy is for display only** — all input goes through ADB directly.
- **Port map over building list** — port map is clean and unambiguous.
- **Tiered vision, not raw OCR** — local cache → local models → Claude
  API, in that order.  See `docs/vision_pipeline.md`.
- **Learn once, reuse forever** — every Claude Vision call is cached;
  the bot gets cheaper to run over time.
- **Building types capture cross-port knowledge** — a Cathedral works
  the same in every city.
- **Markets are always port-specific** — prices fluctuate; always
  re-read, always append to history.
- **Phone runs the game, Mac runs the bot.**
- **OCR engine choice** — EasyOCR for the user-language UI; PaddleOCR
  with multilingual model for player-generated text (CJK / Cyrillic /
  Arabic) — see `docs/ui_anatomy.md`.

## Game Knowledge (load on demand)
Per-system detail kept out of this file to keep context light:
- Game basics, nations, growth tracks → `docs/game_mechanics.md`
- Trade system (price signals, trends, mate abilities, market events) → `docs/trade_system.md`
- Combat (modes, Repel Support, ceasefire) → `docs/combat_system.md`
- Exploration, fishing, port investment → `docs/exploration_and_investment.md`
- UI anatomy (port screen, sea HUD, overlay vs modal) → `docs/ui_anatomy.md`

## Milestone Status
One-line snapshot.  Full detail in `docs/milestones_status.md`.

| # | Milestone | Status |
|---|---|---|
| 1 | Core Infrastructure | ✅ DONE |
| 2 | Sea Navigation & Basic Trading | ✅ DONE |
| 2.5 | Data Flywheel | ✅ DONE |
| 3 | FSM & Robust Recovery | ✅ MOSTLY DONE — Planner / Scene Model / typed detectors / flow + plan completeness shipped.  `recovery.py` cleanup (task #25) still open. |
| 4 | World Map Intelligence | ✅ PARTIALLY DONE — pan / zoom + village support + lat-lon localisation shipped.  City-info reading + multi-hop routing open. |
| 5 | Route Optimisation | Not started |
| 6 | Port Knowledge Base & Discovery | ✅ PARTIALLY DONE — `explore_port`, KB expansion via Claude consult, first-visit overlay handling shipped. |
| 7 | Combat & Sea Exploration | ✅ PARTIALLY DONE — mini-map detector (F1 0.74) + shoreline classifier (F1 0.65) trained and wired into `BotObservation.minimap` / `.shoreline` on sea ticks.  `MinimapNavigationView` (classical image-processing implementation of the NavigationView Protocol) shipped: 8-sector land readings + ship heading + bow-clearance.  `HugShoreGoal` is the first sea-control consumer — calibrated press-and-hold steering (~120°/sec), closed-loop mini-map polling at sub-1s cadence, bow `land_fraction` channel direction.  **MILESTONE 2026-06-02 — first end-to-end autonomous destination reach**: Cairo (30.2, 30.4) → (5.71, 32.12) in 506 ticks, phase=COMPLETE.  Bug2-style commitment via §13.21 destination-anchored tangent + §13.25 clearance term + forward-arc-only avoider (§13.26 removed) + §13.27 stuck-detector/UTurnRecovery as safety net (didn't fire).  See `memory/project_first_autonomous_destination_reached.md`.  **Earlier milestone 2026-05-28: autonomously hugged into Nile river origin** (`memory/project_nile_river_reached.md`).  See `docs/navigation_models_status.md`, `brain/goals/hug_shore.py`, `brain/goals/uturn_recovery.py`.  Outstanding: combat-mode steering, voyage smoothness tuning. |
| 8 | Autonomous Company Growth | Not started |

**Currently in flight:** **MILESTONE 2026-07-31 — single hugging-side
bankline tactical, validated end-to-end.**  Rethought the whole-loop
substrate into ONE hugging-side bankline (`LoopTactical`, `--tactical loop`
— see the "Tactical substrate — CURRENT" pointer above): raw ship-water-CC
loop (full-coverage invariant) + forward-port arc-scan foothold (nearest
land abeam→ahead) + DT-offset nav line to the frame-edge exit/dead-end.
Direction from the ship heading; dead-end/lake "falls out free".
**Live voyage (session `loop_consolidated_2026-07-31`):** Cairo (29.95) →
equator → **lake terminal (-1.27)** → clean U-turn → back to Cairo → on N
into the Med.  Round trip **~734 ticks, 4 commit-flips, ZERO thrash** —
beats the prior best round trip (861 ticks, 13 bounces, WITH PID; this run
had no PID).  The Y-tip/lake terminal, where every earlier tracker thrashed,
was clean.  **Steering: PID is now the live default** (linear RATE_DPS=120
under-turns; `--no-pid` for the sim) — see
`memory/project_steering_pid_calibration_2026-07-28.md`.
**LESSON: validate DIRECTION, not just flips** — an interim version scored 0
flips while going ~180° backward in channels.
See `memory/project_loop_bankline_roundtrip_2026-07-31.md`.
**Tests:** `tests/tactical_scenarios/` are live-run-style perception
replays (real `AiNavPipeline` + `FileVisionSource`, ensemble heading,
dead-reckoned position) — still wired to `LookaheadTactical`; the loop
substrate is validated via `tools/viz_fullcov.py` + `data/loop_scanfoot_report.html`.

Prior milestone (v0.8.0-cnn-heading, 2026-06-30): heading CNN replaced
template-match + self-healing stack (ship-only training, 220K-param
convnet).  See `memory/project_cnn_heading_milestone_2026-06-30.md` and
the `v0.8.0-cnn-heading` git tag.

Next push candidates: Y-tip strategic memory (Trémaux) for the residual
strategic-layer thrash; a resupplied voyage to confirm the full
in-and-out dead-end traverse live; combat-mode steering on the same
substrate.
