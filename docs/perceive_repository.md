# The Perceive Repository — data-oriented perception

**Status: DRAFT, not decided.** Nothing in the running bot depends on this yet. The seam
(`brain.perceive.current_observation` / `is_current`) exists and is tested; no caller uses it.

---

## 1. The thesis

> A design is easier to build and to maintain when the DATA is centralized and properly
> categorized, and the operations on it are wrapped with it. Then the flow of data is clear
> and the operations are in one place instead of scattered. A good design is data-oriented,
> not flow-execution-oriented — and long sub-loops are the central symptom of the flow-
> oriented kind.
>
> Every system acquires data, stores it, transforms it, and presents it. In this bot the
> central datum is THE SCREEN: capture and parse acquire it, actions transform it — and what
> makes it hard is that **the game transforms it too**.
>
> — user, 2026-08-31

That last clause is the whole difficulty, and it is what separates this from an ordinary
repository. We are not the only writer.

## 2. The central datum and its lifecycle

| Stage | What it is here | Measured cost (2026-08-31, this machine) |
|---|---|---|
| **Acquire** | `adb exec-out screencap` → PIL frame | **1.70 s** |
| **Acquire** | OmniParser parse of that frame | **2.90 s** |
| **Acquire (cheap)** | coarse frame diff, ignores clock/HUD | **0.066 s** |
| **Store** | three disconnected caches (§4) | — |
| **Transform (ours)** | 9 primitives in `actions/adb_actions.py` | 0.3–0.8 s per tap |
| **Transform (theirs)** | the game: arrivals, notices, rewards, idle lock, animation | unannounced |
| **Present** | classification, activity decisions, logs, the action trace | — |

A full observation is ~4.6 s. Capture is **not** the cheap half, so "capture freely, parse
rarely" does not apply.

## 3. Two writers, only one of whom tells us

Our writes are **known**: we called `tap`, so we know the screen may have changed.
The game's writes are **discovered**: a voyage ends, a reward dialog opens, the client
idle-locks, an amity cap fires a `Receive` button. Nothing notifies us.

Consequences that the design must accept rather than wish away:

- The repository's copy is never authoritative. It is only **the newest thing we have seen**.
- Therefore staleness cannot be a clock. *(user, 2026-08-31)* Data from frame N is stale
  exactly when frame N+1 exists — not when N is old. At sea, with a 1200 s pacing sleep, a
  twenty-minute-old frame is still the current word. In a market, a two-second-old frame is
  superseded the instant a tap produces a newer one.
- Detecting the game's writes costs a capture (1.70 s). The 0.066 s diff can only confirm a
  change *after* we have paid for the frame; it can never tell us whether to capture.

## 3.1 ONE TAP CAN START A SEQUENCE

*(user, 2026-08-31, after this design caused a live failure — and this one line is the whole
of it; everything below is consequence.)*

Not one change. A SEQUENCE, of unknown length, playing out over unknown time, which the game
performs without us:

    tap `Move to City`
      → the world map closes → port overworld → the sailor walks to the harbour
      → departs → SEA
      → (an idle lock, if we are slow)
      → the arrival cinematic → port overworld AT THE DESTINATION

One tap, and every one of those is a consequence of it. The same shape covers sailing to a
village, and `Exchange` producing a result dialog then a reward dialog then the panel again.

**AND THE CHAIN HAS NO KNOWN END** *(user, 2026-08-31)*. It is tempting to file arrival and
the idle lock under "the game acted on its own" — a separate category from "we acted". They
are not separate. They are LATE consequences of the same tap, arriving minutes or hours after
it. So "we acted" and "the game acted" is not a partition of causes; it is the same causal
chain, and we know only where it STARTS.

That is what rules out every clever scheme, including the one this design nearly adopted: *we
tapped, so expect a change, and then it is settled.* Settled when? `settle_s` covers the
immediate animation. Nothing can cover the tail, because the tail has no length we can know.

So "act once, look once" is wrong, and it is wrong generally rather than in one place.

**THE RULE, which follows from that one line: the repository deduplicates readers WITHIN a
step. It must never reduce the number of steps that LOOK.**

Because a tap starts a sequence rather than causing a change, there is no moment at which the
bot may stop looking and still be right. Every scheme for looking LESS is a bet on knowing
where the sequence ends, and that is the one thing we never know.

