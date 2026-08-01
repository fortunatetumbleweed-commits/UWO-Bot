# Sea Capture and Steering — Findings and Plan

Captures what we learned from the 2026-05-24 Calais south capture runs
and points to where related code / memory lives.

## Capture loop today
`tools/sail_capture.py` is the unattended capture entry point.

Single bash invocation: matches `Bash(python:*)` in the allowlist so
the run is uninterrupted by permission prompts.

Behaviour:
1. Verifies sea state via `perceive()`.
2. Ensures the ship is actually sailing via `sail_start()` if
   `is_ship_moving()` returns False.  This is required because
   `_depart_from_harbour()` leaves the ship anchored on the sea view —
   game days tick but the ship doesn't move otherwise.  See
   `memory/project_depart_from_harbour_anchored.md`.
3. Loops at jittered 4–7s intervals: capture → save PNG → write
   in-progress `metadata.json` (so the supervisor labeler can see frames
   mid-run) → optional steering decision → sleep.
4. After `--enter-after` seconds (default 420 = 7 min), looks for an
   "Enter New City" / "Enter New Village" label via OmniParser; first
   hit → tap once, capture one post-tap frame, exit.
5. Hard stops at `--duration` (default 600 = 10 min) regardless.

## Steering policies available
| `--steer-policy` | Signal | Verdict |
|---|---|---|
| `none` | — | Pure capture; ship sails on whatever heading it had at departure |
| `hug-coast-left` | HSV land-coloured pixels in the upper-left crop | Works, but biased by sunset (false-positive land) and one tap is too gentle to redirect at 27 kn |
| `coast-trend` | Nearest-port distance from the `right_panel:ports` list, change tick-over-tick | Useful in principle; row-pairing inside `vision/destinations_panel.py` is noisy enough to misfire most ticks |
| `latlon-south` | Position read from the mini-map lat / lon line; computes heading from Δposition between successive samples; controls error to south (180°) via signed turns; self-calibrates °/tap from observed change after each action | The best signal we have today.  Limited by ~5s loop cadence vs ship speed |

## Position truth: lat / lon on the mini-map
Every sea frame has the ship's live lat / lon as text immediately below
the mini-map (`LL.LL,±LL.LL`).  Reader code lives in
`tools/sail_capture.py::_read_latlon`:

- **Primary**: OmniParser element list, accept `text` and `button`
  types, regex-match the format anywhere in a loose right-side crop.
- **Fallback**: dedicated EasyOCR pass on a tight crop, with a lenient
  parser that handles the **ship action-radius halo** painting a stray
  leading digit on the lat value (e.g. real `"50.93,1.20"` reads as
  `"750,93,1.20"`).

See `memory/project_sea_hud_latlon.md`.

## Heading control
Helpers in `tools/sail_capture.py`:

- `_heading_deg(p0, p1) → 0..360` — compass bearing from successive
  positions, with a minimum-movement threshold to suppress noise.
- `_heading_error(current, target) → -180..180` — signed shortest turn.
- The controller picks taps = `round(|error| / deg_per_tap)`, capped
  1..6; direction by sign.
- A 10° dead-zone prevents oscillation when on-course.
- **Self-calibration**: after each tap action, observed heading change
  updates the °/tap estimate via EMA (initial guess 5°/tap).  In a real
  run the controller learned ~28°/tap from 6 left-taps (EMA absorbed
  this into ~11.9°/tap).

## What works and what doesn't (2026-05-24 runs)
**Works**
- Speed read every tick (top-right vertical column, 2nd entry).
- Sail-start auto-bring-up after `_depart_from_harbour`.
- Lat/lon read (after OmniParser+OCR fallback chain).
- Heading math and self-calibration.
- "Enter New City" auto-tap after 7-min mark.
- Tier-gated region notice doesn't break anything — captured as data.

**Doesn't, with current architecture**
- The whole loop is too slow.  Each tick is ~5–7s wall-clock
  (capture + OmniParser + decide).  At 27 kn the ship covers ~50 m of
  real distance per second but the game-time multiplier is much higher
  — distances to ports change by hundreds of km between consecutive
  ticks.  By the time a steering decision lands, the geometry has moved
  on.  See the v6 / v7 run logs.

## What real-time would require
Discussed informally; this is a future-work plan, not implemented.

1. **Frame source** — replace `adb screencap` with a tap into the
   already-running **scrcpy** video stream (H.264 over USB, 30–60 fps).
   Removes the round-trip latency per frame.
2. **Localisation model** — a small **YOLO** trained on sea-HUD
   features (ship, rudder/anchor toggle, lat-lon ROI, beam, reef,
   floating ice).  ~10–30 ms vs OmniParser's ~2 s.
3. **Selective OCR** — OCR only fires when YOLO reports the lat-lon ROI
   changed (delta-based work).
4. **Async pipeline** — capture thread feeds a ring buffer; perception
   thread processes; control thread acts.  Decisions can fire within
   ~100–300 ms of the underlying frame.

YOLO alone is single-frame, not video.  True video models exist
(3D-CNNs, video transformers) but most of the temporal info we need
(heading, drift) is already implicit in lat / lon deltas.  YOLO + a
tight loop is the right shape.

## Related memory
- `memory/project_sea_hud_latlon.md` — lat/lon location + action-radius
  halo OCR noise.
- `memory/project_depart_from_harbour_anchored.md` — must call
  `sail_start` after `_depart_from_harbour`.
- `memory/project_region_restriction_notice.md` — region walls halt the
  ship; the notice is non-blocking for input but motion stops.
- `memory/project_port_discovery_beam_vs_aura.md` — beam ≠ aura; aura
  triggers "Enter New City" label.
- `memory/feedback_sea_steering_rudder_deflection.md` — rudder cadence
  model; stop tapping → ship straightens.
- `memory/feedback_sea_visuals_highly_variable.md` — sea visuals are
  too variable for HSV tuning.
