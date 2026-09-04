# Weighted signature scoring

**Status:** future project, agreed 2026-08-26. Not started.
**Interim rule in force:** the family CNN is trusted outright above a confidence of
**0.8** (`brain.perceive._FAMILY_TRUSTED_MIN`); only below that are signatures consulted.
And a `learned_*` fingerprint never outranks a standard one, whatever its confidence
(`vision.state_fingerprints.classify_via_registry`).

## What is wrong today

A fingerprint's confidence is a **fire ratio** — how many of its signals fired, over how many
it has. So a fingerprint carrying ONE signal fires 1/1 = 100% and is graded HIGH: the top
band, on the thinnest evidence there is. `evaluate_fingerprint`'s own docstring has always
described a stricter rule, which the code never implemented:

    Confidence:
      - all positives fire AND >= 2 positives total → HIGH      <- the ">= 2" is not enforced

Live 2026-08-26 that is how the screen came back as
`learned_updates_august_10_mon_update_advance` — an announcement popup, which the FSM has no
node for — and `open_world_map` spent three attempts on a state it could do nothing with.

## Why it was not simply switched on

Ten of the seventeen registered fingerprints carry exactly one signal:

    idle_lock, main_menu, village, building, sub_menu, port_overworld, sea,
    port_loading, loading   (+ every learned_* one)

against `world_map` (4 signals) and `port_map` (2). Enforcing "≥2 for HIGH" demotes every core
state at once — `sea`, `building`, `port_overworld` — to fix a problem caused by the learned
ones. Six tests assert HIGH for exactly those states, and they are right to: "this is the sea,
because the sea HUD tokens are present" IS strong evidence, and the single-signal grading is
what fails to express that.

## What the work actually is

Not a threshold change. **Give the core fingerprints more signals to weigh**, then let the
count carry weight:

1. **Author additional signals** for the single-signal states, so their confidence reflects
   real evidence — `sea` has a HUD, a minimap and no chrome; `building` has a title, a back
   arrow and a right panel.
2. **Weight the signals.** Not every signal is equal evidence: a unique label cluster says
   more than "some icons are present". A weight per signal, summed, beats counting.
3. **Then** enforce a minimum weight for HIGH, and let several signals outvote one — which is
   the point (user, 2026-08-26: "give weight for them so no single signature overrules all
   others").

Step 1 is the bulk of it and is where the value is: a state identified by one string is fragile
whatever the scoring does.

## Related

- `docs/screen_knowledge_as_kb.md` — panels as GROUPS matched by content, the direction this
  should eventually merge into.
- The port map is a known hard case, deliberately deferred: it is a translucent overlay on the
  port overworld and its icons are mostly building icons, so it looks like a port_overworld to
  a whole-image classifier. `port_map` is not one of the CNN's five classes, and it is read as
  `port_overworld` at confidence 1.00. It is identified for now by the one thing the world map
  lacks — its own "World Map" globe button.
