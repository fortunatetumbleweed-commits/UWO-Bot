"""AiNavPipeline — orchestrates L1→L5 once per tick.

The pipeline owns *only* layer sequencing and timing.  All
algorithm choices live in the layer impls.  Swap an impl, the
pipeline doesn't notice.

A single tick is:
    frame  = source.capture()
    state  = L1_heading.estimate (frame, state.heading_prior)
    state  = L2_segmenter.segment(frame, state)
    state  = L3_planner.plan     (frame, state)
    state  = L4_tactical.maybe_consult(frame, state)
    state  = L5_strategic.maybe_replan(frame, state)
    return state.planner_output.command, state.planner_output.hold_ms

L4 and L5 own their own cadence gates — the pipeline calls them
every tick, and they decide whether to actually fire.

Heading prior + Kalman smoothing live in the pipeline (not in
the heading layer) so the same smoother can wrap any L1 impl.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Optional

from brain.ai_nav.layers.heading import HeadingLayer, PCAHeading
from brain.ai_nav.layers.planner import PlannerLayer, ShoreHugPlanner
from brain.ai_nav.layers.segmentation import SegmentationLayer, V11Segmenter
from brain.ai_nav.layers.strategic import NoOpStrategic, StrategicLayer
from brain.ai_nav.layers.tactical import NoOpTactical, TacticalLayer
from brain.ai_nav.state import Heading, NavState
from brain.ai_nav.vision_input import VisionFrame, VisionSource


@dataclass
class PipelineConfig:
    """Layer impl bindings.  Defaults reproduce today's pipeline
    (PCA + V11 + shore-walk + no-op tactical + no-op strategic).
    Override any field to swap a layer."""
    heading: HeadingLayer = field(default_factory=PCAHeading)
    segmentation: SegmentationLayer = field(default_factory=V11Segmenter)
    planner: PlannerLayer = field(default_factory=ShoreHugPlanner)
    mission: "Mission" = None       # late-bound — see __post_init__
    tactical: TacticalLayer = field(default_factory=NoOpTactical)
    strategic: StrategicLayer = field(default_factory=NoOpStrategic)

    # Heading smoothing.  Set to None to disable.
    heading_smoother: Optional["HeadingKalman"] = None

    def __post_init__(self):
        if self.mission is None:
            from brain.ai_nav.mission import NoOpMission
            self.mission = NoOpMission()


class HeadingKalman:
    """1-D Kalman over heading + angular velocity.

    Treats the L1 heading as a noisy measurement with covariance
    `1 / max(conf, 1e-3)`.  When confidence collapses (occlusion),
    the prior dominates and the output stays smooth.  When
    confidence is high, the measurement pulls the state.

    This is a minimal placeholder — replace with a proper EKF when
    we wire in the commanded-turn-rate motion model.  See
    `docs/ai_navigation_landscape.md` §2.3 for the rationale.
    """

    def __init__(self, process_var: float = 25.0):
        self.process_var = process_var
        self._mu: Optional[float] = None       # heading mean (deg)
        self._sigma2: float = 360.0**2          # uniform prior

    def update(self, meas: Heading) -> Heading:
        # Convert to (cos, sin) to dodge the 0/360 wraparound, then
        # collapse back to degrees.  Cheap, correct, no EKF needed
        # at this fidelity.
        if self._mu is None:
            self._mu = meas.bearing_deg
            self._sigma2 = max(1.0, 1.0 / max(meas.confidence, 1e-3) * 100)
            return Heading(self._mu, meas.confidence,
                           source=f"{meas.source}+smoothed",
                           raw_bearing_deg=meas.raw_bearing_deg)

        # Predict — random walk on heading.
        self._sigma2 += self.process_var

        # Measurement update in sin/cos space, then atan2 back.
        meas_var = 1.0 / max(meas.confidence, 1e-3) * 100
        prior_mu = self._mu
        diff = ((meas.bearing_deg - prior_mu + 540.0) % 360.0) - 180.0
        gain = self._sigma2 / (self._sigma2 + meas_var)
        new_mu = (prior_mu + gain * diff) % 360.0
        new_sigma2 = (1 - gain) * self._sigma2

        self._mu = new_mu
        self._sigma2 = new_sigma2
        eff_conf = 1.0 / (1.0 + new_sigma2 / 100)
        return Heading(new_mu, eff_conf, source=f"{meas.source}+smoothed",
                       raw_bearing_deg=meas.raw_bearing_deg)


class AiNavPipeline:
    """Run one tick of the 5-layer stack.

    Usage:
        cfg = PipelineConfig()        # all defaults
        pipe = AiNavPipeline(source=AdbVisionSource(), config=cfg)
        state = NavState()
        for tick in range(N):
            state = pipe.tick(state)
            # state.planner_output.command / hold_ms is what to fire
    """

    # Lat/lon rolling buffer length for motion-bearing computation in
    # the physics-rejection rule.  5 ticks ≈ 10 sec at typical cadence;
    # matches MOTION_WINDOW in tools/build_rl_dataset.py so offline and
    # online perception treat motion the same way.
    _MOTION_BUFFER_LEN = 6
    _MOTION_WINDOW     = 5
    # Bounds on a "trustworthy" motion magnitude per window (degrees of
    # lat+lon).  Below: ship hovering — no bearing signal.  Above: lat/
    # lon OCR garbage — bearing is nonsense.  Same constants as in the
    # offline cross-check.
    _MOTION_MIN_FOR_BEARING = 0.02
    _MOTION_MAX_FOR_BEARING = 1.0
    # Continuous motion cross-check: when heading and motion disagree by
    # this much for this many consecutive ticks, the heading detector
    # has locked onto the wrong antipodal candidate and the inline
    # antipode_physics rule is reinforcing the lock every tick (each
    # local Δ is small, so neither it nor validate_heading_physics
    # re-fires).  Force a 180° flip to break out.  Caught the failure
    # in `data/sessions/ai_nav_2026-06-29T15-28-16` where heading read
    # ~175° (south) but motion went lat 30.44 → 32.79 (north).
    _MOTION_LOCK_DIFF_DEG  = 90.0
    _MOTION_LOCK_THRESHOLD = 5

    def __init__(self, source: VisionSource, config: Optional[PipelineConfig] = None):
        self.source = source
        self.config = config or PipelineConfig()
        # The most recent frame the pipeline captured.  Runners read
        # this after `tick()` returns so they can save / annotate the
        # exact frame perception saw (re-capturing would race the game).
        self.last_frame: Optional[VisionFrame] = None
        # Rolling (lat, lon) buffer for motion-bearing cross-check on
        # the heading validator.  See _MOTION_BUFFER_LEN.
        from collections import deque
        self._latlon_history: deque = deque(maxlen=self._MOTION_BUFFER_LEN)
        # Consecutive-tick counter for the lock-break rule.  Increments
        # when heading and motion bearing disagree by ≥ _MOTION_LOCK_DIFF_DEG
        # AND motion magnitude is in the trust range; resets on agreement
        # or no motion signal.
        self._motion_lock_streak: int = 0
        # HUD OCR plausibility gate — defers implausibly-jumped candidates
        # (pirate/NPC sprite occlusion of lat/lon digits) until they
        # confirm across ticks.  See `vision.sea_hud.accept_or_defer`.
        from vision.sea_hud import _NO_PENDING
        self._ll_pending = _NO_PENDING
        self._ll_accepted_history: list = []      # [(lat, lon, tick), ...]
        # Frame-shift accumulated (px) since the last ACCEPTED lat/lon read.
        # Stale-prior escape: once the gates start rejecting, the held prior
        # goes stale and every true read looks like an implausible jump — so
        # the position freezes for many ticks.  Comparing a read's
        # displacement-from-prior against this accumulated path length (how
        # far the ship actually travelled while held) lets a genuinely-moving
        # ship re-anchor instead of staying locked.
        self._ll_shift_accum: float = 0.0
        # Hard floor: never reject more than this many ticks in a row.  The
        # accumulated shift compounds frame-shift noise the longer it grows,
        # so a long reject streak could wave through a wrong read — force a
        # re-anchor to the raw read after N consecutive rejections instead.
        self._ll_reject_streak: int = 0
        self._LL_MAX_REJECT = 3
        # Previous tick's water mask, used by frame_shift measurement.
        # See docs/pixel_continuity_design.md §3.  Phase 1: measurement-
        # only, populates state.frame_shift_px for offline validation;
        # no consumers yet.
        self._prev_water_mask = None
        # Ring of recent water masks for the multi-tick cascade
        # fallback.  When the per-tick shift fails the sanity gate,
        # we correlate against progressively older masks (N vs N-K)
        # to average through weather-transition ticks.  Entries:
        # (tick, water_mask).  Depth 8 → cascade up to K=8.
        self._mask_ring: deque = deque(maxlen=8)
        # Rolling buffer of recent accepted frame shifts as
        # `(dy, dx, conf)` tuples (per-tick equivalent — cascade
        # results are divided by K before insertion).  Feeds two
        # derived signals on state:
        #   - `expected_shift_px`  — magnitude EMA (adaptive thresholds)
        #   - `shift_motion_bearing_deg` — weighted-avg direction
        self._shift_history: deque = deque(maxlen=8)
        # Consecutive-tick counter of frame-shift rejections; resets
        # on acceptance.  Used to trigger the multi-tick cascade.
        self._shift_staleness: int = 0
        # Rolling max of recent speeds → state.top_speed_est.  Used
        # by the heading physics filter for bounce-recovery grading:
        # when curr_speed / top_speed_est is low, the ship is likely
        # in/just-after a bounce and CNN big-Δheading is more likely
        # valid than reject-worthy.
        self._speed_history: deque = deque(maxlen=30)

    def _motion_bearing_deg(self) -> Optional[float]:
        """Bearing inferred from lat/lon Δ over _MOTION_WINDOW ticks.
        Returns None when motion magnitude is outside the trust range."""
        import math
        if len(self._latlon_history) <= self._MOTION_WINDOW:
            return None
        cur = self._latlon_history[-1]
        ref = self._latlon_history[-1 - self._MOTION_WINDOW]
        if cur is None or ref is None:
            return None
        dlat = cur[0] - ref[0]
        dlon = cur[1] - ref[1]
        mag = math.hypot(dlat, dlon)
        if mag < self._MOTION_MIN_FOR_BEARING or mag > self._MOTION_MAX_FOR_BEARING:
            return None
        return math.degrees(math.atan2(dlon, dlat)) % 360.0

    def tick(self, prior_state: NavState) -> NavState:
        tick_no = prior_state.tick + 1
        frame = self.source.capture(tick_no)
        self.last_frame = frame
        state = prior_state
        state.tick = tick_no

        # Snapshot context BEFORE we overwrite per-tick fields — the
        # physics-rejection rule needs the *previous* tick's commanded
        # turn and speed (the cause of any plausible heading delta this
        # tick).  After this block, state.heading/speed/planner_output
        # all refer to the prior tick still.
        prior_heading_for_check = state.heading
        prior_speed_for_check   = state.speed_kt
        prior_planner_output    = state.planner_output
        prev_commanded_deg = 0.0
        if (prior_planner_output is not None
                and prior_planner_output.command
                and prior_planner_output.hold_ms):
            # 120°/sec is the calibrated rudder rate; positive = right.
            mag = prior_planner_output.hold_ms * 120.0 / 1000.0
            prev_commanded_deg = (mag if prior_planner_output.command == "hold_right"
                                  else -mag)

        # L1 — heading (raw template-match + antipode tiebreaker)
        t0 = time.perf_counter()
        raw_heading = self.config.heading.estimate(
            frame, prior=state.heading,
        )
        if self.config.heading_smoother is not None:
            state.heading = self.config.heading_smoother.update(raw_heading)
        else:
            state.heading = raw_heading

        # Physics-consistency post-filter.  Big heading deltas with
        # neither a commanded turn nor a speed drop are OCR artifacts;
        # substitute the antipode or motion bearing when one is much
        # closer to the prior heading.
        #
        # We read curr_speed HERE (before the main speed-read block
        # later) so physics_reject's bounce-trust rule fires when
        # the ship is currently at low speed (bouncing this tick).
        # Without this, curr_speed would carry the prior tick's value
        # and the bounce-trust rule fires 1 tick late.
        from brain.ai_nav.layers.heading import validate_heading_physics
        mot_brg = self._motion_bearing_deg()
        curr_speed_for_check = prior_speed_for_check   # fallback
        try:
            from vision.sea_hud import read_speed as _read_speed
            _sp = _read_speed(frame.full_screen())
            if _sp is not None:
                curr_speed_for_check = _sp
                state.speed_kt = _sp   # cache so later read block short-circuits
        except Exception:
            pass
        # Update rolling max speed BEFORE the physics filter so
        # top_speed_est reflects this tick's reading too.
        if curr_speed_for_check is not None:
            self._speed_history.append(curr_speed_for_check)
        if self._speed_history:
            state.top_speed_est = float(max(self._speed_history))
        state.heading = validate_heading_physics(
            new_heading=state.heading,
            prior_heading=prior_heading_for_check,
            prev_commanded_deg=prev_commanded_deg,
            prev_speed=prior_speed_for_check,
            curr_speed=curr_speed_for_check,
            motion_bearing_deg=mot_brg,
            top_speed_est=state.top_speed_est,
        )

        # Heading noise-rejection (gated).  validate_heading_physics fires
        # on single-tick jumps > 40° with prev_cmd < 15°.  This rule fills
        # the gap: smaller jumps (25-40°) that still can't be explained by
        # commanded rotation.  Cases observed in
        # ai_nav_2026-06-29T21-16-16 around Nubia (t90-t98): observed Δhdg
        # 30°/70°/55° with prev_cmd=0, all noisy single-frame OCR misses.
        # Confidence reads were 1.00 for many of these (matcher confidently
        # wrong), so heading_conf alone isn't a usable noise signal.
        #
        # Gating: only fire when the bot did NOT command a big turn last
        # tick.  Otherwise a legitimate commanded rotation gets rejected
        # (the bug that caused the circles-out-of-port regression).
        #
        # Fallback ladder when fired:
        #   1. motion bearing (if available + plausible) — independent
        #      perception channel
        #   2. prior heading + small commanded delta — predicted heading
        NOISE_REJECT_RESIDUAL_DEG = 25.0
        NOISE_REJECT_CMD_EXEMPT   = 15.0
        if (prior_heading_for_check is not None
                and state.heading is not None
                and "physics_reject" not in state.heading.source
                and "noise_reject" not in state.heading.source
                and abs(prev_commanded_deg) < NOISE_REJECT_CMD_EXEMPT):
            obs_delta = ((state.heading.bearing_deg
                          - prior_heading_for_check.bearing_deg
                          + 540.0) % 360.0) - 180.0
            residual = abs(obs_delta - prev_commanded_deg)
            if residual > NOISE_REJECT_RESIDUAL_DEG:
                from brain.ai_nav.layers.heading import Heading as _Heading
                # Prefer motion bearing when available — independent channel.
                if mot_brg is not None:
                    substitute = mot_brg
                    fallback = "motion"
                else:
                    # Fall back to prior + expected (small) rotation.
                    substitute = ((prior_heading_for_check.bearing_deg
                                   + prev_commanded_deg) % 360.0)
                    fallback = "prior"
                import logging
                logging.getLogger(__name__).warning(
                    "[ai_nav] heading noise_reject: observed Δ=%.0f° but "
                    "commanded %.0f° (residual=%.0f°) → %s=%.0f°",
                    obs_delta, prev_commanded_deg, residual,
                    fallback, substitute,
                )
                state.heading = _Heading(
                    bearing_deg=substitute,
                    confidence=state.heading.confidence * 0.6,
                    source=f"{state.heading.source}+noise_reject:{fallback}",
                    raw_bearing_deg=state.heading.raw_bearing_deg,
                    shadow_bearing_deg=state.heading.shadow_bearing_deg,
                    shadow_confidence=state.heading.shadow_confidence,
                )
        state.heading_history.append(state.heading)
        t1 = time.perf_counter()

        # L2 — segmentation
        state.water_mask = self.config.segmentation.segment(frame)
        t2 = time.perf_counter()

        # Frame-shift measurement.  See docs/pixel_continuity_design.md §3.
        #
        # Gate rewritten 2026-07-25 after ai_nav_2026-07-25T13-09-55 t210-t213
        # + t371-t374 dual bug diagnosis:
        #
        #   1. The old avg-relative gate (mag ≤ 3 × avg_mag) had a
        #      LOCK-IN failure mode.  When avg_mag briefly dipped low
        #      (slow section), the 3× threshold pinned the gate at
        #      a small value forever — every subsequent legitimate
        #      shift was rejected and never fed back into the average.
        #      Session t204-t213 froze shift_motion_bearing_deg at
        #      111° for 15+ ticks while the ship visibly moved.
        #
        #   2. Weather / hue transitions (e.g. session t372, mean
        #      luminance +7 then -10, contrast std -30%) legitimately
        #      degrade phase-correlate confidence for one or two ticks
        #      even when ship motion is normal.  A conf-only gate
        #      throws away good samples in those transitions.
        #
        # New gate uses SPEED-CONSISTENCY as the primary signal, since
        # ship speed (HUD OCR) is an independent channel and directly
        # predicts expected pixel displacement per tick.  Confidence
        # is a secondary check.
        #
        # When per-tick shift fails the gate AND staleness ≥ 2, a
        # multi-tick cascade runs (N vs N-2, N-4, N-8) — bigger
        # baseline = better phase-correlate SNR through low-signal
        # or hue-transition ticks.  Cascade result is divided by K
        # to get per-tick equivalent for the shift history.
        import math as _m
        from vision.frame_shift import measure_frame_shift
        # Calibrated 2026-07-25 from 184 high-conf ticks on
        # ai_nav_2026-07-25T13-09-55 (Nile session): median 1.34,
        # p10-p90 1.01-1.71.  Ratio of shift-magnitude to ship-
        # speed-in-knots on this UI/zoom.
        _K_PX_PER_KT = 1.35
        _MIN_SPEED_FOR_MOTION = 1.0     # kt; below this, no motion signal
        _MAG_FLOOR = 0.3                # min ratio of expected mag
        _MAG_CEIL = 3.0                 # max ratio of expected mag
        _CONF_FLOOR = 0.20              # loosened from 0.30 (speed check backs it up)
        _STALE_THRESHOLD = 2            # cascade triggers at this many consecutive rejects

        def _shift_passes(dy, dx, conf, expected_mag_ref: float) -> bool:
            """Speed-consistency + confidence gate.  expected_mag_ref
            is expected per-tick magnitude at current speed (for K=1)
            or expected K-tick magnitude (for cascade)."""
            if conf < _CONF_FLOOR:
                return False
            mag = _m.hypot(dy, dx)
            if expected_mag_ref <= 0.0:
                return False   # no motion expected → treat all shifts as noise
            ratio = mag / expected_mag_ref
            return _MAG_FLOOR <= ratio <= _MAG_CEIL

        curr_speed = state.speed_kt or 0.0
        expected_per_tick = curr_speed * _K_PX_PER_KT
        accepted_this_tick = False

        # Push current mask into the ring buffer (before any correlation
        # runs, so cascade K=2 can pick up the previous tick's mask).
        if state.water_mask is not None:
            self._mask_ring.append((tick_no, state.water_mask))

        if (self._prev_water_mask is not None
                and state.water_mask is not None
                and self._prev_water_mask.shape == state.water_mask.shape):
            dy, dx, conf = measure_frame_shift(
                self._prev_water_mask, state.water_mask,
            )
            state.frame_shift_px = (dy, dx, conf)
            if curr_speed < _MIN_SPEED_FOR_MOTION:
                # No motion → skip.  Don't touch staleness (no signal
                # to be stale about).  Downstream consumers fall back
                # to motion_bearing_deg or hold prior values.
                pass
            elif _shift_passes(dy, dx, conf, expected_per_tick):
                self._shift_history.append((dy, dx, conf))
                self._shift_staleness = 0
                accepted_this_tick = True
            else:
                self._shift_staleness += 1

        # Cascade fallback: per-tick failed AND we've been stale for
        # a while.  Try progressively larger baselines.  A K-tick
        # correlation between the current mask and a mask K ticks
        # back produces a K-tick displacement; we divide by K to get
        # a per-tick-equivalent shift for consistent bearing history.
        if (not accepted_this_tick
                and self._shift_staleness >= _STALE_THRESHOLD
                and curr_speed >= _MIN_SPEED_FOR_MOTION
                and state.water_mask is not None
                and len(self._mask_ring) >= 3):
            # Ring holds oldest-to-newest.  The just-appended current
            # mask is at [-1].  Candidates: K = 2, 4, 8 → ring indices
            # -3, -5, -9 (bounded by ring size).
            for K in (2, 4, 8):
                idx = -(K + 1)
                if -idx > len(self._mask_ring):
                    break
                past_tick, past_mask = self._mask_ring[idx]
                if past_mask.shape != state.water_mask.shape:
                    continue
                dy_k, dx_k, conf_k = measure_frame_shift(past_mask, state.water_mask)
                expected_k = expected_per_tick * K
                if _shift_passes(dy_k, dx_k, conf_k, expected_k):
                    # Store per-tick equivalent (divide displacement
                    # by K, keep conf as-is — it's a peak height,
                    # not a magnitude).
                    dy_t = int(round(dy_k / K))
                    dx_t = int(round(dx_k / K))
                    self._shift_history.append((dy_t, dx_t, conf_k))
                    self._shift_staleness = 0
                    accepted_this_tick = True
                    import logging
                    logging.getLogger(__name__).info(
                        "[ai_nav] frame_shift cascade K=%d recovered: "
                        "raw=(%+d,%+d,%.2f) per-tick=(%+d,%+d) at speed=%.1fkt",
                        K, dy_k, dx_k, conf_k, dy_t, dx_t, curr_speed,
                    )
                    break

        self._prev_water_mask = state.water_mask
        # Derived signals from shift history:
        #   - `expected_shift_px`   — magnitude EMA (adaptive thresholds)
        #   - `shift_motion_bearing_deg` — weighted-avg compass bearing
        #     of the SHIP's motion (ship moves opposite to frame shift).
        if self._shift_history:
            mags = [math.hypot(s[0], s[1]) for s in self._shift_history]
            state.expected_shift_px = float(sum(mags) / len(mags))
            # Ship-motion vector in image coords = (-dy, -dx) per shift.
            # Weight each contribution by (confidence × magnitude) so
            # low-conf or tiny shifts don't dominate the direction.
            wy = sum(-s[0] * s[2] * math.hypot(s[0], s[1])
                     for s in self._shift_history)
            wx = sum(-s[1] * s[2] * math.hypot(s[0], s[1])
                     for s in self._shift_history)
            # Only publish a bearing if the summed motion vector is
            # non-trivial (avoids noise-dominated bearing when ship is
            # essentially stationary).
            if math.hypot(wy, wx) > 3.0:
                # Compass bearing: 0°=N (image up), 90°=E (image right).
                # image_up = -wy; image_right = wx.
                state.shift_motion_bearing_deg = (
                    math.degrees(math.atan2(wx, -wy)) % 360.0
                )

        # HUD lat/lon read (authoritative when OCR succeeds; runner's
        # dead-reckoning fills the gap when this returns None).
        # Only pass prev_latlon as a disambiguation hint when our prior
        # reading was ALSO from HUD OCR — passing a dead-reckoned prior
        # causes read_latlon's plausibility-vs-prior filter to reject
        # clean OCR reads that diverge from the (incorrect) dead-reckon,
        # per `feedback_estimator_cache_divergence.md`.
        from vision.sea_hud import (
            read_latlon, accept_or_defer, _HISTORY_FOR_VELOCITY,
        )
        prior_latlon = (
            (state.lat, state.lon)
            if (state.latlon_source == "hud_ocr"
                and state.lat is not None and state.lon is not None)
            else None
        )
        try:
            ll = read_latlon(frame.full_screen(), prev_latlon=prior_latlon)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "[ai_nav] read_latlon raised: %s", e
            )
            ll = None
        raw_ll = ll                                # pre-gate read (stale-escape)
        # Accumulate the frame-shift travelled since the last accepted read.
        if state.frame_shift_px is not None:
            self._ll_shift_accum += math.hypot(
                state.frame_shift_px[0], state.frame_shift_px[1])
        # Frame-shift cross-check — a HUD read whose implied motion far
        # exceeds the PERCEIVED frame shift (with no bounce) is a bad OCR
        # read (digit misread / occlusion), not a real jump.  frame_shift
        # is an independent motion signal (shore-edge phase correlation);
        # the position can't have moved 3× more than the pixels did with
        # the ship still at speed.  Reject → accept_or_defer holds the
        # prior and the runner dead-reckons from the shift.  (session
        # 2026-07-28T18-27-37 t474: lon read jumped 34 km while frame_shift
        # was a normal ~8 km, no bounce → flipped the reflex WP 180° and
        # bounced.)
        if (ll is not None and prior_latlon is not None
                and state.frame_shift_px is not None):
            _dy, _dx, _conf = state.frame_shift_px
            _shift_px = math.hypot(_dy, _dx)
            _ocr_px = math.hypot(ll[0] - prior_latlon[0],
                                 ll[1] - prior_latlon[1]) * 100.0  # PX_PER_DEG
            _bounced = (prior_speed_for_check is not None
                        and state.speed_kt is not None
                        and prior_speed_for_check >= 3.0
                        and state.speed_kt < 0.5 * prior_speed_for_check)
            # Compare against the shift ACCUMULATED since the last accept, not
            # just this tick's — else a read that is consistent with how far
            # the ship actually moved while the prior was held gets rejected,
            # freezing the position (t494+ in loop_uturn_2026-07-31).
            _baseline = max(_shift_px, self._ll_shift_accum)
            if (_conf > 0.3 and not _bounced
                    and _ocr_px > 2.5 * _baseline
                    and _ocr_px - _baseline > 15.0):
                import logging
                logging.getLogger(__name__).warning(
                    "[ai_nav] t=%d HUD read %s implies %.0fpx move but "
                    "frame_shift is %.0fpx (accum %.0f, conf %.2f, no bounce) "
                    "— rejecting as bad OCR read", state.tick, ll,
                    _ocr_px, _shift_px, self._ll_shift_accum, _conf)
                ll = None
        # Plausibility gate — reject impossible per-tick jumps (HUD
        # occluded by NPC / pirate sprites).  Rejection = hold prior
        # state.lat/lon this tick; the runner's dead-reckon step
        # advances the position forward for next tick.  Prevents OCR
        # garbage from corrupting the tactical dest (t38-t42 in
        # ai_nav_2026-07-22T19-08-01 cascade).
        accepted, self._ll_pending = accept_or_defer(
            candidate=ll,
            prev=prior_latlon,
            pending=self._ll_pending,
            history=self._ll_accepted_history,
            current_tick=state.tick,
        )
        # Stale-prior escape — if both gates rejected but the raw read is
        # consistent with the frame-shift ACCUMULATED while the prior was held
        # (the ship genuinely travelled that far), re-anchor to it.  Breaks
        # the freeze where a continuously-moving ship never repeats a value to
        # confirm.  Only fires under real motion (accum grows); a stationary
        # ship keeps accum ~0 so OCR garbage is still rejected.
        if (accepted is None and raw_ll is not None
                and prior_latlon is not None):
            _raw_ocr = math.hypot(raw_ll[0] - prior_latlon[0],
                                  raw_ll[1] - prior_latlon[1]) * 100.0
            if _raw_ocr <= self._ll_shift_accum * 1.3 + 15.0:
                from vision.sea_hud import _NO_PENDING
                accepted = raw_ll
                self._ll_pending = _NO_PENDING
                import logging
                logging.getLogger(__name__).info(
                    "[ai_nav] t=%d stale-prior escape: accepting %s "
                    "(%.0fpx from stale prior, accum shift %.0fpx)",
                    state.tick, raw_ll, _raw_ocr, self._ll_shift_accum)
        # Hard floor — never hold a stale prior for more than _LL_MAX_REJECT
        # ticks in a row (bounds the accumulated-shift error): force a
        # re-anchor to the raw read.
        if accepted is None and raw_ll is not None:
            self._ll_reject_streak += 1
            if self._ll_reject_streak >= self._LL_MAX_REJECT:
                from vision.sea_hud import _NO_PENDING
                accepted = raw_ll
                self._ll_pending = _NO_PENDING
                import logging
                logging.getLogger(__name__).info(
                    "[ai_nav] t=%d hard-floor re-anchor to %s after %d "
                    "consecutive rejections", state.tick, raw_ll,
                    self._ll_reject_streak)
        if accepted is not None:
            state.lat, state.lon = accepted
            state.latlon_source = "hud_ocr"
            self._ll_shift_accum = 0.0             # re-anchored → reset accum
            self._ll_reject_streak = 0
            self._ll_accepted_history.append(
                (accepted[0], accepted[1], state.tick)
            )
            if len(self._ll_accepted_history) > _HISTORY_FOR_VELOCITY:
                self._ll_accepted_history = (
                    self._ll_accepted_history[-_HISTORY_FOR_VELOCITY:]
                )
        elif ll is not None:
            import logging
            logging.getLogger(__name__).warning(
                "[ai_nav] t=%d rejecting HUD lat/lon %s vs prev %s "
                "(pending=%s) — holding prior",
                state.tick, ll, prior_latlon, self._ll_pending,
            )
        # Update the rolling lat/lon buffer used by the next tick's
        # heading physics-rejection rule.  Append the post-read pair
        # (whether OCR-fresh or held-over) so the motion bearing
        # reflects what the planner is actually working with.
        if state.lat is not None and state.lon is not None:
            self._latlon_history.append((state.lat, state.lon))
        else:
            self._latlon_history.append(None)

        # Continuous motion cross-check — catches the steady-state
        # antipode lock that the jump-detection rule above can't see.
        # Each tick is locally consistent (small Δhdg) but the heading
        # is globally 180° off; motion bearing is the independent signal.
        # Fire only after N consecutive disagreements to tolerate
        # transient mid-turn / collision noise.
        from brain.ai_nav.layers.heading import Heading as _Heading
        post_mot_brg = self._motion_bearing_deg()
        # Expose motion bearing for downstream consumers (tactical
        # walker uses it as init_bearing at fork picks).
        state.motion_bearing_deg = post_mot_brg
        if (post_mot_brg is not None and state.heading is not None):
            diff = abs(((state.heading.bearing_deg - post_mot_brg + 540.0)
                        % 360.0) - 180.0)
            if diff >= self._MOTION_LOCK_DIFF_DEG:
                self._motion_lock_streak += 1
            else:
                self._motion_lock_streak = 0
        else:
            # No motion signal — don't accumulate, don't reset.  Keeps
            # the streak alive across short stalls (sail-stop, occluded
            # HUD) so we don't lose hard-won evidence of a lock.
            pass
        if (self._motion_lock_streak >= self._MOTION_LOCK_THRESHOLD
                and state.heading is not None):
            flipped = (state.heading.bearing_deg + 180.0) % 360.0
            import logging
            logging.getLogger(__name__).warning(
                "[ai_nav] motion-lock break: heading %.1f° vs motion %.1f° "
                "for %d ticks → flipping to %.1f°",
                state.heading.bearing_deg, post_mot_brg,
                self._motion_lock_streak, flipped,
            )
            state.heading = _Heading(
                bearing_deg=flipped,
                confidence=state.heading.confidence * 0.5,
                source=f"{state.heading.source}+motion_lock_break",
                raw_bearing_deg=state.heading.raw_bearing_deg,
                shadow_bearing_deg=state.heading.shadow_bearing_deg,
                shadow_confidence=state.heading.shadow_confidence,
            )
            self._motion_lock_streak = 0

        # Speed read.  Independent of lat/lon (separate OCR crop +
        # parser).  Feeds the learned-controller reward + observation
        # vector; also useful as a collision signal in classical
        # strategics.
        try:
            from vision.sea_hud import read_speed
            sp = read_speed(frame.full_screen())
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "[ai_nav] read_speed raised: %s", e,
            )
            sp = None
        if sp is not None:
            state.speed_kt = sp

        # L3 — planner
        state = self.config.planner.plan(frame, state)
        t3 = time.perf_counter()
        commit_before = (state.commit_direction.bearing_deg
                         if state.commit_direction is not None else None)

        # Mission — strategic decision-maker.  Reads topology + lat/lon,
        # writes commit_direction.  Runs after L3 so it can see the
        # current tick's topology.
        state = self.config.mission.update(frame, state)
        tmis = time.perf_counter()

        # L4 — tactical (cadence-gated inside)
        state = self.config.tactical.maybe_consult(frame, state)
        t4 = time.perf_counter()

        # If mission or tactical flipped commit by >= 60° this tick,
        # re-run the planner so the reflex waypoint is picked with the
        # fresh commit direction instead of the stale one it used above.
        # Without this, a large commit rotation (e.g. tactical picks a
        # new frame-edge dest rotating commit 136° from WNW to SSE at t7
        # in ai_nav_2026-07-24T12-52-08) leaves the reflex wp aligned
        # to the pre-flip direction, and wp_bearing_reject latches onto
        # that stale bearing for the rest of the voyage.
        commit_after = (state.commit_direction.bearing_deg
                        if state.commit_direction is not None else None)
        if commit_before is not None and commit_after is not None:
            diff = abs(((commit_after - commit_before + 540.0) % 360.0)
                       - 180.0)
            if diff >= 60.0:
                import logging
                logging.getLogger(__name__).info(
                    "[pipeline] commit rotated %.0f° (%.0f°→%.0f°) "
                    "— re-planning with fresh commit",
                    diff, commit_before, commit_after,
                )
                state = self.config.planner.plan(frame, state)

        # L5 — strategic (cadence-gated inside)
        state = self.config.strategic.maybe_replan(frame, state)
        t5 = time.perf_counter()

        # Stash timings on the planner output for trace.jsonl.
        if state.planner_output is not None:
            po = state.planner_output
            po_meta = getattr(po, "timings_ms", None)
            # PlannerOutput is a dataclass; attach a sidecar dict
            # via object.__setattr__ so trace.snapshot can pick it up.
            setattr(po, "timings_ms", {
                "heading": (t1 - t0) * 1000,
                "segment": (t2 - t1) * 1000,
                "plan": (t3 - t2) * 1000,
                "mission": (tmis - t3) * 1000,
                "tactical": (t4 - tmis) * 1000,
                "strategic": (t5 - t4) * 1000,
            })

        return state
