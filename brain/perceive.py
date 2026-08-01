# brain/perceive.py
#
# Three-pass perception layer.
#
# Pass 1 — Interruptors
#   Detect and dismiss overlays that fire on top of any state.
#   Re-capture after each dismissal.  Process one at a time (stacking handled
#   by looping until no interruptors remain).
#
# Pass 2 — Active flow
#   Detect whether the bot is locked inside an atomic flow (negotiation,
#   purchase result, departure confirmation, etc.).
#   If inside a flow, normal navigation recovery does not apply.
#
# Pass 3 — Navigation state
#   Identify the current navigation state using chrome detection + OCR.
#   Delegates to the existing where_am_i() logic for now; the registry
#   provides the graph structure.
#
# Returns a PerceiveResult with the full picture.
# where_am_i() is kept as a backward-compatible wrapper.

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from brain.kb import RECOVERY_TAP_OK_OR_X
from capture.adb_capture import capture_screen

# Eager-load fingerprint registry at perceive-module import.  Without
# this, the 39 learned fingerprints under memory/knowledge/
# learned_fingerprints/ are first loaded the moment classify_screen()
# falls through to the OmniParser cascade — typically several ticks
# into a run, when the bot first enters a building.  Observed live:
# the load stalled a sail_to tick by ~22 s mid-flow.  Importing the
# data module here moves that cost into bot-boot (alongside the
# OmniParser YOLO warm-up), where the user is already waiting.
try:
    import vision.state_fingerprints_data  # noqa: F401
except Exception as _fp_exc:  # pragma: no cover — must not block perceive
    logger.debug(f"[perceive] eager-load of fingerprint registry failed: {_fp_exc}")

# ── Confidence level identifiers ──────────────────────────────────────────────
# "high" → local perception is reliable; "low" → triggers Claude re-classification.
# Import these constants wherever confidence is set or compared.
CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW  = "low"


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class PerceiveResult:
    state:        str                  # navigation state id
    port:         Optional[str]        # port name if readable
    detail:       str                  # human-readable context
    flow:         Optional[str] = None # active flow id, if any
    flow_step:    Optional[str] = None # current step within flow
    sub_menu:     Optional[str] = None # active sub-menu id within current building (from L2.5)
    interruptors: list[str] = field(default_factory=list)  # cleared before returning
    confidence:   str = CONFIDENCE_HIGH  # CONFIDENCE_HIGH | CONFIDENCE_LOW — low triggers Claude re-classification
    corrected:    bool = False         # True if Claude corrected this result
    scene_type:   Optional[str] = None # Qwen's domain-aware scene tag (village/harbor/market/…); None when Qwen not consulted or unsure
    task_complete: Optional[bool] = None  # Qwen's task-completion verdict when GoalContext was active; None otherwise. Goal layer uses as a SECONDARY arrival signal, not the sole one.

    def to_location_dict(self) -> dict:
        """Backward-compatible format matching the old where_am_i() return value."""
        return {
            "location": self.state,
            "port":     self.port,
            "detail":   self.detail,
        }

    @property
    def at_port(self) -> bool:
        return self.state == "port_overworld"

    @property
    def in_flow(self) -> bool:
        return self.flow is not None


# ── Pass 1: Interruptor detection & dismissal ─────────────────────────────────
# Detection patterns are loaded from interruptors.json via brain.kb.
# _INTERRUPTOR_PATTERNS and _MAIN_MENU_KEYWORDS are kept as lazy fallbacks only.


def _is_top_level_screen(frame) -> bool:
    """True if the current frame shows a screen where pressing system
    Back would exit the current location entirely — village, port
    overworld, sea, or world map.

    Used by the consult-recommendation gate to refuse press_back as a
    popup dismissal on these screens: a wrongly-dismissed popup is
    cheap to retry, but exiting a village/port/sea is expensive
    (re-navigation, possibly losing flow state).

    Heuristic:
      • village — back arrow + top-left text matches the village catalogue
      • port_overworld — top-left text matches the port catalogue AND
        no back arrow (port_overworld is the only screen with a port
        name and no back arrow)

    No need to detect sea / world_map here — the obstruction system
    rarely fires on those screens anyway, and the cost of a refused
    press_back is only that we try tap_close_x instead.
    """
    try:
        from vision.ocr import read_port_name
        from vision.text_correction import correct_village_name, correct_port_name
        from vision.chrome_detector import get_chrome_detector
        port_text = read_port_name(frame)
        if not port_text:
            return False
        # Village interior: the top-left name fuzzy-matches a known village.
        if correct_village_name(port_text)[0] is not None:
            return True
        # Port overworld: top-left name fuzzy-matches a known port AND
        # there's no back arrow.  Back arrow + port name would be a
        # building or sub_menu inside the port — those are nested,
        # back-press is safe.
        chrome = get_chrome_detector().detect(frame)
        if not chrome.has_back_arrow and correct_port_name(port_text)[0] is not None:
            return True
    except Exception as exc:
        logger.debug(f"[perceive] _is_top_level_screen guard failed: {exc}")
    return False


# Session-level suppression for the daily_news Moondream confirm.
#
# Daily news fires at most once per game-day, on the first overworld
# transition.  Once Moondream has confirmed NO on the round-X-glyph
# pixel signature, the signature keeps firing on every sea frame (the
# right-side HUD has a similar close-X geometry).  Each false alarm
# costs ~12-13 s of Moondream inference.  Cache the NO verdict
# sessionwide with a 1-hour TTL — long enough to suppress every false
# alarm during a typical session, short enough that a session
# straddling the daily reset still catches the real popup.
_DAILY_NEWS_NO_SUPPRESS_TTL_S: float = 3600.0
_daily_news_no_suppress_until: float = 0.0


def _has_daily_news_close_x(frame) -> bool:
    """
    Detect the daily_news popup via a two-stage check on the round-X close
    icon at the known position (1794, 240).

    Stage 1 — fast pixel signature.  A 50×50 crop around (1794, 240)
    contains both very-dark pixels (dark disc + X glyph) and bright
    pixels (white ring).  Empirically:
        no popup     : dark<50 = 0,    bright>200 = 0
        popup present: dark<50 = 671,  bright>200 = 232
    This filter is cheap (a few ms) but ambiguous: other UI elements at
    the same coordinates (e.g. the close button on the harbor's
    Recruit-Crew sub-screen) can also produce a dark+bright pattern.
    Observed in the May-1 15:11 run — the bot looped tapping (1794, 240)
    on a non-daily-news screen because pixel match alone reported a
    false positive.

    Stage 2 — Context guard (cheap chrome check).  The user-confirmed
    invariant for daily_news: it ONLY shows on overworld screens (sea
    or port_overworld), never inside a building, sub-menu, port map,
    or any in-game dialog.  So if chrome detection reports has_home or
    has_back_arrow we're inside something — daily_news cannot fire and
    we can short-circuit before the expensive Moondream call.  This
    guard alone would have caught the May-2 Purchase-modal incident
    (Purchase is a sub-menu of the market building → has_back_arrow
    True → daily_news rejected without any model call).

    Stage 3 — Moondream confirmation on the X-outside-dialog visual.
    Daily_news has a unique close-button geometry: the round X is
    positioned OUTSIDE the popup window, hanging just above its
    top-right corner.  Every other in-game dialog (Purchase, recruit,
    confirm) places its close X INSIDE the dialog frame.  This is the
    visual cue Moondream is asked to verify.

    Returns True only when all three stages agree.  When Moondream is
    unavailable, falls back to stage-1 + stage-2 (preserves prior
    behaviour for environments without the local vision model).

    Text-based detection is intentionally NOT relied upon for daily_news:
    the popup has two display states (article-list view and deep-linked
    article view with no title text at all) and 'Uncharted Waters Origin'
    only appears in some article bodies, not on the list page.
    """
    import numpy as np
    crop = frame.crop((1770, 215, 1820, 265))
    arr  = np.array(crop.convert("L"))
    pixel_match = (arr < 50).sum() > 100 and (arr > 200).sum() > 50
    if not pixel_match:
        return False

    # Stage 2: context guard — daily_news cannot fire inside a building
    # or sub-menu.  Use chrome detection (template match, ~100ms) to
    # check.  has_home or has_back_arrow being True means we're inside
    # something — short-circuit before paying the Moondream cost.
    try:
        from vision.chrome_detector import get_chrome_detector
        chrome = get_chrome_detector().detect(frame)
        if chrome.has_home or chrome.has_back_arrow:
            logger.info(
                f"[perceive] daily_news pixel signature fired BUT chrome "
                f"shows we're inside a screen (has_home={chrome.has_home}, "
                f"has_back_arrow={chrome.has_back_arrow}) — daily_news "
                f"only fires on overworld, rejecting"
            )
            return False
    except Exception as e:
        # If chrome detection breaks, fall through to Moondream — don't
        # let a chrome failure mask a real daily_news.
        logger.debug(f"[perceive] daily_news context guard skipped: {e}")

    # Stage 2.5: session-level suppression.  If Moondream already
    # confirmed NO once in this session within the TTL, skip the next
    # call.  The pixel signature keeps firing on sea frames (the HUD
    # has a similar close-X) — without this gate, every false alarm
    # pays the ~12 s Moondream cost.
    global _daily_news_no_suppress_until
    if time.time() < _daily_news_no_suppress_until:
        logger.debug(
            "[perceive] daily_news pixel signature fired but session-level "
            "NO is still in effect — skipping Moondream confirm"
        )
        return False

    # Stage 3: confirm with Moondream on the X-outside-dialog visual.
    # Cached per (id(frame), "daily_news_close_x") so repeated calls on
    # the same frame share inference cost.
    try:
        from vision.moondream_cache import ask_cached
        result = ask_cached(
            frame, "daily_news_close_x",
            lambda: _has_daily_news_close_x_moondream_inference(frame),
        )
        if not result:
            # NO verdict — suppress further confirms for the TTL window.
            _daily_news_no_suppress_until = time.time() + _DAILY_NEWS_NO_SUPPRESS_TTL_S
            logger.info(
                f"[perceive] daily_news confirmed NO — suppressing further "
                f"Moondream confirms for {_DAILY_NEWS_NO_SUPPRESS_TTL_S/60:.0f} min"
            )
        return result
    except Exception as e:
        logger.debug(f"[perceive] daily_news Moondream confirm skipped: {e}")
        return False


def _has_daily_news_close_x_moondream_inference(frame) -> bool:
    """Stage-3 Moondream inference for daily_news detection.  Wrapped by
    the cached entry point in _has_daily_news_close_x so callers that
    repeat the check on the same frame share work."""
    from vision.local_vision import get_vision
    vision = get_vision()
    if not vision.check_available():
        # No model — fall back to stages 1+2 alone.  Logged at debug
        # so a silent miss is recoverable from logs but doesn't spam.
        logger.debug(
            "[perceive] daily_news pixel match fired but Moondream "
            "unavailable — accepting on pixel signature + context guard"
        )
        return True
    thumb = frame.copy()
    thumb.thumbnail((800, 400))
    # Visual-discriminator prompt: daily_news has a uniquely-placed
    # close button — round X OUTSIDE the popup, hanging above its
    # top-right corner.  Every other in-game dialog has its X
    # INSIDE the dialog frame, so this question rejects them.
    answer = vision.ask(
        "Look at the popup window on this screen.  Is its round X "
        "close button positioned OUTSIDE the popup, hanging just "
        "above the popup's top-right corner (NOT inside the popup "
        "frame)?  If the close X is inside the popup's border, or "
        "if there is no popup at all, answer 'no'.  Answer ONLY "
        "'yes' or 'no'.",
        frame=thumb,
    )
    if not answer:
        # Indeterminate model output: be conservative — reject
        # rather than dismiss something that might be a live game
        # screen.  False-positive cost (dismissing real screens)
        # outweighs false-negative cost (delaying daily_news one
        # tick).
        logger.info(
            "[perceive] daily_news pixel signature fired → Moondream "
            "returned no answer; rejecting to avoid false dismissal"
        )
        return False
    confirmed = answer.lower().strip().startswith("y")
    logger.info(
        f"[perceive] daily_news pixel signature fired → Moondream confirm: "
        f"{'YES' if confirmed else 'NO (false positive rejected)'}  "
        f"raw={answer[:40]!r}"
    )
    return confirmed


# ── No-obstruction cache ─────────────────────────────────────────────────────
# Cheap perceptual-hash cache that short-circuits _detect_interruptors when
# a structurally-similar frame was just confirmed as obstruction-free.
# parse_screen() and classify_obstruction() add up to ~2 s; this cache costs
# ~5 ms on hit.  TTL is short because game state can change quickly; the
# cache is only consulted when we'd otherwise pay the full ~2 s.
#
# Origin: 2026-05-22 perf analysis — _detect_interruptors runs on every
# perceive cycle, including back-to-back ticks on the same stable screen
# (e.g. sailing).  Most ticks reach the same "no obstruction" verdict.
_OBSTRUCTION_NONE_CACHE: dict[bytes, float] = {}
_OBSTRUCTION_NONE_TTL_S: float = 5.0
_OBSTRUCTION_NONE_CACHE_MAX: int = 32


def _extract_minimap_text_bboxes(
    frame, minimap_crop: tuple[int, int, int, int]
) -> list[tuple[int, int, int, int]]:
    """§13.19 — Pull text-element bboxes from OmniParser that overlap
    the mini-map crop, translated to crop-local coordinates.

    Used by the navigation view's `text_close` heading pre-pass to
    bridge gaps that label glyphs (port/village names) cut through the
    ship hull when the ship icon sits underneath them.  Reuses the
    per-frame parse_fast cache so this adds ~0 ms when OmniParser has
    already run; returns [] gracefully on any error so the existing
    pipeline still works.

    See `docs/heading_pca_yellow_anchor.md`.
    """
    try:
        from vision.omniparser import parse_fast_cached
        elements = parse_fast_cached(frame)
    except Exception:
        return []
    l, t, r, b = minimap_crop
    out: list[tuple[int, int, int, int]] = []
    for el in elements:
        if el.element_type != "text":
            continue
        # Intersect element bbox with the mini-map crop.
        ex0 = max(el.x1, l); ey0 = max(el.y1, t)
        ex1 = min(el.x2, r); ey1 = min(el.y2, b)
        if ex1 <= ex0 or ey1 <= ey0:
            continue
        # Translate to mini-map-local coordinates.
        out.append((ex0 - l, ey0 - t, ex1 - l, ey1 - t))
    return out


def _frame_perceptual_signature(frame) -> bytes:
    """8×8 grayscale-thumbnail hash.  Frames with the same hash are
    visually near-identical for our purposes (chrome, overlays,
    structure preserved; small animation differences erased)."""
    import hashlib
    # PIL.Image.BILINEAR == 2; use the int directly to avoid a
    # module-level PIL import for this one constant.
    thumb = frame.resize((8, 8), 2).convert("L")
    return hashlib.md5(thumb.tobytes()).digest()


def _no_obstruction_cache_check(frame) -> bool:
    """Return True iff we recently confirmed this frame's signature as
    obstruction-free, within the TTL."""
    sig = _frame_perceptual_signature(frame)
    ts = _OBSTRUCTION_NONE_CACHE.get(sig)
    if ts is None:
        return False
    if time.time() - ts > _OBSTRUCTION_NONE_TTL_S:
        _OBSTRUCTION_NONE_CACHE.pop(sig, None)
        return False
    return True


def _no_obstruction_cache_store(frame) -> None:
    """Remember this frame's signature as obstruction-free.  Bounded to
    avoid unbounded growth in long-running sessions."""
    sig = _frame_perceptual_signature(frame)
    _OBSTRUCTION_NONE_CACHE[sig] = time.time()
    if len(_OBSTRUCTION_NONE_CACHE) > _OBSTRUCTION_NONE_CACHE_MAX:
        # Cheap pruning: drop the oldest half.  We don't need LRU
        # accuracy; we just want to bound memory.
        cutoff = sorted(_OBSTRUCTION_NONE_CACHE.values())[
            _OBSTRUCTION_NONE_CACHE_MAX // 2
        ]
        for k, v in list(_OBSTRUCTION_NONE_CACHE.items()):
            if v < cutoff:
                _OBSTRUCTION_NONE_CACHE.pop(k, None)


