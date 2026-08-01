# Agentic Navigation Harness — Design Notes

Design sketch for turning today's per-tick reactive navigation loop into a
multi-tier agentic harness, modelled after how a good coding agent (e.g.
Claude Code) tackles long tasks.  Written 2026-07-15 after the
Cairo→Lake-Victoria voyage exposed the limits of a single reactive tier.

## The two clocks

Programming with a coding agent is a **slow deliberate loop**: read state →
think → act → verify → loop.  Each step can pause tens of seconds to think
before touching anything.

Steering a ship is a **fast reactive loop**: perception → tap → repeat every
~100–200 ms.

A good harness for the Nile is **two loops nested**, not one loop that does
everything.

| Layer         | Frequency        | Job                                              | Analogue in a coding agent   |
|---------------|------------------|--------------------------------------------------|------------------------------|
| **Reflex**    | every tick (~200 ms) | steering, immediate collision-avoid          | keystrokes / tool calls      |
| **Tactical**  | every 5–30 s     | update waypoint plan, detect stuck patterns      | thinking between tool calls  |
| **Strategic** | every 1–5 min or on trigger | goal change, route re-selection, "we're lost" recovery | "wait, let me re-read the task" moments |

Today the bot has only the **Reflex** layer running well.  The Tactical
layer exists but is thin — the planner emits one waypoint per tick with no
persistence.  The Strategic layer is essentially absent.

## Concrete pieces to build (in priority order)

### 1. A persistent plan artifact — like a TODO list

Not just `commit_direction = 180`.  A structured object:

```python
plan = [
    {"leg": "clear_cairo_channel", "waypoints": [...], "done": False,
     "success": "lat < 28 and topology != channel"},
    {"leg": "descend_upper_nile",  "waypoints": [...], "done": False, ...},
    {"leg": "enter_lake_victoria", "waypoints": [...], "done": False, ...},
]
```

Updated by the Tactical layer, consumed by the Reflex layer.  Each leg has
explicit success criteria.  When a leg completes, log it and load the next.

**Why it fixes real bugs**: Y-tip thrash exists because no state tracks
"I've explored this branch before."  A leg-tracker with branch memory is
basically Trémaux for free.

### 2. A verification step after every maneuver — like `run the tests`

Every time the planner emits a waypoint, before committing to it:

- Is this waypoint inside the mini-map bounds?
- Is it on water pixels in the current mask?
- Is it reachable from ship position without crossing land?

If any answer is no, reject and fall back to a safer default.  Catches the
t324 off-crop waypoint (`y=220` in a 193-tall map) in one line of code.

### 3. A "watcher" sub-agent running slowly — like parallel Agent calls

A separate thread/process that looks at the last 20–50 ticks (not just the
current one) and answers questions the reflex loop can't:

- "Has the ship been bouncing?" — speed drops ≥ 70 % pattern
  (t536–t541 case in the 2026-07-14 voyage)
- "Has the ship been circling in place?" — Y-tip thrash
- "Is the screen still the sea view?" — village-entry catch
  (t904 case in the 2026-07-14 voyage — mini-map went 0 px visible for
  8 + ticks while the pipeline kept issuing `hold_right` into a village
  screen)
- "Have I made no progress toward the goal for N minutes?"

When the watcher fires, it interrupts and either replans, hands off to a
bigger model for a big-think, or aborts the mission.

### 4. A "big think" escape hatch — like extended thinking

When the watcher says "we're stuck," don't try to fix it in the reflex
loop.  Send the last 30 frames + trajectory + goal to a slow model
(Claude Vision, or a local VLM) with the prompt "what's going on and what
should we do?"  Latency budget is 5–30 s — fine because the ship isn't
making progress anyway.

`MoondreamTactical` (`--tactical moondream`) is already scaffolded for
this.  It's the right idea, currently under-used.

### 5. Memory across voyages — like project-memory files

Every voyage:

> at (lat 8.08, lon 31.27) the ship bounced 3 times because the channel
> narrows and the waypoint always lands on the north bank

Save it.  Next voyage through that region, load the note before the
reflex loop starts.  Over time the bot builds a Nile atlas of gotchas
that no amount of pure perception can substitute for.

