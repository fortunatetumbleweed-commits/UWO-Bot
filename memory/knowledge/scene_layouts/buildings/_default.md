# Building chrome (shared)

Layout elements common to **every** building interior in UWO.  This file
is prepended automatically by the loader when the bot is in `building`
or `sub_menu` nav_state.  Building-specific files describe what's
unique; this file describes what they all share.

## Chrome positions

- **Back arrow (←)** at approximately `(60, 45)`.  Top-left.  Tapping
  pops the **previous screen** off the navigation stack — it does NOT
  always return to the building's main view, or to `port_overworld`.
  It just undoes the last screen-opening tap.  Typical sequences:
  - From a sub-menu opened from a building's main view: back → that
    building's main view.
  - From a building's main view opened from `port_overworld`: back →
    `port_overworld`.
  - From a screen opened from the main menu: back → main menu (NOT
    `port_overworld`).
  - From a sub-screen two levels deep: back → one level up; tapping
    back twice → two levels up.

- **Home (⌂)** at approximately `(2300, 45)`.  Top-right.  Tapping
  jumps **all the way out to `port_overworld`** regardless of stack
  depth.  This is the escape hatch when the bot is lost or wants to
  abandon a flow without unwinding the navigation stack screen by
  screen.  From any building, sub-menu, or main-menu child screen,
  home closes everything and returns to the port_overworld view.

- **Building / sub-menu title**, top-left — immediately to the right
  of the back arrow icon (so the row reads `← <Title>` from left to
  right).  NOT centered.  Format varies:
  - Building main view: just the building name, e.g. `"Harbor"`.
  - Sub-menu: `"Building: Sub-menu"`, e.g. `"Harbor: Recruit Crew"`.

- **Top-right chrome cluster** (when present) — same icons as
  port_overworld (mail / mission / settings / hamburger).

## Sub-menu list (left strip)

When inside a building, the left strip lists that building's available
sub-menus, vertically.  The currently-active sub-menu has a **gold/
yellow left-edge glow**; other rows have no glow.

## Common interaction patterns

- **Back / home presses are cancellations**, never commit
  transactions.  A plan that consists only of back/home presses is
  incomplete per the project's plan-completeness rule (see CLAUDE.md).
- **Back ≠ Home.**  Back pops one screen at a time off the
  navigation stack.  Home jumps directly to `port_overworld`.  When
  the bot needs to abandon a flow entirely, prefer home — it bypasses
  intermediate screens like main_menu or transient confirmations.
  When the bot is one step off (e.g. accidentally opened a sub-menu),
  back is cheaper.
- **Gold / yellow action button** (when present) is the **primary
  commit transaction** for the current screen.  Common labels:
  `"Recruit"`, `"Confirm"`, `"Buy"`, `"Sell"`, `"Depart Now"`,
  `"Supply Departure"`, `"Pay"`, `"Receive"`.  Tapping it commits
  state.  A confirmation dialog typically follows.

## Building-owner NPC sprite (present in most buildings)

A large building-owner NPC sprite typically occupies the centre of the
screen as ambient idle content — the Harbor Official, Innkeeper, Market
Owner, Bureaucrat, etc.  An accompanying speech bubble often shows an
introductory line of dialogue.

**The NPC's appearance varies per port.**  Different cultures present
different merchants and officials: clothing, gender, age, ethnicity all
shift to match the port's region.  This means:

- **Do NOT use NPC appearance to identify which building you're in** —
  the gold "Bureaucrat" label tells you it's a Bureau; the costume of
  the figure does not.
- **Do NOT rely on NPC appearance for fingerprinting** — the same
  building looks visually different in different ports.
- **NPC speech bubbles are noise** (handled by `npc_bubble` role).
  Tapping the bubble usually advances or dismisses dialogue, never
  commits a transaction.

## Info labels vs action buttons (CRITICAL)

OmniParser sometimes tags **read-only information panels** as
`[button]` because they're styled as rounded rectangles with text
inside.  These are NOT tap targets.  Common info-label patterns:

- `"Fleet Crew Size <current>/<max> Min Crew <min>"`
- `"Total Load Capacity <current>/<max>"`
- `"Crew Size <current>/<max>"`
- `"<current>/<max> <current>/<max>"` (per-ship stat rows)
- `"<count> Ready to sail"`

The bot has tapped these labels by mistake before (live log
2026-05-12 17:20:33).  In **any** building or sub-menu, **never
recommend tapping an info-style label**.  Action buttons say
`"Recruit"`, `"Confirm"`, `"Buy"`, etc. — short verb / verb+noun, no
numeric ratios.

## Element roles you may see

- `back_arrow` — top-left
- `home` — top-right
- `chrome_icon` — top-right cluster (mail, settings, etc.)
- `building_title` — top-center
- `submenu_item` — left-strip rows
- `action_button` — gold/yellow commit button
- `fleet_stats_panel`, `info_label` — read-only info displays (NOT
  tappable)
- `cost_label` — ducat / token amount, near action button
- `npc_sprite` — building NPC at center-bottom (innkeeper, harbor
  master, etc.)
- `dialog_overlay` — when a confirm dialog has popped up over the
  building view
