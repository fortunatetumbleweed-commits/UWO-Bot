# Item Shop — main view

The Item Shop is the in-port building for buying gear, tools, and black-market goods, and for selling cargo.  Its main view presents a left-strip sub-menu list and a right panel for the currently selected sub-menu's content.

## Layout

### Title
- Top-left: `"Item Shop"` (immediately right of the back arrow).

### Left strip — Item Shop sub-menus

Observed sub-menu options in the left strip (each row is tappable; prefix `+` indicates an alternate sub-menu the player can switch to):

- **Gear** — *(TODO: detail panel layout not yet observed.)*
- **Tool** — *(TODO: detail panel layout not yet observed.)*
- **Black Market** — carries a red notification badge (`·`) indicating new or available items.  *(TODO: detail panel layout not yet observed.)*
- **Sell** — sell cargo or items to the shop.  *(TODO: detail panel layout not yet observed.)*

### Right panel — Requested Items (shown by default)

The right panel on first entry shows a **Requested Items** panel.  In this frame the panel is empty.

- **Panel title** `"Requested Items"` at the top of the right panel.
- **Status message** `"Required Products for Requests not found."` displayed in the centre of the right panel when no matching inventory items are present.
- **`"Put In Bulk"` action button** at the bottom of the right panel — gold/amber styling, always visible.  *(TODO: confirm exact behaviour; likely submits all matching requested items in one action.)*

### Bottom row

- **Language Effect** button at the bottom-left corner.  *(TODO: confirm exact behaviour; likely reflects a language-skill trade bonus.)*

### NPC presence

A building-owner NPC sprite is visible centre-screen as ambient idle content.  Per `_default.md`, NPC appearance varies per port and is noise for scene-state reasoning.

## Element roles

- `back_arrow`, `home`, `building_title` (`"Item Shop"`)
- `chrome_icon` — top-right cluster (currency / settings / mail / chat icons)
- `currency_label` — numeric top-right counters (ducats `2,485,176`; gems `5,952`; additional counters present)
- `submenu_item` — left-strip rows (Gear, Tool, Black Market, Sell)
- `status_badge` — red notification dot on the Black Market sub-menu row
- `panel_title` — `"Requested Items"` header on the right panel
- `status_message` — `"Required Products for Requests not found."` (read-only, not tappable)
- `action_button` — gold `"Put In Bulk"` at the bottom of the right panel
- `button` — `"Language Effect"` at the bottom-left
- `npc_sprite`, `npc_bubble` — Item Shop Owner idle content (noise)

## Hints

- **`"Requested Items"` panel is the default view** — it appears without tapping any sub-menu row.  The sub-menu rows (Gear, Tool, Black Market, Sell) switch to different purchase/sell contexts.
- **`"Required Products for Requests not found."` is a read-only status message**, not a button.  It indicates the player's current inventory contains none of the items this port is requesting.
- **`"Put In Bulk"` remains visible even when the Requested Items panel is empty** — its enabled/disabled visual state under those conditions is *(TODO: confirm)*.
- **Black Market row carries a red notification badge** — this likely signals newly available or time-limited stock.  *(TODO: confirm badge semantics; it may reflect a server-push notification rather than inventory change.)*
- **Sub-menu detail panels not yet captured** — Gear, Tool, Black Market, and Sell interiors each need a dedicated layout file once clean frames are available.

## Source frames

- `data/sessions/2026-04-15_16-05-29/frames/0017_1608453573.png` — Item Shop main view, Requested Items panel active and empty, all four sub-menu rows visible, Black Market badge present, Put In Bulk button visible.
- *(TODO: capture a frame where Requested Items panel contains at least one matching item, to confirm tappable row format and Put In Bulk enabled state.)*
- *(TODO: capture frames for Gear, Tool, Black Market, and Sell sub-menu interiors.)*