## Architecture change

```
┌─ Strategic  (async, 1–5 min)  ──────────────────────┐
│  Load plan  •  React to watcher alerts  •  Big     │
│  think when stuck  •  Update leg list               │
└──────────────────┬──────────────────────────────────┘
                   │ writes: current_leg, waypoint_horizon,
                   │         goal_pose, replan_flag
┌──────────────────▼──────────────────────────────────┐
│  Tactical  (every N ticks, ~5–30 s)                 │
│  Compute next 3–5 waypoints  •  Verify vs mask     │
│  Emit trajectory to Reflex                          │
└──────────────────┬──────────────────────────────────┘
                   │ writes: trajectory
┌──────────────────▼──────────────────────────────────┐
│  Reflex  (every tick, ~100–200 ms)  —  TODAY        │
│  Perception  •  Steering  •  Physics-reject        │
└─────────────────────────────────────────────────────┘

┌─ Watcher  (runs alongside, every ~5 s)  ────────────┐
│  Bounce detector  •  Stuck detector  •  Screen     │
│  detector  •  No-progress alarm                     │
│  → fires "replan_flag" or "abort" upward            │
└─────────────────────────────────────────────────────┘
```

Most of the scaffold exists already:

- `brain/ai_nav/layers/strategic.py` — the Strategic slot
- `--tactical` (`MoondreamTactical`, `NoOpTactical`) — the Tactical slot
- `memory/` — the cross-voyage memory substrate

The gap is **wiring them into a proper hierarchy**, where:

- the Reflex loop trusts the Tactical layer to hand it a **plan**, not a
  single next waypoint;
- the Watcher can pause the Reflex loop and force a Big Think;
- the Strategic layer owns the plan-of-plans and can rewrite it.

## Suggested build order

| # | Item                    | Effort | Fixes                                                     |
|---|-------------------------|--------|-----------------------------------------------------------|
| 1 | Waypoint verification   | ~1 day | off-crop waypoint (t324); waypoint-on-land bounces        |
| 2 | Watcher — bounce + screen | ~2–3 days | multi-bounce cascades; village-entry stuck (t904) |
| 3 | Persistent plan artifact | ~3–5 days | Y-tip thrash; commit_direction oscillation between mission and default |
| 4 | Big-think tactical      | ~1 week    | recovery from cases 1–3 can't handle                     |
| 5 | Cross-voyage memory     | ongoing    | repeated failures at same lat/lon become one-shot fixes  |

After (1) and (2) the classical planner may be good enough that a learned
planner becomes a nice-to-have rather than a rescue.

## Why this differs from "just build a bigger perception model"

| | Coding agent (slow, verifiable)     | Ship navigation (fast, noisy)             |
|---|-------------------------------------|-------------------------------------------|
| Latency budget      | seconds OK                | 50–200 ms per tick                        |
| Grounding           | filesystem + shell (deterministic) | noisy pixels + game physics       |
| Verification        | run tests, read output    | no ground truth mid-voyage                |
| Chain length        | hundreds of tool calls    | must decide *now*                         |

What makes a coding agent effective is not model size — it's the **loop**.
For the bot, the equivalent isn't a bigger perception model; it's giving
the planner more chances to correct itself (temporal memory, trajectory
prediction, sensor-fusion loop, escape hatches).

For big-picture route reasoning ("do I go through Suez or around Africa"),
a slow LLM-style call once per hour makes sense.  For "avoid this rock in
front of me right now," it's the small 4-frame-history CNN discussed in
the trajectory-prediction sketch (see `docs/ai_navigation_landscape.md`).

## Related

- `docs/exploration_navigation_layers.md` — layer split (perception /
  mapping / planning) that the harness sits on top of
- `docs/architecture_overview.md` — current single-tier reactive design
- `memory/project_planner_stale_waypoint_land_heavy_2026-07-02.md` —
  off-crop waypoint pattern (motivates item #2)
- `memory/project_speed_is_bounce_detector.md` — bounce-detection signal
  (motivates item #3 watcher)
- `memory/feedback_motion_bearing_unreliable_during_bounces.md` —
  perception audits must filter bounce ticks
