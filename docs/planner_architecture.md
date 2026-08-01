# Planner Architecture — Plan, Execute, Verify, Replan

*This is the canonical architecture document for how the bot navigates the
game world.  It supersedes the recipe/edge/flow distinctions used by earlier
designs and unifies them under a single concept: **the plan**.*

*Status of related docs:*
- `fsm_design.md` — superseded; the FSM/recovery split it describes is
  collapsed into the loop below.
- `fsm_planning_and_edge_learning.md` — partly superseded; it introduced the
  planner concept and the edge/metadata foundations this doc builds on, but
  its three-rule policy is reframed here as a single self-enhancing cycle.
- `blocking_signals_design.md` — superseded; recipe-based blocking-signal
  resolution is replaced by plan-execute-verify-replan in this doc.
- `perception_reasoning_layer.md` — still current; the L2.5 Qwen layer is a
  perception primitive used by this architecture.
- `bot_design_philosophy.md` — still current; this doc is the concrete
  realisation of those principles.
- `kb_design.md` — needs minor update for the Plan KB and Cue Catalog KB
  introduced here.

---

## The reframe in one sentence

**There is one loop: plan → execute one step → perceive → if needed, replan.
Everything else is a specialisation of that loop.**

Recipes, edges, flows, resolutions, recoveries, claude-guided sub-loops —
they are all *plans of varying quality, provenance, and maturity*.  Naming
them differently was an artifact of how the codebase grew.  This doc names
the unifying concept and reorganises the existing pieces around it.

---

## The central loop

```
┌──────────────────────────────────────────────────────────────────────┐
│                                                                       │
│   plan ← planner.plan(state, goal, history)                           │
│                                                                       │
│   while plan has remaining steps:                                     │
│       step ← plan.next_step()                                         │
│       before_frame ← capture()                                        │
│       execute(step.action)                                            │
│       after_frame  ← capture()                                        │
│       state        ← perceive(after_frame)                            │
│                                                                       │
│       light ← check_progress(before_frame, after_frame, step)         │
│       if light is NO_PROGRESS:                                        │
│           plan ← planner.plan(state, goal, history, replan_reason)    │
│           continue                                                    │
│                                                                       │
│       if step.is_checkpoint:                                          │
│           heavy ← check_goal(state, goal, cue_catalog)                │
│           cue_catalog.update(heavy.cues_observed)                     │
│           if heavy is GOAL_ACHIEVED:                                  │
│               planner.commit_success(plan, history)                   │
│               return SUCCESS                                          │
│           if heavy is UNCERTAIN or NOT_YET:                           │
│               plan ← planner.plan(state, goal, history, replan_reason)│
│               continue                                                │
│                                                                       │
│   # plan exhausted without GOAL_ACHIEVED                              │
│   plan ← planner.plan(state, goal, history, "exhausted")              │
│   if plan is empty:                                                   │
│       return planner.escalate_to_human(goal, history)                 │
│                                                                       │
└──────────────────────────────────────────────────────────────────────┘
```

That is the whole architecture.  The rest of this document explains the
data shapes, the primitives, and the migration from current code.

---

## Plan as the unifying concept

A **plan** is a sequence of steps the planner believes will move the bot
from its current state to the goal.

```python
@dataclass
class Plan:
    plan_id:     str
    goal_id:     str
    steps:       list[PlanStep]
    provenance:  str   # 'hand_authored' | 'claude_generated' | 'human_taught' | 'distilled'
    confidence:  str   # 'unverified' | 'low' | 'medium' | 'high'

    travel_count:        int
    success_count:       int
    failure_count:       int
    avg_duration_secs:   float
    first_traveled_at:   Optional[str]
    last_traveled_at:    Optional[str]

    notes:       str

@dataclass
class PlanStep:
    step_id:           str
    action:            dict   # the actuation: tap, press_back, navigate_to, etc.

    # Light verification annotation — how to recognise the step took effect.
    expected_progress: ProgressExpectation

    # Heavy verification annotation — only on checkpoint steps.
    expected_goal:     Optional[GoalCheckpoint] = None

    # Per-step counters (independent of the plan-level ones).
    travel_count:      int = 0
    success_count:     int = 0
    failure_count:     int = 0
```

