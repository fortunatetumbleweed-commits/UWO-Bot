# Reading a village's Trade List

The Village Info → Barter → Trade List is a scrollable list taller than its panel. Reading it
means assembling several overlapping views of one list, and nearly every bug this reader has
had came from treating a *view* as if it were the list.

## How it works now

    trade_list_elements(frame)      lower icon floor, clipped to the panel
      -> parse_trade_rows           one screen as a flat row list, deduped
      -> align / alignable          stitch screens on shared rows
      -> rows_to_trades             recipes read ONCE from the finished sequence

Both readers share this chain: `actions/village_check.py` (what missions run) and
`tools/learn_village_barter.py` (what builds the knowledge base).

### Rows, not recipes

A good and its materials routinely straddle the fold, so **no single screen holds the whole
recipe**. Screens are stitched into one row sequence first, and the good/material structure is
read from that. Every attempt to bridge the fold by *inference* instead produced a confident
wrong recipe:

* carrying a good's NAME onto the next screen's leading materials wrote
  `American Bison <- Horse, Hand Cannon, Bullet, Moccasin, American Bison, Wool` — six inputs,
  including itself (2026-08-25);
* merging per-screen groups wrote `Juniper Berry <- Iron` and `Meteorite <- Vodka`, each
  material belonging to the good above it (2026-08-24).

Rows are matched on `(name, quantity, is_material)` and never on position — the same row sits
at a different y on every screen. The triple matters: a barter good can also be a material, so
`American Bison(769) as a GOOD` and `American Bison(300) as a MATERIAL` are different rows.

### Gaps are reported, never bridged

If a screen shares no row with the sequence, the scroll jumped past rows nobody saw. Appending
anyway would fabricate an order. The reader backs up and re-reads; failing that it records a
GAP and marks the read INCOMPLETE. An incomplete read is recoverable, a wrong one is not.

### Good or material: three signals

    location pin   materials have one at the row's right edge; goods do not
    indent         materials sit ~29px right of a good's flush-left edge
    height         a good's row is taller (~133px) than a material's (~105px)

The **pin leads**, because it is the only signal that survives a continuation screen: when
every row is a material the indent baseline is itself a material and the height spread
vanishes, so geometry would call the whole screen goods and outvote a correct pin.

Geometry is consulted where the pin fails *dangerously* — a missed pin promotes a material to
a GOOD, and a good with another good's materials beneath it is a wrong recipe. A pinless row
that is indented **or** short is read as a material, gated on the screen showing a real spread
of row heights (that spread is what says both kinds are present).

### The list decides when it is finished

Not the parse. The sweep ends only on **measured absence of movement** (frame alignment) plus
the **scrollbar** at its bottom stop. The old rule — "two screens produced nothing new" —
cannot tell *there is nothing left to read* from *I could not read this screen*, so a parser
bug truncated a list mid-way. A screen that parses empty triggers a re-look, never an ending.

### Its own parse

`trade_list_elements()` departs from the ordinary parse in two measured ways, **for this panel
only**:

* **icon floor 0.18.** Row thumbnails and location pins come back far below the global 0.30:
  measured pins at 0.297 and 0.636, thumbnails at 0.127 / 0.195 / 0.246 / 0.251 / 0.42 / 0.49
  / 0.56. The global floor runs through the middle of that, so classification was close to a
  coin flip per screen — Iron at Svear lost its pin by 0.003 and became a good.
* **clipped to the panel**, using the panel's own landmarks (the tab strip, the title, the
  "View by Min. Exchange Unit" checkbox). The world map shows beside the panel and its labels
  parse exactly like row names.

## Debugging

`/tmp/village_stitch/<village>.json` records every assembly step: each screen's rows, where it
aligned, what it contributed, and any gap. Frames are kept in `/tmp/village_learn/`, so a read
can be **replayed offline** — which is how the alignment bugs were found without the device.
`tools/village_scroll_report.py` renders a tick-by-tick HTML report of a live sweep.

## Traps that cost real time

* **The game remembers each list's scroll position.** Reopening the village panel does not
  clear it; only leaving the world map and coming back does. A sweep once ran twelve scrolls
  against a list that opened on its final row.
* **Gestures must stay inside the list.** The rewind swipe released 60px below it, on the
  "View by Min. Exchange Unit" checkbox, and toggled it. That switch rewrites every quantity
  as a minimum exchange unit (`Chicle 187` becomes `1`) and OCR discards text under two
  characters — so the quantity column vanished and the panel read as broken.
* **The swipe will not honour a requested distance.** Measured: 150px asked delivered 384px;
  300px asked delivered 180px at one moment and 429px at another. Duration does not reliably
  tame it. Do not calibrate a step; keep requests small and rely on overlap.
* **Frame alignment cannot measure a shift larger than one viewport** (~585px) — beyond that
  there is no shared content. So "no movement detected" means *either* the list is stopped
  *or* it jumped a whole screen. The scrollbar tells those apart.