def _detect_interruptors(frame, ocr_tokens: list):
    """
    Return list of interruptor ids that are currently active.

    Phase A2 (2026-05-15): the obstruction classifier runs first.  When
    it reports `kind=none`, the keyword loop is skipped entirely — no
    matter how broad an interruptor's keywords are, they can't fire on
    a clean screen.  This is what stops `union_request`, `perk_event`,
    `daily_login_reward` etc. from looping every tick on building main
    views that happen to contain their substrings.

    When the classifier reports an obstruction, the keyword text is
    restricted to OCR tokens INSIDE the obstruction bbox before
    matching.  Legacy keyword-only entries automatically benefit from
    this restriction without needing per-entry migration.

    Per-interruptor rules layered on top (from interruptors.json):
      - `detection_keywords` — every keyword must appear (case-
        insensitive substring) in the *bbox-restricted* token text.
      - `structural.text_zone` — when present, OVERRIDES the
        obstruction bbox with a tighter normalised zone (legacy
        Fix 11 entries keep working with their explicit zones).
      - `structural.requires_vertical_pair` — keyword-zone tokens
        must form a vertical pair (NPC name above, body below).

    Special-case: `daily_news` has a pixel-signature fallback that
    runs unconditionally — it doesn't depend on keyword detection.

    Returns:
        (found_iids, obstruction) — `obstruction` is the
        ObstructionResult from Layer A (or None when no parse
        succeeded).  The dismiss layer uses `obstruction.bbox` to
        scope its button search to inside the obstruction frame,
        instead of scanning the whole screen.
    """
    from brain.fsm_registry import get_fsm_registry
    from vision.screen_perception import parse_screen
    from vision.obstruction_classifier import classify_obstruction, KIND_NONE

    fw, fh = frame.width, frame.height
    found: list[str] = []

    # Fast-path: if a visually-similar frame was just confirmed as
    # obstruction-free (TTL 5 s), skip the OmniParser parse_screen call
    # entirely.  Costs ~5 ms vs ~2 s for the full structural pass.
    # See _no_obstruction_cache_check above.
    if _no_obstruction_cache_check(frame):
        logger.debug("[perceive] no-obstruction cache hit — skipping detector")
        from vision.obstruction_classifier import ObstructionResult
        return [], ObstructionResult(
            kind=KIND_NONE, bbox=None, signals=[], confidence="high",
        )

    # Layer A: structural obstruction gate.  If no obstruction is
    # detected, the keyword loop is skipped — clean screens never
    # match any interruptor regardless of keyword breadth.
    inventory = parse_screen(frame)
    obstruction = classify_obstruction(inventory)
    if obstruction.kind == KIND_NONE:
        logger.debug("[perceive] no obstruction — skipping interruptor keyword loop")
        _no_obstruction_cache_store(frame)
    else:
        logger.info(
            f"[perceive] obstruction detected: kind={obstruction.kind!r} "
            f"bbox={obstruction.bbox} signals={obstruction.signals}"
        )
        # Restrict OCR tokens to the obstruction bbox.  Per-entry
        # text_zone (if present) overrides this restriction.
        if obstruction.bbox is not None:
            x1, y1, x2, y2 = obstruction.bbox
            tokens_in_bbox = [
                (t, conf, cx, cy)
                for t, conf, cx, cy in ocr_tokens
                if x1 <= cx <= x2 and y1 <= cy <= y2
            ]
        else:
            tokens_in_bbox = ocr_tokens

        for iid, interruptor in get_fsm_registry().interruptors.items():
            keywords = interruptor.detection_keywords
            if not keywords:
                continue
            structural = (interruptor._raw or {}).get("structural") or {}
            # Per-entry structural.text_zone wins over the bbox
            # restriction — Fix 11 entries already encode their
            # exact zone and we don't want to widen it.
            tokens = ocr_tokens if structural.get("text_zone") else tokens_in_bbox
            if _interruptor_matches(keywords, structural, tokens, fw, fh):
                found.append(iid)

        # Phase B2a — novel obstruction fallback.  When the classifier
        # flagged an obstruction but no known interruptor matched,
        # consult Claude for a goal-aware analysis.  Result is cached
        # forever (same screen → no second API call) and every
        # (input, output) pair is appended to the training log for
        # future Qwen distillation.
        #
        # When the analysis comes back with dismissal='tap_decline'
        # (e.g. the system Exit Game? confirmation that fires when
        # Back is pressed from port_overworld), inject a synthetic
        # interruptor id so the dismiss layer taps Cancel/No instead
        # of leaving the dialog stuck on screen.  See the 2026-05-15
        # Amsterdam run: the Exit Game dialog was consulted, cached,
        # and ignored — visits #2…#6 all cache-hit with no action.
        if not found:
            try:
                from vision.obstruction_consult import consult_obstruction
                from brain.goal_context import current_goal
                analysis = consult_obstruction(
                    frame=frame,
                    obstruction=obstruction,
                    ocr_tokens=ocr_tokens,
                    goal_context=current_goal(),
                )
                # 2026-05-19: extended from tap_decline-only to the full
                # set of consult methods _dismiss_interruptor knows how to
                # route.  Without this, Claude's recommendation was cached
                # but never acted on — dialogs stayed stuck on screen
                # while the bot looped on cache hits.
                _CONSULT_ACTIONABLE = {
                    "tap_decline", "tap_close_x", "tap_anywhere",
                    "tap_ok", "tap_accept", "press_back",
                }
                # Skip dismissal entirely when Claude says the
                # "obstruction" is actually irrelevant (e.g. it's part
                # of the persistent HUD, not a blocking popup).  The
                # `dismissal` field is Claude's "if you HAD to dismiss
                # this, how would you" — it's not a directive to act.
                # 2026-05-28 origin: right-side nav panel (mini-map +
                # ports list) was being repeatedly tap_anywhere'd in
                # the centre of the screen on every tick, wasting
                # time + risking spurious side-effects from the
                # centre tap.  The Claude analysis correctly returned
                # outcome_for_goal='irrelevant'; we were ignoring it.
                if analysis is not None and analysis.outcome_for_goal == "irrelevant":
                    logger.info(
                        f"[perceive] consult outcome=irrelevant — "
                        f"NOT dismissing (purpose: {analysis.purpose[:80]!r})"
                    )
                elif analysis is not None and analysis.dismissal in _CONSULT_ACTIONABLE:
                    dismissal = analysis.dismissal
                    # Guard: press_back is unsafe on top-level locations.
                    # Pressing back from village → exits to sea (fleet leaves
                    # the village entirely).  Pressing back from
                    # port_overworld → main_menu.  These are far more
                    # disruptive than the bot's intent (dismiss a popup).
                    # Downgrade to tap_close_x — the dialog-shaped popups
                    # we see on these screens (Village Info, City Info)
                    # always have a close X in their title bar.
                    # 2026-05-22 origin: Berber Village arrival popup;
                    # Claude recommended press_back; bot back-pressed
                    # out of the village to sea.
                    if dismissal == "press_back" and _is_top_level_screen(frame):
                        logger.info(
                            "[perceive] consult recommended press_back on a "
                            "top-level screen (village/port/sea) — downgrading "
                            "to tap_close_x to avoid exiting the location"
                        )
                        dismissal = "tap_close_x"
                    found.append(f"_consult:{dismissal}")
            except Exception as e:
                logger.warning(
                    f"[perceive] obstruction consult skipped: {e}"
                )

    # Image-signature fallback for daily_news — runs unconditionally
    # because the article-list view doesn't surface the title in OCR.
    if "daily_news" not in found and _has_daily_news_close_x(frame):
        found.insert(0, "daily_news")
    return found, obstruction


def _interruptor_matches(
    keywords: list,
    structural: dict,
    ocr_tokens: list,
    fw: int,
    fh: int,
) -> bool:
    """Apply *keywords* + optional *structural* constraints to *ocr_tokens*.

    Each token is (text, conf, cx, cy).  Returns True iff the interruptor
    is considered present.
    """
    zone = structural.get("text_zone")   # [x_lo, y_lo, x_hi, y_hi] normalised
    if zone is not None:
        l, t, r, b = zone
        x_lo, x_hi = l * fw, r * fw
        y_lo, y_hi = t * fh, b * fh

        def _in_zone(cx: int, cy: int) -> bool:
            return x_lo <= cx <= x_hi and y_lo <= cy <= y_hi
    else:
        def _in_zone(cx: int, cy: int) -> bool:   # type: ignore[misc]
            return True

    tokens_in_zone = [(t.lower(), cx, cy)
                      for t, _conf, cx, cy in ocr_tokens
                      if _in_zone(cx, cy)]
    zone_text = " ".join(t for t, _cx, _cy in tokens_in_zone)
    if not all(kw.lower() in zone_text for kw in keywords):
        return False

    if structural.get("requires_vertical_pair"):
        min_gap = structural.get("vertical_pair_min_gap", 30)
        max_gap = structural.get("vertical_pair_max_gap", 120)
        cys = sorted({cy for _t, _cx, cy in tokens_in_zone})
        for i, cy_top in enumerate(cys):
            for cy_bot in cys[i + 1:]:
                if min_gap <= cy_bot - cy_top <= max_gap:
                    return True
        return False

    return True


# ── Parent-building tracker ──────────────────────────────────────────────────
#
# When perceive() classifies the bot as being inside a sub_menu, the layout
# loader (vision/scene_layouts.py) and Qwen prompt both need to know which
# parent building the sub_menu lives under (e.g. inn/recruit_crew vs.
# harbor/recruit_crew — same form, different parent).
#
# We track the last "building: <name>" detail string we saw so that the very
# next sub_menu tick can supply the parent.  This is intentionally simple
# rather than threading state through the FSM registry — the sub_menu is
# always a child of the most-recently-active building screen.

_last_known_building: Optional[str] = None


def _slug_for_building(detail: str) -> Optional[str]:
    """Extract a normalised building slug from a perceive detail string
    like 'building: harbor — extra Qwen prose'.  Returns None when the
    detail doesn't carry a building name in that shape."""
    if not detail or ":" not in detail:
        return None
    head, _, rest = detail.partition(":")
    if head.strip().lower() != "building":
        return None
    name = rest.split("—", 1)[0].strip().lower()
    name = " ".join(name.split())   # collapse whitespace
    return name.replace(" ", "_") if name else None


def _update_last_known_building(nav_state: str, detail: str) -> None:
    """Record the building name on every building-class classification so
    the next sub_menu tick has a parent reference.  Resets when the bot
    leaves a building (port_overworld / sea / world_map clears the cache
    so a future sub_menu detection without a fresh building visit reads
    None instead of a stale value)."""
    global _last_known_building
    if nav_state == "building":
        slug = _slug_for_building(detail)
        if slug:
            if _last_known_building != slug:
                logger.info(
                    f"[perceive] last-known-building updated: "
                    f"{_last_known_building!r} → {slug!r}"
                )
            _last_known_building = slug
    elif nav_state in ("port_overworld", "sea", "sea_cinematic", "world_map"):
        if _last_known_building is not None:
            logger.info(
                f"[perceive] last-known-building cleared "
                f"(now in {nav_state!r}; was {_last_known_building!r})"
            )
            _last_known_building = None


def _resolve_parent_building(nav_state: str, detail: str) -> Optional[str]:
    """Determine the parent_building slug to pass to Qwen / the scene-
    layout loader for THIS tick.  For sub_menu, return the last-known-
    building; for building/other states, None (the loader doesn't need
    parent context for those)."""
    if nav_state == "sub_menu":
        if _last_known_building is None:
            logger.warning(
                "[perceive] sub_menu detected but no last-known-building "
                "cached — parent context unavailable for layout lookup"
            )
        return _last_known_building
    return None


def _dismiss_interruptor(iid: str, frame, obstruction_bbox=None) -> None:
    """
    Execute the dismissal action for a known interruptor.
    Dispatches by the 'dismissal' field in interruptors.json — not by iid —
    so adding new dismissal methods only requires updating the KB, not this code.

    For 'tap_close_button_only', the interruptor record may carry a
    `close_position: [x, y]` field naming the exact pixel to tap.  Required
    when multiple stackable popups (daily_news, perk_event, attendance_popup)
    use the same dismissal method but live in different locations on screen.

    *obstruction_bbox* (Layer A) scopes the OK/X button search to inside
    the modal frame.  Without it, _dismiss_tap_ok_or_x would scan the
    whole screen for ANY 'ok'/'×'/'close' label — which can match the
    wrong button entirely (e.g. an 'OK' deep in a building's sub-menu
    while the actual dialog has nothing of the sort).
    """
    # Synthetic ids of the form '_consult:<method>' come from Phase B2a:
    # Claude consulted the novel obstruction and recommended a specific
    # dismissal that isn't yet stored as a known interruptor.
    #
    # 2026-05-19: extended from tap_decline-only to the full vocabulary
    # of dismissal methods Claude returns (see vision/obstruction_consult.py
    # prompt schema).  Without this the consult result was computed,
    # cached, and ignored — the dialog stayed on screen and every
    # subsequent visit cache-hit with no action (Company-Info-panel
    # incident, 2026-05-19 14:00–14:03).
    if iid.startswith("_consult:"):
        method = iid.split(":", 1)[1]
        if method == "tap_decline":
            _dismiss_tap_decline(frame, obstruction_bbox=obstruction_bbox)
        elif method == "tap_close_x":
            # _dismiss_close_button consults DialogModel.close_button first
            # (Phase 2.4 typed migration); falls back to keyword search.
            # No KB-declared position because this is a Claude-consulted
            # dialog with no learned record yet.
            _dismiss_close_button(frame, iid, position=None)
        elif method == "tap_anywhere":
            from actions.adb_actions import tap as _tap
            cx, cy = frame.width // 2, frame.height // 2
            logger.info(
                f"[perceive] consult tap_anywhere — tapping centre ({cx}, {cy})"
            )
            _tap(cx, cy)
            time.sleep(1.0)
        elif method == "tap_ok":
            _dismiss_tap_ok(frame)
        elif method == "tap_accept":
            # tap_accept is shaped like tap_ok (Confirm/Yes/OK on a
            # consult-recommended dialog).  _dismiss_tap_ok already
            # tries DialogModel actions matching ok/okay/confirm/yes
            # before falling back to keyword search.
            _dismiss_tap_ok(frame)
        elif method == "press_back":
            from actions.sail_actions import press_back as _press_back
            logger.info("[perceive] consult press_back — pressing system Back")
            _press_back()
            time.sleep(1.0)
        else:
            logger.warning(
                f"[perceive] unsupported consult method {method!r} — "
                "no dismissal performed"
            )
        return

    from brain.fsm_registry import get_fsm_registry
    interruptor = get_fsm_registry().interruptors.get(iid)
    raw    = interruptor._raw if interruptor else {}
    method = raw.get("dismissal", RECOVERY_TAP_OK_OR_X)

    # Special case first: 'tap_ok_then_wait_reload' is the Android
    # connection dialog method whose name happens to contain '_then_'.
    # If we let chained-dismissal handling run, we'd split it into
    # ['tap_ok', 'wait_reload'] which is not what's intended.
    if method == "tap_ok_then_wait_reload":
        _dismiss_android_connection(frame)
        return

    # Chained dismissal grammar: any method containing '_then_' is a
    # sequence of primitive steps to execute in order.  Used by
    # learned interruptors where Claude / the teach-loop captured a
    # multi-step recipe (e.g. the fleet_death_recovery_screen entry
    # in interruptors.json:147 stored
    # 'tap(1800,120)_then_wait_1s_then_tap_port return_then_wait_2s_then_tap_ok').
    # The parser handles tap(x,y), tap_<label>, wait_<n>s, wait_<n>ms,
    # and press_back primitives.
    if "_then_" in method:
        _execute_chained_dismissal(method, frame, raw)
        return

    # Single-method dismissals — the original recognised vocabulary.
    if method == "tap_close_button_only":
        _dismiss_close_button(frame, iid, raw.get("close_position"))
    elif method == "tap_ok":
        _dismiss_tap_ok(frame)
    elif method == "tap_skip_or_advance_until_clear":
        _dismiss_story_event(frame)
    else:
        # tap_ok_or_x, tap_collect_or_ok, tap_ok_or_cancel, tap_anywhere, unknown
        _dismiss_tap_ok_or_x(frame, obstruction_bbox=obstruction_bbox)


# ── Chained-dismissal parser ─────────────────────────────────────────────────
#
# Grammar (informal — designed for Claude / human teach-loops that
# naturally emit step-by-step recipes):
#
#   <chain>     ::= <step> ('_then_' <step>)*
#   <step>      ::= 'tap(' INT ',' INT ')'        # tap at exact pixel coords
#                |  'tap_' LABEL                   # find label via OmniParser, tap centre
#                |  'wait_' NUMBER 's'             # sleep N seconds (float OK)
#                |  'wait_' INT 'ms'               # sleep N milliseconds
#                |  'press_back'                   # Android back button
#
# Unrecognised steps are logged and skipped — better to partial-execute
# than to lose the rest of the chain on a typo.

