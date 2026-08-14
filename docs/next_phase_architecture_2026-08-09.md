# Next-Phase Architecture — Knowledge-Driven, Tiered, Self-Correcting Agent

**Status:** design foundation for the next phase of development (authored
2026-08-09, consolidating the 2026-08-08/09 design discussions). This is the
single source of truth; detailed sub-designs are referenced, not duplicated.

The goal of the next phase: turn the bot from "runs scripted flows + calls Claude
every step and often flails" into a **knowledge-driven agent** that (a) decomposes
profit strategy into tasks, (b) executes each task robustly with **memory of what
worked and what didn't**, (c) gets **cheaper over time** as it learns, and (d)
escalates to a strong model only when genuinely surprised.

---

## 1. Framing — the durable asset is a KNOWLEDGE BASE, not model weights

The game: a sea-trading world — earn **ducats** by trading; there are **ports** and
**villages**; a **barter system** and **seasonal barter** (hugely profitable).
Tasks like recruit / buy / sell / barter / sail are multi-step and pass through
several confirm dialogs.

"Teaching the bot" means **growing a retrievable GAME KNOWLEDGE BASE that any model
consumes** — inspectable, editable, shared — NOT baking knowledge into weights.
Two knowledge sources:
1. **Play-learned** — flows, dialog handling, outcomes (trace log + memory + flow
   docs).
2. **Human-provided** — facts/strategy the bot can't cheaply discover, given as
   TEXT or a WEB URL (e.g. *"seasonal barter X is profitable this season"* — time-
   sensitive alpha).

---

## 2. The THREE knowledge layers (strategy → task → control)

| Layer | What it knows | Nature | LLM need | Code home | KB |
|---|---|---|---|---|---|
| **STRATEGY** | WHAT is worth doing + decompose into an ordered PLAN OF TASKS | DYNAMIC (prices/seasons; human alpha) → judgment | **persists** (the profitable decisions) | mission / planner / route_planner + trade signals | strategy-KB: economics, price signals, routes, seasonal opportunities, barter recipes |
| **TASK** | what each task IS, WHERE it's done, its PROCEDURE + DONE-CONDITION | semi-stable (game structure) | occasional | goals/skills + **skill_registry** | task-KB: task→building, procedure, done-condition |
| **CONTROL** | operate the UI: buttons, dialogs, chromed layouts; infer next action from the screen | FIXED / pattern-based → learn once → DETERMINISTIC | **trends to ~0** | perception + actions/skills | control-KB: UI procedures, dialog policies, layout patterns (e.g. `market_buy_flow.md`) |

**Worked example (the bison barter chain).** Strategy: *"bison trade is profitable
this season"* → plan `[buy horse @ Jidda] → [buy handcannon @ Lisbon] → [barter for
bison @ Sioux village] → [sail → Calcutta] → [sell]`. Each step is a TASK; e.g.
"buy horse at Jidda" → task-KB says *buy → market* + the buy procedure → control
executes the market UI. A single task pulls from **all three** layers; **decision
quality = all three lined up.**

**Consequence for the LLM budget:** CONTROL calls fade toward 0 (learn the flows →
deterministic), TASK is mostly stable, STRATEGY calls persist — and that's fine,
because strategy is the money-making judgment that can't be pre-baked.

---

## 3. The TASK EXECUTOR — robust, self-correcting execution (the keystone)

**The problem it fixes:** today the bot **repeats the same non-working action at
the same screen with no idea it isn't working** — recruit tapped the mode toggle
5× (never the commit), negotiation tapped Purchase 5× (mis-read as a lottery). Two
missing mechanisms, a PAIR:

### 3a. Done-conditions (check success, don't assume it)
Every task AND step needs an **observable predicate over real state**, not "the
procedure ran to the end":
- recruit_crew done = **crew ↑** / "Crew is ready" seen.
- buy done = **cargo ↑ AND ducats ↓**.
- sail_to(X) done = **location == X**.
- a step ("tap OK") *worked* = the dialog is gone / state changed as intended.
Stored in the task-KB next to task→building+procedure.

### 3b. Attempt memory + no-progress → try-different → stuck → escalate
Within a task, remember `(screen signature, action, did-it-make-progress?)`:
- action made **no progress** (state unchanged) → mark `(screen, action)`
  ineffective; the next decision at that screen **excludes the tried-and-failed
  actions** and picks a DIFFERENT affordance.
- **cycling / no untried options left** → **STUCK** → escalate ("stuck on X, tried
  A/B/C, none progressed").
An action may only be repeated if it made progress.

### 3c. The executor loop
```
task = {procedure, done_condition}
memory = {}                       # signature -> {actions that made no progress}
while not done_condition(state) and not stuck:
    tried = memory.get(sig(state), set())
    action = decide(state, avoid=tried)          # deterministic / Qwen / Claude
    prev = state; execute(action); state = perceive()
    if not did_progress(prev, state):            # see §4
        memory[sig(prev)].add(action)
    if cycling(memory) or no_untried_actions(state, memory):
        escalate("stuck", tried_summary)