Every artefact the codebase calls a *recipe*, *edge*, *flow*, or *resolution*
maps onto this structure.  The 6-step `harbor_departure.blocking_signals[
"not enough crew"].resolution` is a `Plan` whose provenance is
`hand_authored` and whose confidence is currently undeclared (treated as
`high` by default — historically taken on faith).

### Plans differ in quality, not in kind

| Today's name | What it really is |
|---|---|
| `flows.json:harbor_departure.blocking_signals[].resolution` | A `Plan` with `provenance='hand_authored'` |
| `claude_guided_loop` output | A `Plan` with `provenance='claude_generated'` |
| `learned_recoveries.json` entry | A `Plan` (typically single-step) with `provenance='human_taught'` |
| `flows.json:learned_*` entry | A `Plan` with `provenance='claude_guided'` |
| BFS-derived recovery path (`recover_to_port_overworld`) | A `Plan` with `provenance='hand_authored'` (encoded in `states.json`'s exit edges) |
| Cycle-detected synthetic 'complete' | A patch where verification didn't exist; replaced by heavy check |

The same metadata fields — provenance, confidence, success/failure counts —
apply to all of them.  The same demotion mechanics apply to all of them.
The same loop runs all of them.

---

## Goal as the verification target

```python
@dataclass
class Goal:
    goal_id:        str
    description:    str
    predicate_text: str    # human-readable summary of "what does success look like"

    # Reference to the per-goal cue catalog (separate KB file).
    cue_catalog_path: str

    # Optional structured predicate for cheap evaluation (e.g.
    # "fleet.crew_count_after > fleet.crew_count_before").  When the cheap
    # predicate is decisive, heavy check can short-circuit; otherwise it
    # falls through to Claude.
    cheap_predicate: Optional[Callable[[PerceiveResult], bool]] = None
```

A goal is what the heavy check evaluates against.  A goal is NOT the action
sequence — that's the plan.  A single goal can have many plans (different
providers, different routes, different histories).

Examples:
- `has_enough_crew` — "fleet crew sufficient for departure"
- `at_port_overworld` — "bot has reached the port_overworld navigation state"
- `at_destination_port(name)` — "bot is at port `<name>`'s overworld"
- `recruit_crew_committed` — "the recruitment transaction has actually committed"

Goals can have sub-goals.  `has_enough_crew` is satisfied either by
`recruit_crew_committed` (path A) or by `redistribute_crew` (path B); the
planner picks among them when generating a plan.

---

## Light verification — "have I made progress?"

Cadence: after every executed step.  Cost: cheap, mostly local.

### What it asks

> Did the screen change in a way consistent with the action firing?

It does **not** ask "did we reach the goal?".  It only asks "did anything
happen?".  A confirmation dialog appearing after a tap is *progress* — the
goal isn't reached, but the action took effect and the bot is in a new
position to continue.

### Three tiers, in increasing cost

```
Tier 0 — pure local (~free, ~milliseconds)
  cheap_signature_before = (state, detail, top-N OCR tokens, has_action_button)
  cheap_signature_after  = same shape
  if before != after:
      return PROGRESS
  if before == after for 3 consecutive frames:
      return NO_PROGRESS
  else:
      return CLARIFY     # transient — wait one more frame

Tier 1 — local + Moondream (only on Tier-0 CLARIFY, ~1-2s)
  Moondream: "did the screen visibly change between these two frames?
              yes/no/uncertain"
  return PROGRESS / NO_PROGRESS / NEED_HELP

Tier 2 — Claude (only when light is consistently failing on a step that
                 has succeeded historically; rare)
  Claude: "what changed?  did the change advance toward goal '<goal>'?"
  Claude's answer also feeds the cue catalog as a side effect.
```

Tier 0 handles the steady-state.  Tier 1 catches mid-animation transients.
Tier 2 is the rarely-fired diagnostic when a previously-reliable step has
started misfiring (game UI drift).

### What light catches that today's code misses

The May-2 silent-skip case:

