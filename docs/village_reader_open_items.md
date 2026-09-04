# Village reader — known gaps

State as of 2026-08-25, after the stitching rewrite. Ordered by what would bite first.

## 1. Chained materials are stored as if they were port-bought

`recipes.json` gives every material a `source_ports` list and nothing that says "this comes
from a village". `Moccasin <- American Bison` and `Naverslojd <- Birch Tree` are barter goods,
and `Eagle Feather <- Pulque, Guarana` needs barters at two OTHER villages first. A gathering
plan built from the current KB would send the fleet shopping for goods no port sells. See
`docs/game_mechanics.md` → *Multi-stage barter*. **This blocks the highest-value goods.**

## 2. `village_check` leaves a partial village record — by design

A mission stops scrolling once the good it came for is fully read. Correct for the mission,
wrong for the knowledge base: San Village's record looked complete while `Prunus Padus` had
never been scrolled to, and its recipe was stored empty. The log now says PARTIAL, but nothing
re-reads the village later. **Every village record built by a mission is suspect.**

## 3. A row with no thumbnail and no pin cannot be classified

All three good/material signals can be absent at once: at Svear, `Iron`'s quantity came back
as bare badge text (24px, no thumbnail) with its pin below the icon floor. Stitching covers it
in practice — the row classifies correctly on screens where its thumbnail IS detected, and the
sequence takes that reading — but a row that never gets a thumbnail on ANY screen is decided
by its pin alone.

## 4. The stitch trace keeps only the last pass

`learn_village_barter` runs two passes and each overwrites `/tmp/village_stitch/<village>.json`.
When pass 1 is clean and pass 2 degrades, the debug output shows only the degraded one — which
is precisely backwards for diagnosis. Make it per-pass.

## 5. The icon floor is lowered only for the Trade List

`trade_list_elements()` uses 0.18; everywhere else keeps the global 0.30. The same
distribution problem plausibly affects other panels, but nothing has been measured there, so
the change was deliberately kept narrow. Measure before widening.

## 6. `village_check` has not been exercised by a real mission

It shares every component with the learn tool and passes its unit tests, but every end-to-end
run today went through `learn_village_barter`. The last live mission through `village_check`
predates all of these changes. **A barter mission is the honest test.**

## 7. The scroll cannot be aimed

Measured: 150px requested delivered 384px; 300px delivered 180px once and 429px another time;
longer durations do not reliably tame the fling. The reader compensates by keeping requests
small and relying on overlap, and by measuring what actually moved. Do not add code that
assumes a requested distance was delivered.
