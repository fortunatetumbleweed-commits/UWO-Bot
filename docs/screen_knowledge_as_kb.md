# Screen knowledge as a KB — panels, not loose elements

**Status: DESIGN DIRECTION (user, 2026-08-24). Not built. Recorded so the current
word-matching workarounds are understood as workarounds.**

## The idea

What is on screen is **not random, and not merely located** — it is organised into GROUPS,
and each group is associated with certain screens and certain regions of them. The text and
graphics in a group need to be **matched, or at least roughly matched**, to what that group
is known to contain.

    screen  →  regions  →  panels (groups with a FUNCTION)  →  fields

The long-term aim is a **game knowledge base** that describes this, with an AI model
inferring against it. Specific things change with game updates — wording, layout, iconography
— but the general FUNCTIONALITY does not: a village always has a menu of things you can do
there; a market always has goods with prices; an info panel always reports status. A KB plus
inference degrades gracefully when the specifics move; hardcoded specifics do not.

## Why this came up

The left-menu detector decides what a cluster of rows IS. Two bad versions:

1. **By dimension** — "median member ≥ 260px wide ⇒ a reward widget". OmniParser boxed the
   same San Village rows differently between two captures (median width 121 → 367), so the
   real menu was discarded and the bot reported "no Barter item in the left menu" with
   Explore/Gifting/Loot/Recruit Crew/Barter plainly on screen.
2. **By word, per element** (what the code does today) — reject any row saying "Achievement
   Reward", "Weekly Reward", "Village Status", "Amity Effect". Correct in effect, but the
   wrong granularity: those rows are ONE PANEL, and the code is recognising it by
   pattern-matching its individual words. It holds only while the wording holds.

Under the panel model, neither question arises. The info panel is a group in the centre
region of the Village Info screen; the menu is a group in the left region. Nothing needs to be
measured, and no individual word carries the decision.

## Screen-identity cues that already work this way

These say which KIND of screen you are on, which says what may exist there:

| Cue | Means |
|---|---|
| Mini-map present | overworld (port overworld or sea) |
| Globe icon | in port |
| Right panel translucent | overworld |
| Right panel solid | chromed screen (building / sub-menu) |
| Left menu under the title | chromed — villages excepted (no top-right icon bar) |

## What to build when this is picked up

- A KB entry per screen: its identity cues, its regions, and the panels each region may hold.
- A panel entry per group: its function, its expected field kinds, and rough content matchers
  (not exact strings).
- Detection that asks "which screen, then which panel", instead of classifying loose elements.
- Inference over the KB for the unrecognised case, so a moved or reworded panel is a
  degraded read rather than a wrong one.

Related: `docs/ui_anatomy.md` → "Identify by association, not by dimension";
`docs/scene_model_design.md`; `docs/a2_perceived_state_implementation_plan.md`.
