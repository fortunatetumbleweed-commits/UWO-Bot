# Architecture Review — Perception, Flows, FSM, Recovery (2026-08-02)

> **Status (confirmed 2026-08-23): UNIMPLEMENTED, still accurate.** The verdict below
> still describes the live system. See `docs/one_loop_task_drives_state.md` for the
> 2026-08-22 re-occurrence and the task-loop design that makes the fix executable.

**Scope.** The perception layer, the FSM / flow / state-machine layer, the
recovery / escalation layer, and the action layer's perceive-before-act and
dialog discipline.  Triggered by the `self_grow` ("grow") task failing at
startup: the bot sat at the **Seville port overworld** but perception
mislabelled it a **village** ("Svear Village"), recovery looped 9× on blind
Back/Home, then crashed headless.

**Method.** Four independent code audits over `uwo_bot` at branch
`trading_revisit`, each returning file:line evidence.  All paths below are
relative to the repo root.

---

## Verdict

The system is *intended* to be perceive → FSM → act, but in practice it is
**three competing control structures** sitting on top of a **perception stack
that combines signals by priority ORDER instead of by CONFIDENCE**, with a
**scripted, blind market layer** underneath.  The individual building blocks
(family CNN, learned fingerprints, typed `DialogModel`, the verified
`plan_loop`) are sound.  What is missing is (1) a single perception
*arbitration* stage, (2) a first-class notion of *overlay/popup* and *market*
state, and (3) one *verified* action loop that every transaction goes through.

---

## As-is architecture

### Control structures (three, one dead)
- `brain/fsm.py` (`BotFSM`, 6 states) + `brain/states/*` — the **documented**
  FSM.  Non-functional stub: state modules return `None` /
  `NotImplementedError`; `fsm.py:10` imports `in_building` which does not exist
  → would **crash on import**.  Only caller is `main.py` — a **decoy entry
  point**.
- `run.py → brain/agent.py` (perceive→reason→act loop) backed by
  `brain/fsm_registry.py` (the *actual* "authoritative state graph") — the
  **live** runtime.
- `actions/market_actions.py` — hand-scripted `tap(); time.sleep(1.5)`
  sequences invoked directly, bypassing the verified loop.

### Perception cascade (6+ classifiers, fixed priority order)
`perceive()` → `_detect_navigation_state()` → `_classify_nav_state()`
(`brain/perceive.py:2331`).  Order: family CNN (≥0.7 gate) → OmniParser
registry fingerprints → chrome templates → **cached Moondream verdict** → OCR
text branches → Qwen enrichment.  Each stage can short-circuit the next; the
verdicts are **never compared on a common scale**.

---

## Findings by subsystem

### A. Perception (`brain/perceive.py`, `vision/*`)
1. **[CRITICAL] Strong verdict overridden by weak signal — `perceive.py:2411`.**
   A `port_overworld @ 1.00` CNN verdict is discarded whenever
   `correct_village_name(raw_port)` returns *any* match at ratio ≥ `_CUTOFF=0.6`
   (`vision/text_correction.py:282`).  This is the live Seville→"Svear Village"
   bug.  No confidence comparison; a 1.0 CNN loses to a 0.60 string match.
2. **[CRITICAL] Stale 10-min cache returned AS live classification —
   `perceive.py:2614` + `brain/moondream_family_cache.py:49`.**  When chrome is
   all-False (e.g. open sea), classification returns a module-global cached
   Moondream verdict (10-min TTL) *without examining the current frame*.  Depart
   port without an invalidation event → sea frames read `port_overworld` for
   minutes.  Largest hidden-state hazard.