The value of one owned observation is that the readers inside a step stop each taking their
own frame — not that the dispatcher takes fewer. Making the dispatcher's looking conditional
is exactly where the two-writers problem bites, because the condition can only ever be phrased
in terms of the writer that announces itself: us.

WHAT IT COST TO LEARN THIS. Before the repository, `perceive` called `capture_screen()`
unconditionally — every step was a fresh look, and the flow above worked by brute force: one
step saw `port_overworld`, the next saw `sea`. Binding the look to invalidation broke it
within minutes on 2026-08-31: the fleet departed for Bordeaux, and `observation 11` was the
LAST capture the run ever made. The bot classified `port_overworld` four more times off that
frame — trying to walk into a harbour it had already sailed from — while the ship was at sea
with a daily-news popup on it.

The correction is not clever: `step()` asks for a fresh look every time, which restores the
old cadence exactly. The saving is unchanged, because it was never in the number of steps.

Corollary for the rest of the migration: **making an unconditional thing conditional is where
the risk lives.** Keep the old cadence; remove only the duplicates.

## 4. What exists today

**Caches, but no repository.**

| Store | Key | Holds | Reachable by |
|---|---|---|---|
| `vision/omniparser._FRAME_CACHE` | `id(frame)` + identity check | parses | whoever holds *that frame object* |
| `vision/frame_cache.FrameCache` | `(id(frame), extra)` | family classifier, moondream, minimap, shoreline | same |
| `brain/perceive._PERCEIVE_LAST_*` | — | the last full perception | perceive, `action_trace`, and now `current_observation()` |

The key is **identity, not content**. Two consumers handed the same frame object share the
parse — which is why a run showed ~111 captures but only 46 parses. Two consumers that
capture *separately* get two objects, two parses, and two different pictures of the world,
with nothing able to reconcile them.

So: the parse is centrally cached; **the frame has no owner at all**. Sharing is achieved by
passing the object down a call chain, and it stops the moment a callee calls
`capture_screen()` itself — which **215 call sites** do (`actions/` 136, `brain/` 72 — of
which `perceive.py` is 6 — `vision/` 7).

## 5. What scattered acquisition actually cost (all live, 2026-08-30)

Not hypothetical. Each of these was traced to a frame nobody owned.

- **Two truths in one tick.** At Faro the dispatcher classified one capture while
  `sell_goods` took another. The chromed title had flipped to `Sell` while the goods grid was
  still `Purchase` — each reading correct *of its own frame*. The bot read the shop's stock as
  its hold and loaded Ammo, Chicken Meat and Turron — never bought — into a sell basket. No
  downstream check could have caught it: both readings were true.
- **Seventeen reads, seven answers.** One barter session called `read_barter_panel` 17 times
  and produced 7 distinct readings. Three identical reads per round, ~7 s each.
- **The same question asked twice.** Startup established position, then established it again
  nine seconds later on an untouched screen: 9 captures and 84 s before the first tap.
- **Inferring what was written on screen.** "The day's rounds are spent" was deduced three
  different ways — a locked ribbon, a notice, a vanished panel — while the Trade Count strip
  stated it outright (`[859, 859, 898, 898]`, four of six spent). The run left two rounds
  and ~45M ducats at the village.

## 6. Why long sub-loops are the symptom

A sub-loop is flow-oriented by construction: it takes control, and because it holds control it
also **re-acquires its own data**. While it runs, `d.tick()` has not returned, so no tick
observation exists — its captures are the only ones there are.

That is not itself wrong: a sub-loop that taps a tile and reads back *must* observe again,
because it acted. What is wrong is that it observes **more often than it acts**, and that its
observations are invisible to everyone else.

