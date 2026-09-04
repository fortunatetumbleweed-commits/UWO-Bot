# One Loop: the task drives the state machine

**Status:** design, proposed 2026-08-23
**Origin:** the 2026-08-22 barter runs, where the bot repeatedly undid its own progress.

> **This is the third statement of the same finding.** `docs/architecture_review_perceive_flows_2026-08.md`
> (2026-08-02) reached the verdict — "intended to be perceive → FSM → act, but in practice
> it is not" — and `docs/refactor_plan_perceive_flow_fsm.md` drafted the fix, goal #2:
> "never inner-loop-retries forever". Both are still UNIMPLEMENTED.
>
> What is new here is the **overarching task loop that drives the state machine**. The
> earlier docs asked primitives to stop looping and report upward, but left no single owner
> of the sequence for them to report TO — so the sub-loops had nowhere to go and stayed.
> Naming the task loop, and separating its responsibility from the state machine's, is what
> makes the earlier plan executable rather than aspirational.

## The inversion

Two things are going on when the bot acts:

- **A task** — barter Box of Nutmeg at Melanesian Village. It has a *sequence*:
  plan → gather → barter → take the route and sell.
- **A UI state machine** — port_overworld, harbor, market, sub_menu, sea, village,
  world_map. It has *no sequence*. It only answers "where am I, and what can I do here?"

Today the task is chopped into sub-loops that live **inside** individual actions, and each
sub-loop carries its own private recovery policy. That inverts the hierarchy: a low-level
action ends up deciding the bot's position in the world, without knowing what the bot is
trying to achieve.

Measured in `actions/sail_actions.py` (2026-08-23) — nine loops, of two very different kinds:

| Kind | Examples | Verdict |
|---|---|---|
| Poll for one expected effect | `_dismiss_android_dialog_if_present`, `open_world_map`'s inner "did the map open", `navigate_to_building`'s scroll loops | **Fine.** A primitive waiting for its own effect to settle. |
| Re-perceive, judge, and NAVIGATE | `navigate_to_building`'s attempt loop, `open_world_map`'s outer loop, `depart_from_port_via_world_map`, `_ensure_fleet_ready` | **The problem.** A mini task-runner with its own recovery policy. |

### What the inversion cost, in one day

- `open_world_map` decided `village` was "not port/sea", pressed Back three times, and put
  the fleet **out to sea** — from the village the mission had just spent a voyage reaching.
  The loop had no idea a task wanted to be there.
- `navigate_to_building("harbor")` saw a screen reading *"sailing to Melanesian Village"*,
  judged it "not the harbour", and pressed Back — **cancelling a departure that had
  succeeded**. Four times. 18.5 minutes to achieve what the first attempt had done in 65
  seconds.
- `_read_owned_via_sell` wanted the Sell tab, tapped a label that matched the page title,
  and **left the market it was already standing in**.

In each case the action was locally reasonable and globally wrong, because *locally* is the
only scope it had.

## The target

**One loop. The task runner.** It owns the sequence and it is the only thing that decides
what to do next.

```
        ┌──────────────────────────────────────────────┐
        │  REASONING / GAME KNOWLEDGE                  │
        │  - knows the game's RULES                    │
        │  - "what does this mean, what should we do?" │
        │  - consulted, does not act                   │
        └───────────────▲──────────────────────────────┘
                        │ "OK triggers Auto Supply → safe"
        ┌───────────────┴──────────────────────────────┐
        │  TASK RUNNER  (the only loop)                │
        │  - owns the sequence and the phase           │
        │  - decides the next step from (phase, state) │
        └───────────────┬──────────────────────────────┘
                        │ "I need to be at: market"
                        ▼
        ┌──────────────────────────────────────────────┐
        │  STATE MACHINE  (UI navigation only)         │
        │  - where am I now?                           │
        │  - one move toward the requested state       │
        │  - NO sequence, NO task knowledge            │
        └───────────────┬──────────────────────────────┘
                        │ one action
                        ▼
        ┌──────────────────────────────────────────────┐
        │  PRIMITIVES  (single-shot)                   │
        │  - attempt ONE thing, report what happened   │
        │  - may poll for their OWN effect to settle   │
        │  - never navigate to "fix" a wrong state     │
        └──────────────────────────────────────────────┘
```

Each tick of the one loop:

1. **Perceive.** Where are we? (state) What is blocking us? (obstruction)
2. **Correct, if needed.** If the state is not one the current step can act from, take
   **one** navigation move toward a state it can. If something is blocking — a dialog from
   our own action, a system popup, an unlearned screen — clear it by its own rule.
