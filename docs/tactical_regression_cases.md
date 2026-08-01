# Tactical layer regression cases

Curated list of specific ticks from recent live voyages where the
tactical / heading / OCR pipeline misbehaved.  Each entry is a
targeted test case: given the session frames + trace context, the
tactical output at these ticks should match the "expected" column
after any change.

**Convention**: `session_dir` and `ticks` uniquely identify a case.
The replay framework loads the session frames, reconstructs pipeline
state, and re-runs the tactical layer through those ticks.

---

## OCR / HUD gate cases

| # | session | ticks | issue | expected after fix |
|---|---|---|---|---|
| O1 | `ai_nav_2026-07-22T19-08-01` | 38, 39, 41 | Pirate sprite occludes HUD lat/lon → garbled reads (30.00, 39.00) etc. accepted, corrupts tactical dest | OCR gate defers via `accept_or_defer`; state.lat/lon held from prior; tactical dest not corrupted |
| O2 | `ai_nav_2026-07-22T20-33-50` | 108–115 | Nubia village text overlay drops leading digit ("83,32.19" instead of "22.83"). Phantom (18.33,31.80) accepted via 2-tick same-wild confirm | Strict Path A format regex rejects sprite-drop garbles; falls through to Path C prepend-digit recovery |
| O3 | `ai_nav_2026-07-23T12-32-21` | 117–129 | Two phantom cascades in 10 ticks (accept-or-defer fooled by consecutive similar-value garbles) | Cross-check OCR candidate against `shift_motion` prediction — reject if disagreement > threshold |

## Heading pipeline cases

| # | session | ticks | issue | expected |
|---|---|---|---|---|
| H1 | `ai_nav_2026-07-23T14-30-18` | 15 | PCA tiebreak flips CNN 180° during bounce (speed=0); ship_only_cnn+pca_tiebreak → wrong heading 280° | `bounce_prefer_raw` un-flip fires when raw CNN is 1.2× closer to prior than the flipped value |
| H2 | `ai_nav_2026-07-23T14-30-18` | 58 | Fast ship regains speed in 1 tick after bounce; `physics_reject:hold_prior` fired when CNN was correct | Graded bounce-recovery inflation via `top_speed_est` ratio; plausible_delta scales up when curr/top < 0.75 |
| H3 | `ai_nav_2026-07-23T16-50-40` | 13 | Post-bounce spin: ship rotated N via bounce; heading corrupted; walker used heading as init | Walker uses `shift_motion_bearing_deg` (or `motion_bearing_deg`) — smooth signals unaffected by 1-tick bounce spin |

## SEARCH tracker / fork cases

| # | session | ticks | issue | expected |
|---|---|---|---|---|
| S1 | `ai_nav_2026-07-22T20-33-50` | 358 | Fork emerges — new frame-edge run appears 89 px from projected prev; tracker's radius filters it, hug-side never applies → wrong branch drifts in | Edge-continuity tracker with CC check picks nearest same-CC midpoint; fork emergence trigger removed (redundant) |
| S2 | `ai_nav_2026-07-23T17-01-57` | 507, 518, 590 | Fork emergence fires walker; walker uses goal=180° hug-port → east branch on return trip (opposite side of frame) | Fork emergence trigger removed; edge-continuity tracker handles same-edge continuation naturally |
| S3 | `ai_nav_2026-07-23T17-01-57` | 444 | Bottom-right corner: water continuous across bottom+right edges; tracker must accept cross-edge move | Same-CC contiguity check allows adjacent-edge candidate when in same water body |
| S4 | `ai_nav_2026-07-23T17-47-26` | 374, 375 | Land sliver appears at corner; top+left water splits into two CCs; tracker must NOT pick disconnected pool | CC check on all candidates against SHIP's CC; if none match, reclassify as turning_point |

## Turning-point / walker cases

