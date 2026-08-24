"""WorldMapNavigator — pan-and-find for the world map.

Origin: 2026-05-13.  The list-scroll "port search" path (in
`sail_actions._try_port_search`) only contains nearby/region-loaded
ports, so it cannot resolve cross-ocean targets — e.g. London while
the bot is in the Caribbean.  The fleet ran out of food at sea on
2026-05-12 because the list scroll exhausted without finding London
and the existing fallback ("visual panning") didn't have coordinate
information to navigate by.

Approach (validated empirically on 7 labelled world-map frames):

  1. Bake real game-internal coordinates of every port (224 entries)
     from voyage.tw → memory/knowledge/world_map/port_coordinates.json.
  2. Per pan: capture, parse via OmniParser, match labels against the
     catalogue → list of (port, pixel_pos) pairs.
  3. From visible pairs, derive pixels-per-game-unit by median of far-
     pair same-axis ratios (CV ≤ 5% on clean views).
  4. Back-project to estimate what game-coord is at the screen centre.
  5. Compute swipe vector to bring the target into view; swipe; loop.

See:
  • docs/four_layer_nav_classification.md — perception architecture
  • tools/world_map_calibrate.py            — the diagnostic that
    measured scale CV per frame and validated linearity + crop bounds
  • vision/world_map_parser.py              — the shared OmniParser →
    catalogue-match pipeline

Failure modes the design accepts (not bugs):

  • At extreme zoom, < 3 ports may be visible per frame; calibration
    is noisier but direction-of-travel is still correct.
  • Pinch-zoom-out is not implementable via ADB, so the bot pans at
    whatever zoom the user / game left the map at.  Multi-pan handles
    long distances naturally.
  • Map wrap-around (date-line) IS modelled (2026-08-13): _wrap_dx picks the
    shortest horizontal delta across the globe seam (wrap period derived from the
    lat/lon affine). Latitude does not wrap, so vertical uses the direct delta.
"""

from __future__ import annotations

import json
import math
import statistics
import random
import time
from itertools import combinations
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger

from vision.world_map_parser import VisiblePort, parse_visible_ports, load_port_catalogue


# ── Scale persistence ────────────────────────────────────────────────────────
#
# Scale (game-units per screen pixel) depends only on the world-map zoom level,
# which is stable across sessions unless the user manually pinch-zooms.  By
# persisting the last good calibration we can pre-plan multi-pan strides
# WITHOUT needing a first-frame parse just to figure out the scale.
#
# On the first `pan_to_port` call ever, no persisted file exists → we fall back
# to the iterative loop and save the scale after the first successful
# calibration there.  Subsequent calls (this session or future) can stride.

def fold_name(s: str) -> str:
    """Accent- and case-insensitive key: 'Malé' → 'male'.

    The catalogue stores display spellings ('Malé'), map labels OCR WITHOUT the accent
    ('male'), and callers arrive with either — `catalogue_coords()` hands the mission
    the stripped form. Comparing raw strings therefore fails on every accented port:
    live 2026-08-21 the gather leg reported "'Male' not in port catalogue" while Malé
    sat right there, and the mission stalled on its nearest supplier.

    Module-level so it is the ONE implementation: the departure-notice check in
    sail_actions has to bridge the same 'Male'/'Malé' gap.
    """
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c)).lower().strip()


_SCALE_CACHE_PATH = Path("memory/knowledge/world_map/calibration.json")


def _frame_says_undiscovered(frame) -> bool:
    """True when the map itself prints 'Undiscovered Area' — the game's own fog label."""
    try:
        from actions.sail_actions import _ocr_frame
        text = " ".join((t or "").lower() for t, _c, _x, _y in _ocr_frame(frame, min_conf=0.3))
        return "undiscovered" in text
    except Exception as exc:
        logger.debug(f"[pan_to_port] fog-label check failed: {exc}")
        return False


# Consecutive label-free (or explicitly fogged) frames that mean the camera is over
# unexplored map rather than mid-pan.
_BLANK_FRAMES_MEAN_UNEXPLORED = 4
# How many times to recentre on the fleet before admitting the search cannot proceed.
_MAX_RECENTERS = 2
# Within this many game units, dead-reckoning considers the target reached — so a blank
# screen here is contradictory rather than merely "not there yet".
_NEAR_TARGET_UNITS = 400


def _load_persisted_scale() -> Tuple[Optional[float], Optional[float]]:
    """Return (scale_x, scale_y) from disk, or (None, None) if missing/corrupt
    or implausible.

    Implausible values must NOT be returned because they would become the
    `cached` reference inside `pan_to_port`, and the sanity check rejects
    fresh calibrations that diverge from the cache — i.e. a poisoned
    cache permanently rejects every correct fresh value.  The 2026-05-20
    failure had `scale_x=-0.0046, scale_y=0.0255` persisted from an
    earlier buggy run.  Delete the file on detection so it stops biting.
    """
    try:
        data = json.loads(_SCALE_CACHE_PATH.read_text())
        sx = float(data["scale_x"])
        sy = float(data["scale_y"])
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None, None
    if not (_is_plausible_scale(sx) and _is_plausible_scale(sy)):
        logger.warning(
            f"[scale-cache] persisted scale ({sx}, {sy}) is implausible — "
            f"deleting {_SCALE_CACHE_PATH} and starting fresh"
        )
        try:
            _SCALE_CACHE_PATH.unlink()
        except OSError:
            pass
        return None, None
    if not _is_isotropic_scale(sx, sy):
        logger.warning(
            f"[scale-cache] persisted scale ({sx}, {sy}) is anisotropic "
            f"(ratio > {_MAX_ANISOTROPY_RATIO}× — world map should render "
            f"isotropically) — deleting {_SCALE_CACHE_PATH} and starting fresh"
        )
        try:
            _SCALE_CACHE_PATH.unlink()
        except OSError:
            pass
        return None, None
    return sx, sy


def _save_persisted_scale(scale_x: float, scale_y: float) -> None:
    """Write the latest good scale to disk for future sessions.

    Refuses to save implausible values — saving garbage would corrupt
    the next session's cache (see _load_persisted_scale).
    """
    if not (_is_plausible_scale(scale_x) and _is_plausible_scale(scale_y)):
        logger.debug(
            f"[scale-cache] refusing to save implausible scale "
            f"({scale_x}, {scale_y})"
        )
        return
    if not _is_isotropic_scale(scale_x, scale_y):
        logger.warning(
            f"[scale-cache] refusing to save anisotropic scale "
            f"({scale_x:.2f}, {scale_y:.2f}) — world map renders "
            f"isotropically, this is a bad calibration"
        )
        return
    try:
        _SCALE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "scale_x": float(scale_x),
            "scale_y": float(scale_y),
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        _SCALE_CACHE_PATH.write_text(json.dumps(payload, indent=2))
    except OSError as exc:
        logger.debug(f"[scale-cache] could not save: {exc}")


# ── Tuning knobs ─────────────────────────────────────────────────────────────

# Pairs must span ≥ this game-coord distance on the axis to contribute to
# scale calibration.  Below this, the OCR centroid jitter (~10-20 px) is
# a large fraction of Δgame, blowing up the ratio.  Empirically: clean
# views with far pairs give CV 3-5%, mixed views with close pairs give
# 10-25% — the filter promotes the former.
_MIN_PAIR_GAME_DIST = 80