Data-orientation dissolves this. If the datum persists and is owned, the loop can return
after each action, because the next caller reads the same store rather than rebuilding it.
That is the same argument as "one loop, no flow sub-loops" (CLAUDE.md #5) — arrived at from
the data side rather than the control side.

## 7. Proposed design

### 7.1 Categories of the datum

The repository holds one **Observation** per generation, and derivations are cached against
it — never against a private copy:

| Category | Examples | Derived how |
|---|---|---|
| **Raw** | the PIL frame | capture |
| **Whole-frame** | OmniParser elements, OCR tokens, nav state | parse, once per generation |
| **Region** | title crop, price tile, Trade Count strip | crop of the held frame, ~0 cost |
| **Interpreted** | barter panel reading, market goods, sea HUD | a reader, over the above |

Region reads deserve first-class support: a tight crop is not merely cheaper, it is often
*more accurate* — the Lisboa price read 0.372 whole-frame (below the 0.40 cutoff, dropped)
and 0.634 from its own tile.

The interpreted layer should carry the owner it already has in CLAUDE.md #4
(COMPANY > FLEET > PLACE > BUILDING > PANEL), so invalidating a place invalidates its panels.

### 7.2 CAPTURE is sealed; the frame is shared read-only

*(user, 2026-08-31, revising an earlier "the frame never leaves")*

Some consumers genuinely need pixels. The family classifier is a CNN over the raw image —
`classify_family(frame)` decides chromed / sea / world-map, and no amount of bbox, icon or
text can serve it. Sealing the frame in would either move CNN inference into the repository
(interpretation, over the line) or break the classifier outright.

So the rule is narrower and stronger:

> **The frame may be ACCESSED by others, but not MODIFIED.**

The defect was never that callers hold a frame — it is that they **capture their own**. One
shared frame that many readers examine is consistent by construction; two independently
captured frames are the Faro failure. So capture is what the repository seals; the frame it
holds is handed out read-only.

**It must be the SAME object, never a copy.** Every cache in `vision/` keys on `id(frame)`
guarded by a reference — `parse_fast_cached`, and `FrameCache` for the family classifier,
moondream, minimap and shoreline readers. `classify_family`'s own docstring gives the reason:
"cached so multiple callers within one perceive tick share inference cost". Handing each
caller a copy would give each a fresh id, miss every cache, and reintroduce the cost the
sharing exists to avoid.

**Read-only is a real constraint, not a formality.** PIL images are mutable; a consumer that
draws on or pastes into the shared frame corrupts every other reader's view — and every cached
answer already derived from it. Background work must still copy before it leaves the main loop,
which is what `brain/agent.py` already does (`frame.copy()`, "PIL images are not thread-safe to
share"). Whether this is enforced by convention, by a wrapper, or by a ratchet test is
undecided.

**This also settles `_hog_sim`** (§7.3): patch matching can take the shared frame and read the
patches it needs. The sealing rule no longer costs anything there.

**And it keeps nav state OUT.** Classifying a screen is interpretation — a cascade of gates,
vocab matches and CNN verdicts. With the frame accessible, the classifier lives outside the
repository and reads from it, rather than the repository returning a verdict it had to
interpret to produce.

### 7.3 What the repository owns, and where the line is

**Owns — acquisition and primitive derivation:**

| | Today's scattered form |
|---|---|
| capture | `capture_screen()` × 215 |
| whole-frame parse | `parse_fast_cached(frame)` |
| OCR | `read_text(...)` × 68 call sites |
| region crops | ad-hoc crops in readers (price tile, Trade Count strip) |
| pixel questions (boundary — see below) | `has_positive_background` ×3, `_hog_sim` ×3 |
| frame diff (boundary — see below) | `classify_action_outcome` ×2 |
| **recording** | the action trace (see §7.5) |

**Does NOT own — interpretation.** *(user, 2026-08-31)*

> This repository only manages frames and provides what info is in the regions the callers ask
> for, and tells the caller if they are asking for stale data (i.e. the caller is lost). It
> does not interpret. The goal is to centralize frame capturing and its life-cycle management,
> and the reading of primitive info — bbox, icon, texts. Nothing being interpreted.

So the vocabulary it returns is exactly that: **bounding boxes, icons, text**. "What does this
panel mean", "is this good sold out", "is the day's barter finished" all stay with the readers,
which consume boxes/icons/text instead of pixels. This is the line that keeps the repository
from absorbing the vision layer: it answers *what is on the screen*, never *what it implies*.

**Three cases sit on the line and are NOT yet decided:**

| Case | Sites | Argument |
|---|---|---|
| `has_positive_background` — is this box's background gold? | 3 | The pixel read is primitive; "therefore Exchange is live" is interpretation. Probably in, as a colour primitive. |
| `classify_action_outcome` — did the screen change? | 2 | A comparison of two frames the repository already owns; "the tap did not register" is interpretation. Probably in. |
| `_hog_sim` — do these two patches depict the same thing? | 3 | SETTLED by §7.2: it reads the shared frame directly. It is a matcher used to identify a good, so it stays OUTSIDE the repository. |

The third is settled by §7.2: with the frame readable, pixel matchers need nothing from the
repository but the frame it already holds. The first two are answered by §7.8 — they are not
repository primitives at all, they are **UI-recognizer** vocabulary: "gold" and "changed" are
facts about the game's visual grammar, one layer above a crop and one below a meaning.

A practical consequence for the readers: functions like `read_barter_panel(frame)`,
`read_market_page_omni(frame)`, `read_destinations(frame)`, `read_crew(frame)` currently do
BOTH — they parse and they interpret. They split: the parse moves in, the interpretation stays
and takes elements, crops and text as arguments. Several already have the seam —
`read_market_page_omni(frame, elements=None)` and `read_fleet_state(elements, frame)` take
pre-computed elements when given them.

### 7.4 API sketch

**The governing invariant: EXACTLY ONE MEMBER ACQUIRES.** Everything else reads what is
already held. That makes "who can cost 4.6 s, and who can create a new observation"
answerable by looking at one function instead of auditing 215 call sites.

```
PerceiveRepository

    # ---- the ONLY acquiring call. Full screen. May capture, may parse, may block. ----
    get_perceive_data(need=...)  -> elements / nav state / ...      # NOT the frame

    # ---- non-acquiring. Served from the held observation; NEVER capture. ----
    read_region(box, how=...)    -> value        # crop -> OCR / colour / template
    for_logging()                -> whatever is held, plainly marked as such

    # ---- bookkeeping ----
    invalidate(why, not_before=...)   # an action happened; do not look before `not_before`

    # ---- private ----
    _is_current(...)             # sealed together with capturing; never public
```

**`get_perceive_data` — full screen, may acquire.** Captures iff invalid, otherwise serves
what it holds. Derivation stays lazy per kind.

**`read_region` — a member, but never acquiring.** Region reads belong ON the repository
because they must read THE SAME observation everyone else is reading, and because their
results cache naturally per `(observation, region)` — which `FrameCache` already supports via
its `extra` key (`key = (id(frame), extra)`).

Region reads are where accuracy lives, not just economy: the Lisboa price scored 0.372
whole-frame (below the 0.40 cutoff, dropped) and **0.634 from its own tile**.

**`for_logging` — never acquires, ever.** A log line changes nothing, so it must never cost a
frame *(user, 2026-08-31)*. Returns what is held, possibly stale, possibly nothing, always
marked. The codebase already names this failure at `brain/barter_command.py:669` — "a full
OmniParser pass for a log line".

### 7.5 Recording collapses in too

*(user, 2026-08-31)* With the frame and its derivations owned in one place, recording is the
repository's job, and the open question about it disappears.

Measured on the 2026-08-30 session: **111 recorded frames, 2 with perception attached.** Not
only the 78 sink captures — 28 of 28 taps carried none either. `record_tap` attaches the
tick's parse only when a coarse diff says the frame is UNCHANGED since that perceive, which
inside an acting sub-loop it never is. The gate is right (it prevents misattribution); it
simply almost never fires. So the viewer re-derives nearly every frame at 2.9 s — about 5.4
minutes of the ~10 it took.

Owned together, "record one observation with whatever was derived from it" is exact and free:
the parse is already in hand, and no diff is needed to decide whether it belongs to the frame.

### 7.6 The two hooks, and why both are required

1. **Capture goes in.** `capture_screen` becomes private to the repository. Leaving it public
   is precisely what produced 215 call sites; discipline that every caller must remember has
   already been tried here and lost.
2. **Invalidation comes out of the action layer, automatically.** `actions/adb_actions.py` is
   a real choke point — `tap`, `tap_fast`, `long_press`, `swipe`, `swipe_fast`, `press_back`,
   `wake`, `pinch_zoom`, `input_text` — and "never raw `input tap`" is already enforced by
   convention across the codebase.

**Hook 1 without hook 2 is worse than today.** A caller that reads without invalidating gets
a stale frame, where today it would at least have captured a fresh one. That trades a visible
cost for an invisible correctness bug. Hook 2 makes the rule structural: an action invalidates
whether or not anyone remembered to say so.

### 7.7 Settle belongs to invalidation

Capturing immediately after a tap yields a mid-animation frame — worse than none. The
primitives already know their dwell, so `invalidate()` carries a *not-before* stamp. This is
what the **55 settle waits** scattered through `actions/` are really doing, each locally and
invisibly.

Sleeps invalidate too: after `_sleep_jittered(1200)` at sea the world has moved. That is the
whole of "time" in this design — not a staleness rule, just an event that invalidates.

### 7.8 A UI-recognizer layer sits between primitives and interpretation

*(user, 2026-08-31)* Between "text and crops" and "what this panel means" there is a third
thing: **the game's visual grammar**. Gold means affirmative. Grey means unavailable. A red
ribbon means locked. A brown title bar means an action dialog rather than a game popup. A
ducat icon is a ducat icon, wherever it appears.

That vocabulary is screen-agnostic and game-specific. It is not a primitive — a primitive is a
box, an icon, a string — and it is not interpretation, because "gold" does not say what the
button DOES. It deserves its own layer.

**Half of it already exists.** The STRUCTURAL recognizers are consolidated in
`vision/region_detectors/` — `action_buttons`, `dialog`, `left_menu`, `panels`, `overlay`,
`commit_button`, `composite_buttons`, `bottom_chrome`, `top_left`, `market_restock`,
`sea_destination`, `building_npc_overlay` — plus `chrome_detector`, `chrome_via_omniparser`,
`panel_context` and `scene_model`.

**The APPEARANCE recognizers are not**, and they have drifted into the wrong layers:

| Feature | Lives in | Should be |
|---|---|---|
| `has_positive_background` (gold = affirmative) | `brain/commit_actions.py` | UI layer |
| `tile_in_stock` (grey = unavailable) | `actions/buy_materials.py` | UI layer |
| `tile_grayed` | inline, no definition | UI layer |
| `is_locked` (red ribbon) | inline, no definition | UI layer |
| ducat icon | **nowhere** | UI layer |
| brown dialog title vs game popup | **only in a memory note** | UI layer |

So "is this button gold" is currently a BRAIN concern, and "is this shelf empty" belongs to the
buy loop. Each site rediscovers the grammar by hand, which is how the same visual fact gets
three different implementations and one of them is wrong.

**What it would have cost less of, from 2026-08-30 alone:**

- the commit loop tapped a gold `Ok` agreeing to discard 460 units of cargo — it knew "gold"
  but had no vocabulary for what the dialog was;
- a grey Exchange is the only sound ending for a barter, and it was spelled inline;
- the locked ribbon was one of two "the day is spent" signals; the other went unwritten for
  months;
- the **ducat icon** was mistaken for the daily-news close button, because nothing named it;
- the brown-title rule ("tapping outside dismisses an action dialog but not a game popup") is
  real, load-bearing, and exists only as prose in a memory file.

**The layering, then:**

| Layer | Answers | Example |
|---|---|---|
| Repository (§7.2–7.4) | what is on the screen, primitively | this box, this icon, this text, this crop |
| **UI recognizer** | what KIND of control or chrome this is | gold button, grey tile, locked ribbon, brown-title dialog, ducat icon |
| Readers | what this screen MEANS | "the day's rounds are spent", "Birch Tree costs 13,455" |
| Activities | what to DO about it | commit a round, leave the village |

Undecided: whether this is one module (`vision/ui_features.py`) or a package alongside
`region_detectors/`; and whether the structural detectors move under the same name, since they
are the same layer under a different aspect.

## 8. Migration

Nothing here requires new caching machinery; `FrameCache` is sound. What is missing is an
owner for *which frame is current*.

1. **Seam** (done): `current_observation()` / `is_current()` / generation counter.
2. **Skeleton**: `PerceiveRepository` + the `adb_actions` invalidation hook; `perceive`
   delegates to it. No caller changes.
3. **Prove on one path**: the barter panel — the best-measured case (17 reads → 7 answers).
4. **Sweep by module**, largest first: `sail_actions` (63), `market_actions` (34). Most sites
   need only to *pass the frame they were given*; **31 functions already accept `frame=None`**
   and capture only when it is absent.
5. **Ratchet**: a test asserting only the repository calls `capture_screen`, with today's
   offenders as an explicit shrinking allowlist — the pattern `test_the_layering_is_enforced`
   already uses.

## 9. Status of the questions

### 9.1 Decided

| Question | Decision |
|---|---|
| Blocking cost (4.6 s per acquisition) | One acquiring call, N non-acquiring ones (§7.4). `read_region` and `for_logging` never capture. |
| `is_current` as a public promise | Sealed and made private (§7.2). Currency is enforced, not reported. *The counter added 2026-08-31 is incomplete — one bump against 215 captures — and has no consumers; do not rely on it before the repository exists.* |
| Recording frames without their parse | Resolved by ownership (§7.5). Measured 111 frames, 2 with perception; owned together, the parse is in hand. |
| Frame access | CAPTURE is sealed; the FRAME is shared read-only (§7.2). The CNN family classifier needs pixels, and copies would defeat every `id(frame)` cache. |
| Nav-state in the acquiring call | OUT. Classification is interpretation; the classifier reads the shared frame from outside. |
| Pixel matching (`_hog_sim`) | Stays outside. With the frame readable it needs nothing from the repository. |
| Capture failure | **Retry a few times, then STOP** (user). A capture that will not succeed means the bot is blind, and a blind bot must not act on remembered pixels. |
| Test frames | **Real captures, dedicated folder, NOT in the repo** (user). Done 2026-08-31: 22 PNGs untracked and ignored. Reverses a 2026-08-26 decision; the "survive a prune" concern is met by a durable folder outside the repo, at the cost that a frameless checkout skips those tests QUIETLY. |
| Enforcing read-only | **Accepted as UNENFORCED** (user), caught by review. Copies defeat the caches; a wrapper breaks `classify_family` and `_hog_sim`, which need a real `PIL.Image`; a ratchet catches only the mutators it knows (`paste`, `putpixel`, `ImageDraw`, `thumbnail` — not a write through a numpy view). Nothing here has reason to modify a frame. |
| Cull the suite before migrating | **DROPPED as a precondition** (2026-08-31). It rested on the suspicion that much of the suite was stale. All 11 then-failing tests were examined: 4 were contract changes from that day's work, 2 drifted stubs, 1 a count assertion, 1 a global-state leak, 3 fixtures behind a sector change — **none stale in the way assumed, and none deleted**. The suite is now green at 4,033. |

### 9.2 Constraints the migration must carry (not questions)

- **The test seam moves in the same commit.** `tests/conftest.py` patches `capture_screen`
  AND `_capture_screen_raw`, autouse, for the whole suite. Sealing capture breaks that; tests
  must inject into the repository instead. This is the riskiest single step in the plan.
- **The capture sink must become a list.** The hook added 2026-08-31 is a single global slot,
  so a tracer and anything else wanting capture notifications would fight over it.
- **The repository is main-loop-only.** Background work is HANDED an Observation, never
  fetches one. `brain/agent.py` already follows that shape with `frame.copy()`; the rule needs
  stating rather than discovering. No live collision today — it never captures in its thread,
  and it uses `omni.parse`, not the unlocked `parse_fast_cached` cache.
- **The reader split is smaller than first estimated.** That "68 `read_text` sites" figure was
  computed under the abandoned *frame-never-leaves* rule. With the frame shared, a reader
  crops it and asks for OCR — it only stops CAPTURING — and 31 functions already accept
  `frame=None`. Readers keep their region knowledge. Still unmeasured, but a smaller job.

### 9.3 Genuinely open

**None.** The last three were settled together on 2026-08-31 by one observation: the UI layer
is not a thing to found, it is a thing already standing.

- **Where it lives — `vision/region_detectors/`.** It holds twelve modules, and `dialog.py`
  contains **DialogModel** — a recogniser of a KIND of UI, not of a region. The package name
  undersells what it already does. The appearance idioms join it as a module rather than
  starting a home elsewhere: `has_positive_background` (from `brain/commit_actions.py`),
  `tile_in_stock` / `tile_grayed` (from `actions/buy_materials.py`), the locked ribbon, the
  ducat icon, and brown-title-vs-game-popup. Its siblings by shape live one level up —
  `ChromeDetector`, `SceneModel`, `GridModel`, `ShipHeadingDetector` — while `WorldModel`,
  `StuckDetector` and `JunctionDetector` sit in `brain/` because they INTERPRET.
  *Renaming the package to match its scope is churn for little gain; noted, not done.*

- **What `need=` enumerates — elements and OCR tokens.** The vocabulary is bbox / icon / text
  (§7.3), and nav state is out (§7.2).

- **DialogModel was never a placement question.** It is already in the layer. What is open is
  a DEFECT: `vision/obstruction_classifier.py` calls it "the single source of truth for dialog
  detection AND dismissal" (2026-05-23), yet a legacy heuristic still runs beside it and
  disagrees in production, and on 2026-08-30 it returned `kind='none'` for the "moving to
  another menu will empty the cart" confirmation — which wedged a run for six ticks while the
  legacy signal was suppressed. Two owners of one visual fact, plus a blind spot. That is the
  layer's first repair job, and it is work rather than a decision.