```

### 3d. Bonus — this IS the learning signal
`"screen X: action A → no-progress, action B → done"` are exactly the labeled
control-KB facts the cache / Qwen reuse. The same mechanism that **stops the
flailing produces the knowledge** that makes the flow deterministic next time.

---

## 4. Progress / change detection — "did my action do anything?"

Full design: **`docs/state_change_detection.md`**. Summary: **NOT** a whole-frame
pixel diff (the UI has a live timer + animations → over-fires; a purchased item
stays grayed-in-panel → under-fires). Compare **structured state**, layered:
1. **World-state delta = ground truth** — cargo ↑ / ducats ↓ / crew ↑ / location.
   (These are also the done-conditions.) Needs the HUD readers (Gap, §6).
2. **Screen/dialog signature change** — `verify._state_signature` / `light_check`
   already do this.
3. **Coarse region-diff** (the structural signal) — DOWNSCALE both frames (low-pass
   filter kills the timer, keeps big regional changes) → per-block diff → mask the
   animated background → **label changed blocks via OmniParser's layout** ("cart
   region changed" = item moved = progress). Filters small value-changes by design
   → those come from (1).
4. **Grayed-out (saturation) + region pHash** — fine signals, last.

---

## 5. Tiered reasoning cascade — cache → Qwen → Claude (learn-once)

Mirrors the project's proven **tiered vision** (local cache → local models → Claude
Vision), applied to the DECISION layer. The reasoning loop's `llm_fn` is already
pluggable, so a tiered resolver drops in:

```
decide(state):
  1. deterministic CACHE  (signature -> action)          # 0 cost
  2. Qwen (local, learned from Claude's past decisions)   # cheap/local
  3. Claude (novel case) — AND log the decision as a lesson (teaches 1 & 2)
```

- **Known MECHANICS** = deterministic skills, **no LLM at all**.
- **Known JUDGMENT** (which dialog? negotiation risk/reward?) = **Qwen**, taught by
  Claude.
- **Novel** = **Claude**, which also enriches the KB the lower tiers reuse.
- **Learn-once distillation:** start with **retrieval / few-shot** (store Claude's
  decisions; retrieve similar past `(screen, decision)` examples to few-shot Qwen —
  no training infra); **fine-tune** later once there's volume.
- **Failure detection → escalate:** the game gives ground truth. Combine *pre-action*
  (whitelist + **red-gem safety gate applies to EVERY tier** + Qwen self-confidence)
  and *post-action* (did the state progress? — §4). A wrong-but-confident local
  action is worse than a Claude call, so invest here.
- Claude usage trends toward "only when the world surprises us."

---

## 6. Current code vs gaps + build order

Full map: `project_design_gap_analysis_2026-08-09` (memory). **Much of the design
already EXISTS but is disconnected** — the gap is mostly wiring + a few bricks.

**Already exists:** reasoning stack (reasoning/reasoning_loop/action_executor/
world_model/skill_registry/llm_client + trace log); escalation hook (market);
**progress+done machinery** `brain/verify.py` (`_state_signature`, `light_check`,
`heavy_check` with per-goal predicate files) — but used by the PLANNER, not the
reasoning/task loop; cycle detection (`claude_guidance`); flow no-repeat step IDs
(`recovery.py`); strong classification + element inventory + learned fingerprints
(perception learn-once); CREW reader (`state_extractor.py`, unwired); route
economics (`route_planner`/`trade_priors`/`self_grow`); Qwen LIVE but PERCEPTION-only.

**Gaps, in build order:**
1. **HUD numeric readers** (foundational): cargo MISSING, ducats detection-only,
   crew exists-unwired → build + wire to the world model each tick. Ground truth
   for done-conditions + progress (§3a, §4.1).
2. **Unify the TASK EXECUTOR** (keystone, mostly wiring): connect `verify.light_check`
   + `_state_signature` + attempt-memory into `resolve()`; pass a `done_fn`;
   escalate on stuck (§3).
3. **Done-conditions + real skills**: attach observable done-conditions per task;
   grow `skill_registry` (data-only today) into a task-KB with run functions +
   procedures + done-conditions (recruit/barter/sail have none).
4. **Decision learn-once cache** (signature → action) — make control cheap (§5).
5. **Tiered Qwen for DECISIONS** (perception-only today) — cache→Qwen→Claude.
6. **Strategy**: wire trade-good SELECTION from market prices (read but unused); an
   explicit ordered task-plan object; goal-layer KB retrieval; **human text/URL
   ingestion** (seasonal-barter alpha).
7. **Coarse region-diff** helper (§4.3) + retrieval-based KB (primer is a flat blob).

**Keystone = Gap 2; prerequisite = Gap 1. Then 3 + 4.** Do 1–4 and "known path =
cheap/deterministic, novel = Claude" + the end of the repeat-action loops fall out.

---

## 7. Reference map

- Change detection: `docs/state_change_detection.md`
- A control-layer procedure (worked example): `docs/market_buy_flow.md`
- Reasoning-fallback layer: `docs/reasoning_fallback_layer_design.md`,
  `docs/bot_architecture_layers.md`, `docs/affordance_learning_and_curiosity.md`
- Game mechanics/strategy: `docs/game_mechanics.md`, `docs/trade_system.md`,
  `docs/exploration_and_investment.md`
- Memory (design + facts): `project_game_knowledge_base_vision`,
  `project_task_executor_done_and_attempt_memory`,
  `project_design_gap_analysis_2026-08-09`, `project_price_negotiation_dialog`,
  `project_cargo_load_ratio`, `project_recruit_min_crew_normal`,
  `project_yellow_commit_button_style`.
