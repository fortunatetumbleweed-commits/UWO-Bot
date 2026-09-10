"""Yellow commit-button detector — normalises UWO's canonical positive action.

Every transaction screen in Uncharted Waters Origin (recruit crew, market Buy /
Sell, item shop, every shop) commits via one **yellow/gold button** styled as
`<currency-icon> <cost>  <VERB>` — cost on the left, action verb on the right.
Yellow = positive action, game-wide.

OmniParser localises this button reliably but collapses it to ONE element and
OCRs only ONE of its two texts — and *which* one is unstable: across live
captures it grabbed the COST (`205,848`, `707,048`) every time and the verb
(`Recruit`) never, so the reasoning layer never saw a button it could recognise.
Its bbox is right though, and the button is trivially separable by colour
(yellow-fraction ~0.35–0.77 vs ~0.00 for menus / list rows / type toggles).

So we re-OCR OmniParser's own button crops that are yellow, and split the text
into `{verb, cost}` — recovering BOTH regardless of which one OmniParser's label
caught. The result is a stable `[COMMIT]` affordance the reasoning layer can
always act on. See memory `project_yellow_commit_button_style`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np
from loguru import logger
from PIL import Image

from vision.ocr import read_text

# The commit/OK button lights up ~0.32–0.77 yellow AND is a WIDE pill (verb, or
# cost-left+verb-right). A goods TILE's yellow price-bar / "Specialties" banner is
# only a partial highlight (~0.21) on a squarish tile (aspect ~1.8) — NOT a button.
# Require both a strong yellow fill and a wide aspect to reject those highlights.
# How much of a commit button must read as gold. Set from MEASUREMENT of both classes rather
# than from the one button it was first tuned on (2026-09-02):
#
#     Purchase, DISABLED, cost 0 (Tripoli)      0.0207
#     goods tile 'Shea Butter' (Madeira)        0.2100   ← the false positive to exclude
#     ------------------------------- the gap -------------------------------
#     Purchase, ENABLED, 185,250 (Madeira)      0.2798
#     OK on the cart dialog                     0.3212
#
# At 0.28 the cut sat at the very TOP of the enabled range and rejected a lit, priced
# Purchase button by 0.000213 — two ten-thousandths. The buy never committed, the round was
# retried against an already-staged cart, and the mission died on the confirm dialog that
# dangling cart raised.
#
# It reads pale because the pill is two-toned: a DARK BROWN cost panel inset on the left
# ('🪙 185,250') beside a pale cream-gold action half ('Purchase'). Averaged over the whole
# control the gold is diluted, and the dialog's OK — one flat saturated pill — scores higher.
# So the number was never about "how gold is gold"; it was about which button was measured.
#
# 0.25 sits between the tile and the palest enabled button. The margin is NARROW — 0.04 below,
# 0.03 above — and that is worth saying plainly rather than presenting a tuned number as a
# principled one. A first attempt at 0.15 admitted the Shea Butter TILE, which is the very
# false positive the aspect test and this threshold were both written for; the docstring
# already recorded a tile at 0.21 and I had measured a frame that happened to have none.
#
# The durable fix is structural, not chromatic: a commit control sits in the bottom strip and
# carries a commit VERB beside a numeric cost, and a goods tile carries a GOODS NAME. Colour
# should confirm that it is ENABLED (0.28 vs 0.02 is an enormous, reliable gap) rather than
# carry the identification on its own. Left as the next step rather than done here.
YELLOW_MIN_FRAC = 0.25
MIN_ASPECT      = 2.3      # width/height; buttons ≥2.8, tiles ~1.8

# The cost sits at the button's LEFT as `<currency-icon> <cost>`. The icon tells
# us what we'd SPEND: gold coin = ducat, red diamond = RED GEM (real money —
# never autonomously), blue diamond = blue gem. The button is itself yellow/gold
# so a ducat coin blends in — but a red or blue gem stands out as saturated
# red / blue pixels against the yellow. So: red pixels → red_gem, blue → blue_gem,
# neither → ducat. Calibrated on the live top-bar gem counters (blue 0.061 at the
# blue icon, red 0.100 at the red icon, ~0 at the gold coin). See memory
# feedback_commit_gate_misses_yellow_purchase.
GEM_MIN_FRAC = 0.015      # gem-icon pixel fraction over the button's left band


@dataclass
class CommitButton:
    verb: str            # action verb, e.g. "Recruit" / "Buy" / "Sell" ("" if unread)
    cost: str            # currency amount text, e.g. "205,848" ("" if none)
    currency: str        # "ducat" | "red_gem" | "blue_gem" — what the cost SPENDS
    cx: int
    cy: int
    x1: int
    y1: int
    x2: int
    y2: int
    yellow_frac: float

    @property
    def label(self) -> str:
        """The affordance label the reasoning layer sees (verb, or cost fallback)."""
        return self.verb or (f"commit ({self.cost})" if self.cost else "commit")


def yellow_fraction(arr: np.ndarray, x1: int, y1: int, x2: int, y2: int) -> float:
    """Fraction of yellow/gold pixels in a bbox — the commit-button signature."""
    h, w = arr.shape[:2]
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    c = arr[y1:y2, x1:x2].reshape(-1, 3)
    if len(c) == 0:
        return 0.0
    r, g, b = c[:, 0].astype(int), c[:, 1].astype(int), c[:, 2].astype(int)
    return float(((r > 150) & (g > 120) & (b < 110) & (abs(r - g) < 80)).mean())


def cost_currency(arr: np.ndarray, x1: int, y1: int, x2: int, y2: int) -> str:
    """Which currency the button spends, from the cost-icon colour on its LEFT.

    Red diamond → 'red_gem' (real money), blue diamond → 'blue_gem', else the
    gold coin → 'ducat'. Only the leftmost band (icon + cost) is scanned; the
    white/dark verb text on the right never reads as saturated red/blue."""
    h, w = arr.shape[:2]
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    xe = x1 + int(0.40 * (x2 - x1))               # left band: icon + amount
    band = arr[y1:y2, x1:xe].reshape(-1, 3)
    if len(band) == 0:
        return "ducat"
    r, g, b = band[:, 0].astype(int), band[:, 1].astype(int), band[:, 2].astype(int)
    red  = ((r > 130) & (g < 110) & (b < 110) & (r - g > 40) & (r - b > 40)).mean()
    blue = ((b > 120) & (r < 140) & (b - r > 25) & (b - g > 0)).mean()
    if red >= GEM_MIN_FRAC and red >= blue:
        return "red_gem"
    if blue >= GEM_MIN_FRAC:
        return "blue_gem"
    return "ducat"


def looks_like_commit_button(w: int, h: int, yellow_frac: float) -> bool:
    """True for a WIDE, mostly-yellow pill (commit/OK) — rejects a squarish,
    weakly-yellow goods-tile highlight. Calibrated on: Recruit 554×75/0.37 (yes),
    dialog OK 215×77/0.32 (yes), Whisky tile 432×240/0.21 (NO)."""
    return w >= MIN_ASPECT * max(h, 1) and yellow_frac >= YELLOW_MIN_FRAC


# A GOODS TILE CONTAINS NO BUTTONS (user, 2026-09-10: "the yellow banner is not a yellow
# button ... there are no buttons in the good tiles"). These are BANNERS painted across a
# tile to mark what the port is known for, and they are the same gold as a commit control —
# which is the only thing this detector goes by, since in this game a positive button is
# identified by its background and not its wording.
#
# `looks_like_commit_button` was the guard, and it is a SHAPE test: it rejects the squarish,
# weakly-yellow tile highlight. A Specialties banner defeats it by being wide and strongly
# gold — 508x46 at 0.9-ish against a commit pill's 554x75.
#
# Live 2026-09-10 at Lisboa: the hold held 1,841 Almond, which is a LISBOA SPECIALTY, so its
# Sell tile carried the banner. With the basket empty the real Sell button was greyed and
# undetectable, this banner was the only gold thing on screen, and `_find_sell_commit`'s bare
# `commits[0]` handed it back as the Sell button. The tap landed inside the tile, which with
# Put In Bulk staged the whole stack, and the next tick sold all 1,841 — during a trim whose
# keep list named Almond. It had never fired before because the trim runs BEFORE gathering,
# so the hold normally carries goods with no relationship to this port.
_TILE_BANNERS = frozenset([
    "specialties", "specialty", "on sale", "onsale", "recommended", "favorites",
])


def _is_tile_banner(verb: str) -> bool:
    v = (verb or "").strip().lower()
    return bool(v) and v in _TILE_BANNERS


def _split_verb_cost(text: str) -> tuple:
    """Classify OCR tokens: numeric-ish → cost, alphabetic → verb."""
    verb_toks, cost_toks = [], []
    for t in (text or "").replace("\n", " ").split():
        core = t.replace(",", "").replace(".", "").replace("%", "")
        if core.isdigit():
            cost_toks.append(t)
        elif any(ch.isalpha() for ch in t):
            verb_toks.append(t)
    return " ".join(verb_toks).strip(), " ".join(cost_toks).strip()


# Commit verbs that identify the yellow action button by its RIGHT-side label.
# Used ONLY by the TEXT fallback below (when OmniParser fails to emit a button bbox
# for the yellow pill and gives just its <cost> + <verb> as text elements).  Gated
# on the yellow bottom band so goods-tile prices don't false-positive.  "ok" is
# excluded (too generic).  See memory project_yellow_commit_button_style.
COMMIT_VERBS = {
    "sell", "buy", "purchase", "recruit", "confirm", "pay", "gift",
    "exchange", "barter", "invest", "hire", "depart",
}


def _numeric_frac(s: str) -> float:
    """Fraction of digit characters — a cost token like '557,550' is ~all digits."""
    core = s.strip().replace(",", "").replace(".", "").replace(" ", "")
    if not core:
        return 0.0
    return sum(c.isdigit() for c in core) / len(core)


def _detect_from_text(elements, arr, frame, min_yellow) -> List[CommitButton]:
    """Fallback: OmniParser gave no yellow BUTTON bbox for the commit pill, only its
    canonical `<cost> … <verb>` TEXT.  Reconstruct the button from a commit-verb text
    low on screen with a numeric cost to its LEFT on the same row, over a yellow
    region.  Robust to the ~0.63 flakiness where OmniParser drops the button bbox."""
    H = arr.shape[0]
    def _txt(e):
        return (getattr(e, "content", "") or getattr(e, "label", "") or "").strip()
    # TEXT *OR* BUTTON, because the parse returns the control either way and both are the
    # control — the search box lesson (4b94a86), applied here rather than left to the one
    # place it was found. Live 2026-09-02 the commit came back split, the cost '185,250' as a
    # BUTTON and the verb 'Purchase' as TEXT, and a text-only pool could never pair them.
    #
    # This was NOT what broke that run — the threshold above was — and saying so matters:
    # the fallback is for the frames where OmniParser drops the pill's bbox entirely, and it
    # could not have covered for a threshold that rejects the pill when the bbox IS there.
    texts = [e for e in elements
             if getattr(e, "element_type", "") in ("text", "button")
             and getattr(e, "cy", 0) > 0.85 * H]
    out: List[CommitButton] = []
    for v in texts:
        if _txt(v).lower() not in COMMIT_VERBS:
            continue
        cost_el = None
        for c in texts:
            if c is v:
                continue
            if _numeric_frac(_txt(c)) >= 0.6 and abs(c.cy - v.cy) <= 30 and c.cx < v.cx:
                cost_el = c
                break
        x1 = int((cost_el.x1 if cost_el else v.x1)) - 20
        x2 = int(v.x2) + 20
        y1 = int(min(v.y1, cost_el.y1 if cost_el else v.y1)) - 18
        y2 = int(max(v.y2, cost_el.y2 if cost_el else v.y2)) + 18
        x1, y1 = max(x1, 0), max(y1, 0)
        yf = yellow_fraction(arr, x1, y1, x2, y2)
        if yf < min_yellow:
            continue
        out.append(CommitButton(
            verb=_txt(v), cost=(_txt(cost_el) if cost_el else ""),
            currency=cost_currency(arr, x1, y1, x2, y2),
            cx=int(v.cx), cy=int(v.cy),
            x1=x1, y1=y1, x2=x2, y2=y2, yellow_frac=round(yf, 2)))
    return out


def detect_commit_buttons(elements, frame: Image.Image,
                          min_yellow: float = YELLOW_MIN_FRAC) -> List[CommitButton]:
    """Find yellow OmniParser buttons and re-OCR them into {verb, cost}.

    `elements` — raw OmniParser elements (need `.element_type`, `.x1..y2`, `.cx/cy`).
    Returns the normalised commit buttons on screen (usually one; markets can have
    Buy + Sell).  When OmniParser emits no yellow button bbox, falls back to
    reconstructing the commit from its <cost>+<verb> text (`_detect_from_text`).
    """
    arr = np.asarray(frame.convert("RGB"))
    out: List[CommitButton] = []
    for e in elements:
        if getattr(e, "element_type", "") != "button":
            continue
        x1, y1, x2, y2 = e.x1, e.y1, e.x2, e.y2
        yf = yellow_fraction(arr, x1, y1, x2, y2)
        # a real commit/OK button is a WIDE, mostly-yellow pill; a squarish weakly-
        # yellow thing is a goods-tile highlight (tap to LOAD), not the commit button.
        if yf < min_yellow or not looks_like_commit_button(x2 - x1, y2 - y1, yf):
            continue
        verb, cost = _split_verb_cost(read_text(frame.crop((x1, y1, x2, y2))))
        if not verb:
            # verb lives on the right — OCR that portion alone as a fallback
            cut = x1 + int(0.55 * (x2 - x1))
            v2, c2 = _split_verb_cost(read_text(frame.crop((cut, y1, x2, y2))))
            verb = verb or v2
            cost = cost or c2
        if _is_tile_banner(verb):
            # A LABEL, NOT A CONTROL — see `_TILE_BANNERS`. Tapping it taps the tile.
            logger.debug(f"[commit] ignoring the {verb!r} banner at "
                         f"({e.cx},{e.cy}) — a goods tile holds no buttons")
            continue
        out.append(CommitButton(verb=verb, cost=cost,
                                currency=cost_currency(arr, x1, y1, x2, y2),
                                cx=int(getattr(e, "cx", (x1 + x2) // 2)),
                                cy=int(getattr(e, "cy", (y1 + y2) // 2)),
                                x1=x1, y1=y1, x2=x2, y2=y2, yellow_frac=round(yf, 2)))
    # OmniParser flakiness: no yellow BUTTON bbox this frame → reconstruct from text.
    if not out:
        out = _detect_from_text(elements, arr, frame, min_yellow)
    return out