# Swipe magnitude clamp.  A swipe larger than this (as a fraction of
# screen size) gets interpreted by the game as a "fling" — content slides
# with inertia and overshoots in unpredictable ways.  80% is the empirical
# safe ceiling.  Used for the *math* in pan_to_port; the actual gesture
# is further constrained to the safe map rect by _swipe_pan.
_SWIPE_FRACTION_MAX = 0.8

# Map safe rectangle: the central area of the world map that contains
# *only* terrain/port labels — no UI controls.  Swipes that touch down
# on the top toolbar (mode tabs, Search) or the bottom toolbar
# (Trade Event, My Location, Filter) get interpreted by UWO as button
# interactions and the drag motion is discarded.  Anchoring inside this
# rect prevents that.
#
# Approximate boundaries on 2400×1080:
#   • left  ≥ 250  — leaves room for the left-edge port-list icon column
#   • top   ≥ 200  — clears the "World Map / Port / Explore / Route /
#                     Trade / Search" toolbar (≈ y < 180)
#   • right ≤ 2150 — clears the right-edge Target Location / My Location
#                     indicators
#   • bot   ≤ 900  — clears the bottom toolbar (Trade Event, Schedule,
#                     My Location, Filter) which sits at y ≈ 950+
_MAP_SAFE_LEFT   = 250
_MAP_SAFE_TOP    = 200
_MAP_SAFE_RIGHT  = 2150
_MAP_SAFE_BOTTOM = 900

# Slow swipe so the game treats it as a pan, not a fling.
_SWIPE_DURATION_MS = 600

# Settle time after each pan before re-capturing.
_PAN_SETTLE_S = 1.5

# Settle time between consecutive stride pans (no perception between them,
# so a shorter delay is enough to let the pan animation finish).
_STRIDE_SETTLE_S = 0.6

# Last-resort: when calibration fails (zero usable pairs), pan blindly
# this fraction of the screen in the direction the catalogue says the
# target lies.  Smaller than the clamped pan so we don't overshoot.
_BLIND_PAN_FRACTION = 0.6

# ── Globe wrap (date-line) ────────────────────────────────────────────────────
# The world map wraps HORIZONTALLY like a globe: pan far enough east and you come
# back to where you started.  Latitude (vertical) does NOT wrap.  So the direction
# to a target must use the SHORTEST horizontal delta across the seam — otherwise a
# far port (e.g. Nagasaki→Port Royal) is chased the long way and the pan budget
# runs out before it's found.  The wrap period (catalogue-x per 360° longitude) is
# derived from the calibrated lat/lon affine so it tracks the real map.
_WORLD_WRAP_FALLBACK = 10269.0
_wrap_period_cache: Optional[float] = None


def _world_wrap_gx() -> float:
    global _wrap_period_cache
    if _wrap_period_cache is not None:
        return _wrap_period_cache
    w = _WORLD_WRAP_FALLBACK
    try:
        from actions.latlon_localize import latlon_to_catalogue
        a, b = latlon_to_catalogue(0, 0), latlon_to_catalogue(0, 360)
        if a and b and 6000 < abs(b[0] - a[0]) < 16000:
            w = abs(b[0] - a[0])
    except Exception:
        pass
    _wrap_period_cache = w
    return w


def _wrap_dx(dgx: float) -> float:
    """Shortest horizontal game-coord delta to a target across the globe seam."""
    w = _world_wrap_gx()
    dgx %= w
    return dgx - w if dgx > w / 2 else dgx


# Anchored-swipe margin: stay this many pixels off the screen edge to
# avoid system gestures (back, status bar, navigation pill).
_EDGE_MARGIN = 80

# Per-axis minimum pan magnitude in pixels.  Below this the game tends
# to register the gesture as a tap rather than a drag and the map
# doesn't move at all.  ~1 cm on a typical phone (≈ 180-200 px on a
# 2400×1080 mirror).  When the computed Δ on an axis is non-zero but
# below this floor, scale it up to the minimum — direction preserved.
_MIN_PAN_PX = 200


# Plausible bounds on world-map scale (pixels per game-coord-unit).
# Empirically the typical zoom yields ~2.0; the user can pinch-zoom but
# the range is bounded.  Values outside this window are calibration
# errors (mis-matched ports → bogus pair ratio), not real zoom changes.
_MIN_PLAUSIBLE_SCALE = 0.3
_MAX_PLAUSIBLE_SCALE = 15.0
# Within a single pan_to_port call zoom doesn't change.  Reject fresh
# calibration if it diverges from the cached value by more than this
# fraction — keeps a single bad pair from overwriting a good cache.
_SCALE_CHANGE_TOLERANCE = 0.5


# The world map renders isotropically at any fixed zoom level — pixels-
# per-game-unit should be the same on x and y.  Anisotropic ratios in
# practice come from bad calibration (a noisy single pair, mostly-y or
# mostly-x separations).  Reject scales where the axes differ by more
# than this multiplicative factor.
_MAX_ANISOTROPY_RATIO = 1.5


def _is_isotropic_scale(
    scale_x: Optional[float], scale_y: Optional[float],
) -> bool:
    """True iff scale_x and scale_y are within _MAX_ANISOTROPY_RATIO of
    each other.  Both must be > 0.

    Origin: 2026-05-21 live run had persisted (1.92, 0.86) — a 2.2×
    anisotropy that produced the bot's village-search oscillation.
    """
    if not scale_x or not scale_y or scale_x <= 0 or scale_y <= 0:
        return False
    ratio = max(scale_x, scale_y) / min(scale_x, scale_y)
    return ratio <= _MAX_ANISOTROPY_RATIO


def _is_plausible_scale(
    scale: Optional[float], *, cached: Optional[float] = None,
) -> bool:
    """True if *scale* is in a sensible range and (if cache is set)
    close enough to *cached* to be the same zoom level.

    Background: with too few or noisy anchors, _calibrate can return
    near-zero or even negative values (e.g. -0.00, 0.03 on 2026-05-20
    when 3 visible ports included a chrome label).  Blindly accepting
    such values overwrote a good cached scale and the rest of the loop
    issued garbage vectors.
    """
    if scale is None or scale <= 0:
        return False
    if not (_MIN_PLAUSIBLE_SCALE <= scale <= _MAX_PLAUSIBLE_SCALE):
        return False
    if cached is not None and cached > 0:
        ratio = scale / cached
        if ratio < (1 - _SCALE_CHANGE_TOLERANCE) \
                or ratio > (1 + _SCALE_CHANGE_TOLERANCE):
            return False
    return True


def _enforce_min_pan(delta: float) -> float:
    """Floor a non-zero per-axis delta at *_MIN_PAN_PX*, preserving sign.
    Zero stays zero (no spurious motion when the axis is already aligned).
    """
    if 0 < abs(delta) < _MIN_PAN_PX:
        return _MIN_PAN_PX if delta > 0 else -_MIN_PAN_PX
    return delta


