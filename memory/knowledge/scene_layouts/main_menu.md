# main_menu

The game's top-level main menu, opened by tapping the hamburger (≡)
icon on `port_overworld` or other screens.  Typically slides in as a
panel of menu items the player can choose from (e.g. Company, Mate,
Fleet, Settings, etc.).

## Layout

*(TODO: not yet observed in labelled frames — the description below
is based on conversational notes and may need refinement once a frame
is captured.)*

- The menu typically appears as a vertical list of items on one side
  of the screen.
- Tapping a menu item opens a **child screen** that visually
  resembles a building interior — header, left-strip sub-menus or
  content tabs, detail panel — but it lives under the main menu
  branch, NOT under any building.

## Navigation behaviour (important — confirmed by user 2026-05-13)

- **From a main-menu child screen**, tapping **back arrow** pops back
  to the **main menu** (not to `port_overworld`).
- **From a main-menu child screen**, tapping **home (⌂)** closes the
  main menu entirely and goes directly to `port_overworld`.
- This is the same back-vs-home distinction as buildings: back pops
  one screen off the stack, home jumps to overworld.  The difference
  is the stack — main-menu screens are NOT children of any building.

## Element roles

- `back_arrow` (top-left) — returns to main menu
- `home` (top-right) — exits to `port_overworld`
- `menu_item` — vertical list entries in the main menu panel
- Other roles will be added as main-menu child screens are observed
  and labelled.

## Hints

- When a child screen looks like a building (same chrome shape) but
  the bot doesn't recognise the building name in the title bar, it
  may actually be a main-menu child.  Examine the title carefully:
  a building's title is its name (`"Harbor"`, `"Inn"`); a main-menu
  child's title is its menu function (`"Company"`, `"Mate"`,
  `"Settings"`, etc.).
- Recovery flows that rely on "back arrow takes me to
  `port_overworld`" will FAIL inside main_menu (back takes you to
  the menu panel, not the overworld).  Use home for unconditional
  escape.

## Source frames

*(TODO: capture labelled frames showing the main menu open and one
or two of its child screens.)*
