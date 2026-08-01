# Market — Purchase sub-menu

The Purchase sub-menu is where the player buys trade goods from the port's
market.  It shows a grid of goods currently on sale, a cargo summary panel
on the right, negotiation/tax info, and action buttons at the bottom.

## Layout

### Section header — Trade Goods on Sale

- A `"Trade Goods on Sale"` label spans the top of the central grid area.
- A countdown timer (format `HH:MM:SS`, e.g. `"00:03:47"`) sits to the
  right of the label — presumably the time until stock refreshes.
  *(TODO: confirm whether this is a refresh timer or an offer-expiry
  timer.)*
- A small icon button immediately right of the timer (likely a manual
  refresh trigger).  *(TODO: confirm tap behavior.)*
- A numeric badge (e.g. `"2"`) appears to the right of the refresh icon —
  meaning unclear.  *(TODO: confirm what this counter represents.)*
- A filter/funnel icon button at the far right of the header row.

### Trade goods grid — centre

Six goods cards are arranged in a 3-column × 2-row grid.  Each card
is a tappable `trade_good_card` and displays:

- **Good name** (large, top of card)
- **Category** (sub-label below name, e.g. `"Textile"`, `"Food"`,
  `"Wares"`, `"Jewelry"`, `"Spices"`)
- **Quantity badge** (numeric, overlaid on the card icon, e.g. `158`,
  `183`, `237`, `223`, `85`, `128`)
- **Supply/demand percentage** (e.g. `99%`, `106%`, `103%`, `96%`)
- **Price** in gold (e.g. `565`, `268`, `131`, `395`, `18,088`)
- **`"Specialties"`** gold banner — shown on cards where the good is a
  local specialty (observed on Flax and Juniper Berry in this frame)
- A snowflake-style badge (top-right of card) on some goods —
  *(TODO: confirm what this icon indicates; possibly "perishable" or
  "cold storage" flag.)*
- **`"Unlock Condition (0/1)"`** overlay with a lock icon — observed on
  the Juniper Berry card; indicates a purchase prerequisite that has not
  yet been met.  A `+` button appears to the right of this label.

Observed goods in this frame (left-to-right, top-to-bottom):

| Card | Name | Category | Price |
|------|------|----------|-------|
| R1C1 | Flax | Textile | 565 |
| R1C2 | Duck Meat | Food | 268 |
| R1C3 | Rye | Food | 131 |
| R2C1 | Stone | Wares | 395 |
| R2C2 | Crystal | Jewelry | 18,088 |
| R2C3 | Juniper Berry | Spices | — (locked) |

### Right panel — Cargo & inventory

The right panel runs the full height of the scene.

- **Panel title**: `"Purchase"` at the top of the right panel.
- **Cargo bar**: A horizontal progress bar near the top of the panel
  labelled `"Cargo"` with a `>` arrow (possibly drill-down tappable).
  Format: `<current>(+<pending>)/<max>`, e.g. `3,040(+432)/3,161`.
  The `+432` portion reflects goods currently selected/staged for
  purchase.  *(TODO: confirm `>` arrow tap behavior.)*
- **Inventory grid**: A scrollable icon grid fills the remainder of the
  right panel, showing the player's current cargo items as small
  thumbnail icons with numeric quantity badges.  Individual cells appear
  to be tappable (`button` role detected on multiple cells with numeric
  labels such as `50`, `55`, `54`, `28`, `29`, `15`, `59`, `41`, etc.).
  *(TODO: confirm tap behavior on inventory cells — may select/deselect
  for selling or simply show item detail.)*

### Left strip — utility controls

Below the `"Purchase"` and `"Sell"` sub-menu labels on the left:

- **Trade Points** button/display — label `"Trade Points"` with a `>`
  arrow; shows current value as a progress bar format `357/1,000`.
  *(TODO: confirm whether this is tappable for detail or informational
  only.)*
- **Quantity selector row** — a row containing a numeric input showing
  `"10"` flanked by icon buttons (likely decrement / increment / bulk
  controls for the purchase quantity).
- **Trade Info** button — tappable, opens trade information detail.
  *(TODO: confirm what is shown.)*
- **Language Effect** button — bottom of left strip.
- **Trade Goods Purchase Discount** — a sub-label below Language Effect;
  appears to describe the effect active.  *(TODO: confirm whether this
  is a separate tappable row or a description label for Language Effect.)*

### Negotiation / Tax info bar — lower right panel

Immediately above the Purchase button at the bottom of the right panel:

