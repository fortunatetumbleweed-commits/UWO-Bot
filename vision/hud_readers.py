"""HUD numeric readers — ducats / cargo / crew off the game HUD.

These are the GROUND-TRUTH signals for the task executor's done-conditions and
progress checks (docs/next_phase_architecture_2026-08-09.md §3a, §4.1): buy done =
cargo up AND ducats down; recruit done = crew up. Read from the OmniParser element
inventory already produced each perceive tick (ducats/cargo), plus OCR for crew
(reusing the proven state_extractor "Fleet Crew Size" anchor).

Return None when the value isn't visible — absence is informative ("couldn't read"
!= "zero"). Fractions are of the 2400x1080 landscape frame.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

_INT_RE = re.compile(r"^\s*[\d,]+\s*$")
_PAIR_RE = re.compile(r"(\d[\d,]*)\s*/\s*(\d[\d,]*)")


def _to_int(s: str) -> Optional[int]:
    s = (s or "").strip().replace(",", "")
    return int(s) if s.isdigit() else None


def _to_int_sep(s: str) -> Optional[int]:
    """Parse an integer treating BOTH ',' and '.' as thousands separators — OCR
    frequently reads '2,275' as '2.275'. Crew/cargo are integers, so this is safe."""
    s = (s or "").strip().replace(",", "").replace(".", "").replace(" ", "")
    return int(s) if s.isdigit() else None


def _pos(e):
    return getattr(e, "cx", None), getattr(e, "cy", None)


def read_ducats(elements, w: int = 2400, h: int = 1080) -> Optional[int]:
    """Ducat balance = leftmost number in the top-right currency cluster.

    The currency icons sit top-right (ducat, blue gem, red gem, energy). The gold
    ducat coin is the LEFTMOST of the cluster, and ducats dwarf the gem counts.
    """
    cluster = []
    for e in elements:
        cx, cy = _pos(e)
        if cx is None or cy is None:
            continue
        if cy < 0.09 * h and cx > 0.60 * w:
            v = _to_int(getattr(e, "label", "") or "")
            if v is not None:
                cluster.append((cx, v))
    if cluster:
        cluster.sort()                   # by cx; leftmost = ducat
        return cluster[0][1]
    # Fallback (e.g. Company Overview menu, where ducats are in a left panel):
    # the number nearest a 'ducat' label.
    return _nearest_int_to_keyword(elements, ("ducat",))


# Top-right currency cluster, left→right: gold ducat, blue gem, red gem, green
# (action/voyage points).  Ducats are millions–billions; gems are thousands — so
# the ducat is identified by MAGNITUDE (OmniParser inconsistently parses the huge
# ducat number, so position alone mislabels gems as ducat).  #15 will make this
# fully robust via per-icon colour detection + region OCR.
_GEM_ORDER = ("blue_gem", "red_gem", "action_points")
_DUCAT_MIN = 1_000_000   # ducats dwarf gem counts; clean separator


def read_currencies(elements, w: int = 2400, h: int = 1080) -> dict:
    """Read the top-right currency cluster into {name: value}.

    The ducat is the ducat-magnitude token (>= _DUCAT_MIN); the remaining tokens
    map left→right onto (blue_gem, red_gem, action_points).  Returns only what it
    could read — the ducat is omitted (not guessed) when OmniParser misses it."""
    cluster = []
    for e in elements:
        cx, cy = _pos(e)
        if cx is None or cy is None:
            continue
        if cy < 0.075 * h and cx > 0.62 * w:
            v = _to_int(getattr(e, "label", "") or "")
            if v is not None:
                cluster.append((cx, v))
    cluster.sort()                                   # by cx, left→right
    result: dict = {}
    ducats = [(cx, v) for cx, v in cluster if v >= _DUCAT_MIN]
    if ducats:
        dcx, dval = max(ducats, key=lambda z: z[1])  # the ducat
        result["ducat"] = dval
        cluster = [(cx, v) for cx, v in cluster if cx != dcx]
    for name, (_cx, val) in zip(_GEM_ORDER, cluster):
        result[name] = val
    return result


_COMPANY_CROP = (700, 1025, 1700, 1080)   # bottom-centre "LV 92 … 68.81%" bar
_LV_RE = re.compile(r"lv\s*(\d+)", re.I)
_PCT_FLOAT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _parse_company_level(text: str) -> Optional[Tuple[int, Optional[float]]]:
    """Parse '(LV) 92 … 68.81%' → (92, 68.81). None if no level. Pure/testable."""
    m = _LV_RE.search(text or "")
    if not m:
        return None
    pm = _PCT_FLOAT_RE.search(text)
    return (int(m.group(1)), float(pm.group(1)) if pm else None)


def read_company_level(frame) -> Optional[Tuple[int, Optional[float]]]:
    """(company_level, xp_pct) from the LV progress bar shown bottom-centre on any
    overworld / sea view.  xp_pct may be None if only the level parsed."""
    try:
        tokens = _ocr_frame_crop(frame, _COMPANY_CROP)
    except Exception:
        return None
    return _parse_company_level(" ".join(t for t, _c, _x, _y in tokens))


def read_fleet_state(elements, frame):
    """Assemble a barter_kb.FleetState from a single frame: currencies (top-right
    cluster from OmniParser `elements`) + company level/XP (bottom-centre bar via
    OCR).  Skill/guild/gift-token fields are left None (not on this screen)."""
    from memory.barter_kb import FleetState
    state = FleetState(currencies=read_currencies(elements))
    lv = read_company_level(frame)
    if lv:
        state.company_level, state.company_xp_pct = lv
    return state


def _ocr_frame_crop(frame, box):
    from actions.sail_actions import _ocr_frame
    return _ocr_frame(frame.crop(box), min_conf=0.3)


def _find_pairs(elements):
    """Every 'N/M' pair on screen: list of (cur, cap, cx, cy, label)."""
    out = []
    for e in elements:
        lab = (getattr(e, "label", "") or "").strip()
        m = _PAIR_RE.search(lab)
        if not m:
            continue
        cur, cap = _to_int(m.group(1)), _to_int(m.group(2))
        cx, cy = _pos(e)
        if cur is not None and cap is not None and cx is not None:
            out.append((cur, cap, int(cx), int(cy), lab))
    return out


def _nearest_pair_to_keyword(elements, keywords) -> Optional[Tuple[int, int]]:
    """The 'N/M' pair whose element is nearest to any element whose label
    contains one of `keywords`. Returns (cur, cap) or None."""
    kw_pos = []
    for e in elements:
        lab = (getattr(e, "label", "") or "").strip().lower()
        if any(k in lab for k in keywords):
            cx, cy = _pos(e)
            if cx is not None:
                kw_pos.append((cx, cy))
    pairs = _find_pairs(elements)
    if not pairs or not kw_pos:
        return None

    def d2(p):
        _, _, cx, cy, _ = p
        return min((cx - kx) ** 2 + (cy - ky) ** 2 for kx, ky in kw_pos)

    cur, cap = min(pairs, key=d2)[:2]
    return (cur, cap)


def _nearest_int_to_keyword(elements, keywords) -> Optional[int]:
    """The integer-valued element nearest to any element whose label contains one
    of `keywords`. Used for currencies shown as '<label> <number>' pairs."""
    kw_pos = []
    for e in elements:
        lab = (getattr(e, "label", "") or "").strip().lower()
        if any(k in lab for k in keywords):
            cx, cy = _pos(e)
            if cx is not None:
                kw_pos.append((cx, cy))
    if not kw_pos:
        return None
    best, best_d = None, None
    for e in elements:
        v = _to_int(getattr(e, "label", "") or "")
        if v is None:
            continue
        cx, cy = _pos(e)
        if cx is None:
            continue
        d = min((cx - kx) ** 2 + (cy - ky) ** 2 for kx, ky in kw_pos)
        if best_d is None or d < best_d:
            best, best_d = v, d
    return best


def read_cargo(elements, w: int = 2400, h: int = 1080) -> Optional[Tuple[int, int]]:
    """(used, capacity). REQUIRES a 'cargo'/'load' anchor — cargo is context-
    dependent (market / overview), so we do NOT guess from an arbitrary N/M pair
    (that would grab crew '953/2,275' on the recruit screen)."""
    return _nearest_pair_to_keyword(elements, ("cargo", "load"))


_CREW_ANCHORS = ("crew size", "fleet crew")
_TOK_PAIR_RE = re.compile(r"(\d[\d.,]*)\s*/\s*(\d[\d.,]*)")


def read_crew(frame) -> Optional[Tuple[int, int]]:
    """(current, max) FLEET crew. OCR the frame, anchor on 'Crew Size' / 'Fleet
    Crew', and take the 'N/M' token nearest the anchor (separator-robust — OCR
    reads the thousands-comma as a period). Per-ship crew pairs sit by the ship
    rows, far from the anchor, so nearest-to-anchor picks the fleet total."""
    try:
        tokens = _ocr_tokens(frame)     # (text, conf, cx, cy)
    except Exception:
        return None
    has_anchor, pairs = False, []
    for txt, _conf, cx, cy in tokens:
        low = (txt or "").lower()
        if any(a in low for a in _CREW_ANCHORS):
            has_anchor = True
        m = _TOK_PAIR_RE.search(txt or "")
        if m:
            cur, cap = _to_int_sep(m.group(1)), _to_int_sep(m.group(2))
            # guard: a garbled fleet token like '0(+9531/2.275' yields cur>cap;
            # a valid crew reading has current <= capacity.
            if cur is not None and cap is not None and cur <= cap:
                pairs.append((cur, cap))
    if not has_anchor or not pairs:
        return None
    # FLEET total = the largest-capacity crew pair (it's the sum of the ships,
    # so its denominator exceeds any single ship's).
    cur, cap = max(pairs, key=lambda p: p[1])
    return (cur, cap)


_SUPPLY_ANCHORS = {"water": ("water",), "food": ("food",)}

# A value must be ADJACENT to its label — directly beneath it (the Cargo Hold and Discard
# dialog stack label-over-count) or beside it on the same row.  Plain "nearest number"
# also accepts DIAGONAL neighbours, and that is how the main menu read supply as
# 4108/4108 = 149 days for a fleet holding ZERO: the Total Load Capacity pair sat 43px up
# and 375px right of the Water label, closer than anything else on screen.  An unsupplied
# fleet would have sailed straight past the 7-day village gate (live 2026-08-21).
_SUPPLY_BELOW = (80, 120)      # (max |Δx|, max |Δy|) for a count stacked under its label
_SUPPLY_BESIDE = (260, 30)     # (max |Δx|, max |Δy|) for a count on the label's own row


def _adjacent_to_label(cx, cy, kw_pos) -> bool:
    for kx, ky in kw_pos:
        dx, dy = abs(cx - kx), cy - ky
        # Stacked: the count sits BELOW its label (direction matters — the main-menu
        # capacity pair sits 44px ABOVE 'Food' and would otherwise qualify).
        if dx <= _SUPPLY_BELOW[0] and 0 <= dy <= _SUPPLY_BELOW[1]:
            return True
        if dx <= _SUPPLY_BESIDE[0] and abs(dy) <= _SUPPLY_BESIDE[1]:
            return True
    return False


def _nearest_held_to_keyword(elements, keywords) -> Optional[int]:
    """The HELD count of the numeric element nearest to a `keywords` anchor.

    Supply renders two ways: a bare tile count ('329') in the Cargo Hold, or an
    'N/M' slider pair ('50/226') in the Discard dialog where M is the amount held.
    Return the bare int, or the pair's denominator (held total), whichever token is
    nearest the anchor. Returns None if no anchor or no numeric token."""
    kw_pos = []
    for e in elements:
        lab = (getattr(e, "label", "") or "").strip().lower()
        if any(k in lab for k in keywords):
            cx, cy = _pos(e)
            if cx is not None:
                kw_pos.append((cx, cy))
    if not kw_pos:
        return None
    def d2(cx, cy):
        return min((cx - kx) ** 2 + (cy - ky) ** 2 for kx, ky in kw_pos)

    best, best_d = None, None
    for e in elements:
        lab = (getattr(e, "label", "") or "").strip()
        cx, cy = _pos(e)
        if cx is None:
            continue
        held = None
        m = _PAIR_RE.search(lab)
        if m:                       # 'N/M' slider → held total is M
            held = _to_int(m.group(2))
        else:
            held = _to_int(lab)     # bare tile count
        if held is None:
            continue
        if not _adjacent_to_label(cx, cy, kw_pos):
            continue
        d = d2(cx, cy)
        if best_d is None or d < best_d:
            best, best_d = held, d
    return best


def read_supply_rows(frame, elements, ocr_fn=None
                     ) -> Optional[Tuple[Optional[int], Optional[int]]]:
    """(water, food) read from the MAIN MENU fleet panel.

    That panel renders each amount INSIDE its label's tile ("Water   0"), and OmniParser
    emits the tile as one element carrying only the word — the number never becomes its
    own element.  So crop the detected tile's own right-hand side and OCR it: the region
    comes from the element's bbox, so it follows the camera-cutout shift like everything
    else, and there is no coordinate to go stale.

    Returns None when neither label is present; a component is None when its tile holds
    no readable digits.  NEVER falls back to a number from another row."""
    if ocr_fn is None:
        def ocr_fn(image):
            from vision.ocr import _get_reader
            import numpy as _np
            return _get_reader().readtext(_np.array(image), detail=0,
                                          allowlist="0123456789,")

    def _tile_value(el):
        x1 = el.x1 + int((el.x2 - el.x1) * 0.45)      # right ~55% of the tile
        box = (max(0, x1), max(0, el.y1), min(frame.width, el.x2), min(frame.height, el.y2))
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        crop = frame.crop(box)
        # Upscale before OCR — the amount is often a SINGLE glyph in a ~176x53 crop, and
        # EasyOCR returns nothing at native size (a lone "0" read as empty, which would
        # look like "unreadable" for a fleet that genuinely has zero supply).
        crop = crop.resize((crop.width * 3, crop.height * 3))
        try:
            raw = ocr_fn(crop)
        except Exception as exc:
            logger.debug(f"[hud] supply tile OCR failed: {exc}")
            return None
        digits = "".join(ch for tok in raw for ch in str(tok) if ch.isdigit())
        return int(digits) if digits else None

    found = {}
    for e in elements or []:
        lab = (getattr(e, "label", "") or "").strip().lower()
        if lab in ("water", "food") and lab not in found:
            found[lab] = _tile_value(e)
    if not found:
        return None
    return (found.get("water"), found.get("food"))


def read_supply(elements) -> Optional[Tuple[Optional[int], Optional[int]]]:
    """(water, food) supply units held. Anchored on the 'Water'/'Food' cargo labels
    (Cargo Hold tiles or the Discard dialog slider). Either component may be None if
    that label isn't on screen; returns None only when NEITHER is found.

    Feed into brain.supply_planner.Supply(water, food) for the reserve checks."""
    water = _nearest_held_to_keyword(elements, _SUPPLY_ANCHORS["water"])
    food = _nearest_held_to_keyword(elements, _SUPPLY_ANCHORS["food"])
    if water is None and food is None:
        return None
    return (water, food)


def _ocr_tokens(frame):
    """OCR the frame into state_extractor's (text, conf, cx, cy) token tuples."""
    from actions.sail_actions import _ocr_frame
    return _ocr_frame(frame, min_conf=0.3)