import re as _re


def _execute_chained_dismissal(method: str, frame, raw: dict) -> None:
    """Parse a '_then_'-joined chain and execute each step in order."""
    import time as _time
    from actions.adb_actions import tap as _tap, press_back as _press_back
    from actions.sail_actions import _find_button
    from capture.adb_capture import capture_screen as _capture_screen

    steps = [s.strip() for s in method.split("_then_") if s.strip()]
    logger.info(
        f"[perceive] Chained dismissal: {len(steps)} step(s)  "
        f"raw={method!r}"
    )

    current_frame = frame
    for i, step in enumerate(steps, 1):
        prefix = f"[perceive]   step {i}/{len(steps)}"

        # tap(x,y) — exact pixel coords
        m = _re.match(r"^tap\(\s*(\d+)\s*,\s*(\d+)\s*\)$", step)
        if m:
            x, y = int(m.group(1)), int(m.group(2))
            logger.info(f"{prefix}: tap({x},{y})")
            _tap(x, y)
            _time.sleep(0.5)
            continue

        # wait_<n>s — seconds (allow decimals)
        m = _re.match(r"^wait_(\d+(?:\.\d+)?)s$", step)
        if m:
            secs = float(m.group(1))
            logger.info(f"{prefix}: wait {secs}s")
            _time.sleep(secs)
            continue

        # wait_<n>ms — milliseconds
        m = _re.match(r"^wait_(\d+)ms$", step)
        if m:
            ms = int(m.group(1))
            logger.info(f"{prefix}: wait {ms}ms")
            _time.sleep(ms / 1000.0)
            continue

        # press_back
        if step == "press_back":
            logger.info(f"{prefix}: press_back")
            _press_back()
            _time.sleep(0.5)
            continue

        # tap_<label> — find by label via OmniParser (whole-word match)
        if step.startswith("tap_"):
            label = step[len("tap_"):].strip()
            if not label:
                logger.warning(f"{prefix}: empty label after 'tap_' — skipping")
                continue
            # Re-capture each label-tap because earlier steps may have
            # changed the screen.  If capture fails, fall back to the
            # initial frame.
            try:
                current_frame = _capture_screen()
            except Exception:
                pass
            btn = _find_button(current_frame, label)
            if btn:
                logger.info(f"{prefix}: tap_{label!r} @ {btn}")
                _tap(*btn)
                _time.sleep(0.5)
            else:
                logger.warning(
                    f"{prefix}: tap_{label!r} — button not found on current "
                    "screen; skipping (chain continues)"
                )
            continue

        logger.warning(f"{prefix}: unrecognised primitive {step!r} — skipping")


def _dismiss_android_connection(frame) -> None:
    """Tap OK on the connection dialog then wait for game reload."""
    from actions.adb_actions import tap
    from actions.sail_actions import _find_button, _ocr_frame

    logger.warning("[perceive] Android connection dialog — tapping OK and waiting for reload")
    btn = _find_button(frame, "ok", "okay")
    if btn:
        tap(*btn)
    else:
        tap(frame.width // 2, frame.height // 2)
    time.sleep(3.0)

    # Wait for reload
    for _ in range(60):
        time.sleep(1.0)
        f = capture_screen()
        tokens = _ocr_frame(f.crop((
            f.width // 4, f.height // 4,
            3 * f.width // 4, 3 * f.height // 4,
        )), min_conf=0.3)
        text = " ".join(t.lower() for t, _, _, _ in tokens)
        if "connection" not in text or "unstable" not in text:
            logger.info("[perceive] Game reloaded after connection dialog")
            break


def _dismiss_close_button(frame, iid: str, position) -> None:
    """
    Tap the close (X) button of a popup that requires explicit close-button
    interaction (tapping elsewhere does nothing).  Used for daily_news,
    perk_event, attendance_popup and similar.

    `position` is the optional [x, y] from the KB.  When supplied, tap there
    directly — required for popups whose X is a small unlabelled icon that
    OCR cannot find (e.g. the daily_news round-X glyph hangs OUTSIDE the
    dialog and is not detected as a text 'X').

    Without a KB-declared position, fall back to OCR-locating an 'X'/'close'
    glyph; if even that fails, log and tap a conservative top-right region.
    The old behaviour tapped (0.92*W, 0.08*H), which lands on the perk/event
    popup's X when that popup is stacked behind daily_news — closing the
    wrong popup.  Avoid that fallback when the caller named a specific
    interruptor.
    """
    from actions.adb_actions import tap
    from actions.sail_actions import _find_button

    from brain.dismissal_telemetry import record as _telem

    if position and len(position) == 2:
        x, y = int(position[0]), int(position[1])
        logger.info(f"[perceive] Dismissing {iid!r} via KB close_position ({x}, {y})")
        tap(x, y)
        _telem("dismiss_close_button", "legacy")  # KB-position is non-typed
        time.sleep(1.0)
        return

    # Phase 2.3 (cont.): prefer DialogModel.close_button (typed
    # structural detection) over keyword glyph search.
    dialog = _detect_dialog_on_frame(frame)
    if dialog is not None and dialog.close_button is not None:
        x1, y1, x2, y2 = dialog.close_button
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        logger.info(
            f"[perceive] Dismissing {iid!r} via DialogModel.close_button @ "
            f"({cx}, {cy})"
        )
        tap(cx, cy)
        _telem("dismiss_close_button", "typed")
        time.sleep(1.0)
        return

    logger.info(f"[perceive] Dismissing {iid!r} via close button (no KB position)")
    btn = _find_button(frame, "×", "x", "close")
    if btn:
        tap(*btn)
        _telem("dismiss_close_button", "legacy")
    else:
        # Last-resort heuristic.  Likely wrong when popups are stacked —
        # the KB should declare close_position for any popup hitting this
        # path more than once.
        logger.warning(
            f"[perceive] No close button found for {iid!r} and no KB position — "
            "tapping screen-corner fallback (may close the wrong popup)"
        )
        tap(int(frame.width * 0.92), int(frame.height * 0.08))
        _telem("dismiss_close_button", "noop")
    time.sleep(1.0)


def _dismiss_tap_ok(frame) -> None:
    """Dismissal that confirms/proceeds — tap OK/Confirm/Yes first, never X/close.
    Used when X means 'cancel' and would return to the same blocked state.

    Phase 2.3: prefers DialogModel.actions (structural typed detector)
    when available, falls back to the legacy keyword button search.
    Logs which path was taken so we can measure the migration's
    coverage in real runs.
    """
    from actions.adb_actions import tap
    from actions.sail_actions import _find_button, press_back

    from brain.dismissal_telemetry import record as _telem

    dialog = _detect_dialog_on_frame(frame)
    if dialog is not None:
        for action in dialog.actions:
            label = action.label.strip().lower()
            if label in {"ok", "okay", "confirm", "yes"}:
                cx = (action.bbox[0] + action.bbox[2]) // 2
                cy = (action.bbox[1] + action.bbox[3]) // 2
                logger.info(
                    f"[perceive] Dismissing via DialogModel "
                    f"action={action.label!r} @ ({cx}, {cy})"
                )
                tap(cx, cy)
                _telem("dismiss_tap_ok", "typed")
                time.sleep(1.0)
                return

    logger.info("[perceive] Dismissing via OK/Confirm (legacy keyword search)")
    btn = _find_button(frame, "ok", "confirm", "yes", "leave", "abandon")
    if btn:
        tap(*btn)
        _telem("dismiss_tap_ok", "legacy")
    else:
        # No button found — tap centre of dialog (usually lands on OK for simple dialogs)
        tap(frame.width // 2, frame.height // 2)
        _telem("dismiss_tap_ok", "noop")
    time.sleep(1.0)


def _detect_dialog_on_frame(frame):
    """Compute DialogModel for *frame* using cached OmniParser elements.

    Helper for Phase 2.3 dismissal handlers that want to consult
    DialogModel before falling back to legacy keyword button search.
    OmniParser parsing is cached per frame id; this call is cheap.
    """
    try:
        from vision.omniparser import get_omniparser
        from vision.region_detectors.dialog import detect_dialog
        parser = get_omniparser()
        elements = parser.parse_fast(frame)
        return detect_dialog(elements, frame.width, frame.height)
    except Exception as e:
        logger.debug(f"[perceive] _detect_dialog_on_frame failed: {e}")
        return None


def _dismiss_story_event(frame) -> None:
    """
    Skip out of an active storyline / cutscene.  Triggered when both 'Skip'
    and 'Dialog History' are visible (the universal cutscene chrome in UWO).

    The bot does not need to play story content for now, so the strategy is
    a small loop:
      1. Tap 'Skip' when visible (fastest exit).
      2. Tap 'Yes/Confirm/OK' if a skip-confirmation dialog appears.
      3. Tap any visible button if a mandatory-choice screen appears
         (per user: 'as long as you click one [button] it will move forward'
         — choice content does not matter to the bot).
      4. Fall back to a safe lower-centre tap to advance dialog narration.
      5. Re-OCR; exit when the cutscene chrome ('skip' / 'dialog history')
         is no longer present.
    Capped at MAX_ITER iterations to prevent runaway.
    """
    from actions.adb_actions import tap
    from actions.sail_actions import _find_button, _ocr_frame
    from capture.adb_capture import capture_screen as _cap

    MAX_ITER = 20
    logger.info("[perceive] Active story event — entering skip-loop")

    for i in range(MAX_ITER):
        f = _cap()
        tokens = _ocr_frame(f, min_conf=0.3)
        text   = " ".join(t.lower() for t, _, _, _ in tokens)

        # Termination — cutscene chrome no longer visible.
        if "skip" not in text and "dialog history" not in text:
            logger.info(f"[perceive] Cutscene chrome cleared after {i} taps")
            return

        # Priority 1: Skip button — fast path out.
        btn = _find_button(f, "skip")
        if btn:
            logger.debug(f"[perceive] story step {i}: Skip @ {btn}")
            tap(*btn)
            time.sleep(1.0)
            continue

        # Priority 2: Skip-confirmation dialog (Yes / Confirm / OK).
        btn = _find_button(f, "yes", "confirm", "ok")
        if btn:
            logger.debug(f"[perceive] story step {i}: confirm @ {btn}")
            tap(*btn)
            time.sleep(1.0)
            continue

        # Priority 3: Any button — mandatory-choice screen.
        # OmniParser returns clickable elements with bboxes; pick the
        # bottom-most button (choices typically render in the lower half).
        try:
            from vision.omniparser import get_omniparser
            parser = get_omniparser()
            if parser.yolo_available():
                btns = [
                    e for e in parser.parse_fast(f)
                    if getattr(e, "element_type", "") == "button"
                ]
                btns.sort(key=lambda e: -e.cy)
                if btns:
                    coord = (btns[0].cx, btns[0].cy)
                    logger.debug(f"[perceive] story step {i}: any-button @ {coord}")
                    tap(*coord)
                    time.sleep(1.0)
                    continue
        except Exception as e:
            logger.debug(f"[perceive] omniparser unavailable for any-button fallback: {e}")

        # Priority 4: Safe-point tap — advances narration on most cutscenes.
        safe = (f.width // 2, int(f.height * 0.85))
        logger.debug(f"[perceive] story step {i}: safe-point tap @ {safe}")
        tap(*safe)
        time.sleep(1.0)

    logger.warning(
        f"[perceive] Story dismissal hit MAX_ITER={MAX_ITER} — cutscene chrome may still be present"
    )


def _dismiss_tap_ok_or_x(frame, obstruction_bbox=None) -> None:
    """Generic dismissal — find an X/close or OK/confirm button INSIDE
    the obstruction's bbox (Layer A) and tap it.

    When *obstruction_bbox* is provided, the button search is scoped
    to inside the frame.  This is the key fix for the false-fire
    cascade where a phantom popup detection in one corner of the
    screen triggered a search across the WHOLE frame, found a
    matching token somewhere unrelated, and tapped it — disrupting
    the underlying scene.

    When no button is found inside the bbox, the function LOGS AND
    RETURNS WITHOUT TAPPING.  The previous blind fallback (tap
    `(0.92×w, 0.08×h)` — top-right chrome) was reckless: it could
    open the settings menu, the world map, or hit dead space.  The
    no-op tracking in `dismiss_interruptors` retires the interruptor
    after `_NOOP_SKIP_THRESHOLD` consecutive "no actual button to
    tap" occurrences.

    When no bbox is provided (back-compat path), falls back to the
    legacy whole-frame search WITHOUT the blind tap fallback —
    safer than the original behaviour while preserving the
    well-targeted searches.
    """
    from actions.adb_actions import tap
    from actions.sail_actions import _find_button

    from brain.dismissal_telemetry import record as _telem

    logger.info("[perceive] Dismissing interruptor via OK/X")

    # Phase 2.3 (cont.): prefer DialogModel before keyword search.
    # Try the X close anchor first (X-only dismissal is safer than
    # tapping a positive action when intent is just "go away").
    dialog = _detect_dialog_on_frame(frame)
    if dialog is not None:
        bbox_tap = _dialog_close_or_ok_tap(dialog, obstruction_bbox)
        if bbox_tap is not None:
            cx, cy, label = bbox_tap
            logger.info(
                f"[perceive]   tapping via DialogModel {label!r} @ ({cx}, {cy})"
            )
            tap(cx, cy)
            _telem("dismiss_tap_ok_or_x", "typed")
            time.sleep(1.0)
            return

    if obstruction_bbox is not None:
        x1, y1, x2, y2 = obstruction_bbox
        btn = (_find_button(frame, "×", "x", "close",
                            x_min=x1, x_max=x2, y_min=y1, y_max=y2)
               or _find_button(frame, "ok", "confirm", "collect",
                               x_min=x1, x_max=x2, y_min=y1, y_max=y2))
        if btn:
            logger.info(f"[perceive]   tapping button inside bbox @ {btn}")
            tap(*btn)
            _telem("dismiss_tap_ok_or_x", "legacy")
            time.sleep(1.0)
            return
        logger.info(
            "[perceive]   no OK/X button found inside obstruction bbox "
            f"({obstruction_bbox}) — skipping tap (no-op tracking will "
            "retire this interruptor after repeated misses)"
        )
        _telem("dismiss_tap_ok_or_x", "noop")
        return

    # Back-compat path: caller did not supply a bbox.  Search the
    # whole frame but DO NOT tap a blind fallback.
    btn = (_find_button(frame, "×", "x", "close") or
           _find_button(frame, "ok", "confirm", "collect"))
    if btn:
        logger.info(f"[perceive]   tapping button @ {btn}")
        tap(*btn)
        _telem("dismiss_tap_ok_or_x", "legacy")
        time.sleep(1.0)
        return
    logger.info(
        "[perceive]   no OK/X button found anywhere on frame — skipping "
        "tap (no-op tracking will retire this interruptor after "
        "repeated misses)"
    )
    _telem("dismiss_tap_ok_or_x", "noop")


def _dialog_close_or_ok_tap(dialog, obstruction_bbox):
    """Pick a structural tap point from a DialogModel — X close first,
    then OK/Confirm action.  When *obstruction_bbox* is supplied, the
    chosen bbox must lie inside it (defends against typed detector
    matching a different dialog than the one the caller cares about).

    Returns (cx, cy, label) or None.
    """
    def inside(b):
        if obstruction_bbox is None:
            return True
        ox1, oy1, ox2, oy2 = obstruction_bbox
        cx = (b[0] + b[2]) // 2
        cy = (b[1] + b[3]) // 2
        return ox1 <= cx <= ox2 and oy1 <= cy <= oy2

    if dialog.close_button is not None and inside(dialog.close_button):
        b = dialog.close_button
        return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2, "close_button")
    for action in dialog.actions:
        if action.label.strip().lower() in {"ok", "okay", "confirm", "yes"}:
            if inside(action.bbox):
                b = action.bbox
                return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2, action.label)
    return None


def _dismiss_tap_decline(frame, obstruction_bbox=None) -> None:
    """Tap a Cancel / No / Decline button inside the obstruction bbox.

    Wired up for the Phase B2a consult path: when Claude inspects a
    novel obstruction and returns dismissal='tap_decline' (typically
    a confirmation dialog the bot DID NOT mean to trigger — e.g. the
    system Exit Game? prompt that fires when Back is pressed from
    port_overworld), this helper looks for the negative-action button
    INSIDE the bbox and taps it.

    Tries Cancel-family labels first (Cancel/No/Decline/Back/Close),
    then explicit Cancel-only fallback, all scoped to the bbox so we
    never tap a button outside the modal frame.  Skips silently when
    no decline button is visible — better to leave the dialog up and
    let the noop tracker retire it than to tap something arbitrary.
    """
    from actions.adb_actions import tap
    from actions.sail_actions import _find_button

    from brain.dismissal_telemetry import record as _telem

    logger.info("[perceive] Dismissing via tap_decline (Cancel/No)")

    if obstruction_bbox is None:
        logger.info(
            "[perceive]   tap_decline called without bbox — skipping "
            "(refusing to scan the whole screen for a Cancel button; "
            "could match a button outside the dialog)"
        )
        _telem("dismiss_tap_decline", "noop")
        return

    x1, y1, x2, y2 = obstruction_bbox

    # Phase 2.3 (cont.): prefer DialogModel.actions matching a negative
    # label inside the bbox over keyword glyph search.
    dialog = _detect_dialog_on_frame(frame)
    if dialog is not None:
        for action in dialog.actions:
            if action.label.strip().lower() not in {
                "cancel", "no", "decline", "back", "close",
            }:
                continue
            bcx = (action.bbox[0] + action.bbox[2]) // 2
            bcy = (action.bbox[1] + action.bbox[3]) // 2
            if x1 <= bcx <= x2 and y1 <= bcy <= y2:
                logger.info(
                    f"[perceive]   tapping decline via DialogModel "
                    f"action={action.label!r} @ ({bcx}, {bcy})"
                )
                tap(bcx, bcy)
                _telem("dismiss_tap_decline", "typed")
                time.sleep(1.0)
                return

    btn = _find_button(
        frame, "cancel", "no", "decline", "back", "close",
        x_min=x1, x_max=x2, y_min=y1, y_max=y2,
    )
    if btn:
        logger.info(f"[perceive]   tapping decline button inside bbox @ {btn}")
        tap(*btn)
        _telem("dismiss_tap_decline", "legacy")
        time.sleep(1.0)
        return
    logger.info(
        f"[perceive]   no Cancel/No/Decline button found inside obstruction "
        f"bbox ({obstruction_bbox}) — skipping tap (no-op tracking will "
        "retire this interruptor after repeated misses)"
    )
    _telem("dismiss_tap_decline", "noop")


# ── Unknown blocking thing: learn and dismiss ─────────────────────────────────
#
# Called when perceive() ends up in state="unknown" — something is blocking
# the screen that no keyword pattern recognises.
#
# Tier 1 (Qwen): fast local LLM describes the dialog and flags simple vs complex.
#   - Simple: has an OK/close button → tap it; save as new interruptor.
#   - Complex: requires a decision (sea event, crew amount, etc.) → escalate to Claude.
#
# Tier 2 (Claude Vision): full screenshot + Qwen description → Claude decides
#   what to tap and returns a structured action.
#
# Tier 3 (human): unchanged existing path.
#
# After any successful resolution the entry is saved to interruptors.json so
# the next occurrence is caught at pass 1 (known interruptors) with no model call.

# Dismissal types understood by _dismiss_interruptor.
# These mirror the values used in interruptors.json.
DISMISSAL_TAP_OK        = "tap_ok"
DISMISSAL_TAP_OK_OR_X   = RECOVERY_TAP_OK_OR_X   # "tap_ok_or_x"
DISMISSAL_TAP_ANYWHERE  = "tap_anywhere"
DISMISSAL_TAP_CLOSE     = "tap_close_button_only"
DISMISSAL_NEEDS_CLAUDE  = "needs_claude"          # Qwen flag: escalate to Claude

_QWEN_UNKNOWN_SYSTEM = """\
You are a dialog analyst for a mobile game bot (Uncharted Waters Origin).
You will be given OCR tokens read from the current screen.

Your task: identify the dialog/popup using ONLY the OCR tokens.

CRITICAL — describe what is on the screen, NOT the bot's state.
Bad output examples to AVOID:
  "The screen shows an unrecognized layout"
  "A vision check overlay message"
  "A modal dialog hiding chrome"
  "A layout that is not recognized"
These describe the bot's perception, not the actual UI.

Good output examples:
  "Daily Login Reward popup with Collect button"
  "Recruit Crew confirmation asking to recruit 110 crew for 23,760 gold"
  "Android connection unstable warning with OK button"
  "NPC speech bubble narrating quest objective"

If the OCR tokens don't contain enough text to identify a real dialog
(e.g. you only see scenery or in-world content), set confidence='low'
and describe what's actually visible — don't fabricate a dialog.

The game's dismissal vocabulary (use ONLY one of these as "dismissal"):
  tap_ok            — there is an OK / Confirm / Yes button; tap it
  tap_ok_or_x       — there is OK/X/Close; tap whichever is visible
  tap_anywhere      — overlay notice; tap anywhere to dismiss
  tap_close_button_only — only the X/close button works (e.g. daily news web popup)
  needs_claude      — dialog requires a decision (choose amount, pick option, event choice)

NPC speech bubbles and building name plates are NOT dismissable —
they auto-clear or only respond to specific in-world taps.  If you
suspect the screen contains an NPC bubble or building name plate
rather than a modal dialog, set dismissal='needs_claude' and note
this in description; do not guess at tap_ok_or_x.

Return ONLY valid JSON, no markdown:
{
  "description": "<one sentence describing what the dialog/screen is>",
  "detection_keywords": ["<word1>", "<word2>"],
  "dismissal": "<one of the five dismissal types above>",
  "confidence": "high or low"
}"""


def _qwen_describe_unknown(ocr_tokens: list, nav_detail: str) -> Optional[dict]:
    """
    Ask Qwen to describe an unknown blocking screen. Returns parsed dict or None.

    PHASE 5A FOLLOWUP: nav_detail is intentionally NOT included in the prompt.
    Earlier versions passed perceive's detail string ("unrecognised layout —
    no chrome, no port HUD…" or older "vision check says NOT at sea") as
    "Current nav context", which Qwen ingested and paraphrased back as a
    "screen description".  That feedback loop produced phantom interruptor
    saves with metacognitive descriptions ("The screen shows an unrecognized
    layout", "A vision check overlay message").  Removing the context line
    from the prompt forces Qwen to describe ONLY what it can read in OCR
    tokens — actual game UI text.

    The nav_detail parameter stays in the signature for backward compat
    (callers may still pass it; we just don't use it).
    """
    try:
        from mlx_lm import generate
        from vision.qwen_perception import _load, _model, _tokenizer, _SYSTEM
        if not _load():
            return None

        ocr_text = "\n".join(f"  {t}" for t, *_ in ocr_tokens) or "  (no OCR tokens)"
        prompt = (
            f"OCR tokens from screen:\n{ocr_text}\n\n"
            "Identify the dialog and choose a dismissal action.  Describe "
            "ONLY what is on screen based on the OCR tokens.  Do NOT speculate "
            "about whether the screen 'looks like' an unrecognised layout or "
            "a modal dialog — describe the concrete game UI element you can "
            "name from the tokens, or say confidence='low' if the tokens are "
            "insufficient."
        )
        messages = [
            {"role": "system", "content": _QWEN_UNKNOWN_SYSTEM},
            {"role": "user",   "content": prompt},
        ]
        import json as _json
        from vision.qwen_perception import _tokenizer as _tok, _model as _mdl
        formatted = _tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        raw = generate(_mdl, _tok, prompt=formatted, max_tokens=150, verbose=False).strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1][4:] if raw.split("```")[1].startswith("json") else raw.split("```")[1]
        return _json.loads(raw.strip())
    except Exception as e:
        logger.debug(f"[perceive] Qwen unknown-dialog query failed: {e}")
        return None