3. **Re-check the task.** After any correction, ask the task runner what to do next. Do not
   resume a half-finished intention from before the correction.
4. **Act.** One step. Report the outcome. Back to 1.

Step 3 is the part that does not exist today, and it is the whole point: after a correction
the world has changed, so the next move must be re-derived, not remembered.

## Polling is a state, not a loop

A transition in progress ("loading", "the map is opening", "the dialog is animating") is
just another UI state. Perceive moves it forward; the one loop ticks again; the task keeps
being consulted throughout.

This collapses the *legitimate* inner loops as well, so there is no privileged loop left
anywhere — only states and one clock:

```
    for _ in range(10):          →    state = transitioning
        sleep(1.0)                    (loop ticks; task re-consulted each tick)
        if opened(): break
```

The practical gain is that a task can react DURING a transition — abandon a leg, notice
supplies, take an interrupt — which a blocking inner poll makes impossible.

## Where UI knowledge lives: the how / whether split

Knowledge of how the game UI is operated is a genuine gray area. The dividing line:

> **The state machine owns the HOW. The task owns the WHETHER — advised by what the
> reasoning layer knows about the GAME.**
>
> Test: if the answer is the same no matter what the bot is trying to achieve, it belongs
> to the state machine. If it could differ depending on the goal, it is a task decision.

| Thing | Same regardless of goal? | Owner |
|---|---|---|
| Dismiss daily news / promo / event popup | yes | state machine |
| Unlearned chromed screen → tap Home | yes | state machine |
| Scroll a list, find a button, wait out a transition | yes | state machine |
| Accept a dialog that COMMITS the fleet, spends money, or burns a limited daily round | **no** | reasoning + task |

The dialog live on 2026-08-23 is the case that proves the rule:

> *"Moving to Melanesian Village after Auto Supply. Continue? Fleet will immediately set
> sail if Auto Supply is not possible. Sailing can be dangerous with a lack of Food and
> Water."*

A blanket "tap the positive button" answers OK always — and here OK happens to be right,
but not for a reason the state machine could ever reach. The deciding fact is a GAME RULE:
**choosing OK runs Auto Supply, which resupplies the fleet**; the warning applies only when
Auto Supply is impossible (user, 2026-08-23). Knowing that turns an alarming warning into a
safe confirmation.

