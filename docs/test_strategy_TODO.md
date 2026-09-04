# Test suite: what to run when, and what to retire

**Status:** plan, 2026-08-28. Not yet implemented.
**Why now:** a day of running the full suite (~3,400 tests, 17–20 min) after every change,
including a stretch where repeated launches piled up 41 concurrent pytest processes and
starved each other. The suite is not uniformly slow — a handful of tests dominate — so most
of that time bought nothing.

## 1. Where the time actually goes

Measured on a mid-size slice, 2026-08-28:

    92.6s  test_globe_tap_is_corroborated::test_it_still_taps_the_globe_at_a_real_port
    60.4s  test_mission_runner::test_a_short_supply_stops_the_mission_before_the_village_leg
    57.7s  test_the_device_defaults_are_reachable::test_the_village_defaults
     6.3s  test_bootstrap_establishes_position::test_a_screen_nothing_can_clear...
     5.2s  test_the_migrated_nodes_wire_up::BarterRunsFromTheVillage::*
     3.1s  test_village_badge_recovered_from_its_tile::*
     ...   everything else, well under a second

**Three tests = 210 seconds.** They are slow for a reason — they load models, parse real
frames, or resolve every default import — but they do not need to run on a logic change.

Note even the "pure logic" tests cost seconds, because importing `default_activities()`
pulls in the whole activity graph and its models. That is worth a separate look: an import
side-effect is paid by every test that touches the dispatcher.

## 2. Proposed tiers

    fast     (default)   everything not marked slow           seconds      every edit
    slow     (marked)    models, real frames, import sweeps    ~4 min      perception changes
    stages   (marked)    the frame corpus in tests/stage_suite ~2 min      reader changes
    full                 all of it                             ~18 min     see below

**THE TRIGGER FOR `full` IS THE SCOPE OF THE CHANGE, NOT THE FACT THAT A LIVE RUN FOLLOWS**
(user, 2026-08-28). This restates CLAUDE.md's *Scoped Test Runs*, which was already the rule
and which a day of full-suite runs ignored:

  * **Localised change** — everything inside the market, or inside one reader — run THAT
    area's tests and go live. A full suite before it buys nothing: no other module was
    touched, so no other module can have broken.
  * **Major refactor** — a concept introduced or removed across the codebase. Today
    qualifies: activities as contexts, several sub-loops deleted, the activity registry
    changed shape from one activity per state to a list. That is the case `full` exists for,
    and it is rare.

The distinguishing question is not "how risky does this feel?" but **"could a module I did
not edit behave differently now?"** Changing `MarketActivity` cannot alter the village.
Changing the registry's shape alters everything that reads it — which is exactly how a single
`AshoreActivity` registration silently overwrote `VillageActivity`, and only a broad run
would have caught it.

Mechanically: `@pytest.mark.slow` on anything over ~5s, `addopts = -m "not slow"` by default,
and `-m slow` / `--all` when it matters. The markers are the cheap part; deciding WHICH tests
deserve which mark is the work.

## 3. Retire or rewrite — candidates seen today

Several tests were found asserting contracts that no longer exist. Each was rewritten rather
than deleted, but they suggest a systematic pass is overdue:

- **Tests pinning behaviour that was the bug.**
  `test_blocked_perception_is_not_a_wrong_place` asserted "the recovery must survive for the
  case it exists for" — protecting the navigation that sailed the fleet home mid-voyage.
- **Two files asserting one rule through different literals.**
  The world-map tab guard was pinned in two places; when the call sites moved to
  `require_world_map_tab`, one file broke and the other passed. Consolidated, but the shape
  recurs.
- **Stubs that agree with a mistake.**
  `classify()` read `good`/`out` (from the LOG's formatting) instead of `selected_good`/
  `output_quantity`, and every stub used the same wrong names, so 27 tests agreed with the
  bug. The fix — assert against the real dataclass, not a stub — should be applied wherever
  a stub stands in for a typed object.
- **Tests for deleted machinery.** `run_barter_phase`, `_await_route_arrival` and
  `run_phase_fn` are gone or dead; their coverage moved, and the leftovers should follow.

## 4. What "obsolete" means here, concretely

A test earns removal when the behaviour it protects no longer exists AND nothing else
asserts the rule behind it. A test earns REWRITING when the rule survives but the mechanism
changed — which was every case today. Deleting those would have lost the reason, and the
reasons are most of what these tests carry: nearly every one names the live incident it came
from.


## Future enhancement: the `barter_mission_live` façade (deferred 2026-08-28)

`brain/barter_mission_live.py` is a task module that still reaches the UI — 8 UI imports, 6
perception calls, 5 `run_goal` calls. Two otherwise-clean modules, `brain/barter_command.py`
and `brain/barter_task.py`, reach the UI THROUGH it. That is how `barter_command` came to
measure 0 UI imports while using the entire barter panel: a guard that looks one hop is
defeated by a façade.

**Deferred on purpose** (user: "lets mark this barter_mission_live facade as future
enhancement, and log all the calls and see if they cause issues"). The market work is the
bulk of it and is out of scope for now.

**Held in place, not merely noted:**

- `tests/test_the_layering_is_enforced.py::KNOWN_SECOND_HOP` declares who reaches through it,
  so the set cannot grow.
- `FACADES` records its UI-import count, so that cannot grow either.
- Every call through it logs `[facade] <fn> <- <caller>`.

**How to decide when to do it.** After a live run:

```bash
python tools/facade_calls.py <logfile>
```

A quiet report is evidence the deferral is safe. A noisy one names the first thing to move,
and the CALLERS matter more than the counts — a function called only from inside the façade
is internal plumbing, while one called from `barter_command` or `barter_task` is a clean
module reaching the UI through it.

**What is left, and who owns each piece when it moves:**

| still in the façade | owner when it moves |
|---|---|
| `current_position`, `_current_port` | the dispatcher already perceives this |
| `_at_a_market_port`, `supply_verify` | the same question again |
| `gather`, `sell`, `sell_surplus`, `barter` | market work orders |
| `_enter_market_at`, `_exit_market_to_overworld` | transitions the dispatcher should route |

**Known dead code inside it:** `_await_route_arrival` has had no callers since 2026-08-28 and
is marked "delete on the next pass".