def _claude_resolve_unknown(frame, qwen_description: str) -> Optional[dict]:
    """Ask Claude Vision to resolve a complex/unknown dialog. Returns action dict or None."""
    import base64, io, json as _json
    from vision.claude_vision import ClaudeVision

    cv = ClaudeVision()
    client = cv._get_client()
    if client is None:
        return None

    thumb = frame.copy()
    thumb.thumbnail((1200, 540))
    buf = io.BytesIO()
    thumb.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    prompt = (
        f"The bot encountered an unexpected screen. "
        f"Local analysis says: {qwen_description}\n\n"
        "Look at the screenshot and tell the bot exactly what to tap to proceed.\n\n"
        "Return ONLY valid JSON:\n"
        '{\n'
        '  "description": "<what this screen is>",\n'
        '  "detection_keywords": ["<word1>", "<word2>"],\n'
        '  "dismissal": "<tap_ok | tap_ok_or_x | tap_anywhere | tap_close_button_only>",\n'
        '  "tap_x": <pixel x or null>,\n'
        '  "tap_y": <pixel y or null>,\n'
        '  "reasoning": "<brief explanation>"\n'
        "}"
    )
    try:
        resp = client.messages.create(
            model=cv.model,
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return _json.loads(raw.strip())
    except Exception as e:
        logger.debug(f"[perceive] Claude unknown-dialog query failed: {e}")
        return None


# Phantom-save filter — Phase 5a #3.
#
# When perceive's classifier falls through to the "unknown" branch and
# handle_unknown_blocking invokes Qwen, Qwen sometimes paraphrases the
# detail string we fed it back as a "screen description".  That gets saved
# as an interruptor entry that has no usable detection_keywords (because
# Qwen never saw a real overlay to describe) and adds no value — it just
# pollutes the KB.
#
# Heuristic to refuse the save:
#   - The description contains any of the perceive-context-leak phrases
#     below (words that came from our own prompt, not from a real screen)
#     AND the entry has no concrete detection_keywords.  Real interruptors
#     typically come with concrete keywords from real on-screen text;
#     paraphrase confabulations come with empty keyword lists.
#
# This catches the feedback-loop class without preventing legitimate
# interruptor learning from real screens.
# Phrases that should NOT appear in real game-UI descriptions.  Real
# interruptors describe NAMED dialogs ("Daily Login Reward popup",
# "Recruit Crew confirmation"); phantoms describe meta-state about the
# bot's perception ("The screen shows…", "A vision check overlay…",
# "layout that is not recognized").  We add the new paraphrases as
# observed.
_PHANTOM_PHRASES = (
    # original Phase 5a list
    "vision check",
    "not at sea",
    "unrecognised layout",
    "modal dialog or overlay hiding",
    "vision check overlay",
    # added after May-2 14:10 run — Qwen paraphrasing the new neutral
    # detail string ("unrecognised layout — no chrome…") as
    # "layout that is not recognized" / "lack of visual cues".
    "not recognized",                    # American spelling of unrecognised
    "lack of visual cues",
    "lack of visual",
    "screen shows a layout",
    "screen displays a layout",
    "visual cues for navigation",
    "indicating a lack",
    "main navigation",                   # phantom paraphrase pattern, not a real game term
    "preventing access to the main",     # phantom paraphrase pattern
)


def _looks_like_phantom_paraphrase(entry: dict) -> bool:
    """
    True when the entry looks like Qwen paraphrasing our perceive context
    rather than describing a real on-screen overlay.

    Heuristic: ANY phantom phrase in the description is sufficient to
    refuse the save.  Initial version also required no detection_keywords,
    but Qwen sometimes invents plausible-looking keyword lists by mixing
    prompt phrases ("NOT at sea", "modal", "overlay") with real OCR tokens
    (e.g. "Plymouth").  The mixed entries are still phantoms — their
    keyword lists fire on whatever screen Qwen happened to mention,
    causing false-positive interruptor matches at the Plymouth overworld.

    Real game interruptors have descriptions that name the dialog
    concretely ("Recruit Crew confirmation", "Daily Login Reward popup",
    "Android connection unstable warning") — not metacognitive
    descriptions about overlays/checks/modal-state.  So the phantom phrase
    list itself is the reliable discriminator.
    """
    desc = (entry.get("description") or "").lower()
    return any(p in desc for p in _PHANTOM_PHRASES)


# Module-level path — extracted so tests can monkey-patch it via
# perceive_mod._INTERRUPTORS_PATH instead of Path inside the function.
from pathlib import Path as _Path
_INTERRUPTORS_PATH = _Path("memory/knowledge/fsm/interruptors.json")


def _save_new_interruptor(entry: dict, source: str) -> None:
    """
    Append a newly learned interruptor to interruptors.json.
    Skips if an entry with the same id already exists, or if the entry
    looks like a Qwen paraphrase of perceive's own context (phantom).
    """
    import json as _json
    from datetime import datetime, timezone

    iid = entry.get("id", "")

    # Phase 5a #3: refuse to save phantom paraphrase confabulations.
    if _looks_like_phantom_paraphrase(entry):
        logger.warning(
            f"[perceive] refusing to save phantom interruptor {iid!r} — "
            f"description contains perceive-context phrases AND no "
            f"detection_keywords (likely Qwen paraphrasing prompt context, "
            f"not a real on-screen overlay).  description="
            f"{(entry.get('description') or '')[:120]!r}"
        )
        return

    try:
        interruptors = _json.loads(_INTERRUPTORS_PATH.read_text())
    except Exception:
        interruptors = []

    if any(e.get("id") == iid for e in interruptors):
        return   # already known

    interruptors.append({
        "id":                iid,
        "description":       entry.get("description", ""),
        "atomic":            True,
        "detection_keywords": entry.get("detection_keywords", []),
        "dismissal":         entry.get("dismissal", DISMISSAL_TAP_OK_OR_X),
        "resumes":           "current_state_unchanged",
        "learned_at":        datetime.now(timezone.utc).isoformat(),
        "learned_from":      source,
        "correction_count":  0,
    })
    _INTERRUPTORS_PATH.write_text(_json.dumps(interruptors, indent=2, ensure_ascii=False))
    logger.info(f"[perceive] Saved new interruptor {iid!r} (learned from {source})")

    # Reload KB so the new entry is used immediately
    from brain.kb import reload as _reload_kb
    _reload_kb()


# PHASE 5A FOLLOWUP: debounce.  handle_unknown_blocking previously fired on
# every "unknown" perceive, which conflated three categories of screen:
#   - real blocking dialogs (persistent until dismissed)
#   - transient overworld overlays (NPC speech bubbles, building name plates;
#     auto-dismiss within 1-2 seconds)
#   - mid-animation / chrome-load frames (resolve themselves on next perceive)
#
# The bot's response to "unknown" — invoke Qwen, save an interruptor,
# attempt a tap — is harmful for the second and third categories.  In the
# May-2 14:10 run, an NPC speech bubble at Plymouth port_overworld
# triggered handle_unknown_blocking, Qwen suggested tap_ok_or_x, the tap
# landed near the Fortune Teller name plate, the plate expanded, the next
# tap entered the wrong building.
#
# Debounce: require N consecutive same-signature unknown perceives before
# treating the screen as actionable.  Transient overlays clear themselves
# before reaching the threshold; real persistent dialogs accumulate same-
# signature perceives and eventually trigger.
_UNKNOWN_DEBOUNCE_THRESHOLD = 3
_unknown_signature_history: "deque[str]" = None  # type: ignore  # initialised lazily


def _unknown_signature(ocr_tokens: list) -> str:
    """Cheap stable signature for the debounce — top alphabetic OCR tokens."""
    seen: set[str] = set()
    out: list[str] = []
    for entry in ocr_tokens:
        t = entry[0] if isinstance(entry, (list, tuple)) and entry else None
        if not isinstance(t, str):
            continue
        tl = t.lower()
        if len(tl) < 4 or not tl.replace(" ", "").isalpha():
            continue
        if tl in seen:
            continue
        seen.add(tl)
        out.append(tl)
        if len(out) >= 8:
            break
    return "|".join(sorted(out))


def _unknown_debounce_should_fire(ocr_tokens: list) -> tuple[bool, int]:
    """
    Update the unknown-signature history with the current frame and return
    (should_fire, consecutive_count).
    should_fire=True only when the SAME signature has been observed for at
    least _UNKNOWN_DEBOUNCE_THRESHOLD consecutive perceives.
    """
    global _unknown_signature_history
    if _unknown_signature_history is None:
        from collections import deque
        _unknown_signature_history = deque(maxlen=_UNKNOWN_DEBOUNCE_THRESHOLD)

    sig = _unknown_signature(ocr_tokens)
    _unknown_signature_history.append(sig)

    consecutive = 0
    for s in reversed(list(_unknown_signature_history)):
        if s == sig:
            consecutive += 1
        else:
            break
    return (consecutive >= _UNKNOWN_DEBOUNCE_THRESHOLD, consecutive)


def reset_unknown_debounce() -> None:
    """Reset the debounce counter — called by tests and by callers that
    know a context-switch just happened (e.g. a successful dismissal,
    a new goal pursuit starting)."""
    global _unknown_signature_history
    _unknown_signature_history = None


def handle_unknown_blocking(frame, ocr_tokens: list, nav_detail: str = "") -> bool:
    """
    Handle an unexpected blocking screen.  Called when state="unknown" after
    all known interruptors have been dismissed.

    Tier 0 — debounce: require N consecutive same-signature unknown
             perceives before invoking the model tiers.  Catches transient
             overlays (NPC speech bubbles, building name plates) and
             mid-animation frames without harmful auto-actions.
    Tier 1 — Qwen (fast, local): describe + simple/complex flag
    Tier 2 — Claude Vision: complex dialogs requiring a decision
    Tier 3 — human escalation: unchanged existing path

    Saves each newly resolved interruptor to interruptors.json so future
    occurrences are caught by pass-1 keyword detection with no model call.

    Returns True if handled (bot can continue), False if debounce hasn't
    triggered yet OR escalation is required.
    """
    from actions.adb_actions import tap
    from actions.sail_actions import _find_button
    import re, time as _time

    # Tier 0: debounce.  Don't fire Qwen on the first unknown frame —
    # transient overlays clear themselves before the threshold is reached.
    should_fire, consecutive = _unknown_debounce_should_fire(ocr_tokens)
    if not should_fire:
        logger.info(
            f"[perceive] Unknown blocking screen — debounce gating "
            f"(consecutive={consecutive}/{_UNKNOWN_DEBOUNCE_THRESHOLD}); "
            "deferring Qwen/Claude — likely transient overlay"
        )
        return False

    logger.info(
        f"[perceive] Unknown blocking screen — invoking learn-and-dismiss "
        f"(consecutive same-sig perceives: {consecutive})"
    )

    # ── Tier 1: Qwen ──────────────────────────────────────────────────────────
    qwen = _qwen_describe_unknown(ocr_tokens, nav_detail)
    description = qwen.get("description", "unknown dialog") if qwen else "unknown dialog"

    # Dismissals split into two classes: those that are SAFE to auto-act on
    # during perception (overlay-style notices that close on any safe tap)
    # vs SEMANTIC actions where tapping the wrong target has consequences
    # (confirm dialogs — tapping OK actually commits a transaction;
    # tapping the body or Cancel undoes it).
    #
    # For semantic dismissals, only LEARN here; let the caller (flow
    # advancer or interactive teach) commit the action with full context.
    # This is the structural fix for the May-2 dismiss-instead-of-OK bug:
    # handle_unknown_blocking was firing inside perceive Pass-1.5 and
    # auto-tapping the OK button via OCR, but when OCR couldn't read
    # 'OK' the centre-tap fallback closed the dialog without confirming.
    # Now perceive only saves the new interruptor; the next perceive
    # cycle's Pass-1 keyword scan will dismiss it correctly via the now-
    # known keywords + dismissal — OR the teach loop's prompt sees the
    # dialog and asks the human what to do.
    SEMANTIC_DISMISSALS = {DISMISSAL_TAP_OK, "tap_collect_or_ok"}

    if qwen and qwen.get("confidence") == CONFIDENCE_HIGH and qwen.get("dismissal") != DISMISSAL_NEEDS_CLAUDE:
        dismissal = qwen.get("dismissal", DISMISSAL_TAP_OK_OR_X)
        logger.info(f"[perceive] Qwen identified: {description!r}  dismissal={dismissal!r}")

        if dismissal in SEMANTIC_DISMISSALS:
            logger.info(
                f"[perceive] Semantic dismissal {dismissal!r} — learning only, "
                "leaving action to caller (avoid auto-confirm via OCR-based tap)"
            )
        else:
            _execute_learned_dismissal(frame, dismissal, tap, _find_button)
            _time.sleep(1.0)

        # Generate a stable id from the description
        iid = re.sub(r"[^a-z0-9]+", "_", description.lower())[:40].strip("_")
        _save_new_interruptor({
            "id":                iid,
            "description":       description,
            "detection_keywords": qwen.get("detection_keywords", []),
            "dismissal":         dismissal,
        }, source="qwen")
        return True

    # ── Tier 2: Claude Vision ─────────────────────────────────────────────────
    logger.info("[perceive] Qwen uncertain or complex dialog — escalating to Claude Vision")
    claude = _claude_resolve_unknown(frame, description)

    if claude:
        dismissal = claude.get("dismissal", DISMISSAL_TAP_OK_OR_X)
        tap_x     = claude.get("tap_x")
        tap_y     = claude.get("tap_y")
        logger.info(
            f"[perceive] Claude identified: {claude.get('description', description)!r}  "
            f"dismissal={dismissal!r}  reasoning={claude.get('reasoning', '')[:60]}"
        )

        if dismissal in SEMANTIC_DISMISSALS and not (tap_x and tap_y):
            # Semantic action with no explicit Claude-supplied coords: don't
            # auto-act.  The new interruptor record still gets saved so the
            # next encounter can reuse the keywords; but committing the
            # confirmation needs explicit coordinates or human confirmation.
            logger.info(
                f"[perceive] Semantic dismissal {dismissal!r} without explicit "
                "tap coords — learning only.  Caller decides the actual tap."
            )
        elif tap_x and tap_y:
            tap(int(tap_x), int(tap_y))
            _time.sleep(1.0)
        else:
            _execute_learned_dismissal(frame, dismissal, tap, _find_button)
            _time.sleep(1.0)

        iid = re.sub(r"[^a-z0-9]+", "_", claude.get("description", description).lower())[:40].strip("_")
        _save_new_interruptor({
            "id":                iid,
            "description":       claude.get("description", description),
            "detection_keywords": claude.get("detection_keywords", []),
            "dismissal":         dismissal,
        }, source="claude")
        return True

    # ── Tier 3: human escalation ───────────────────────────────────────────────
    logger.warning("[perceive] Could not identify unknown blocking screen — falling back to human")
    return False


def _execute_learned_dismissal(frame, dismissal: str, tap_fn, find_button_fn) -> bool:
    """
    Execute a dismissal action by type name.

    Returns True if a real tap was performed at a button found by OCR.
    Returns False when the named button could not be located — in that
    case NO tap is issued (centre-of-frame fallbacks were the source of
    the May-2 00:38 dismiss-instead-of-confirm bug: when 'OK' couldn't
    be OCR'd on the recruit-confirm dialog the centre tap landed on the
    dialog body, the modal closed without confirming, the bot saw the
    parent screen again and looped.

    The previous behaviour silently chose action over inaction; the new
    contract makes inaction explicit so the caller can decide what to
    do (ask Claude for explicit coords, escalate to human, or skip).
    """
    import time as _time

    if dismissal == DISMISSAL_TAP_OK:
        btn = find_button_fn(frame, "ok", "confirm", "yes")
        if not btn:
            logger.warning(
                "[perceive] tap_ok dismissal: 'OK'/'Confirm'/'Yes' not found by "
                "OCR — refusing to tap.  Centre-of-frame fallback was the "
                "May-2 confirm-dialog dismiss-instead-of-OK bug.  Caller "
                "should escalate."
            )
            return False
        tap_fn(*btn)

    elif dismissal in (DISMISSAL_TAP_OK_OR_X, "tap_collect_or_ok", "tap_ok_or_cancel"):
        # NOTE: previous label order put X/close BEFORE ok/confirm, which on a
        # confirm dialog meant Cancel/X was preferred over OK.  Reversed so
        # confirmation wins when both are present.  X/close is the fallback
        # for popups that have only a close button.
        btn = (find_button_fn(frame, "ok", "confirm", "collect") or
               find_button_fn(frame, "×", "x", "close"))
        if not btn:
            logger.warning(
                f"[perceive] {dismissal!r} dismissal: no OK/confirm/collect/X "
                "button found by OCR — refusing to tap centre."
            )
            return False
        tap_fn(*btn)

    elif dismissal == DISMISSAL_TAP_ANYWHERE:
        # Safe by definition — overlay notices that resolve on any tap
        # outside the modal area.  Top-centre is the safest region.
        tap_fn(frame.width // 2, frame.height // 4)

    elif dismissal == DISMISSAL_TAP_CLOSE:
        btn = find_button_fn(frame, "×", "x", "close")
        if not btn:
            logger.warning(
                "[perceive] tap_close dismissal: no close-button found by OCR — "
                "refusing the screen-corner fallback (was the source of the "
                "daily_news mis-tap bug)."
            )
            return False
        tap_fn(*btn)

    else:
        # Unknown dismissal type — try OK, but never centre-fallback.
        btn = find_button_fn(frame, "ok", "confirm")
        if not btn:
            logger.warning(
                f"[perceive] unknown dismissal type {dismissal!r} and no OK "
                "button found — skipping action."
            )
            return False
        tap_fn(*btn)

    _time.sleep(1.0)
    return True


# ── Pass 2: Active flow detection ─────────────────────────────────────────────

# Chrome-level signal: game control bar disappears or becomes untappable
# (semitranslucent curtain) when a flow is active.
# We detect this by checking whether the home button is obscured AND
# specific dialog patterns are present.

def _detect_active_flow(frame, nav_state: str, nav_detail: str) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (flow_id, step_id) if an atomic flow is active, else (None, None).

    Detection strategy:
    - If we're in a building state and a known flow-trigger pattern is visible
      in the screen content, a flow is active.
    - If the screen doesn't match any flow trigger, return (None, None).
    """
    from actions.sail_actions import _ocr_frame, fuzzy_contains
    from brain.kb import control

    if "building" not in nav_state:
        return None, None

    tokens = _ocr_frame(frame, min_conf=0.3)
    text   = " ".join(t.lower() for t, _, _, _ in tokens)

    # Flow detection order and market flow disambiguation set come from KB.
    # No flow IDs, step IDs, or game strings are hardcoded here.
    ckb = control()
    flow_check_order   = ckb.flow_detection_order()   # [(flow_id, step_id), ...]
    market_flow_ids    = ckb.market_flow_ids()         # set of flows needing tab disambiguation
    detail_lower       = nav_detail.lower()

    # Phase 4: skip flows whose history says they are unreliable.  A learned
    # flow that has accumulated FAILURE_DEMOTION_THRESHOLD failures with zero
    # successes is treated as if not present in the KB — Rule 1 then triggers
    # fresh learning rather than replaying a known-broken recipe.
    from brain.fsm_registry import get_fsm_registry
    _fsm = get_fsm_registry()

    for flow_id, step_id in flow_check_order:
        flow_obj = _fsm.flows.get(flow_id)
        if flow_obj is not None and flow_obj.should_skip():
            logger.debug(
                f"[perceive] Skipping demoted flow {flow_id!r} "
                f"(confidence={flow_obj.confidence}, fail={flow_obj.failure_count}, "
                f"success={flow_obj.success_count})"
            )
            continue
        # Flow-completeness guard (CLAUDE.md → "Flow Completeness &
        # Self-Correction"): a flow whose runtime status is INCOMPLETE
        # is not authoritative.  Skipping it here lets the planner take
        # over and the learning hook re-fire on the next encounter.
        from brain.flow_completeness import STATUS_INCOMPLETE as _STATUS_INCOMPLETE
        if flow_obj is not None and getattr(flow_obj, "status", None) == _STATUS_INCOMPLETE:
            logger.debug(
                f"[perceive] Skipping incomplete flow {flow_id!r} "
                f"(transactions={flow_obj.positive_transaction_count}, "
                f"terminal_recognized={flow_obj.terminal_state_recognized}) — "
                "planner / learning hook will re-attempt."
            )
            continue
        keywords = ckb.flow_step_keywords(flow_id, step_id)
        if keywords and any(fuzzy_contains(text, kw) for kw in keywords):
            if flow_id in market_flow_ids:
                # Use nav_detail tab indicator — more reliable than OCR text
                # because the inactive tab label always appears in OCR too.
                if "purchase" in detail_lower:
                    resolved = "market_purchase"
                elif "sell" in detail_lower:
                    resolved = "market_sell"
                else:
                    sell_count = text.count("sell")
                    resolved = "market_sell" if sell_count > 1 else "market_purchase"
            else:
                resolved = flow_id
            return resolved, step_id

    return None, None


# ── Consistency validation ────────────────────────────────────────────────────

def _validate_flow_consistency(
    flow_id: Optional[str],
    flow_step: Optional[str],
    nav_state: str,
    nav_detail: str,
) -> tuple[Optional[str], Optional[str], str]:
    """
    Check whether the detected flow is consistent with the navigation state.
    Returns (flow_id, flow_step, confidence) where confidence is 'high' or 'low'.

    Two checks:
    1. Cross-building: if the flow has parent_state_detail_contains and nav_detail
       names a specific building that is NOT in that list, the flow is a false positive.
    2. Tab disambiguation: for market flows, the nav_detail tab indicator is more
       reliable than OCR keyword matching.

    The flow is cleared (set to None) on any contradiction.
    """
    if flow_id is None:
        return None, None, CONFIDENCE_HIGH

    from brain.kb import control as _ckb
    from brain.fsm_registry import get_fsm_registry

    market_flow_ids = _ckb().market_flow_ids()
    detail_lower    = nav_detail.lower()

    # ── Check 1: cross-building contradiction ─────────────────────────────────
    # If the flow expects specific building contexts (e.g. market_purchase
    # expects ["market"]) and NONE of them appear in the nav_detail, the flow
    # was almost certainly false-detected from generic keywords (e.g. "decline"
    # in an interview dialog matching market_purchase/negotiate).
    # Clear it regardless of whether we can identify the actual building —
    # the absence of the expected context is sufficient evidence.
    flow = get_fsm_registry().flows.get(flow_id)
    if flow:
        expected_contexts = flow._raw.get("parent_state_detail_contains", [])
        if expected_contexts and not any(ctx in detail_lower for ctx in expected_contexts):
            logger.warning(
                f"[perceive] Flow {flow_id!r} expects building context "
                f"{expected_contexts} but nav_detail={nav_detail!r} — "
                "clearing flow (confidence=low)"
            )
            return None, None, CONFIDENCE_LOW

    # ── Check 2: market tab disambiguation ───────────────────────────────────
    if flow_id in market_flow_ids:
        # purchase tab → can only be market_purchase
        if "purchase" in detail_lower and flow_id != "market_purchase":
            logger.warning(
                f"[perceive] Flow {flow_id!r} contradicts nav_detail {nav_detail!r} "
                "— clearing flow (confidence=low)"
            )
            return None, None, CONFIDENCE_LOW
        # sell tab → can only be market_sell
        if "sell" in detail_lower and flow_id != "market_sell":
            logger.warning(
                f"[perceive] Flow {flow_id!r} contradicts nav_detail {nav_detail!r} "
                "— clearing flow (confidence=low)"
            )
            return None, None, CONFIDENCE_LOW

    return flow_id, flow_step, CONFIDENCE_HIGH


# ── Claude re-classification ──────────────────────────────────────────────────

def reclassify_with_claude(frame, hint: "PerceiveResult") -> "PerceiveResult":
    """
    Ask Claude Vision to determine the true current state.
    Used by recovery when local perception is low-confidence or stalled.

    Returns a new PerceiveResult with corrected fields and corrected=True.
    Falls back to the original hint if the API is unavailable.
    """
    import base64, io, json as _json
    from vision.claude_vision import ClaudeVision

    cv = ClaudeVision()
    client = cv._get_client()
    if client is None:
        logger.warning("[perceive] Claude Vision unavailable — returning original perception")
        return hint

    # Resize to a small thumbnail for the API call (faster, cheaper)
    thumb = frame.copy()
    thumb.thumbnail((800, 400))
    buf = io.BytesIO()
    thumb.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    prompt = f"""\
The bot is stuck. Local perception returned:
  state={hint.state!r}  flow={hint.flow!r}  step={hint.flow_step!r}
  detail={hint.detail!r}  confidence={hint.confidence!r}

Look at this screenshot from Uncharted Waters Origin (UWO) and determine the ACTUAL current state.

Return ONLY a JSON object:
{{
  "state": "<port_overworld|sea|sea_cinematic|building|world_map|loading|unknown>",
  "flow": "<market_purchase|market_sell|harbor_departure|null>",
  "flow_step": "<basket|confirm|negotiate|result|null>",
  "blocking": "<blocking condition text visible on screen, e.g. 'Not Enough Crew', or null>",
  "detail": "<brief description of what you see>",
  "reasoning": "<why you chose these values>"
}}

State definitions:
- port_overworld: town overworld, NPCs visible, no building interior
- building: inside any building (market, harbour, bank, etc.)
- sea: sailing on open water, HUD visible
- world_map: the full world map is open
- loading: loading/transition screen

IMPORTANT flow rules:
- A flow is ONLY active when a modal dialog (confirmation, result, negotiation) is open.
- Seeing a button label like "Depart Now" on the harbor screen does NOT mean the
  harbor_departure flow is active. The flow starts AFTER tapping Depart Now successfully,
  when a departure confirmation dialog appears.
- If a button appears greyed out / disabled, or a blocking message is visible
  (e.g. "Not Enough Crew", "Not Enough Supply"), set flow=null and report the
  blocking text in the "blocking" field. The bot cannot proceed until the blocker
  is resolved.
- Harbor supply screen (water/food/ammo/materials) is NOT a market — do not set
  flow to market_purchase or market_sell.
"""

    try:
        response = client.messages.create(
            model=cv.model,
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = _json.loads(raw)

        # If Claude reports a blocking condition, there is no active flow —
        # the bot must resolve the blocker first.  Include the blocking text
        # in the detail so the goal layer can act on it.
        blocking = data.get("blocking")
        flow     = data.get("flow") or None
        detail   = data.get("detail", hint.detail)
        if blocking:
            logger.info(
                f"[perceive] Claude reclassification: BLOCKED by {blocking!r}"
            )
            flow = None
            detail = f"{detail} [BLOCKED: {blocking}]"

        logger.info(
            f"[perceive] Claude reclassification: "
            f"state={data.get('state')!r}  flow={flow!r}  "
            f"step={data.get('flow_step')!r}  — {data.get('reasoning', '')[:80]}"
        )
        return PerceiveResult(
            state      = data.get("state", hint.state),
            port       = hint.port,
            detail     = detail,
            flow       = flow,
            flow_step  = data.get("flow_step") or None if flow else None,
            confidence = CONFIDENCE_HIGH,
            corrected  = True,
        )
    except Exception as e:
        logger.warning(f"[perceive] Claude reclassification failed: {e} — using original")
        return hint


# ── Public interruptor dismissal helper ──────────────────────────────────────

# Fix D: cross-call no-op tracker.  When the same interruptor is detected
# and "dismissed" on consecutive perceive() calls without making progress
# (the post-dismissal frame still shows the same interruptor with the
# same OCR signature), the dismissal action is a no-op — likely the
# close-button coordinate is stale or the dialog's structure changed.
# Without tracking, the bot loops 30s+ per perceive cycle dismissing
# the same dialog over and over (live log 2026-05-04, 16:43-16:45).
#
# This counter persists across calls; entries are reset when the
# interruptor stops appearing or its signature changes.
_DISMISSAL_NOOP_COUNTERS: dict[str, int] = {}
_DISMISSAL_LAST_SIGNATURE: dict[str, str] = {}
_NOOP_WARN_THRESHOLD = 2     # log loudly after this many consecutive no-ops
_NOOP_SKIP_THRESHOLD = 3     # skip subsequent attempts after this many


def _signature_for_dismissal(tokens) -> str:
    """Cheap signature of the OCR tokens — used to decide if a dismissal
    actually changed the screen."""
    parts = sorted({str(t[0]).lower().strip() for t in tokens})
    return "|".join(parts)[:200]


def _reset_dismissal_tracker(iid: str) -> None:
    _DISMISSAL_NOOP_COUNTERS.pop(iid, None)
    _DISMISSAL_LAST_SIGNATURE.pop(iid, None)


def dismiss_interruptors(frame=None):
    """
    Detect and dismiss all known interruptors on the current screen.
    State-agnostic: works at sea, in port, in buildings — anywhere.

    Scans the FULL frame (not just the centre crop) so that dialogs whose title
    sits in the top quarter of the screen (e.g. Perk Event, Attendance) are still
    detected even when their body spans the whole screen.

    Runs up to MAX_INTERRUPTOR_ROUNDS times, re-capturing after each dismissal
    batch, until no known interruptors remain.  Returns the latest frame.

    Fix D: a no-op tracker breaks the cross-call loop where the same
    interruptor is detected and "dismissed" repeatedly without changing
    the screen.  After _NOOP_SKIP_THRESHOLD consecutive no-ops, that
    interruptor's dismissal is skipped so the perceive cycle can move on.
    """
    from capture.adb_capture import capture_screen as _cap
    from actions.sail_actions import _ocr_frame

    if frame is None:
        frame = _cap()

    MAX_ROUNDS = 5
    for round_idx in range(MAX_ROUNDS):
        tokens = _ocr_frame(frame, min_conf=0.3)   # full frame — not centre crop
        found, obstruction = _detect_interruptors(frame, tokens)
        obstruction_bbox = obstruction.bbox if obstruction is not None else None

        # Phase 4: when no top-layer interruptor matched, fall through to
        # learned-recovery matching so a recipe the user already taught us
        # (e.g. recruit_crew_confirmation_dialog) fires here instead of
        # waiting for the 5-minute recovery timeout.  Layered AFTER
        # interruptors because learned recoveries target the UNDERLYING
        # screen — taps would hit the wrong layer if dismissed first.
        recoveries: list = []
        if not found:
            recoveries = _match_learned_recoveries(tokens)

        if not found and not recoveries:
            break

        # Fix D: pre-screen any interruptors whose dismissal has been a
        # no-op too many times in a row.  Skip them here so we don't
        # keep firing the same broken close-button tap on every call.
        for iid in found:
            noop_count = _DISMISSAL_NOOP_COUNTERS.get(iid, 0)
            if noop_count >= _NOOP_SKIP_THRESHOLD:
                logger.warning(
                    f"[perceive] STUCK DISMISSAL: {iid!r} dismissal is a "
                    f"no-op ({noop_count} consecutive attempts didn't "
                    "change the screen).  Skipping further attempts in "
                    "this cycle so the bot can proceed; the interruptor "
                    "will surface in result.interruptors for the caller "
                    "to handle.  Likely cause: stale close_position, UI "
                    "drift, or a different dialog now occupying the same "
                    "OCR signature."
                )
                continue
            logger.info(f"[perceive] Interruptor detected: {iid!r} — dismissing")
            pre_sig = _signature_for_dismissal(tokens)
            _dismiss_interruptor(iid, frame, obstruction_bbox=obstruction_bbox)
            # Compare post-dismissal signature.  If unchanged, this attempt
            # was a no-op — bump the counter so we eventually back off.
            try:
                post_frame = _cap()
                post_sig = _signature_for_dismissal(_ocr_frame(post_frame, min_conf=0.3))
                if post_sig == pre_sig:
                    _DISMISSAL_NOOP_COUNTERS[iid] = noop_count + 1
                    _DISMISSAL_LAST_SIGNATURE[iid] = pre_sig
                    if _DISMISSAL_NOOP_COUNTERS[iid] >= _NOOP_WARN_THRESHOLD:
                        logger.warning(
                            f"[perceive] Dismissal no-op: {iid!r} did NOT "
                            f"change the OCR signature "
                            f"({_DISMISSAL_NOOP_COUNTERS[iid]} consecutive "
                            "no-op(s)).  Will skip this dismissal after "
                            f"{_NOOP_SKIP_THRESHOLD} no-ops total."
                        )
                else:
                    # Signature moved — dismissal worked, reset counter.
                    _reset_dismissal_tracker(iid)
            except Exception as e:
                logger.debug(f"[perceive] dismissal no-op tracking failed: {e}")
        # Phase 4 loop guard: before firing any matched recovery, ask the
        # tracker whether the same recipe has already been attempted from
        # this exact screen signature 2× without progress.  If so, skip
        # this fire and persist confidence='low' so future processes don't
        # repeat the loop.  Catches recipes that the human or Claude
        # approved at teaching time but that have stopped working in
        # practice (UI drift, overfit keywords, partial recipes that
        # never actually completed a flow).
        from brain.human_escalation import (
            _execute_plan, _signature_from_tokens, should_skip_recovery_for_loop,
        )
        sig = _signature_from_tokens(tokens)
        for plan in recoveries:
            if should_skip_recovery_for_loop(plan.scenario_id, sig):
                logger.warning(
                    f"[perceive] Skipping {plan.scenario_id!r} — auto-disabled "
                    "after consecutive failures from the same screen signature"
                )
                continue
            logger.info(
                f"[perceive] Learned recovery matched proactively: "
                f"{plan.scenario_id!r} — executing {len(plan.actions)} action(s)"
            )
            _execute_plan(plan)
        frame = _cap()

    return frame


def _match_learned_recoveries(ocr_tokens: list) -> list:
    """
    Proactive learned-recovery matching: load learned_recoveries.json and
    return any entries whose detection_keywords are all present in the
    current OCR text.  Mirror of _match_learned_recovery in
    human_escalation.py but matches against raw OCR tokens (more direct
    than state+detail, which has not yet been computed at this point in
    perceive).

    Returns a list of EscalationPlan objects ready for _execute_plan.
    Empty list when nothing matches OR the file is absent.

    Promotes human-taught recoveries from "fires only after 5-minute
    timeout" (current behaviour: human_escalation calls
    _match_learned_recovery in its fallback path) to "fires on the next
    perceive iteration", removing the long blocked-bot wait the user
    observed in the May-1 14:23 run.
    """
    import json as _json
    from pathlib import Path as _Path
    from brain.human_escalation import EscalationPlan, ActionStep

    path = _Path("memory/knowledge/fsm/learned_recoveries.json")
    if not path.exists():
        return []
    try:
        entries = _json.loads(path.read_text())
    except Exception as e:
        logger.debug(f"[perceive] cannot read learned_recoveries.json: {e}")
        return []

    from brain.human_escalation import _entry_is_demoted, _session_disabled_recoveries

    text = " ".join(t.lower() for t, _, _, _ in ocr_tokens)
    matched: list = []
    for entry in entries:
        # Phase 4: skip persistent-demoted entries (confidence='low') and
        # any entry auto-disabled this session by the loop guard.
        if _entry_is_demoted(entry):
            continue
        if entry.get("id") in _session_disabled_recoveries:
            continue
        keywords = entry.get("detection_keywords", [])
        if not keywords:
            continue
        if all(kw.lower() in text for kw in keywords):
            actions = [ActionStep(**a) for a in entry.get("actions", [])]
            matched.append(EscalationPlan(
                scenario_id        = entry["id"],
                category           = entry.get("category", "other"),
                description        = entry.get("description", ""),
                actions            = actions,
                detection_keywords = keywords,
                save_as_knowledge  = False,   # already saved
            ))
    return matched


# ── Pass 3: Navigation state ──────────────────────────────────────────────────


def _classify_nav_state(frame) -> dict:
    """
    Classify a frame into a navigation state, returning the same dict shape
    where_am_i() used to produce: { "location", "port", "detail" }.

    Phase 5a addition: every branch now logs at INFO level so failures are
    diagnosable without re-running with debug.  When the classifier returns
    'unknown' the log trail shows exactly which checks were attempted and
    what each saw — chrome flags, port name read result, right-panel
    template, OCR menu tokens, Moondream verdict.

    The fallback path's detail wording was rewritten (Phase 5a #2): the
    previous text contained the literal phrase "vision check says NOT at
    sea" which was being ingested by Qwen in handle_unknown_blocking and
    paraphrased back as a screen description, then saved as a phantom
    interruptor.  Neutral wording removes that feedback loop.
    """
    from vision.chrome_detector import get_chrome_detector
    from vision.ocr import read_port_name
    from actions.sail_actions import (
        _ocr_frame, _is_loading_screen, _SEA_HUD_TOKENS,
    )
    from utils.fuzzy import fuzzy_contains

    # ── Phase 4a: family classifier short-circuit ─────────────────────────
    # Whole-image MobileNetV3 classifier returns the nav-state family
    # (sea / port_overworld / world_map / chromed / transient) in ~50 ms.
    # Short-circuit three families:
    #   sea, world_map  → return immediately, no further detail needed
    #   port_overworld  → return after a cheap OCR for the port name —
    #                     no need to re-run the full classification
    #                     cascade; family already told us where we are
    #
    # chromed and transient fall through to the existing cascade because
    # they need finer Stage-2 detail (which building / sub_menu / which
    # overlay).  Family verdict is also stashed for the post-cascade
    # tie-breaker that catches building-vs-port_overworld
    # misclassifications.
    #
    # Confidence floor 0.7 — anything below defers to the cascade.
    # See vision/family_classifier.py and Phase 4a in the architecture
    # overview doc.
    family_verdict = None
    try:
        from vision.family_classifier import classify_family
        family_verdict = classify_family(frame)
        if family_verdict.confidence >= 0.7:
            if family_verdict.family in ("sea", "world_map"):
                logger.info(
                    f"[classify] → {family_verdict.family} (family-classifier "
                    f"conf={family_verdict.confidence:.2f}) — short-circuit"
                )
                return {
                    "location": family_verdict.family,
                    "port":     None,
                    "detail":   (
                        f"{family_verdict.family} (family-classifier@"
                        f"{family_verdict.confidence:.2f})"
                    ),
                }
            if family_verdict.family == "port_overworld":
                # Cheap OCR for the port name; correct against the
                # catalogue.
                from vision.text_correction import (
                    correct_port_name, correct_village_name,
                )
                raw_port = read_port_name(frame)

                # Village arrival check — the family classifier was
                # trained to lump village interiors under port_overworld
                # (visually similar outdoor scenes), but villages have a
                # distinct top-left title that matches the village
                # catalogue.  Without this, SailToGoal.is_complete never
                # fires on village arrival because the FSM only marks
                # ARRIVED on state ∈ {port_overworld, village} AND port
                # matches destination — and 'Berber Village' fuzzy-
                # corrected as a port returns 'Las Palmas' (nearest
                # match), losing the village identity.  Origin:
                # 2026-05-23 Berber sail, where the bot reached the
                # village but kept running.
                if raw_port:
                    village, vratio = correct_village_name(raw_port)
                    if village is not None:
                        logger.info(
                            f"[classify] → village {village!r} "
                            f"(family-classifier said port_overworld@"
                            f"{family_verdict.confidence:.2f}, but top-"
                            f"left {raw_port!r} matched village catalogue "
                            f"@ {vratio:.2f})"
                        )
                        return {
                            "location": "village",
                            "port":     village,
                            "detail":   f"Village interior: {village}",
                        }

                # Generic 'Village' title — in-game village screens
                # show literally "Village" as the top-left text, not
                # the village's proper name.  The village catalogue
                # has entries like "Berber Village" so fuzzy-matching
                # 'Village' alone scores below cutoff (and per the
                # `_is_generic_title` guard in text_correction, we
                # don't try).  Recognise as `village` anyway when the
                # raw OCR is the bare word — identity will be
                # inferred from goal context by SailToGoal.  Origin:
                # 2026-05-23 Berber sail; without this branch the
                # code fell into the port_overworld block below and
                # the bot believed it was at port='Village'.
                from vision.text_correction import _is_generic_title as _is_gen
                if raw_port and _is_gen(raw_port) and raw_port.strip().lower() == "village":
                    logger.info(
                        f"[classify] → village (generic title {raw_port!r}, "
                        "no specific village name available from OCR; "
                        f"family-classifier said port_overworld@"
                        f"{family_verdict.confidence:.2f}; identity must "
                        "come from goal context)"
                    )
                    return {
                        "location": "village",
                        "port":     None,
                        "detail":   "Village interior (identity unknown — generic title)",
                    }

                # Sea-zone-label guard — if the OCR'd title matches a
                # sea-zone word ('Dangerous Waters', 'Safe Waters',
                # etc.), the family classifier is wrong; this is
                # actually a sea frame and the title text is the zone
                # label, not a port name.  Skip the short-circuit and
                # let the cascade below run — its HUD-token override
                # (chrome.has_right_panel missing + sea-HUD tokens)
                # correctly reclassifies.  Same defence as the cascade
                # at line ~2530.  Origin: 2026-05-23 Berber sail, frame
                # at 14:25:39 misclassified port_overworld@1.00 with
                # OCR'd port='Dangerous Waters'.
                defer_to_cascade = False
                if raw_port:
                    from brain.kb import control
                    _ZONE_WORDS = control().zone_words()
                    matched_zone = [w for w in _ZONE_WORDS
                                    if w in raw_port.lower()]
                    if matched_zone:
                        logger.info(
                            f"[classify] family-classifier said "
                            f"port_overworld@{family_verdict.confidence:.2f} "
                            f"but top-left {raw_port!r} matches zone-word "
                            f"{matched_zone} — deferring to cascade for sea "
                            "reclassification"
                        )
                        defer_to_cascade = True

                if not defer_to_cascade:
                    port, ratio = (correct_port_name(raw_port)
                                   if raw_port else (None, 0.0))
                    final_port = port or raw_port or None
                    logger.info(
                        f"[classify] → port_overworld (family-classifier "
                        f"conf={family_verdict.confidence:.2f}, "
                        f"port={final_port!r}) — short-circuit"
                    )
                    return {
                        "location": "port_overworld",
                        "port":     final_port,
                        "detail":   (
                            f"port_overworld (family-classifier@"
                            f"{family_verdict.confidence:.2f}, "
                            f"port={final_port!r})"
                        ),
                    }
    except Exception as exc:
        logger.debug(f"[classify] family classifier skipped: {exc}")

    # ── Phase 5e L1: OmniParser-primary classifier ──────────────────────────
    # OmniParser detects elements with bounding boxes regardless of contrast /
    # font / phone-resolution drift; matching those elements against state
    # fingerprints (normalised regions + label content) is more robust than
    # the chrome-template + fixed-crop-OCR + Moondream chain that follows.
    # Run it first; on any non-unknown verdict, return immediately.  Fall
    # through to the legacy chain only when OmniParser is unavailable or no
    # fingerprint matches.
    #
    # Side effect: capture the element count so the legacy chain's fallback
    # path can distinguish "lots of UI but unrecognised" (call Moondream as
    # tiebreaker — Phase 5B in-world-overlay recovery) from "barely any UI,
    # 3D world still rendering" (return `pending`, caller polls — Slice 1
    # of four_layer_nav_classification.md, 2026-05-12 reload-cascade fix).
    omni_elements_count: Optional[int] = None
    try:
        from vision.omniparser import get_omniparser, parse_fast_cached
        from vision.screen_classifier import classify_screen
        if get_omniparser().yolo_available():
            try:
                # Use the comprehensive port catalogue (224 ports) for
                # title-name matching rather than PORT_POSITIONS (83
                # ports, screen-pixel positions for world-map taps).
                # The two drifted: ports like Tripoli have game-coord
                # entries in the larger catalogue but no pixel position,
                # so the classifier was returning port_overworld with
                # port=None even when OCR read the title correctly.
                from vision.world_map_parser import load_port_catalogue
                _kp = [k for k in load_port_catalogue().keys()
                       if not k.startswith("_")]
            except Exception:
                _kp = None
            elements = parse_fast_cached(frame)
            omni_elements_count = len(elements) if elements is not None else None
            sc = classify_screen(frame, elements, known_ports=_kp)
            if sc.state != "unknown":
                logger.info(
                    f"[classify] → {sc.state} (omniparser, conf={sc.confidence}, "
                    f"signals={sc.signals})"
                )
                return {
                    "location": sc.state,
                    "port":     sc.port,
                    "detail":   sc.detail,
                }
            logger.info(
                f"[classify] omniparser fingerprint did not match — "
                f"falling back to legacy chain ({sc.detail})"
            )
    except Exception as e:
        logger.debug(f"[classify] omniparser-primary skipped: {e}")

    chrome = get_chrome_detector().detect(frame)
    logger.info(
        f"[classify] chrome: home={chrome.has_home} back={chrome.has_back_arrow} "
        f"hamburger={chrome.has_hamburger} right_panel={chrome.has_right_panel}"
    )

    # World map — top-left title region first; world map shares Home button
    # with buildings so has_home is not a discriminator on its own.
    title_crop = frame.crop((0, 0, int(frame.width * 0.35), 80))
    title_tokens = _ocr_frame(title_crop, min_conf=0.25)
    title_text = " ".join(t.lower() for t, _, _, _ in title_tokens)
    if fuzzy_contains(title_text, "world map"):
        logger.info(f"[classify] → world_map (title text {title_text!r})")
        return {"location": "world_map", "port": None,
                "detail": f"World map title in top-left: {title_text!r}"}

    # Building interior — has_home + world_map already ruled out.
    if chrome.has_home:
        from vision.ocr import read_screen_title
        bld_title = read_screen_title(frame)
        detail = f"building: {bld_title}" if bld_title else "building (title unreadable)"
        logger.info(f"[classify] → building (has_home=True, title={bld_title!r})")
        return {"location": "building", "port": None, "detail": detail}

    if _is_loading_screen(frame):
        logger.info("[classify] → loading (loading-screen pattern matched)")
        return {"location": "loading", "port": None,
                "detail": "Transition loading screen"}

    # Phase 3.3 — Moondream family cache as EARLY ARBITER
    # ────────────────────────────────────────────────────
    # When chrome is all-False the deterministic rules become unreliable
    # (transition frames, in-world overlays, sea-after-depart).  Yesterday's
    # bug ("Safe" classified as a port name) lived in this branch.  The
    # cache holds Moondream's family verdict and is invalidated on events
    # (set sail, arrival, task start) or after a 10-minute TTL.  Caller
    # pays at most ~5s per ~10 minutes of bot runtime, not per frame.
    chrome_strong = (
        chrome.has_home
        or chrome.has_back_arrow
        or chrome.has_hamburger
        or chrome.has_right_panel
    )
    # Skip the Moondream early-arbiter on obviously-transient frames:
    # if chrome is all-False AND OmniParser found ≤ 2 elements, the 3D
    # world is still rendering and the eventual answer is `pending`
    # regardless.  Asking Moondream costs ~10-12 s for in_town? + at_sea?
    # and both will come back "no" on a half-rendered frame.  Observed
    # live in the 2026-05-22 Berber sail right after the supply-departure
    # tap: 1 OmniParser element, chrome all-False, Moondream spent 12 s
    # answering "No / No" before the pending gate eventually fired.
    transient_frame = (
        omni_elements_count is not None and omni_elements_count <= 2
    )
    if not chrome_strong and transient_frame:
        logger.info(
            f"[classify] skipping Moondream early-arbiter — chrome all-False "
            f"and OmniParser yielded only {omni_elements_count} element(s); "
            "transient frame, falling through to pending gate"
        )
    elif not chrome_strong:
        from brain import moondream_family_cache as _mfc
        cached = _mfc.get_family()
        if cached is None:
            # Cache cold / stale — call Moondream once and store the verdict.
            try:
                from actions.sail_actions import (
                    _confirm_in_town_moondream, _confirm_at_sea_moondream,
                )
                in_town_v = _confirm_in_town_moondream(frame)
                at_sea_v  = _confirm_at_sea_moondream(frame)
                logger.info(
                    f"[classify] Moondream early-arbiter: "
                    f"in_town={in_town_v!r} at_sea={at_sea_v!r}"
                )
                if in_town_v is True:
                    _mfc.set_family("in_town")
                    cached = "in_town"
                elif at_sea_v is True:
                    _mfc.set_family("at_sea")
                    cached = "at_sea"
                # Both no/None → leave cache cold, fall through to text branches.
            except Exception as e:
                logger.debug(f"[classify] Moondream early-arbiter skipped: {e}")
        else:
            logger.info(
                f"[classify] Moondream family cache hit: {cached!r} "
                f"(skipping live call)"
            )

        if cached == "in_town":
            return {
                "location": "port_overworld", "port": None,
                "detail": "port_overworld (Moondream family arbiter — chrome all-False)",
            }
        if cached == "at_sea":
            return {
                "location": "sea", "port": None,
                "detail": "sea (Moondream family arbiter — chrome all-False)",
            }

    # Port overworld — port name top-left is the strongest signal.
    # Two guards: zone-label keyword filter, and right-panel chrome check.
    from brain.kb import control
    _ZONE_WORDS = control().zone_words()
    port = read_port_name(frame)
    logger.info(f"[classify] read_port_name → {port!r}")

    # Village interior — back arrow + village name top-left.  Villages
    # share the building_interior chrome shape (back arrow + title +
    # home button + right-side menu list) but the top-left text matches
    # the village catalogue, not the port catalogue.  Check this BEFORE
    # the port_overworld fallback so the bot recognises arrival at a
    # village instead of misreading the village name as a garbled port.
    if port and chrome.has_back_arrow:
        from vision.text_correction import correct_village_name, _is_generic_title
        village, vratio = correct_village_name(port)
        if village is not None:
            logger.info(
                f"[classify] → village {village!r} "
                f"(top-left {port!r} matched village catalogue @ {vratio:.2f})"
            )
            return {"location": "village", "port": village,
                    "detail": f"Village interior: {village}"}
        # Generic 'Village' title — see family-classifier branch above
        # for full rationale.  Identity comes from goal context.
        if _is_generic_title(port) and port.strip().lower() == "village":
            logger.info(
                f"[classify] → village (generic title {port!r}, no specific "
                "village name available from OCR; identity must come from "
                "goal context)"
            )
            return {"location": "village", "port": None,
                    "detail": "Village interior (identity unknown — generic title)"}

    if port:
        is_zone_label = any(w in port.lower() for w in _ZONE_WORDS)
        if is_zone_label:
            logger.info(
                f"[classify] port name {port!r} matches zone-label keywords "
                f"{[w for w in _ZONE_WORDS if w in port.lower()]} — discarding, "
                "falling through to sea checks"
            )
            # garbled sea zone reading — fall through to sea checks
        elif not chrome.has_right_panel:
            # Right panel absent — the port_overworld evidence is weak
            # (just a top-left text reading).  Before committing to
            # port_overworld, check whether this is actually the sea
            # screen with the port name being a misread of a sea-zone
            # label.  Triggered by a real run where OCR captured "Safe"
            # from "Safe Waters" and the classifier committed to
            # port_overworld with port='Safe', then the bot got stuck
            # looking for a Harbor building that didn't exist.
            tokens_for_sea = _ocr_frame(frame, min_conf=0.3)
            full_for_sea  = " ".join(t.lower() for t, _, _, _ in tokens_for_sea)
            matched_sea_hud_early = [
                kw for kw in _SEA_HUD_TOKENS
                if fuzzy_contains(full_for_sea, kw)
            ]
            if matched_sea_hud_early:
                logger.info(
                    f"[classify] right_panel missing AND sea HUD tokens "
                    f"matched {matched_sea_hud_early} — overriding port "
                    f"name {port!r} (likely 'Safe' from 'Safe Waters' etc.); "
                    "classifying as sea"
                )
                detail = next(
                    (t for t, _, _, _ in tokens_for_sea
                     if any(kw in t.lower() for kw in ("day", "sailing", "eta"))),
                    "",
                )
                return {"location": "sea", "port": None,
                        "detail": f"Sailing HUD visible — {detail}".strip(" —")}

            # Not on sea — distinguish port_overworld from main_menu.
            from vision.ocr import read_building_menu
            _MAIN_MENU_KEYWORDS = control().main_menu_keywords()
            bld_labels = {lbl.lower() for lbl, *_ in read_building_menu(frame)}
            menu_overlap = bld_labels & _MAIN_MENU_KEYWORDS
            logger.info(
                f"[classify] right_panel template miss; main_menu check: "
                f"bld_labels={sorted(bld_labels)[:10]} overlap={sorted(menu_overlap)}"
            )
            if len(menu_overlap) >= 2:
                logger.info(f"[classify] → main_menu (items: {sorted(menu_overlap)})")
                return {"location": "main_menu", "port": None,
                        "detail": f"Main menu open (items: {sorted(menu_overlap)})"}
            logger.info(
                f"[classify] → port_overworld via port name {port!r} "
                "(right panel unconfirmed)"
            )
            return {"location": "port_overworld", "port": port,
                    "detail": f"Port name in top-left: {port} (right panel unconfirmed)"}
        else:
            logger.info(f"[classify] → port_overworld via port name {port!r}")
            return {"location": "port_overworld", "port": port,
                    "detail": f"Port name in top-left: {port}"}

    # Open sea — sailing HUD tokens on full-frame OCR.
    tokens = _ocr_frame(frame, min_conf=0.3)
    full   = " ".join(t.lower() for t, _, _, _ in tokens)
    matched_sea_hud = [kw for kw in _SEA_HUD_TOKENS if fuzzy_contains(full, kw)]
    if matched_sea_hud:
        detail = next((t for t, _, _, _ in tokens
                       if any(kw in t.lower() for kw in ("day", "sailing", "eta"))), "")
        logger.info(f"[classify] → sea (HUD tokens matched: {matched_sea_hud})")
        return {"location": "sea", "port": None,
                "detail": f"Sailing HUD visible — {detail}".strip(" —")}

    # Fallback path: no chrome, no port, no sea HUD.
    #
    #   1. OCR cross-check: if the visible text contains common menu/dialog
    #      button labels it is definitely not the open ocean regardless of
    #      what any vision model says.  Cheap; rejects false positives.
    #
    #   2. Moondream vision check: when OCR doesn't conclusively say "menu",
    #      ask the small vision model whether the frame is actually open
    #      ocean.  On "no" → unknown.  On "yes" or model unavailable →
    #      preserve existing fall-through to sea_cinematic.
    _MENU_BUTTON_TOKENS = (
        "recruit", "cancel", "confirm", "depart now",
        "purchase", "sell", "supply",
        "fleet crew", "standby crew", "redistribute crew",
    )
    text_full = " ".join(t.lower() for t, _, _, _ in tokens)
    matched_menu = [w for w in _MENU_BUTTON_TOKENS if w in text_full]
    if len(matched_menu) >= 2:
        logger.info(
            f"[classify] → unknown (menu-token check fired with {matched_menu})"
        )
        return {
            "location": "unknown",
            "port":     None,
            "detail":   f"Fallback path — OCR shows menu tokens {matched_menu!r}, "
                        "rejecting sea_cinematic classification",
        }

    # ── Slice 1: `pending` gate (2026-05-12) ─────────────────────────────────
    # Before invoking Moondream, check whether ANY structural signal fired.
    # If chrome is all-False, port name unreadable, no sea HUD, no menu
    # tokens, AND OmniParser saw very few elements, the 3D world is still
    # rendering after a reload / scene transition.  Asking Moondream
    # "at sea?" on such a frame is the wrong question — both port and sea
    # overworlds show ship-on-water, and the deciding signal (UI chrome)
    # simply hasn't drawn yet.  Return `pending` so the caller polls
    # rather than guesses.  See docs/four_layer_nav_classification.md.
    chrome_has_any_signal = (
        chrome.has_home or chrome.has_back_arrow
        or chrome.has_hamburger or chrome.has_right_panel
    )
    sparse_elements = (
        omni_elements_count is not None and omni_elements_count < 5
    )
    if (not chrome_has_any_signal
            and not port
            and not matched_sea_hud
            and not matched_menu
            and sparse_elements):
        logger.info(
            f"[classify] → pending (chrome all-False, no port name, no sea HUD, "
            f"no menu tokens, only {omni_elements_count} OmniParser elements — "
            "3D world still rendering, caller should poll)"
        )
        return {
            "location": "pending",
            "port":     None,
            "detail":   "Scene still rendering — chrome not drawn, OmniParser "
                        f"yielded {omni_elements_count} element(s); caller "
                        "should re-capture and re-classify",
        }

    # PHASE 5B: vision-led fallback.  Before committing to 'unknown' or
    # 'sea_cinematic', ask Moondream the targeted "is in town?" question.
    # Reliable on labeled frames where in-world overlays (name plates, NPC
    # bubbles) obscure the chrome.  Order matters:
    #   in_town YES  → port_overworld (recovers from chrome misses)
    #   at_sea  YES  → sea_cinematic (preserves the prior fallback)
    #   both NO       → unknown (real candidate for handle_unknown_blocking)
    #
    # Moondream is reached only when the `pending` gate above did NOT fire,
    # i.e. there is at least some structural evidence on screen (chrome
    # signal, non-empty OCR, or rich OmniParser element list).  This
    # preserves the in-world-overlay recovery use case while preventing the
    # reload-cinematic false positive.
    in_town: Optional[bool] = None
    at_sea:  Optional[bool] = None
    try:
        from actions.sail_actions import _confirm_in_town_moondream, _confirm_at_sea_moondream
        in_town = _confirm_in_town_moondream(frame)
        logger.info(f"[classify] Moondream in-town-check returned {in_town!r}")
        if in_town is True:
            return {
                "location": "port_overworld",
                "port":     None,
                "detail":   "port_overworld_with_overlay — vision-led fallback "
                            "(rule-based chrome/port-name miss; Moondream "
                            "confirmed town view).  Likely an in-world overlay "
                            "(building name plate, NPC speech bubble) is "
                            "obscuring the standard chrome-detection signals.",
            }
        # in_town is False or None — try the sea check
        at_sea = _confirm_at_sea_moondream(frame)
        logger.info(f"[classify] Moondream sea-check returned {at_sea!r}")
        if at_sea is False:
            # Neither in-town nor at-sea — return unknown with neutral
            # wording so handle_unknown_blocking's debounce can decide.
            return {
                "location": "unknown",
                "port":     None,
                "detail":   "unrecognised layout — no chrome, no port HUD, "
                            "no sea HUD; classifier could not determine state",
            }
    except Exception as e:
        logger.debug(f"[perceive] vision-led fallback skipped: {e}")

    logger.info(
        f"[classify] → sea_cinematic (fallback; in_town={in_town!r} "
        f"at_sea={at_sea!r}; no positive signals)"
    )
    return {"location": "sea_cinematic", "port": None,
            "detail": "No HUD, no port name — likely idle cinematic or arrival overlay"}


def _location_to_family(location: str) -> Optional[str]:
    """Map _classify_nav_state result locations to Moondream family verdicts.

    Returns None for transient/uncertain locations (loading, pending,
    unknown, sea_cinematic) — those don't contradict either cached
    verdict, so we don't touch the cache.
    """
    in_town_states = {
        "port_overworld", "building", "sub_menu",
        "port_map", "world_map", "main_menu",
    }
    if location in in_town_states:
        return "in_town"
    if location == "sea":
        return "at_sea"
    return None


def _reconcile_moondream_family_cache(location: str) -> None:
    """Post-classification cache consistency check.

    If Moondream was called during a transition frame (e.g. mid-departure
    before the sea HUD rendered) it could have cached the WRONG family.
    Later ticks where the rule-based path produces a definitive answer
    via strong chrome signals expose the disagreement.  This function
    invalidates the cache on contradiction so the next ambiguous-chrome
    frame triggers a fresh Moondream probe.

    Only acts when:
      - cache has a verdict (else nothing to reconcile)
      - rule-based location maps to a definitive family (else uncertain)
      - the two disagree
    """
    try:
        from brain import moondream_family_cache as _mfc
        cached = _mfc.get_family()
        if cached is None:
            return
        family = _location_to_family(location)
        if family is None or family == cached:
            return
        logger.info(
            f"[moondream_family_cache] reconcile: classifier returned "
            f"{location!r} (family={family!r}) but cache says {cached!r} — "
            "invalidating so next ambiguous frame re-probes"
        )
        _mfc.invalidate(f"classifier_mismatch:{location}")
    except Exception as e:
        logger.debug(f"[reconcile_moondream_family_cache] skipped: {e}")


def _detect_navigation_state(frame) -> tuple[str, Optional[str], str]:
    """
    Returns (state_id, port_name, detail) for Pass 3 of perceive.
    Calls _classify_nav_state directly — must NOT route through
    actions.sail_actions.where_am_i (that is now a shim back into
    perceive() and would recurse infinitely).
    """
    loc = _classify_nav_state(frame)
    _reconcile_moondream_family_cache(loc.get("location", ""))
    return loc["location"], loc.get("port"), loc.get("detail", "")


# ── Main perceive function ────────────────────────────────────────────────────

def perceive(frame=None) -> PerceiveResult:
    """
    Three-pass perception.  Returns a PerceiveResult with full state picture.

    Interruptors are dismissed inline — caller receives a clean result with
    interruptors already cleared.  If interruptors could not be dismissed,
    they are listed in result.interruptors for the caller to handle.
    """
    from actions.sail_actions import _ocr_frame

    if frame is None:
        frame = capture_screen()

    # ── Pass 1: Interruptors ──────────────────────────────────────────────────
    frame = dismiss_interruptors(frame)

    # Check whether any interruptors remain (full frame — same as dismiss_interruptors)
    remaining_interruptors, _residual_obstruction = _detect_interruptors(
        frame, _ocr_frame(frame, min_conf=0.3)
    )
    remaining_interruptors = list(remaining_interruptors)   # mutable for downstream .append

    # ── Pass 3 first (need nav state before flow detection) ───────────────────
    nav_state, port, detail = _detect_navigation_state(frame)

    # ── Slice 1: `pending` polling (2026-05-12) ──────────────────────────────
    # The classifier returns `pending` when the 3D world is still rendering
    # after a reload / scene transition (chrome not drawn, OmniParser sparse).
    # Poll up to ~15s for chrome to appear before escalating.  See
    # docs/four_layer_nav_classification.md.
    if nav_state == "pending":
        _PENDING_POLL_INTERVAL_S = 1.5
        _PENDING_POLL_MAX_TRIES = 10   # ≈15s total
        for attempt in range(1, _PENDING_POLL_MAX_TRIES + 1):
            time.sleep(_PENDING_POLL_INTERVAL_S)
            frame = capture_screen()
            frame = dismiss_interruptors(frame)
            nav_state, port, detail = _detect_navigation_state(frame)
            logger.info(
                f"[perceive] pending poll {attempt}/{_PENDING_POLL_MAX_TRIES} "
                f"→ {nav_state!r}"
            )
            if nav_state != "pending":
                break
        # Re-read interruptors against the (possibly newly-rendered) frame
        # so downstream consumers see the current overlay state, not the
        # stale list from before the poll.
        remaining_interruptors, _residual_obstruction = _detect_interruptors(
            frame, _ocr_frame(frame, min_conf=0.3)
        )
        remaining_interruptors = list(remaining_interruptors)
        if nav_state == "pending":
            # Polling timed out — fall back to `unknown` so existing
            # handle_unknown_blocking / teaching paths take over.
            logger.warning(
                f"[perceive] pending state persisted after "
                f"{_PENDING_POLL_MAX_TRIES} polls (~{int(_PENDING_POLL_INTERVAL_S * _PENDING_POLL_MAX_TRIES)}s); "
                "downgrading to 'unknown'"
            )
            nav_state = "unknown"
            detail    = ("pending polling timeout — chrome still not drawn "
                         "after ~15s; classifier could not commit")

    # ── Pass 1.5: Unknown blocking — learn and dismiss ────────────────────────
    # If nav_state is "unknown" after pass 1 interruptor dismissal, something
    # unrecognised is blocking the screen.  Try Qwen → Claude → human in order.
    # If handled, re-capture and re-detect so the rest of perceive() sees clean state.
    if nav_state == "unknown":
        from actions.sail_actions import _ocr_frame as _ocr_for_unknown
        _unk_tokens = _ocr_for_unknown(frame, min_conf=0.3)
        handled = handle_unknown_blocking(frame, _unk_tokens, detail)
        if handled:
            frame = capture_screen()
            nav_state, port, detail = _detect_navigation_state(frame)

    # ── L2.5: Local LLM reasoning over OCR + KB context ───────────────────────
    # Augments detail, confidence, sub_menu, overlays when the cheaper
    # signals weren't enough.  Skipped when:
    #   - nav_state is classified (not "unknown")
    #   - AND the fingerprint detail names a specific building / sub_menu
    #     (e.g. "building: harbor", "sub_menu: recruit crew") rather than
    #     the generic "building: 1/1 signals matched" placeholder.
    # In that case the freeform Qwen detail is cosmetic — fingerprint
    # already identified the screen, sub_menu fingerprint catches sub-menus,
    # and the keyword scan plus L2.5 flow_hint fallback already runs in
    # _detect_active_flow.  Qwen on MLX costs ~3-5s per perceive() and
    # this gate avoids paying that cost when it cannot change the outcome.
    detail_is_specific = (
        bool(detail)
        and ":" in detail
        and not detail.endswith("signals matched")
    )
    # The family-classifier short-circuit in _classify_nav_state emits
    # details like "port_overworld (family-classifier@1.00, port='Las
    # Palmas')" — no ':' separator, but the verdict is already as
    # specific as we need (state + port name).  Treat any
    # family-classifier verdict at ≥0.95 confidence as specific so Qwen
    # (currently ~9-10 s/call on MLX) is skipped on the common
    # port_overworld / sea / world_map ticks.  See 2026-05-22 sail-Berber
    # trace, where two consecutive port_overworld@1.00 ticks each spent
    # ~9 s in Qwen producing a result the FSM didn't consume.
    if not detail_is_specific and detail:
        import re as _re
        m = _re.search(r"family-classifier@(\d+\.\d+)", detail)
        if m and float(m.group(1)) >= 0.95:
            detail_is_specific = True
    skip_qwen = nav_state != "unknown" and detail_is_specific

    l25_result   = None
    l25_sub_menu = None
    if skip_qwen:
        logger.info(
            f"[perceive] qwen skipped: detail={detail!r} is already "
            "specific (fingerprint identified screen; layout context "
            "still loads for non-Qwen consumers)"
        )
    else:
        try:
            from actions.sail_actions import _ocr_frame
            from vision.qwen_perception import qwen_perceive
            from vision.omniparser import parse_fast_cached
            ocr_tokens = _ocr_frame(frame, min_conf=0.3)
            parent_building = _resolve_parent_building(nav_state, detail)
            # Pass cached OmniParser elements as structured anchors —
            # Qwen reasons about real positions + types instead of
            # guessing from a flat OCR token list.  parse_fast_cached
            # is a no-op cache hit when classify_nav_state already
            # called it for this frame (usual path).
            try:
                op_elements = parse_fast_cached(frame)
            except Exception as _e:
                op_elements = None
                logger.debug(f"[perceive] OmniParser unavailable for Qwen: {_e}")
            # Goal-aware Qwen — pass the active GoalContext (if any)
            # as a short hint so Qwen can cross-check "did we arrive?"
            # against the observed scene.  See 2026-05-23 Berber sail.
            task_hint: Optional[str] = None
            try:
                from brain.goal_context import current_goal as _current_goal
                _g = _current_goal()
                if _g is not None:
                    task_hint = _g.summary()
            except Exception as _exc:
                logger.debug(f"[perceive] goal_context unavailable: {_exc}")
            l25_result = qwen_perceive(
                nav_state, detail, ocr_tokens,
                parent_building=parent_building,
                elements=op_elements,
                task_hint=task_hint,
            )
            if l25_result:
                l25_detail = l25_result.get("detail")
                if l25_detail:
                    # Preserve the L0/L1 building identity prefix (e.g. "building: Harbor")
                    # so downstream flow detection and consistency checks can still match
                    # on building type keywords.  Qwen's free-text description is appended
                    # as context — it must never overwrite the structural identification.
                    if nav_state == "building" and detail.startswith("building:"):
                        detail = f"{detail} — {l25_detail}"
                    else:
                        detail = l25_detail
                l25_sub_menu = l25_result.get("sub_menu")
                # If L2.5 found additional overlay ids not yet caught, add them
                for ov in l25_result.get("overlays", []):
                    if ov not in remaining_interruptors:
                        remaining_interruptors.append(ov)
            else:
                logger.warning(
                    f"[perceive] qwen returned None — "
                    "no L2.5 enrichment for this tick"
                )
        except Exception as e:
            logger.warning(
                f"[perceive] qwen call raised {type(e).__name__}: {e} — "
                "L2.5 skipped for this tick"
            )

    # Update the last-known-building tracker so the NEXT sub_menu tick
    # has parent context available.
    _update_last_known_building(nav_state, detail)

    # ── Pass 2: Active flow ───────────────────────────────────────────────────
    flow_id, flow_step = _detect_active_flow(frame, nav_state, detail)

    # If L2.5 provided a flow_hint and keyword scan found nothing, use it
    if flow_id is None and l25_result and l25_result.get("flow_hint"):
        flow_id   = l25_result["flow_hint"]
        flow_step = "unknown"
        logger.debug(f"[perceive] L2.5 flow_hint applied: {flow_id!r}")

    # ── Consistency check — catch contradictions before returning ─────────────
    confidence = CONFIDENCE_HIGH
    # Honour L2.5 low-confidence signal
    if l25_result and l25_result.get("confidence") == CONFIDENCE_LOW:
        confidence = CONFIDENCE_LOW
    else:
        flow_id, flow_step, confidence = _validate_flow_consistency(
            flow_id, flow_step, nav_state, detail
        )

    result = PerceiveResult(
        state         = nav_state,
        port          = port,
        detail        = detail,
        flow          = flow_id,
        flow_step     = flow_step,
        sub_menu      = l25_sub_menu,
        interruptors  = remaining_interruptors,
        confidence    = confidence,
        scene_type    = (l25_result or {}).get("scene_type"),
        task_complete = (l25_result or {}).get("task_complete"),
    )
    _publish_observation(result, frame)
    return result


def _publish_observation(result: PerceiveResult, frame) -> None:
    """Push this tick's perception into the BotObservation singleton.

    Bridge 1 from docs/memory_and_agent_architecture.md.  Read-mostly
    for now — consumers (flows, planner, recovery) still use their
    existing code paths.  As they migrate, they read from
    brain.observation.current() instead of re-deriving from pixels.

    Phase 2 (2026-05-18): also publish the typed overlay (DialogModel
    / BuildingNpcOverlay / keyword Overlay).  We run the typed
    detectors directly on this frame's OmniParser elements rather
    than going through the full detect_scene() pipeline — same
    result, less work.  OmniParser parsing is cached per frame, so
    the cost is one cached lookup plus the lightweight detector
    code.
    """
    from brain import observation as _obs
    scene_kind = result.state
    if result.detail and ":" not in result.state and result.state in (
        "building", "sub_menu"
    ):
        scene_kind = f"{result.state}:{result.detail}".lower()
    scene = type("PerceiveSceneAdapter", (), {
        "scene_kind": scene_kind,
        "confidence": result.confidence,
    })()

    overlay = None
    if frame is not None:
        try:
            from vision.omniparser import get_omniparser
            from vision.region_detectors.dialog import detect_dialog
            from vision.region_detectors.building_npc_overlay import (
                detect_building_npc_overlay,
            )
            from vision.region_detectors.overlay import detect_overlay
            parser = get_omniparser()
            elements = parser.parse_fast(frame)
            fw, fh = frame.size
            overlay = (
                detect_dialog(elements, fw, fh)
                or detect_building_npc_overlay(elements, fw, fh)
                or detect_overlay(elements, fw, fh)
            )
        except Exception as e:
            logger.debug(f"[perceive] overlay detection failed: {e}")
            overlay = None

    # Sea-only perception.  The NavigationView is the primary substrate
    # for steering decisions (image processing on the mini-map crop, no
    # trained model required).  The classifier-based minimap_verdict /
    # shoreline_verdict are kept as deprecated aliases for one cycle so
    # any consumer still reading obs.minimap / obs.shoreline keeps
    # working while it migrates.
    nav_view = None
    minimap_verdict = None
    shoreline_verdict = None
    if frame is not None and result.state == "sea":
        try:
            from vision.minimap_navigation_view import (
                read_navigation_view, MINIMAP_CROP,
            )
            # §13.19 — feed text bboxes that overlap the mini-map crop
            # to the navigation view so its heading detector can run
            # the text_close pre-pass when a port/village label sits
            # on top of the ship icon.  See
            # docs/heading_pca_yellow_anchor.md.
            text_bboxes_local = _extract_minimap_text_bboxes(frame, MINIMAP_CROP)
            nav_view = read_navigation_view(
                frame, text_bboxes_local=text_bboxes_local)
        except Exception as e:
            logger.debug(f"[perceive] minimap navigation view failed: {e}")
        try:
            from vision.minimap_reader import read_minimap
            minimap_verdict = read_minimap(frame)
        except Exception as e:
            logger.debug(f"[perceive] minimap read failed: {e}")
        try:
            from vision.shoreline_reader import read_shoreline
            shoreline_verdict = read_shoreline(frame)
        except Exception as e:
            logger.debug(f"[perceive] shoreline read failed: {e}")

    _obs.update(
        scene=scene,
        overlay=overlay,
        detected_settlement=result.port,
        frame_id=str(id(frame)) if frame is not None else None,
        nav=nav_view,
        minimap=minimap_verdict,
        shoreline=shoreline_verdict,
    )