```
Step 3 fires: tap recruit_crew submenu
  Tier 0: state changes from inn-top to recruit-crew sub-screen → PROGRESS ✓
Step 4 fires: tap_home (planned exit)
  Tier 0: state changes to port_overworld → PROGRESS ✓
```

Light correctly reports progress at every step.  Light *cannot* catch the
fact that the bot's progress is in the wrong direction (recruitment never
committed).  That's heavy's job.

---

## Heavy verification — "have I solved it?"

Cadence: at checkpoint steps + at plan completion.  Cost: Claude visual call
with curious-observation prompt.

### What it asks

> Has the goal predicate been satisfied?  Look at the per-goal cue catalog
> AND any other visual signals you can see.

Heavy is the place where the bot's own perception (chrome, OCR, signature)
is **deliberately bypassed** in favour of Claude's richer visual reasoning.
The whole point is to fill the "weak perceiving + weak reasoning" gap that
the local pipeline can't close.

### The Claude prompt — curious observation, not yes/no

```
You are verifying whether a multi-step task has actually achieved its goal.

GOAL: <goal_id> — <predicate_text>

CUES we know to look for (from prior observations):
  • <cue 1>  (confidence: high, observed 47×)
  • <cue 2>  (confidence: medium, observed 12×)
  • ...

ALSO look at the screenshot for any visual indicators relevant to the goal
that aren't in the list above — colour states (red/green/yellow), badges,
dots, meter fills, button enabled-vs-disabled, NPC dialogue text changes,
numerical changes, etc.

Reply as JSON:
{
  "goal_achieved": "yes" | "no" | "uncertain",
  "confidence":    0.0 to 1.0,
  "cues_observed": [
    { "cue": "<text>", "present": true|false,
      "confirms_goal": true|false }
  ],
  "new_cues": [ "<additional indicator you noticed>" ],
  "evidence_summary": "<one sentence describing what you actually see>"
}
```

The output is **structured**, **opinionated**, and **growable**.  Each call
is both a verification AND an observation that feeds the cue catalog.

### What heavy catches

The May-2 silent-skip:

```
Step 4 heavy at exit:
  Goal: has_enough_crew
  Cues to check: ["fleet crew count strictly increased",
                  "no 'not enough crew' warning at harbor"]
  Claude observes: fleet crew unchanged (still N, was N before recipe
                   started), warning still present
  Returns: { goal_achieved: "no", evidence_summary: "fleet crew is
            unchanged; recruitment did not commit" }
```

The bug today's cycle-detector cannot detect — because the local frame
signature looked identical — heavy catches because it's evaluating *outcome*,
not *frame state*.

---

## Cue catalog — accumulated, growing, audited

Per goal, lazy growth, provenance-tracked.

### File layout

```
memory/knowledge/verification/<goal_id>.json
```

### Schema

```jsonc
{
  "goal":           "has_enough_crew",
  "description":    "Recruitment committed; fleet has crew sufficient for departure",
  "predicate_text": "fleet crew count strictly increased AND no 'not enough crew' warning",

  "cues": [
    {
      "cue":        "fleet crew count strictly greater than before-frame",
      "polarity":   "confirms",
      "confidence": "high",
      "stats":      { "observed": 47, "confirmed": 46, "refuted": 1 },

      "provenance": {
        "source":          "hand_authored",
        "first_seen":      "2026-04-29T15:00Z",
        "first_seen_in":   "flows.json:harbor_departure (not_enough_crew)",
        "noted_during":    null,
        "noted_by":        null
      }
    },
    {
      "cue":        "no red dot on the Recruit Crew menu item in the inn",
      "polarity":   "confirms",
      "confidence": "medium",
      "stats":      { "observed": 12, "confirmed": 12, "refuted": 0 },

      "provenance": {
        "source":          "claude_observed",
        "first_seen":      "2026-05-02T16:43Z",
        "first_seen_in":   "claude_guided_loop (recipe: harbor_departure_not_enough_crew, step 3)",
        "noted_during":    "heavy_check_after_step_4",
        "noted_by":        "claude_vision"
      }
    },
    {
      "cue":        "harbor official NPC says 'Ready to sail' instead of 'Not enough crew'",
      "polarity":   "confirms",
      "confidence": "high",
      "stats":      { "observed": 5, "confirmed": 5, "refuted": 0 },

      "provenance": {
        "source":          "human_taught",
        "first_seen":      "2026-05-02T20:00Z",
        "first_seen_in":   "interactive teach session, step 5",
        "noted_during":    "human input",
        "noted_by":        "human_operator"
      }
    }
  ],

  "open_questions": [
    "Does the red dot also indicate other crew problems (loyalty, injury)?
     One verification observed crew was sufficient but red dot remained."
  ]
}
```