Note what does NOT settle it. Task context alone ("we carry ~9 days of supply, the voyage may
be longer") argues for Cancel — the wrong answer. The state machine cannot decide it either,
since it does not know a voyage exists. The knowledge needed is neither UI mechanics nor task
state; it is **how the game works**, and it belongs in its own layer:

- **Reasoning / game knowledge** — knows the rules ("OK triggers Auto Supply"). Consulted;
  never acts. Falls back to an LLM over the dialog text when the rule is not yet known, and
  what it learns becomes a recorded rule.
- **Task runner** — supplies the situation (a voyage is in flight, this is its departure) and
  carries out the answer.
- **State machine** — surfaces the dialog and executes the tap. Owns no opinion about it.

So a self-caused dialog crosses all three boundaries:

- the state machine **surfaces** it — "blocked by a dialog, text is X, options [OK, Cancel]"
- the reasoning layer **interprets** it against the game's rules and the task's situation
- the task **carries out** the answer
- the state machine **executes and verifies** the tap

**Every dialog goes through the decider, and the decider has a default: take the positive
option** (user, 2026-08-23). That default belongs in the reasoning layer and nowhere else —
"OK is usually safe in this game" is a fact about the game, not about UI mechanics. The state
machine still REFUSES a dialog when handed no decider (`brain.unexpected.resolve`); the moment
it defaults instead, the decision has silently moved back down and this layer is decorative.

The one thing the default will not do is spend **red gems** — premium currency, real money.
`brain/action_executor.py` already enforces the same discipline on commit buttons via the
cost-icon colour; `brain/game_rules.py` refuses on the dialog's words, and callers holding
detector evidence pass `spends_red_gem=True`. A red-gem dialog stops the loop for a human.

This also gives the LLM consult its correct home: it is the TASK's fallback for "I do not
recognise this decision", not the state machine's way of clearing noise. The existing
`vision/obstruction_consult.py` already keys its cache by `goal`, so the code half-knew
this — the goal was threaded through, but the decision was still taken in the dismissal
layer.

## The desired state, made concrete

`docs/perceive_act_verify_substrate.md` asked every action to declare an expected
post-condition and then compare expected against actual. It was right, and it did not happen,
because "expected" had no value to hold: an action that ends "somewhere" cannot be checked
against anything, so the four unexpected cases had nothing to fire on.

A step that names the states bracketing it supplies exactly that value:

    read_hold      needs_state = market (sell)      expect_state = market (sell)
    sell_surplus   needs_state = port_overworld     expect_state = port_overworld
    sail_to_village                                 expect_state = village

- **`needs_state`** — where the step must RUN FROM. The loop navigates there first, so a step
  never has to move the bot itself.
- **`expect_state`** — where success LEAVES the bot. The next tick compares it against what is
  actually on screen; a mismatch is reported as case 1 (STATE_CHANGED) rather than carried
  silently forward.

This is also what retires the hidden navigator. Live 2026-08-23, `orders_already_held()` —
a function whose signature says `-> bool`, a pure question — walked into the Market three
calls below the plan to answer, and left the fleet there; `sell_surplus` then aborted with
"not at a port". No plan node said "go to the market": the navigation was a side effect of a
predicate, invisible to everything above it. With the states declared, the loop moves the bot
and the reader only reads.

## Rules that follow

- **A primitive never presses Back to make the screen match its expectation.** If the screen
  is not what it wanted, it says so and returns. The screen is ground truth; a stale
  expectation is what needs updating. (See `_screen_says_under_way` for the first instance
  of this rule being applied by hand.)
- **A primitive never walks the fleet out of a settlement.** Position is the task's, not the
  primitive's, to spend.
- **A loop inside a primitive may only wait for that primitive's own effect.** "Did the map
  open?" is legitimate. "Am I somewhere else now?" is the task runner's question.
- **Recovery has one home.** Four private recovery policies cannot be reasoned about, and
  they conflict — one loop's Back undoes another's progress.
- **The state machine is stateless about tasks.** It answers "how do I get from here to
  port_overworld", nothing more. It must never know what the fleet is carrying or why.

## Handling the unexpected (the four cases)

The task runner routes by what perceive found, then **re-checks the task**:

1. **Unexpected but recognised state** → adopt it. Update the expectation to the perceived
   state and re-derive the next step. Never force the screen back.
2. **Dialog caused by our own action** → read its text, decide (LLM when unknown), act —
   usually the positive button. Verify it unblocked us, and record the path as learned.
3. **System popup** (daily news, event, promo) → dismiss by its own rule (close-X / Back).
4. **Unlearned chromed screen, nothing blocking** → tap Home to return to an overworld
   (`actions/screen_exit.exit_current_screen`), then re-derive.

All four end the same way: **get to a good state, then ask the task what is next.**

## Migration

Incremental — the loops can be inverted one at a time, and the phase record already exists.

1. **Stop the bleeding.** Every navigating sub-loop returns instead of forcing:
   `open_world_map`, `navigate_to_building`, `depart_from_port_via_world_map`,
   `_ensure_fleet_ready`. Their callers already handle failure; today they never get the
   chance.
2. **Fix the obstruction gate.** Case 2 cannot work while `classify_obstruction` reports
   `kind=none` on a real modal (see the Notice-dialog trace, 2026-08-23).
3. **Route the four cases** through one handler, called from the one loop.
4. **Give the state machine a single-move API** — `step_toward(state) -> moved | blocked`,
   with the loop owning iteration.
5. **Move the mission graph under the task runner**, so phases (`brain/mission_progress.py`)
   and the loop are the same thing rather than two schedulers.

Steps 1–2 are worth doing on their own merits even if the rest waits: they remove the
behaviour that undoes progress, and they unblock dialog handling.

### Built so far (updated 2026-08-25)

| Step | Where | State |
|---|---|---|
| 1 — primitives report instead of forcing | `actions/sail_actions.py` | **done** for the six world-changers — see the fold table below |
| 2 — the obstruction gate sees modals | `vision/region_detectors/dialog.py` | done — anchors must belong to one widget; the departure Notice reads as `kind=dialog` |
| 3 — one handler for the four cases | `brain/unexpected.py` | done — `look()` classifies, `resolve()` acts; an ACTION_DIALOG without a decider REFUSES rather than guessing |
| 4 — single-move state machine + the loop | `brain/nav_step.py`, `brain/task_loop.py` | done — `step_toward` executes ONE FSM edge; the loop re-asks the task after every correction |
| 5 — the mission graph under the task runner | `brain/barter_task.py`, `brain/game_rules.py` | **flagged** — `UWO_TASK_LOOP=1` drives the barter phase with the loop; the graph remains the default until the loop is proven live |
| 6 — the state machine's dispatcher | `brain/dispatcher.py` | done — `tick()` perceives, resolves an activity, runs it to completion, then takes the result to the task runner and converts the next goal into an intent. A transition happens in exactly ONE place: `dispatch`. |

#### The folds (2026-08-25)

| Function | What it used to do | Now |
|---|---|---|
| `depart_from_port_via_world_map` | walked to the harbour, hunted a Depart button, re-selected the destination 3× — six minutes after the fleet had ARRIVED | **deleted**; `commit_departure` + `tap_supply_departure` tap and report |
| `navigate_to_building` | Back on a sub-menu / wrong building / world map, a learned-recovery plan, a Home-escape — 695 lines, 7 loops | `tap_building_entry` taps once; sub-menu, world map, sea and wrong building all report and return |
| `exit_to_overworld` | escalated into `recover_to_port_overworld`, whose sea branch SAILS THE FLEET home | clears what covers the exit control (bounded at two), then reports |
| `open_world_map` | pressed Back until the fleet left a village it had sailed to | reports a PLACE; still exits a PANEL, which costs nothing |
| `select_world_map_tab` | claimed success without looking | verified by effect; retries the same point with a longer settle |
| `_navigate_world_map_to_village` | polled `_wait_for_state_change` after Move | hands back; the goal owns the arrival verdict |
| `_ensure_fleet_ready` | detected a blocker, climbed a four-rung resolution ladder and re-entered the harbour after EVERY rung, three times over | `read_fleet_readiness` reads; `resolve_fleet_blocker` climbs ONE rung and navigates nowhere; `SailToGoal` owns the retry and the walk back |

`recover_to_port_overworld` now has **no primitive-level callers** — only `task_runner`,
`planner`, `plan_actions` and `explore_actions`, which is where a recovery that may sail
belongs.

Cases 2 and 3 are separated STRUCTURALLY — a popup offering ACTIONS is a decision, one
offering only a close-X is noise — rather than by "did we cause it?", which depends on
bookkeeping the bot does not reliably keep.

Permission to leave a settlement travels on the STEP (`m| 6 — the state machine's dispatcher | `brain/dispatcher.py` | done — `tick()` perceives, resolves an activity, runs it to completion, then takes the result to the task runner and converts the next goal into an intent. A transition happens in exactly ONE place: `dispatch`. |

#### The folds (2026-08-25)

| Function | What it used to do | Now |
|---|---|---|
| `depart_from_port_via_world_map` | walked to the harbour, hunted a Depart button, re-selected the destination 3× — six minutes after the fleet had ARRIVED | **deleted**; `commit_departure` + `tap_supply_departure` tap and report |
| `navigate_to_building` | Back on a sub-menu / wrong building / world map, learned-recovery plan, Home-escape — 695 lines, 7 loops | `tap_building_entry` taps once; sub-menu, world map, sea and wrong building all report and return |
| `exit_to_overworld` | escalated into `recover_to_port_overworld`, whose sea branch SAILS THE FLEET home | clears what covers the exit control (bounded), then reports |
| `open_world_map` | pressed Back until the fleet left a village it had sailed to | reports a PLACE, still exits a PANEL (leaving a panel costs nothing) |
| `select_world_map_tab` | claimed success without looking | verified by effect; retries the same point with a longer settle |
| `_navigate_world_map_to_village` | polled `_wait_for_state_change` after Move | hands back; the goal owns the arrival verdict |
| `_ensure_fleet_ready` | detected a blocker, climbed a four-rung resolution ladder and re-entered the harbour after EVERY rung, three times over | `read_fleet_readiness` reads; `resolve_fleet_blocker` climbs ONE rung and navigates nowhere; `SailToGoal` owns the retry and the walk back |

`recover_to_port_overworld` now has **no primitive-level callers** — only `task_runner`,
`planner`, `plan_actions` and `explore_actions`, which is where a recovery that may sail
belongs.

ay_leave_a_place`), set by the task.
From a village, leaving is the only route to the world map; that it is *allowed* is the
task's call, never the primitive's.

## Related

- `brain/mission_progress.py` — the task's phases; already the single source of "what is
  settled". The one loop should read from it rather than re-deriving.
- `docs/next_phase_architecture_2026-08-09.md` — the self-correcting task executor this
  design is the navigation-layer half of.
- `docs/perceive_act_verify_substrate.md` — expected-vs-actual at the primitive level; this
  document is the same principle one layer up.
- `CLAUDE.md` → "Decisions at the right level — escalate, don't absorb". This design is that
  rule applied to navigation: a primitive that presses Back has absorbed a decision that
  belongs to the task.
