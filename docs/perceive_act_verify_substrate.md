# Perceive → Act → Verify: the Act-Layer Substrate (Design)

Status: **proposed** (2026-08-19). Scopes an overhaul of how the bot takes actions.
Motivating principle from the user: *every action must be the result of a perception, and after
acting the bot must perceive again and compare **expected vs actual** before deciding the next
action — it must never assume a state and blindly take the next action.*

Related memory: `feedback_perceive_act_verify_expected_vs_actual`,
`feedback_stuck_vary_the_action`, `project_task_executor_done_and_attempt_memory`,
`project_design_gap_analysis_2026-08-09`.

---

## 1. Problem

Today many actions are taken on an **assumed** state and are **not verified**. The pattern is
"do X, assume it worked, do Y." When X doesn't do what we assumed, the bot either loops on the same
action or walks off a cliff. Concrete failures we've hit:

| Symptom | Root cause (assumed state / no verify) |
|---|---|
| `navigate_to_building` looped opening the main menu, summoning "Exit Game?" every ~20s | blind `press_back()` on any "unexpected" state; Back is context-dependent (overworld/main_menu → quit prompt) and was never checked for effect |
| Sailing kept "opening the world map" and hit the hamburger | tapped a hardcoded coord assumed to be the minimap; no check that the map actually opened |
| Recruit toggle tapped 5×, negotiation "Purchase" tapped 5× | repeated the same tap without checking it changed anything |
| Buy flow aborted waiting for a dialog that never came | a blocking subloop assumed a post-tap dialog instead of perceiving the actual result |

All the same shape: **no declared expectation, no post-action perception, no expected-vs-actual
comparison.**

## 2. Principle

