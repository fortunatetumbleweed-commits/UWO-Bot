# Agent Memory & Architecture — a stocktake

Status: discussion captured 2026-05-18. High-level only — no specific
schemas or code yet.

The standard agent-memory taxonomy maps to five layers.  The bot today
has parts of three of them.  This doc names the layers, marks what
exists, and lists what is missing before the bot can plan, reason, and
grow the company autonomously.

## The five memory layers

### 1. Working memory (tick-scoped)
What the agent is sensing *right now*.  For this bot: the current
frame, the OmniParser element list, the current `SceneModel`.
Lives for one `perceive()` cycle and is discarded.  **Have this.**

### 2. Semantic memory / KB (facts about the world)
Static-ish facts that the agent has learned about its environment.
"Amsterdam has a Bureau."  "Pacific requires high seaworthiness."
"This is what a Castle does."  Revised when new evidence comes in.

The folder `memory/knowledge/` is almost entirely this layer —
`ports/`, `building_types/`, `buildings/`, `fingerprints/`, `fsm/`,
`control/ui_signals.json`.  **Strongest part of the stack.**

### 3. Procedural memory (how to do things)
Reusable skills / policies.  `flows.json`, `learned_recoveries.json`,
the planner's edge metadata.  **Have this**, but it is currently
mingled with the KB in the same folder.  Semantic facts and
procedural skills have different update semantics (facts get revised,
skills get versioned and evaluated), so this is a smell rather than a
bug — fine for now.

### 4. Episodic memory (what happened to me)
A queryable log of past attempts and their outcomes.  "Tried
Diu→Aceh on May 5, profit 412k."  "The recruit-crew loop has failed
4× this week against this fingerprint."

The bot has *raw* episodes (session logs, `voyage_log.jsonl`,
`trade_log/`) but they aren't structured for retrieval.  The bot
can't ask "what happened the last 3 times I tried this?".
**Partially present, not retrievable.**  This gap is what makes the
bot keep stepping on the same rake — the recruit-crew incident is
the canonical example.

### 5. Identity / goal / self-model
What the agent *is* and what it *wants*.

  - Top-level objectives ("grow the company")
  - A hierarchy of sub-goals
  - The agent's own resource state — ducats, fleet, current port,
    mate skills, cargo — as a first-class object distinct from raw
    game state.

Today the goals live in YAML task files; the bot's self-state is
reconstructed from screen reads on every tick.  There is no
persistent "I am pursuing goal G, I committed to plan P at step 3".
**Largely missing.**  This is exactly what a planner needs.

## What's missing, in priority order

### a. Self / agent state  *(cheap, big payoff)*
One persistent record per session:

  - current location (port / sea / building)
  - ducats, blue gems, energy
  - cargo manifest
  - fleet composition + stats (seaworthiness, momentum, capacity)
  - supply days remaining
  - active major trends the bot has noticed
  - last-update timestamp per field

Right now this is re-derived every tick from pixels, which is why a
single OCR misread breaks decisions downstream.  A persistent
self-state makes perception *update* the state instead of *replace*
it, and gives every other layer a stable thing to read.

### b. Goal stack with provenance  *(cheap, enables planning)*
Example:

```
Goal: 10M ducats by end-of-month
 └─ Sub-goal: complete 5 Caribbean runs
     └─ Sub-goal: depart London for Port Royal
         └─ Sub-goal: be at the harbour with full supply
```

Each entry knows its parent and its success criterion.  The planner
walks this stack; the recovery layer reads it to decide what to
resume after an interruption.

### c. Episodic memory with indexes  *(medium)*
Append-only events keyed by structure (port, action, outcome,
ducats_delta, timestamp).  Enables:

  - "show me my last 10 attempts at this flow"
  - "what was the profit on London→Port Royal the last time?"
  - "have I tried this market this week?"

Feeds reflection (layer e).  Without it the bot is amnesic across
sessions — every restart re-discovers the same lessons.

### d. World model / predictive model  *(medium, the planning core)*
KB facts composed into

> *if I take action A in state S, expected outcome E with cost C*

Most planners need this.  Could start very simply:

  - voyage time   ≈ distance / fleet speed
  - profit / unit ≈ sell_price − buy_price (minus tax)
  - supply usage  ≈ days × crew_size × consumption_rate

Refined by episodic memory: actual outcomes update the predictions.
This is what turns the bot from a reactive policy executor into an
agent that can weigh alternatives before acting.

