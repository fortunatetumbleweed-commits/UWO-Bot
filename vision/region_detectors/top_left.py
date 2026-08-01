# vision/region_detectors/top_left.py
#
# Top-left region detector — produces a TopLeftRegion describing the
# top-left compound area of any UWO scene.  Slice 1.
#
# Three structural shapes the detector recognises:
#
#   1. Overworld (port_overworld / sea)
#      Big icon (lighthouse on port, ship on sea) — w≥80, h≥80 — plus
#      a smaller flag icon (port only) and the title text to the right
#      of the icons.  The whole compound sits on an irregular arrow-
#      shaped background that ALSO covers the title row.
#
#   2. Chromed (building / sub_menu / world_map / port_map)
#      Small back-arrow (w<60, h<60) followed by the title text and
#      a '?' tutorial icon in a circle.  No flag, no big icon.
#
#   3. Unknown
#      Nothing recognisable at the top-left.  Caller falls through to
#      the existing classifier.
#
# Empirical sizing baseline (2400×1080 frame, captured 2026-05-16):
#   - Port lighthouse: w≈100-135, h≈85-155
#   - Sea ship/compound: w≈260-590, h≈40-200 (whole compound is one
#     detection on sea — OmniParser returns it as a button labelled
#     with the waters name)
#   - Building back-arrow: w≈30-50, h≈30-50
#   - National flag (port only): w≈40-60, h≈20-30
#   - '?' tutorial icon: w≈30-50, h≈30-50
#   - Title text bbox: w≈100-330 (port name + bubble fusion noise),
#                      h≈35-50 (title font height)
#
# The detector classifies the big icon by SIZE and disambiguates port
# vs sea by flag presence.  Title extraction uses the existing height-
# filtered title picker plus fuzzy-match correction (text_correction.py).

from __future__ import annotations

from typing import List, Optional, Tuple

from loguru import logger

from vision.omniparser import DetectedElement
from vision.scene_model import (
    BigIconKind,
    SceneFamily,
    TitleField,
    TopLeftRegion,
)


# ── Region constants (normalised, so they scale with resolution) ──────────


# Generous outer bounds of the top-left zone we scan.  On a 2400×1080
# frame this is x < 800, y < 280.  Covers compound region + a margin
# for NPC bubbles that may bleed in.
_ZONE_X_NORM = 800.0 / 2400.0   # ≈ 0.333
_ZONE_Y_NORM = 280.0 / 1080.0   # ≈ 0.259

# Big-icon size thresholds (normalised against 1080-tall frame).
_BIG_ICON_MIN_W_NORM = 80.0 / 2400.0
_BIG_ICON_MIN_H_NORM = 80.0 / 1080.0

# Big-icon position constraints.  Real overworld big icons centre at
# cx ∈ [191, 421] and cy ∈ [95, 126] across the captured frames; chromed-
# scene chrome elements that look big-icon-shaped tend to sit either
# further right (item grid icons cx > 500) or higher up (chrome icons at
# cy ≤ 80, title-row).  Tightening the position window rejects both
# while preserving every real overworld big-icon position observed.
_BIG_ICON_MAX_CX_NORM = 450.0 / 2400.0
_BIG_ICON_MIN_CY_NORM = 90.0 / 1080.0

# Back-arrow size thresholds.  Generous on max so partial captures still
# qualify, strict-ish on min to reject pixel noise.
_BACK_ARROW_MIN_W_NORM = 25.0 / 2400.0
_BACK_ARROW_MAX_W_NORM = 70.0 / 2400.0
_BACK_ARROW_MIN_H_NORM = 25.0 / 1080.0
_BACK_ARROW_MAX_H_NORM = 70.0 / 1080.0

# Flag size thresholds (port_overworld only).
_FLAG_MIN_W_NORM = 25.0 / 2400.0
_FLAG_MAX_W_NORM = 70.0 / 2400.0
_FLAG_MIN_H_NORM = 15.0 / 1080.0
_FLAG_MAX_H_NORM = 45.0 / 1080.0

# Tutorial '?' icon size thresholds (chromed scenes).
_TUTORIAL_MIN_W_NORM = 25.0 / 2400.0
_TUTORIAL_MAX_W_NORM = 60.0 / 2400.0
_TUTORIAL_MIN_H_NORM = 25.0 / 1080.0
_TUTORIAL_MAX_H_NORM = 60.0 / 1080.0