1. **Perception-gated** — every action is chosen from a *current* perceived state, not an assumed one.
2. **Expectation-declared** — every action names its **expected post-condition** ("what should this
   produce?").
3. **Verified** — after acting, **re-perceive and compare** expected vs actual.
4. **Mismatch ≠ repeat** — on mismatch, do NOT blindly repeat; vary the action or escalate.
5. **No blind destructive action** — never take an irreversible/destructive tap (Back on the
   overworld → "Exit Game?", tapping OK on a quit/real-money dialog) on an assumed state.

### 2a. Perception is error-CORRECTION (anti-drift), not just action-selection

The deeper reason to perceive (user 2026-08-19): **inferred/assumed state DRIFTS.** Every action
taken on an assumption is a small unverified guess; guesses compound, and after enough steps the
bot's model of "where it is" diverges from reality until it's lost — the hamburger loop, the
wrong-screen `buy_to_goal`, the "Athens vs Jakarta" mislabel.

A perception does two jobs: it selects the next action AND it **re-anchors to ground truth,
clearing stale inference** so the accumulated error resets instead of compounding. So the rule is
not just "verify each action" — it's **bound how long you're allowed to act on inference before
re-perceiving.**

This is the SAME principle as the navigation stack: dead-reckoned position drifts, so it can't be
trusted for many ticks — the periodic lat/lon read re-anchors it (`read_latlon`,
`project_first_autonomous_destination_reached`; and the stall-detector fix that requires a *fresh*
position read before discarding a route). The act layer inherits it: don't chain N assumed steps;
perceive to reset the drift, and treat each perception as a chance to CORRECT a wrong assumed state,
not merely to read the expected one.

```
        ┌─────────────────────────────────────────────────────┐
        │ perceive() → state   (RE-ANCHOR: clears stale        │
        │                       inference, corrects drift)     │
        │      │                                               │
        │      ▼                                               │
        │ choose action  +  EXPECTED post-condition            │
        │      │                                               │
        │      ▼                                               │
        │ act (tap/back/swipe …)  [pre-gated on state]         │
        │      │                                               │
        │      ▼                                               │
        │ re-perceive → actual                                 │
        │      │                                               │
        │      ▼                                               │
        │ compare(expected, actual):                           │
        │    match      → proceed                              │
        │    no-change  → vary / escalate (don't repeat)       │
        │    wrong-state→ handle actual state / escalate       │
        └─────────────────────────────────────────────────────┘
```

## 3. Core abstraction — a *Verified Action*

An action is not "tap (x,y)". It is an **intent + expectation + verification**:

```
VerifiedAction:
  precondition:  a predicate on the CURRENT perceived state (must hold before we act)
  do():          the raw effect (tap / press_back / swipe / input_text …)
  expected:      the post-condition we expect (a predicate on the NEXT perceived state)
  check_level:   how hard to look (L0 cheap … L3 full perceive) — see §4
  on_mismatch:   policy (retry-with-variation | handle-actual | escalate | refuse)

→ returns ActionOutcome{ ok, expected, actual, evidence, attempts, verdict }
```

The caller (FSM / goal / planner) supplies *intent + expectation*; the substrate enforces
*pre-gate → act → verify → policy*. Callers stop hand-rolling "tap then sleep then hope."

## 4. Verification levels (cheap → expensive)

Pick the **cheapest sufficient** check per action; don't run a full `perceive()` after every tap.

| Level | Mechanism (reuse existing) | Answers |
|---|---|---|
| **L0** region diff | `vision/frame_diff.classify_action_outcome` (unchanged / central_dialog / panel_or_button / state_change; ignores TOP_HUD) | Did *anything* change? Where? |
| **L1** state signature | `verify.py` `light_check` / `_state_signature` | Did the coarse state change as expected? |
| **L2** targeted assertion | one OCR/OmniParser/template check for the specific expected element (e.g. "Purchase grid visible", "world-map tab row", `_is_on_world_map`) | Is the *specific* expected thing present? |
| **L3** full perceive | `brain/perceive.perceive()` | Full state (use sparingly) |

Rule of thumb: a *toggle/tap* usually needs L0/L1; a *screen transition* needs L2; only ambiguous
cases fall through to L3.

## 5. Outcome taxonomy & policy

| Verdict | Meaning | Policy |
|---|---|---|
| **matched** | actual == expected | proceed |
| **no-change** | action had no effect (L0 unchanged) | retry ONCE with a variation; then escalate — never spam the same tap |
| **wrong-state** | changed, but not the expected state (e.g. a dialog/menu opened) | route the *actual* state to its handler (interruptor / back-out), then re-attempt or escalate |
| **refused** | a destructive-action guard tripped (Back-on-overworld, OK-on-quit, red-gem) | do not act; escalate |

Backed by **attempt memory** (already partially exists — reasoning-loop "Brick 2",
`feedback_stuck_vary_the_action`): track `(state, action, progressed?)`; exclude non-progressing
actions; detect cycling → escalate with "stuck, tried A / B / C" instead of looping.

## 6. What already exists (compose, don't rebuild)

This substrate is mostly **wiring existing parts**, not new vision:

- **Perceive**: `brain/perceive.perceive()`, `classify_nav_state`.
- **Cheap diff**: `vision/frame_diff.classify_action_outcome`.
- **State signature / checks**: `verify.py` (`light_check` / `heavy_check` / `_state_signature`).
- **Interruptors / dialog handling**: `brain/unexpected_dialog` (`clear_blockers`, promo/notice).
- **Destructive-action guards** (partial today): commit-button currency gate (refuse red-gem),
  `exit_to_overworld`'s Exit-Game dialog handling, `_assert_on_sea` / `_assert_at_port` location
  gates. These are *ad-hoc*; the substrate makes "pre-gate + refuse" uniform.
- **Escalation**: `brain/recovery`, Claude-consult path.
- **Tracing**: `actions/action_trace` already records taps + attaches perception — the raw material
  for expected-vs-actual replay.

Gap = there is **no single executor** that ties *expectation + verify + attempt-memory + escalate*
around a primitive. That's exactly the "unify the task executor" keystone from
`project_design_gap_analysis`.

## 7. Anatomy — a verified primitive (examples)

```
tap_and_verify(Market_tile,
               expected = StateIs("building", port_building="market"),
               check    = L2,
               on_mismatch = handle_actual)      # greeting page? overlay? re-tap; else escalate

back_safely(expected = StateChanged(),
            refuse_if = StateIs("port_overworld"))   # Back here opens "Exit Game?" → refuse
            # if an Exit-Game dialog IS up, Back = Cancel (safe); never tap OK

open_world_map()  # already unified: SEA→minimap centre, PORT→globe; verifies _is_on_world_map, retries
```

Reference table for the offenders we just touched:

| Action | Expected | Check | On mismatch |
|---|---|---|---|
| tap Market tile | `building:market` (or greeting→grid) | L2 | re-open grid; else escalate |
| open world map | `_is_on_world_map` true | L2 | retry (already implemented) |
| press_back (recovery) | state *changed* toward overworld | L1 | wait+re-perceive if settling; refuse on overworld |
| buy round | cargo/owned rose OR shelf grayed | L1/L2 | refresh or stop (already `buy_to_goal`) |

## 8. Migration plan (incremental — wrap, don't rewrite)

- **Phase 0 — guardrails (mostly done):** make destructive-action refusal uniform (Back-on-overworld,
  OK-on-quit/real-money, location asserts). Cheap, high safety payoff.
- **Phase 1 — retrofit the riskiest offenders** to *declare + verify*: `navigate_to_building`,
  world-map open (done), market buy/sell, recruit, negotiation. Keep signatures; wrap internals.
- **Phase 2 — a general `act(intent, expected, check)` executor** used by all *new* code; old call
  sites migrate opportunistically. This is the "unified task executor."
- **Phase 3 — expectation declared by the planner/FSM**, enforced uniformly by the substrate; retire
  ad-hoc post-tap subloops and blind sleeps.

Do NOT big-bang. Each phase is independently shippable and testable (mock-ADB unit tests assert the
verdict/policy, not the pixels).

## 9. Non-goals & open questions

- **Non-goal:** replacing perception/vision — this *consumes* it.
- **Open:** how expectations are declared — small predicate callables vs a per-screen expectation
  registry vs a tiny DSL. Lean callables first.
- **Open:** cost budget — verification must stay cheap (favor L0/L1); measure added latency per tap.
- **Open:** where expectations live — with the action, or keyed by (screen, control). Probably both:
  primitives carry a default, callers can override.
- **Open:** how this composes with the existing goal/FSM `tick` loop (which already re-perceives each
  tick) — the substrate is the *intra-tick* verify; the tick loop is the *inter-tick* one.

## 10. Relationship to existing rules

- Realizes **flow/plan completeness** at the primitive level: a step "completes" only when the
  *expected* recognized state is reached (not when a tap was issued).
- Directly supersedes the "stuck → vary the action" band-aids by making *verify + vary + escalate*
  the default, not an afterthought.
- When adopted, CLAUDE.md's "Decisions at the right level — escalate, don't absorb" rule becomes
  enforceable: a primitive returns a structured mismatch and the caller decides — instead of a
  primitive silently retrying forever.
