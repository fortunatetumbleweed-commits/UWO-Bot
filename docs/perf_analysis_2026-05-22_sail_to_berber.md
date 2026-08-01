# Performance breakdown — 2026-05-22 sail-to-Berber cycle

Analysis of a 12-minute Sail-to-Berber attempt (09:20:20 → 09:32:08).
The earlier summary attributed 72% to "anti-cheat delays + game
animation" — this doc decomposes that bucket step by step and
identifies which delays are *policy* (changeable now) vs *compute*
(needs YOLO/architecture change) vs *game* (immovable).

## Annotated timeline

Each row in the inner column shows where the time goes between the
log entry above and the log entry below.

| Time | Event | Gap to next | What the gap is composed of |
|---|---|---:|---|
| 09:20:20.2 | Qwen perceive (sea) starts | 8.6 s | **Qwen compute** (mlx model inference, first call → +1.2 s model load) |
| 09:20:28.8 | Qwen result | 9.0 s | sail_to tick processed (`delay=2.0`) + departure-confirmed flow + ADB screencap + chrome detect |
| 09:20:37.8 | "Departure confirmed" | 26.3 s | next perceive cycle (no Qwen) — capture + OmniParser (~2 s) + OCR (~0.5 s) + classify cascade (~2 s) + `_detect_interruptors` walking 31 interruptors + obstruction bbox classifier ≈ **15 s of compute** + 10 s of `time.sleep` in the flow loop |
| 09:21:04.0 | Obstruction detected (HUD false-positive) | 5.9 s | **Claude consult** API call (network + Sonnet inference) |
| 09:21:09.9 | Consult result (dismissal=unknown) | 9.1 s | dismissal skipped (`unknown`) + next perceive cycle |
| 09:21:19.0 | Obstruction detected again, cache hit visit #2 | 7.4 s | **Qwen perceive (sea)** |
| 09:21:26.5 | Qwen result | 30.2 s | sail_to tick + `_open_world_map_from_sea` setup + ADB tap on mini-map + `time.sleep(2.0)` flow waits ×3 + perceive cycle |
| 09:21:56.7 | Obstruction detected (different hash, again HUD) | 5.8 s | **Claude consult** |
| 09:22:02.5 | Dismiss via tap_anywhere | 0.0 s | tap centre + 1.5 s human delay |
| 09:22:02–43 | (40.8 s gap) | 40.8 s | post-dismiss perceive cycle: ADB capture + OmniParser + OCR + classify + `_has_daily_news_close_x` pixel signature fires → **Moondream confirm** (~12 s) → returns NO (false positive) — total cycle ≈ 30 s |
| 09:22:43.2 | Moondream confirms "NO" | 13.0 s | continue classify cascade + obstruction detect again |
| 09:22:56.2 | Classify → sea | 0.0 s | Qwen call starts |
| 09:22:56–23:08 | (12.0 s) | 12.0 s | **Qwen perceive** — unusually slow (12 s vs 7 s typical, probably due to longer prompt with all sea overlays in context) |
| 09:23:08.2 | Qwen result | 10.9 s | sail_to tick + open_world_map_from_sea state check |
| 09:23:19.1 | Tap mini-map | 14.3 s | tap delay (1.5 s) + **game animation** opening world map (~10 s) + OCR poll (1 s) |
| 09:23:33.4 | World map open confirmed | 41.4 s | post-open perceive cycle: full classify cascade, no Qwen yet — includes 20 s `delay=20.0` in sail_to's SEA_NAVIGATE phase? OR multiple `time.sleep(2.0)` accumulating in transition. **Largest unexplained gap.** |
| 09:24:14.8 | Classify → world_map | 5.0 s | **Qwen perceive (world_map)** |
| 09:24:19.8 | Qwen result | 9.8 s | sail_to tick + dest-dispatch logic |
| 09:24:29.6 | "berber → village navigation" | 29.8 s | `_navigate_world_map_to_village` setup: chrome glossary load + capture frame + Qwen prompt build + classify → re-perceive |
| 09:24:59.4 | Classify → world_map | 5.2 s | **Qwen perceive (world_map)** |
| 09:25:04.5 | Qwen result | 20.4 s | `select_world_map_tab("explore")` → OmniParser parse (2 s) → tap explore tab (1.5 s) + `time.sleep(1.5)` settle |
| 09:25:24.9 | Tap explore tab | 3.4 s | tap delay + settle |
| 09:25:28.3 | pan_to_port starts | 1.6 s | stride compute + capture |
| 09:25:29.9 | Stride skipped | 10.9 s | iterative pan attempt: capture + OmniParser + OCR (~5 s) + `parse_visible_ports` + swipe gesture (~600 ms) + `_PAN_SETTLE_S = 1.5 s` + capture again |
| 09:25:40.8 | FOUND Berber Village | 10.9 s | **`tap(village_pos)` (1.5 s tap delay) + `time.sleep(3.0)` (post-village-tap) + 6 s of game-animation opening village info panel** |
| 09:25:51.7 | Tap village | 15.5 s | poll loop for Move-to-Village button: `capture_screen` + `_ocr_frame` + 2 s sleep interval — happened ~5–7 times |
| 09:26:07.1 | Move to Village tap fires | 29.1 s | **`time.sleep(8.0)` after tap + 8 s game animation (panel did not close because tap landed wrong) + perceive cycle** |
| 09:26:36.3 | Obstruction (Village Info panel still up, visit #4) | 9.8 s | dismiss via tap_close_x (cache hit, no consult): perceive + DialogModel close_button lookup + tap + 1.5 s tap delay |
| 09:26:46.0 | Dismissed | 41.3 s | post-dismiss perceive cycle: full classify cascade (no Qwen yet) + sail_to `delay=2.0` + flow waits |
| 09:27:27.3 | Classify → world_map | 5.1 s | **Qwen perceive (world_map)** |
| 09:27:32.4 | Qwen result | 10.9 s | sail_to abort logic + perceive |
| 09:27:43.2 | "Aborting" | 35.1 s | tick delay + retry loop |
| 09:28:18.3 | Classify → sea (auto-recovered to sea) | 7.2 s | **Qwen perceive (sea)** |
| ... | …repeats 2 more cycles… | ~3 min | re-attempt with same OCR result this time successful at 09:32:01 |
| 09:32:08.7 | "Sailing to berber started" | — | done |

## Categorised totals

Adding up the gaps over the 720-second wall clock:

| Category | Time | % | Notes |
|---|---:|---:|---|
| **Qwen perception** | 73 s | 10.1% | 11 calls × ~6.6 s avg.  YOLO Phase 4a kills this for ~80% of calls. |
| **Claude obstruction consults** | 12 s | 1.7% | 2 fresh consults; the other obstruction encounters were cache hits (free). |
| **Moondream daily_news confirms** | ~13 s | 1.8% | One false-positive trigger; ~12 s of Moondream inference. |
| **OmniParser + OCR + classify cascade** | ~100 s | 14% | Distributed across every perceive cycle; ~10 calls × ~10 s. Cached per frame_id but not across ticks. |
| **`time.sleep(N)` in flow code** | ~95 s | 13% | Mostly `time.sleep(2.0)` between flow steps, plus `time.sleep(8.0)` after Move-to-Village, plus `time.sleep(3.0)` after village tap, plus 2 s poll intervals. |
| **`_human_delay()` after each tap** | ~25 s | 3.5% | ~15 taps × ~1.6 s avg.  TAP_DELAY_MIN/MAX is 1.0–2.0 s in config. |
| **sail_to tick `delay=N`** | ~40 s | 5.6% | `delay=2.0` per regular tick × ~15 ticks; one `delay=20.0` SEA_NAVIGATE block. |
| **Game animation waits** | ~120 s | 17% | World map open (~10 s), sailing transition (~30 s), tab switch (~2 s), village info panel open (~6 s), pan animation (~1 s × multiple).  Not skippable. |
| **Retry-of-failed-attempt overhead** | ~180 s | 25% | Bot's first Move-to-Village tap missed; the second cycle was a full repeat (Qwen + perceive + pan + tap + animation).  Eliminated when tap lands correctly. |
| **Other / unattributed** | ~63 s | 8.7% | Logging, scheduling, ADB round-trip latency, mlx model first-load (1.2 s), etc. |

## What's *policy* (changeable now, no YOLO)

Ordered by impact × low risk:

### Big wins (10–60 s/cycle)

1. **Shrink the `time.sleep(8.0)` after Move-to-Village tap to a polled wait.**
   Currently the code does `tap(*btn); time.sleep(8.0); where_am_i()`.  The 8 s is a worst-case-animation guess.  Replace with:
   ```
   tap(*btn)
   for _ in range(8):
       time.sleep(1.0)
       loc = where_am_i()
       if loc["location"] in ("sea", "sea_cinematic", "loading"):
           break
   ```
   On a successful tap the game transitions in ~1.5–3 s.  Saves 4–6 s per successful voyage start; identical worst-case on failure.  Same pattern applies to the `time.sleep(3.0)` after village tap (~2 s win) and several other 2 s sleeps in `actions/sail_actions.py` after taps that the game responds to within ~500 ms.

2. **Drop the redundant Moondream daily_news confirmation when context rules it out.**
   The pixel signature for the daily-news close-X fires on a lot of sea/world_map frames.  Daily news *only* shows on port_overworld and sea, *and only once per day*.  Gate the Moondream confirm: only run it if (a) we haven't already confirmed-no for daily-news in this session AND (b) the current rule-based classify hasn't already decided we're inside a building/sub_menu/world_map.  Saves ~13 s per false alarm.  YOLO Phase 4a/4b would replace this entirely, but the gate works today.

3. **Cache "no obstruction" verdicts across consecutive perceive cycles.**
   The bot ran `_detect_interruptors` on every perceive — including back-to-back cycles where the screen signature (OmniParser elements + chrome flags) hadn't changed.  Add a 5 s TTL keyed by frame signature: if signature matches the last "no obstruction" verdict, skip the detector.  Saves ~3–5 s per skipped cycle; multiple cycles per minute = ~20–30 s/cycle.

4. **Tighten the Village-Info-button poll interval from 2.0 s to 1.0 s.**
   The poll fires ~5–7 times before the button is reliably visible.  Half the wait, half the time before the bot acts.  ~5–7 s/cycle.

### Medium wins (5–15 s/cycle)

5. **Reduce `TAP_DELAY_MIN/MAX` from 1.0/2.0 s to 0.5/1.2 s.**
   The current 1.0–2.0 s `_human_delay()` between taps is conservative.  Anti-cheat sensitivity in UWO appears to be tied to *gesture* realism (swipe-vs-tap, drift, press duration) rather than inter-tap interval.  Each tap currently adds 1.6 s avg; with 15 taps per cycle, that's 24 s.  Halving the range saves ~12 s.  Risk: anti-cheat false positive.  Suggest A/B by running a 1-week trial with the lower bound and watching for the security shutdown.

6. **Skip Qwen when the rule-based classifier's `detail` is already specific.**
   This is the existing Perf-2 from completed task #54, but the log shows it doesn't catch some cases — e.g. `detail='sea: 1/1 signals matched'` still triggers Qwen because the detail string isn't recognised as "specific enough."  Tighten the regex / specificity check to include simple matched-signal details.  Saves ~7 s × N misses per cycle (3–4 in this run, ~25 s).

7. **Parallelise OmniParser + OCR per perceive cycle.**
   They both consume the same screencap and run for ~2 s and ~0.5 s respectively.  Sequential = 2.5 s; parallel = 2 s.  Saves 0.5 s × ~10 cycles = ~5 s.  Low complexity in Python (`concurrent.futures`).

### Small wins (<5 s/cycle, mostly hygiene)

8. **Skip the chrome-glossary file re-read on every perceive.**
   The 1663-char layout is loaded from disk every Qwen call.  Cache once at process start.  Saves ~50 ms × 11 calls = 0.5 s.

9. **Skip `_classify_nav_state` on consecutive identical OmniParser signatures.**
   Same idea as #3 but for the family classifier.  ~1 s per skipped cycle.

### Don't change (high anti-cheat risk)

- The 80–220 ms press duration in `tap()` — the swipe-based realistic timing is the actual anti-cheat surface; cutting this changes the touch signature, not the inter-tap rate.
- Pan animation settle time (`_PAN_SETTLE_S=1.5`) — the world map needs that long to commit the pan before the next capture is meaningful.

## What's *compute* (needs YOLO or architecture change)

| Bucket | Time | Path to eliminate |
|---|---:|---|
| Qwen for known states | ~60 s | YOLO Phase 4a family classifier returns state + visible elements in 50 ms; Qwen only on unknown/novel states. |
| OmniParser per cycle | ~25 s | Could be replaced by YOLO for the specific elements the bot acts on (buttons, panels).  Keeping OmniParser for unknown UI exploration. |
| Moondream daily-news confirm | ~13 s | YOLO Phase 4b `daily_news_close_x` class direct detection. |
| Claude obstruction consult false-positives | ~12 s | YOLO Phase 4b `nearby_ports_hud`, `right_panel_minimap` classes recognise the persistent HUD as not-an-obstruction. |

YOLO total addressable: ~110 s/cycle = 15% wall clock.  Stacks with
policy wins.

## What's *game* (immovable)

| Bucket | Time | Why immovable |
|---|---:|---|
| World map open animation | ~10 s | Game-side cinematic transition. |
| Voyage start animation | ~10–30 s | World map closes + sea transition + ship cinematic. |
| Sailing time itself | varies (minutes) | Day/night cycle, supply countdown, intentional gameplay. |
| Inter-tap settle time (~500 ms) | ~7 s | Game UI commits the prior action before accepting the next. |
| Panel slide-in animations | ~1–2 s each | Visual polish hard-coded in client. |

Total ~30–60 s per voyage cycle.  Floor.

## Combined potential

| Bucket | Today | After policy wins | After policy + YOLO |
|---|---:|---:|---:|
| Qwen | 73 s | 50 s (#6 specificity) | 10 s (Phase 4a) |
| OmniParser + OCR cascade | 100 s | 85 s (#7 parallel, #9 skip) | 50 s (Phase 4a partial replace) |
| Flow `time.sleep` | 95 s | 50 s (#1 polled waits, #4 tighter poll) | 50 s |
| Tap human delay | 25 s | 12 s (#5 halved) | 12 s |
| Sail_to tick delays | 40 s | 30 s (reduce to 1.5 s for fast paths) | 30 s |
| Obstruction false-positive consults | 12 s | 5 s (#3 caching) | 0 s (Phase 4b) |
| Moondream false alarms | 13 s | 5 s (#2 gating) | 0 s (Phase 4b) |
| Game animation | 120 s | 120 s | 120 s |
| Retry-of-failed-tap | 180 s | 0 s (already fixed via min_y) | 0 s |
| Other | 63 s | 50 s | 50 s |
| **Total** | **721 s** | **407 s (-44%)** | **322 s (-55%)** |

**Policy alone is a 44% reduction.**  YOLO adds another 11 points.
The retry-of-failed-tap line (180 s, 25%) was the single biggest cost
in this specific run — and we already eliminated it earlier today via
the `min_y=850` + 25 px row-tolerance fixes.  Most cycles going
forward won't pay that 180 s.

## Recommended ordering

1. **Policy win #1** (polled wait instead of `time.sleep(8.0)`) — biggest ROI, no risk, one-day change.
2. **Policy win #3** (cache no-obstruction verdicts) — significant compute saving, low risk.
3. **Policy win #2** (gate Moondream daily-news) — straightforward gating logic.
4. **Policy win #5** (reduce `TAP_DELAY_MIN/MAX`) — requires the 1-week observation period for anti-cheat.
5. **YOLO Phase 4a** — wide-ranging eliminations.
6. **Remaining policy wins** in any order.

The first three are well under a day each and would shave ~80 s
(~11%) off every cycle.  Worth doing before or in parallel with the
YOLO labeling work.
