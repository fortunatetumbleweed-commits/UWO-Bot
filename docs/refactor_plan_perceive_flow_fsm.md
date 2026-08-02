# Refactor Plan — Perceive / Flow / FSM (draft, 2026-08-02)

Companion to `docs/architecture_review_perceive_flows_2026-08.md`.  This is a
**draft** direction for discussion, not a committed plan; open decisions are at
the end.

## Goals
1. **One perceived truth per tick** — a single, arbitrated `PerceivedState` with
   overlay/popup as a first-class, orthogonal dimension.
2. **Perceive-before-AND-after every action** — no blind `tap; sleep`; every
   action verifies it reached the expected state, or reports structured failure
   up (never inner-loop-retries forever).
3. **One canonical home per concern** — dialog detection/dismissal, location
   assertion, button-finding, state classification each have exactly one
   implementation.
4. **Deterministic mechanics, learned strategy** — the dialog/flow correctness
   the user described is enforced by *verification gates*, not RL; learning is
   reserved for the *strategy* layer (destination / negotiation / route).
5. **Degrade, don't crash** — headless recovery has a safe autonomous fallback.

## Design principles
- **Understand, don't pattern-match — on the screens that matter.**  For
  information-rich, low-frequency screens (port overworld, market, inn,
  dialogs) use a **VLM over the whole frame** to *understand* the scene, not
  crop-a-region-OCR-fuzzy-match.  The current `read_port_name` (crop → OCR →
  fuzzy-match @0.6) misreads exactly when an NPC speech bubble or a neighbouring
  building label falls in the crop — the Seville→"Svear Village" bug.  A VLM
  asked "what is the port name in the top-left banner? ignore floating speech
  bubbles and building labels" doesn't have that failure mode.
- **Tier by frequency × information.**  VLM calls are slow (~1–5s) and cost
  money, so they can't run every tick — and don't need to.  **High-frequency,
  low-info** (sea navigation, hundreds of ticks) stays on cheap local CNNs.
  **Low-frequency, high-info** (ports/markets/inns/dialogs, dozens of ticks)
  is VLM-**first**, not VLM-as-fallback.  Elevate the existing
  `qwen_perception` / `claude_vision` / Moondream from narrow fallbacks to the
  primary layer for rich screens.
