# Shipyard — main view

The Shipyard is where ships are built, upgraded, and managed. Its main
view presents a left-strip sub-menu list and a right panel showing
active **Shipbuilding Status** slots. Up to three concurrent build jobs
are visible in the right panel.

## Layout

### Title
- Top-left: `"Shipyard"` (immediately right of the back arrow).

### Left strip — Shipyard's sub-menus

Observed sub-menu options in the left strip (each row is tappable;
prefix `+` indicates an alternate sub-menu the player can switch to):

- **Build** — initiate a new ship build order.  *(TODO: detail panel
  layout not yet observed.)*
- **Blueprint** — manage ship blueprints.  *(TODO: detail panel layout
  not yet observed.)*
- **Parts Shop** — browse and purchase ship parts.  *(TODO: detail
  panel layout not yet observed.)*
- **Repair** — repair ship hull or equipment at the shipyard.  *(TODO:
  detail panel layout not yet observed; note that Harbor also has a
  Repair sub-menu — confirm whether these are distinct.)*
- **Dismantle** — break down a ship for materials.  *(TODO: detail
  panel layout not yet observed.)*
- **Modify** — modify an existing ship.  *(TODO: detail panel layout
  not yet observed.)*
- **Ship Inscription** — apply or manage ship inscriptions.  *(TODO:
  detail panel layout not yet observed.)*

Red dot notification badges are visible on **Build** and **Repair**
sub-menu items, indicating pending attention is required (e.g. a build
has completed, or a ship needs repair).

### Right panel — Shipbuilding Status

The right panel is titled `"Shipbuilding Status"` (panel_title at the
top of the right zone).

The panel shows up to **three active build-job rows** stacked
vertically. Each row contains:

- A **ship thumbnail icon** (`icon`) on the left of the row.
- A **level badge** overlaid on the thumbnail (e.g. `"17"`).  *(TODO:
  confirm whether this is the ship's current level or the target level
  after build.)*
- A **ship name label** (`"The Amity"`) and a **port/location label**
  (`"Cohasset"`) immediately to the right of the thumbnail.
- A **countdown timer** displayed as `HH:MM:SS` (e.g. `12:25:19`,
  `12:24:33`, `12:24:38`), indicating remaining build time for that
  slot.
- A **progress bar** below the ship name / timer, showing build
  completion visually.
- Two `action_button` elements per row:
  - **`"Cancel Build"`** — grey button on the left; cancels the active
    build job.
  - **`"Quick Build"`** — gold button on the right; presumably
    accelerates completion.  *(TODO: confirm whether Quick Build
    consumes a premium currency or a speed-up item.)*

A **`"Build Status"`** button appears below the three job rows, near
the bottom of the right panel.  *(TODO: confirm whether this opens a
full history / queue view or is purely informational.)*

### Bottom row

- **Language Effect** button at the bottom-left corner.  *(TODO:
  confirm exact behavior; likely a language-skill bonus indicator shared
  across building screens.)*

### NPC presence

A building-owner NPC sprite is visible centre-screen as ambient idle
content.

## Element roles

- `back_arrow`, `home`, `building_title` (`"Shipyard"`)
- `chrome_icon` — top-right cluster (currency icons, settings, mail)
- `currency_label` — numeric top-right counters (ducats `14,001,701,263`,
  gems `28,429`, and two further resource values `2,724` / `562`)
- `submenu_item` — left-strip rows: Build (with red badge), Blueprint,
  Parts Shop, Repair (with red badge), Dismantle, Modify, Ship Inscription
- `status_badge` / `notification_dot` — red dots on Build and Repair rows
  indicating pending action required  *(TODO: propose role name
  `notification_dot` if not yet in element_postprocess.py)*
- `panel_title` — `"Shipbuilding Status"` heading at the top of the right
  panel
- `right_panel_row` — each of the three active build-job rows in the
  Shipbuilding Status panel
- `icon` — ship thumbnail within each build-job row
- `info_label` — ship name, port label, and countdown timer within each
  build-job row (read-only)
- `progress_bar` — build completion bar within each row  *(TODO: confirm
  role name; propose `progress_bar` if not yet in element_postprocess.py)*
- `action_button` — `"Cancel Build"` (grey) and `"Quick Build"` (gold)
  per row; `"Build Status"` below the rows
- `npc_sprite`, `npc_bubble` — Shipyard Owner idle content (noise)
- `button` — `"Language Effect"` bottom-left

## Hints

- **Three concurrent build slots are visible** in this frame; it is not
  confirmed whether this is the maximum or whether more slots can exist.
  *(TODO: capture a frame with a different number of active builds to
  confirm slot count behaviour.)*
- **Countdown timers are read-only** — the `HH:MM:SS` labels are
  informational and are not tappable.
- **`"Cancel Build"` and `"Quick Build"` are per-row**, not global — the
  bot must target the correct row's buttons when acting on a specific
  ship build.
- **Red notification dots on Build and Repair** may indicate a completed
  build ready for collection, or a ship awaiting repair.  The bot should
  check these sub-menus when dots are present.
- **`"Build Status"` button** location is below all job rows at the
  bottom of the right panel; distinguish it from the per-row
  `"Quick Build"` buttons.
- **NPC sprite and bubble are noise** — the `"Shipyard Owner"` label and
  `"Welcome to"` bubble text should be ignored for scene-state reasoning.
- **Port labels within build rows** (e.g. `"Cohasset"`) reflect where
  the build is taking place, not the player's current location.
  *(TODO: confirm whether builds can occur at a different port than the
  player's current location.)*

## Source frames

- `data/sessions/2026-04-14_21-52-17/frames/0008_2152559297.png` —
  Shipyard main view with Shipbuilding Status panel showing three active
  build jobs for `"The Amity"` at Cohasset; red notification badges on
  Build and Repair; NPC sprite centre-screen.
- *(TODO: capture a frame with the Build sub-menu open to document the
  ship-selection and ordering flow.)*
- *(TODO: capture a frame with the Blueprint sub-menu open.)*
- *(TODO: capture a frame with Parts Shop, Dismantle, Modify, and Ship
  Inscription sub-menus open.)*
- *(TODO: capture a frame with zero active build jobs to confirm empty-
  state right panel appearance.)*
- *(TODO: capture a frame immediately after a build completes to confirm
  what the red dot on Build signifies and how collection works.)*