### e. Meta-cognition / reflection  *(later)*
A periodic pass over recent episodes that updates beliefs:

  - "The Diu market has been unprofitable for 3 visits — depreciate
    its estimated profitability."
  - "The fingerprint for sub_menu/recruit fires on port_overworld 8%
    of the time — flag it for cleanup."
  - "I tend to misread `Den Helder` as `Montpellier` — bias the OCR
    correction."

This is how the bot gets smarter without code changes.  Cheap to
add once episodic memory is structured.

## The concrete framing

The gap is easiest to see by asking what the bot knows after a crash
mid-trade-run.

**Today:** "I'm at some port" — reconstructed from pixels.

**With the missing pieces:**

> *I was halfway through London → Port Royal.  My next planned action
> was 'sell at Port Royal Market'.  I have 2.3M ducats in cargo.  Last
> visit to that market was 12 days ago at 108% price index.  The
> parent goal is 'complete 5 Caribbean runs' and this is run 2 of 5.*

That is the difference between a reactive bot and one that can plan,
recover sensibly, and grow.

## Bridges between "navigate the UI with confidence" and "plan, reason, grow"

The current focus is making UI navigation reliable.  The next focus is
the memory/planning layers above.  Three bridges sit between them —
small but load-bearing.  Without them the planning layer ends up
duct-taped to the current ad-hoc perception/action code and inherits
all of its fragility.

### Bridge 1 — Stable perception → state contract

`perceive()` should produce one immutable, schema'd `BotObservation`
per tick that *all* consumers (flows, planner, recovery, learning
hooks) read.  Today every decision reads pixels and OmniParser
output ad-hoc and each consumer interprets it locally.

The SceneModel work is heading in this direction but isn't there
yet.  Without a canonical observation:

  - episodic memory has nothing well-typed to record
  - the world model has no "observed" to compare its "predicted"
    against
  - reflection cannot diff what the bot believed vs what it saw

### Bridge 2 — Action outcome detection

Every action should declare an expected post-condition ("after this
tap I expect `sub_menu:purchase`") and the bot should record
observed vs expected.

Today actions return success/failure of the *tap mechanism* — not
"did this advance the goal".  Success is implicit and noisy: the
next tick maybe shows progress, maybe not.

Adding declared post-conditions is a small change but it is the
prerequisite for:

  - **episodic memory** — outcome records need a definition of
    outcome
  - **world model** — predictions need actuals to learn from
  - **plan completeness** — would have caught the recruit-crew
    failure where a tap succeeded mechanically but produced no
    transaction

### Bridge 3 — Reconcile units of work

There are at least four overlapping "thing to do" abstractions
today: YAML tasks, planner plans (state-graph edges), flows
(committed transactions), recoveries (learned dismissal sequences).
Each has its own runner and its own status conventions.

Before stacking a goal hierarchy on top, these should collapse into
one tree with a single shared notion of `started / committed /
completed / failed`.  Otherwise the goal stack becomes a fifth layer
rather than the top of a clean tree, and reflection / episodic
memory cannot tell *which* layer a record belongs to.

### Less urgent

  - **Confidence-aware decisions.** SceneModel emits confidence;
    the rest of the bot mostly ignores it.  Planning will eventually
    need to reason about uncertainty, but you can ship a goal stack
    without it.
  - **Persistent self-state.** Per layer (a) above — important, but
    only becomes a *blocker* once goals exist.  While the focus is
    UI confidence, rebuilding state from pixels each tick is
    acceptable.

### Dependency order

The minimum bridge set, in order:

```
observation contract  →  outcome detection  →  unit-of-work consolidation
```

That is the seam where "navigates with confidence" meets "plans and
grows" cleanly.

## Adjacent dimensions (orthogonal to the five layers)

Worth naming for completeness; not urgent yet:

  - **Spatial memory** — world map, port adjacency, routes, distances.
    Lives inside semantic memory today.
  - **Temporal memory** — when things happened, decay rates for stale
    prices.  Episodic memory handles this once it exists.
  - **Social memory** — other players, guild membership, NPC
    relationships.  Mostly irrelevant for solo trading; matters for
    Combat / Union features later.

## See also

  - `memory/knowledge/` — the semantic-memory tree
  - `memory/knowledge/fsm/` — procedural memory (flows, transitions)
  - `brain/goals/` — early scaffolding for the goal stack (placeholder)
  - `docs/scene_model_design.md` — Stage-2 deterministic perception
  - `docs/temporal_scene_classifier.md` — Stage-1 family classifier
  - `docs/planner_architecture.md` — current planner design (state-graph BFS)
