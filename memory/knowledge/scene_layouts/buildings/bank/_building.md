# Bank — main view

The Bank allows the player to deposit or withdraw funds, manage a
savings account, and configure voyage insurance.  Its main view opens
directly to the Deposit/Withdrawal sub-menu selection; a right-side
panel shows live account and insurance summaries.

## Layout

### Title
- Top-left: `"Bank"` (immediately right of the back arrow).

### Left strip — Bank's sub-menus

Observed sub-menu options in the left strip (each row is tappable;
prefix `+` indicates an alternate sub-menu the player can switch to):

- **Deposit/Withdrawal** — move gold between the player's wallet and
  the bank.  *(TODO: detail panel layout not yet observed as a
  dedicated right-panel state; may share or replace the Savings Account
  Info panel.)*
- **Savings Account** — accumulates interest on deposited funds.  Has
  a red notification badge (`·`) indicating pending action or new
  information.  *(TODO: detail panel layout not yet observed.)*
- **Insurance** — configure voyage insurance level and review
  compensation terms.  *(TODO: detail panel layout not yet observed as
  a dedicated right-panel state; current frame shows Insurance Info
  inline in the default right panel.)*
- *(TODO: confirm whether additional sub-menus exist below Insurance;
  the left strip may be truncated in this frame.)*

### Right panel — default summary view

The right side shows two stacked summary panels when no sub-menu is
active (or when the building is first entered).

#### Savings Account Info panel

- **`"Savings Account Info"` panel title** at the top of the right panel.
- **`"Total Savings"` row** — `right_panel_row`; value shown as a gold
  coin icon followed by `0` in this frame (i.e. no savings deposited).
- **`"Interest Rate 0%"` row** — `right_panel_row`; a second numeric
  line below it also shows `0`.  *(TODO: confirm whether the second `0`
  is the accrued interest amount or a separate field.)*

#### Insurance Info panel

- **`"Insurance Info"` panel title** below the Savings Account Info panel.
- **`"Insurance LV 1"` status badge** — a highlighted (gold/amber)
  banner row indicating the current insurance tier.
- **`"Maximum Compensation"` row** — `right_panel_row`; value shown as
  a gold coin icon followed by `50,000`.
- **`"Rate"` row** — `right_panel_row`; value `30%`.
- **`"Daily Insurance Fee"` row** — `right_panel_row`; value shown as a
  gold coin icon followed by `0` in this frame.
- **`"Savings"` button** at the bottom of the right panel — appears as
  a distinct tappable element with a gold coin icon and value `0`.
  *(TODO: confirm exact tap behaviour — may open the Savings Account
  sub-menu or trigger a deposit action.)*

### Bottom row

- **Language Effect** button at the bottom-left corner.  *(TODO:
  confirm exact behaviour; consistent with the same element observed in
  other buildings.)*

### NPC presence

A Bank Clerk NPC sprite is visible centre-screen as ambient idle
content.  Per `_default.md`, NPC appearance varies per port and is
noise for scene-state reasoning.

## Element roles

- `back_arrow`, `home`, `building_title` (`"Bank"`)
- `chrome_icon` — top-right cluster (currency / settings / mail icons)
- `currency_label` — numeric top-right counters (ducats `2,485,176`,
  gems `5,952`, and additional tracked resources)
- `submenu_item` — left-strip rows (`Deposit/Withdrawal`,
  `Savings Account`, `Insurance`)
- `status_badge` — `"Insurance LV 1"` highlighted row in the Insurance
  Info panel
- `panel_title` — `"Savings Account Info"`, `"Insurance Info"`
- `right_panel_row` / `info_label` — read-only display rows (`Total
  Savings`, `Interest Rate`, `Maximum Compensation`, `Rate`,
  `Daily Insurance Fee`)
- `button` — `"Savings"` at the bottom of the right panel
  *(TODO: confirm if tappable or purely informational)*
- `button` — `"Language Effect"` bottom-left
- `npc_sprite`, `npc_bubble` — Bank Clerk idle content (noise)

## Hints

- **Right panel is a summary, not a sub-menu detail view** — both
  `"Savings Account Info"` and `"Insurance Info"` appear simultaneously
  as read-only summaries on the default view.  Tapping a left-strip
  sub-menu likely replaces this summary with an actionable detail panel.
- **`"Savings Account"` sub-menu has a red notification badge** — the
  bot should check this sub-menu when a pending-action badge is visible,
  as it may indicate unclaimed interest or a required confirmation.
- **Info rows are not tappable** — `Total Savings`, `Interest Rate`,
  `Maximum Compensation`, `Rate`, and `Daily Insurance Fee` are
  read-only labels matching the universal `<words> N` pattern
  (see `_default.md`).
- **`"Daily Insurance Fee"` showing `0`** may indicate either that
  insurance is inactive, the current tier has no daily cost, or the
  player has no insured cargo.  *(TODO: confirm the condition under
  which this value becomes non-zero.)*
- **Insurance LV 1 is the currently active tier** as indicated by the
  highlighted `"Insurance LV 1"` badge.  Higher tiers and their
  parameters are not yet observed.  *(TODO: capture frames with higher
  insurance levels active.)*

## Source frames

- `data/sessions/2026-04-15_16-05-29/frames/0028_1609523404.png` —
  Bank main view with default right panel showing Savings Account Info
  (zero balance, 0% interest) and Insurance Info (LV 1, 50,000 max
  compensation, 30% rate, 0 daily fee).  Savings Account sub-menu has
  a red notification badge.
- *(TODO: capture a frame with the Deposit/Withdrawal sub-menu active
  to document its detail panel layout.)*
- *(TODO: capture a frame with the Savings Account sub-menu active,
  especially when the notification badge is present.)*
- *(TODO: capture a frame with the Insurance sub-menu active to
  document tier selection and upgrade options.)*
- *(TODO: capture a frame with non-zero savings balance and accrued
  interest to confirm the Interest Rate row format.)*
