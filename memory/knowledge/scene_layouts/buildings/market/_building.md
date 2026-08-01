# Market — main view

The Market is where the player buys and sells trade goods, monitors
trade-point progress, and interacts with requested trade orders.  This
is the entry point for all in-port commerce.

## Layout

### Title
- Top-left: `"Market"` (immediately right of the back arrow).

### Left strip — sub-menus and side controls

Tappable controls along the left strip (top to bottom):

- **`+ Purchase`** — opens the buy sub-menu.
  See `buildings/market/purchase.md`.  *(TODO: layout not yet observed.)*
- **`+ Sell`** — opens the sell sub-menu.
  See `buildings/market/sell.md`.  *(TODO: layout not yet observed.)*
- **Trade Points panel** — contains:
  - **`"Trade Points"`** row header (tappable; opens detail / history).
  - **Progress bar** showing `<current>/1,000` (e.g. `"357/1,000"`).
    Read-only — this is the player's trade-point progress toward the
    next reward box.
  - **Reward-box icon** next to the progress bar (the chest icon).
    **Tappable when the progress bar reaches 1,000.**  When enabled,
    a count badge shows how many reward boxes are available.  Tapping
    claims a reward.  When the progress bar is below 1,000 (as in the
    seed frame), the chest icon is disabled.
- **Quantity row** — a small numeric value (e.g. `"10"`) flanked by
  icons.  *(TODO: confirm purpose — possibly a quantity selector or
  a count indicator.)*
- **`"Trade Info"`** — bottom-left tappable row.  Opens trade
  information detail.
- **`"Language Effect"`** — bottom-left tappable button.  Reflects the
  language-skill bonus applied to trades at this port.

### Right panel — Requested Trade Goods

The right side of the screen is the **Requested Trade Goods** panel:

- **Panel title**: `"Requested Trade Goods"`.
- **Status message** (when no matches): `"Required Products for
  Requests not found."`
- **`"Put In Bulk"` action button** at the bottom of the panel — gold
  when goods matching active requests are in cargo.  Submits eligible
  goods toward open trade requests.

### NPC presence

A Market Owner NPC sprite is visible centre-screen as ambient idle
content.  Per `_default.md`, NPC appearance varies per port (different
ports show different merchant figures reflecting local culture).

## Element roles

- `back_arrow`, `home`, `building_title` (`"Market"`)
- `chrome_icon` — top-right icons (settings, home)
- `currency_label` — numeric top-right counters (ducats, gems, points)
- `submenu_item` — `+ Purchase`, `+ Sell` (left strip top)
- `info_label` — `"357/1,000"` trade-point progress bar (read-only)
- `tappable_row` — `"Trade Points"`, `"Trade Info"`, `"Language
  Effect"`, the trade-quantity row (each carries a `>` chevron)
- `action_button` — `"Put In Bulk"` (right panel, when active)
- `reward_chest` — the trade-point reward-box icon next to the
  progress bar (disabled until trade-points hit 1,000)
- `panel_title` — `"Requested Trade Goods"` right-panel header
- `status_message` — `"Required Products for Requests not found."`
- `npc_sprite`, `npc_bubble` — Market Owner idle content (noise)

## Hints

- **Trade-point reward mechanic**: trade points accumulate as you
  make profitable trades.  When trade points reach 1,000, a reward
  box is unlocked.  The chest icon next to the progress bar enables
  with a count badge showing how many reward boxes are available.
  Tap the chest to claim.  Each claim resets the progress (so the
  bar typically reads `<small_value>/1,000` again after claiming).
- **`"Put In Bulk"`** is only useful when the player has goods that
  match active trade requests.  If the status message reads
  `"Required Products for Requests not found."`, the button has no
  effect — first acquire matching goods via Purchase, then return.
- **`"Language Effect"`** reflects the active character's language
  bonus for THIS port's trade region.  Higher language proficiency
  improves buy/sell prices.  Not relevant to decision logic; bot can
  ignore unless investigating poor prices.
- **The `"357/1,000"` progress bar is not tappable** — only the chest
  icon next to it (and only when enabled).  Don't tap the bar itself.

## Source frames

- `data/sessions/2026-04-14_21-52-17/frames/0017_2153293848.png` —
  Market main view with Market Owner NPC, left-strip controls including
  Trade Points panel at 357/1,000 (chest disabled), right panel showing
  "Required Products for Requests not found." and Put In Bulk button.
- *(TODO: capture frames for Purchase sub-menu, Sell sub-menu, and an
  expanded Trade Info detail view.)*
- *(TODO: capture a frame where trade points have reached 1,000 and
  the reward chest is enabled with a count badge — verify the claim
  flow.)*
