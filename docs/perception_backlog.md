# Perception backlog (2026-08)

Concrete perception work items, from the architecture review + refactor plan +
live-run findings. Tagged for the **two-agent split** (trading vs navigation):

- **A/B/C = shared foundation** — both tracks depend on these. Land first, or
  give one owner; the other track builds on top. Avoid two agents editing the
  perception layer at once.
- **D = trading track · E = nav track.**

Priority: **P0** unblock now · **P1** foundation · **P2** cleanup/medium-term.

See `docs/architecture_review_perceive_flows_2026-08.md` (findings) and
`docs/refactor_plan_perceive_flow_fsm.md` (design).

---

## A. Shared perception foundation
- **A0 [✅ DONE 2026-08-05, commit 816fbb9] Perception is now a pure read.**
  Fix: `save_inventory` refuses a **title-less scene** (empty `screen_title`
  collapsed the path to `slug(scene_type)` → `scenes/unknown.json`, the shared
  file many frames overwrote then read back); and a scene-cache **hit** no longer
  re-persists to bump visit telemetry. **Verified:** the benchmark with
  cache-writes ENABLED is now identical across 3 runs (village stable 2/11, port
  31/31) — previously drifted village 3→2→1. Original P0 note below.
- **A1 [✅ DONE 2026-08-05, commit 816fbb9] Strong-vs-weak arbitration.** A
  village fuzzy-match overrides `port_overworld` only when it's STRONG
  (`_VILLAGE_OVERRIDE_FLOOR=0.80`) AND beats the port reading of the same OCR
  text (`perceive.py` ~2419). **Verified:** `Seville` (village@0.70 vs port@1.00)
  stays port; `Berber Village`/`Berber Villag`@0.96 still resolve to village;
  benchmark port_overworld 31/31 = 100%. Original note below.
- **A0 [P0, PROVEN] Perception must be a pure read — it isn't.** `_classify_nav_state`
  **writes** `memory/knowledge/scenes/unknown.json` during classify (via
  `claude_vision.save_inventory`, `claude_vision.py:141/631/738`), which
  **self-poisons subsequent runs**: on identical frames + unchanged labels, the
  benchmark drifted **village 3/11 → 2/11 → 1/11** across three runs; disabling
  the write made it **stable at 2/11**. So the classifier can't reproduce its own
  answer, which corrupts any regression measurement *and* explains flaky live
  behavior. Fix: classify must be **side-effect-free** — caches may be *read* as
  priors but a classify call must never write. (Proves review finding #10 +
  overlaps A5.) The benchmark disables this write by default; the code must too.
- **A1 [P0, small] Arbitrate strong-vs-weak signals.** Stop a 0.6 fuzzy
  village-match from overriding a confident port CNN (the Seville→"village" bug
  that stalls the grow loop). `perceive.py:2411`.
- **A2 [P1, big] Structured `PerceivedState` (structure vs identity).**
  → **Concrete phased implementation plan: `docs/a2_perceived_state_implementation_plan.md`**
  (additive via `legacy_state()`; Phase 1 = overlay axis first for the village-recall win).
  Replace
  the 5-way whole-image scheme (which conflates *structure* with *identity*) and
  the priority cascade (`perceive.py:2331`) with a **structured tuple**, each
  field read by its BEST signal, arbitrated on a common confidence scale:
  - `base` = **coarse structure**: `overworld | world_map | loading | panel`.
    (Fold sea+port into ONE `overworld` — they share the chrome: top-left
    icon+name and the right minimap panel. Not `village` — see A2b.)
  - `overlay` = orthogonal (dialog / main_menu / confirm / …).
  - **The top-left region is the UNIVERSAL identity slot** — one slot detector +
    OCR reads "who/where am I" on every screen: on `overworld` it's the icon
    (ship/lighthouse) + name (sea-region/city); on `panel` it's the title
    (Inn/Bank/Village X). Strong argument for the layout detector (C9): this one
    slot pays off everywhere.
  - `overworld` sub-attribute `mode: sea|port` has **two signals** — the
    whole-image CNN (open water vs town, already reliable) AND the top-left icon
    (ship/lighthouse) — arbitrated (agree→confident, disagree→flag).
  - For `panel` screens, parse the **fixed chromed template** into:
    `context` (top-left title — *which* screen: Village/Inn/Bank…, via OCR +
    nav-state memory), `menu` (left list + active item — via the left-menu slot
    detector + OCR), `center`/`right` panel flags.
  - **Why:** identical layouts across contexts (village-recruit ≈ inn-recruit ≈
    harbor-recruit) make whole-image *identity* impossible for a CNN; but the
    **title + active menu item disambiguate cleanly via text** from the same
    template. Building blocks exist (region detectors: left_menu / panels /
    action_buttons; the sub_menu parent tracker for context).