def _pick_scale(
    fresh: Optional[float],
    odo: Optional[float],
    cached: Optional[float],
    *,
    axis: str,
) -> Tuple[Optional[float], str]:
    """Choose the most authoritative scale value from the three signals
    available each frame.  Returns (value, source) where *source* is one
    of "fresh+odo", "odo-override", "odo", "fresh", or "cache".

    Priority:
      1. fresh + odo both plausible and agree (within ±30%) — use fresh
         (more anchors, lower noise per anchor).
      2. fresh + odo disagree — trust odo (independently derived from
         observed screen + known catalogue + tracked swipe; can't be
         polluted by a bad pair the way fresh can).  Log a warning.
      3. only odo plausible — use it.
      4. only fresh plausible — use it.
      5. otherwise — keep cached (even if cache itself is plausible we
         still prefer fresh/odo when available; this branch only fires
         when neither produced a usable value this frame).
    """
    fp = _is_plausible_scale(fresh)
    op = _is_plausible_scale(odo)
    if fp and op:
        ratio = fresh / odo if odo else 0
        if 0.7 <= ratio <= 1.3:
            return fresh, "fresh+odo"
        logger.warning(
            f"[pan_to_port] scale_{axis}: fresh={fresh:.3f} disagrees with "
            f"odometry={odo:.3f} (ratio={ratio:.2f}) — adopting odometry"
        )
        return odo, "odo-override"
    if op:
        return odo, "odo"
    if fp:
        return fresh, "fresh"
    return cached, "cache"


# ── Navigator ────────────────────────────────────────────────────────────────

