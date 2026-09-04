# Bot architecture — activities, intents, use cases

DRAFT. Merged from the activities/intents and goal-vocabulary sketches; the goal vocabulary
turned out to BE the use-case layer, so it lives here now.

The model follows Android, which fits this game closely: **activities** are worlds you can be
resumed in, **intents** move between them, **use cases** are named operations composed of both,
and a **repository** holds data that outlives any one screen.

---

## Where things are

This doc is long, and grew by argument rather than by outline. The map is by the
QUESTION you arrive with, not by the order the sections happen to sit in.

**Start here**

* [The four layers](#the-four-layers) — the whole model in four lines
* [Activities — the worlds](#activities--the-worlds) — which states are activities, and why

**Who decides what — the layer boundaries**

* [What crosses the boundary, in each direction](#what-crosses-the-boundary-in-each-direction) — state+data up, a goal down; and the test for whether it has leaked
* [Only the dispatcher causes transitions](#only-the-dispatcher-causes-transitions) — the single rule the rest follows from
* [What the dispatcher actually does](#what-the-dispatcher-actually-does) — transitions and actions, and nothing else
* ["Exit the village" is not a goal — it is the dispatcher's business](#exit-the-village-is-not-a-goal--it-is-the-dispatchers-business) — where a task-level ask stops and UI begins
* [A goal is a direction and a stopping condition — not a quantity](#a-goal-is-a-direction-and-a-stopping-condition--not-a-quantity) — why plan numbers are estimates, not contracts
* [The same lens on the other seven nodes](#the-same-lens-on-the-other-seven-nodes) — the split applied across the mission

**How an activity starts and ends**

* [Two ways an activity ends — and only two](#two-ways-an-activity-ends--and-only-two) — finish() vs startActivity(intent) — and why the dispatcher need not care
* [A transition is a switch of activity, however long it takes](#a-transition-is-a-switch-of-activity-however-long-it-takes) — why no caller has wait-for-the-transition logic
* [A lost activity hands back; the dispatcher recovers the bearings](#a-lost-activity-hands-back-the-dispatcher-recovers-the-bearings) — perceive → transition → refresh stale data → ask for a goal
* [State-scoped data: what a world change invalidates](#state-scoped-data-what-a-world-change-invalidates) — data has an OWNER and dies with it; the fleet's owner moves
* [An activity may loop, but must not hold it](#an-activity-may-loop-but-must-not-hold-it) — the bound on what an activity may do
* [How each transition ends](#how-each-transition-ends) — what each intent leaves behind

**The unexpected**

* [Two kinds of unknown, learned separately](#two-kinds-of-unknown-learned-separately) — a new world vs a dialog inside a known one
* [The question has three layers](#the-question-has-three-layers) — game knowledge, where-and-goal, what has been tried
* [The question describes the dialog, and the buttons ARE the answer space](#the-question-describes-the-dialog-and-the-buttons-are-the-answer-space) — the shape of the ask is what makes the answer safe
* [The answer must be an action, not an explanation](#the-answer-must-be-an-action-not-an-explanation) — constrained to something the activity can perform
* [Worked example: "Not Enough Crew", asked rather than taught](#worked-example-not-enough-crew-asked-rather-than-taught) — the case that cost the most, re-done under the model
* [A blocker is not a recovery — it is the next goal](#a-blocker-is-not-a-recovery--it-is-the-next-goal) — crew shortage is a goal, not an exception path
* [An obstruction is not a world](#an-obstruction-is-not-a-world) — popups are cleared while perceiving; the idle lock is NOT one of them
* [The daily news popup: who identifies it, who dismisses it, what follows](#the-daily-news-popup-who-identifies-it-who-dismisses-it-what-follows) — one interruption traced end to end

**Worked examples**

* [Worked example: splitting `barter`](#worked-example-splitting-barter) — the split done for real, one task line and one activity
* [Overflow: a policy, not an escalation](#overflow-a-policy-not-an-escalation) — dump non-barter, keep 6 days of supply, abandon the rest
* [Worked example — barter Birch Tree at Svear, sell at Lisboa](#worked-example--barter-birch-tree-at-svear-sell-at-lisboa) — the mission that proved the split live
* [Worked example — Port Royal <-> London trade run](#worked-example--port-royal---london-trade-run) — the plain trade loop
* [Worked example — discover ports while hugging the shore](#worked-example--discover-ports-while-hugging-the-shore) — a non-trade use case, fitting without changes

**The other pieces**

* [Intents — transitions, carrying a purpose](#intents--transitions-carrying-a-purpose) — what an intent is, and what rides in extras
* [The world map has a FILTER — a lens the intent should choose](#the-world-map-has-a-filter--a-lens-the-intent-should-choose) — the lens belongs to the intent, not the screen
* [Use cases — what the task runner asks for](#use-cases--what-the-task-runner-asks-for) — the catalogue and why preconditions make them resumable
* [Dialogs](#dialogs) — the model perception already extracts
* [The repository — and why the screen outranks it](#the-repository--and-why-the-screen-outranks-it) — cached belief loses to what is on the screen
* [Sketch — battle (NOT yet built)](#sketch--battle-not-yet-built) — recorded, not designed

**What is not true yet**

* [Where today's code breaks the model](#where-todays-code-breaks-the-model) — the violations, named
* [Still open](#still-open) — questions with no answer yet, including the missing strategic layer
* [Order of work](#order-of-work) — what to do, in order

**Taught in more than one place.** Three topics are arguments rather than sections, so they
appear wherever they bite:

* *the idle standby lock* — a genuine state, not an obstruction
  ([An obstruction is not a world](#an-obstruction-is-not-a-world)), whose only exit is a finish
  ([Two ways an activity ends — and only two](#two-ways-an-activity-ends--and-only-two)), and
  whose first fix was the per-path habit in a new costume (same section, *The mistake this
  corrects*).
* *no wait-for-the-transition logic anywhere* — stated at
  [A transition is a switch of activity](#a-transition-is-a-switch-of-activity-however-long-it-takes)
  and relied on at every transition below it.
* *the screen outranks the belief* — [the repository](#the-repository--and-why-the-screen-outranks-it),
  and the refresh step in [a lost activity hands back](#a-lost-activity-hands-back-the-dispatcher-recovers-the-bearings).

---

## The four layers

    task runner    picks the next USE CASE from data and state. Knows no actions, no screens.
    use case       precondition -> work -> result. Re-entrant and resumable.
    dispatcher     perceives which activity is resumed; routes work to it; dispatches intents.
    activity       a world. Bounded work. Yields.

The cycle: **the dispatcher perceives, routes work to the resumed activity or dispatches an
intent, and reports the result up; the task runner chooses the next use case.**

---

## Activities — the worlds

These are the states perception already returns:

| activity | work that belongs here | intents out |
|---|---|---|
| `port_overworld` | read the building list, read the fleet, choose where to go | ENTER_BUILDING, OPEN_WORLD_MAP |
| `building:market` | read prices and stock, **buy loop**, **sell loop** | EXIT, OPEN_SUBMENU |
| `building:harbour` | read supply and capacity, Supply Departure | DEPART, EXIT |
| `building:*` | whatever that building is for | EXIT |
| `sub_menu` | one screen deeper inside a building | BACK (title arrow) |
| `world_map` | find a port or village, read the trade-event schedule, commit a destination | COMMIT_DESTINATION, OPEN_VILLAGE_PANEL, CLOSE |
| `sea` | watch speed and ETA, handle sea events | OPEN_WORLD_MAP |
| `village` | read the trade list, **barter rounds** | LEAVE |

The bolded work is where a loop is legitimate: it stays inside one activity and drives to a
measurable target.

**`loading` is not an activity.** It is the transition itself — nothing is resumed, no work is
possible, and the only correct response is to wait and perceive again. Treating it as a state
to reason from is what made the bot forget where it was (2026-08-25). Nor is `unknown`.

**`sea` IS an activity — and it is the busiest one.** The temptation is to treat it as a
corridor between ports, because a voyage looks like waiting. It is not: the fleet lives there
for days and there is more to do at sea than in most buildings. Everything below is work that
happens while `sea` is resumed, none of it leaving the activity:

* **sail** toward a destination — watch speed and ETA, notice the speed-0 bug;
* **hug the coast** under the hugging rule, a continuous steering loop;
* **read the nearby PORTS list**, monitored across ticks, to spot `???` harbours coming into
  range as the coast is followed;
* **read the nearby FLEETS list** — the same panel, a different tab — for battle targets;
* **switch that panel's tab**, which decides which of those two lists is even visible;
* **steer manually by a minimap bearing** when a target is visible there but not yet in a list;
* **chase a fleet** once tapped, and answer the battle confirmation the chase raises;
* handle sea events and interruptions.

Four surfaces carry information at different ranges — world map marker, minimap, fleet list,
battle confirmation — and the bot hands off between them without ever leaving `sea`.

Arrival is therefore not an intent to dispatch; it is the activity ENDING, which the dispatcher
notices by perceiving `port_overworld`. That is exactly what the six-minute departure loop got
wrong: it treated the sea as a gap to be waited out rather than a place the fleet was living
in, so when the fleet arrived nothing was watching.

### Only the dispatcher causes transitions

The core of the state machine is that a **central dispatcher drives it**. Transitions happen in
exactly one place: when the dispatcher dispatches an intent. Nothing else moves the bot between
worlds — no sub-loop, no primitive, no recovery call buried in an action.

That gives every activity a simple contract, and one escape hatch:

    the dispatcher perceives  ->  routes work to the resumed activity
    the activity does LOCALIZED work and returns
    if the activity does not RECOGNISE what it sees, it FINISHES
      -> control returns to the dispatcher, which perceives globally and decides

**Localized perceive is fine, and necessary.** Inside the market the activity reads prices,
finds the Purchase button, checks the hold. That is perception scoped to a screen it already
knows it is on, and it does not cross anything.

**An activity that gets confused does not recover — it ENDS.** This is the rule that removes
the recovery calls scattered through the action layer. `_depart_from_harbour` used to call
`recover_to_port_overworld` when a blocker appeared; `exit_to_overworld` escalated into it when
back presses stopped helping; the two could re-enter each other. Under this contract none of
that belongs to an activity: an unrecognised screen is reported, and the dispatcher — which can
see the whole state and the current goal — decides whether that means recover, re-plan, or
give up.

So there are only two kinds of failure an activity can have:

* **"I could not do the work"** — reported with a reason, the activity still knows where it is;
* **"I do not recognise this screen"** — the activity ends immediately and hands back.

Neither is recovered from in place, and neither moves the bot.

### What the dispatcher actually does

It is the STATE MACHINE's dispatcher, and it owns two things: **state transitions** and
**actions**. The task runner sits above it and owns neither. The dispatcher does not "do the
task" — it **starts activities, regains control when one finishes, hands the result up, and
converts the goal it gets back into the next intent.**

    activity finishes
      -> dispatcher regains control
      -> dispatcher sends the RESULT to the task runner
      -> task runner decides the next GOAL
      -> dispatcher converts that goal into an INTENT
      -> the intent starts the next activity

Worked through the gather leg (user, 2026-08-25):

    market activity: buys until the Candle order is met, then FINISHES
      -> dispatcher takes control, collects the result
      -> task runner: "that leg is done; now get Iron, and Amsterdam has it"
      -> dispatcher converts that to an intent: sail to Amsterdam
      -> sea activity runs, arrives, FINISHES
      -> dispatcher reports the arrival
      -> task runner: "now buy 500 Iron"
      -> dispatcher converts that to an intent: enter the market
      -> market activity runs the buy loop ...

So the **granularity of the task runner's involvement is the activity boundary**. It is not
consulted every tick, and it is never consulted mid-activity — which is what keeps it free of
screens and taps while still letting it change its mind at every point where changing it is
possible.

Two things this settles:

* **The dispatcher holds the current goal while an activity runs**, and asks for a new one when
  the activity ends. It does not need to know when a use case is satisfied — the task runner
  tells it what to do next, and "nothing" is a valid answer.
* **An activity runs its bounded work to completion**, then hands back. The market does not
  return "still buying" to be re-entered each tick; it buys until the order is met or it
  cannot continue. That is consistent with the loop rule, because the loop is bounded and
  goal-directed and therefore terminates.

### An obstruction is not a world

> **Corrected 2026-08-26.** This section originally listed the idle standby lock alongside the
> daily news and promo banners. That was wrong, and the distinction matters: see *Two kinds of
> unknown* above. The lock replaces the **entire** screen — nothing of the previous world
> survives — so it is a genuine STATE with its own exit action and an unknown destination. Only
> the popups belong here, because they leave the world underneath intact.

The daily news, a promo banner, an announcement: each is a film laid **over** a world the bot
is still in. None is somewhere work can happen, and none of them moved the bot. So none is an
activity, and none may be dispatched on.

They are cleared by the **dispatcher, as part of perceiving** — before an activity is
resolved. Three reasons they cannot belong to an activity:

* they can appear over ANY world, so giving them to one activity means giving them to every
  activity — the "one canonical implementation per concern" rule;
* clearing one changes the screen, and screen-changing actions are the dispatcher's;
* an activity's contract is localized perception *on a screen it already knows it is on*, and
  an obstruction is exactly the case where it does not know.

Prevention is not enough on its own, because an obstruction can arrive **mid-activity**. That
is the backstop case, and the lost-activity rule above already covers it: the activity returns
UNRECOGNISED at once, and `_regain_bearings` clears the obstruction in its transition step.

#### What it cost to have neither (2026-08-26)

At Svear Village, with 445 Iron, 146 Matchlock Gun and 438 Candle aboard and the barter one
tap away, the game dropped into "Slide up to unlock" while the panel was opening. Perception
named it correctly on **every single look**:

    nav_state='learned_on_standby_at_sea_slide_up_to_unlock': 1/1 signals matched
    qwen: "The player is on 'On Standby at Sea' with 'Slide up to unlock' visible."

Nothing acted on the name. The mission re-perceived a screen it could not act through for 16
minutes, failed, retried, re-perceived for another 17, and aborted — at the destination it had
sailed an hour to reach.

Two lessons, and the second is the general one:

* **Re-perceiving cannot clear an obstruction**, for the same reason it cannot fix being lost:
  looking again tells you what is on the screen, it does not change what is on the screen.
* **A classifier that names a screen nobody handles is worse than one that fails.** The
  knowledge was there, correct, and repeated dozens of times. Because it was never wired to an
  action, the logs read healthy while the bot did nothing for half an hour. Recognition without
  a handler is not partial progress; it is a silent stall that looks like work.

Note also what that screen SAID: "On Standby at Sea", server "Atlantic Ocean" — while the fleet
stood in a village. Routing an obstruction by its wording would have handed a village problem
to the sea activity. Obstructions are identified by what they ARE, never by what they say about
where you are.

## Two kinds of unknown, learned separately

The bot meets things it does not recognise, and until now it filed all of them in one place —
`memory/knowledge/learned_fingerprints/`. That store turned out to be a mixed bag (audited
2026-08-26): two idle locks, two promo/announcement popups, two dialogs, two duplicates of
`port_overworld`, one duplicate of `loading`, and one whose name is a truncated Claude
sentence. Nothing in it is routable, because the FSM never gains a node for any of it.

The task/UI separation makes the right split obvious. There are **two** kinds of unknown, they
live at different layers, and they must be learned into different stores.

### State-level unknown — a whole-screen world change

The idle lock is the type case. It replaces the entire UI: the port is gone, the sea is gone,
there is nothing of the previous world left on screen. That is a **state**, and it should be a
first-class one with an edge in the FSM, not something swept aside during perception.

It has two properties worth stating separately, because they are usually confused:

* **Its exit action is known.** The screen says so: *Slide up to unlock*. There is exactly one
  way out and the screen names it.
* **Its destination is NOT known.** The swipe returns you to wherever you were, and the bot
  must not assume that. It may *remember* a previous state, but a remembered state is a belief,
  and beliefs are what go stale.

So: dispatch the known action, then **re-perceive to discover where it landed**. Not as a
safety net — as the only way the destination is ever established.

And note that this needs no machinery of its own. Re-perceive, then consult the task runner,
are the two **general** steps after every activity finishes (user, 2026-08-26) — so the lock is
handled by the ordinary loop and nothing that might encounter it has to learn what it is. The
barter code never gains a line about locks. Proposing to "wire the wake into the barter path"
was the per-path habit this architecture exists to delete: a fix that works once, in one place,
and leaves the next path to rediscover the same problem.

**One state, not two.** The lock appears at sea and in port, and it is tempting to make those
separate states, or one state parameterised by where it happened. Both are wrong, and the log
of 2026-08-26 says why: the screen read *"On Standby at Sea"* with the server *"Atlantic
Ocean"* while the fleet was tied up in **Svear Village**. The wording describes the standby
mode, not the fleet's position, so routing on it would have sent a village problem to the sea.
The exit action is identical in every case and the destination is unknown in every case —
there is nothing left for a parameter to carry.

### Activity-level unknown — a dialog inside a world the bot knows

A dialog that appears in the market is a different animal. The bot has not left the market. It
knows where it is, what it was doing, and what it has already done — it is at Amsterdam, at the
buying stage, having staged 500 Iron and tapped Purchase. Only this one dialog is unrecognised,
and every clue needed to handle it is in the dialog's own text.

That is not a new state. It is a **gap in one activity's repertoire**, and the activity is the
only layer holding the context that makes it answerable.

Which is what makes a targeted question possible:

> I am at the Amsterdam market, trying to buy 500 Iron. I have opened the Purchase tab, staged
> 500 units and tapped Purchase. A dialog has appeared saying "…". What should I do?

Compare that with what the bot can ask when it treats the same dialog as an unknown *state*:
"here is a screen, what is it?" — which is why the learned store filled up with descriptions
instead of actions. The context is not decoration; it is the difference between an answer that
can be executed and an answer that can only be filed.

The answer is learned **for that activity**, keyed by the activity and the stage within it —
market/buying, harbor/departure — and it never becomes a state. Two dialogs with similar
wording in the market and the shipyard are different problems with different right answers.

### The question has three layers

An activity-level answer is only as good as the situation handed to the model. Three layers,
from three different sources, with three different lifetimes:

**1. Game knowledge — the manual.** Stable, shared, true regardless of what the bot is doing:
trade goods are bought and sold at markets to earn ducats; rewards are collected, not bought.
It also covers what a given screen can TELL you — the market UI carries items reporting the
fleet's load capacity, and shows language effects, since a mate with a high local-language
level earns benefits at that port.

That last kind matters most here. A dialog is unfamiliar precisely because the bot has no rule
for it, so the only way to reason about it rather than describe it is to know what this screen
deals in. A model that knows the market UI reports load capacity can recognise a
hold-is-full dialog for what it is; one that does not can only report that a dialog appeared.

It already exists — `docs/game_mechanics.md`, `docs/trade_system.md`,
`docs/combat_system.md`, `docs/exploration_and_investment.md` — and today it is referenced only
in code COMMENTS. Nothing loads it into a prompt. The manual was written for humans and the bot
has never been allowed to read it. Load the section relevant to the activity, not the lot: a
market dialog needs the trade system, not the combat system.

**2. Activity context — where I am and what I am doing.** The activity, the place, and the
goal in progress: *the market at Amsterdam, buying Iron toward 500.*

**3. Progress — what I have already done this attempt.** *Opened Purchase, staged and bought
300, going back for the rest.* This is what separates "a dialog appeared" from "a dialog
appeared after my second purchase of the same good", and those have different right answers.

Together:

> This is Uncharted Waters Origin. Trade goods are bought and sold at markets for ducats;
> a mate's local-language level affects what a port yields; …
>
> I am in the **market at Amsterdam**, buying **Iron toward 500**. I have opened the Purchase
> tab, staged and bought **300**, and tapped Purchase again for the remainder. A dialog has
> appeared reading: "…".
>
> Which of these may I do: tap a named button, close the dialog, abandon the purchase, report
> blocked?

Contrast the question the old learned-fingerprint path could ask — *"here is a screen, what is
it?"* — which can only produce a description. That is precisely what those ten files contain.

### The answer must be an action, not an explanation

Constrain the reply to actions the activity can actually perform — a named button on this
screen, close, abandon, report blocked — as `docs/reasoning_fallback_layer_design.md` argues
for the whole-state case. An unconstrained answer can be perfectly correct and still
unexecutable, and an unexecutable answer gets filed rather than run, which is how a store of
descriptions accumulates.

### What gets stored, and under what key

Keyed by **activity + stage**, with the dialog's signature discriminating within that key:

    market/buying + <dialog signature>  ->  the action that worked

Not by dialog text alone. Near-identical wording in the market and the shipyard are different
problems with different right answers, and keying on text is how the mixed bag gets rebuilt in
a new location. The key is the situation the bot can recognise itself in again — which is the
same reason the question carries the situation in the first place.

Store the action, the situation it resolved, and the outcome. Next time that key matches,
replay it without asking; when a replay fails, the situation has changed and it is worth
asking again.

#### Worked example: "Not Enough Crew", asked rather than taught

This was, in the user's words, a monumental struggle to teach. It is worth setting the old
flow beside the new one, because the difference is not incremental.

**What it does today.** `brain/human_escalation.escalate` — one function in a 1457-line module:

1. try `learned_recoveries.json`; replay the macro if the keywords match;
2. otherwise prompt a HUMAN, with a ten-minute wall-clock timeout that aborts the task if
   nobody is at the keyboard;
3. if someone answers, enter a teaching loop with no timeout — parse input, ask Claude,
   execute, re-perceive, prompt again — and on "done" save the accumulated steps as ONE
   learned recovery.

The endpoint of all that is a recorded sequence of taps, replayed later by bag-of-words match.
That is where `recruit_crew_confirmation_dialog` — tap OK on a 181,224-ducat spend at fixed
(1302,812) — came from. It is a saved teaching session, doing its best.

**What it becomes.** Not enough crew is a BLOCKER on the transition to sailing, so the bot is
simply stopped at the harbour with a goal it cannot meet (user, 2026-08-26):

    activity  harbor: I was setting sail. There is no active Supply/Depart button.
              Here is what is on the screen.
    ask       -> "you need crew; tap Recruit Crew"
    act       tap it; the tick re-perceives
    activity  a confirmation dialog I do not know, in the middle of recruiting crew,
              having tapped Recruit Crew. Here is what it says.
    ask       -> "tap OK"
    act       tap it; the tick re-perceives; the departure is no longer blocked

Four things fall away.

* **The human is no longer the primary path.** The question carries the goal, the place and
  the steps already taken, which is what makes it answerable by a model rather than only by
  someone who knows the game.
* **The ten-minute timeout that killed the task goes** with it.
* **No macro is recorded.** What is learned is keyed to activity and stage — harbor/departure
  plus this dialog — not to a coordinate on a particular day's screen.
* **The teaching subloop disappears.** parse -> execute -> re-perceive -> prompt IS a subloop,
  the exact shape this architecture removes. Each answer is now ONE action, and the
  dispatcher's ordinary tick does the re-perceiving.

**The ask is iterative, and that is the point.** Each answer leads to a new screen, which is a
new question carrying more progress. The bot does not need the whole recipe up front; it needs
the next action, repeatedly — which is also why the answer must be constrained to something
the activity can perform (a named button on this screen), never free text to be interpreted.

Auto-confirming the recruitment spend is within policy — everything but red gems — but the
difference matters: it is confirmed because a model was told the bot is mid-recruitment and
asked what to do about THIS dialog, not because a coordinate recorded last week matched some
keywords.

#### The question describes the dialog, and the buttons ARE the answer space

The best form of the ask (user, 2026-08-26): *I see a dialog, it has this title, this body
text, an OK button and a Cancel button — what should I do?*

Enumerating the buttons is what makes the answer safe, and it does it by the shape of the
question rather than by filtering afterwards. A model told "there is [Cancel] and [OK]" answers
with one of them. Nothing has to parse prose, guess which OK was meant, or decide whether an
instruction is executable — the affordances were the offer.

    This is Uncharted Waters Origin. Trade goods are bought and sold at markets for
    ducats; a fleet cannot set sail without enough crew, and crew are hired at the
    harbour or the inn.

    I am in the HARBOR at Lisboa. My goal is to set sail for Amsterdam.
    The Depart button was inactive, so I tapped Recruit Crew.

    A dialog has appeared:
      title:   "Notice"
      body:    "Recruit Crew? 181,224 ducats will be spent."
      buttons: [Cancel] [OK]

    What should I do?

Three layers, as above: game knowledge, where-and-what-goal, and what has been done so far.
The dialog itself is the fourth thing — the situation being asked about.

**The structure is already extracted.** `vision.region_detectors.DialogModel` carries
`title_bar.text`, `body_text` and `actions` — each a label, a bbox and `is_positive` — and
`kind()` already tells confirmation from informational from reward. Live 2026-08-26 the
obstruction layer logged exactly this, and used it only to classify:

    obstruction detected: kind='dialog' bbox=(726,0,1673,844)
      signals=["anchors=['actions']", 'close_button=False', "actions=['Cancel','Ok']"]

So the question needs no new perception. It needs the fields that are already read to be
written into a sentence instead of a log line — and the chosen label maps straight back to a
bbox to tap, because `DialogAction` carries both.

**No screenshot.** A structured description beats an image here: it is what the model can
reason about, it is stable under a re-skin, and it can be stored as the key for what was
learned. Asking about a picture produces a description of a picture, which is how a store of
descriptions accumulated in the first place.

### What separates them

| | State-level | Activity-level |
|---|---|---|
| What changed | the whole screen — a different world | a dialog, over a world the bot still knows |
| Who notices | the dispatcher, while perceiving | the activity, mid-operation |
| What is known | the exit action; **not** the destination | the location, the goal, the steps already taken |
| What is asked | nothing — the action is on the screen | a targeted question carrying that context |
| Where it is learned | as a state, with an edge | as an activity's handler, keyed by stage |
| After acting | re-perceive; the destination was never known | continue the operation; the world did not change |

The test for which one you have: **is the world underneath still there?** A promo banner over
the port overworld leaves the port underneath — activity level. The idle lock leaves nothing —
state level.

### Why the old store mixed them

The learned-fingerprint mechanism was an attempt to let the bot learn what to do instead of
hard-coding every case — the right instinct, applied before there was a structure to hang it
on. With no separation between task and UI, every unrecognised screen looked alike, so
everything became a "state", and a state with no edges is unreachable by construction. The
record for the idle lock holds a Claude paragraph correctly identifying it as
*"the standby/idle lock screen that appears when the game is left unattended"* — the knowledge
was there, in English, wired to nothing.

That is the general lesson: **a name without a handler is not partial progress.** It makes the
logs read healthy while nothing happens.

## The daily news popup: who identifies it, who dismisses it, what follows

Worked through 2026-08-26 after the popup stalled a run at Stockholm.

### What it is

An **obstruction**, by the test already in this document: the world underneath is still there.
The lock's own prompt, "Slide up to unlock", was visible at the bottom of the screen the whole
time the popup was up. So it is not a state, it gets no activity, and nothing is ever
dispatched on it.

But this case has a shape the earlier section did not cover: **an obstruction laid over a
STATE that itself needs an action**. The lock is a state whose activity is one gesture, and
the popup sat on top of it. Measured that day:

    tap the popup's close-X   ->  still idle_lock      (the popup was covering it)
    swipe up                  ->  port_overworld, Stockholm

Two steps, in that order, and neither alone was enough. The unlock swipe had been failing for
minutes — centre column and left column both — because the gesture never reached the lock.

### Who identifies it

The **dispatcher, while perceiving**, before any activity is resolved — the same place every
obstruction is handled, for the same reason: it can appear over any world.

Identified by **what it is**: its own panel and its own close-X. NOT by a string found
somewhere on screen. That distinction is not theoretical here — `looks_like_lock_screen`
matched this frame and reported `{'cleared': True, 'kind': 'lock'}`, because the phrase "slide
up to unlock" was present. It was present because it belongs to the screen UNDERNEATH. The
handler named the thing it was covering instead of the thing that was covering it, then did a
wake-and-swipe that changed nothing and reported success.

**When several are stacked, the topmost is the one to identify.** A handler that matches on
text has no way to know which layer the text came from.

### Who dismisses it

The dispatcher, through the one obstruction handler (`brain/unexpected.py` look/resolve).

It is **dismissed, never completed** — the bot did not ask for it. Per CLAUDE.md's dialog
rule, that means its own close-X or Back, never a positive button, and never the screen corner
(which is the hamburger on an overworld).

And the dismissal must be **verified**: did the screen actually change? Reporting `cleared:
True` without looking is what turned a five-second problem into a stalled run, because every
layer above believed the obstruction was gone.

### What follows

**Re-perceive, and go round the loop again. Nothing else.**

Clearing an obstruction does not put the bot anywhere useful — it reveals what was underneath,
which may be another obstruction, or a state that still needs its own action. Here it revealed
the lock, which then needed its gesture. A handler that clears a popup and then resumes the
interrupted plan is assuming a position it has not looked at.

This is the same rule as everywhere else in this document: after acting, look. The dispatcher
already re-perceives after every activity, so the obstruction case needs no special machinery —
only that clearing counts as "something happened", and the next tick starts from a fresh look.

### The loop, spelled out for this frame

    perceive        -> daily news popup over something
    clear it        -> tap ITS close-X; verify the screen changed
    perceive        -> idle_lock (what was underneath)
    resolve         -> the idle-lock activity: swipe up, FINISH
    perceive        -> port_overworld, Stockholm
    ask the task    -> "sail to Lisboa"

No step assumes the next. Each one looks.

### Where this lives: perceive's own first pass

Perception does it, and it already has the slot — `brain.perceive._perceive_uncached` opens
with `frame = dismiss_interruptors(frame)` before it classifies anything. The contract for
that pass (user, 2026-08-26):

    identify the interruptors  ->  clear them  ->  RE-PERCEIVE  ->  classify the state

and the state that comes out of the re-perceive is the one the bot transitions to. Not the one
it expected, not the one it held before the interruption — the screen is the truth and the
belief is what updates.

This is why no caller needs to know the popup existed. `perceive` returns `idle_lock`, or
`port_overworld / Stockholm`, or whatever is genuinely there once the covering is gone, and
the dispatcher resolves an activity for THAT. The obstruction never becomes a state, never
reaches an activity, and never appears in anyone's control flow.

It also explains why the interruptor pass has to re-perceive rather than classify the frame it
already holds: that frame is the one WITH the popup in it. Classifying it after dismissing
something is reading a photograph of a screen that no longer exists.

### What this changes in the code (not yet done)

* obstruction identity comes from the obstruction's own elements, not from a frame-wide text
  match — so a popup over a lock is read as a popup, not as a lock;
* `clear_blockers` verifies the screen changed before reporting `cleared`;
* stacked obstructions are handled topmost-first, one per tick, each followed by a fresh look.

### A lost activity hands back; the dispatcher recovers the bearings

An activity that cannot recognise the screen is **lost**, and being lost is not something it
can fix: re-perceiving in place tells you where you are, it cannot change where you are. So it
ends immediately and hands control back, and the dispatcher — the only layer that may cause a
transition — does the recovery, in this order (user, 2026-08-26):

    activity: "I do not recognise this" -> UNRECOGNISED, ends
      -> dispatcher PERCEIVES globally
      -> dispatcher TRANSITIONS to a state work can happen in
      -> dispatcher REFRESHES the data the lost episode made stale or wrong
      -> dispatcher asks the task runner for the next goal

The third step is the one that is easy to leave out. Getting lost usually means a belief was
already wrong — the bot thought it was in the market and it was not — so whatever was cached on
that belief is now suspect. The screen is the truth and the belief is what updates
(`memory/lost-means-reorient-not-reread`, `memory/verify-a-root-cause-visually`). Recovering
position without refreshing what was believed just re-enters the same mistake from a tidier
starting point.

Note what is NOT in that sequence: the task runner is asked LAST, and asked only for a goal. It
is not told to recover, and it is not consulted about how. Recovery is a transition, and
transitions are the dispatcher's.

## State-scoped data: what a world change invalidates

> **The case that forced this (2026-08-26, live).** The fleet departed Seville for Barcelona.
> The departure WORKED — the auto-supply notice was confirmed, and the next tick perceived
> `sea`. But `commit_departure` verified itself by asking *"can I still select Barcelona on
> the map?"*, and after a successful departure that panel is gone. It reported failure. The
> goal, still holding `could not depart`, re-opened the world map **mid-voyage**, re-targeted
> the port it had just left, sailed back to Seville, bought nothing, and started again.
>
> The user's observation is the whole design: **once the fleet is at sea, "unable to depart"
> cannot be true.** Nothing needed to re-check it. The world had already answered.

### Data belongs to a world, and dies with it

Every remembered thing has a WORLD it is about. "The departure is blocked" is about being in
a port. "The Exchange button is grey" is about a barter panel. "The shelf is sold out" is
about a market. None of them means anything anywhere else, and none of them should survive
being somewhere else.

So the rule is not "expire after N seconds" and not "re-read when in doubt". It is:

    when the perceived world changes, everything scoped to the world just left is DROPPED

Not marked stale, not lowered in confidence — dropped. A belief that outlives its world is
worse than no belief, because it answers a question nobody re-asked.

### Worlds nest, so invalidation cascades

The second half of the user's point, and the part that makes this more than a flag: **the
buildings belong to the port world.** A market is not a peer of the port it stands in; it is
inside it. Which means leaving the port must drop everything about every building in it —
the market's shelves, the harbour's departure panel, the shipyard's prices — without anyone
listing them.

    at sea / world map          (no port scope at all)
      port_overworld            PORT      — this port, its buildings, its prices
        building:market         BUILDING  — shelves, tabs, what is in the basket
          sub_menu:purchase     SCREEN    — the open dialog, the selected tile
      village                   PLACE     — amity, today's rounds
        sub_menu:barter         SCREEN    — selected good, materials, Exchange state

Dropping a scope drops everything under it. Sailing away invalidates the port, and therefore
the market, and therefore the tile that was selected in it. That cascade is the reason to
model containment rather than keep a flat set of flags: a flat set has to be remembered to be
cleared, and the ones that get forgotten are exactly the ones that cause this class of bug.

### Who OWNS the datum decides when it dies

> **Corrected 2026-08-26.** This section first described five nested SCOPES, keyed to where
> the bot was standing. That is not quite it, and the difference matters: the levels are
> **owners**, and an owner's data lives exactly as long as the owner is in play. Some owners
> nest with place (a market is inside a port). One of them MOVES WITH YOU, which no purely
> spatial model can express (user, 2026-08-26).

    COMPANY   ducats, gems, the mission and its plan          always valid
    FLEET     cargo, capacity, supply, crew, ship life        valid in EVERY world — it moves
    PLACE     this port or village: its buildings, prices,    dies on leaving
              amity, today's remaining rounds
    BUILDING  what this market stocks, which tab is open      dies on leaving the building
    PANEL     the cart, the selected tile, the open dialog    dies when the panel closes

The question to ask of any remembered value is **not** "where did I read it?" but **"who does
it belong to?"** Those give different answers, and only the second one predicts when it goes
bad.

**The cart is the example that makes it obvious.** The market's right panel shows the cart and
the hold TOGETHER — active tiles are staged for purchase, greyed tiles are cargo already owned
(`memory/market-right-panel-is-cart-and-cargo`). One panel, read in one glance, and the two
halves have completely different lifetimes:

* the **cart** belongs to the market session. Walk out and it is gone — not stale, GONE, and
  a cart remembered across a departure is an instruction to buy things nobody chose.
* the **cargo** belongs to the fleet. It sails with the ship, and it is still true at Svear.

So "where I read it" cannot be the key. Both were read from the same rectangle.

**The fleet is the owner that breaks containment.** Cargo, capacity, remaining supply, crew,
ship life — these are valid at a port, in a market, at sea and at a village, because the fleet
is present in all of them. Nothing about sailing makes them stale. That is why a purely
place-based scope model gets this wrong: it would drop the hold's contents on departure, which
is exactly the fact the next leg needs.

**Which means there are two ways data goes bad, not one:**

    LEAVING     kills PLACE / BUILDING / PANEL data.        Automatic, on transition.
    ACTING      kills whatever the action changed.          Buying changes the hold; bartering
                                                            changes the hold AND the amity AND
                                                            today's rounds.

The fleet's data is immune to the first and fully exposed to the second. A port's prices are
exposed to both — and, awkwardly, to neither, since shelves restock on a timer while nobody
is looking. That is what makes PLACE data a cache to re-read cheaply and never a record to
trust.

**And it explains the task runner's position.** The mission is COMPANY-owned — it outlives
every port, every building and every voyage — which is precisely why it must never hold
anything from a shorter-lived owner. A task runner that remembers which tab was open has
taken custody of something that dies without telling it. The layering rule and the lifetime
rule turn out to be the same rule seen from two sides.

### The sharper rule: do not store a conclusion

Scoping makes a stale belief die on time. But `could not depart` should never have been
stored, because it is not an observation — it is a CONCLUSION drawn from one, and the
observation it came from ("no destination panel") had a second explanation nobody considered:
the panel is also absent when the departure succeeded.

    an OBSERVATION can be checked by looking      -> cache it, scope it
    a CONCLUSION cannot                           -> recompute it, never store it

Had that rule been in force, the bug is impossible: the goal would have asked "where am I?"
on the next tick, seen `sea`, and been done. Which is exactly what the architecture already
says about transitions — the next tick perceives, and no caller carries a verdict across one.

**Where this leaves `commit_departure`.** Its success test asks a question that can only be
answered before it succeeds. The fix is not a better test: it is no test. Tap, return, and let
the dispatcher perceive whether the world changed — the rule already written down under *a
transition is a switch of activity, however long it takes*.

### Where the invalidation happens

The dispatcher, and only there. It is the one layer that sees BOTH the world of the previous
tick and the world of this one, so it is the only place that can know a boundary was crossed.
It already refreshes stale data when an activity gets lost; this is the same act, promoted
from an error path to a rule that runs on every transition — which is the honest version,
because getting lost is not the only way a belief goes bad. Sailing away is the ordinary way.

### What this does NOT change

Which activity serves which goal is unaffected. "Depart" is served in a port and "select a
destination" on the world map, reachable from a port or from the sea — that is already what
SERVES describes, and a goal spanning two worlds is just a transition away from one of them.
The scope question is about DATA, not about who does the work.

### A goal is a direction and a stopping condition — not a quantity

The plan's numbers are an **estimate**, formed from the ratio at the moment of the remote
check. Between that check and the fleet's arrival the amity moves, the stock re-rolls, the
day's rounds tick down. So the numbers shape the ROUTE — which ports to visit, roughly how
much to buy — and they do not gate execution (user, 2026-08-26).

At the village the goal is: **barter until you cannot.**

Not "obtain 896 Birch Tree". Today's run shows why that would have been wrong twice over, in
opposite directions:

* The plan said 2 rounds. Bartering raised amity Neutral -> Favorable, which improved the
  ratio from 105/53/105 to **80/40/80**, and a third round became fundable — a round the plan
  could not have known about, because the thing that created it was the bartering itself.
* Then the material arithmetic said **74 more rounds** were fundable, while the day's rounds
  were spent and the Exchange button was grey.

A quantity goal stops short in the first case and loops forever in the second. A stopping
condition read from the world is right in both, which is the same rule as *the game's answer
outranks the maths in both directions* — stated at the goal layer instead of inside one
executor.

#### What that makes a goal look like

    barter Birch Tree at Svear Village    until the village refuses
    gather Iron at Amsterdam (~242)       until the hold covers it, or the shelf is empty
    sail to Lisboa                        until arrival
    sell the hold at Lisboa               until nothing profitable is left

The quantity in the gather is in brackets on purpose: it is what the PLAN thought, carried
along so the buy knows roughly what to ask for. It is not a test anyone fails. Live 2026-08-26
the plan wanted 242 Iron and a bulk tap bought 445 — no one should call that a failure, and
nothing did.

#### The corollary: a shortfall is information, not a fault

When the panel funds fewer rounds than planned, that is the estimate ageing exactly as
expected. It is worth SAYING — the mission logged "planned 2 round(s), the hold funds 1,
limited by 'Firearms', short 13" — because it tells the operator the ratio moved. It is not a
failure, and the mission carried on and did the round it could.

The same goes for the reverse. When the panel funds MORE than planned, take them: the
bartering stays in its phase while a full round is still fundable, rather than sailing away on
the strength of a number computed before the amity changed.

### Worked example: splitting `barter`

Today `brain/barter_mission_live.barter` is 241 lines and is BOTH the goal and the procedure —
it opens the panel, picks the tile, taps Exchange, confirms the dialog, clears overflow,
counts rounds, and decides whether to sail. Nearly every bug of 2026-08-26 lived in it.

#### The task's half — one line

    barter Birch Tree at Svear Village    until the village refuses

That is the whole goal. The task runner does not know what a panel is, does not count rounds,
and does not decide when the bartering is finished — it is TOLD, and then chooses what comes
next.

What comes back up, in task vocabulary:

    { rounds_committed: 3,
      good: 'Birch Tree',
      amity: Neutral(60000) -> Favorable(64769),
      materials_left: {Wares: 155, Firearms: 0, Sundries: 148},
      stopped_because: 'the village refused' }

`stopped_because` is the load-bearing field. "The village refused" and "the materials ran out"
and "the good is not on offer today" are three different situations for the task, and none of
them is a failure. What the task must never receive is a number it is expected to re-derive
anything from.

#### The dispatcher's half — a village activity

`brain/activities/village.py` owns every screen:

* open the Barter panel — its own effect, so its own business;
* select the good — walk the strip left to right, below the lock banner, retry a swallowed
  tap, and never conclude anything about the village from a panel that did not change;
* commit a round — tap Exchange, confirm, verify by amity/cargo, refresh a stale panel once;
* clear overflow if the hold fills;
* repeat while the village keeps accepting.

Then FINISH, with the result above. It never sails, never leaves the village, and never
decides where to go next. If it finds itself somewhere that is not a village barter panel it
returns UNRECOGNISED and hands back.

The loop inside it is allowed by the rule already stated: it drives to a measurable target and
terminates. "Until the village refuses" is measurable — the Exchange button is grey.

> **Done, 2026-08-26** (`54f0727`, `095a9ee`). The split is built and the nodes are migrated.
> `barter` is a goal and a translation; `gather` and `sell_surplus` state `Hold` and
> `TrimHold`. `more_rounds_fundable` is 0 by construction, so `barter_command`'s re-entry
> loop — the one that produced the 74-round run — now breaks immediately and should be
> deleted once this has run live.
>
> Two things the move surfaced, both worth keeping: the activity had stopped NAMING THE
> SCREEN it saw when the panel would not open, which is the one thing a lost caller needs;
> and the arrival-reading cap is gone with the plan, deliberately, because capping later
> rounds by one early observation stops the loop exactly when rising amity funds more.

#### What this fixes by construction

* **The 74-round loop cannot happen.** It happened because the TASK re-derived "rounds
  fundable" from materials (155/2 and 148/2) and sent itself back in after the phase had
  declared itself finished. A task that never counts rounds cannot disagree with the panel.
* **The stale-panel refresh stops being a special case** — refreshing is how the activity gets
  a usable panel, like selecting the Buildings tab is for entering a building.
* **The tile walk, the banner, the info tip** disappear from the task layer entirely. They are
  the activity's private problem, and the task's report is unchanged by any of it.
* **`planned_rounds` stops being carried through the result.** It was only ever there so the
  caller could compare plan against reality; the comparison belongs to whoever made the plan.

#### Overflow: a policy, not an escalation

The open question is settled (user, 2026-08-26). When the hold fills mid-round and the game
holds units pending, the task does not get asked — it has already said what it wants, as an
ORDER OF SACRIFICE:

1. **Dump the non-barter goods first**, to make room. Whatever they might have sold for, they
   are ballast next to what the barter produces.
2. **Then dump supply, down to a floor of 6 days.** Surplus supply is expendable: barter goods
   fetch far more than the food and water above that floor, so carrying extra days at their
   expense is a bad trade.
3. **Abandon whatever still will not load.** After the rest is gone, barter goods that cannot
   be carried are given up — the alternative is the game discarding them anyway.

The one invariant is the floor: **never below 6 days of supply.** That is the whole point of
the rule. Barter goods are high-profit and a poorer voyage is survivable; a fleet out of food
and water at a village that cannot resupply is not.

Note what this ordering says that a naive one would not: supply is sacrificed BEFORE barter
goods, not after. Protecting all the supply and abandoning the cargo would preserve the cheaper
thing.

#### It is a goal, not a procedure

Nothing above mentions a dialog, a button or a tap. The task says *what to keep and what to
give up, in what order*; the dispatcher turns that into whichever screens the game puts in the
way — an overflow dialog here, a jettison confirmation there — and those may change with a
patch without the policy changing at all (user, 2026-08-26).

That is also why it needs no round-trip. The activity meets the overflow, applies a rule set
before the fleet ever sailed, and reports what it gave up. The task learns of the loss in the
result rather than being interrupted for a decision it has already made.

**Note the 6 is not the 7.** `barter_quantity.SUPPLY_RESERVE_DAYS = 7.0` reserves SPACE when
planning — headroom so a round trip's supply can be carried. This 6 is a FLOOR on supply
already aboard that must survive a jettison. Adjacent quantities, different meanings, and
collapsing them would be easy and wrong.

### A blocker is not a recovery — it is the next goal

Some of what the bot has "learned to recover from" is not a screen it got lost on. It is a
CONDITION that blocks a goal, and the thing that clears the condition is another goal (user,
2026-08-26).

Not enough crew does not block a screen. It blocks **setting sail** — and hiring crew is a
legitimate act that happens in a building, the inn or the harbour. So:

    goal        be at Lisboa
    activity    harbor: the departure is refused — "Not Enough Crew"
    result      BLOCKED { blocker: 'not_enough_crew', short: 232 }
    task        adds a goal: recruit crew here
    dispatcher  ENTER_BUILDING(inn or harbor) -> crew activity -> hires
    task        the original goal again: be at Lisboa

Nothing in that is a recovery. It is the mission graph gaining a node, the same way a gather
leg is added when a material is missing — which `brain/mission.py` already does for out-of-stock
gathers. The same shape covers `not enough ship life`, whose remedy is a repair.

#### Why this matters more than tidiness

Until 2026-08-26 these conditions were handled by `learned_recoveries.json`, replayed
PROACTIVELY from inside `perceive` — before any goal existed. Audited that day, eleven entries
could fire that way, and among them:

    recruit_crew_confirmation_dialog   taps OK on a spend of 181,224 ducats, at fixed (1302,812)
    market_purchase_confirm_dialog     taps OK on a purchase confirm, at fixed (1306,976)
    insufficient_crew_harbor           taps (200,160), (400,160), (600,160), (1200,600), (800,160)

Matched by BAG OF WORDS — `['Market','Purchase','Sell','Trade Points', ...]` matches any market
screen, which is how one fired during a SELL goal on 2026-08-26 and tapped Purchase. Auto-
confirming a spend is within policy (everything but red gems), but not fired blind from
perception, against a dialog matched on keywords, at a coordinate recorded on another day.

The last of those three is not a recovery at all. Keywords `['crew','recruit','departure',
'disabled']` and a five-tap sweep across the screen is flailing that got saved as a recipe.

> **Done, 2026-08-26.** The proactive firing is gone (`0aa1714`): `perceive` still MATCHES a
> recovery and logs it, and no longer runs it, because a recovery acts on the world underneath
> and perception does not act. The reactive path in `human_escalation` is untouched, so
> nothing lost the capability — it lost the shortcut. `tests/test_perception_does_not_act.py`
> locks the rule with a source grep so it cannot return by another route.
>
> The replacement is built: `brain/activities/harbor.py` reports the blocker BY NAME and stops,
> `goal_for_blocker` turns the name into the next goal, and `brain/dialog_question.py` answers
> the confirmation by describing it. `sail_to`'s FLEET_CHECK phase still owns the old ladder
> and is the remaining wiring.

#### What follows

1. **Perception stops acting.** It identifies and clears obstructions; it does not replay
   taps. Only the dispatcher causes transitions, and it needs a goal to do it for.
2. **Blockers are reported, not fixed in place.** An activity that meets "Not Enough Crew"
   returns BLOCKED and names it. The task decides whether crew is worth buying here.
3. **The remedies become goals and activities** — `recruit crew`, `repair the ship` — served
   by a harbour/inn activity, with the recorded tap sequences becoming that activity's
   internals rather than a replayed macro.
4. **The market entries are already obsolete.** `MarketActivity` drives those screens properly,
   which is what a learned macro was standing in for.

The knowledge in that file is real and hard-won — that a crew shortage stops a departure, that
the fix is in the inn or the harbour. It is in the wrong shape, not wrong.

### The same lens on the other seven nodes

`barter` is the extreme case; the rest fall into three groups.

| node | lines | what it really is | goal (task) | activity (dispatcher) |
|---|---|---|---|---|
| `gather` | 42 | market shopping | *buy Iron at Amsterdam (~242), until the hold covers it or the shelf is empty* | market: enter, Purchase tab, bulk, tile, quantity, verify by cargo |
| `sell_surplus` | 23 | market shopping | *free the hold at this port, keeping the barter materials* | market: same activity, selling |
| `sell` | 15 | market shopping | *sell the hold at Lisboa* | market: same activity, selling |
| `barter` | 241 | village trading | *barter Birch Tree at Svear until the village refuses* | village |
| `sail_to_village` | 16 | movement | *be at Svear Village* | sea / world map |
| `sail_to_sell` | 3 | movement | *be at Lisboa* | sea / world map |
| `sail_route` | 10 | movement | *take the saved route* | sea / world map |
| `supply_verify` | 27 | **already a task decision** | *carry 7 days' supply before a village leg* | none — it reads and decides |

Two things this table says.

**The three market nodes are one activity with three goals.** That is the "ONE sell flow,
several goals" rule from CLAUDE.md, arrived at from the other direction: `gather`,
`sell_surplus` and `sell` differ only in WHICH goods and HOW MANY, and each currently
re-implements entering the market. `_enter_market_at` exists because that duplication had to be
patched in four places at once.

**The three sail nodes are one goal shape.** *Be at X.* They differ only in how X is named — a
port, a village, a saved route — and all three delegate to `drive_sail_to` already, which is
the closest thing in the codebase to a proper activity today.

**`supply_verify` is the model.** Twenty-seven lines, zero screen touches: it reads the fleet,
compares against a floor and an ETA, and returns a decision. That is what every task node
should look like.

#### The reduction

Eight nodes, driving screens in seven of them, become **four goal shapes** over **three
activities**:

    goals       hold / free the hold      -> market activity
                trade until refused       -> village activity
                be at X                   -> sea + world map activities
                carry enough supply       -> no activity; a decision on read data

    activities  market, village, sea/world-map

The graph's dependency structure is untouched. What changes is that a node stops being a
function that drives screens and becomes a sentence the task runner can say.

### What crosses the boundary, in each direction

The dispatcher hands **up** the state and the data the activity observed. The task runner
consults **its own** record of progress and hands **down** the next goal. Both halves matter,
and the second is easy to get wrong.

**Upward: state + observed data.** The state answers "where is the bot now" and the observed
data answers "what came of the work" — 445 Iron aboard, the hold at 2177/4108, the barter panel
showing four rounds left. The data is what stops the task runner going to the screen itself.

**Downward: a goal, in task vocabulary.** "Gather Matchlock Gun." "Sail to Svear Village."
"Sell the hold at Lisboa." Never "tap the Purchase tab."

**The task runner does not need to understand UI states** (user, 2026-08-26). It is told the
state, but it decides from *task progress* — which materials are aboard, which legs are done,
what the mission still needs. It must never branch on `sub_menu:purchase` or `port_map`; those
belong to the dispatcher's vocabulary, and a task runner that reads them has taken on knowledge
that will then have to be kept in sync with every UI change the game ships.

So the practical test of the interface: **could this task runner still decide if the UI were
replaced wholesale?** If the answer is no, the boundary has leaked. What it genuinely needs is
the task-level part of the state — the PLACE (Amsterdam, Svear Village, at sea) — plus the
observed data. "Which screen" is never an input to "what next".

This is the same rule as *the granularity of involvement is the activity boundary*, pointed the
other way. That rule keeps the task runner out of screens and taps on the way DOWN; this one
keeps screens and taps out of the task runner on the way UP. Together they are what let the two
layers change independently — new UI, same task; new task, same UI.

And "nothing" remains a valid goal. A task runner with no next step says so, and the dispatcher
dispatches nothing rather than inventing work to fill the gap.

### A transition is a switch of activity, however long it takes

Entering a building, departing a port, opening the world map: each ends one activity and
expects a new one (user, 2026-08-26). The fact that it takes time changes nothing about what
it is. `loading` is simply the state the next tick perceives on the way, so no caller needs
wait-for-the-transition logic and none should have any — a poll inside a primitive is the
thing this architecture exists to remove.

Which is also why entering a building is an INTENT and not part of the market activity. The
activity begins when perception already says `building: market`; getting there is a
transition, and transitions are dispatched. `brain/barter_mission_live._enter_market_at` —
which perceives, taps, reorients and retries — is that transition written by hand at the task
layer, in four call sites, because there was nowhere else to put it.

### Two ways an activity ends — and only two

Android gives an activity exactly two exits, and both END it (user, 2026-08-26):

* **`finish()`** — the activity ends itself and names no successor. Whatever was underneath
  comes back.
* **`startActivity(intent)`** — the activity names the successor.

The bot is the same. The idle lock's swipe up is a **finish**: it ends that world and names
nothing, because nothing about the lock knows what is beneath it. Tapping Supply Depart is an
**intent**: it names where the fleet is going.

What matters is that the dispatcher does not care which it was. In both cases the activity
ends, the dispatcher regains control, perceives, and asks the task runner. There is no branch
for "this one ended by finishing". A time lag between the two is not a special case either:
`loading` is simply the state the next tick perceives, which is why nothing anywhere needs
wait-for-the-transition logic.

This is what makes the FSM's edge shapes read properly:

| Exit | Means | Example |
|---|---|---|
| `"to": null` | a **finish** — ends the activity, names no successor | `idle_lock` swipe_up, `loading` wait |
| `"to": "<state>"` | an **intent** — names the successor | `port_overworld` → `building` |

So `idle_lock` and `loading` are not odd states with broken edges. They are the states whose
ONLY exit is a finish, which is exactly why BFS cannot route through them — there is nothing
to route *to*, by construction. Their destination is established the same way every other
destination is: the dispatcher perceives after the activity ends.

**The mistake this corrects.** On 2026-08-26 the lock was first handled by adding paths for it
— a branch in `step_toward` for edges with no far end, and a hook in the mission runner's retry
loop. Both work, and both are the per-path habit in a new costume: they encode "this kind of
screen is special" in two more places. Under the rule above the lock is not special at all; it
is an activity whose entire repertoire is one finish. Those two hooks are scaffolding for the
pre-dispatcher world and should be deleted when the dispatcher owns the loop — the general
mechanism already covers them, which is the test of whether a rule is really general.

### "Exit the village" is not a goal — it is the dispatcher's business

The goal is **sell at Lisboa**. Getting out of wherever the fleet happens to be — backing out
of a village, departing a port, closing a panel — is HOW, and how belongs to the state machine
(user, 2026-08-26). A task runner that says "back out until you reach the sea" has given a UI
instruction, and it will be wrong the moment the bot is somewhere it did not expect.

`brain/barter_command._depart_village_to_sea` is that mistake, and it cost something on
2026-08-26. The fleet was at **Stockholm — a port**, because the user had moved it there for
supply, while the tail still believed it stood in a village. Perception said so plainly:

    [barter_command] leaving the village for the tail (state='port_overworld', back 2/4)
    [perceive] obstruction detected: kind='dialog' actions=['Cancel','Ok']

The state was read correctly and ignored, because the caller was executing a fixed idea of
what had to happen next rather than asking what to do from where it actually was. Back on an
overworld raises "Exit Game?", so the bot ended up one positive tap from quitting.

Under the dispatcher none of this exists. The task runner says "sail to Lisboa"; the dispatcher
perceives `port_overworld` and dispatches the intent that opens the world map by its globe, or
perceives `village` and dispatches the one that leaves first. Neither route is written down at
the task layer, and the task layer cannot be wrong about which one applies, because it never
had an opinion.

Two further things this frame is a good example of:

* **The dialog was handled by nobody.** A Cancel/Ok panel is an obstruction the dispatcher
  clears before resolving an activity — and one carrying ACTIONS is a decision, so it is
  refused rather than guessed at when no goal names the button (CLAUDE.md). Here the
  obstruction consult ran with `goal=None`, which is precisely the case that degrades to
  "tap whatever looks positive".
* **The interim fix is a patch on something that should not exist.** Teaching
  `_depart_village_to_sea` that a port overworld needs no escaping makes today safe. It does
  not make the layering right, and the function should go when the dispatcher owns the loop.

### An activity may loop, but must not hold it

A loop inside an activity is fine when it drives to a **measurable** target and terminates on
reaching it — buy until the hold has 411 Iron. It is not fine when it waits to see whether the
world changed: that is the dispatcher's job, and a loop that does it blinds every layer above.

---

## Intents — transitions, carrying a purpose

An intent is dispatched, not awaited. Like an Android intent it carries **extras** saying why:

    OPEN_WORLD_MAP(purpose = SET_SAIL,     destination = "Svear Village")
    OPEN_WORLD_MAP(purpose = REMOTE_CHECK, subject     = "Svear Village")
    ENTER_BUILDING(name = "Market")

The world map is an ACTIVITY, not a corridor — you go there to do something that changes state,
exactly like entering a market. Its purpose decides what that is:

* **REMOTE_CHECK** — read a village or port from a distance to make a plan, then return to the
  overworld. Enter, do the task, come back: the same shape as any building visit.
* **SET_SAIL** — behaviour depends on the **entry condition**:
  * entered from `sea` — committing the destination sets sail immediately;
  * entered from `port_overworld` — the game is asked to perform the whole departure, with
    three possible outcomes:

    1. **full success** — the player runs to the harbour, the fleet auto-supplies, and sails;
    2. **blocked at port** — nothing happens; the bot must walk to the harbour, tap Supply
       Departure by hand, then open the world map AGAIN to set the destination;
    3. **departed but not moving** — the speed-0 bug: at sea, supplied, ETA never falls. Open
       the world map again and re-commit.

Those are three **results of one activity**, not three failure paths to retry inside it. The
dispatcher perceives which happened; the use case decides what to do. Today a primitive owns
all three internally and can report none of them.

### The world map has a FILTER — a lens the intent should choose

The button in the map's lower left is not a button but a selector. Opening it (live
2026-08-25) shows a grid, with the current choice highlighted:

    Limited Area      Monopolizing Guild   Invest
    Enemy Company LV  Company LV           Seaworthiness
    None (selected)   Momentum             Ice Breaking

All nine read cleanly from the ordinary parse. They are lenses over the same map, and several
map directly onto use cases we have sketched:

| filter | the use case that wants it |
|---|---|
| `Invest` | investment — which ports can be invested in (blue-gem economy) |
| `Enemy Company LV` | `hunt` — finding the enemy fleets |
| `Monopolizing Guild` | barter and trade — guild-monopoly goods never restock |
| `Seaworthiness`, `Ice Breaking`, `Momentum` | route planning — can this ship make that passage |

So the filter belongs in the intent alongside the purpose:

    OPEN_WORLD_MAP(purpose = REMOTE_CHECK, subject = "Svear Village")
    OPEN_WORLD_MAP(purpose = INVEST_SURVEY, filter = "Invest")
    OPEN_WORLD_MAP(purpose = FIND_ENEMY,    filter = "Enemy Company LV")

and selecting it is intra-activity work, like the tab strip on the sea panel. Two of the same
lesson in one game: **a panel's mode decides what the bot can perceive**, and the use case has
to establish that mode before it can read anything. The fleet tab was the first instance; the
map filter is the second.

### The Invest lens makes the investment survey readable

Selecting `Invest` puts a number under every port and colours it (live 2026-08-25). The
threshold is **500 investment points** — below that the investment earns no blue gem and is
wasted — and the game marks the disqualified ones in RED:

    red   171, 166, 171 ...   below 500, investing is a waste
    grey  640 (Cologne)       qualifies for a blue gem

Both signals read from an ordinary parse, and they **agree on every port** — nine read on one
frame, with the colour test (`r - g > 18` over the plaque) classifying exactly as the
`value >= 500` test does. That is a free cross-check of the same kind the sorted port list
gives: two independent readings of one fact, so a misread number is caught by its colour and
vice versa.

Which makes the survey straightforward:

    OPEN_WORLD_MAP(purpose = INVEST_SURVEY, filter = "Invest")
      -> read every visible port's number and colour
      -> the qualifying ports are the ones not in red
      -> sail to one, and invest at its Bureau

The whole map is surveyed from one screen without visiting anywhere, which is what makes
investment plannable at all rather than discovered a port at a time.

Two practical limits:

* **Qualifying ports are rare.** On that frame exactly one of nine qualifies — Cologne at 640,
  with everything else at 166-171, under a third of the threshold. So investment is a TARGETED
  trip, not something done opportunistically wherever the fleet happens to be. That is another
  reason it belongs to the strategic layer: it is worth planning a voyage around, or worth
  folding into one already planned.
* **The survey only sees the current viewport.** Ports outside the visible map are not read, so
  a full survey means panning — which today's runs showed to be slow and error-prone. Better to
  survey what is in view around the fleet's actual route than to attempt the whole world.

Still unknown: **the Bureau screen itself.** The map says WHERE; it does not say what the
transaction looks like — how an amount is chosen, what confirms it, or whether the number
shown is points already invested, points still needed, or the player's standing there.

**And the numbers seen here are a snapshot of an UNDER-INVESTED state** — the player has not
been investing actively (user, 2026-08-25), so one port in nine qualifying says more about the
current save than about the game. Once investing begins, the map changes underneath the
survey, which is itself a reason the survey must be re-read rather than cached.

### The shape it will take: a regional investment tour

Investment is a big part of the game and will be its own topic. The task shape the bot needs
(user, 2026-08-25) is a REGIONAL TOUR, not a single trip:

    "invest in the Baltic ports"
      depart London
      select the ports in that region
      visit them and invest at each
      following a strategy that maximises return

That is a fourth distinct shape, after the point-to-point trade run, the gather-and-barter
mission, and the coast-hugging discovery sweep:

* the destination is a **region**, not a port — the members are chosen, not given;
* the **order matters**, because it is a circuit and sailing is the cost;
* each stop carries a **decision, not just an action** — invest how much, or skip;
* and the criterion is **return**, which needs a model of what investing buys: the 500-point
  threshold is only the qualifying bar, not the payoff.

In the layer terms of this document it decomposes cleanly — `sail_to(port)` and `invest(port,
amount)` are ordinary use cases, and the tour is a task-runner plan over them. What does NOT
exist is whatever picks the region, orders the circuit and sizes each investment. That is the
strategic layer again, and this is the third thing asking for it.

### How each transition ends

| transition | done when | if it has not happened |
|---|---|---|
| port -> building | `state == building` | 20s -> tap the building again |
| building -> port | `state == port_overworld` | tap Home again |
| port -> sea | `state == sea` | re-commit the destination |
| sea -> port | at the destination port | ETA-driven; longer budget |

Building entry has **no ETA** — the player runs, and "still running" is indistinguishable from
"stuck" except by elapsed time. A flat 20s covers it: ports are small and the market sits near
the harbour, so a timeout means the tap was lost, not that the player is still en route.

The minimap was considered as a motion reference (present <-> walkable port world; the player is
a green arrow, as the ship is at sea) and confirmed on real frames — but `detect_minimap_bbox`
missed 1 frame in 10 while STATIONARY, so it cannot gate a transition. The state
classification is reliable and needs no motion signal once a timeout exists.

### Leaving a world uses the GAME's controls

Never the Android back key — it can overshoot past the overworld into the system "exit game?"
dialog. The chrome detector already locates the game's own controls and records each match's
centre: measured on a real Market frame, `home @ (2219,44)`, absent on the port overworld.

* **Home** — one tap out of any depth.
* **the chromed title's back arrow** — up exactly one level, to stay inside the building.

---

## Use cases — what the task runner asks for

A use case is one named operation with a precondition, some work, and a result. Both existing
task shapes are already use-case lists:

    barter:  gather:Amsterdam -> gather:Barcelona -> gather:Tripoli -> sell_surplus
             -> supply_verify -> sail_to_village -> barter -> sail_to_sell -> sell

    trade:   sell_all(London) -> buy_all(London) -> sail_to(Port Royal)
             -> sell_all(Port Royal) -> buy_all(Port Royal) -> sail_to(London)

### The catalogue

| use case | precondition | result carries |
|---|---|---|
| `gather(port, materials[])` | at `port`, market resumed | what was bought, what was short, stock seen |
| `sell(port, goods, aim)` | at `port`, market resumed | what sold, for how much |
| `barter(village, good, rounds)` | in `village` | rounds done, quantity held, amity/rounds left |
| `sail_to(destination)` | anywhere | arrived, or why not |
| `supply_verify(days)` | at a port | days of water and food aboard |
| `read_village(village)` | anywhere | recipes, amity, rounds remaining |
| `read_market(port)` | market resumed | prices and stock, both tabs |
| `read_fleet()` | anywhere the menu opens | capacity, cargo, supply |
| `discover_nearby(max_km)` | in `sea`, nearby list readable | the name learned, where it was |

### Preconditions are what make a use case resumable

`gather:Amsterdam` failed on 2026-08-25 because it was a script, not a use case: it had no
entry condition to test, so when the fleet arrived nothing re-evaluated. Written properly —

    gather(Amsterdam, [Iron 411, Matchlock Gun 183, Candle 411])
      precondition:  at Amsterdam, market resumed
      postcondition: hold >= the listed quantities

— it is resumable by construction. Run it again after any interruption and it either completes
what is missing or reports that it is already satisfied. That also subsumes the resume path
`barter_mission_live` hand-rolls today, which crashed on 2026-08-23 when the phase model routed
a mission straight to `bartering`.

### The market is told the WHOLE list

    gather(Amsterdam, [Iron 411, Matchlock Gun 183, Candle 411])

not one material per visit. The market activity is the only layer that can see what this port
actually stocks — a port may carry one, two or all three — so splitting the decision forces the
layer above to guess. And it **stops the bot selling what it needs**: a visit that knows the
full list will not clear Iron out of the hold to make room. Split into three visits, that is
lost, and a `sell_surplus` step has no way to know.

### The result

    {use_case, ok, detail, observed}

`observed` is what perception saw when it finished — the port, the cargo, the rounds left — so
the task runner never has to ask the screen anything itself:

    gather(Amsterdam, [...])  -> {ok: true,  observed: {Iron: 411, cargo: 1307/2873}}
    sail_to(Svear)            -> {ok: false, detail: "no route", observed: {port: London}}
    barter(Svear, Birch, 3)   -> {ok: true,  observed: {rounds_done: 3, held: 1236}}

---

## Dialogs

Two things get called "popups" and only one blocks:

* **A game dialog** — brownish frame, positive and sometimes negative button, raised BY an
  action (confirm a purchase, confirm a departure). It genuinely blocks: nothing else in the
  activity is possible until it is answered. This is Android's dialog over the resumed
  activity, and it belongs to the activity that raised it.
* **A notice** — daily news, an event banner. It blocks some UI controls, not the activity, and
  nothing the bot did raised it.

Dismissal lives in the activity; the **recognition** is shared — find the positive and negative
buttons and answer. Mostly **confirm**: the dialog is the last step of the action the bot chose.

**One exception: RED GEMS are never auto-confirmed.** Everything else that costs something is
confirmed, blue gems included — blue gems are earned through investment and other in-game
activity, so spending one is recoverable. Red gems are not, and nothing in the bot's current
work needs them, so a red-gem dialog means something unexpected is on screen: stop and surface
it rather than pay.

---

## The repository — and why the screen outranks it

Use cases need data that outlives an activity: the recipe being bartered, the plan's material
list, fleet capacity, a village's remaining rounds. That is a repository shared across
activities, as in Android.

**With one difference that matters: the bot can be wrong.** A normal repository trusts its
cache. Here the cache is a belief formed by an earlier perception, and beliefs go stale — the
village re-rolls its quantities, amity decays, the hold changes, the fleet moves.

> The cache exists to avoid re-reading what cannot have changed, never to override what is
> visible. Whenever there is a chance to check, take the truth from the SCREEN and refresh.

Every value should carry where it came from and when, so a use case can decide whether to trust
it or re-read. The departure loop violated exactly this: a remembered "we are departing"
outranked a visible "we have arrived".

---

## Worked example — Port Royal <-> London trade run

    TASK RUNNER                      DISPATCHER / ACTIVITY                    RESULT
    sell(London, all, profit)   ->   market resumed? no
                                     ENTER_BUILDING(Market)          [INTENT]
                                     perceive -> building:market
                                     sell loop until the hold is clear
                                                                       <-    {ok, sold 1240, +8.4M}
    gather(London, best[])      ->   market already resumed; buy loop
                                                                       <-    {ok, cargo 2601/2873}
    sail_to(Port Royal)         ->   EXIT                             [INTENT]
                                     OPEN_WORLD_MAP(SET_SAIL, Port Royal)
                                     perceive -> sea ... ... arrival observed
                                                                       <-    {ok, port: Port Royal}
    sell(Port Royal, all, ...)  ->   ... and around again

The task runner never names a building, a tab, or a tap. It reads `observed` and picks the next
use case — which is also how it can skip a leg (nothing worth selling) or re-plan (the market is
out of stock) without any action having to guess.

## Worked example — barter Birch Tree at Svear, sell at Lisboa

    TASK RUNNER                      DISPATCHER / ACTIVITY                    RESULT
    read_village(Svear)         ->   OPEN_WORLD_MAP(REMOTE_CHECK, Svear)
                                     find village, Barter tab, read the list
                                     CLOSE -> port_overworld
                                                                       <-    {ok, Birch Tree 412 <-
                                                                              Iron 119, Matchlock 53,
                                                                              Candle 119; rounds 0/4}
    -- plans from THAT data: 3 rounds, buy 411 / 183 / 411 --
    sail_to(Amsterdam)          ->   OPEN_WORLD_MAP(SET_SAIL, Amsterdam)
                                     one of the three outcomes; sea; arrival
                                                                       <-    {ok, port: Amsterdam}
    gather(Amsterdam, [3 mats]) ->   ENTER_BUILDING(Market); buy what is stocked
                                                                       <-    {ok, Iron 411, Candle 411,
                                                                              short: Matchlock 183}
    -- short: the task runner reroutes, it is the only layer with the plan --
    sail_to(Barcelona)          ->   ...
    gather(Barcelona, [short])  ->   ...
                                                                       <-    {ok, Matchlock 183}
    sail_to_village(Svear)      ->   OPEN_WORLD_MAP(SET_SAIL, Svear); Move to Village
                                                                       <-    {ok, village: Svear}
    barter(Svear, Birch, 3)     ->   barter rounds until materials or rounds run out
                                                                       <-    {ok, rounds 3, held 1236}
    sail_to(Lisboa)             ->   LEAVE; OPEN_WORLD_MAP(SET_SAIL, Lisboa)
                                                                       <-    {ok, port: Lisboa}
    sell(Lisboa, [Birch], profit) -> ENTER_BUILDING(Market); sell loop
                                                                       <-    {ok, sold 1236}

**What this fixes in the run that failed.** `sail_to(Amsterdam)` has a checkable postcondition,
so when the fleet arrived the dispatcher's next perceive satisfied it. Today the equivalent step
encoded the how, had nothing to re-evaluate, and sat inside one call for six minutes while
perception reported `port_overworld / Amsterdam` to nobody who could act on it.

---

## Worked example — discover ports while hugging the shore

The third use case, and deliberately NOT trade-shaped: no material list, no chosen destination.
It exists to test whether the model holds outside trading. It does, and without changes.

The bot already sails the coast under the hugging rule (`HugShoreGoal`), and finds ports and
villages that show as **`???`** because they have never been visited. The `sea` activity's right
panel already lists what is around, with a name and a distance:

    (anchor) Amsterdam     Nearby
    (anchor) Den Helder    Approx.  93.9km
    (anchor) Groningen     Approx. 143.1km
    (anchor) Bremen        Approx. 275.9km

An undiscovered one appears in that same list as `???`. So:

    discover_nearby(max_km = 500)
      precondition:  in the `sea` activity, the nearby list readable
      trigger:       an entry named ??? within max_km
      work:          tap that entry -> it becomes the destination -> arrival
                     read the port or village NAME from the screen it lands on
      postcondition: the name is recorded; the fleet is at that port/village
      then:          resume hugging from there

**Why this validates the model.** Every piece is something the architecture already has:

* the nearby list is **work inside the `sea` activity** — which is the clearest evidence yet
  that `sea` is an activity and not a corridor: the bot reads it, reasons about it, and acts on
  it without leaving;
* tapping the `???` entry is an ordinary **COMMIT_DESTINATION** intent — the same one the trade
  run uses, entered from `sea`, so it sets sail immediately;
* **arrival is the activity ending**, noticed by perceiving `port_overworld` (or `village`) —
  nothing new to dispatch;
* learning the name is a **repository write**, and it is the screen-as-truth rule at its
  simplest: the name was unknown, the screen now says it, so the repository takes it;
* resuming the coast is just the next use case — `hug_shore` picks up from wherever the fleet
  now is.

**What has to be built:** only a reader for that panel. Measured on a labelled frame
(`observe_2026-05-27_16-44-29/0001`, five undiscovered ports off Genoa):

    (anchor) Genoa    Nearby
    (anchor) ???      Approx.  85.3km      <- within 500
    (anchor) ???      Approx. 364.3km      <- within 500
    (anchor) ???      Approx. 582.6km
    (anchor) ???      Approx. 731km
    (anchor) ???      Approx. 877.7km

**Do not try to read `???`.** It never OCRs as question marks. Across two labelled frames it
came back as nothing at all, as the icon placeholder, or as garbage — `|3 222|`, the marks read
as 2s:

    Genoa row    text |# Genoa|   button |Nearby|
    ??? row      icon, icon                            <- no name text at all
    ??? row      button |Approx. 364.3km|              <- distance only
    ??? row      button |3 222|                        <- garbage where the name should be

So the rule is association, not text: **pair each distance with the row above it; a row whose
name slot holds no word of three or more letters is undiscovered.** Icon placeholders must be
excluded first — the parser labels an icon element `icon`, which otherwise reads as a name.
Same lesson as the trade list: identify by what a row HAS and where it sits, never by a glyph.

Prototyped against both frames, this reads them exactly:

    frame 0 (off Genoa)          frame 5
      Genoa      Nearby  known     ???  67.7km    <= 500  ***
      ???     85.3km  <= 500 ***   ???  937.8km
      ???    364.3km  <= 500 ***   ???  1,111.5km
      ???    582.6km                ???  1,168.8km   (name read as "3 222")
      ???      731km                ???  1,168.8km
      ???    877.7km                ???  1,413km

* **Distances need forgiving parsing.** `731km` came back as `73lkm` — l for 1 in this font —
  and larger ones carry commas (`1,168.8km`). Strip commas, substitute `l`/`I` for `1`, and
  treat a genuinely unparseable distance as UNKNOWN rather than zero, or an unreadable row
  becomes the nearest target.
* **500 km is a reasonable default.** It selects 2 of 5 unknowns in the first frame and 1 of 6
  in the second, leaving the ones that would drag the fleet far off the coast.
* **The minimap also shows an undiscovered port** (a yellow anchor with `???` beside the ship),
  but the parser did not pick that marker up on the frame where it is plainly visible. The LIST
  is the reliable route; the minimap marker is a nice-to-have, not the trigger.

### Villages are in the same list, with a different icon

A third frame (`2026-05-25_18-05-41/0011`) carries both:

    (anchor) ???       Nearby
    (house)  ???       Approx.    48km      <- a VILLAGE
    (anchor) ???       Approx. 1,238.7km
    (anchor) Plymouth  Approx. 1,248.1km
    (anchor) Calais    Approx. 1,478.3km

Ports carry an anchor, villages a house. For the trigger this does not matter — tap either and
the fleet sails and enters. It matters for the POSTCONDITION: a port lands in `port_overworld`
and a village in `village`, so `discover_nearby` must accept both, and reads the name from
whichever it got.

### The list is sorted by proximity — use it

The top row is always the closest. Two consequences:

**The nearest unknown is simply the topmost unknown.** No distance comparison is needed to
choose a target; the distances only decide whether it is within range at all.

**The ordering validates the parse.** Any row whose distance is smaller than the row above it
is a misparse, because the game does not order them wrongly. Run over the buggy regex this
catches the Plymouth error on its own:

    BUGGY  [0.0, 48.0, 1238.7, 1.0, 1400.0, 1478.3]
           ORDER VIOLATION at row 4: parsed 1.0 but row 3 was 1238.7
    FIXED  [0.0, 48.0, 1238.7, 1248.1, 1400.0, 1478.3]
           consistent

That is a free invariant on every read, and it would have caught a bug that otherwise made the
fleet sail to the wrong place confidently. A violation means: re-read the row, do not trust it.

### The sea activity MONITORS the list across ticks

Discovery is not a one-shot check. The fleet is hugging the coast, and the list changes as it
moves — an unknown that is 600km away now climbs toward the top as the coast is followed. So
the `sea` activity's work each tick includes reading this list, and its result carries what it
saw:

    sea tick -> {observed: {nearby: [(unknown, 48km), (Plymouth, 1248km), ...]}}

The task runner watches those results and issues `discover_nearby` when an unknown comes within
range. That keeps the decision where it belongs — the sea activity reports, it does not decide
to abandon the coast — and it is the clearest illustration of why `sea` is an activity: it does
real, repeated work while resumed.

### Two failure modes found by testing, not by reading the code

**1. The distance regex chose the wrong number.** `Approx. 1,248.1km` is OCR'd as
`1,248.lkm`, and a pattern allowing `l` only among the integer digits matched the trailing `l`
alone — parsing Plymouth as **1.0 km** and making it the nearest target. `l`/`I` must be
allowed anywhere a digit can appear, the decimal part included.

**2. OCR drops KNOWN names too.** On that same frame neither `Calais` nor `Dover` was read at
all, only `|d Plymouth|` and a `|222|`. So "no name means undiscovered" has a false-positive
mode: a known port whose label the parser missed looks new.

The cost is bounded — the fleet sails somewhere it has already been, which wastes a leg but
corrupts nothing, and arrival is self-correcting because the name is read there and the
repository simply confirms what it knew. Worth stating plainly rather than pretending the rule
is exact: **`discover_nearby` will occasionally re-visit a known port.** If that proves
expensive, the cross-check is the repository — a row whose position matches a port already
known is not new — but that needs positions, which the list does not give.

One thing still to decide: **ports and villages may be separate tabs.** The panel has an icon
strip with counts (7, 10, 11, …) and the anchor tab is the one selected here. If villages live
under a different tab, the use case has to name which it is discovering, or check both.

---

## Sketch — battle (NOT yet built)

The same sea panel, on the **ship tab**, lists nearby fleets — also sorted by distance
(`2026-05-24_capture_steered/0006`):

    (flag) Doria Pirates      LV 12   2.9 km
    (flag) Reis Garrison      LV 11   8.6 km
    (flag) Imai Sokun Merc... LV  8  10.9 km
    (flag) Cespedes Advent... LV  9  12.5 km

and above the list sit the repel controls:

    [ ] Use Repel                          <- a checkbox: finish the battle instantly
        Repel Support Owned      0         <- tickets held
        Repel Support Used       5         <- tickets a repel costs

**The flow** (user, 2026-08-25): tap a fleet and the player's fleet CHASES it; when close
enough a battle confirmation screen appears; tapping Combat or Repel may raise dialogs; OK
starts the battle.

**Where the pieces land in this model:**

* reading the fleet list and tapping a target is **work inside the `sea` activity** — the
  chase does not leave it, so the tap is not an intent;
* the battle confirmation is a **game dialog**: it blocks, and it belongs to `sea`. Note it is
  raised by the CHASE completing, not directly by the tap, so it can appear several ticks
  later — the dispatcher has to expect it rather than wait for it;
* the battle itself is presumably a new **activity**; nothing here has been examined yet;
* **repel is a precondition, not a preference.** `Use Repel` is only usable when
  `owned >= used` — here 0 >= 5 is false, so the checkbox is meaningless and the battle must be
  fought. A use case that assumes repel would stall.

**Measured about reading it:**

* fleet name, level and distance all parse from the ordinary read, with the same trailing noise
  as the port list (`2.9 km]`, `12.5 kml`) — so the same forgiving parse applies;
* `Repel Support Used  5` reads at confidence 1.00;
* `Repel Support Owned  0` does **not** read at all from the plain crop — the value is rendered
  in red, and only a **3x upscale** recovers it (conf 0.99). Same technique the trade list
  needs for its missing badges.

### Battle is not one use case but at least two

**1. `repel_until_spent()`** — repel fleets until the repel tickets run out. Bounded by a
resource the panel states (`Owned`), so it has a clear termination condition. Requires
`owned >= used` to start at all.

**2. `hunt(enemy)`** — fight a NAMED enemy. The powerful ones — the yellow and red pirate
markers visible on the world map (`2026-05-26_09-02-11/0001`, three yellow and one red among
the `???` ports near Cairo) — are a different problem entirely, because **the target moves**.

Tapping such a marker opens a panel giving:

* the enemy's name and fleet composition;
* the **conditions your fleet must meet to fight** — and any stat you do not have shows in
  RED. That is a precondition the bot can READ before committing, which is unusually kind.

But tapping only yields a **location**, not a destination that tracks. `Move` sails there and
the enemy has moved on. So the pursuit is a loop the task runner owns:

    open world map (REMOTE_CHECK)  -> where is the enemy NOW?
    Move to that location           -> sail  (the sea activity)
    on arrival: not in the fleet list, because it has moved
    open the world map again        -> updated location
    ... until it appears on the MINIMAP, at which point steer manually toward it
    ... until it appears in the FLEET LIST, at which point it can be tapped and chased

Three separate identification surfaces, coarse to fine: **world map marker -> minimap ->
fleet list**. The bot has to hand off between them, and only the last one lets it engage.

**The tab decides what the bot can even see.** That right-hand panel is one surface with four
tabs — a count on each: quests (7), ports (10), fleets (11), cargo. Only ONE list is shown at a
time. In `observe_2026-05-27_16-44-29/0009` a red pirate sits directly beside the ship on the
minimap while the panel is on the PORTS tab, so the list is six `???` harbours and the enemy
does not appear in the readable data at all. Selecting the fleet tab is intra-activity work in
`sea`, and it is a PRECONDITION of hunting: without it there is no fleet list to match against.

**Distance decides whether it is reachable in the list.** The fleet list is sorted by
proximity like the port list, so a distant enemy may be far enough down to need scrolling, or
absent. Sailing toward it — steering by the minimap bearing, which works when the list cannot
help — brings it UP the list until it is near the top and can be tapped. So the pursuit is:

    minimap gives a BEARING           -> steer manually
    proximity brings it INTO the list -> then up the list
    near the top                      -> tap, chase, battle confirmation

**Catch:** long names are truncated in the fleet list (`Imai Sokun Merc...`), so recognising
the pursued enemy there is a fuzzy match, not an equality test. (The matcher now strips
trailing ellipses for this reason.)

**Why starting from a port is the awkward case.** The use case's precondition is *at sea, with
a target in range*. From a port that means sailing out first with no destination in mind, which
none of the existing use cases do — every other one sails TOWARD something. That is a task
runner question (what does "go looking for a fight" mean as a goal?) rather than an activity
one, and it is unresolved.

---

## Where today's code breaks the model

* **`navigate_to_building`** spans two activities and the transition between them: it chooses in
  `port_overworld`, dispatches the tap, waits out the transition, and works inside `building`.
  In Android terms it starts an activity and runs the next one's setup inline.
* **`depart_from_port_via_world_map`** spanned three — `port_overworld` -> `world_map` -> `sea` —
  with its own retries at each. That is the call that held the goal for six minutes.
* **80 functions** own an internal loop with waits; nesting reaches **depth 5**; and
  `exit_to_overworld` <-> `recover_to_port_overworld` is a **cycle**, two sub-loops able to
  re-enter each other with no shared budget. Under the world-change rule the set needing change
  is **12 functions / 49 call sites** (`docs/subloop_inventory_2026-08-25.txt`).
* The **world-change actions** and their reach today:

  | function | callers | transition |
  |---|---|---|
  | `navigate_to_building` | 13 | port_overworld -> building (the player RUNS; no ETA) |
  | `recover_to_port_overworld` | 10 | anywhere -> port_overworld |
  | `open_world_map` | 7 | port/sea -> world_map |
  | `exit_to_overworld` | 6 | building -> port_overworld |
  | `_depart_from_harbour` | 5 | port -> sea (manual) |
  | `run_sail_to` | 4 | task-level sail |
  | `_tap_depart_button` | 1 | port -> sea (the tap) |
  | `_wait_for_arrival` | 1 | sea -> port |
  | `_navigate_world_map_to_village` | 1 | world_map -> village |
  | `_wait_until_at_sea` | 1 | port -> sea (confirmation) |
  | `commit_departure` | 1 | port -> sea — **already the target shape** |
  | `depart_from_port_via_world_map` | 0 | superseded |

* **10 of `navigate_to_building`'s 13 call sites are layering violations** — task-runner-level
  code reaching for an action:

      LEGITIMATE   brain/plan_actions.py:_handle_navigate_to
                   brain/goals/sail_to.py:_action_navigate_to_harbor
                   brain/goals/clear_cloud.py:_tick_go_to_harbor

      VIOLATIONS   actions/task_runner.py:_navigate_to_market
                   run_event_sell.py x2
                   brain/barter_mission_live.py x5
                   actions/explore_actions.py:explore_port

  `barter_mission_live.gather` is the clearest: it imports `navigate_to_building`, `tap`,
  `MARKET_COORDS` and `buy_to_goal` directly, so it encodes the HOW and has no goal to
  re-evaluate.

---

## Still open

* Does `sub_menu` deserve to be its own activity, or is it `building` in another layout?
* Is the Village Info panel on the world map a different activity from being inside the village
  after Move to Village?
* Blue-gem economics — earned through investment and activities not yet explored. Once
  understood, "confirm any spend except red gems" may need a budget rather than a blanket yes.
* **Battle, recorded but not designed.** `repel_until_spent` is straightforward; `hunt(enemy)`
  needs a pursuit loop across three identification surfaces (world map marker, minimap, fleet
  list) against a moving target, and nobody has looked at the battle screen itself. The fleet
  conditions shown in red are a readable precondition and worth using.
* **A STRATEGIC layer above the task runner is missing, and three things want it.** The task
  runner executes a plan it is given; nothing decides what is worth pursuing.

      "go looking for a fight"   has no destination — the target is a KIND, not a place
      self-grow                  chooses its own objectives
      investment for blue gems   invest at a port's Bureau to earn blue gems

  The third closes an economic loop the barter work has been spending from without modelling:
  trade and barter earn gold, gold invested at the Bureau earns blue gems, and blue gems
  refresh a village's stock — which is what makes the next barter possible. So "should I
  invest here?" is not a task-runner question about the current plan; it is a growth question
  about the next several plans, and it needs state that outlives any of them.

  Likely the next topic once barter is finished (user, 2026-08-25). Worth designing as one
  layer rather than three features, since all three are the same shape: choosing an objective
  from long-lived state rather than being handed one.

* The autonomous self-grow mode has not been checked against the model. `discover_nearby`
  showed that a non-trade use case fits without changes, which is encouraging, but self-grow
  chooses its own objectives rather than being handed one — that is a task-runner question, not
  an activity question, and it may want a layer above the task runner rather than inside it.

## Order of work

1. `tap_exit_to_overworld` — **done** (c995d06): the control changed; the loop is still the
   caller's, marked interim.
2. The 1-caller actions: `_wait_until_at_sea`, `_tap_depart_button`,
   `_navigate_world_map_to_village`, `_wait_for_arrival`. The `_wait_until_*` ones are deleted
   rather than converted — they verify transitions the dispatcher should observe.
3. `exit_to_overworld`'s six callers — move the loop out, leaving only the primitive.
4. Define the use-case interface (precondition, work, result) and the dispatcher.
5. Route the 10 violating call sites through it, so `navigate_to_building` ends with one caller.

Step 4 gates step 5: the violating callers stop reaching for actions when they have something
better to say.


## Known gap: the main menu is an overlay, and you cannot enter a building from it

The main menu is **not a place**. It is an overlay drawn over whichever world you were
already in — the sea or the port overworld — and the world underneath is unchanged. But
while it is up, **no building can be entered**: the port's building list is covered.

`ENTER_BUILDING` therefore has a precondition it does not currently state: *the port
overworld must be showing, with no main menu over it.* Live 2026-08-27 the dispatcher
dispatched `ENTER_BUILDING(market)` while `state == 'main_menu'`, and
`tap_building_entry` went looking for the building list by **tapping three calibrated
port tab-strip coordinates** — on the main menu, where those coordinates are different
controls. One of them opened Placement Setting, and the bot was stranded on a chromed
screen it could not name.

Two things to fix, neither done yet:

1. **State the precondition and check it at dispatch.** `ENTER_BUILDING` from `main_menu`
   is impossible by construction and should be refused there, with the menu closed first —
   and the close *verified*. The `back` that was meant to close it that day did not, and
   nothing checked: nine seconds later the classifier still read `main_menu`.
2. **`tap_building_entry` must confirm where it is BEFORE the first tab tap.** It already
   has the right test — "none of 3 tabs listed buildings" — but it runs it *after* spending
   three taps to gather the evidence. A precondition checked after acting is not a
   precondition.

The escape added 2026-08-27 (`brain/escape.py`) recovers from the consequence; it does not
prevent the cause. See the memory `unnameable-chromed-screens-have-two-exits`.


## Known gap: the world map is searched before it is READ

`_navigate_world_map_to_port` tries strategies in this order:

1. `_try_port_search` — open the port-list panel, type a prefix, read the results
2. coordinate pan — commented "fallback for UNDISCOVERED ports (no search row)"

Nothing looks at the labels **already rendered on the map**, though `_find_port_on_world_map`
is exactly that read and is pure — no taps, no typing, no state change.

Live 2026-08-27, sailing Tripoli → Barcelona with Barcelona plainly in view:

    18:07:43  Typing 'Barc' into search box
    18:08:02  'Barcelona' not found after typing — saw ['19.08','2123','barc','my loc:',…]
    18:08:05  Soft keyboard detected — pressing BACK to dismiss before panning
    18:09:25  'Barcelona' not found on world map (pan_to_port + port-search both failed)
    18:11:43  [pan_to_port] attempt 1/8 (after 0 stride pans): 11 visible port(s)
    18:11:43  FOUND 'Barcelona' @ (1580, 153)
    18:11:47  Matched 'Barcelona': conf=0.97 @ (1581,154)

Zero stride pans, first attempt, confidence 0.97 — it was on screen throughout. The search
cost ~4 minutes and one outright failure before a retry succeeded.

Three problems with the order:

1. **The cheapest and most reliable read runs last.** Reading what is on screen needs no
   interaction; the search needs a panel, typed text, a results list and a keyboard
   dismissal — four failure points, three of which fired.
2. **Searching damages what the fallback needs.** The soft keyboard covers the lower half of
   the map. The explicit "dismiss the keyboard before panning" step is a workaround for
   damage the first strategy caused.
3. **The fallback is carrying a case it was not written for** — the comment says undiscovered
   ports, and it is being used for "the search failed".

Fix: READ FIRST — `_find_port_on_world_map` on the current frame — and fall back to the
search only when the destination is not visible. Same shape as the rest of these: check
before acting, and prefer the observation that costs nothing.

**Second instance, 2026-08-27 21:19 (user):** sailing Bordeaux → Gijón for Pig, with Gijón
fully in view on the map, the bot still opened the port list and typed a prefix:

    21:19:56  [port-search] Searching for 'Gijon'
    21:20:22  Typing 'Gij' (prefix of 'Gijon') into search box @ (304, 144)
    21:20:35  Found 'Gijon' via search @ (214, 200)

~40 seconds for a name already rendered. The search SUCCEEDED here — the Port-tab guard
fixed the correctness bug — so what remains is purely cost: the free reading is still last.
`_find_port_on_world_map` on the current frame is a pure read and already exists; checking
the OmniParser results the bot has just parsed, before opening any panel, would skip the
whole panel-and-keyboard path whenever the destination is on screen.

**The rule for BOTH paths (user, 2026-08-27): LOOK, THEN TYPE, AND SCROLL ONLY AS BACKUP.**

    1. Is it already in the OmniParser results — on the map, or in the open list?  -> tap it.
    2. Otherwise TYPE A PREFIX and search.  Typing is FASTER than scrolling.
    3. SCROLL the list as the BACKUP — for when the soft keyboard breaks the search.
    4. Pan only as a last resort.

Scrolling is not a cheaper middle step: at Hutu Village on 2026-08-27 the row was on neither
the map nor the visible list, and two scrolls cost ~18s where three characters would have
found it at once. It earns its place one rung lower, because the soft keyboard can defeat the
search outright — it covers the lower half of the map and does not always dismiss, as the
"Soft keyboard detected — pressing BACK to dismiss before panning" line above shows. So the
free READ comes first, typing is the method, and scrolling is what gets through when the
keyboard will not.

**What each path gets wrong today.** `_try_village_search` tries, in
order of cost:

    0. ALREADY ON SCREEN?  -> tap it — no tab, no panel, no typing
    1. select the Explore tab (by name)
    2. open the village list -> visible? tap it
                             -> not visible? SCROLL looking for the row
    3. still not visible -> search box, type a PREFIX
    (then pan as a fallback)

Live 2026-08-27 it stopped at step 2 for Svear ("is in the open list — tapping without
searching") and at step 2's scroll for Hutu ("found by scrolling @ (234,425)"), reaching the
keyboard in neither case. So the village path has the right FIRST step (0: already on
screen) but runs its BACKUP ahead of its method: the scroll at step 2 should sit BELOW the
search box at step 3, not above it.

`_try_port_search` has the opposite problem: it opens the panel and types FIRST, and only
looks at the rendered map after that has failed — by which point the soft keyboard is
covering half of it. It needs step 0 added; its typing step is already right.

Neither needs a new mechanism. `_find_port_on_world_map` and the village list's own
visible-row check are both pure reads that already exist; the fix is to run them before any
tap, and to reorder the scroll below the search rather than remove it.