- **A2b [P1] Fix the category scheme + tiered labels.** `village` and `main_menu`
  are miscategorized today: **`main_menu` is an overlay** (over sea/port, not a
  base); **a village is a `panel` screen** (menu list + title + unique items),
  not an outdoor peer of `port_overworld`. And **`sub_menu` is a catch-all** of
  ALL chromed screens lumped together — unlearnable. Re-label chromed frames as
  the **tuple** (`base=panel, context=<title>, menu_item=<active>, right_panel?`)
  instead of flat classes; extend `supervisor.labeler` to capture context +
  active-menu-item. This makes each tier a clean, trainable signal.
- **A2c [P1] Navigation-state memory for `context`.** A panel's *which-building*
  identity often can't be read from pixels alone (occlusion, shared layout) — it
  comes from **what the bot entered** (a state-machine fact). Carry an entered-
  location memory as the prior for `context`, cross-checked with the title OCR.
- **A3 [P1] First-class `overlay` axis** — structural `detect_dialog` every
  tick, ungated (drop the obstruction pre-check); keep last-confident base when
  occluded; unify keyword-interruptor + structural DialogModel into one detector.
- **A4 [P1] First-class `market` base state** (today it's just `building`).
- **A5 [P1] Kill stale-cache-as-classification** — the 10-min Moondream verdict
  returned *as* the live read on chrome-less frames (`perceive.py:2614`); caches
  seed priors only, never replace the frame.
- **A6 [P2] De-duplicate** the two copies of village/zone logic in `perceive.py`.

## B. Shared perf / infra
- **B7 [P1] OmniParser ~8 s/call (likely CPU)** — force MPS / speed up
  (potential 10–20×); currently the structural-detection bottleneck.
- **B8 [P2] Text-LLM tier — re-scope, don't revive-as-was.** `qwen_perceive`
  (mlx-lm, currently uninstalled) failed because it was fed the *whole screen's*
  noisy OCR. Re-scope it to **bounded, cleanly-extractable text decisions**
  (dialogs, market-info panels, crew stat panels): detect the box, OCR just that
  region, feed clean text + game context to a text LLM. Whole-scene / noisy
  understanding stays with a **pixel-aware VLM**. (Rule: bounded-clean-text →
  text LLM; whole-scene/noisy → VLM.) So: keep off for now; reintroduce scoped
  to dialogs once A3 lands and perception feeds it clean regions.

## C. Learned perception — shared, medium-term (durable direction)
- **C9 Learned UI-slot layout detector** (game-specific YOLO) — replaces
  hardcoded crops + slow OmniParser and fixes the port-name/icon-drawn-on-water
  misread. Needs a slot taxonomy + label bootstrap. (See the action item in the
  refactor plan.)
- **C10 VLM-first understanding + distillation** for info-rich screens — expensive
  VLM as offline teacher → fast local student.

## D. Trading track
- **D11 [P1] Market-page reader is broken** — parses 1–2 garbage tiles vs the
  9-tile grid → buy/sell fail ("no goods available", empty-basket sell). Fix via
  a **detected tile grid** (or VLM read). Trading's #1 blocker.
- **D12 Dialog resolution via detected affordances** — expected-vs-unexpected
  branch (act on confirm/result dialogs; dismiss interrupts). For choice dialogs
  (hiring/events) use the bounded-clean-text → text-LLM path (B8).

## E. Nav track
- **E13 Hybrid nav** — keep mini-map primary; **prototype a 3D shoreline U-Net**
  for close hugging and *measure* reliability before wiring (not a rewrite). See
  `docs/minimap_vs_3d_navigation.md`.
- **E14 [P2] Interim mini-map port-icon-on-water filter** until C9 lands.

## Cross-cutting: data / labeling (shared, ongoing)
More labeled sea / market / dialog frames via `supervisor.labeler` feed C9–D11;
`data/labels.jsonl` is the shared asset both tracks draw from (nav pulls the
sea/minimap subset, trading pulls the port/market/dialog subset).

- **[P1] Fix `village` scarcity — part label-quality, part capture.**  Village
  was the rarest class (2 frames), but many village frames were **mislabeled as
  `sub_menu`**; re-labeling has been moving them over (2 → ~11 and rising, with
  `sub_menu` dropping correspondingly).  So it's *partly a label-quality
  problem*, not only a capture gap.  Class distribution in `data/labels.jsonl`
  (~933 frames, last-wins): **sea 46%** dominant, `village` still low-single-%,
  `negotiation`/`task_progress` = 1 each.  Village scarcity is a **root cause** of
  the Seville→"village" confusion — too few examples for a *visual* village
  signal, so classification falls back on the brittle name fuzzy-match (A1).
  Actions: **(a)** audit `sub_menu` (and other outdoor classes) for more hidden
  villages; **(b)** capture more *diverse* village frames (different villages;
  approaching / inside / nameplate / barter-over-water states); **(c)** retrain
  with **balanced sampling / class weights** so village isn't drowned by sea.
  **Superseded framing (see A2b):** "more village CNN data" won't fix the hard
  case — a village *panel* is visually ≈ an inn/harbor panel, so a whole-image
  CNN can't do village *identity*. The durable fix is the **structured
  decomposition**: `village` becomes `base=panel, context=Village` read from the
  title/menu, not a visual class. Clean village frames still help the *coarse
  outdoor-approach* case; the panel case needs A2/A2b/A2c + tiered relabeling of
  the `sub_menu` catch-all. Top up `negotiation`/`task_progress` if they matter.

  **Benchmark evidence (Step 1, `tools/perception_benchmark.py`):** village recall
  is **27% (3/11)**; the dominant error is villages classified as `port_overworld`
  (7/11), NOT the false-village direction I first chased (ports = 100%). And the
  8 misses are **village-WITH-overlay** frames (menu/dialog panels): flat
  single-label can't represent "village + menu," so the label ("village") and the
  prediction ("sub_menu"/"port") are *both partially right*. → the real fix is
  the **base+overlay redesign (A2/A3)** + cleaner labels (`base=village,
  overlay=menu`), not just more village data. (This is *why* the benchmark comes
  first — it caught that "more data" alone wouldn't fix a representation problem.)

---

## Done this session (not on the list)
- Orientation-lock guard (`actions/orientation.py`) — canonical `ROTATION_270`.
- Structural Load-All/Sell + reliable cargo gate in `sell_all_cargo`.

## Suggested order
~~A0 (make classify a pure read — gates *any* trustworthy measurement) + A1
(unblock grow)~~ **✅ done 2026-08-05 (816fbb9)** → **next: A2–A3** (PerceivedState
+ overlay, the shared base — the tiered labeler vocab built 2026-08-04 is the
ground truth for the context/menu-item reader) → then D11 (trading) and E13
(nav) parallelize on top. B7 anytime (cheap perf win).
