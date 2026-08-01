# Harbor — main view

The Harbor is the only way to leave port by sea.  Its main view is the
entry point for departure, supply, repair, and crew recruitment AT this
port.  The Departure detail panel is shown by default on the right side;
the left strip lists alternative sub-menus the player can switch to.

## Layout

### Title
- Top-left: `"Harbor"` (immediately right of the back arrow).

### Left strip — Harbor's sub-menus

Observed sub-menu options in the left strip (each row is tappable;
prefix `+` indicates an alternate sub-menu the player can switch to):

- **Supply** — buy food / water / supplies separately.  *(TODO: detail
  panel layout not yet observed.)*
- **Repair** — repair ship hull / equipment.  *(TODO: detail panel
  layout not yet observed.)*
- **Recruit Crew** — hire active crew (joins the fleet immediately).
  See `buildings/harbor/recruit_crew.md`.
- *(TODO: additional sub-menus such as Sail Companion and Ship
  Management have been referenced in code but not yet observed in a
  labelled frame.  Capture a clean frame with the full sub-menu list
  visible.)*

### Right panel — Departure detail (shown by default)

The right side of the Harbor's main view shows the **Departure** detail
panel — this is the default view; you do not have to tap a sub-menu to
get here.

- **Ready-to-sail badge** at the top of the panel: a circular icon with
  a numeric value (e.g. `"2d"`) and the label `"Ready to sail"`.
  *(TODO: confirm whether the number reflects ship count, day count,
  or another quantity.)*
- **Total Load Capacity** info label: format `"Total Load Capacity
  <current>/<max>"`.
- **Cargo sub-rows** under Total Load Capacity — multiple `<current>/
  <max>` rows (e.g. `"722/1,847"`, `"178/754"`, `"127/0"`, `"0/0"`).
  *(TODO: confirm what each sub-row represents.  Likely per-cargo-type
  or per-ship breakdown.)*
- **Crew Size** info label: format `"Crew Size <current>/<max>"`
  (e.g. `"2,491/2,950"`).  May have a small `→` arrow to the right
  for a drill-down view.  *(TODO: confirm arrow behavior.)*
- **`"Depart Now"` action button** at the bottom — gold when enabled,
  greyed out when departure is blocked.

### Bottom row

- **Distribute and Redistribute Crew** toggle near the bottom-centre,
  with a checkmark indicator when enabled.  Controls whether crew is
  auto-distributed across ships before departure.
- **Language Effect** button at the bottom-left corner.  *(TODO:
  confirm exact behavior; likely a language-skill bonus indicator.)*

### NPC presence

A Harbor Official NPC sprite is visible centre-screen as ambient idle
content.  Per `_default.md`, NPC appearance varies per port and is
noise for scene-state reasoning.

## Element roles

- `back_arrow`, `home`, `building_title` (`"Harbor"`)
- `chrome_icon` — top-right cluster (currency / settings / mail icons)
- `currency_label` — numeric top-right counters (ducats, gems, etc.)
- `submenu_item` — left-strip rows (Supply, Repair, Recruit Crew, …)
- `fleet_stats_panel` / `info_label` — read-only displays
  (`Total Load Capacity`, cargo sub-rows, `Crew Size`,
  `Ready to sail` badge)
- `action_button` — gold `"Depart Now"` at the bottom of the right panel
- `toggle` — `"Distribute and Redistribute Crew"` checkbox
- `npc_sprite`, `npc_bubble` — Harbor Official idle content (noise)

## Hints

- **`"Depart Now"` may be disabled for several reasons** — and the bot
  has seen most of them:
  - Not enough crew (the most common; resolved by Recruit Crew).
  - Not enough supply (resolved by Supply sub-menu).
  - **Overload or overstaff on one or more ships** — even if fleet
    totals look fine, individual ship limits may be exceeded.  Check
    the cargo sub-rows and per-ship crew counts.
- **Total Load Capacity sub-rows are not tappable** — they are
  read-only info labels matching the universal `<words> N/M` pattern
  (see `_default.md`).
- **Distribute and Redistribute Crew toggle** affects crew balancing
  on departure.  If individual ships look uneven, verify the toggle
  is enabled before retrying departure.
- **Repair sub-menu existence**: this Harbor view shows a `Repair`
  sub-menu we hadn't previously documented.  Worth noting the bot
  may need to use it after combat.

## Source frames

- `data/sessions/2026-04-14_21-49-12/frames/0007_2149462165.png` —
  Harbor with Departure panel default-active, NPC sprite, full left-
  strip showing Supply / Repair / Recruit Crew, Depart Now disabled.
- *(TODO: capture a clean frame with Depart Now enabled (gold) to
  confirm the active button state.)*
- *(TODO: capture frames for Supply, Repair, Sail Companion, and Ship
  Management sub-menus.)*
