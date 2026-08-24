# Reasoning Fallback Layer — design

> **Status (reviewed 2026-08-23): PARTIALLY BUILT.** The LLM consult exists
> (`vision/obstruction_consult.py`, keyed by structural hash + goal), but it is gated
> behind an obstruction verdict that misses real modals, so it rarely runs. See
> `docs/one_loop_task_drives_state.md` for where the decision belongs (task, not
> dismissal layer).

A reasoning tier that sits **between automated recovery and human escalation**.
When the bot is stuck, in the wrong place, or on an unfamiliar screen, it hands
the **structured perceived state + its intent** to an LLM and gets back a
diagnosis + a next action from a fixed vocabulary. Written 2026-08-07.

## 1. Motivation — the failure that named it

A live London→Amsterdam round failed like this (session log 00:20–00:21):

```
Inside 'inn' — confirmed            ← the bot KNEW it was in the Inn
step 'step_2_navigate_inn' → NO_PROGRESS; terminating
commit_failure: 'inn_recruit_route'  fail=17         ← hammered a dead plan 17×
… fleet cannot depart → escalate → headless → 600s timeout → crash
```

The bot had **everything needed to self-correct** — it perceived it was in the
*Inn* while its goal required the *Harbor* — but nothing **reasoned** about that
mismatch. It retried a doomed plan, then asked a human who wasn't there. Two
gaps: (a) no reasoning over "wrong place vs. intent," (b) headless escalation
can only time out.

## 2. Why now — perception finally emits a clean description

The A2 work makes this feasible: `PerceivedState` is a compact, structured
description an LLM can reason over directly (no pixels, no VLM needed):

```
base=panel  context=Inn  mode=—  menu_item=Hire
menu=[Recruit, Hire, Party, Employee, Manage Mate]
title="Inn"  panels={center:mate_list, right:none}  overlay=none
buttons=[Hire, Party, …]
```

Per the backlog **B8** rule (bounded-clean-text → text LLM; whole-scene/noisy →
VLM): the structured state is bounded clean text, so this is a **text-LLM** tier.
Only if structure extraction itself fails (truly unknown screen) do we fall back
to a Claude-Vision *describe*.

## 3. When it fires (triggers)

1. **Plan no-progress** — `plan_loop` reports `light_no_progress` / repeated
   `commit_failure` (the inn_recruit_route 17× case) — *before* abandoning.
2. **Precondition mismatch** — the perceived `base`/`context` doesn't satisfy the
   current step's precondition (goal needs `Harbor`, state is `Inn`). Proactive:
   catches wrong-place *before* running a doomed plan.
3. **Unfamiliar screen** — classify returns `unknown` / low-confidence / no
   fingerprint match.
4. **Before human escalation** — the first thing `escalate()` tries, replacing
   the headless 600s timeout.

## 4. LLM input contract (all bounded clean text)

```
INTENT:
  goal: "depart and sail London → Amsterdam"
  current step: "navigate to Harbor and depart"
  expected precondition: base=panel, context=Harbor  (or overworld to sail)
OBSERVED (structured PerceivedState):
  base=panel context=Inn title="Inn" selected=Hire
  menu=[Recruit,Hire,Party,Employee,Manage Mate]  panels={center:mate_list}
  overlay=none  buttons=[Hire,Party,Back]
RECENT HISTORY (last N ticks):
  (Inn, tap Hire, no_progress) ×17
GAME PRIMER (static, compact):
  - Harbor = resupply + depart to sea. Inn = recruit mates. Market = trade goods…
  - To sail: be at port_overworld or in Harbor → depart.
  - Back/Home exits a building to port_overworld.
```

The **game primer** is a short, curated block (bounded — the B8 discipline),
not the whole rulebook.

## 5. LLM output contract (structured, whitelisted)

```json
{
  "screen_identity": "Inn (recruit mates)",
  "matches_intent": false,
  "diagnosis": "In the Inn, but departure needs the Harbor.",
  "confidence": 0.9,
  "next_action": {"op": "go_to_building", "arg": "Harbor"}
}
```

`next_action.op` is constrained to a **fixed vocabulary** that maps 1:1 to
existing primitives:

| op | primitive |
|---|---|
| `exit_building` | `exit_to_overworld` / `exit_building` |
| `go_to_building(name)` | `navigate_to_building(name)` |
| `tap(label)` | `_find_button(frame, label)` |
| `back` | `press_back` |
| `wait` | sleep + re-perceive |
| `abort(reason)` | return structured failure to caller |

No free-form taps — the whitelist keeps the LLM from inventing coordinates.

## 6. Action execution + guardrails

- Map `next_action` → primitive; **primitives still gate on location**
  (`_assert_on_sea`/`_assert_at_port`) — the reasoning layer *proposes*, the
  primitives *verify*. (Preserves the transactional-primitive rule.)
- **Red-gem discipline preserved** — `tap` never confirms a red-gem spend; those
  still require explicit user confirmation.
- **Bounded retries** — max K reasoning cycles per stuck-episode; then human
  escalation (or abort, headless).
- **Log** every `(intent, structured_state, llm_verdict, action, outcome)` for
  audit + learning.
- **Learn once** — persist a successful resolution keyed on
  `(context, intent-class)` into `learned_recoveries.json`, so the same
  wrong-place is fixed *without* an LLM call next time (ties into the existing
  learned-recovery replay in `escalate`).

## 7. Integration points (extend, don't fork)

- **`brain/human_escalation.escalate()`** — call the reasoning fallback *first*;
  only fall through to the human teaching loop if the LLM can't resolve or
  confidence < threshold. **This fixes the headless-timeout crash.** There is
  already a `_resolve_blocker_with_reasoning` + `reclassify_with_claude` here —
  **consolidate** the new tier with those (one canonical reasoning entry), don't
  add a parallel path.
- **`brain/plan_loop.achieve_goal`** — add a replan decision that invokes the
  reasoning fallback on `light_no_progress` before demoting/abandoning a plan
  (catches inn_recruit_route-style dead loops early).
- **Flow failure points** (e.g. `sail_actions._ensure_fleet_ready`,
  building-nav failures) — route through the reasoning fallback instead of
  straight to `escalate`.
- **Serializer** — one function `perceived_state_to_prompt(PerceivedState, goal,
  history)` producing §4; lives next to `PerceivedState`.

## 8. Phasing

- **Phase 1** — reasoning fallback as the pre-step inside `escalate()`:
  serializer + LLM contract + whitelisted executor. Fixes the immediate
  Inn/Harbor + headless case. *Highest value, self-contained.*
- **Phase 2** — wire into `plan_loop` no-progress (kill dead plans at ~3 fails,
  not 17).
- **Phase 3** — proactive precondition-mismatch trigger (detect wrong-place
  before a plan runs), using the step's declared precondition vs `base/context`.
- **Phase 4** — learn-once persistence (skip the LLM on repeat).

## 9. Relation to existing work / backlog

- Consumes the **A2 structured `PerceivedState`** (base/context/menu_item) — its
  first real consumer beyond classification.
- Realizes backlog **B8** (bounded-text → LLM) and **D12** (dialog/blocker
  resolution via reasoning).
- Embodies the **"escalate, don't absorb"** principle: reason at the LLM tier
  before a human, and let the *caller/planner* decide — the reasoning layer
  returns a structured verdict, it doesn't silently act inside a primitive.
- Model choice + latency budget — **resolved, see §11**. This is off the nav
  hot-path (fires only when stuck/unknown), so a slower, more capable model is
  fine — same "precision over speed when not navigating" principle as the
  OmniParser readers.

## 10. Scope — not just recovery: the general next-action operator

The same machinery serves **two roles**, and the second is the bigger one:

1. **Recovery fallback** — stuck / wrong-place / unfamiliar (the Inn/Harbor case).
2. **Primary executor for un-scripted operations** — anything without a
   deterministic skill: restore/repair a ship, use a blueprint, an unfamiliar
   building flow, a new event dialog, an operation whose *location* isn't
   hardcoded. The LLM **infers the next action** from context — including *where*
   the operation happens (e.g. "restore is at the Shipyard") — reasoned from game
   knowledge, not a script.

**"Provide enough information" is the design goal.** The LLM's context is exactly
what the lower layers produce:
```
GOAL (intent)  +  WORLD MODEL (Company→Fleets→Ships: currencies, ship life,
                  supply, discovered ports, cargo)  +  PERCEPTION (structured
                  PerceivedState of the current screen)  +  GAME KNOWLEDGE
                  (compact primer: where/how operations happen)  +  ACTION VOCAB
                  (skill names + visible UI elements)
```
Loop: **infer one action → execute → re-perceive → repeat** (the `claude_guidance`
frame-by-frame loop, enriched with the world model + game knowledge). This is why
the perception + world-model work matters — they *are* the information.