### Provenance dimensions

- `source` — `hand_authored | claude_observed | human_taught | distilled | inherited`
- `first_seen` — UTC timestamp
- `first_seen_in` — what context produced the cue (recipe + step, teach
  session, replan call, etc.)
- `noted_during` — which verification phase noticed it
- `noted_by` — `claude_vision | qwen | moondream | human_operator | hand_authored`

### Cue lifecycle

```
candidate (new, low confidence, not yet trusted)
    │
    │ accumulates confirmations through subsequent verifications
    ▼
medium  ─── refuted enough times ──▶ pruned (manual or auto)
    │
    │ continued confirmations
    ▼
high (canonical signal for this goal)
```

### Curation policy

Lazy growth — no automatic cap, no automatic pruning.  The catalog grows as
verifications run.  When a catalog gets noisy (>30 cues, or many low-
confidence entries), it is curated manually.  This is the agreed simplest
starting point; if cost or signal-to-noise becomes a problem, automated
pruning can be added later.

---

## Replan — the planner's self-enhancement cycle

Replan is **not a special-case error handler**.  Replan is `planner.plan()`
called again with updated inputs.  The same function that produced the
initial plan produces the next plan when reality has diverged from
expectation.

### What triggers replan

1. **Light reports NO_PROGRESS** for more than one perceive cycle —
   the executed action didn't fire as expected.  Don't ask "are we done?";
   ask "what's actually happening, and what should we do from here?".

2. **Heavy reports UNCERTAIN** — Claude couldn't confidently say whether
   the goal was achieved.  Re-evaluate: has any progress been made?  Do
   the observed cues suggest the next planned action is still right?

3. **Heavy reports NOT_YET at end of plan** — plan exhausted without
   reaching the goal.  Either the plan was incomplete (extend it) or
   wrong path (replace it).

### The replan call

When triggered, the planner sends Claude the rich context.  This is heavy
+ a decision question, in one API call:

```
You are planning the bot's next move.

GOAL: <goal>
PLAN-IN-PROGRESS: <list of steps, with which ones already executed>
HISTORY: <observations from each executed step's light + heavy checks>
CURRENT FRAME: <screenshot>
CUE CATALOG: <relevant cues for this goal>

Q1. Has any progress been made since the plan started?  Compare the
    current screen against the start-of-plan context.

Q2. Looking at the current screen, what action OPTIONS do you see?
    (list buttons, menu items, dialog choices, back/home affordances)

Q3. Do the cues you observed change what action makes sense next?

Q4. Decision — should the bot:
      a) continue the current plan unchanged
      b) insert one or more corrective steps before continuing
      c) replace the remaining plan with a new sequence
      d) escalate to human (you don't know enough to advise)

    If b or c, list the specific step(s).

Reply as JSON:
{
  "progress_made":   "yes" | "partial" | "no",
  "new_cues":        [...],
  "options_visible": [...],
  "decision":        "continue" | "insert_step" | "replace" | "escalate",
  "inserted_steps":  [{ action, expected_progress, expected_goal? }, ...],
  "updated_plan":    [...],
  "reasoning":       "..."
}
```

The output amends the plan in-place.  The bot continues the loop with the
new plan.  Inserted steps inherit `provenance='claude_replan'` and start at
`confidence='unverified'`; promotion follows the standard lifecycle.

### Why this is the architecture's "exploration policy"