class WorldMapNavigator:
    """Find a target port on the world map by panning iteratively.

    Stateless across calls — each pan_to_port invocation is self-contained
    and starts with no cached scale.  *Within* a single call the scale
    is cached after the first successful calibration and reused on
    subsequent iterations, because zoom never changes during a pan_to_port
    call.  This lets sparse-region frames (≤1 visible port) still produce
    a real swipe vector instead of falling into the blunt fixed-fraction
    blind pan.
    """

    def __init__(
        self,
        catalogue: Optional[dict] = None,
        aliases:   Optional[dict] = None,
    ) -> None:
        """Construct a navigator over the given *catalogue*.

        Defaults to the port catalogue + port aliases so existing
        callers don't change.  Pass a village catalogue to navigate to
        villages instead — `pan_to_port` is catalogue-agnostic, the name
        is historical.  See `make_village_navigator()` for the
        village-flavoured factory.
        """
        if catalogue is not None:
            self._ports = catalogue
        else:
            self._ports = load_port_catalogue()
        if aliases is not None:
            self._aliases = aliases
        else:
            try:
                from actions.sail_actions import _PORT_ALIASES
                self._aliases = _PORT_ALIASES
            except Exception:
                self._aliases = {}

    # ── Public ────────────────────────────────────────────────────────────────

    def pan_to_port(
        self, name: str, max_pans: int = 8,
        from_port: Optional[str] = None,
    ) -> Optional[Tuple[int, int]]:
        """Pan until *name* is visible, then return its on-screen tap
        position.  Caller is responsible for tapping it and handling
        the resulting City Info panel.

        If *from_port* is supplied and a previously-calibrated scale is
        persisted on disk, the first leg is *pre-planned*: we compute the
        total Δpix from departing port to target and issue back-to-back
        stride pans without perception between them, then fall into the
        normal iterative loop for fine-tuning.  This relies on the
        invariant that the world map opens centred on the fleet, and the
        fleet sits at the departing port immediately after departure.

        Returns None when the target is not in the catalogue, no visible
        ports could be matched to ground the calibration, or *max_pans*
        iterations exhausted without finding the target.
        """
        target_key = name.lower().strip()
        target_info = self._ports.get(target_key)
        if target_info is None:
            logger.warning(f"[pan_to_port] {name!r} not in port catalogue")
            return None
        target_gx, target_gy = target_info["x"], target_info["y"]

        from capture.adb_capture import capture_screen

        # Scale is invariant within one pan_to_port call (no zoom changes),
        # so cache it across iterations.  Sparse regions where only 1 port
        # is visible cannot recalibrate (need ≥2 ports for far-pair scale),
        # but with cached scale + 1 anchor _estimate_center can still
        # compute a real centre — far better than the fixed 0.6×screen
        # blind pan which overshoots when the target is close.
        # Seed from the persisted scale (if any) so even iteration 1
        # benefits from prior knowledge in sparse regions.
        cached_scale_x, cached_scale_y = _load_persisted_scale()
        # Resolve departing port once — used by both stride and odometry.
        from_info = self._lookup_port(from_port) if from_port else None
        logger.info(
            f"[pan_to_port] target={name!r} from_port={from_port!r} "
            f"from_info={'hit' if from_info else 'MISS'} "
            f"cached_scale=({cached_scale_x},{cached_scale_y})"
        )
        # Track total commanded swipe across stride + iterations.  Used by
        # _odometry_scale to derive a scale that doesn't depend on the
        # potentially-poisoned pair-ratio cache.
        total_swipe_dpx = 0
        total_swipe_dpy = 0
        pans_used = 0

        # ── Pre-planned stride ───────────────────────────────────────────
        # Conditions: we know the departing port (catalogue match) AND we
        # have persisted scale.  Compute total Δpix and issue most of the
        # distance as back-to-back swipes, leaving budget for the
        # iterative recalibration loop to fine-tune.
        if cached_scale_x and cached_scale_y and from_info is not None:
            pans_used, stride_dpx, stride_dpy = self._stride_from(
                from_info, target_gx, target_gy,
                cached_scale_x, cached_scale_y,
                max_pans=max_pans,
            )
            total_swipe_dpx += stride_dpx
            total_swipe_dpy += stride_dpy

        # Iterative loop budget shrinks by however many pans the stride used.
        remaining = max(1, max_pans - pans_used)
        # Stuck detector: if the same (visible-set, swipe-vector) repeats
        # the map didn't move and we're wasting budget.
        last_swipe: Optional[Tuple[int, int]] = None
        last_visible_keys: Optional[frozenset] = None
        # Closed-loop swipe calibration (user 2026-08-20): each iteration we KNOW how many px we
        # swiped and (via water-tap localize) how far the camera actually moved in game units —
        # the ratio IS the true scale at the current zoom.  A stale persisted scale (calibrated at
        # another zoom) made every swipe ~2× too far, ping-ponging across the target (Jakarta →
        # Melanesian Village oscillated 7714↔9431 around 8554 and gave up).  Update the scale from
        # each observed swipe so the pan converges.
        cal_prev_wt: Optional[Tuple[float, float]] = None   # water-tap fix before the last swipe
        cal_last_swipe: Optional[Tuple[int, int]] = None    # px actually swiped since that fix
        cal_expected: Optional[Tuple[float, float]] = None  # camera we EXPECTED the swipe to reach
        pan_step = 0                                        # per-swipe step counter (logging)
        blank_streak = 0                                    # consecutive frames with NO labels
        recenters = 0                                       # My Location recoveries used
        last_delta: Optional[float] = None                  # game units still to travel
        for attempt in range(1, remaining + 1):
            frame = capture_screen()
            visible = parse_visible_ports(frame, self._ports, self._aliases)
            logger.info(
                f"[pan_to_port] attempt {attempt}/{remaining} "
                f"(after {pans_used} stride pan(s)): "
                f"{len(visible)} visible port(s)"
            )

            # UNEXPLORED / CLOUD-COVERED: the world map fogs regions the player has never
            # sailed, and no label renders under fog. Panning reads that as "not here yet"
            # and keeps swiping until it runs out of attempts, then reports "not found" —
            # which is misleading, because the target could be dead centre and still
            # invisible (user 2026-08-21: "it swiped to an area the bot has not explored,
            # so it is just cloud covered"). A populated map region always shows SOME
            # port, so several consecutive label-free frames mean the camera is over fog
            # or open ocean, and more swiping cannot help. Stop and say so.
            # The game LABELS fog: an unexplored region renders the words "Undiscovered
            # Area" on the map (seen live 2026-08-21 while hunting Melanesian Village).
            # That is positive evidence the camera is over never-visited territory —
            # far stronger than merely counting label-free frames, which can also mean
            # "still crossing open ocean". Treat it as a blank frame so the streak reacts.
            fogged = _frame_says_undiscovered(frame)
            if not visible or fogged:
                if fogged:
                    logger.info("[pan_to_port] the map reads 'Undiscovered Area' here — "
                                "this region has never been visited")
                blank_streak += 1
                # WHY nothing is visible has several possible answers, and they do not
                # exclude one another (user 2026-08-21):
                #   • still en route — a far target crosses open ocean with nothing in view;
                #   • the area was never visited, so it renders as CLOUD (that is what
                #     cloud MEANS — unexplored, not merely off-screen);
                #   • the camera went somewhere wrong;
                #   • or the target is simply not in view yet, cloud or no cloud.
                # Blankness alone therefore proves nothing. What IS contradictory is blank
                # WHILE dead-reckoning claims we have arrived: a catalogued port has been
                # visited, so its surroundings are cleared and SOMETHING would render. That
                # combination means the camera is lost, not the target.
                arrived = last_delta is not None and last_delta <= _NEAR_TARGET_UNITS
                if (blank_streak >= _BLANK_FRAMES_MEAN_UNEXPLORED and pans_used >= 1
                        and arrived):
                    if recenters < _MAX_RECENTERS and self._recenter_on_fleet(frame):
                        recenters += 1
                        blank_streak = 0
                        total_swipe_dpx = total_swipe_dpy = 0   # the old track is void
                        from_info = None                        # re-localize from scratch
                        cal_expected = None
                        logger.warning(
                            f"[pan_to_port] cloud/open ocean — the camera is lost, not the "
                            f"target ({name!r} is a known place and known places are never "
                            f"fogged). Recentred on the fleet and restarting the search "
                            f"(recentre {recenters}/{_MAX_RECENTERS})."
                        )
                        continue
                    logger.warning(
                        f"[pan_to_port] {blank_streak} frames with no port labels while "
                        f"dead-reckoning says we are on top of {name!r}, and no way to "
                        "recentre. Use the typed port search."
                    )
                    return None
                if blank_streak == _BLANK_FRAMES_MEAN_UNEXPLORED and not arrived:
                    logger.info(
                        f"[pan_to_port] nothing in view, but still ~{last_delta:.0f} game "
                        "units out — unexplored ocean en route looks exactly like this, "
                        "so keep panning rather than assuming the camera is lost."
                        if last_delta is not None else
                        "[pan_to_port] nothing in view and no distance estimate yet — "
                        "keep panning."
                    )
            else:
                blank_streak = 0

            # Hit?  Compare by CATALOGUE ENTRY, not key string — a village entry is reachable
            # under two keys ('melanesian' AND 'melanesian village'); the visible-set reports the
            # short key while the target key is the display name, and a string compare panned
            # forever while staring at the target (live 2026-08-20).
            for vp in visible:
                if vp.key == target_key or self._ports.get(vp.key) is target_info:
                    logger.info(
                        f"[pan_to_port] FOUND {vp.name!r} @ {vp.tap_pos}"
                    )
                    try:
                        from actions import action_trace
                        if action_trace.active():
                            action_trace.record_decision(
                                inputs={"attempt": attempt,
                                        "visible_ports": len(visible),
                                        "target": [target_gx, target_gy],
                                        "found": vp.name},
                                output={"result": "FOUND",
                                        "tap_pos": list(vp.tap_pos)},
                                model="world_map_nav.pan_to_port",
                                label=f"FOUND {vp.name} (attempt {attempt})",
                            )
                    except Exception as _exc:
                        logger.debug(f"[pan_to_port] trace FOUND record failed: {_exc}")
                    return vp.tap_pos

            if not visible:
                # No anchor points this frame.  Two strategies, in order:
                #
                #  1. Water-tap localization (ground truth, when affine
                #     calibrated).  Tap a water pixel near screen-centre,
                #     read its lat/lon, apply the persisted affine, get
                #     the catalogue coord of the tap, back-project to
                #     screen-centre.  Independent of starting-position
                #     assumptions and cumulative-swipe odometry.
                #
                #  2. Dead-reckoning fallback (existing path).  Assumes
                #     start = departing port, integrates swipe vectors.
                #     Wrong if the world map didn't open centred on the
                #     fleet (the 2026-05-21 Berber failure).
                #
                # Either path produces (camera_x, camera_y); the swipe
                # math below is shared.
                camera_x: Optional[float] = None
                camera_y: Optional[float] = None
                camera_source: str = "unknown"

                # Compute BOTH estimates, then reconcile.
                wt_x = wt_y = None       # water-tap
                dr_x = dr_y = None       # dead-reckon
                scale_ok = (_is_plausible_scale(cached_scale_x)
                            and _is_plausible_scale(cached_scale_y))

                if scale_ok:
                    try:
                        from actions.latlon_localize import (
                            localize_screen_center,
                            affine_available,
                        )
                        if affine_available():
                            loc = localize_screen_center(frame)
                            if loc is not None:
                                tap_px, tap_py = loc["tap_pixel"]
                                tap_cx, tap_cy = loc["catalogue"]
                                # Back-project: camera-centre catalogue =
                                # tap catalogue - (tap pixel - centre pixel) / scale.
                                wt_x = tap_cx - (tap_px - frame.width / 2) / cached_scale_x
                                wt_y = tap_cy - (tap_py - frame.height / 2) / cached_scale_y
                                logger.info(
                                    f"[pan_to_port] water-tap localize: "
                                    f"tap=({tap_px},{tap_py}) "
                                    f"latlon=({loc['latlon'][0]:.2f},{loc['latlon'][1]:.2f}) "
                                    f"→ camera≈({wt_x:.0f},{wt_y:.0f})"
                                )
                                # PAN-STEP RESULT (perceive→act→VERIFY): where did the last swipe
                                # actually land vs where we expected?
                                if cal_expected is not None:
                                    err_x = _wrap_dx(wt_x - cal_expected[0])
                                    err_y = wt_y - cal_expected[1]
                                    logger.info(
                                        f"[pan-step {pan_step} RESULT] expected camera→"
                                        f"({cal_expected[0]:.0f},{cal_expected[1]:.0f}), "
                                        f"REACHED ({wt_x:.0f},{wt_y:.0f}) — "
                                        f"error ({err_x:+.0f},{err_y:+.0f}) game-units"
                                    )
                                # CLOSED-LOOP SCALE UPDATE: compare the px we swiped since the
                                # previous fix to the observed camera movement.  swipe +px drags
                                # the camera -game, so scale = -swipe/Δcamera.  Blend 50/50 to
                                # damp single-fix misreads; persist so the next open starts right.
                                if cal_prev_wt is not None and cal_last_swipe is not None:
                                    moved_x = _wrap_dx(wt_x - cal_prev_wt[0])
                                    moved_y = wt_y - cal_prev_wt[1]
                                    sdx, sdy = cal_last_swipe
                                    updated = False
                                    if abs(sdx) > 400 and abs(moved_x) > 80:
                                        obs = -sdx / moved_x
                                        if _is_plausible_scale(obs):
                                            cached_scale_x = 0.5 * cached_scale_x + 0.5 * obs
                                            updated = True
                                    if abs(sdy) > 250 and abs(moved_y) > 60:
                                        obs_y = -sdy / moved_y
                                        if _is_plausible_scale(obs_y):
                                            cached_scale_y = 0.5 * cached_scale_y + 0.5 * obs_y
                                            updated = True
                                    if updated:
                                        logger.info(
                                            f"[pan_to_port] swipe-calibrated scale → "
                                            f"({cached_scale_x:.2f},{cached_scale_y:.2f}) "
                                            f"(swiped {cal_last_swipe}, camera moved "
                                            f"({moved_x:.0f},{moved_y:.0f}))"
                                        )
                                        try:
                                            _save_persisted_scale(cached_scale_x, cached_scale_y)
                                        except Exception:
                                            pass
                                        # Re-project this fix with the corrected scale.
                                        wt_x = tap_cx - (tap_px - frame.width / 2) / cached_scale_x
                                        wt_y = tap_cy - (tap_py - frame.height / 2) / cached_scale_y
                    except Exception as exc:
                        logger.debug(
                            f"[pan_to_port] water-tap localize raised: {exc}"
                        )

                if from_info is not None and scale_ok:
                    dr_x = from_info["x"] - total_swipe_dpx / cached_scale_x
                    dr_y = from_info["y"] - total_swipe_dpy / cached_scale_y

                # Reconcile.  Dead-reckon's premise — the world map opens
                # centred on the fleet — was live-verified 2026-08-13
                # (raw open-center 2508 vs Port Royal catalogue 2504).  So
                # right after a known stride, dead-reckon is trustworthy.
                # Water-tap is ground truth WHEN it reads cleanly, but in
                # port-less open ocean it misreads badly (2026-08-13
                # London→Port Royal: it read camera x=6709 when dead-reckon
                # said ~3680 — a bad reading that sent the next hops the wrong
                # way until the globe-wrap coincidentally recovered).  So:
                # prefer water-tap, but if it disagrees with dead-reckon by
                # more than one screen-width, treat it as an open-ocean
                # misread and use dead-reckon instead.
                if wt_x is not None and dr_x is not None:
                    disagree_px = max(
                        abs(wt_x - dr_x) * cached_scale_x,
                        abs(wt_y - dr_y) * cached_scale_y,
                    )
                    if disagree_px > frame.width:
                        camera_x, camera_y = dr_x, dr_y
                        camera_source = "dead-reckon (water-tap rejected)"
                        logger.warning(
                            f"[pan_to_port] water-tap≈({wt_x:.0f},{wt_y:.0f}) "
                            f"disagrees with dead-reckon≈({dr_x:.0f},{dr_y:.0f}) "
                            f"by {disagree_px:.0f}px (> 1 screen-width) — likely "
                            f"an open-ocean misread; using dead-reckon"
                        )
                    else:
                        camera_x, camera_y = wt_x, wt_y
                        camera_source = "water-tap"
                elif wt_x is not None:
                    camera_x, camera_y = wt_x, wt_y
                    camera_source = "water-tap"
                elif dr_x is not None:
                    camera_x, camera_y = dr_x, dr_y
                    camera_source = "dead-reckon"

                if camera_x is not None and camera_y is not None:
                    dgx = _wrap_dx(target_gx - camera_x)   # short way round the globe
                    dgy = target_gy - camera_y             # latitude does not wrap
                    dpx = dgx * cached_scale_x
                    dpy = dgy * cached_scale_y
                    max_dpx = _SWIPE_FRACTION_MAX * frame.width
                    max_dpy = _SWIPE_FRACTION_MAX * frame.height
                    dpx = max(-max_dpx, min(max_dpx, dpx))
                    dpy = max(-max_dpy, min(max_dpy, dpy))
                    dpx = _enforce_min_pan(dpx)
                    dpy = _enforce_min_pan(dpy)
                    last_delta = (dgx ** 2 + dgy ** 2) ** 0.5
                    logger.info(
                        f"[pan_to_port] no-anchor swipe ({camera_source}): "
                        f"camera≈({camera_x:.0f},{camera_y:.0f}) "
                        f"target=({target_gx},{target_gy}) "
                        f"Δgame=({dgx:.0f},{dgy:.0f}) "
                        f"scale=({cached_scale_x:.2f},{cached_scale_y:.2f}) "
                        f"→ swipe Δpix=({-dpx:.0f},{-dpy:.0f})"
                    )
                    swipe_dpx = -int(dpx)
                    swipe_dpy = -int(dpy)
                    # Stuck detector: same swipe + same (empty) visible set.
                    # Skip the detector when the camera source is water-tap
                    # — that's ground truth and a repeat means we're
                    # legitimately near the target; let the loop converge.
                    current_swipe = (swipe_dpx, swipe_dpy)
                    current_visible_keys: frozenset = frozenset()
                    if (camera_source != "water-tap"
                            and last_swipe == current_swipe
                            and last_visible_keys == current_visible_keys):
                        logger.warning(
                            "[pan_to_port] dead-reckon appears stuck — "
                            "same swipe with no anchors twice in a row. "
                            "Aborting so caller can fall back."
                        )
                        return None
                    last_swipe = current_swipe
                    last_visible_keys = current_visible_keys
                    try:
                        from actions import action_trace
                        if action_trace.active():
                            action_trace.record_decision(
                                inputs={"attempt": attempt, "visible_ports": 0,
                                        "camera_est": [round(camera_x), round(camera_y)],
                                        "camera_source": camera_source,
                                        "target": [target_gx, target_gy]},
                                output={"swipe_dpx": swipe_dpx, "swipe_dpy": swipe_dpy},
                                model="world_map_nav.pan_to_port",
                                label=f"pan attempt {attempt} (no anchors: {camera_source})",
                            )
                    except Exception as _exc:
                        logger.debug(f"[pan_to_port] trace no-anchor record failed: {_exc}")
                    actual_dpx, actual_dpy = self._swipe_pan(
                        swipe_dpx, swipe_dpy, frame.width, frame.height,
                    )
                    total_swipe_dpx += actual_dpx
                    total_swipe_dpy += actual_dpy
                    # Remember (fix, issued swipe) so the NEXT water-tap fix can calibrate the
                    # scale from observed movement.  Only pair with a CLEAN water-tap fix.
                    cal_prev_wt = ((wt_x, wt_y) if (camera_source == "water-tap"
                                                    and wt_x is not None) else None)
                    cal_last_swipe = (actual_dpx, actual_dpy)
                    # PAN-STEP log (user 2026-08-20): saw → decided → swiped → EXPECT.  The next
                    # fix logs "[pan-step N RESULT] expected vs REACHED" to close the loop.
                    pan_step += 1
                    exp_x = camera_x - actual_dpx / cached_scale_x
                    exp_y = camera_y - actual_dpy / cached_scale_y
                    cal_expected = (exp_x, exp_y)
                    logger.info(
                        f"[pan-step {pan_step}] saw camera=({camera_x:.0f},{camera_y:.0f}) "
                        f"[{camera_source}] target=({target_gx},{target_gy}) "
                        f"Δgame=({dgx:.0f},{dgy:.0f}) scale=({cached_scale_x:.2f},"
                        f"{cached_scale_y:.2f}) → swiped ({actual_dpx},{actual_dpy})px, "
                        f"EXPECT camera→({exp_x:.0f},{exp_y:.0f})"
                    )
                    time.sleep(_PAN_SETTLE_S)
                    continue
                # No way to estimate position — give up.  Be specific
                # about which precondition is missing so the next log
                # tells the operator exactly what to fix.
                missing = []
                if from_info is None:
                    missing.append(f"from_port (got {from_port!r})")
                if not _is_plausible_scale(cached_scale_x):
                    missing.append(f"plausible cached_scale_x (got {cached_scale_x})")
                if not _is_plausible_scale(cached_scale_y):
                    missing.append(f"plausible cached_scale_y (got {cached_scale_y})")
                missing.append("water-tap localize returned None")
                logger.warning(
                    f"[pan_to_port] no visible ports on attempt {attempt}; "
                    f"cannot localize — missing: {', '.join(missing)}; "
                    "giving up"
                )
                return None

            # Two independent scale signals for this frame:
            #   • fresh — pair-ratio calibration from ≥2 visible ports
            #   • odo   — departing-port + cumulative-swipe + any visible
            #             port (works with a single anchor; immune to a
            #             bad pair contaminating the fresh ratio)
            # Odometry is the tie-breaker because the cache-comparison
            # rule (used before this change) created a cache-poison loop:
            # once a bad value was on disk, every correct fresh got
            # rejected.  Odometry is independently derived from observed
            # screen positions + known catalogue + tracked swipe and
            # doesn't depend on cache at all.
            fresh_x, fresh_y = self._calibrate(visible)
            odo_x, odo_y = (None, None)
            if from_info is not None:
                odo_x, odo_y = self._odometry_scale(
                    from_info, total_swipe_dpx, total_swipe_dpy,
                    visible, frame.width, frame.height,
                )

            chosen_x, source_x = _pick_scale(
                fresh_x, odo_x, cached_scale_x, axis="x",
            )
            chosen_y, source_y = _pick_scale(
                fresh_y, odo_y, cached_scale_y, axis="y",
            )
            if source_x != "cache" or source_y != "cache":
                logger.info(
                    f"[pan_to_port] scale: x={chosen_x} (via {source_x}) "
                    f"y={chosen_y} (via {source_y}) — "
                    f"fresh=({fresh_x},{fresh_y}) odo=({odo_x},{odo_y}) "
                    f"cache=({cached_scale_x},{cached_scale_y})"
                )
            if chosen_x is not None:
                cached_scale_x = chosen_x
            if chosen_y is not None:
                cached_scale_y = chosen_y
            if chosen_x is not None and chosen_y is not None:
                _save_persisted_scale(chosen_x, chosen_y)
            eff_scale_x = cached_scale_x
            eff_scale_y = cached_scale_y

            # _estimate_center works on any number of visible ports as
            # long as scale is provided.  Single-port frames + cached
            # scale still produce a real centre estimate.
            center_gx, center_gy = self._estimate_center(
                visible, eff_scale_x, eff_scale_y, frame.width, frame.height,
            )

            # Below this line, treat the effective scale as authoritative.
            scale_x = eff_scale_x
            scale_y = eff_scale_y

            if scale_x is None or scale_y is None or center_gx is None \
                    or center_gy is None:
                # Not enough far-pair data for a proper scale.  Pan
                # blindly in the direction of the target as inferred
                # from the centroid of visible labels.
                cx_centroid = statistics.fmean(vp.game_x for vp in visible)
                cy_centroid = statistics.fmean(vp.game_y for vp in visible)
                dgx_sign = 1 if _wrap_dx(target_gx - cx_centroid) > 0 else -1
                dgy_sign = 1 if target_gy > cy_centroid else -1
                dpx = dgx_sign * frame.width  * _BLIND_PAN_FRACTION
                dpy = dgy_sign * frame.height * _BLIND_PAN_FRACTION
                logger.info(
                    f"[pan_to_port] blind pan (scale unknown): "
                    f"target=({target_gx},{target_gy}) "
                    f"centroid=({cx_centroid:.0f},{cy_centroid:.0f}) "
                    f"swipe Δpix=({-dpx:.0f},{-dpy:.0f})"
                )
            else:
                dgx = _wrap_dx(target_gx - center_gx)   # short way round the globe
                dgy = target_gy - center_gy             # latitude does not wrap
                dpx = dgx * scale_x
                dpy = dgy * scale_y
                max_dpx = _SWIPE_FRACTION_MAX * frame.width
                max_dpy = _SWIPE_FRACTION_MAX * frame.height
                dpx = max(-max_dpx, min(max_dpx, dpx))
                dpy = max(-max_dpy, min(max_dpy, dpy))
                logger.info(
                    f"[pan_to_port] target=({target_gx},{target_gy}) "
                    f"center≈({center_gx:.0f},{center_gy:.0f}) "
                    f"Δgame=({dgx:.0f},{dgy:.0f}) "
                    f"scale=({scale_x:.2f},{scale_y:.2f}) "
                    f"→ swipe Δpix=({-dpx:.0f},{-dpy:.0f})"
                )

            # Per-axis minimum: a non-zero but tiny Δ gets ignored by the
            # game as a tap.  Below the floor, scale up to the minimum —
            # preserves direction, ensures the map actually moves.
            dpx = _enforce_min_pan(dpx)
            dpy = _enforce_min_pan(dpy)

            # Stuck detection: if the same swipe is being issued against
            # the same visible-port set as last attempt, the map didn't
            # move — either we hit a panning boundary or the game ignored
            # the gesture.  Don't burn the rest of the budget on the
            # same wrong vector; fail fast so the caller can fall back.
            current_swipe = (int(dpx), int(dpy))
            current_visible_keys = frozenset(v.key for v in visible)
            if (last_swipe == current_swipe
                    and last_visible_keys == current_visible_keys):
                logger.warning(
                    f"[pan_to_port] map appears stuck — same swipe "
                    f"({current_swipe[0]},{current_swipe[1]}) and same "
                    f"visible set {sorted(current_visible_keys)} as previous "
                    f"attempt.  Aborting so caller can fall back."
                )
                return None
            last_swipe = current_swipe
            last_visible_keys = current_visible_keys

            # Capture the pan decision for frame-by-frame diagnosis in the
            # trace viewer (only when a trace session is active — no cost
            # otherwise).  Records the world-map frame + the numbers behind
            # this swipe so overshoot / oscillation is visible with context.
            try:
                from actions import action_trace
                if action_trace.active():
                    _c = ([round(center_gx), round(center_gy)]
                          if center_gx is not None and center_gy is not None
                          else None)
                    action_trace.record_decision(
                        inputs={
                            "attempt": attempt,
                            "visible_ports": len(visible),
                            "center_est": _c,
                            "target": [target_gx, target_gy],
                            "scale_chosen": [round(scale_x, 3) if scale_x else None,
                                             round(scale_y, 3) if scale_y else None],
                            "scale_source": [source_x, source_y],
                            "fresh": [round(fresh_x, 3) if fresh_x else None,
                                      round(fresh_y, 3) if fresh_y else None],
                            "odo": [round(odo_x, 3) if odo_x else None,
                                    round(odo_y, 3) if odo_y else None],
                        },
                        output={"swipe_dpx": -int(dpx), "swipe_dpy": -int(dpy)},
                        model="world_map_nav.pan_to_port",
                        label=f"pan attempt {attempt}",
                    )
            except Exception as _exc:
                logger.debug(f"[pan_to_port] trace record failed: {_exc}")

            # Swipe direction is OPPOSITE the desired map-content motion:
            # to move the camera RIGHT (to see ports east of us), we drag
            # the map content LEFT, i.e. swipe from right to left.
            swipe_dpx = -int(dpx)
            swipe_dpy = -int(dpy)
            actual_dpx, actual_dpy = self._swipe_pan(
                swipe_dpx, swipe_dpy, frame.width, frame.height,
            )
            # Track ACTUAL swipe magnitude — may differ from requested if
            # the safe-rect clamp shortened the stroke.  Honest odometry.
            total_swipe_dpx += actual_dpx
            total_swipe_dpy += actual_dpy
            # Anchored-path swipe breaks the water-tap (fix, swipe) pairing — invalidate it so
            # the closed-loop calibrator never ratios across an unrecorded swipe.
            cal_prev_wt = None
            cal_last_swipe = None
            time.sleep(_PAN_SETTLE_S)

        logger.warning(
            f"[pan_to_port] gave up after {max_pans} pan(s) without "
            f"finding {name!r}"
        )
        return None

    # ── Internals ─────────────────────────────────────────────────────────────

    def _recenter_on_fleet(self, frame) -> bool:
        """Tap the world map's My Location control to bring the camera back to the fleet.

        The fleet's own position is always explored, so this lands the camera somewhere
        labels actually render — a known-good starting point after a pan has wandered into
        fog. Found by LABEL, never by remembered coordinates."""
        from actions.sail_actions import _find_button
        from actions.adb_actions import tap
        pos = _find_button(frame, "my location", "location")
        if pos is None:
            logger.warning("[pan_to_port] My Location control not found — cannot recentre")
            return False
        logger.info(f"[pan_to_port] recentring on the fleet via My Location @ {pos}")
        tap(*pos)
        time.sleep(random.uniform(1.8, 2.6))
        return True

    _fold = staticmethod(fold_name)

    def _lookup_port(self, name: str) -> Optional[dict]:
        """Catalogue lookup that respects port aliases (Lisbon → Lisboa, etc.) and is
        accent-insensitive (see `_fold`).

        Falls back to the global port catalogue if the lookup fails in
        this navigator's catalogue.  This matters for the village
        navigator: the *from*-port passed in (e.g. "Tripoli" when
        sailing to Berber Village) is always a real port, not a
        village, so we must look it up in the port catalogue even when
        self._ports holds villages.  Without this fallback, stride and
        odometry can't anchor on the departing port and the navigator
        gives up on attempt 1 when no villages are in the initial view.
        """
        key = name.lower().strip()
        if not key:
            return None
        folded = self._fold(name)

        # 1. Local catalogue (the one we're navigating over).
        info = self._ports.get(key)
        if info is not None:
            return info
        for variant in self._aliases.get(key, []):
            v = variant.lower().strip()
            info = self._ports.get(v)
            if info is not None:
                return info
        # Accent-folded sweep — 'Male' must find the catalogue's 'Malé'.
        for k, v in self._ports.items():
            if self._fold(k) == folded:
                logger.info(f"[_lookup_port] {name!r} matched catalogue entry {k!r} "
                            "by accent-folded name")
                return v

        # 2. Global port catalogue fallback — needed when *self* is a
        #    village navigator and *name* is the departing port.
        try:
            from vision.world_map_parser import load_port_catalogue
            ports = load_port_catalogue()
            if ports is self._ports:
                return None    # we're already a port nav, no fallback to do
            info = ports.get(key)
            if info is not None:
                return info
            for k, v in ports.items():
                if self._fold(k) == folded:
                    logger.info(f"[_lookup_port] {name!r} matched port catalogue entry "
                                f"{k!r} by accent-folded name")
                    return v
            # Port-alias lookup against the global aliases table.
            try:
                from actions.sail_actions import _PORT_ALIASES
            except Exception:
                _PORT_ALIASES = {}
            for variant in _PORT_ALIASES.get(key, []):
                v = variant.lower().strip()
                info = ports.get(v)
                if info is not None:
                    return info
        except Exception as exc:
            logger.debug(
                f"[_lookup_port] port-catalogue fallback failed: {exc}"
            )
        return None

    def _stride_from(
        self,
        from_info: dict,
        target_gx: int, target_gy: int,
        scale_x: float, scale_y: float,
        *,
        max_pans: int,
    ) -> Tuple[int, int, int]:
        """Issue back-to-back pans covering most of the catalogue distance
        from the departing port to the target, without perception between
        them.

        Returns (n_stride, cum_swipe_dpx, cum_swipe_dpy) — number of pans
        actually issued and the total commanded swipe vector summed across
        them (in the same sign convention as `_swipe_pan`'s args, so the
        caller can accumulate into total odometry).

        Reserves at least 2 pans of *max_pans* budget for the iterative
        recalibration loop afterwards — never strides through the whole
        budget, since dead reckoning compounds error.
        """
        from capture.adb_capture import capture_screen

        # One capture just to get screen dims and to confirm we're on a
        # world map.  (The caller already verified, but this is cheap.)
        frame = capture_screen()
        dgx = target_gx - from_info["x"]
        dgy = target_gy - from_info["y"]
        total_dpx = dgx * scale_x
        total_dpy = dgy * scale_y

        max_dpx = _SWIPE_FRACTION_MAX * frame.width
        max_dpy = _SWIPE_FRACTION_MAX * frame.height
        n_needed = max(
            math.ceil(abs(total_dpx) / max_dpx) if max_dpx else 0,
            math.ceil(abs(total_dpy) / max_dpy) if max_dpy else 0,
        )
        # Reserve ≥ 2 iterative pans for recalibration + fine-tune.
        max_stride = max(0, max_pans - 2)
        # Cover roughly HALF the distance in stride; leave the rest for
        # the iterative loop so perception can correct any error before
        # we commit further blind pans.  Earlier rule "n_needed - 1"
        # was too aggressive — 2026-05-20 live run showed stride pan 1
        # already reaching Europe (Lisbon visible) but stride pan 2
        # overshooting into an empty Atlantic region.  Half-power stride
        # gets ~50% of the way in one fast hop, then iterative perception
        # catches any drift.
        n_stride = min(n_needed // 2, max_stride) if n_needed >= 2 else 0
        if n_stride <= 0:
            logger.info(
                f"[pan_to_port] stride skipped — total Δpix=({total_dpx:.0f},"
                f"{total_dpy:.0f}) fits in ≤1 pan or no headroom"
            )
            return 0, 0, 0

        # Distribute the stride evenly; each pan covers an equal share of
        # the total distance, capped at max per-pan.
        per_dpx = total_dpx / n_stride
        per_dpy = total_dpy / n_stride
        # Clamp per-axis at max per-pan magnitude.
        per_dpx = max(-max_dpx, min(max_dpx, per_dpx))
        per_dpy = max(-max_dpy, min(max_dpy, per_dpy))
        # Per-axis minimum so each pan registers as a drag, not a tap.
        per_dpx = _enforce_min_pan(per_dpx)
        per_dpy = _enforce_min_pan(per_dpy)

        logger.info(
            f"[pan_to_port] stride: from=({from_info['x']},{from_info['y']}) "
            f"→ target=({target_gx},{target_gy}) "
            f"Δgame=({dgx:.0f},{dgy:.0f}) scale=({scale_x:.2f},{scale_y:.2f}) "
            f"plan={n_stride} pan(s) of Δpix=({-per_dpx:.0f},{-per_dpy:.0f}) each"
        )

        # Swipe direction is OPPOSITE the desired map-content motion
        # (see iterative-loop comment for the same convention).
        swipe_dpx = -int(per_dpx)
        swipe_dpy = -int(per_dpy)
        total_actual_dpx = 0
        total_actual_dpy = 0
        for i in range(n_stride):
            actual_dpx, actual_dpy = self._swipe_pan(
                swipe_dpx, swipe_dpy, frame.width, frame.height,
            )
            total_actual_dpx += actual_dpx
            total_actual_dpy += actual_dpy
            time.sleep(_STRIDE_SETTLE_S)
        return n_stride, total_actual_dpx, total_actual_dpy

    def _odometry_scale(
        self,
        from_info: dict,
        total_swipe_dpx: int, total_swipe_dpy: int,
        visible: list[VisiblePort],
        frame_w: int, frame_h: int,
    ) -> Tuple[Optional[float], Optional[float]]:
        """Derive (scale_x, scale_y) from the departing port catalogue
        position + cumulative commanded swipe + any visible port's screen
        position.  Independent of pair-ratio calibration.

        Assumption: the world map opened centred on the fleet, and the
        fleet was at the departing port (catalogue *from_info*) at that
        moment.  Holds when pan_to_port is called immediately after
        departure (the current production use case).

        Derivation: with camera_now = L − cum_swipe/scale and
        projection px_P = screen_centre + (gx_P − camera_now) × scale,
        substitution yields
            scale_x = (px_P − screen_centre_x − S_x) / (gx_P − L_x)
        and likewise for y.  Median across visible ports for robustness;
        skip the departing port itself (denominator zero) and pairs
        whose catalogue distance is below _MIN_PAIR_GAME_DIST (denominator
        too small → ratio noise blows up).
        """
        Lx, Ly = from_info["x"], from_info["y"]
        cx_screen = frame_w / 2
        cy_screen = frame_h / 2

        sxs: list[float] = []
        sys: list[float] = []
        for vp in visible:
            dgx = vp.game_x - Lx
            dgy = vp.game_y - Ly
            if abs(dgx) >= _MIN_PAIR_GAME_DIST:
                sxs.append((vp.pix_cx - cx_screen - total_swipe_dpx) / dgx)
            if abs(dgy) >= _MIN_PAIR_GAME_DIST:
                sys.append((vp.pix_cy - cy_screen - total_swipe_dpy) / dgy)

        return (
            statistics.median(sxs) if sxs else None,
            statistics.median(sys) if sys else None,
        )

    def _calibrate(
        self, visible: list[VisiblePort],
    ) -> Tuple[Optional[float], Optional[float]]:
        """Compute (scale_x, scale_y) in pixels per game-coord-unit.

        Median of all same-axis pair ratios where the game-coord delta is
        ≥ _MIN_PAIR_GAME_DIST.  Median (not mean) is robust to the noisy
        close-pair ratios that elevate CV on tightly-clustered views
        (Frame 2's NW Europe cluster went from CV 24% with mean to ~5%
        with median + far-pair filter)."""
        if len(visible) < 2:
            return None, None
        rx, ry = [], []
        for a, b in combinations(visible, 2):
            dgx = b.game_x - a.game_x
            dgy = b.game_y - a.game_y
            dpx = b.pix_cx - a.pix_cx
            dpy = b.pix_cy - a.pix_cy
            if abs(dgx) >= _MIN_PAIR_GAME_DIST:
                rx.append(dpx / dgx)
            if abs(dgy) >= _MIN_PAIR_GAME_DIST:
                ry.append(dpy / dgy)
        return (
            statistics.median(rx) if rx else None,
            statistics.median(ry) if ry else None,
        )

    def _estimate_center(
        self,
        visible:   list[VisiblePort],
        scale_x:   Optional[float],
        scale_y:   Optional[float],
        frame_w:   int,
        frame_h:   int,
    ) -> Tuple[Optional[float], Optional[float]]:
        """Back-project: what game-coord is at the centre of the current
        view?  For each visible port:
            game_at_screen_center = port.game + (screen_center_px - port.pix) / scale

        Median across visible ports — robust to one anomalous label
        position.  Returns (None, None) when scale is unavailable on
        that axis (caller falls back to blind pan)."""
        if not visible:
            return None, None
        cx_target = frame_w / 2
        cy_target = frame_h / 2
        gxs, gys = [], []
        for vp in visible:
            if scale_x:
                gxs.append(vp.game_x + (cx_target - vp.pix_cx) / scale_x)
            if scale_y:
                gys.append(vp.game_y + (cy_target - vp.pix_cy) / scale_y)
        return (
            statistics.median(gxs) if gxs else None,
            statistics.median(gys) if gys else None,
        )

    def _swipe_pan(
        self, dpx: int, dpy: int, frame_w: int, frame_h: int,
    ) -> Tuple[int, int]:
        """Issue a slow swipe by (dpx, dpy), anchored inside the map safe
        rect so the gesture never touches the toolbar zones.

        Returns the *actual* (dpx, dpy) achieved after clamping to the
        safe rect.  Caller must use this return value (not the request)
        for dead-reckoning, otherwise cumulative odometry drifts.

        Background — 2026-05-20 failure: an earlier fix anchored at the
        screen corner opposite the motion direction.  That keeps the
        stroke on-screen but for some target directions the start lands
        in the top toolbar zone (mode tabs, Search, World Map title).
        UWO interprets the touch-down as a tab click and discards the
        drag — the map doesn't move.  The Caribbean → London pan
        repeatedly emitted identical swipes from (2320, 80), all
        consumed by the toolbar.

        Fix: anchor inside _MAP_SAFE_RECT (which excludes top/bottom
        toolbars and edge icon columns).  Start at the side of the rect
        opposite the motion direction; end gets clamped if requested Δ
        exceeds rect dimensions.  Returns the actual swipe vector so
        callers can track real progress.

        Slow swipe avoids the game treating it as a fling.
        """
        from actions.adb_actions import swipe

        sl, st, sr, sb = (
            _MAP_SAFE_LEFT, _MAP_SAFE_TOP,
            _MAP_SAFE_RIGHT, _MAP_SAFE_BOTTOM,
        )
        # Anchor at the side of the safe rect opposite the motion.
        start_x = sl if dpx >= 0 else sr
        start_y = st if dpy >= 0 else sb
        # End point — clamp inside the safe rect (may shorten the stroke).
        end_x = max(sl, min(sr, start_x + dpx))
        end_y = max(st, min(sb, start_y + dpy))
        actual_dpx = end_x - start_x
        actual_dpy = end_y - start_y
        swipe(start_x, start_y, end_x, end_y,
              duration_ms=_SWIPE_DURATION_MS)
        return actual_dpx, actual_dpy


# ── Factories ────────────────────────────────────────────────────────────────

def make_village_navigator() -> WorldMapNavigator:
    """Construct a WorldMapNavigator over the village catalogue.

    Uses the same pan / odometry / safe-rect machinery as the port
    navigator — only the catalogue differs.  Aliases are empty
    (villages don't have local-language alternates in our data).

    Callers MUST ensure the world map is on the Explore tab before
    invoking pan_to_port on this navigator (village labels are
    suppressed on the Port tab).

    The baked catalogue is keyed by voyage.tw SHORT keys ('melanesian'),
    but pan_to_port looks up (and OCR label-matching matches) the DISPLAY
    name lowercased ('melanesian village') — re-key by display name, else
    every village lookup misses (live 2026-08-20: 'Melanesian Village'
    "not in port catalogue" → world-map open/close loop)."""
    from vision.world_map_parser import load_village_catalogue
    cat = {}
    for k, v in load_village_catalogue().items():
        cat[k.lower().strip()] = v                          # short key ('berber')
        cat[(v.get("name") or k).lower().strip()] = v       # display name ('berber village')
    return WorldMapNavigator(catalogue=cat, aliases={})