3. **[HIGH] Two classifiers with different confidence scales fight by order —
   family (≥0.7) at `2373` vs OmniParser registry (HIGH/MEDIUM) at `2502`.** The
   *more robust* fingerprint path (per the code's own comment) is unreachable
   whenever the weaker family CNN is confident-but-wrong.
4. **[HIGH] No first-class `market` state; no first-class `popup/overlay`
   state — `vision/state_fingerprints_data.py:151`.**  Registry states:
   `main_menu, world_map, port_map, village, building, sub_menu, port_overworld,
   sea, port_loading, loading`.  Market is just `building`.  There is **no state
   meaning "clean screen WITH a dialog on top"**: a market and a market+confirm
   dialog yield the **same `state`**; popup-presence lives only in a separate
   `interruptors` list the classifier never sees.
5. **[HIGH] Popup detection gated behind an "obstruction" pre-check that can
   suppress real dialogs — `perceive.py:447`.**  The interruptor keyword loop is
   skipped when no "obstruction" is detected; the structural `DialogModel` is
   used only to *dismiss*, never to classify.  Structural and keyword detectors
   are not unified.
6. **[MEDIUM] Uncalibrated, inconsistent magic thresholds.**  `family≥0.7`,
   skip-Qwen `≥0.95`, `_CUTOFF=0.6` (fuzzy port AND village), `fuzzy_contains`
   `0.82`, port window `0.5<r<0.95`, `menu_overlap≥2`, `omni_elements≤2/<5` —
   the same "is this a match?" question has ≥4 different numeric answers, no
   shared source of truth.
7. **[MEDIUM] `id(frame)` result cache can return a stale verdict —
   `vision/family_classifier.py:110`.**  Per-frame LRU keyed on `id(frame)`;
   CPython recycles ids → a freed frame's id reused by a new frame returns the
   previous verdict.  Rare but non-deterministic.
8. **[MEDIUM] Symmetric decisions use asymmetric evidence bars —
   `perceive.py:2707` (sea-HUD override needs ONE fuzzy@0.82 token) vs `2780`
   (menu-token needs ≥2).**  A single short token fuzzy-matching building text
   can flip a port to sea.
9. **[MEDIUM] Duplicated village/zone logic in two branches that can diverge —
   `perceive.py:2411` (family branch, no back-arrow guard) vs `2668` (text
   branch, requires back-arrow).**  Violates "one canonical implementation".
10. **[LOW] Perception is not a pure read — `perceive.py:2972`.**  A single
    `perceive()` can sleep ~15s polling and *tap/dismiss* as part of
    classification, coupling perceive to act and breaking replay determinism.

**Assessment.** Strong and weak signals are combined by ordering, not
arbitration; a 10-min cache can stand in for the live frame; overlay-presence
and "market" are second-class.  The fix is a single arbitration stage scoring
all signals on one scale, with overlay as an orthogonal first-class dimension.

### B. FSM / flows (`brain/fsm*.py`, `brain/states/*`, flows)
1. **Two competing FSMs; the documented one is a dead stub** (`brain/fsm.py`,
   `brain/states/*`; crashes on import).  Live system uses
   `brain/fsm_registry.py` via `brain/agent.py`.
2. **Two entry points; `main.py` is a decoy** — real loop is `agent.step()`
   (`brain/agent.py:1158`), launched from `run.py:448`.
3. **Transaction primitives execute BLIND fixed sequences** — e.g.
   `market_actions.py:906` `tap(sell); sleep(1.5)` with no re-classify; 27 bare
   `time.sleep()` vs 14 `_wait_for_screen` checkpoints in `market_actions.py`.
4. **Partial credit:** primitives *do* verify at *checkpoints*
   (`_wait_for_screen("nego"/"confirm")`) and prefer `_find_button` structural
   detection — so it's a *checkpoint-verified script*, not fully blind; the
   inter-checkpoint taps are unverified.
5. **The high-level agent loop IS perceive-driven** (`agent.step` re-perceives
   each tick, `_wait_for_outcome` polls until scene changes) — the good model
   that the primitives bypass.
6. **`plan_loop.achieve_goal` is the one fully-correct perceive→act→verify→
   replan loop — but only on the Claude escalation path** (`brain/plan_loop.py`),
   not routine transactions.
7. **Flow-completeness enforced at LEARN time, not EXECUTION time**
   (`brain/flow_completeness.py` consumed by `brain/claude_guidance.py` on
   persist; a perceive/recovery *skip* guard exists but nothing validates a
   *run* produced a transaction).
8. **Shipped `flow.json` files end on a "Back arrow" success step**
   (`harbour_depart` step 12, `market_buy` step 8) — the Back=Cancel
   anti-pattern the completeness doc forbids.
9. **`analyze_flow.py` is authoring-only** — runtime uses flows as a
   *coordinate lookup table*, never steps through `leads_to`/`is_optional`; that
   rich structure is inert at runtime.
10. **No runtime step-verifier** ("am I on the step I think I am?" before
    firing) for market flows; `sail_actions` verifies blocking-signals (best
    behaved), market flows have no equivalent.

**Assessment.** Not one FSM and not uniformly perceive-driven: a dead skeleton +
a live agent loop + scripted primitives.  The top of the stack verifies; the
transaction bottom assumes-and-hopes.

### C. Recovery / escalation (`brain/recovery.py`, `brain/human_escalation.py`)
1. **Blind Back/Home on any state with no FSM path, no village/port guard —
   `recovery.py:1024`.**  `village` isn't an FSM state → this fires every
   attempt (`press_back()` + `tap(2300,45)`).  Per
   `memory/... project_village_back_press_unsafe`, System Back on village/port
   *leaves the location*.  Direct cause of the "9× No FSM path from 'village'".
2. **Hard-stall escape re-loops instead of escalating — `recovery.py:967`.**
   Same signature ≥6 iters → more blind Back/Home, resets `stall_count`; cycles
   until the 300s/20-attempt cap.
3. **Claude-reclassify escape resets even when state didn't change —
   `recovery.py:957`** → infinite-until-timeout spin on a stuck misclassification.
4. **`TeachingAbortedError` uncaught in headless entry — `run.py:781`
   (`_single_shot`)** and **`actions/self_grow.py:227` catches only
   `RecoveryError`** → the grow task crashes uncaught instead of degrading.
5. **No autonomous fallback when headless — `human_escalation.py:1068`.**  Human
   teaching is the only terminal rung; headless it's a dead end (no
   force-restart / safe-abort / skip-round).