# Title text height threshold (already used in vision/ocr.py).
_TITLE_MIN_HEIGHT_NORM = 32.0 / 1080.0


# ── Public API ────────────────────────────────────────────────────────────


def detect_top_left(
    elements:     List[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> TopLeftRegion:
    """Detect the top-left region's family + content.

    Walks OmniParser elements inside the generous top-left zone, classifies
    each one by size into one of:
      - big_icon (lighthouse/ship — w & h ≥ 80 px on a 2400×1080 frame)
      - back_arrow (small icon at the very top-left)
      - flag (small icon adjacent to a big icon)
      - tutorial_q (small icon adjacent to title text on a chromed scene)
      - title (text element above the title height threshold)
    Then composes a TopLeftRegion based on which shapes are present.

    The detector is positionally tolerant — it doesn't insist on exact
    coordinates, just rough size and rough adjacency.  This makes it
    robust to OmniParser bbox jitter and partial occlusions.
    """
    zone_x = _ZONE_X_NORM * frame_width
    zone_y = _ZONE_Y_NORM * frame_height

    in_zone = [
        el for el in elements
        if el.cx < zone_x and el.cy < zone_y
    ]

    # Classify each in-zone element by shape.
    big_icons    = [e for e in in_zone if _is_big_icon(e, frame_width, frame_height)]
    back_arrows  = [e for e in in_zone if _is_back_arrow(e, frame_width, frame_height)]
    title_texts  = [e for e in in_zone if _is_title_text(e, frame_height)]

    # ── Branch 1: chromed (back-arrow present) ────────────────────────────
    # Back-arrow takes priority over big-icon when BOTH are detected.
    # Empirically OmniParser sometimes styles chromed-scene title bars
    # (e.g. Item Shop's 'Tool' sub-menu title) as button-typed elements
    # large enough to pass the big-icon size threshold.  But chromed
    # scenes also have a definite back-arrow at the very corner — a
    # signal that overworld scenes never produce.  When back-arrow is
    # present, the scene IS chromed regardless of any oversized
    # button-typed title element.
    if back_arrows:
        back = _pick_smallest(back_arrows)
        # Building/sub-menu titles are NOT port names — no fuzzy correction.
        # A future slice may add a known-building-titles correction set.
        title = _build_title(title_texts, correction_mode="none")
        # '?' tutorial icon: small icon at title height, to the right of title
        tutorial = _find_tutorial_q(
            back, title_texts, in_zone, frame_width, frame_height,
        )
        family: SceneFamily = "world_map" if (title and title.text.lower() == "world map") else "chromed"
        return TopLeftRegion(
            family=family,
            title=title,
            back_arrow=True,
            tutorial_q=tutorial,
        )

    # ── Branch 2: overworld (big icon present, no back-arrow) ─────────────
    if big_icons:
        big = _pick_largest(big_icons)
        # Look for a flag immediately to the right of the big icon.
        flag = _find_flag_near(big, in_zone, frame_width, frame_height)
        # Disambiguate lighthouse (port) vs ship (sea) using multiple
        # signals — flag is the cleanest when present, but it can be
        # occluded by NPC bubbles.  See _disambiguate_big_icon for the
        # full signal cascade.
        big_kind: BigIconKind = _disambiguate_big_icon(big, flag, title_texts)
        correction_mode = "port" if big_kind == "lighthouse" else "waters"
        title = _build_title(title_texts, correction_mode)
        return TopLeftRegion(
            family="overworld",
            title=title,
            big_icon=big_kind,
            big_icon_bbox=(big.x1, big.y1, big.x2, big.y2),
            flag_present=flag is not None,
            flag_bbox=(flag.x1, flag.y1, flag.x2, flag.y2) if flag else None,
            flag_nation=None,        # filled by a later slice (template-match)
        )

    # ── Branch 3: title-only fallback ─────────────────────────────────────
    # Some chromed frames have the title visible but neither the back-
    # arrow nor a chromed-shaped big icon was cleanly detected (OmniParser
    # sometimes merges the back-arrow into a larger button, or low
    # contrast on the tutorial '?' icon hides it).  When we see ONLY a
    # title with no overworld big-icon signal, the scene is most likely
    # chromed — overworld always has a big icon, so its absence plus
    # a title strongly suggests a chromed interior.  Set family="chromed"
    # but with back_arrow=False so consumers can detect the weaker
    # signal at the SceneModel level (the scene classifier downgrades
    # confidence accordingly).
    if title_texts:
        title = _build_title(title_texts, correction_mode="none")
        # World map sometimes has no detected back-arrow (OmniParser misses
        # it under chrome variants) and no big icon — but the title 'World
        # Map' is unambiguous.  Recognise it here so the chromed-family
        # classifier doesn't treat it as a sub_menu.
        if title and title.text.lower() == "world map":
            return TopLeftRegion(family="world_map", title=title)
        return TopLeftRegion(family="chromed", title=title)

    # ── Branch 4: nothing detected ────────────────────────────────────────
    return TopLeftRegion(family="unknown")


# ── Element classification helpers ────────────────────────────────────────


def _is_big_icon(el: DetectedElement, fw: int, fh: int) -> bool:
    """A big icon is icon/button-typed AND ≥ 80×80 px on a 2400×1080 frame.

    Additional content discrimination for button-typed candidates:
    OmniParser sometimes packages chromed-scene title bars and large
    menu items as button elements with sizes above the big-icon
    threshold (observed: Item Shop / Tool sub-menu returns
    [button] 'Tool' 419x114; Item Shop's left menu items come back
    as [button] 'Gear' 433x98).  These are NOT overworld big icons.

    Discrimination rules:
      - element_type 'icon' → always accepted (real lighthouse icon
        is icon-typed with generic 'icon' label).
      - element_type 'button' → only accepted when the label fuzzy-
        matches a known waters name (real sea compound is button-
        typed with the waters name as label) OR the label is empty
        / generic 'icon' (rare; defensive).

    This stops every chromed-scene-button-mistaken-for-big-icon false
    positive without depending on enumerating every possible chromed
    sub-menu title — we discriminate by what a real big icon looks
    like, not by what every chromed alternative could be.
    """
    if el.element_type not in ("icon", "button"):
        return False
    if el.width < _BIG_ICON_MIN_W_NORM * fw:
        return False
    if el.height < _BIG_ICON_MIN_H_NORM * fh:
        return False
    # Position constraints — real big icons are in the LEFT portion of
    # the screen and BELOW the title row.  Item-grid icons in chromed
    # sub-menus sit further right; chromed-scene title-row chrome sits
    # higher up (cy ≤ 80).  Both bands exclude real big icons cleanly.
    if el.cx > _BIG_ICON_MAX_CX_NORM * fw:
        return False
    if el.cy < _BIG_ICON_MIN_CY_NORM * fh:
        return False
    if el.element_type == "button":
        label = (el.label or "").strip()
        if not label or label.lower() == "icon":
            return True
        # Real sea compound regions have a waters-name label; chromed
        # title bars have building/sub-menu names.  Fuzzy-match decides.
        from vision.text_correction import correct_waters_name
        _, ratio = correct_waters_name(label)
        return ratio >= 0.6
    return True


def _is_back_arrow(el: DetectedElement, fw: int, fh: int) -> bool:
    """A back-arrow is icon/button-typed, small, in the very top-left."""
    if el.element_type not in ("icon", "button"):
        return False
    w_ok = (
        _BACK_ARROW_MIN_W_NORM * fw <= el.width  <= _BACK_ARROW_MAX_W_NORM * fw
    )
    h_ok = (
        _BACK_ARROW_MIN_H_NORM * fh <= el.height <= _BACK_ARROW_MAX_H_NORM * fh
    )
    if not (w_ok and h_ok):
        return False
    # Position constraint — back-arrow is at the very corner (cx<120, cy<80
    # on a 2400×1080 frame).  Tighter than the general zone.
    return el.cx < (120.0 / 2400.0) * fw and el.cy < (80.0 / 1080.0) * fh


def _is_title_text(el: DetectedElement, fh: int) -> bool:
    """A title text is a text-or-button-typed element above the title
    height threshold whose label contains at least one alphabetic
    character.

    The alphabetic-character requirement filters out shield counts and
    other purely-numeric labels (like '20' from the Row 2 shield icon)
    that OmniParser sometimes bundles with their icon into a tall
    button bbox.  Without this, the title picker would prefer the
    shield bundle (height ~87px) over the actual title (~40px).
    """
    if el.element_type not in ("text", "button"):
        return False
    label = (el.label or "").strip()
    if len(label) < 2:
        return False
    # Must contain at least one alphabetic character — excludes shield
    # counts ('20'), currency values, time strings, version codes.
    if not any(c.isalpha() for c in label):
        return False
    return el.height >= _TITLE_MIN_HEIGHT_NORM * fh


def _is_flag_shape(el: DetectedElement, fw: int, fh: int) -> bool:
    """Flag icon is small icon-typed element, within flag size range."""
    if el.element_type != "icon":
        return False
    w_ok = _FLAG_MIN_W_NORM * fw <= el.width  <= _FLAG_MAX_W_NORM * fw
    h_ok = _FLAG_MIN_H_NORM * fh <= el.height <= _FLAG_MAX_H_NORM * fh
    return w_ok and h_ok


def _is_tutorial_shape(el: DetectedElement, fw: int, fh: int) -> bool:
    """'?' tutorial icon is small icon-typed element near title height."""
    if el.element_type not in ("icon", "button"):
        return False
    w_ok = _TUTORIAL_MIN_W_NORM * fw <= el.width  <= _TUTORIAL_MAX_W_NORM * fw
    h_ok = _TUTORIAL_MIN_H_NORM * fh <= el.height <= _TUTORIAL_MAX_H_NORM * fh
    return w_ok and h_ok


def _pick_largest(els: List[DetectedElement]) -> DetectedElement:
    """Pick the element with the largest area."""
    return max(els, key=lambda e: e.width * e.height)


def _pick_smallest(els: List[DetectedElement]) -> DetectedElement:
    """Pick the element with the smallest area."""
    return min(els, key=lambda e: e.width * e.height)


def _disambiguate_big_icon(
    big: DetectedElement,
    flag: Optional[DetectedElement],
    title_texts: List[DetectedElement],
) -> BigIconKind:
    """Decide whether the big icon is a lighthouse (port) or ship (sea).

    Signal cascade (in priority order):
      1. Flag detected adjacent to big icon → lighthouse (port).  Flag
         is unambiguous; only ports have national flags.
      2. Big element is a button with a meaningful (non-'icon') label
         → ship.  Empirically, OmniParser returns the sea compound
         region as a button labelled with the waters name (e.g.
         'Lauless Watters'); on port it's just labelled 'icon'.
      3. Title text fuzzy-matches a known waters name better than any
         known port name → ship.  Catches sea frames where flag isn't
         present (always) and the button-label heuristic somehow misses.
      4. Title text fuzzy-matches a known port → lighthouse.
      5. Default → lighthouse.  Port overworld is the much more common
         scene type and we'd rather mis-call a sea frame than a port one
         (the title text on sea will simply not match any port name
         well, so consumers can detect the anomaly).
    """
    # Signal 1: flag presence — strongest port signal.
    if flag is not None:
        return "lighthouse"

    # Signal 2: button-typed big element with a real label → sea.
    big_label = (big.label or "").strip().lower()
    if big.element_type == "button" and big_label and big_label != "icon":
        return "ship"

    # Signal 3 & 4: title fuzzy-match — try both, pick higher similarity.
    if title_texts:
        # Pick the tallest title candidate for matching (mirrors _build_title)
        max_h = max(t.height for t in title_texts)
        big_titles = [t for t in title_texts if t.height >= 0.8 * max_h]
        big_titles.sort(key=lambda t: (t.y1, t.x1))
        raw = big_titles[0].label.strip()
        if raw:
            from vision.text_correction import correct_port_name, correct_waters_name
            _, port_ratio = correct_port_name(raw)
            _, waters_ratio = correct_waters_name(raw)
            if waters_ratio > port_ratio and waters_ratio > 0.6:
                return "ship"
            if port_ratio > 0.6:
                return "lighthouse"

    # Signal 5: default — port is more common; mis-calling a sea frame as
    # port is easier to detect downstream than the other way around.
    return "lighthouse"


# ── Adjacency helpers ─────────────────────────────────────────────────────


def _find_flag_near(
    big: DetectedElement,
    in_zone: List[DetectedElement],
    fw: int,
    fh: int,
) -> Optional[DetectedElement]:
    """Find a flag-sized icon to the RIGHT of the big icon, at roughly
    the big icon's vertical centre.

    Tolerances are generous so partial captures and OmniParser bbox
    jitter don't break the match.
    """
    big_right = big.x2
    big_cy = big.cy
    # Flag should be within ~200 px to the right of the big icon's right edge,
    # and within ±60 px vertically of the big icon's centre on a 1080 frame.
    x_max = big_right + 200.0 / 2400.0 * fw
    y_tol = 60.0 / 1080.0 * fh
    candidates = [
        e for e in in_zone
        if _is_flag_shape(e, fw, fh)
        and big_right <= e.cx <= x_max
        and abs(e.cy - big_cy) <= y_tol
    ]
    if not candidates:
        return None
    # Closest to big_right wins
    return min(candidates, key=lambda e: e.cx - big_right)


def _find_tutorial_q(
    back: DetectedElement,
    titles: List[DetectedElement],
    in_zone: List[DetectedElement],
    fw: int,
    fh: int,
) -> bool:
    """Detect the '?' tutorial icon.  Located immediately to the right
    of the title text on chromed scenes; vertically aligned with title.

    Position note (UI observation, 2026-05-18): the '?' sits VERY
    close to the title — gap is roughly 2 character widths of the
    title font.  On a 2400×1080 frame that's ~50-80 px depending on
    title font width.  The search range below is calibrated to this:
    a ~80 px window starting near title.x2.  A wider window would
    risk picking up unrelated icons further right (e.g. the home
    button on chromed scenes, which sits at the very top-right).

    Returns True if a tutorial-sized icon is present near a title.
    """
    if not titles:
        # Without a title we can't anchor — but back-arrow + small icon
        # to the right of it is still plausible.  Fall back to checking
        # for any tutorial-shaped icon to the right of back.
        candidates = [
            e for e in in_zone
            if _is_tutorial_shape(e, fw, fh) and e.cx > back.x2
        ]
        return len(candidates) > 0

    # Anchor on the leftmost title.  Tight window because the gap
    # between title and '?' is only ~2 character widths (~50-80 px).
    title = min(titles, key=lambda t: t.x1)
    x_min = title.x2 + 5.0  / 2400.0 * fw   # just past title's right edge
    x_max = title.x2 + 80.0 / 2400.0 * fw   # ~2 char widths away
    y_tol = 30.0 / 1080.0 * fh              # tight — '?' aligns to title baseline
    candidates = [
        e for e in in_zone
        if _is_tutorial_shape(e, fw, fh)
        and x_min <= e.cx <= x_max
        and abs(e.cy - title.cy) <= y_tol
    ]
    return len(candidates) > 0


# ── Title extraction ──────────────────────────────────────────────────────


def _build_title(
    title_texts: List[DetectedElement],
    correction_mode: str,            # "port" | "waters" | "none"
) -> Optional[TitleField]:
    """Build a TitleField from candidate title text elements.

    Selection strategy:

      - When `correction_mode` is "port" or "waters", we have a known
        set to match against.  Score EACH candidate's fuzzy-match
        similarity to the known set; pick the highest-scoring candidate
        above the 0.6 cutoff.  This is robust against NPC bubble lines
        with bigger fonts than the actual title (which DO occur — see
        Amsterdam frame 0000 where 'buildings! Perfect' at h=48 sits
        above the title 'Amsterdamads!' at h=39).
      - When `correction_mode` is "none", no known set available; fall
        back to tallest-then-leftmost-topmost (the original strategy).
      - When fuzzy match returns no candidate above cutoff (none of
        the visible text resembles a known port/waters name), fall
        back to the tallest-then-leftmost-topmost strategy too — the
        title will still be returned (in raw form) for downstream
        consumers to inspect.

    The raw OCR string is preserved on the returned TitleField for
    diagnostics regardless of correction mode.
    """
    if not title_texts:
        return None

    # Merge horizontally-adjacent title fragments before selection.
    # OmniParser sometimes splits multi-word titles ("World Map",
    # "Item Shop") into two text elements on the same line.  Without
    # merging, _build_title's pick-tallest would discard one half and
    # downstream classifiers see "World" or "Map" alone.
    title_texts = _merge_adjacent_titles(title_texts)

    corrector = _get_corrector(correction_mode)

    chosen: DetectedElement
    canonical: Optional[str] = None

    if corrector is not None:
        # Score each candidate against the known set.  Highest match
        # above the cutoff wins.
        scored = []
        for el in title_texts:
            raw = el.label.strip()
            canon, ratio = corrector(raw)
            scored.append((el, canon, ratio))
        scored.sort(key=lambda s: -s[2])  # highest ratio first
        best_el, best_canon, best_ratio = scored[0]
        if best_ratio >= 0.6:
            chosen = best_el
            canonical = best_canon

    if canonical is None:
        # No known set OR no candidate matched — fall back to tallest.
        max_h = max(t.height for t in title_texts)
        big_titles = [t for t in title_texts if t.height >= 0.8 * max_h]
        big_titles.sort(key=lambda t: (t.y1, t.x1))
        chosen = big_titles[0]

    raw = chosen.label.strip()
    text = canonical if canonical else raw
    return TitleField(
        text=text,
        raw_ocr=raw,
        bbox=(chosen.x1, chosen.y1, chosen.x2, chosen.y2),
    )


def _merge_adjacent_titles(
    title_texts: List[DetectedElement],
) -> List[DetectedElement]:
    """Merge title-text elements that lie on the same line and are
    horizontally close.

    OmniParser splits some multi-word UWO titles into separate text
    elements ("World" + "Map", "Item" + "Shop").  Two elements are
    treated as one line when their cy values agree within ~12 px AND
    their heights are within 30%.  Within a line, elements are sorted
    by x1 and merged when the horizontal gap between consecutive
    elements is ≤ ~1.2× the median character width.

    The merged element is synthesised by mutating attributes on a
    duck-typed wrapper that mirrors the DetectedElement interface
    used downstream (label, x1, y1, x2, y2, cx, cy, width, height).
    Original elements are returned untouched when not merge-eligible.
    """
    if len(title_texts) < 2:
        return list(title_texts)

    # Group into lines by cy proximity + similar height.
    items = sorted(title_texts, key=lambda e: (e.cy, e.x1))
    lines: List[List] = []
    for el in items:
        placed = False
        for line in lines:
            ref = line[0]
            # Height-ratio bounds widened from 0.7-1.4 to 0.5-2.0 because
            # OmniParser sometimes returns one fragment as a button (bbox
            # extends to the chrome edges) and the other as plain text
            # (bbox hugs the glyphs).  Same line, different bbox styles.
            # cy alignment is the stricter test — height alone shouldn't
            # split fragments that share a baseline.
            if (abs(el.cy - ref.cy) <= 12
                    and 0.5 <= (el.height / max(ref.height, 1)) <= 2.0):
                line.append(el)
                placed = True
                break
        if not placed:
            lines.append([el])

    out: List = []
    for line in lines:
        if len(line) == 1:
            out.append(line[0])
            continue
        line.sort(key=lambda e: e.x1)
        # Merge consecutive elements that are part of the same title.
        # Already on the same line (cy + height filter above), so we
        # accept either a small positive gap ("World Map") OR any
        # horizontal overlap.  Overlap on the same line happens when
        # OmniParser emits both a button covering the title bar and a
        # text element inside it — both describe the same title.
        merged_groups: List[List] = [[line[0]]]
        for prev, cur in zip(line, line[1:]):
            ref = merged_groups[-1][-1]
            char_w = max(ref.height * 0.6, 8)
            gap = cur.x1 - ref.x2
            if gap <= char_w * 1.5:
                merged_groups[-1].append(cur)
            else:
                merged_groups.append([cur])
        for group in merged_groups:
            if len(group) == 1:
                out.append(group[0])
            else:
                out.append(_synthesize_merged_element(group))
    return out


def _synthesize_merged_element(group: List) -> object:
    """Create a duck-typed element from a list of merged DetectedElements.

    De-dupe labels: when a button and an inner text element overlap on
    the same line, OmniParser sometimes labels them with the SAME word
    (e.g. both say 'World').  Concatenating gives 'World World'.
    Skip an incoming label that exactly repeats one already present.
    """
    seen = []
    for e in group:
        token = e.label.strip()
        if token and (not seen or seen[-1].lower() != token.lower()):
            seen.append(token)
    label = " ".join(seen).strip()
    x1 = min(e.x1 for e in group)
    y1 = min(e.y1 for e in group)
    x2 = max(e.x2 for e in group)
    y2 = max(e.y2 for e in group)
    first = group[0]
    merged = type("MergedTitle", (), {
        "label":        label,
        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2,
        "width":        x2 - x1,
        "height":       y2 - y1,
        "element_type": getattr(first, "element_type", "text"),
    })()
    return merged


def _get_corrector(mode: str):
    """Return the appropriate fuzzy-match corrector for *mode*, or None."""
    if mode == "port":
        from vision.text_correction import correct_port_name
        return correct_port_name
    if mode == "waters":
        from vision.text_correction import correct_waters_name
        return correct_waters_name
    return None
