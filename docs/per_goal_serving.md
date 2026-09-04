# Where a goal can be done — an open design note

**Status: SUPERSEDED by `docs/activity_capability_registry.md` (2026-08-30),** which answers this from the activity's side — what work can be done here — and adds the task/support split that removes most of the cases below. Kept for the evidence and the four-way comparison table.

**Originally: PROPOSED, not decided.** Written 2026-08-30 after the third live wedge of the same
shape in one day. Recorded so the next person to hit it starts from here rather than from the
symptom.

## The observation

An activity declares two things:

```python
class AshoreActivity:
    SERVES = ("port_overworld", "village")     # where the ACTIVITY runs
    GOALS  = (ArriveAshore, ReadHold)          # what it can be ASKED for
```

`to_intent` then asks `where in SERVES` and, if so, dispatches nothing — *"already where the
work happens"*. That is true for one of those goals and false for the other:

| goal | port_overworld | sea | village | building / sub_menu |
|---|---|---|---|---|
| `ArriveAshore` | ✓ | — | ✓ | — |
| `ReadHold` (needs the ☰) | ✓ | ✓ | ✗ | ✗ |
| `ChooseDestination` (needs the globe) | ✓ | ✓ | ✗ | ✗ |

One `SERVES` tuple cannot say this. `ReadHold` is served at sea as well as at a port — the ☰
is on both overworlds — and is not served in a village, which `SERVES` currently claims.

## What it cost, live 2026-08-29

Three wedges, one pattern — a state serves an activity but not every goal that activity carries:

1. `OPEN_WORLD_MAP` dispatched from the market's Sell submenu. The globe is on the overworlds;
   `tap_world_map_control` answered *"no world-map control on 'sub_menu'"* on every tick until
   the no-progress guard stopped the run.
2. `OPEN_WORLD_MAP` dispatched at Svear Village, with the barter done and the Birch Tree
   aboard. A village screen is chromed — back arrow, the Explore/Gifting/Loot/Recruit/Barter
   menu, no ☰ and no globe. The run stopped one leg from finishing.
3. `ReadHold` dispatched at Svear on the next start. *"cannot open the main menu from
   'village' (no ☰ there)"* — dead on tick 2, before touching the game.

Each was patched where it surfaced: (1) and (2) by widening `_is_inside`, (3) by removing the
read altogether (the plan discarded its cargo number and capacity is a property of the ship).
None of those patches wrote down the fact underneath.

## The shape proposed in the moment

```python
SERVES_FOR = {ArriveAshore: ("port_overworld", "village"),
              ReadHold:     ("port_overworld", "sea")}
```

`to_intent` asks "does this state serve THIS GOAL" instead of "does it serve this activity".
It is a small change to the activity contract and the dispatcher's resolve step, and it makes
each of the three wedges above a routing answer rather than a live discovery.

## Why it may not be the right model

The user's note when this was raised: *"I think we may need to consider this deeper to find a
good overall design."* Two reasons to think the table above is the symptom, not the cure:

**The fact is about CONTROLS, not about goals.** `ReadHold` and `ChooseDestination` have the
same table not by coincidence — both need something the overworlds have and the chromed
screens do not (the ☰, the globe). A goal-to-states map re-states, once per goal, a fact that
belongs to the screen. Add a fourth goal needing the ☰ and it is copied a fourth time.

**The knowledge is already duplicated, and that is what wedged.** Two places encode "the globe
is on the overworlds": `to_intent`'s `_is_inside`, and `tap_world_map_control`'s `loc ==`
branches. They disagreed — routing said OPEN_WORLD_MAP, the primitive refused — and neither
could see the other. `SERVES_FOR` would add a *third* copy.

The alternative worth designing: **states declare the controls they carry; goals declare the
control they need.** One fact per screen, asked by both the router and the primitive.

```
port_overworld : {hamburger, globe, building_list}
sea            : {hamburger, minimap}
port_map       : {world_map_button}
village        : {back}                       # no hamburger, no globe
building:*     : {back}
```

Then `to_intent` routes out when the needed control is absent, `tap_world_map_control` stops
carrying its own per-state branches, and adding a screen means describing it once. It also
gives `_is_inside` — currently "anywhere with a Back and no ☰", a rule whose *words* were
right while its prefix implementation missed the village — something real to test against.

## Open questions

- Where do control declarations live: on the state (a perception-layer fact) or in a table
  beside `NAV_STATES`? A screen's controls are observable, which argues for perception.
- Is `port_map` a state the classifier actually emits? A real port-map frame classifies as
  `port_overworld`, so the `port_map` branch added to `tap_world_map_control` is unverified.
- Does this subsume `_TAIL_CAN_SAIL_FROM` and `_NEVER_BACK_FROM` in `brain/intents.py`?

  Both are used in exactly ONE place — the `ClearOfTheVillage` branch — and are OR'd into the
  same answer for OPPOSITE reasons:

  ```python
  if where in _TAIL_CAN_SAIL_FROM or where in _NEVER_BACK_FROM:
      return None                  # clear already, or a screen Back must not touch
  ```

  `_TAIL_CAN_SAIL_FROM` means *"already clear, nothing to leave"*; `_NEVER_BACK_FROM` means
  *"Back here raises Exit Game?"* (live 2026-08-26 it pressed twice at Stockholm and stood one
  positive tap from quitting). `port_overworld` is in BOTH, for different reasons, and the
  branch cannot tell which applied.

  And `_TAIL_CAN_SAIL_FROM`'s own comment gives the game away — *"Where the world map can be
  opened from: at sea, on the map already, or standing on a port overworld — the globe is
  right there."* That is a FOURTH copy of the globe fact, and the copies do not agree:

  | | sea | port_overworld | port_map | world_map | village | building / sub_menu |
  |---|---|---|---|---|---|---|
  | `_TAIL_CAN_SAIL_FROM` | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |
  | `tap_world_map_control` | ✓ | ✓ | ✓ (unverified) | — | ✗ | ✗ |
  | `_is_inside` (inverse) | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |

  One fact — "this screen affords the globe" — written three ways ("can sail from", "has a
  control to tap", "is not inside") across three hand-maintained lists. Under a controls model
  the branch reads: *ClearOfTheVillage is satisfied when the screen affords sailing; Back is
  available when the screen affords back* — and `port_overworld` appearing twice stops being
  ambiguous, because "affords the globe" and "Back exits the game" become separate facts about
  one screen rather than two list memberships that happen to mean `None`.