6. **Post-escalation retry recurses with a fresh 120s budget —
   `recovery.py:1048`** → compounds wasted wall-clock (the ~15 min observed =
   300s + escalate + 120s + 600s teaching timeout).
7. **Recovery trusts the state label** — a never-changing high-confidence
   non-FSM state should be treated as *suspect*, not authoritative.

**Assessment.** Sound structure (bounded BFS + graduated ladder) but brittle
against persistent misclassification, unsafe blind Back/Home, and a
half-built headless story that crashes instead of degrading.

### D. Action layer (`actions/*.py`)
1. **Market entry points tap fixed coords with NO location gate —
   `market_actions.py:906/1632/1862`** fire `tap(*MARKET_COORDS[...])` first;
   **no `_assert_at_market` exists**.  Violates "transactional primitives gate
   on location."  (Sea + explore primitives *do* gate — `sea_actions.py:148`,
   `explore_actions.py:761`.)
2. **Dialog handling duplicated + embedded in transaction functions** — market
   has its own `_dialog_ok_pos`/`_resolve_dialog_ok` **and** inline "press Back
   on whatever dialog I find" branches (`market_actions.py:1584, 771, 1194,
   1729`), plus a hardcoded `tap(2300,45)` home-bail.  Canonical
   `dismiss_interruptors`/`screen_exit.exit_current_screen` exists and
   `sea_actions` uses it — market reinvents it.
3. **Hardcoded unguarded taps** — `MARKET_COORDS`, `_tap_home(2300,45)`,
   `_DIGIT_FALLBACK` keypad positions.  (Sea rudder/arrow coords are hardcoded
   but *are* guarded by `_assert_on_sea`.)
4. **Blind `tap; sleep` transitions** — tab switches (`sleep(1.5)`), explore
   item taps (`sleep(2.5)`), building settle (`sleep(2.0)`) fire without
   verifying the transition.

**Assessment.** Bifurcated: sea/explore are disciplined (gate + structural +
canonical dialog layer); `market_actions.py` is the consistent offender (no
location gate, duplicated dialog logic, blind tab taps).

---

## Cross-cutting root causes
1. **Composition by ordering, not confidence arbitration** (perception).
2. **Overlay/popup and "market" are not first-class** perceived state.
3. **Two-plus control structures**, one dead + a decoy entry point.
4. **The verified loop exists (`plan_loop`) but routine transactions bypass it.**
5. **Completeness / safety rules are advisory, enforced at learn-time or
   scattered, not at execution-time by one gate.**
6. **Recovery trusts labels and blindly presses Back/Home**, unsafe + spins.

See `docs/refactor_plan_perceive_flow_fsm.md` for the proposed target
architecture and phased plan.
