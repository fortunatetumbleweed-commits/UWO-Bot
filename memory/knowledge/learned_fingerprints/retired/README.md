# Retired learned fingerprints

Learned fingerprints that have been PROMOTED to canonical states, or that duplicated one.

They are kept rather than deleted because each records a real screen the bot met, with the
Claude description that identified it — useful if a promotion later needs revisiting. They are
moved out of the loaded directory so they cannot shadow the canonical fingerprint.

## Promoted to a state (2026-08-26)

- `learned_on_standby_at_sea_slide_up_to_unlock`
- `learned_london_slide_up_to_unlock`

Both are the game's idle standby lock. They named the screen correctly on every look and named
it something the FSM had no node for, so nothing could route out of it — at Svear Village the
mission re-perceived one for 34 minutes across two attempts and aborted with a full hold.

They are now the canonical state `idle_lock`: a node in `memory/knowledge/fsm/states.json` with
a `swipe_up` exit to `null`, the fingerprint in `vision/state_fingerprints_data.py`, and the
activity in `brain/activities/idle_lock.py`.

Note there were TWO of them, one per place the lock was met — which is the argument for a
single state rather than per-location variants. A third would have been learned at Svear.

## Retired wholesale — the layer no longer classifies (2026-09-08)

The remaining twelve were moved here and `vision/state_fingerprints_data.py` no longer
auto-loads this directory. The discovery hook still WRITES candidates; only the registration
as classifiers is off.

Measured over every session log the repo holds: 134 learned matches in 16,176 classifications
(0.83%), none ever acted on, because no activity serves a `learned_*` state —
`default_activities()` has 19 entries and not one is learned. 121 of the 134 were screens that
already have an owner under their proper name (the market Purchase page 78, the idle lock 43);
the rest were two notice popups.

What they were keyed on says the rest:

| fingerprint | matched on |
|---|---|
| `learned_purchase_cargo` | `purchase`, `sell` — two words on every market page |
| `learned_cargo_kris` | `copper ore`, `kris` — two goods on the shelf |
| `learned_barter_2_780` | `2,780` — the red-gem count in the top bar |
| `learned_this_is_a_port`, `learned_this_is_the_port` | `slide up to unlock` — the idle lock, twice |
| `learned_the_screen_is_almost`, `..._is_entirely` | nothing; named from the opening words of Claude's prose |

A shelf changes per port and per restock, and a gem count changes when you spend one. This is
`identify-by-association-not-dimension` applied to the most volatile text on the screen.

A wrong name is also worse than no name: an unmatched frame is `unknown`, which IS served.
`learned_cargo_kris` won on the 2026-09-08 Purchase page only because `sub_menu` could not
name its own screen — see `SUB_MENU_TITLE` in `vision/state_fingerprints_data.py`. Fix the
vacuum, and there is nothing for this layer to fill.

New candidates written from here on are a discovery LOG. Promote one by hand, as the idle
lock was, or leave it.