The architecture doc that preceded this one (`fsm_planning_and_edge_learning.md`)
described three exploration rules (Rule 1 / 2 / 3) for selecting between
candidate edges.  Under this design those rules are still valid, but they
live INSIDE the planner's lookup-and-generate pipeline, not as a separate
selection module:

| Architecture-doc rule | Realisation |
|---|---|
| Rule 1 — zero edges → learn | `planner.plan()` finds no plan in KB → asks Claude to generate one |
| Rule 2 — single edge with `travel_count=1` → forced exploration | `planner.plan()` finds exactly one weak plan; on N-th encounter, asks Claude to propose an alternative |
| Rule 3 — multiple mature edges → epsilon-greedy | `planner.plan()` finds multiple plans; picks best by quality with ε% chance to try a non-leader |

The replan call subsumes all of them: it's `planner.plan()` invoked with
strong context, and the policy decisions happen inside that single function.

---

## The "weak plan" boot-strapping principle

**You don't earn the right to a plan by reasoning perfectly.  You start with
whatever rough sketch you have, and let execution + verification + replan
correct it.**

Three consequences:

1. **Hand-authored content is just frozen output of an earlier (human)
   planner.**  It carries no special status.  The 6-step
   `harbor_departure.blocking_signals.not_enough_crew.resolution` is a plan
   produced by a human under fire-fighting pressure, persisted, never
   updated.  The same provenance/confidence/history fields apply to it as
   to anything Claude generates.

2. **An empty/weak plan is better than no plan**, because the alternative
   is no execution and therefore no learning.  The planner's worst case is
   "tap somewhere reasonable and see what happens" — even that produces
   a perceive result that informs the next plan.

3. **Removing a weak plan manually short-circuits the very mechanism the
   architecture is designed to deliver.**  Either the plan grows into a
   working plan (through replan adding the missing steps) or it gets
   demoted in favour of a better alternative.  Both outcomes are the
   architecture working correctly.

This reframes how the migration thinks about existing artefacts.  The
6-step recipe stays in `flows.json` until the planner naturally either
strengthens it (replan inserts the missing tap-OK + tap-OK-on-confirm
sub-steps; success accumulates; confidence promotes) or replaces it
(harbor.recruit_crew alternative wins on quality; inn-route gets demoted to
`supersedes` link from the harbor route).

---

## Worked example — the 6-step under this architecture

### Static state at start of run

`flows.json:harbor_departure.blocking_signals[0]`:
```jsonc
{
  "text": "not enough crew",
  "action": "resolve",
  "resolution": [...]
}
```

This becomes a `Plan` of provenance `hand_authored`, confidence
`unverified` (no Phase-4 metadata yet), and steps:

```
[
  { id: "step_1", action: exit_to_port_overworld,         expected_progress: "state==port_overworld" },
  { id: "step_2", action: navigate_to_building(inn),      expected_progress: "state==building, detail contains 'inn'" },
  { id: "step_3", action: tap_primary_action(recruit_crew), expected_progress: "detail contains 'recruit crew', primary action button visible" },
  { id: "step_4", action: exit_to_port_overworld,         expected_progress: "state==port_overworld",
                                                          expected_goal:     { goal: "has_enough_crew" } },
  { id: "step_5", action: navigate_to_building(harbor),   expected_progress: "state==building, detail contains 'harbor'" },
  { id: "step_6", action: retry,                          expected_progress: "the original blocking_signal has cleared",
                                                          expected_goal:     { goal: "has_enough_crew" } }
]
```

### First run with this architecture

