# Inn — Recruit Crew sub-menu

<!-- Note: layout is currently identical to buildings/harbor/recruit_crew.md.
     The difference is the parent building (Inn vs Harbor) — visible in
     the left-strip sub-menu list and the title bar.  If you change one
     and not the other, that's intentional — the screens may have
     diverged.  Grep for "identical to" to find sibling files. -->

Hire active crew from the Inn's recruit pool.  This is the screen the
bot uses most often during the trade loop, since crew needs to be
replenished after long voyages.

## Layout

### Title
- Top-center: `"Inn: Recruit Crew"` (or similar — the substring
  `"Recruit Crew"` is the reliable signal).

### Left strip
- Same Inn sub-menu list as the main view, with **`"Recruit Crew"`
  highlighted** (gold left-edge glow).

### Form area (right side of screen)

The form layout is identical to the Harbor's Recruit Crew screen.  See
that file for the detailed description of:

- The `"Fleet Crew Size <current>/<max> Min Crew <min>"` info label
  at `(cx ≈ 2092, cy ≈ 236)` — **NEVER tap; it's read-only.**
- The quantity stepper `[−] <quantity>/<available> [+]` in the right
  column.
- The cost label and gold `"Recruit"` action button at the bottom-
  right (observed label: `"1,836 Recruit"` — live log
  2026-05-12 17:11:53).

### After the gold Recruit tap

Same as Harbor's version — a confirmation dialog with Cancel + Recruit
buttons appears.  Tapping gold Recruit on the confirmation closes the
transaction.  A success notice may follow.

## Element roles

- `back_arrow`, `home`, `building_title`
- `submenu_item` — Inn's sub-menu list on the left
- `info_label` / `fleet_stats_panel` — Fleet Crew Size display
  (**NEVER tap**)
- `quantity_stepper`
- `cost_label`
- `action_button` — gold `"Recruit"`
- `dialog_overlay` — confirmation dialog

## Hints

- See `buildings/harbor/recruit_crew.md` for the same hints (check
  stepper value before tapping; don't tap info labels; etc.).
- After a successful recruit, the Inn's pool may have fewer
  recruitable crew left; the stepper's `<available>` may drop.

## Source frames

- Live log evidence from 2026-05-12 17:11:53 — `tap '1,836 Recruit'`
  on what was likely the Inn's recruit screen.
- Live log evidence from 2026-05-12 17:12:18 — recruit notice
  dismissed after the commit.
- *(TODO: capture a clean labelled frame of the Inn recruit_crew
  form for reference.)*