- **`"Nego. Chance"` label** with a `?` info icon and a percentage value
  (e.g. `58.2%`).
- **`"Success Rate"` label** with a percentage value (e.g. `73.2%`).
- **`"Tax"` label** with a percentage and a gold icon + value (e.g.
  `0%`, `0` gold).
- A small magnifier icon next to Tax — *(TODO: confirm tap behavior.)*

### Bottom action bar

Runs across the full bottom of the screen:

- **`"Put In Bulk"` checkbox** (left of centre) — checkmark visible in
  this frame (enabled state).
- **`"Apply Load Ratio"` checkbox** (right of `"Put In Bulk"`) — no
  checkmark visible in this frame (disabled state).
- **`"Sell Supplies"` action button** — right of centre.
- **`"Sell Overload"` action button** — rightmost; has a red badge
  indicator in this frame, suggesting pending overload condition.
- **`"Purchase"` action button** — far right of the right panel, at
  bottom.  Appears greyed out / inactive in this frame (zero gold shown
  above it, no goods selected).

## Element roles

- `panel_title` — `"Trade Goods on Sale"` (grid header); `"Purchase"`
  (right panel header)
- `info_label` — countdown timer (`"00:03:47"`), cargo bar
  (`"3,040(+432)/3,161"`), Trade Points progress (`"357/1,000"`),
  Nego. Chance / Success Rate / Tax values
- `trade_good_card` — six tappable cards in the centre grid (proposed
  new role; each card is a compound tappable element)
- `status_badge` — quantity overlays on trade good cards; `"Specialties"`
  banner; lock/`"Unlock Condition"` overlay on Juniper Berry
- `cargo_inventory_cell` — individual icon+badge cells in the right panel
  inventory grid (proposed new role)
- `button` — refresh icon, filter icon, `"+"` on locked card, Trade Info,
  `>` on Trade Points, `>` on Cargo bar, quantity decrement/increment
  icons, Language Effect, Trade Goods Purchase Discount
- `toggle` — `"Put In Bulk"` checkbox, `"Apply Load Ratio"` checkbox
- `action_button` — `"Sell Supplies"`, `"Sell Overload"`, `"Purchase"`
  (bottom-right)
- `text` — `"Put In Bulk"`, `"Apply Load Ratio"` checkbox labels
- `npc_sprite`, `npc_bubble` — ambient idle content (noise)

## Hints

- **`"Purchase"` button is disabled when no goods are selected** — the
  button appears greyed out and shows `0` gold in this frame.  Select
  one or more trade goods from the grid to enable it.
- **`"Unlock Condition"` cards cannot be purchased** — the lock overlay
  on a card (e.g. Juniper Berry) means a prerequisite condition must
  be met first.  The `+` button next to the unlock label likely
  navigates to the unlock requirement detail.  *(TODO: confirm `+`
  tap behavior.)*
- **`(+N)` in the cargo bar** reflects the volume of goods currently
  staged/selected for purchase — not yet in cargo.  The displayed
  format is `current(+staged)/max`.
- **`"Sell Overload"` red badge** indicates the player is currently
  overloaded — the bot should resolve overload before or after
  purchasing.
- **`"Specialties"` banner** on a card typically indicates better trade
  margins at this port — relevant for route optimisation logic.
- **Trade Points (`357/1,000`)** are consumed per trade action — the
  bot should check remaining points before attempting bulk purchases.
  *(TODO: confirm exact cost per transaction and what happens when
  points are exhausted.)*
- **Countdown timer** on the goods header — when it reaches zero the
  stock list likely refreshes.  The bot should not rely on specific
  goods being available across sessions.
- **`"Put In Bulk"` checkbox** — when enabled, purchases are placed
  into the bulk cargo hold.  State visible from checkmark presence.
  The bot should verify this is in the desired state before purchasing.

## Source frames

- `data/sessions/2026-04-14_21-52-17/frames/0018_2153318953.png` —
  Purchase sub-menu with six goods visible (Flax, Duck Meat, Rye,
  Stone, Crystal, Juniper Berry), Juniper Berry locked, Purchase
  button inactive, Sell Overload badge active, Put In Bulk enabled.
- *(TODO: capture a frame with one or more goods selected to confirm
  staged-quantity display, enabled Purchase button state, and gold
  cost preview.)*
- *(TODO: capture a frame showing the Cargo `>` drill-down panel.)*
- *(TODO: capture a frame showing the Trade Info panel.)*
- *(TODO: capture a frame with the Unlock Condition `+` button tapped
  to document the unlock requirement flow.)*