```
Step 1: exit_to_port_overworld
  before: harbor screen with "Not Enough Crew" warning
  action: tap_home
  after:  port_overworld
  light:  PROGRESS (state changed)            ✓
  heavy:  not a checkpoint                    -

Step 2: navigate_to_building(inn)
  before: port_overworld
  action: tap inn entry in building list
  after:  inn interior
  light:  PROGRESS                            ✓
  heavy:  not a checkpoint                    -

Step 3: tap_primary_action(recruit_crew)
  before: inn interior
  action: tap recruit_crew submenu
  after:  recruit_crew sub-screen with gold Recruit button
  light:  PROGRESS                            ✓
  heavy:  not a checkpoint                    -

Step 4: exit_to_port_overworld
  before: recruit_crew sub-screen
  action: tap_home
  after:  port_overworld
  light:  PROGRESS (state changed)            ✓
  heavy:  CHECKPOINT — goal: has_enough_crew
          Claude evaluates: fleet crew unchanged.  Warning still latent.
          Returns: goal_achieved=no, confidence=0.95, evidence: "crew
                   count is identical to before this plan started"
          → REPLAN TRIGGERED

  Replan call:
    Claude observes:
      progress_made: "no, the recruitment never committed; the recipe
                      jumped directly from opening the recruit submenu
                      to exiting, skipping the actual recruit + confirm"
      decision: insert_step
      inserted_steps:
        [
          { action: tap_button("Recruit", region: right_panel_gold_btn),
            expected_progress: "confirmation dialog appears" },
          { action: tap_button("OK", region: confirm_dialog),
            expected_progress: "dialog closes; crew count increments" }
        ]
    The bot rolls back to before step 4, inserts the two new steps
    between step 3 and step 4, and continues.

  step_3.5_a (newly inserted):
    action: tap Recruit gold button
    light:  PROGRESS (confirm dialog appeared)      ✓
    heavy:  not yet a checkpoint                    -

  step_3.5_b (newly inserted):
    action: tap OK on confirm dialog
    light:  PROGRESS (dialog dismissed)             ✓
    heavy:  CHECKPOINT — fleet crew increased       ✓ goal_achieved (intermediate)

  Step 4 retry: exit_to_port_overworld
    light:  PROGRESS                                ✓
    heavy:  goal_achieved (since crew is now sufficient AND we have
            exited)                                 ✓

Step 5: navigate_to_building(harbor)
  light: PROGRESS  ✓

Step 6: retry
  before: harbor with no warning
  action: re-test blocking_signal
  light:  PROGRESS (Depart Now is gold/active)
  heavy:  goal_achieved                             ✓ FINAL SUCCESS

planner.commit_success(plan, history):
  - The amended plan (now 8 steps) is persisted as a new plan with
    provenance="claude_replan", confidence="unverified", success=1
  - The original 6-step plan's failure_count increments by 1 (it
    failed without the inserted steps; the inserted steps are now
    a sibling plan)
  - Cue catalog for has_enough_crew gains:
      "confirmation dialog appears after tap Recruit" (new, claude_observed)
      "fleet crew count strictly greater after tap OK" (confirmed)
      "Depart Now button is gold not gray" (new, claude_observed)
```

### Subsequent runs

The 8-step amended plan is now a candidate alongside the 6-step.  Quality
comparison:
- 6-step: success_count=0, failure_count=1+, confidence=low (after enough
  failures)
- 8-step: success_count=1, confidence=unverified, but no failures

`planner.plan()` selects the 8-step.  Each successful traversal increments
its success_count and grows the cue catalog further.  Eventually the 8-step
reaches `confidence=high` and the 6-step is auto-demoted by the existing
Phase 4 mechanism.

If the harbor.recruit_crew alternative is added (as a sibling plan with its
own steps), Rule 2-style forced exploration on the early traversals exposes
it; if it's faster (saves the inn round-trip), Rule 3-style epsilon-greedy
selection shifts traffic to it.

---

## Migration from current code

This is a reorganisation, not a rewrite.  The pieces all exist; they need
to be renamed, reconnected, and have one missing primitive added (light
verification + cue catalog + replan call).

### Layer 1 — Plan and Goal as data types  *(small, no behaviour change)*

- `Plan`, `PlanStep`, `Goal` dataclasses
- Re-read existing `flows.json` entries as Plans on load (no schema change
  yet — the new fields are added when next-saved)
- `Goal` registry with cue_catalog_path per goal

### Layer 2 — Light verification + cue catalog  *(new primitives)*

- `light_check(before_frame, after_frame, expected_progress)` with three
  tiers
- Cue catalog file format and CRUD helpers
- `heavy_check(state, goal)` with the curious-observation Claude prompt

### Layer 3 — Planner facade  *(unification of existing scattered pieces)*