| # | session | ticks | issue | expected |
|---|---|---|---|---|
| W1 | `ai_nav_2026-07-22T20-33-50` | 362, 367, 375, 397 | Walker returns `no_skeleton` in tiny Y-tip water pocket (only 1 anchor, no leaves via DEAD_END_DT_MAX filter) | Single-anchor fallback picks the sole frame-edge exit as `frame_edge` dest |
| W2 | `ai_nav_2026-07-23T16-10-15` | 367 | Walker inherits stale SW commit direction on return leg → picks yet another SW target | Walker `init_bearing` = motion (not commit); picks direction ship is actually going |
| W3 | `ai_nav_2026-07-23T18-51-40` | 589–596 | Reflex-closer rule fires "reached" at tact=100 / reflex=109 px (both huge); walker picks bottom-edge on return | Gate: only fires when `tact_dist < 2 × approach_px` |
| W4 | `ai_nav_2026-07-23T19-01-57` | 845 | Same as W3 — reflex-closer at 100/109 → walker picks bottom-edge dest despite ship heading N | Same fix as W3 |
| W5 | `ai_nav_2026-07-23T19-31-02` | 379 | At Y-tip walker picks top-right (starboard) instead of bottom-left (port); motion=WNW, port should be SW | Direct hug-side picker on frame-edge candidates: bearing closest to `motion ± 90°` |
| W6 | `ai_nav_2026-07-24T07-56-39` | 328–332 | Walker picked "right" branch entering Y-tip (was hug=port on return leg). User-debatable if correct | TBD — depends on whether user confirms right pick is desired at this specific fork |

## Steering / rudder cases

| # | session | ticks | issue | expected |
|---|---|---|---|---|
| R1 | `ai_nav_2026-07-23T14-30-18` | multiple | Fast ship (27 kt): linear `_speed_scale=3.375×` → 1266 ms hold for 45° target → over-rotates to ~150° | `_speed_scale = sqrt(speed/8)` empirical fit; 45° target = 690 ms → ~45° rotation |
| R2 | `ai_nav_2026-07-23T14-30-18` | multiple | Fast ship attempts 137° corrections in single command; ship's forward arc slams shore during hold | `MAX_PER_COMMAND_DEG = 45` cap; multi-tick corrections; reflex re-picks after each partial rotation |

---

## Test replay procedure (proposed)

For each row, the replay tool should:
1. Load `data/sessions/<session>/tick_XXXX.png` for the tick range (± context)
2. Reconstruct pipeline state up to the first test tick using saved trace
3. Run `AiNavPipeline.tick()` through the test range using current code
4. Assert the "expected" property holds:
   - **OCR cases**: check `state.latlon_source` and `state.lat/lon` don't accept the garbled value
   - **Heading cases**: check `state.heading.bearing_deg` closer to expected than to the failure mode value
   - **SEARCH tracker**: check `state.tactical_dest_px_offset` stays on expected edge / in expected water CC
   - **Walker**: check `state.tactical_dest_latlon` bearing from ship matches expected direction
   - **Steering**: check `state.planner_output.hold_ms` and post-tick heading Δ

## Session index

Sessions referenced above:
- `ai_nav_2026-07-22T19-08-01` — Nile descent, pirate OCR cascade
- `ai_nav_2026-07-22T20-33-50` — Y-tip stuck (v1.5.0-1 baseline)
- `ai_nav_2026-07-22T22-35-04` — first smooth round-trip (v1.5.0)
- `ai_nav_2026-07-23T12-32-21` — fast ship attempt, phantom cascades
- `ai_nav_2026-07-23T14-30-18` — fast ship steering diagnosis
- `ai_nav_2026-07-23T16-10-15` — slow ship with fast-ship tuning
- `ai_nav_2026-07-23T16-50-40` — slow ship, heading fixes
- `ai_nav_2026-07-23T17-01-57` — fork emergence bug
- `ai_nav_2026-07-23T17-47-26` — Y-tip return, land sliver split
- `ai_nav_2026-07-23T18-51-40` — reflex-closer spurious arrival
- `ai_nav_2026-07-23T19-01-57` — same, other reproduction
- `ai_nav_2026-07-23T19-31-02` — post reflex-closer gate
- `ai_nav_2026-07-24T07-56-39` — post shift-motion-bearing walker init
