# Affordance learning & curiosity — growing the KB from play

The bot should **learn the things it sees but doesn't yet know**. OmniParser
detects every UI element on a screen; the KB only knows a subset. Elements the
KB doesn't know (`Trade Points`, `Trade Info`, reward tiles, an unfamiliar
building's menu) should become **things to learn**, not noise to discard — by
reasoning about them, safely trying them, and recording what they do. Written
2026-08-07, from a design discussion. Extends the Data Flywheel (milestone 2.5)
and the learn-once principle in `reasoning_fallback_layer_design.md`.

## Motivation

- OmniParser *sees* `Trade Points` on the market screen — we even **filtered it
  out** as non-menu noise (see `left_menu` reward-box fix). But it's real game
  content the bot could learn.
- The bot needs to know **when an operation is done** (finished buying/selling)
  rather than guess or loop.
- "Explore a bit to learn the things not already in the KB."

## The loop

1. **Discovery.** Each tick, diff the OmniParser elements for the current
   screen-context (`base`/`context`) against the **affordance KB**. Elements not
   in it are **discovery candidates**.
2. **Identify — two paths (cheapest first):**
   - **Reasoned:** ask the LLM "on a `Market` screen, what is `Trade Points`?"
     given context → a labeled guess (`reward/info widget`). Cheap, no taps.
   - **Experimental (curiosity):** in a **safe probe**, tap it → observe the
     resulting screen → record `element → outcome` → **Back out**. Now the KB
     knows the affordance and its transition.
3. **Record** into the **affordance KB**, keyed by screen-context:
   `label → {kind: menu|button|info|reward, risk, leads_to: <screen>, effect}`.
4. **Completion detection.** Learn the "done" signature of an operation by
   observing the post-action state: e.g. buy → `Confirm → Result dialog → back at
   the market menu` **and** `cargo count increased`. Persist the signature so the
   bot *knows* the operation finished (vs. re-tapping or hanging).
5. **Curiosity / explore mode.** A task or idle behavior that probes the
   *unexplored, safe* affordances of the current building, records outcomes, and
   grows the KB. Bounded and prioritized — a few probes, highest-value first, not
   exhaustive.

## Safety gating (non-negotiable)

The bot "trying things" is where curiosity meets the red-gem rule. Before any
experimental probe, classify the affordance by **risk**:

| risk | examples | policy |
|---|---|---|
| **safe** | open an info/detail panel, a tab, a list — reversible with Back | **probe freely** (open → read → Back) |
| **gated** | Buy / Sell / Confirm / spend ducats·blue_gem / recruit / anything that commits | **never probe without explicit confirmation** |
| **hard-stop** | anything touching **red gems** (real money) or irreversible/destructive | **never** experiment; require the user |

Rules: prefer **reversible** probes; **log** every probe + outcome; **bounded**
attempts per screen; when in doubt, treat as gated. The bot learns by *reading*
(open info, Back), not by blindly tapping Confirm or spending currency.

## Data model (sketch)

```
affordance_kb[screen_context][label] = {
  kind:      menu | button | info | reward | tab,
  risk:      safe | gated | hard_stop,
  leads_to:  <screen_context or dialog>,      # learned from a probe
  effect:    <short description>,             # learned/reasoned
  source:    reasoned | experimented,
  confidence, last_seen,
}
operation_completion[operation] = {           # learned "done" signature
  signals: ["result_dialog", "back_at_menu", "cargo_delta>0"],
}
```

## How it fits the stack

- **Perception** supplies the elements (OmniParser) + structured `PerceivedState`
  (which context we're in) — the discovery substrate.
- **Reasoning layer** does the reasoned identification and drives safe probes
  (an extension of the "infer next action" operator — here the *intent* is
  "learn what this is," and the actions are safe probes).
- **Memory** holds the affordance KB + completion signatures — the growing
  knowledge.
- **Learn once:** a probed/learned affordance is deterministic thereafter; the
  KB grows from play. Same principle as the reasoning layer's learned recoveries,
  applied to *UI knowledge* rather than *recovery flows*.

## Relation to existing work

- **Data Flywheel (milestone 2.5, done)** — this is its active-learning
  extension: instead of only caching what Claude Vision was asked, the bot
  *proactively* learns affordances it observes.
- **Scene model / learned fingerprints** — the affordance KB layers on top
  (per-context element knowledge, not just whole-screen identity).
- **`left_menu` reward-box fix** — the very boxes we excluded from the menu
  (`Trade Points`/`Trade Info`) are the first discovery candidates: excluded from
  *menu* parsing, but flagged for *affordance learning*.
