# Navigation is not a business run

**Status: the rule, stated (2026-09-12).** Written after a shared reader was optimised for one
mode and silently doubled the other's tick.

Two loops drive this bot and they have OPPOSITE costs. Code shared between them must be told
which one it is serving, because what is careful in one is waste in the other.

## The two modes

| | **business run** (`run_barter.py`, `run_task.py`) | **manual navigation** (`tools.run_ai_nav_live`) |
|---|---|---|
| where it is | anywhere: port, market, village, sea, world map | AT SEA, for the whole voyage |
| what interrupts it | daily news, promos, reward cards, the idle lock, arrival gates | **nothing — it assumes no interruptor** |
| what it must read | whatever screen it is on, which it does not know in advance | the **mini-map**, the **steering controls**, the **speed gauge**. Nothing else |
| what moves between ticks | the whole screen, on any tick | nothing. The HUD is fixed for the voyage |
| tick budget | seconds are fine — it is advancing work, not chasing a moving ship | **under 3 s**. A slower tick is a worse navigator |
| how it is driven | `Dispatcher.step()` — perceive, route, one action, hand back | its own control loop over `AiNavPipeline` |

**The tick budget is not a preference.** The ship keeps moving between decisions, so the
interval sets how far it travels blind. At 6 s a fast hull covers enough water on a tight
shoreline to run out of margin — the Jeddah voyage of 2026-09-12 took two collisions on
exactly those stretches, having sailed the rest of the Arabian coast cleanly.

## The rule

> **Navigation assumes no interruptor and reads only the steering, the speed and the mini-map.**
> (user, 2026-09-12)

Everything that follows is a consequence.

**Nothing on the navigation path may call `perceive()` per tick.** The full stack exists to
answer "what screen is this, and is something in the way", and navigation already knows. It
runs `perceive()` ONCE as a warm-up; `_assert_on_sea` has a fast path over the cached nav view
for the same reason, and it works — measured across 134 steering holds on the Jeddah voyage,
`perceive` ran twice, both at startup.

**Nothing on the navigation path may run OmniParser per tick.** A whole-frame parse costs
~2.6 s (measured 2026-09-12). That is one third of a business-run tick and the WHOLE of a
navigation tick.

**Anything the loop needs is CALIBRATED ONCE, at startup.** Not hardcoded — measured, then
kept. That is the contract `MINIMAP_CROP` has always had, and now the speed gauge too:

```
[calibrate] MINIMAP_CROP (1984, 205, 2379, 395) -> (1983, 208, 2377, 395)
[calibrate] speed gauge: 4 cell(s) ((1898,128,1982,198), …)
[calibrate] wheel L=(234, 974) R=(434, 975) rudder=(334, 715)
```

The distinction that matters: **measured once is not the same as remembered.** The old speed
reader derived its crop from four offsets against `MINIMAP_CROP`, and when the UI drifted it
read the inside of the map disc. Calibration re-measures at startup, so it follows the drift;
it simply does not re-measure on every tick.

## How this went wrong, so it is recognisable next time

`read_speed` was changed on 2026-09-12 to locate the gauge through
`vision.region_detectors.overworld_panel` instead of the four offsets. That was RIGHT for the
business run — the offsets had a fallback that landed inside the mini-map, and the panel
detector is structural and correct.

It was wrong for navigation, and the file's own docstring had said so all along:

> `locate=True` … is opt-in because locating costs an OmniParser parse (~2-3s), which the
> manual-navigation loop cannot afford at its sub-1s cadence; that path calibrates
> `MINIMAP_CROP` once at startup instead.

The change made locating unconditional, removing the opt-in the comment describes. The
navigation tick went from under 3 s to 6 s, and nothing failed — it just got slower, which is
why it was not noticed until a voyage steered worse than remembered.

**The shape to watch for: a shared reader made more careful, with only one caller in mind.**
Correctness work on a business-run path lands on the navigation path too, and navigation pays
in the one currency it cannot spare.

## Checklist for touching a shared reader

- Who calls this? If `brain/ai_nav/` or `tools/run_ai_nav_live.py` do, it is on the hot path.
- Does it parse the frame, run a model, or call `perceive()`? If so it cannot run per tick.
- Can the answer be calibrated once? If it is furniture that does not move at sea, it should be.
- Is there a fallback that guesses? For navigation, a failed calibration should be LOUD —
  there is no varied-screen case to degrade into.

## Related

- `docs/one_loop_task_drives_state.md` — the business-run loop and why it has no sub-loops.
- `docs/navigation_models_status.md` — the mini-map and shoreline models the nav tick uses.
- `actions/orientation.py` — `CANONICAL_ROTATION` is 270 and the business run enforces it;
  the navigation constants measure correct at ROTATION_90 (wheel, mini-map and gauge alike,
  2026-09-12). The two disagree and it is unresolved; `run_ai_nav_live` has no orientation
  guard at all, which is how a run at the wrong rotation tapped the combat button.
