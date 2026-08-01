"""Layer 1 — heading regression.

Per-tick estimate of the ship's bow direction in world coordinates
(0 = N, CW).  Pluggable: today's default is the legacy PCA reader
wrapped to emit a confidence; tomorrow's is a tiny CNN with a
sin/cos head; later, a VLM that reads the whole screen.

Every implementation MUST:
  - return a Heading with `bearing_deg ∈ [0, 360)` and
    `confidence ∈ [0, 1]`
  - tolerate occlusion (return low confidence rather than crash)
  - never block longer than its declared latency budget
"""
from __future__ import annotations

import math
from typing import Optional, Protocol

import numpy as np

from brain.ai_nav.state import Heading, NavState
from brain.ai_nav.vision_input import VisionFrame


class HeadingLayer(Protocol):
    """Protocol for L1 implementations."""
    name: str
    latency_budget_ms: float

    def estimate(self, frame: VisionFrame, prior: Optional[Heading]
                 ) -> Heading: ...


# ── Default impl: legacy PCA reader ────────────────────────────────────


class PCAHeading:
    """Wraps the legacy PCA-on-green-pixels heading from the
    centerline prototype.

    Known failure modes (covered in
    `docs/ai_navigation_landscape.md` §5.1):
      - bow/stern sign flip when overlays brighten the stern half
      - chaotic when ship sprite is partially occluded by NPC / village
      - returns *some* angle even when the input is garbage

    Emits a coarse confidence: 0.5 baseline, scaled down when fewer
    than ~30 green pixels are present (proxy for occlusion).  The
    Kalman smoother in `pipeline.py` uses this to gate jumps.
    """
    name = "pca_legacy"
    latency_budget_ms = 5.0

    def estimate(self, frame: VisionFrame, prior: Optional[Heading]
                 ) -> Heading:
        # Lazy import — legacy module is heavy on first load.
        from tools.centerline_waypoint_prototype import estimate_ship_heading
        rgb = np.asarray(frame.minimap())
        vec = estimate_ship_heading(rgb)
        if vec is None:
            # No ship pixels at all — caller's prior is more reliable
            # than any guess we could make.
            bearing = prior.bearing_deg if prior else 0.0
            return Heading(bearing, confidence=0.0, source="pca_legacy_miss")
        dy, dx = vec
        bearing = (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0
        # Crude confidence — refine later when we have CNN ground truth.
        R, G, B = (rgb[..., c].astype(np.int16) for c in range(3))
        green = (G > 140) & (G - R > 30) & (G - B > 30)
        n_green = int(green.sum())
        conf = min(1.0, n_green / 60.0) * 0.5   # cap at 0.5
        return Heading(bearing, confidence=conf, source="pca_legacy")


# ── Rotational template matcher ────────────────────────────────────────


class TemplateMatchHeading:
    """Rotational template matcher against the canonical bow-up
    ship sprite (`data/reference/ship_template_raw.png`).

    Replaces PCA + brightness-tiebreaker.  Auto-switches between
    IoU (full ship visible — asymmetric filled region breaks the
    180° tie) and forward edge chamfer (partial visibility — bow
    off-screen, text overlay, NPC sprite occlusion).

    Validated on Palma (4/4 PCA flips caught) and Nubia (partial
    visibility cases).  See tools/match_ship_template.py for
    algorithm + tools/viz_ship_match.py for diagnostics.

    Confidence: maps the match score linearly into [0, 1], capped
    at 1.0 at score=0.5 (empirical IoU/chamfer peaks in clean
    frames are 0.5–0.6).  Returns confidence 0 on no-green frames
    so the caller's prior wins.
    """
    name = "template_match"
    latency_budget_ms = 100.0

    # Antipodal tie threshold.  When the MAX score in a ±ANTIPODAL_WINDOW
    # window around (best + 180°) is within this fraction of the best
    # score, IoU/chamfer can't distinguish the 180° flip — break the
    # tie via temporal continuity with the prior heading.  Observed
    # ratios on flip-prone frames: 0.82–0.99 across Cairo voyage.
    # 0.85 catches the bulk while leaving Palma/Nubia clean cases
    # (typically 0.70–0.80) alone.
    #
    # The window catches second peaks that aren't exactly 180° away
    # from the best — text-contaminated frames often have the two
    # peaks 180°±10° apart, and looking at the exact antipode lands
    # in the dip between them.
    ANTIPODAL_TIE_RATIO = 0.85
    ANTIPODAL_WINDOW_DEG = 15.0

    # Physics-implausibility threshold.  Max ship rotation rate is
    # ~120°/sec; max command hold is 800ms → max plausible per-tick
    # bow rotation ≈ 96°.  Any best-vs-prior delta above this MUST be
    # a perception misread.  When this fires AND the antipodal peak
    # sits closer to prior, prefer the antipode regardless of how
    # confident the matcher's `best` claimed to be — physics trumps
    # IoU/chamfer score.
    MAX_PER_TICK_DELTA_DEG = 100.0

    def __init__(self, template_path: Optional[str] = None,
                 step: float = 5.0):
        from pathlib import Path as _P
        from PIL import Image as _PIL_Image
        from tools.match_ship_template import template_green_mask
        if template_path is None:
            template_path = (_P(__file__).resolve().parents[3]
                             / "data/reference/ship_template_raw.png")
        tpl = _PIL_Image.open(template_path).convert("RGBA")
        self._template_mask = template_green_mask(tpl)
        self._step = step

    def estimate(self, frame: VisionFrame, prior: Optional[Heading]
                 ) -> Heading:
        from tools.match_ship_template import match_heading
        rgb = np.asarray(frame.minimap())
        best, score, scores = match_heading(
            rgb, self._template_mask, step=self._step,
        )
        if score <= 0.05:
            bearing = prior.bearing_deg if prior else 0.0
            return Heading(bearing, confidence=0.0,
                           source="template_match_miss")

        source = "template_match"
        if prior is not None and scores:
            antipode = (best + 180.0) % 360.0
            window = [
                (k, v) for k, v in scores.items()
                if abs(((k - antipode + 540) % 360) - 180)
                   <= self.ANTIPODAL_WINDOW_DEG
            ]
            if window:
                anti_key, anti_score = max(window, key=lambda kv: kv[1])
                d_best = abs(((best - prior.bearing_deg + 540) % 360) - 180)
                d_anti = abs(((anti_key - prior.bearing_deg + 540) % 360) - 180)

                # Two reasons to break in favor of the antipode:
                #   (a) score-ratio tie: matcher genuinely can't tell
                #       which rotation is correct.
                #   (b) physics impossibility: best peak sits at a
                #       bearing the bow can't have reached in one
                #       tick — must be a misread.
                tie = anti_score >= self.ANTIPODAL_TIE_RATIO * score
                impossible = d_best > self.MAX_PER_TICK_DELTA_DEG

                if (tie or impossible) and d_anti < d_best:
                    best = anti_key
                    score = anti_score
                    source = ("template_match+antipode_physics"
                              if impossible else
                              "template_match+antipode_break")
                elif tie:
                    source = "template_match+antipode_keep"
        return Heading(
            best, confidence=min(1.0, score / 0.5),
            source=source,
        )


# ── Ship-only CNN heading (v3) ─────────────────────────────────────────


class ReconstructMatchHeading:
    """Rotational template matcher with separate axis + direction
    confidence.  Wraps `ship_template_reconstruct.ShipTemplateMatcher`.

    Key advantages over the older `TemplateMatchHeading`:
      - Reports axis (0-180°) and direction (bow vs. stern)
        confidence separately, so a fragment that only carries the
        axis signal (t556-style flip case) still returns a usable
        answer instead of being discarded.
      - Uses coverage × fill^0.3 scoring, empirically 1.9° mean
        error on the labelled hard-case set (vs 100°+ for the older
        matcher on the same ambiguous cases).
      - Correctly flags information-limited fragments (t082, t083
        with ~30 ship pixels) as low-confidence instead of guessing.

    Confidence encoding in the returned `Heading`:
      - `confidence` = axis_conf × dir_conf (overall)
      - `source` includes both sub-confidences and a mode flag:
          "reconstruct_match:axis_conf=0.82,dir_conf=0.71"
          "reconstruct_match_axis_only:axis_conf=0.9,dir_conf=0.1,flip=180"
          "reconstruct_match_miss:visible_px=12"

    Latency budget ~80ms — the coarse+refine sweep dominates.  Fine
    for a 1Hz control loop; not suited for the 10Hz combat loop.
    """
    name = "reconstruct_match"
    latency_budget_ms = 100.0

    # When only the axis is trustworthy, fall back to the prior's
    # bow/stern direction if available.  Otherwise return the raw
    # matcher call and let the pipeline's smoother handle it.
    AXIS_ONLY_DIR_THRESHOLD = 0.35

    def __init__(self, template_path: Optional[str] = None):
        from pathlib import Path as _P
        from brain.ai_nav.learned.ship_template_reconstruct import (
            ShipTemplateMatcher,
        )
        if template_path is not None:
            template_path = _P(template_path)
        self._matcher = ShipTemplateMatcher(template_path=template_path)

    def estimate(self, frame: VisionFrame, prior: Optional[Heading]
                 ) -> Heading:
        rgb = np.asarray(frame.minimap())
        # Crop / centre around the ship if the input is bigger than
        # 80×80.  ShipTemplateMatcher assumes an 80×80 crop centred
        # on the ship centroid.
        if rgb.shape[0] != 80 or rgb.shape[1] != 80:
            rgb = self._crop_around_ship(rgb)
            if rgb is None:
                bearing = prior.bearing_deg if prior else 0.0
                return Heading(bearing, confidence=0.0,
                               source="reconstruct_match_miss:no_ship")

        res = self._matcher.estimate(rgb)

        if res.visible_px < 5 or res.score <= 0.05:
            bearing = prior.bearing_deg if prior else 0.0
            return Heading(bearing, confidence=0.0,
                           source=f"reconstruct_match_miss:"
                                  f"visible_px={res.visible_px}")

        # Axis-only case: direction is uncertain but axis is solid.
        # If prior is available, snap to whichever of {angle, angle+180}
        # is closer to the prior.  If no prior, return raw and let the
        # smoother handle it.
        if (res.dir_confidence < self.AXIS_ONLY_DIR_THRESHOLD
                and res.axis_confidence >= self.AXIS_ONLY_DIR_THRESHOLD
                and prior is not None):
            flip = (res.angle_deg + 180.0) % 360.0
            d_best = abs(((res.angle_deg - prior.bearing_deg + 540) % 360)
                         - 180)
            d_flip = abs(((flip - prior.bearing_deg + 540) % 360) - 180)
            if d_flip < d_best:
                bearing = flip
                source = (f"reconstruct_match_axis_only+prior_flip:"
                          f"axis_conf={res.axis_confidence:.2f},"
                          f"dir_conf={res.dir_confidence:.2f}")
            else:
                bearing = res.angle_deg
                source = (f"reconstruct_match_axis_only+prior_keep:"
                          f"axis_conf={res.axis_confidence:.2f},"
                          f"dir_conf={res.dir_confidence:.2f}")
            return Heading(
                bearing_deg=bearing,
                confidence=res.axis_confidence,   # trust axis, not dir
                source=source,
                raw_bearing_deg=res.angle_deg,
            )

        source = (f"reconstruct_match:"
                  f"axis_conf={res.axis_confidence:.2f},"
                  f"dir_conf={res.dir_confidence:.2f}")
        return Heading(
            bearing_deg=res.angle_deg,
            confidence=res.confidence,
            source=source,
            raw_bearing_deg=res.angle_deg,
        )

    def _crop_around_ship(self, rgb: np.ndarray) -> Optional[np.ndarray]:
        """Centre-crop 80×80 around the ship's green centroid."""
        r = rgb[..., 0].astype(np.int16)
        g = rgb[..., 1].astype(np.int16)
        b = rgb[..., 2].astype(np.int16)
        mask = (g > 110) & (g - r > 60) & (g - b > 50)
        ys, xs = np.where(mask)
        if len(ys) < 5:
            return None
        H, W, _ = rgb.shape
        cy = int(round(max(40, min(H - 40, ys.mean()))))
        cx = int(round(max(40, min(W - 40, xs.mean()))))
        return rgb[cy - 40:cy + 40, cx - 40:cx + 40]


class ShipPartsHeading:
    """Region-parts U-Net heading estimator (the `ship_parts` drop-in).

    A U-Net segments the ship into distinctive parts (bow / hull / stern /
    sail_l / sail_r), then a rigid part-layout fit recovers the heading —
    the direction is read from whichever parts survive the occlusion, with
    an open-water penalty enforcing scene consistency.  This resolves the
    bow/stern 180° ambiguity that the shape-only template matcher and the
    scalar-regressing CNN both flip on, and is more accurate than either on
    the labelled set (0.7° clean / 6.1° occluded, 9/9 real occluded frames
    vs the CNN's 2.2°/9.3°, 8/9).  See `brain/ai_nav/learned/ship_parts/`.

    Input contract: an 80×80 RGB crop centred on the ship (a FIXED crop at
    the minimap centre — the ship is always rendered dead-centre there).
    ~40ms/tick on CPU (≈ the CNN's ~36ms).  Heading convention matches ours
    natively (0 = up/north, clockwise).
    """
    name = "ship_parts_unet"
    latency_budget_ms = 60.0

    def __init__(self, device: str = "cpu", step: int = 3):
        from brain.ai_nav.learned.ship_parts import ShipHeading
        self._est = ShipHeading(device=device, step=step)

    @staticmethod
    def _crop80(rgb: np.ndarray) -> Optional[np.ndarray]:
        """Centre-crop 80×80 at the minimap centre — the ship is always
        rendered dead-centre in the ship-centred minimap, so the crop is a
        FIXED region, not computed per tick.

        (An earlier version centred on the ship-green *centroid*.  The river
        is translucent and overworld labels bleed through it, adding stray
        green-ish text pixels in the water.  On t423 (2026-07-30, Y-tip) that
        stray green dragged the centroid ~34 px off the ship, pushing it into
        the crop corner beyond the pose-fit's ±16 px shift range → the fit
        rotated ~90° and read 273° instead of ~5°.  A fixed centre is immune:
        it read 273→6 on t423 with zero change on every clean tick.)"""
        if rgb.shape[:2] == (80, 80):
            return np.ascontiguousarray(rgb[..., :3])
        H, W, _ = rgb.shape
        cy = max(40, min(H - 40, H // 2))
        cx = max(40, min(W - 40, W // 2))
        crop = np.ascontiguousarray(rgb[cy - 40:cy + 40, cx - 40:cx + 40, :3])
        # Ship-presence guard: the ship green must actually be in the crop
        # (returns None on non-sea frames so estimate() reports no_ship_crop).
        r = crop[..., 0].astype(np.int16)
        g = crop[..., 1].astype(np.int16)
        b = crop[..., 2].astype(np.int16)
        if int(((g > 110) & (g - r > 60) & (g - b > 50)).sum()) < 5:
            return None
        return crop

    def estimate(self, frame: VisionFrame, prior: Optional[Heading]
                 ) -> Heading:
        rgb = np.asarray(frame.minimap())
        crop = self._crop80(rgb)
        if crop is None or crop.shape[:2] != (80, 80):
            bearing = prior.bearing_deg if prior else 0.0
            return Heading(bearing, confidence=0.0,
                           source="ship_parts_unet:no_ship_crop")
        r = self._est(crop)
        seen = list(r.parts_seen)
        # Confidence from the parts recovered.  All 5 parts ⇒ the pose is
        # fully anchored; bow OR stern present ⇒ the DIRECTION (not just the
        # axis) is fixed.  Only sails/hull ⇒ axis clear but bow/stern is a
        # coin flip, so cap it low and let prior-snapping downstream help.
        if not seen:
            conf = 0.15
        else:
            conf = min(0.97, 0.50 + 0.09 * len(seen))
            if "bow" not in seen and "stern" not in seen:
                conf = min(conf, 0.45)
        return Heading(
            float(r.heading) % 360.0, confidence=conf,
            source=f"ship_parts_unet(parts={'+'.join(seen) or 'none'})",
        )


class ParallelEnsembleHeading:
    """CNN inline, matcher in a background worker process.

    Every tick:
      1. Poll for any completed matcher results and stash them by
         frame_id.
      2. If ship is occluded, submit the current frame to the matcher
         (no-op if a job is already in flight or a fresh cached result
         exists).
      3. Run CNN inline (~2 ms).
      4. If a *recent* matcher result exists and it's confident, prefer
         it over the CNN answer.  "Recent" = within `MAX_LAG_TICKS`
         calls; ships don't turn fast enough for a 1-2 tick lag to
         matter under normal conditions.

    Latency profile (measured on the hard-case set):
      - Normal tick (nothing pending):  ~2 ms  (CNN only)
      - Occluded tick submitting a job: ~3 ms  (submit is cheap)
      - Occluded tick with matcher result ready: ~2 ms
      - Matcher itself: ~630 ms end-to-end but in another process,
        never blocking the tick loop.

    ProcessPoolExecutor is used (not ThreadPoolExecutor) to bypass the
    GIL — the matcher's Python-level shift loop wouldn't otherwise run
    truly parallel with the CNN.  Cost is one 19 KB pickle per
    submission (~1 ms), acceptable.
    """
    name = "parallel_ensemble"
    latency_budget_ms = 10.0    # inline path only; matcher is async

    VISIBLE_PX_THRESHOLD = 250       # below this, submit to matcher
    MAX_LAG_TICKS = 3                # matcher result older than this is stale
    MIN_MATCHER_CONF_TO_OVERRIDE = 0.4

    def __init__(self,
                 cnn_ckpt_path: Optional[str] = None,
                 reconstruct_template_path: Optional[str] = None,
                 reference_bg_path: Optional[str] = None,
                 enable_pca_tiebreak: bool = True,
                 shadow_ckpt_path: Optional[str] = None):
        from concurrent.futures import ProcessPoolExecutor
        from brain.ai_nav.learned.ship_template_reconstruct import (
            _worker_init,
        )
        self._cnn = CNNHeading(
            ckpt_path=cnn_ckpt_path,
            reference_bg_path=reference_bg_path,
            enable_pca_tiebreak=enable_pca_tiebreak,
            shadow_ckpt_path=shadow_ckpt_path,
        )
        self._executor = ProcessPoolExecutor(
            max_workers=1,
            initializer=_worker_init,
            initargs=(reconstruct_template_path,),
        )
        self._pending: Optional[tuple[int, "Future"]] = None  # (tick_id, future)
        self._latest_result: Optional[tuple[int, dict]] = None
        self._tick_counter = 0

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def estimate(self, frame: VisionFrame, prior: Optional[Heading]
                 ) -> Heading:
        from brain.ai_nav.learned.ship_template_reconstruct import (
            _worker_estimate,
        )
        self._tick_counter += 1
        tick = self._tick_counter
        rgb = np.asarray(frame.minimap())

        # 1. Reap any completed matcher result
        if self._pending is not None and self._pending[1].done():
            submitted_tick, fut = self._pending
            try:
                result_dict = fut.result()
                self._latest_result = (submitted_tick, result_dict)
            except Exception:
                pass
            self._pending = None

        # 2. Count visibility (once — used both for gating and logging)
        r = rgb[..., 0].astype(np.int16)
        g = rgb[..., 1].astype(np.int16)
        b = rgb[..., 2].astype(np.int16)
        visible_px = int(((g > 110) & (g - r > 60)
                          & (g - b > 50)).sum())

        # 3. Submit the current frame if occluded and no job in flight
        if (visible_px < self.VISIBLE_PX_THRESHOLD
                and self._pending is None):
            self._pending = (
                tick,
                self._executor.submit(_worker_estimate, rgb.copy()),
            )

        # 4. Run CNN inline (fast)
        cnn_heading = self._cnn.estimate(frame, prior)

        # 5. Use a recent, confident matcher result if we have one
        if self._latest_result is not None:
            submitted_tick, r_dict = self._latest_result
            lag = tick - submitted_tick
            if (lag <= self.MAX_LAG_TICKS
                    and r_dict["confidence"]
                        >= self.MIN_MATCHER_CONF_TO_OVERRIDE
                    and r_dict["confidence"] > cnn_heading.confidence):
                return Heading(
                    bearing_deg=r_dict["angle_deg"],
                    confidence=r_dict["confidence"],
                    source=(f"parallel:matcher(lag={lag},"
                            f"axis_conf={r_dict['axis_confidence']:.2f},"
                            f"dir_conf={r_dict['dir_confidence']:.2f}):"
                            f"cnn_shadowed={cnn_heading.bearing_deg:.0f}"),
                    raw_bearing_deg=r_dict["angle_deg"],
                    shadow_bearing_deg=cnn_heading.bearing_deg,
                )

        # 6. Fall through to CNN, tagging with matcher-status
        pending_note = "pending" if self._pending is not None else "idle"
        return Heading(
            bearing_deg=cnn_heading.bearing_deg,
            confidence=cnn_heading.confidence,
            source=(f"parallel:cnn(visible_px={visible_px},"
                    f"matcher={pending_note}):{cnn_heading.source}"),
            raw_bearing_deg=cnn_heading.raw_bearing_deg,
            shadow_bearing_deg=cnn_heading.shadow_bearing_deg,
        )


class EnsembleCNNReconstructHeading:
    """CNN when ship visibility high AND CNN is confident; matcher otherwise.

    Two-gate router (both gates cheap; failure to trip either triggers
    the ~630 ms matcher fallback):

      1. Visibility gate (~50 µs): count ship-green pixels, divide by
         canonical.  Below `VISIBILITY_RATIO_THRESHOLD` → matcher.
      2. CNN self-confidence gate (~2 ms): run CNN, check its returned
         confidence.  Below `CNN_CONF_THRESHOLD` → matcher.

    Why both:
      - Visibility alone is insufficient.  Observed 2026-07-14: CNN
        returned conf=0.00 for 13 straight ticks (t18-t30) but handed
        back a stale bearing (337.9°) via its `physics_reject:hold_prior`
        path, while visibility was 87 %.  Bot kept steering on the
        stale value.  Bad.
      - Confidence alone is also insufficient.  Observed earlier: on
        the hard-case set, CNN reports 0.68-0.94 confidence on frames
        it gets 16-47 ° wrong.
      - AND-ing them: mostly-visible frames where CNN is genuinely
        confident get the fast 2 ms path.  Everything else — hidden
        occlusion, CNN loses track, matcher rescue frames — gets the
        slower but honest matcher.

    Latency profile:
      - Mostly-visible + confident CNN: ~2 ms
      - Any other case:                 ~630 ms (CNN + matcher)
    """
    name = "ensemble_cnn_reconstruct"
    latency_budget_ms = 700.0

    VISIBILITY_RATIO_THRESHOLD = 0.70
    CNN_CONF_THRESHOLD = 0.60

    def __init__(self,
                 cnn_ckpt_path: Optional[str] = None,
                 reconstruct_template_path: Optional[str] = None,
                 reference_bg_path: Optional[str] = None,
                 enable_pca_tiebreak: bool = True,
                 shadow_ckpt_path: Optional[str] = None):
        self._cnn = CNNHeading(
            ckpt_path=cnn_ckpt_path,
            reference_bg_path=reference_bg_path,
            enable_pca_tiebreak=enable_pca_tiebreak,
            shadow_ckpt_path=shadow_ckpt_path,
        )
        self._reconstruct = ReconstructMatchHeading(
            template_path=reconstruct_template_path,
        )
        self._canonical_px = int(
            (np.asarray(self._reconstruct._matcher._template) > 128).sum()
        )

    def _visibility_ratio(self, rgb: np.ndarray) -> tuple[float, int]:
        r = rgb[..., 0].astype(np.int16)
        g = rgb[..., 1].astype(np.int16)
        b = rgb[..., 2].astype(np.int16)
        visible_px = int(((g > 110) & (g - r > 60)
                          & (g - b > 50)).sum())
        return visible_px / max(self._canonical_px, 1), visible_px

    def _matcher_result(self, frame: VisionFrame,
                        prior: Optional[Heading],
                        visible_px: int, ratio: float,
                        why: str,
                        shadow: Optional[Heading] = None,
                        ) -> Heading:
        matcher_h = self._reconstruct.estimate(frame, prior)
        return Heading(
            bearing_deg=matcher_h.bearing_deg,
            confidence=matcher_h.confidence,
            source=(f"ensemble:matcher_{why}(visible={visible_px}px,"
                    f"{ratio*100:.0f}%):{matcher_h.source}"),
            raw_bearing_deg=matcher_h.raw_bearing_deg,
            shadow_bearing_deg=(shadow.bearing_deg if shadow is not None
                                else matcher_h.shadow_bearing_deg),
        )

    def estimate(self, frame: VisionFrame, prior: Optional[Heading]
                 ) -> Heading:
        rgb = np.asarray(frame.minimap())
        ratio, visible_px = self._visibility_ratio(rgb)

        # Gate 1: visibility too low → matcher decides.  Still run CNN
        # once (~2 ms) so its reading gets logged as shadow — lets us
        # audit matcher-vs-CNN performance on low-visibility ticks
        # offline.
        if ratio < self.VISIBILITY_RATIO_THRESHOLD:
            cnn_h = self._cnn.estimate(frame, prior)
            return self._matcher_result(
                frame, prior, visible_px, ratio, "lowvis",
                shadow=cnn_h)

        # Gate 2: CNN self-confidence low → matcher rescue
        cnn_h = self._cnn.estimate(frame, prior)
        if cnn_h.confidence < self.CNN_CONF_THRESHOLD:
            return self._matcher_result(
                frame, prior, visible_px, ratio,
                f"cnnlowconf_{cnn_h.confidence:.2f}",
                shadow=cnn_h)

        # Both gates pass → trust the fast CNN answer
        return Heading(
            bearing_deg=cnn_h.bearing_deg,
            confidence=cnn_h.confidence,
            source=(f"ensemble:cnn(visible={visible_px}px,"
                    f"{ratio*100:.0f}%,conf={cnn_h.confidence:.2f}):"
                    f"{cnn_h.source}"),
            raw_bearing_deg=cnn_h.raw_bearing_deg,
            shadow_bearing_deg=cnn_h.shadow_bearing_deg,
        )


class CNNHeading:
    """Heading from the trained ship-only CNN
    (`brain/ai_nav/learned/heading_cnn.py`).

    Inference pipeline matches training:
      1. Detect green ship pixels → centroid
      2. Crop 80×80 around centroid
      3. Build ship-only mask = dilate(green, 2) AND NOT yellow
         (this STRIPS the yellow sonar fan AND any yellow NPC overlay
         like the pirate boss — both look the same to the model)
      4. Composite ship pixels over a fixed reference background
      5. Run CNN → (sin θ, cos θ) + confidence

    Confidence semantics: ~visibility-fraction of the ship after
    masking.  High when the ship is fully visible; drops sharply
    when occluded (e.g. pirate covers half the ship at voyage 9
    t140/t141 → conf 0.26-0.42).  Caller's physics-rejection +
    motion-bearing fallback handle the low-confidence cases.

    Validated empirically: at voyage 9 cold-start t1-t15, the
    legacy template-match was 120-180° off every single tick due
    to an antipode lock.  CNN was within 2-67° of motion bearing
    on each tick, confidence 0.71-0.87 — no cascade.
    """
    name = "ship_only_cnn"
    latency_budget_ms = 50.0

    # Yellow-mask thresholds — must match training-time
    # `_yellow_mask` in tools/extract_ship_sprites.py.
    _YELLOW_R = 180
    _YELLOW_G = 150
    _YELLOW_B = 130
    _YELLOW_RG_DIFF = 40

    # PCA antipode tiebreaker: when the CNN and PCA disagree by
    # ~180° they agree on the ship's principal AXIS but disagree
    # on which end is the bow.  PCA's brightness-based direction
    # pick is naturally robust to small NPC occluders (a pirate
    # face sitting on the bow doesn't shift PCA's average
    # brightness the way it shifts the CNN's shape-based bow
    # detection).  So when CNN and PCA are ~180° apart, we flip
    # CNN to its antipode (which aligns with PCA).
    # Concrete case: voyage 6 t413 — small pirate NPC at the bow,
    # CNN read 164° (wrong end), PCA read 331° (correct end),
    # human labeled 337°.
    _PCA_TIEBREAK_LOW_DEG  = 150.0    # window around 180° for antipode disagreement
    _PCA_TIEBREAK_HIGH_DEG = 210.0
    _PCA_AGREE_DEG         =  30.0    # CNN and PCA in same direction

    def __init__(self, ckpt_path: Optional[str] = None,
                 reference_bg_path: Optional[str] = None,
                 enable_pca_tiebreak: bool = True,
                 shadow_ckpt_path: Optional[str] = None):
        import torch
        from pathlib import Path as _P
        from PIL import Image as _PIL_Image
        from brain.ai_nav.learned.heading_cnn import HeadingCNN
        repo = _P(__file__).resolve().parents[3]
        if ckpt_path is None:
            ckpt_path = repo / "data" / "heading_cnn" / "best.pt"
        if reference_bg_path is None:
            reference_bg_path = (repo / "data" / "heading_backgrounds"
                                 / "bg_000000.png")
        self._enable_pca_tiebreak = enable_pca_tiebreak
        self._pca = PCAHeading() if enable_pca_tiebreak else None
        self._device = torch.device(
            "mps" if torch.backends.mps.is_available()
            else "cuda" if torch.cuda.is_available()
            else "cpu")
        self._model = HeadingCNN().to(self._device).eval()
        ckpt = torch.load(ckpt_path, map_location=self._device,
                          weights_only=False)
        self._model.load_state_dict(ckpt["model"])
        # Optional shadow model — runs in parallel for A/B comparison but
        # does NOT affect runtime decisions.  Its raw output is stashed
        # on the Heading result for trace logging.
        self._shadow_model = None
        if shadow_ckpt_path is not None:
            self._shadow_model = HeadingCNN().to(self._device).eval()
            shadow_ckpt = torch.load(shadow_ckpt_path,
                                     map_location=self._device,
                                     weights_only=False)
            self._shadow_model.load_state_dict(shadow_ckpt["model"])
        bg_path = _P(reference_bg_path)
        if bg_path.exists():
            self._ref_bg = np.asarray(
                _PIL_Image.open(bg_path).convert("RGB"))
        else:
            # Solid mid-grey fallback if no background library is
            # available.  Training also saw monochrome backgrounds
            # during the early data pipeline.
            self._ref_bg = np.full((80, 80, 3), 80, dtype=np.uint8)

    def estimate(self, frame: VisionFrame, prior: Optional[Heading]
                 ) -> Heading:
        import torch
        from scipy.ndimage import label as cc_label, binary_dilation
        from brain.ai_nav.learned.heading_cnn import decode_heading

        rgb = np.asarray(frame.minimap())
        H, W, _ = rgb.shape
        R, G, B = (rgb[..., c].astype(np.int16) for c in range(3))
        green = (G > 140) & (G - R > 30) & (G - B > 30)
        if int(green.sum()) < 10:
            bearing = prior.bearing_deg if prior else 0.0
            return Heading(bearing, confidence=0.0,
                           source="ship_only_cnn_miss")
        lab, n = cc_label(green)
        sizes = np.bincount(lab.ravel())[1:]
        biggest = int(np.argmax(sizes)) + 1
        ys, xs = np.where(lab == biggest)
        cy = float(ys.mean())
        cx = float(xs.mean())

        half = 40
        cy_i = int(round(max(half, min(H - half, cy))))
        cx_i = int(round(max(half, min(W - half, cx))))
        crop = rgb[cy_i - half:cy_i + half, cx_i - half:cx_i + half]

        cR, cG, cB = (crop[..., c].astype(np.int16) for c in range(3))
        c_green = (cG > 140) & (cG - cR > 30) & (cG - cB > 30)
        c_yellow = ((cR > self._YELLOW_R) & (cG > self._YELLOW_G)
                    & (cB < self._YELLOW_B)
                    & (np.abs(cR - cG) < self._YELLOW_RG_DIFF))
        ship_region = binary_dilation(c_green, iterations=2)
        ship_only = ship_region & ~c_yellow

        composite = self._ref_bg.copy()
        composite[ship_only] = crop[ship_only]

        arr = composite.astype(np.float32) / 255.0
        tensor = (torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)
                  .to(self._device))
        with torch.no_grad():
            sin_cos, conf = self._model(tensor)
            # Shadow model runs on the same composite tensor for a
            # fair A/B.  Its output does NOT affect runtime decisions —
            # only logged so offline analysis can compare.
            shadow_bearing = shadow_confidence = None
            if self._shadow_model is not None:
                s_sin_cos, s_conf = self._shadow_model(tensor)
                shadow_bearing = float(decode_heading(s_sin_cos).item())
                shadow_confidence = float(s_conf.item())
        bearing = float(decode_heading(sin_cos).item())
        confidence = float(conf.item())
        source = "ship_only_cnn"
        raw_bearing = bearing   # preserve pre-tiebreak for offline analysis

        # Flag disagreement between primary and shadow > 30° so
        # offline analysis can grep the trace easily.  Both raw
        # bearings are already logged; this just adds a source tag.
        if shadow_bearing is not None:
            disagree = abs(((bearing - shadow_bearing + 540.0)
                            % 360.0) - 180.0)
            if disagree > 30.0:
                source = f"{source}+cnn_shadow_disagree:{int(disagree)}"

        # PCA antipode tiebreaker.  See _PCA_TIEBREAK constants for
        # rationale.  Runs the older PCA-on-green detector; when it
        # disagrees with the CNN by ~180° (i.e. same axis, opposite
        # direction), we flip the CNN to its antipode.
        if self._enable_pca_tiebreak and self._pca is not None:
            try:
                pca_h = self._pca.estimate(frame, prior=None)
                if pca_h.confidence > 0.0:
                    diff = abs(((bearing - pca_h.bearing_deg + 540.0)
                                % 360.0) - 180.0)
                    if (self._PCA_TIEBREAK_LOW_DEG <= diff
                            <= self._PCA_TIEBREAK_HIGH_DEG):
                        # ~180° apart — same axis, opposite direction.
                        # Trust PCA's direction pick.
                        bearing = (bearing + 180.0) % 360.0
                        source = "ship_only_cnn+pca_tiebreak"
            except Exception:
                pass  # PCA is best-effort; don't crash if it fails
        return Heading(bearing, confidence=confidence, source=source,
                       raw_bearing_deg=raw_bearing,
                       shadow_bearing_deg=shadow_bearing,
                       shadow_confidence=shadow_confidence)


# ── Physics-consistency post-filter ────────────────────────────────────
#
# Ships have inertia: heading cannot change drastically in one tick
# unless either (a) we issued a hard steering command or (b) we hit
# something (sudden speed drop).  When neither holds, a big Δheading
# is an OCR / template-match artifact (NPC sprite covering the bow,
# antipode misread, etc.) and should be rejected.  Replace with
# whichever of {antipode of new, motion bearing} sits closer to the
# prior heading, or fall back to the prior if neither helps.
#
# Caught the canonical t142 case in
# `data/sessions/ai_nav_2026-06-24T20-49-36`: heading jumped 165° →
# 70° (Δ=-95°) with prev_cmd=0° and no speed drop — pirate boss
# partially blocked the ship sprite, matcher locked into the wrong
# antipode.  Without this filter the bot then issued hold_right@792ms
# based on the corrupted reading, slammed into the bank, and limped
# along the wrong heading for 30+ ticks.

PHYSICS_REJECT_MIN_DELTA_DEG    = 40.0    # heading delta beyond which we suspect OCR
PHYSICS_REJECT_MAX_PREV_CMD_DEG = 15.0    # legacy gate — kept for compatibility
# Plausible Δhdg given a commanded turn = |cmd| × (1 + REL) + FLOOR.
# REL=0.25 captures rotation-rate variance (anti-cheat jitter +
# game-physics noise); FLOOR=15° is the noise budget for "no
# commanded turn at all" cases.  These are starting points — the
# right calibration is per-hold-duration empirical variance,
# measured across many sessions.  See discussion 2026-06-30.
PHYSICS_REJECT_CMD_TOLERANCE_REL = 0.25
PHYSICS_REJECT_CMD_TOLERANCE_FLOOR = 15.0
PHYSICS_REJECT_MIN_PREV_SPEED   = 5.0     # was actually cruising before
PHYSICS_REJECT_SPEED_DROP_FRAC  = 0.30    # ≥30% drop ⇒ collision (legit reason for big Δhdg)
# Bounce-aware trust: when the ship is currently at very low speed
# it's probably mid-bounce or just recovered from one, and it can
# legitimately have rotated a lot.  Trust perception in this regime.
# Concrete case: 2026-06-30 voyage 6 t405-t414 (deep dead-end
# thrash, sp=0-1.5 kt), CNN was correct on 9/10 ticks but
# physics_reject substituted the antipode because delta > plausible.
# Human-labeled truth confirmed CNN was right; the substitutions
# were the failures.
PHYSICS_REJECT_LOW_SPEED_KT     = 1.5
PHYSICS_REJECT_BEST_IMPROVE_X   = 2.0     # candidate must beat new reading by ≥this factor
# Graded bounce-recovery trust.  When curr_speed / top_speed_est is
# below RECOVERY_RATIO, the ship is likely in/just-after a bounce and
# a big Δheading is more likely valid than spurious.  Slow ships take
# 3-5 ticks to regain speed after a bounce (so binary "curr < 1.5 kt"
# suffices); fast ships (27 kt) regain in 1 tick, so we need to grade
# the trust by the recovery ratio.  The plausible-delta budget is
# inflated proportional to (1 - ratio) — at ratio=0 it's 3× baseline,
# at ratio=1 it's unchanged.  See conversation notes 2026-07-23.
BOUNCE_RECOVERY_RATIO           = 0.75
BOUNCE_RECOVERY_MAX_INFLATE     = 3.0


def _angular_diff_deg(a: float, b: float) -> float:
    return abs(((a - b + 540.0) % 360.0) - 180.0)


def validate_heading_physics(
    new_heading: Heading,
    prior_heading: Optional[Heading],
    prev_commanded_deg: float,
    prev_speed: Optional[float],
    curr_speed: Optional[float],
    motion_bearing_deg: Optional[float],
    top_speed_est: Optional[float] = None,
) -> Heading:
    """Reject implausible heading jumps; substitute antipode or motion
    bearing if either is much closer to prior heading.

    Inputs are read at the call site:
      - prev_commanded_deg: last tick's *commanded* delta (degrees,
        positive = right).  Derived from the planner output that was
        issued *before* this tick's heading reading.
      - prev_speed / curr_speed: speed_kt last tick / this tick, if
        OCR succeeded.  Pass None when unavailable; the rule then
        relies on the commanded-turn check alone.
      - motion_bearing_deg: bearing inferred from lat/lon Δ over the
        last few ticks.  Pass None when motion is too small / large
        to trust (caller decides; see CROSS_CHECK_MOTION_* constants
        in tools/build_rl_dataset.py).

    Returns a Heading: either the original `new_heading` (no rejection
    needed), a corrected one with `source` tagged
    `+physics_reject:{antipode|motion|hold_prior}`, or the prior held
    over with confidence=0 when no candidate is acceptable.
    """
    if prior_heading is None:
        return new_heading
    delta = _angular_diff_deg(new_heading.bearing_deg, prior_heading.bearing_deg)
    if delta < PHYSICS_REJECT_MIN_DELTA_DEG:
        return new_heading
    # Big delta — was it explained by either a commanded turn or a collision?
    # Use the commanded turn as an UPPER BOUND on plausible rotation
    # (with a small tolerance for actuator + perception noise).  The
    # old binary rule ("if cmd>15° accept any delta") was too
    # permissive: it let single-tick CNN spikes through whenever the
    # bot happened to have commanded a moderate turn the prior tick.
    # Concrete example: 2026-06-30 voyage 15:14, t30 — CNN spiked to
    # 11° from 182° prior (Δ=171°) while prev_cmd was −31°.  Old rule
    # accepted because |cmd|≥15°; new rule rejects because Δ > |cmd|
    # + 25° = 56°.
    abs_cmd = abs(prev_commanded_deg)
    plausible_delta = (abs_cmd * (1.0 + PHYSICS_REJECT_CMD_TOLERANCE_REL)
                       + PHYSICS_REJECT_CMD_TOLERANCE_FLOOR)
    # Graded bounce-recovery inflation.  Fast ships regain speed in 1
    # tick after a bounce, so the binary curr < 1.5 kt clause below
    # doesn't cover the recovery tick where the rotation actually
    # completes.  Inflate plausible_delta smoothly by
    # (curr_speed / top_speed_est).  At ratio ≥ 0.75 no inflation.
    if (curr_speed is not None and top_speed_est is not None
            and top_speed_est > 0.0):
        ratio = min(1.0, curr_speed / top_speed_est)
        if ratio < BOUNCE_RECOVERY_RATIO:
            # Linear ramp: ratio=0 → max inflate, ratio=RECOVERY → 1x
            deficit = (BOUNCE_RECOVERY_RATIO - ratio) / BOUNCE_RECOVERY_RATIO
            inflate = 1.0 + (BOUNCE_RECOVERY_MAX_INFLATE - 1.0) * deficit
            plausible_delta *= inflate
    if delta < plausible_delta:
        return new_heading
    if (prev_speed is not None and curr_speed is not None
            and prev_speed >= PHYSICS_REJECT_MIN_PREV_SPEED):
        drop = (prev_speed - curr_speed) / prev_speed
        if drop > PHYSICS_REJECT_SPEED_DROP_FRAC:
            return new_heading
    # Bounce state: currently at very low speed => trust perception.
    # Ships legitimately rotate a lot while bouncing off shore or
    # spinning post-impact.  A big Δhdg with speed ~ 0 is expected,
    # not a perception error.  But if pca_tiebreak flipped the raw
    # CNN, sanity-check: bounce sprites are noisy for PCA, so if the
    # RAW reading is much closer to prior than the flipped one, prefer
    # raw (un-flip the tiebreaker).
    if (curr_speed is not None
            and curr_speed < PHYSICS_REJECT_LOW_SPEED_KT):
        if new_heading.raw_bearing_deg is not None:
            raw_delta = _angular_diff_deg(new_heading.raw_bearing_deg,
                                          prior_heading.bearing_deg)
            if raw_delta * 1.2 < delta:
                return Heading(
                    bearing_deg=new_heading.raw_bearing_deg,
                    confidence=new_heading.confidence * 0.9,
                    source=f"{new_heading.source}+bounce_prefer_raw",
                    raw_bearing_deg=new_heading.raw_bearing_deg,
                    shadow_bearing_deg=new_heading.shadow_bearing_deg,
                    shadow_confidence=new_heading.shadow_confidence,
                )
        return new_heading

    # Physics violated. Try alternative candidates, ordered by their
    # closeness to the prior heading.
    candidates: list[tuple[float, float, str]] = []
    anti = (new_heading.bearing_deg + 180.0) % 360.0
    candidates.append((_angular_diff_deg(anti, prior_heading.bearing_deg),
                       anti, "antipode"))
    if motion_bearing_deg is not None:
        candidates.append((_angular_diff_deg(motion_bearing_deg,
                                              prior_heading.bearing_deg),
                           motion_bearing_deg, "motion"))
    candidates.sort()
    best_delta, best_brg, best_src = candidates[0]
    # Only adopt the candidate if it's substantially better than the
    # raw new reading.  Otherwise neither makes sense — hold prior.
    if best_delta * PHYSICS_REJECT_BEST_IMPROVE_X < delta:
        return Heading(
            bearing_deg=best_brg,
            confidence=new_heading.confidence * 0.7,
            source=f"{new_heading.source}+physics_reject:{best_src}",
            raw_bearing_deg=new_heading.raw_bearing_deg,
            shadow_bearing_deg=new_heading.shadow_bearing_deg,
            shadow_confidence=new_heading.shadow_confidence,
        )
    return Heading(
        bearing_deg=prior_heading.bearing_deg,
        confidence=0.0,
        source=f"{new_heading.source}+physics_reject:hold_prior",
        raw_bearing_deg=new_heading.raw_bearing_deg,
        shadow_bearing_deg=new_heading.shadow_bearing_deg,
        shadow_confidence=new_heading.shadow_confidence,
    )


# ── Stub: VLM full-screen reader ───────────────────────────────────────


class VLMHeading:
    """VLM (Qwen2-VL / Moondream) reads the full screen and emits
    the bow bearing as text.  Slow (~1–3 s), only useful when CNN
    confidence is repeatedly low.

    Calls `frame.full_screen()` — this is the swap point the user
    asked for: when this layer is plugged in, perception moves from
    minimap to full screen without changing the pipeline.

    Not implemented yet.
    """
    name = "vlm"
    latency_budget_ms = 3000.0

    def estimate(self, frame, prior):
        raise NotImplementedError(
            "VLMHeading not implemented yet — see docs/ai_navigation_landscape.md §5.4"
        )
