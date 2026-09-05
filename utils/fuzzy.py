# utils/fuzzy.py
#
# Fuzzy string matching for OCR output.
#
# OCR produces single-character corruptions: "W0orld" for "World", "over"
# for "Dover" (leading char occluded), "Dan Helder" for "Den Helder".
# Exact substring checks miss these.  The two functions here add fault
# tolerance calibrated to that error profile.
#
# Two distinct use cases:
#
#   fuzzy_contains(text, target)
#     Is `target` present somewhere inside a longer OCR text string?
#     Uses a sliding window of ±2 chars around target length, accepts
#     if any window reaches the similarity threshold.
#     Safe for short targets (< 5 chars): falls back to exact substring only.
#
#   token_sim(a, b) → float
#     Similarity score [0, 1] between two strings of comparable length
#     (individual OCR token vs port/building name).  Use the score directly
#     rather than a boolean; callers apply their own threshold.

from __future__ import annotations
from difflib import SequenceMatcher


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def fuzzy_contains(text: str, target: str, threshold: float = 0.82) -> bool:
    """
    True if `target` appears anywhere in `text` with OCR fault tolerance.

    Algorithm:
      1. Exact substring check first (fast, zero false-positive risk).
      2. For targets ≥ 5 chars: slide a window of lengths [n-2 … n+2]
         over `text`; return True if any window's similarity to `target`
         reaches `threshold`.

    Short targets (< 5 chars) use exact match only — fuzzy on short tokens
    produces too many false positives (e.g. "map" ≈ "tap").

    Examples:
      fuzzy_contains("w0orld map open",  "world map")   → True   # inserted '0'
      fuzzy_contains("16 days 0f sailing left", "days of sailing") → True  # '0' for 'o'
      fuzzy_contains("g0 to city",       "go to city")  → True
      fuzzy_contains("safe waters pass", "world map")   → False
    """
    t = text.lower()
    k = target.lower()

    if k in t:
        return True

    n = len(k)
    if n < 5:
        return False  # exact only for short targets

    for length in range(max(n - 2, 3), n + 3):
        for i in range(len(t) - length + 1):
            if _sim(t[i:i + length], k) >= threshold:
                return True

    return False


def token_sim(a: str, b: str) -> float:
    """
    Similarity score [0, 1] between two OCR tokens / names.

    Use this when comparing individual tokens (port name in list, building
    label) rather than searching inside a longer text.

    Examples:
      token_sim("las palmas", "las palmas") → 1.0
      token_sim("over",       "dover")      → 0.89   # leading char missing
      token_sim("dan helder", "den helder") → 0.90   # one substitution
      token_sim("w0orld",     "world")      → 0.91   # one insertion
      token_sim("palma",      "las palmas") → 0.67   # different port — below 0.75
      token_sim("london",     "lisbon")     → 0.50   # clearly different
    """
    return _sim(a.lower().strip(), b.lower().strip())


def same_good_name(a: str, b: str, *, tol: float = 0.80) -> bool:
    """Whether two OCR'd trade-good names name the SAME good.

    THE EXTRA WORD IS THE WHOLE DIFFERENCE. This game is full of pairs whose names are
    prefixes of one another — Almond / Almond Oil, Duck / Duck Meat, Olive / Olive Oil
    (user, 2026-09-05) — and a substring test says yes to every one of them.

    Live 2026-09-05 at Lisboa, both tiles on screen and both read correctly:

        button 'Almond'      @ (1450, 557)
        button 'Almond Oil'  @ ( 570, 798)     <- what a substring match returned

    The mission bought ~1,020 Almond Oil for ~194,000 ducats and two blue gems, drained
    Lisboa's Almond Oil shelf twice, and carried zero Almond. Nothing downstream could catch
    it: the ledger, the goal counter and the log all say the name they were ASKED for, never
    the one on the tile that was tapped.

    So the words must match one-for-one. Per WORD the comparison stays OCR-tolerant, because
    a tile that reads 'Almend' is still Almond — what it is not is a good with another word
    after it.

    The per-word tolerance is 0.80 because one wrong character in a short word sits just
    under 0.85: 'almond' against 'almend' scores 0.833. Discrimination does not rest on this
    number — the prefix pairs are rejected on WORD COUNT, before any ratio is taken — so it
    is free to be generous enough for real OCR damage. Two genuinely different one-word goods
    stay far below it ('coral'/'corn' 0.67, 'pig'/'fig' 0.67).

    Deliberately not symmetric with `fuzzy_contains`: that answers "does this text contain
    that phrase", which is the right question for a title on a noisy screen and the wrong one
    for choosing between two goods sitting side by side.
    """
    wa, wb = str(a or "").lower().split(), str(b or "").lower().split()
    if not wa or not wb or len(wa) != len(wb):
        return False
    return all(x == y or SequenceMatcher(None, x, y).ratio() >= tol
               for x, y in zip(wa, wb))
