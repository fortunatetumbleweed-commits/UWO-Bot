# Apache Village remote check — live walkthrough (2026-08-20)

The step-by-step record of the FIRST remote village barter read (Apache Village / Camas),
performed live from a port with the user staging the entry point.  This is the ground-truth
trace behind `actions/village_check.py`; the frames are preserved as fixtures in
`data/reference/village_check_apache/` so parsers can be regression-tested against the real
screens.  Design + layout rules: `docs/barter_command_flow.md`,
`memory/project_remote_village_barter_check_2026-08-20.md`.

All coordinates are 2400×1080 landscape.

## 0. Entry (staged by the user this time; driver must do it itself)
World map open (from PORT via the minimap globe — `open_world_map(context="port_overworld")`),
Explore tab, village located and TAPPED → the **Village Info** panel opens on the right.
Village-finding is the same machinery as the sea path (`pan_to_village`, all 2026-08-20 fixes).
⚠️ Do NOT tap "Move to Village" — that starts a voyage; the check is read-only.

## 1. Base tab — fixture `01_base_tab.png`
Default tab.  OmniParser elements (label @ centre):

```
'Village Info'            @ (2077, 144)     panel header
'Apache Village'          @ (2077, 210)     village name
'Base'                    @ (1896, 277)  ┐
'Explore'                 @ (2077, 277)  ├─ tab row
'Barter'                  @ (2260, 278)  ┘
'Friendly'                @ (2078, 345)     amity grade (text)
'93,958/100,000'          @ (2079, 391)     amity points (button)
'Daily Barter Progress'   @ (1961, 454)     …
'0/7'                     @ (2314, 452)     barters used/total TODAY
```

Parser: `parse_base_tab` → grade, points, used/total.  `rounds_remaining = total − used`.

## 2. Barter tab — tap (2260, 278) → Trade List — fixture `02_barter_tab_screen1.png`
First visible screen of the goods list.  Key element dump (the layout law):

```
qty tile 953    button x1=1810  + text 'Camas'   @ (2003,424) + 'Food' cat     ← GOOD (flush-left, no pin)
qty tile 130    button x1=1840  + text 'Avocado' @ (2015,567) + 'Luxuries' + pin icon @ (2303,614)   ← MATERIAL
qty tile 150    button x1=1841  + text 'Cassava' @ (2011,677) + 'Food'     + pin icon @ (2302,725)   ← MATERIAL
qty tile 1,022  button x1=1812  + text 'Pulque'  @ (2004,796) + 'Liquor'                              ← GOOD
'Coral'         (next row, cut by the fold — Pulque's first material)
'View by Min. Exchange Unit' toggle @ (2081, 1000)
```

**Layout law (user):** goods and materials are INTERLEAVED in one list, no popup on tap.
- BARTER GOOD  = flush-left qty tile (x1 ≈ 1810), NO location pin.
- MATERIAL     = qty tile indented ~30 px (x1 ≈ 1840), category row HAS a location-pin icon
  (pin cx ≈ 2302) — the pin links to the material's source ports.
- Indent threshold used by the parser: `x1 − min(x1) > 15`.
- Name text column x ≈ 1950–2080; qty tiles x1 ∈ (1780, 1900); pins cx ∈ (2270, 2340).

## 3. Dead ends (recorded so nobody retries them)
- Tapping the good row centre (2077, 424): **no effect** — no popup, no expansion
  (fixture `03_barter_tab_screen1_dup.png` is the unchanged screen after the tap).
- Tapping a presumed right-edge chevron (2303, 440): **no effect**.
- The materials are ALREADY on screen — the list needs only scrolling, not expanding.

## 4. Scroll-accumulate
Swipe inside the panel: `(2080, 880) → (2080, 500)`, 400 ms, settle ~1.8 s.  Repeat:
parse each screen (`parse_trade_list`), accumulate (`merge_trade_screens`), STOP when the
parse signature repeats (list did not move = end).  Materials CONTINUE across scroll
boundaries — indented rows at the top of a new screen belong to the last good already seen.

## 5. Result — the complete Apache catalogue, read without sailing
```
Camas   obtain 953    ←  130 Avocado + 150 Cassava
Pulque  obtain 1,022  ←  150 Coral  + 150 Silver
Wampum  obtain 635    ←  150 Platinum + 150 Coral + 150 Silver
Base: Amity Friendly 93,958/100,000 · Daily Barter Progress 0/7
```
(These quantities are the 2026-08-20 snapshot — they re-roll ~every 6 h and shift with
amity tier; treat as fixture data, not current truth.)

## 6. Gotchas for the driver
- The user staged step 0 this run; the driver owns the full navigate-in and MUST end with
  a clean exit (close the panel / back to overworld), never "Move to Village".
- OCR noise appears near pins (a stray `'ge'` text token at (2372, 665)) — the qty/name/pin
  matching must key on geometry, not on reading every token.
- OmniParser misses some rows' tiles occasionally; the scroll pass re-sees them — accumulate
  across screens rather than trusting any single parse.
- The Melanesian screen showed the volatile-refresh countdown as **'Stock/Negotiation
  Refresh 05:08:19'** — same ~6 h clock governs these numbers.
