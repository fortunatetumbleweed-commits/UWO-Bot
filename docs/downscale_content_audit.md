# Audit: reading CONTENT from a whole-frame parse

**Rule** (CLAUDE.md, 2026-08-26): a reduced or whole-frame read answers COARSE questions —
which screen, did a panel change, is a dialog up. It cannot answer *what does this say* or
*how many*. Content must be read from a REGION at full resolution.

> **On the verdict column.** It describes how the WHOLE-FRAME read fails. The region crop
> reads all of these correctly except where noted — that is the point of the table, and an
> earlier draft labelled the column "Verdict", which read as though the value were missing
> altogether rather than missing *from one of the two reads*.

## The mechanism, stated correctly

We do **not** downscale before OCR. `omniparser._detect_text` passes the full frame to
EasyOCR and no `canvas_size` / `mag_ratio` is set. The failure is subtler and the fix is the
same: **reading a whole large frame loses small text that a region read recovers.** No
upscaling is needed — simply parsing a 750×520 crop instead of the 2400×1080 frame is enough.

It is not universal, and that is what makes it dangerous. Most text reads fine whole. What
fails is a specific pattern:

> **a small number drawn ON a thumbnail** — light text over artwork, ~30px tall.

Panel text on a flat background (`0/3`, `445/2`, `3,979/4,952`) reads correctly whole.

## Measured

| Where | Whole-frame read | Region crop | How the WHOLE-FRAME read fails |
|---|---|---|---|
| Village Info trade list — material qty on thumbnail | `358, 102, 102` — the **`44` never proposed as text** (`parse_raw` conf=0.01 shows only an `icon` box) | `44` read cleanly | **misses it entirely** |
| Market grid — owned qty on tile | **`,451`** — truncated, leading digit lost | `1,451` | **truncates it — looks valid** |
| Barter panel — Trade Material | `0/3`, `445/2` read | same | — it does not |
| Cargo bar `N/M` | `3,979/4,952` read | — | — it does not |

A truncated number is the worse failure: a missing one gets a material dropped and can be
noticed; `451` for `1,451` is accepted and acted on.

## Ranked findings

**1. `vision/market_reader.read_market_page_omni` — owned/stock quantities. HIGH.**
Feeds `buy_to_goal`'s progress and `_read_owned_via_sell`. The tile quantity is the exact
failing pattern, and measured truncated on a real frame. This is the reader behind
`owned=UNREADABLE` and the 2,000-Iron overbuy.

**2. `actions/village_check` — trade-list materials. CONFIRMED, partly fixed.**
Cost five runs a recipe with two of three materials. The completeness check is fixed; the
READ is not — it still takes rows from the whole-frame parse.

**3. `actions/buy_materials._cargo_tiles` / `track_bought_good` — cargo tile counts. HIGH.**
Same pattern: small counts on thumbnails, and the whole tracking loop depends on them.

**4. `actions/fleet_status`, `vision/hud_readers.read_cargo` — MEDIUM.**
Large panel text; read correctly tonight. `hud_readers` already crops twice.

**5. `actions/overflow_dialog`, `vision/region_detectors/market_restock` — INHERITED.**
Take `elements` from the caller, so they inherit whatever the caller parsed.

**6. Coarse consumers — NO CHANGE.** family classifier (224×224), minimap/shoreline tag
readers, chrome flags, obstruction presence, dialog presence, button-finding by label.
Downscale is correct for all of these.

## The fix pattern

Read the region, and MERGE with the whole-frame parse rather than replacing it — the two
fail on different items:

    full frame:  358, 102, 102        + Candle 102      − Matchlock 44
    panel crop:  358, 102, 44         + Matchlock 44    − Candle (read 402)

Whole-frame authoritative where it has a value; the crop fills the gaps. Neither alone is
correct on that frame; together they are.

## The merge, demonstrated

Measured end to end on the Village Info trade list. Neither read is correct alone:

    whole-frame: {Birch Tree: 358, Iron: 102,                   Candle: 102}
    region  x2 : {Birch Tree: 358, Iron: 102, Matchlock Gun: 44, Candle: 402}
    MERGED     :                   Iron  102, Matchlock Gun  44, Candle  102   <- all correct

Three things had to be right, and the first two attempts were wrong:

1. **The region comes from the panel's own landmarks, not from constants.**
   `_list_viewport(elements)` already gives the vertical bounds ("Trade List" tab down to
   the "View by Min. Exchange Unit" checkbox); the same landmarks give the horizontal ones.
   Deriving x from "every element in that row band" instead sweeps in the village list on
   the left and reproduces the whole-frame failure.

2. **The region must be UPSCALED, and cropping alone is not the fix.** The correctly-derived
   548x582 crop does NOT read the `44`; the same crop at x2 does. A hand-picked 750x520 crop
   read it and a tighter, more principled one did not — so an unscaled crop that happens to
   work is luck, not method. x2 is the smallest that worked here, and the sea-HUD reader
   needed the same x2 for the same reason.

3. **Merge by ASSOCIATION, never by coordinate.** The two parses disagree on box geometry —
   the crop's `358` maps back to y~505 where the frame has it at 462 — so any absolute-y
   pairing shifts rows. A row's quantity is drawn BELOW its name, so bind each number to the
   nearest name ABOVE it, using only the ordering WITHIN one parse. And every row label must
   be a candidate including the GOOD's: with only materials eligible, Birch Tree's output
   quantity `358` binds to Iron and displaces the real value.

   This is `memory/identify-by-association-not-dimension` applied within a row, and it is
   what makes merging two disagreeing parses possible at all: reading order is the only
   thing they agree on.

## Correction, 2026-08-27: read the TILE, not an upscaled whole frame

Fresh frames from Barcelona overturned the "upscale the region" recommendation above. Truth
read off the tiles by eye and user-confirmed (`Lemon Oil 1`, `Neroli 2`):

| read | Lemon Oil (1) | Neroli (2) |
|---|---|---|
| full frame | `None` — silent | **2** correct |
| whole frame ×2 LANCZOS | **1** correct | **8 WRONG** |
| whole frame ×2 bicubic | **31 WRONG** | **2** correct |

Every whole-frame variant is wrong somewhere, **and the resample filter changes the answer**.
So filling a gap from an upscaled whole-frame read is not safe — it could have written `31`,
and a wrong number is worse than a missing one because nothing downstream can tell.

What works is a tight crop of the ONE tile, upscaled ×4: `Lemon Oil 1`, `Iron 2,099`,
`Gunpowder 2` all correct, including the value no whole-frame variant got right.

    full frame reads what it can          -> keep every value it produces
    for each good it left as None         -> crop THAT tile, ×4, read the trailing number

The tile crop earns its reliability by having ONE number alone in the image instead of one of
forty in a busy 2400×1080 frame. It is also cheap: only the gaps need it.

The trade-list merge earlier in this document stands — that panel is a vertical list and its
region read was verified — but `rows_by_association` is built for stacked rows and does NOT
work on the 3×3 market grid, where names and numbers sit side by side in tiles.

## What must not be done

Do not simply swap the whole-frame parse for a crop everywhere. The crop has its own errors
(`402` for `102` above), and the whole-frame parse is what makes per-tick perception sharing
affordable. The rule is *region-parse the content that decides something*, not *stop sharing*.