- **LLM-as-policy for info-rich decisions.**  Buy/sell, crew hiring, dialog
  choices are decisions a model can *reason* about from a goal/rule prompt +
  a clean structured read ("buy the most profitable good within budget, prefer
  demand at destination").  This is the "more intelligent gameplay" tier — it
  is NOT RL; RL stays a later topic for pure sequential strategy (route /
  destination / timing) where there is no promptable optimum.
- **Arbitrate, don't order.** Signals produce evidence scored on ONE scale; a
  fusion stage picks the winner.  A 1.0 CNN is never silently beaten by a 0.6
  fuzzy string.  On rich screens the VLM is the strong evidence / arbiter; on
  sea the local CNN is.
- **Overlay is orthogonal to base state.** "market" and "market + confirm
  dialog" are different perceived states; the classifier must be able to say so.
- **Perception is a pure read.** No tapping/sleeping inside `perceive()`; acting
  belongs to the loop, not the classifier.
- **Actions are contracts.** Each declares preconditions and expected post-state;
  the loop enforces them.  Contract violations are the deterministic
  "reward/punish" signal (logged for future learning).
- **Robust + affordable VLM use:** structured JSON-schema output (grounded, not
  prose) · verification via the perceive→act→verify loop (a bad read is caught,
  not trusted) · learn-once caching for static understanding (building types, UI
  layouts), re-read only dynamic data (prices, crew).

---

## Target architecture

### 1. Unified `PerceivedState`
```python
@dataclass
class PerceivedState:
    base:     BaseState      # sea | port_overworld | village | world_map |
                             # market | building | main_menu | loading | unknown
    overlay:  Overlay        # none | dialog | confirm | negotiation | result |
                             # news | reward | error   (ORTHOGONAL axis)
    identity: str | None     # port/village/building name, if known
    confidence: float        # arbitrated, single scale
    evidence: list[Signal]   # what each detector said + its score
```
- Produced by a **tiered fusion/arbitration** function.  Cheap local detectors
  (family CNN, OmniParser fingerprints, chrome, `DialogModel`) run first to get
  base + overlay + a "is this a rich screen?" flag.  On **rich screens**
  (port / market / inn / dialog) a **VLM** produces the authoritative structured
  read (identity, overlay, goods+prices, options) and is the arbiter; on **sea**
  the local CNN is.  All evidence is scored on one scale — no priority cascade
  as in `perceive.py:2331`, and no weak fuzzy-match beating a strong verdict.
- `market` becomes first-class (fingerprint or Qwen-confirmed), not "building".
- `overlay` is computed from the structural `DialogModel` FIRST (unify the
  keyword + structural detectors), so popup-presence is always known.
- **No hidden 10-min cache stands in for the live frame.**  Caches may *seed*
  priors but never *replace* the current-frame read; a cache that contradicts
  the frame loses.

### 2. One verified action loop (the FSM)
Consolidate on the `agent.step` / `plan_loop.achieve_goal` model; delete the
dead `BotFSM`/`brain/states/*` stub and the `main.py` decoy.  Every action runs:
```
perceive()                                  # PerceivedState
if state.overlay != none:                   # a blocker is up
    resolve_overlay(state)                  # ONE canonical handler, typed action
    continue
assert state.base == action.precondition    # location gate (extends _assert_*)
act()
post = perceive()                           # re-perceive
verify(post == action.expected_post)        # transition check
  → on mismatch: structured failure UP to the policy (bounded retry / replan)
```
- Routine transactions (buy/sell/depart) go through this loop instead of
  scripted `tap; sleep`.
- The location gate generalizes `_assert_on_sea` / `_assert_at_port` and adds
  `_assert_at_market`.

### 3. Action contract (= deterministic reward/punish)
Every action declares `preconditions` (base + overlay) and `expected_post`.
The loop turns the user's desired reward rules into **hard gates** now, and
**logs the same signals** as a dataset for later learning:

| Signal | Condition | Deployed (gate) | Logged (future RL) |
|---|---|---|---|
| − phantom dismiss | tapped close/Back while `overlay==none` | **refuse** the tap | −1 |
| − missed blocker | acted while `overlay` unresolved | **block**, resolve first | −1 |
| − cancel-not-complete | reached a recognized state with **no positive transaction** (Back/Home used as "success") | mark flow **incomplete at runtime** | −1 |
| − wrong-state tap | `base ≠ precondition` | **refuse** (assert gate) | −1 |
| − stuck | same `PerceivedState` N ticks after an action | escalate (don't repeat) | −1 |
| + verified progress | `post == expected_post` AND transaction observed | proceed | +1 |

Each tick logs `(PerceivedState, action, expected_post, actual_post, verified)`
→ this **is** the imitation/RL dataset, obtained for free once the loop exists.

### 4. Three decision tiers (mechanics / understanding / strategy)
- **Mechanics (dialog dismissal, flow steps, navigate-to-screen):
  deterministic.**  The failure modes named (phantom dismiss, missed blocker,
  cancel-not-complete) are *verifiable perception facts* — solved by the action
  contract, not learning.
- **Info-rich understanding + decisions (market buy/sell, crew hiring, dialog
  choices): VLM + goal/rule prompt.**  A VLM reads the screen into a structured
  scene and an LLM (or rules) chooses the action from a mission prompt.  This is
  the "intelligent gameplay" tier — grounded reads + reasoning, verified by the
  loop.  Not RL.
- **Pure strategy (which port, negotiate once/all/skip, which route): learned
  (later, optional).**  A sequential-decision problem with a real reward
  (**ducats/hour**) and no promptable optimum → contextual bandit / lightweight
  RL.  `self_grow`'s `_pick_destination` scoring stub is the natural first host.
  RL is infeasible for *mechanics* anyway (real phone ≈ seconds/action,
  anti-cheat tap limits, non-reproducible episodes, 700+-tick voyages).

---

## Phased plan

### Phase 0 — stop the bleeding (small, unblocks grow)
- Fix the Seville→village override at the root of the current bug: in the
  fusion (or, interim, at `perceive.py:2411`) don't let a village fuzzy-match
  beat a better/equal port-name match or a high-confidence port CNN.
- Recovery: exclude `{village, port_overworld}` from blind Back/Home; treat a
  never-changing high-confidence non-FSM state as suspect → escalate early.
- Headless: catch `TeachingAbortedError` in `self_grow` / `_single_shot`; safe
  autonomous fallback (abort round / return to game main menu) instead of crash.
- *Verify:* re-run the grow task past startup at Seville.

### Phase 1 — unified perception
- Introduce `PerceivedState` (base + overlay + confidence + identity) and a
  fusion stage that arbitrates evidence on one scale.
- Make `overlay` first-class from the structural `DialogModel`; unify the
  keyword + structural interruptor detectors.
- Make `market` a first-class base state.
- Remove the stale-cache-as-classification path; caches seed priors only.
- *Verify:* replay-test on recorded frames — market vs market+dialog vs
  port vs village vs sea all label correctly incl. overlay; the Seville frame
  labels `port_overworld / none`.

### Phase 2 — one verified action loop + gates
- Route buy/sell/depart through the perceive→act→verify loop; delete blind
  `tap; sleep` in `market_actions.py`.
- Add `_assert_at_market`; generalize the location gate.
- Consolidate all dialog dismissal onto `dismiss_interruptors` /
  `exit_current_screen`; remove market-local `_dialog_ok_pos` and inline
  "dismiss whatever I find" branches.
- Kill the dead `BotFSM` / `brain/states/*` / `main.py` decoy (or reduce
  `main.py` to a thin call into the agent loop).
- *Verify:* buy + sell end-to-end via the loop; scoped tests for market/sea/
  explore; no un-gated `MARKET_COORDS` tap remains.

### Phase 3 — action contract + runtime completeness
- Encode preconditions/expected_post per action; enforce the reward/punish
  table as gates; log the per-tick signal tuple.
- Enforce flow-completeness at **execution** time (positive transaction
  required; Back/Home ≠ success), not just at persist time.
- *Verify:* injected failures (phantom dialog, missing dialog, Back-as-success)
  are caught and refused; the signal log is populated.

### Phase 4 — strategy learning (optional, later)
- Contextual bandit over destination / negotiation / route using ducats/hour and
  the Phase-3 signal log; keep mechanics deterministic.

---

## Action item — Learned structural layout (UI-slot detector)
**Status:** candidate; **prioritise when the gameplay-perception work starts.**
Likely reshapes Phase 1 (the `PerceivedState` gets *produced* by this detector
instead of the heuristic cascade), and largely retires Phase 0 as a stopgap.

**Idea.** The game UI is a stable set of spatial *slots* that only shift
slightly between versions.  Train a **game-specific UI-slot detector** (same
spirit as the heading CNN / ship U-Net — and easier, because chrome is
high-contrast and stable) that outputs, per frame, the labelled slots present +
their boxes + confidence, e.g. for port overworld:
`{ name_banner, national_flag, minimap, world_area, nameplate?, action_button
[Quick Supply | Enter City/Village | Depart]?, dialog? }`.  Note the **sea↔port
symmetry**: the "get-close → nameplate + action plate" element is the *same*
learnable slot in both contexts — train once, reuse.

**Why it's high-leverage.**
- **Retires the absolute-position fragility class** we fought all session
  (notch/orientation, version drift, `MARKET_COORDS`/`MINIMAP_REGION`/
  `OCR_PORT_NAME_REGION` hardcoded crops).  A detector finds the banner/minimap/
  buttons *wherever they are*, so small shifts don't break anything; the
  orientation guard becomes belt-and-suspenders, not load-bearing.
- **Fixes the misread class** (Seville→village): OCR runs only inside the
  *localised* banner box, so NPC speech bubbles / neighbouring labels are no
  longer in the read.
- **Produces most of `PerceivedState` for free, every tick, locally:** slots
  present → base state; dialog slot → overlay; banner box → clean read region;
  button boxes → action targets (tap the *detected* button, not a coord).

**Model choice.** Small fine-tuned **YOLO** for the discrete chrome slots (reuse
the existing ultralytics/OmniParser stack; OmniParser is *generic* "a button",
this head is *semantic* "the Enter-City button").  Keep **U-Net** for pixel
masks / ship-sprite work.  Likely both.

**Fits the three tiers:** detector (localise + presence, cheap/every-tick) →
OCR on localised slots (read text) → VLM (reason about semantics only where
needed).  Detector + VLM are complementary, not competing.

**Main cost = labels**, but bootstrappable: auto-generate first-pass labels from
OmniParser + chrome detector + templates, correct, then train; thousands of
unlabelled frames already exist in `data/sessions/`.  Tolerant of small shifts →
retrain only on big UI redesigns, not every version wobble.

## Migration / compatibility notes
- The recorded `flow.json` files stay as a **coordinate fallback**, but
  structural `_find_button` becomes primary everywhere (already true for buy;
  extend to sell — done on `trading_revisit` — and depart).
- Keep the orientation guard (`actions/orientation.py`) — it is orthogonal and
  already load-bearing.
- Anti-cheat discipline unchanged (jittered sleeps, no burst taps).

## Decisions
1. **RL scope — DECIDED (2026-08-02): parked as a later topic.**  Learning is
   reserved for pure sequential *strategy* (route / destination / timing) only,
   explored later.  The "intelligent gameplay" the bot needs now (buy/sell,
   hiring, dialog choices) is the **VLM + goal-prompt** tier, not RL.
2. **Perception — DECIDED (2026-08-02): VLM-first for rich screens.**  Don't
   just arbitrate the existing weak OCR/fuzzy signals better — for
   information-rich screens *replace* crop-OCR-fuzzy with grounded VLM
   understanding (tiered; cheap local perception keeps high-frequency sea nav).
   Root fix for the NPC-bubble / label-collision misreads.

### Still open (need input)
3. **Sequencing** — land Phase 0 immediately (unblock grow), then Phase 1;
   or design the full `PerceivedState` + VLM read schema first?
4. **`main.py`/`BotFSM`** — delete outright, or keep `main.py` as a thin shim
   into the agent loop for the documented entry point?
5. **VLM choice per tier** — which model for the rich-screen read: Qwen-VL
   (local, already wired), Claude Vision (accurate, paid, already cached), or
   Moondream (cheap, weaker)?  Likely per-screen (cheap for classify, strong
   for market/crew reads) — decide when Phase 1 is scoped.