**Fast path vs long tail.** Common/hot operations (buy, sell, sail) stay
deterministic scripted skills — fast, cheap, no LLM. The long tail is
LLM-inferred. Script the common; reason the rest.

**Learn once (the KB grows from inference).** A successful LLM-inferred flow is
recorded as a new skill/recovery keyed on `(context, intent)`, so next time it's
deterministic and free. **Not-in-KB → describe structurally → LLM infers →
succeed → now it's in the KB.** The bot never hard-fails on an unknown screen; it
reasons, acts, and *learns* the flow.

## 11. Tooling — what actually runs the learning

The "learning" is two separate things with different tools; most already exist
in the codebase.

**Runtime reasoning/inference = an LLM call (not training).**
- **Claude API** (`anthropic`, already wired: `claude_vision`,
  `reclassify_with_claude`, `claude_guidance`) — the **primary** engine. Off the
  hot-path, so a capable cloud model is fine on cost/latency.
- **Ollama** (already in `requirements.txt`) — a **local** tier (Llama/Qwen) for
  free/offline; good as a cheap first pass with Claude as fallback. (The old
  mlx-lm Qwen-0.5B was too small — use a bigger Ollama model if going local.)
- **Claude Vision / Moondream** (already used) — only for a *truly* unknown
  screen where structure extraction fails; the default is **text-LLM over the
  OmniParser-extracted structure** (B8 rule: bounded clean text → text LLM).

**Learn-once = JSON KB persistence (no ML).** A learned recovery / operation
flow / affordance / completion-signature is **written to a JSON KB** under
`memory/knowledge/` — the existing pattern (`learned_recoveries.json`, `scenes/`,
`learned_fingerprints/`). Retrieval is a lookup. This is what makes it
deterministic and free next time. Tool = JSON + the existing KB machinery.

**Distillation = optional, later.** Once many `(context → action)` decisions
accumulate, train a small **local policy** to replace the LLM for common cases
(backlog C10): `torch` (installed) for a small classifier/policy, or **mlx-lm
LoRA** (Apple-Silicon fine-tune; would need install). A scale optimization, not
needed to start.

**Start here:** Claude API for reasoning + JSON KB for recording — both already
in the codebase, so the full learn-once loop needs **zero new infrastructure**;
it's wiring, not new tools. Add Ollama (offline/cost) and/or distillation (speed
at scale) later.

## 12. Development methodology — instrument, run, iterate

**Don't guess what context the LLM needs — log it and find out.** The two open
questions ("does it need more game knowledge?" vs "is a perception field
missing?") are answered by evidence, not design intuition. Same "instrument
before patching" discipline the rest of the bot uses.

**Reasoning-trace log.** Every reasoning-layer call persists one JSONL record
under `memory/knowledge/reasoning_traces/`:
```
{ trigger, goal,
  context_sent: { world_model, perception (PerceivedState), game_knowledge,
                  action_vocab },              # EXACTLY what the LLM received
  llm_response_raw, parsed_action,
  outcome: { action_taken, new_state, progressed },
  result: resolved | failed | gave_up }
```
Log the **full context sent** and the **full response** — the point is to see
what the LLM had to work with and what it did.

**The gap-closing loop.** Run the bot → review traces where the action was
wrong / uncertain / failed → for each, ask "what fact would have made this
obvious, and which layer should carry it?":
- missing "Recruit Crew is at the Harbor" → **game-knowledge** primer;
- missing crew count → **world model**;
- didn't realise the screen was the Inn → **perception**.
Add it, re-run, confirm the trace resolves. The trace **disambiguates a knowledge
gap from a perception gap** — you're not guessing which.

**Shadow mode.** Run the reasoning layer in *log-only* mode first — record what it
*would* do without executing — to gather traces safely before letting it drive.

**Dual purpose.** These traces are also the **distillation training data** (§11):
(context → good action) pairs accumulated from real runs.

**Build implication.** Trace-logging is one of the *first* things to build — it's
the microscope that tells you what knowledge/perception to add, so it should
precede tuning the prompt or the game primer.