- `planner.plan(state, goal, history, replan_reason=None)` — looks up cached
  plans, picks one, falls through to Claude generation if none found
- `planner.commit_success(plan, history)` — persistence + metadata update
  + cue catalog update
- `planner.escalate_to_human(goal, history)` — wraps the existing teach loop
- `achieve_goal(goal, context)` — runs the central loop above
- Existing entry points (`execute_resolution`, `recover_to_port_overworld`,
  `claude_guided_loop`) become thin callers of `achieve_goal` with specific
  goals

### Layer 4 — Replan as planner.plan() invoked mid-execution  *(behaviour change)*

- The same function that produced the initial plan accepts `replan_reason`
  and `history` arguments to produce an amended plan
- Cycle-detection rail in `claude_guided_loop` retires (replaced by
  goal-predicate evaluation in heavy)
- `handle_unknown_blocking`'s dismiss-and-act behaviour retires (replaced
  by replan triggered from light NO_PROGRESS)

### Order of work

1. Layer 1 lands first (no behaviour change; just gives types to existing
   data)
2. Layer 2 adds the verification primitives (light becomes available; cue
   catalog seeded for one or two goals)
3. Layer 3 introduces the planner facade and migrates `execute_resolution`
   to use it (one specific goal at a time — start with `has_enough_crew`)
4. Layer 4 enables replan on uncertainty/no-progress, retires the patches

After Layer 4 the existing `recovery.py`, `claude_guidance.py`, and
`human_escalation.py` files have minimal logic left — they become thin
specialised planner callers.

---

## What this design does NOT change

- The interruptor pass in `perceive.py`.  Pass-1 known interruptors stay as
  the fast filter for overlay popups.  Auto-purge / loop guard stay.
- The teach loop's interactive multi-step contract.  Human teaching still
  produces multi-step plans saved with `provenance='human_taught'`.
- The save-time plausibility guard for `terminal_state in {sea_cinematic,
  unknown}`.  Stays as a defence-in-depth.
- The data flywheel pattern (Claude as teacher, local models as students)
  for vision.  Orthogonal to this architecture; continues unchanged.
- The KB structure for game-content knowledge
  (`memory/knowledge/control/`, `memory/knowledge/strategy/`).  These are
  inputs to the planner, not part of it.

---

## Open design questions, deferred deliberately

1. **Cue catalog automated pruning.**  Lazy growth is the agreed starting
   point; automation lives behind a manual curation step until the cost
   becomes painful.

2. **Sub-goal decomposition.**  A goal like `has_enough_crew` could be
   formally broken into `recruit_crew_committed` OR `crew_redistributed`
   OR `sail_crew_purchased` — providers of the parent goal.  This is the
   capability registry from the predecessor doc.  Useful but not blocking;
   add when the planner has demonstrated multiple plans coexisting under
   one goal.

3. **Plan-quality scoring beyond `success_rate × avg_duration`.**  Cost
   awareness (gold spent), risk tolerance (operations that can lose
   inventory), and time-of-day weighting are all candidates.  Defer
   until basic quality scoring is in place.

4. **Cross-bot plan sharing.**  Plans learned by one bot instance could
   theoretically be exported and consumed by another.  Out of scope until
   single-bot operation is solid.

---

## Why this is the right shape

Because the current code's failure modes all share a structure: **actions
without verified outcomes**.  The cycle detector tries to substitute for
verification using a coarse signature; the dismiss-on-OK fallback substitutes
for verification using a guessed coordinate; hand-authored recipes
substitute for verification by hoping each step works.  Every patch we
apply at the symptom level addresses one specific case of the missing
primitive without filling the gap.

The unifying primitive is **per-step verification of "what was supposed to
happen actually happened"**.  Once that exists, every other concern follows:
- Replan exists *because* verification can fail or be uncertain.
- Cue catalogs exist *because* verification needs evidence-rich predicates.
- Plan metadata exists *because* verifications accumulate into reliability
  measures.
- Capability registries exist *because* multiple plans may serve the same
  goal and verification quality differentiates them.

Patching here and there has stopped converging.  The plan/verify/replan
loop is the structure that makes the failures self-correcting instead.
