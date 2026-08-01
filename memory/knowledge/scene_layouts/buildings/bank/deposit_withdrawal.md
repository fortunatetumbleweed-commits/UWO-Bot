# Bank — Deposit/Withdrawal sub-menu

The Deposit/Withdrawal sub-menu is the default active sub-menu of the Bank
building.  It presents a modal-style transaction panel in the centre of the
screen with controls for depositing gold into and withdrawing gold from the
player's savings account.

## Layout

### Active sub-menu indicator

The left strip shows **Deposit/Withdrawal** as the currently active sub-menu
(highlighted), followed by **Savings Account** and **Insurance** below it.
These sub-menu items are consistent with the parent building layout documented
in `buildings/bank/_building.md`.

### Transaction panel — centre-screen modal

A decorative card-style panel occupies the centre of the screen.  It contains
all interactive deposit/withdrawal controls.

#### Header row (top of panel)

- **`"Account Balance"` label** — top-left of the panel, with a gold-coin icon
  and a numeric value beneath it.  In the observed frame the value is `0`
  (no gold currently in the savings account).
- **`"Balance"` label** — top-right of the panel, with a gold-coin icon and a
  numeric value `2,485,176` (matches the player's on-hand balance shown in the
  top chrome bar).

#### Amount display (centre of panel)

- A large centred label reads **`"Deposit"` / `"0"`** — the label indicates the
  current transaction direction and the value below it shows the amount currently
  entered.  In the observed frame the entered amount is `0`.

#### Mode tabs (below the amount display)

Two coloured tab labels divide the lower half of the panel:

- **`"! Deposit"` tab** — left side, styled with a yellow/gold accent, indicating
  the currently active direction.
- **`"Withdrawal !"` tab** — right side, styled with a teal/green accent.

These behave as `mode_tab` elements: tapping one switches the transaction
direction between Deposit and Withdrawal.

#### Quick-amount buttons

Two symmetric rows of shortcut buttons flank a calculator icon on each side:

**Deposit side (left of centre):**
- `1M` button
- `100K` button
- `10K` button
- Calculator `icon` button (opens a numeric keypad for manual entry)
  *(TODO: confirm whether the calculator icon opens a separate input dialog
  or edits the amount field inline.)*

**Withdrawal side (right of centre):**
- Calculator `icon` button
- `10K` button
- `100K` button
- `1M` button

The buttons are mirror-image across the centre axis.  Tapping a quick-amount
button presumably adds that value to the current entered amount.
*(TODO: confirm whether tapping 100K sets the amount TO 100K or ADDS 100K to
whatever is already entered.)*

### Confirm button (bottom of panel)

Below the transaction panel a single wide **`"Deposit"` action button** is
visible at the bottom-centre of the screen.  It is visually greyed out in the
observed frame (entered amount is `0`), suggesting it is disabled when the
amount is zero.  The button is prefixed with a gold-coin icon and the current
entered amount (`0`).

*(TODO: confirm the button label changes to `"Withdrawal"` when the Withdrawal
mode tab is active.)*

### Language Effect / Withdrawal Fee Discount

At the bottom-left corner, below the sub-menu list:

- A **`"Language Effect"` button** — consistent with other building screens.
- A **`"Withdrawal Fee Discount"` button** directly below it — unique to this
  sub-menu.  *(TODO: confirm whether this opens a detail tooltip or is itself
  tappable to apply/toggle a discount.)*

### NPC presence

A building-owner NPC sprite is visible centre-screen behind the transaction
panel as ambient idle content.

## Element roles

- `panel_title` — `"Deposit/Withdrawal"` in the top-left chrome (also the
  active `submenu_item` in the left strip)
- `submenu_item` — `"Savings Account"`, `"Insurance"` (inactive sub-menus in
  the left strip)
- `info_label` — `"Account Balance"` (read-only, shows current savings amount)
- `info_label` — `"Balance"` (read-only, shows current on-hand gold)
- `text` — centre amount display (`"Deposit"` direction label + numeric amount)
- `mode_tab` — `"! Deposit"` (active) and `"Withdrawal !"` (inactive) tabs
- `button` — quick-amount shortcut buttons: `1M`, `100K`, `10K` (deposit side
  and withdrawal side)
- `icon` — calculator icon buttons on each side of the quick-amount row
- `action_button` — `"Deposit"` confirm button at the bottom (disabled when
  amount is `0`)
- `button` — `"Language Effect"` bottom-left
- `button` — `"Withdrawal Fee Discount"` bottom-left, below Language Effect
- `currency_label` — top chrome bar: `2,485,176` (gold), `5,952` (gems)
- `chrome_icon` — top-right cluster (settings gear showing `357`, mail, home)
- `npc_sprite`, `npc_bubble` — ambient idle content (noise)

## Hints

- **`"Deposit"` confirm button is disabled when the entered amount is `0`** —
  the bot must tap at least one quick-amount button or use the calculator icon
  before the confirm button becomes tappable.
- **Account Balance vs Balance**: `"Account Balance"` is the amount stored in
  the savings account; `"Balance"` is the player's current on-hand gold.  These
  are two separate pools.  In the observed frame the savings account is empty
  (`0`) while the on-hand balance is `2,485,176`.
- **Mode tabs change the transaction direction** — the panel label and confirm
  button label both reflect the current mode (`"Deposit"` or `"Withdrawal"`).
  Ensure the correct tab is active before tapping the confirm button.
- **Withdrawal Fee Discount** is listed as a language-effect sub-item — it may
  indicate a passive bonus rather than an actionable toggle.
  *(TODO: verify by observing the panel when a Language Skill is active.)*
- **Quick-amount buttons are symmetric** — the same denominations (`1M`, `100K`,
  `10K`) appear on both the deposit and withdrawal sides.  The side that is
  active (matching the current `mode_tab`) is the one whose buttons affect the
  entered amount.  *(TODO: confirm whether the inactive side's buttons are
  disabled or simply ignored.)*
- **Calculator icon** — each side has its own calculator icon adjacent to the
  quick-amount buttons.  *(TODO: confirm behavior — likely opens a numeric
  input overlay for precise amounts.)*

## Source frames

- `data/sessions/2026-04-15_16-05-29/frames/0029_1609565576.png` — Deposit/
  Withdrawal panel with Deposit mode active, entered amount `0`, Account Balance
  `0`, on-hand Balance `2,485,176`, confirm button disabled.
- *(TODO: capture a frame with a non-zero amount entered to confirm the confirm
  button's active/gold visual state and whether the label matches the mode tab.)*
- *(TODO: capture a frame with the Withdrawal mode tab active to confirm panel
  label, confirm button label, and whether the withdrawal side quick-amount
  buttons become active.)*
- *(TODO: capture a frame with the calculator icon tapped to document the numeric
  input overlay layout.)*
