# Harbor — Recruit Crew sub-menu

<!-- Note: layout is currently identical to buildings/inn/recruit_crew.md.
     The difference is the parent building (Harbor vs Inn) — visible in
     the left-strip sub-menu list and the title bar.  If you change one
     and not the other, that's intentional — the screens may have
     diverged.  Grep for "identical to" to find sibling files. -->

Hire active crew from the Harbor's recruit pool.  Active crew joins the
fleet immediately and counts toward Crew Size; this is the screen used
when the harbor reports `"Not Enough Crew"` on the depart button.

## Layout

### Title
- Top-center: `"Harbor: Recruit Crew"` (or similar — exact wording
  varies; the substring `"Recruit Crew"` is the reliable signal).

### Left strip
- Same Harbor sub-menu list as the main view, with **`"Recruit Crew"`
  highlighted** (gold left-edge glow).

### Form area (right side of screen)

The right side contains the recruit form:

- **Fleet Crew Size info label**, around `(cx ≈ 2092, cy ≈ 236)`:
  read-only display formatted as:
  `"Fleet Crew Size <current>/<max> Min Crew <min>"`.
  Observed example: `"Fleet Crew Size 786/1,676 Min Crew 786"`.
  
  **THIS IS NOT A BUTTON.**  OmniParser may tag it `[button]` because
  it's styled as a rounded rectangle, but tapping does nothing.  The
  bot has tapped it by mistake before — live log 2026-05-12 17:20:33.
  
- **Quantity stepper**, in the right column (cy ~700–800):
  `[−] <quantity>/<available> [+]`.  The `<quantity>` is how many crew
  will be hired when Recruit is tapped.  The `<available>` is how many
  the pool currently has.
  
  Observed stepper values:
  - `"0/890"` — fleet is already at capacity, stepper defaulted to 0.
  - `"14/904"` — fleet needs 14 more, stepper defaulted to 14.
  - Default is **whatever the game pre-fills** — usually "fill up to
    capacity" but may be 0 when at capacity.
  
  Bumping `+` / `−` adjusts the quantity.  The cost display next to
  the action button updates accordingly.
  
- **Cost label**: ducats required to hire `<quantity>` crew, displayed
  near the bottom-right.  Format: `"<cost> Recruit"` when fused with
  the action button label by OCR.

- **Gold `"Recruit"` action button** at bottom-right.  Observed label
  format includes the cost prefix: `"1,836 Recruit"` (live log
  2026-05-12 17:11:53).  Tapping commits the transaction.

### After the gold Recruit tap

A confirmation dialog appears (modal overlay) with:
- A confirmation message
- **Cancel** button (left, non-gold)
- **Recruit** / **Confirm** button (right, gold)

Tapping the gold Recruit on the confirmation closes the transaction.
A "recruit succeeded" notice may appear afterwards and dismiss
automatically.

## Element roles

- `back_arrow`, `home`, `building_title`
- `submenu_item` — Harbor's sub-menu list on the left
- `info_label` / `fleet_stats_panel` — the Fleet Crew Size display
  (**NEVER tap**)
- `quantity_stepper` — `+` / `−` controls and the current value
- `cost_label` — ducat cost near the action button
- `action_button` — gold `"Recruit"` (with optional cost prefix)
- `dialog_overlay` — when the confirmation dialog appears post-tap

## Hints

- **Always check the stepper value before tapping Recruit.**  Stepper
  showing `"0/M"` → fleet is at capacity, no hire needed; tapping
  Recruit here is a no-op.  Stepper showing `"N/M"` where N > 0 →
  tapping Recruit hires N crew at the displayed cost.
- The pre-filled stepper value may not be optimal.  If the bot needs
  to top up to capacity, bump `+` until quantity equals available pool
  (or fleet max minus current).
- **Do not tap the `"Fleet Crew Size N/M Min Crew M"` info label.**
  It's read-only.  Tapping it is the documented failure mode of
  2026-05-12 — the cycle-close detector classified it as a positive
  button and counted no-op taps as transactions.
- After a successful recruit, the form resets and the stepper goes
  to 0.  If more crew is still needed, repeat.

## Source frames

- Live log evidence from 2026-05-12 17:11:53 — Inn recruit tap that
  produced `"1,836 Recruit"`.
- Live log evidence from 2026-05-12 17:20:33 — Fleet Crew Size info
  label mistakenly tapped.
- Live log evidence from 2026-05-12 17:25:41 — Harbor depart panel
  right-side scan confirming the Crew Size info label format.
- *(TODO: capture a clean labelled frame of the Harbor recruit_crew
  form for reference.)*
