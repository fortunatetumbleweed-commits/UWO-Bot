# Inn — main view

The Inn is where the bot recruits crew and mates, rests to advance
game time, and reads in-game news.

## Layout

### Title
- Top-center: `"Inn"`.

### Left-strip — Inn's sub-menus

Observed sub-menu options:

- **Recruit Crew** — hire active crew (same recruit form as Harbor's
  Recruit Crew screen).  See `buildings/inn/recruit_crew.md`.
- **Recruit Mate** — hire a mate / first officer.  *(TODO: layout not
  yet observed.)*
- **Rest** — sleep at the inn.  Advances game time.  *(TODO: layout
  not yet observed.)*
- **News** — read recent in-game news.  *(TODO: layout not yet
  observed.)*

### Detail panel (right side)

Shows the currently-selected sub-menu's contents, or the innkeeper NPC
sprite when nothing is selected.

## Element roles

- `back_arrow`, `home`, `building_title` (`"Inn"`)
- `chrome_icon` (top-right cluster)
- `submenu_item` — left-strip rows
- `action_button` — gold button (depends on active sub-menu)
- `npc_sprite` — innkeeper

## Hints

- **Inn's Recruit Crew vs Harbor's Recruit Crew** — same form layout
  but different recruit pools.  Inn's pool is the general standby
  set; Harbor's pool is whatever's currently available at the docks.
  Bot logic shouldn't assume the pool size or composition is the
  same.
- The Inn is one of the most-visited buildings during the early-game
  loop because crew needs to be replenished frequently.

## Source frames

- 2026-05-12 inn recruit-flow captures (Port Royal, Southside
  sessions).
- *(TODO: capture a clean labelled frame of the Inn main view.)*
