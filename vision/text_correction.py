# vision/text_correction.py
#
# Fuzzy-match correction for noisy OCR reads of port names and sea-region
# (waters) names.  Both have known closed sets — UWO has a finite list of
# ports and named sea regions — so a single fuzzy match against the set
# canonicalises corrupted reads without an LLM call.
#
# Origin: 2026-05-17.  After empirical tests on captured frames showed:
#
#   - EasyOCR fuses port-title text with overlapping NPC speech-bubble
#     fragments into a single line, producing reads like 'Amsterdamads!'
#     and 'Amsterdamez familyl' (bubble suffixes fused with 'Amsterdam').
#     The fusion happens inside EasyOCR before OmniParser sees it; we
#     cannot prevent it at the vision layer alone.
#
#   - Sea waters names render in red/orange against animated water and
#     suffer plain OCR character confusion ('Lauless Watters' instead
#     of 'Lawless Waters').
#
#   - Past corruptions in production logs: 'Amsterdanted', 'home -',
#     'horizons!', 'Amsterdamrt', 'comtagious aiseases' (this last one
#     is so far gone fuzzy match correctly rejects it).
#
# Vision-level interventions (luminance thresholds, min_size, region
# crops, color filters) each help partially but none solve the problem
# universally — see /tmp/easyocr_*_probe.py for the empirical record.
# Fuzzy matching against the known set handles every corruption pattern
# observed in one place with deterministic, debuggable behaviour.

from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger


# ── Known port names ───────────────────────────────────────────────────────
#
# The authoritative port list lives in
# `memory/knowledge/world_map/port_coordinates.json` (baked from
# voyage.tw — ~224 ports).  `_SEED_PORTS` below is only a FALLBACK used
# when that file is missing or unreadable (fresh checkout before
# tools/bake_* has run, or in test environments).  In normal operation
# the seed list is shadowed entirely by the catalogue.  See
# `_known_ports()` for the tiered resolution.
#
# Keep this list of CANONICAL display names (mixed case, real spaces).
# We lowercase for matching and return the display form on success.
_SEED_PORTS: tuple[str, ...] = (
    # England / North Atlantic
    "London", "Plymouth", "Edinburgh", "Dublin",
    # Netherlands / Belgium
    "Amsterdam", "Antwerp",
    # Iberian peninsula
    "Lisbon", "Porto", "Seville", "Cadiz", "Madeira",
    # Mediterranean
    "Marseille", "Genoa", "Venice", "Naples", "Athens",
    "Istanbul", "Alexandria", "Tunis", "Algiers",
    # Caribbean
    "Port Royal", "Havana", "Santiago de Cuba", "Cartagena",
    "San Juan", "Santo Domingo",
    # West Africa
    "Sao Tome", "Luanda", "Cape Verde",
    # Indian Ocean / South Asia
    "Diu", "Calicut", "Goa", "Colombo", "Ceylon",
    "Aceh", "Surabaya", "Malacca",
    # East Asia
    "Hong Kong", "Macao", "Nagasaki",
    # Nordic
    "Bergen", "Oslo", "Stockholm", "Copenhagen",
    "Reykjavik", "Cohasset",
    # Misc
    "Montpellier", "Socotra",
)
_SEED_PORTS_COUNT = len(_SEED_PORTS)


# ── Known waters / sea region names ────────────────────────────────────────
#
# Static list; we don't yet persist these in the KB.  Add as we discover
# new sea regions.
_SEED_WATERS: tuple[str, ...] = (
    "Atlantic Ocean",
    "Pacific Ocean",
    "Indian Ocean",
    "Arctic Ocean",
    "Mediterranean Sea",
    "North Sea",
    "Baltic Sea",
    "Caribbean Sea",
    "Red Sea",
    "Arabian Sea",
    "Bay of Bengal",
    "South China Sea",
    "Lawless Waters",
    "Safe Waters",
    "Dangerous Waters",
)


# ── Loader ─────────────────────────────────────────────────────────────────

_PORT_KB_DIR = Path("memory/knowledge/ports")
_PORT_COORDS_FILE = Path("memory/knowledge/world_map/port_coordinates.json")

# Process-level cache for the 224 voyage.tw canonical names — the file is
# baked at build time and never changes within a run.
_VOYAGE_PORT_NAMES_CACHE: Optional[frozenset[str]] = None


def _voyage_port_names() -> frozenset[str]:
    """Canonical port names baked from voyage.tw — the authoritative tier.

    The file `memory/knowledge/world_map/port_coordinates.json` is built
    by `tools/bake_*` from voyage.tw's `json_city` table + `lang_4.js`
    English overrides.  It contains every UWO port (~224) under
    `ports.<slug>.name` in canonical display form.  When present, this
    set is the SINGLE SOURCE OF TRUTH for port-name fuzzy matching;
    `_SEED_PORTS` is only consulted as a fallback when this file is
    missing or unreadable.

    Returns an empty frozenset when the file is absent — callers
    (`_known_ports`) interpret that as "use the seed fallback".

    Origin: 2026-05-22.  Bot OCR'd 'Las Palmas' correctly but fuzzy
    match returned 0.42 because Las Palmas was in the catalogue but
    not in `_SEED_PORTS`, and the loader was unioning seeds + catalogue
    instead of preferring the catalogue.
    """
    global _VOYAGE_PORT_NAMES_CACHE
    if _VOYAGE_PORT_NAMES_CACHE is not None:
        return _VOYAGE_PORT_NAMES_CACHE
    names: set[str] = set()
    if _PORT_COORDS_FILE.exists():
        try:
            data = json.loads(_PORT_COORDS_FILE.read_text(encoding="utf-8"))
            for entry in (data.get("ports") or {}).values():
                name = (entry or {}).get("name")
                if isinstance(name, str) and name.strip():
                    names.add(name.strip())
        except Exception as e:
            logger.warning(
                f"[text_correction] failed to load {_PORT_COORDS_FILE}: {e}"
            )
    _VOYAGE_PORT_NAMES_CACHE = frozenset(names)
    return _VOYAGE_PORT_NAMES_CACHE


def _known_ports() -> list[str]:
    """Tiered resolution of the candidate port set used for fuzzy matching.

    Tiers (the first one with content wins as the canonical set; KB
    discoveries always layer on top):

      Tier 1 — voyage.tw catalogue
        `memory/knowledge/world_map/port_coordinates.json`, baked from
        voyage.tw (~224 ports).  When present, this IS the truth.
        Will grow as the developer re-runs `tools/bake_*` and as the
        game adds ports.

      Tier 2 — `_SEED_PORTS` (fallback only)
        Used solely when Tier 1 is missing or unreadable (fresh
        checkout, broken JSON).  ~52 hand-maintained names; intended
        as a safety net, not as a parallel source.

      Tier 3 — KB stubs `memory/knowledge/ports/<slug>.json`
        Written by the bot when it learns about a port (visit history,
        per-port notes).  Always layered on top of the canonical set
        so genuinely-novel discoveries — ports not yet in the catalogue
        — are still fuzzy-matchable.  Entries that smell like a
        corrupted version of a canonical name (similarity 0.5-0.95)
        are rejected; without that guard, a previously-corrupted KB
        slug like 'amsterdanted' would match its own corrupted form
        back to itself with ratio 1.0 and defeat canonicalisation.
    """
    # Tier 1 vs Tier 2: voyage catalogue wins when present.
    voyage = _voyage_port_names()
    if voyage:
        canonical: set[str] = set(voyage)
        canonical_source = "voyage.tw"
    else:
        canonical = set(_SEED_PORTS)
        canonical_source = "seed-fallback"
        logger.debug(
            f"[text_correction] port_coordinates.json unavailable; "
            f"using {_SEED_PORTS_COUNT}-name seed list as fallback"
        )

    # Tier 3: union in KB-discovered novel ports.  Corruption filter
    # compares against whichever canonical set we ended up with.
    canonical_lower = {p.lower() for p in canonical}
    names: set[str] = set(canonical)
    if _PORT_KB_DIR.exists():
        for fp in _PORT_KB_DIR.glob("*.json"):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            raw = (data.get("port") or "").strip()
            if not raw:
                continue
            cleaned = raw.replace("_", " ").strip()
            if not _looks_like_port_slug(cleaned):
                continue
            cleaned_lower = cleaned.lower()
            if cleaned_lower in canonical_lower:
                continue
            corrupted_of = _is_likely_corruption_of_canonical(
                cleaned_lower, canonical_lower,
            )
            if corrupted_of is not None:
                logger.debug(
                    f"[text_correction] skipping KB entry {cleaned!r} as "
                    f"likely corruption of canonical port {corrupted_of!r} "
                    f"(tier: {canonical_source})"
                )
                continue
            names.add(cleaned.title())
    return sorted(names)


def _is_likely_corruption_of_canonical(
    candidate_lower: str, canonical_lower: set[str],
) -> Optional[str]:
    """Return the canonical name this candidate looks like a corruption
    of, or None when the candidate is genuinely novel.

    A candidate is "corrupted" if it's similar-but-not-identical to a
    canonical entry.  Similarity window (0.5, 0.95):
      - ≥ 0.95: essentially the same (caller's exact-match check
        should have caught this already; treat as duplicate).
      - 0.5 to 0.95: smells like OCR drift from the canonical name
        (e.g. 'amsterdanted' vs 'amsterdam' = 0.76).  Reject.
      - < 0.5: genuinely different — likely a real new port.
    """
    for canonical in canonical_lower:
        ratio = SequenceMatcher(None, candidate_lower, canonical).ratio()
        if 0.5 < ratio < 0.95:
            return canonical
    return None


def _looks_like_port_slug(s: str) -> bool:
    """Reject obviously-corrupted candidates like 'home -', 'amsterdanted'.

    Heuristic: must be ≥ 3 chars, must contain at least one vowel, must
    not contain digits or odd punctuation.  Slugs are pre-lowercased.
    """
    if len(s) < 3:
        return False
    if not any(c in s for c in "aeiouAEIOU"):
        return False
    for ch in s:
        if ch.isalpha() or ch in " '-":
            continue
        # Anything else: digit, punctuation, slash, etc.
        return False
    # Heuristic against the specific corruptions we've seen.  'home -'
    # and 'amsterdanted' both pass the above; the seed list outranks
    # them anyway, but we exclude trailing-dash patterns explicitly.
    if s.endswith("-") or s.endswith(" "):
        return False
    return True


# ── Public API ─────────────────────────────────────────────────────────────


# Similarity cutoff: minimum ratio to accept a fuzzy match.  Calibrated
# empirically against observed corruptions:
#   'Amsterdamads!'        vs 'Amsterdam'  → 0.82
#   'Amsterdamez familyl'  vs 'Amsterdam'  → 0.62
#   'Amsterdanted'         vs 'Amsterdam'  → 0.76
#   'Amsterdamrt'          vs 'Amsterdam'  → 0.90
#   'Lauless Watters'      vs 'Lawless Waters' → 0.86
# Garbage like 'home -' or 'horizons!' is well below 0.6 against any
# real port name.  0.6 is the standard difflib default for the same
# reason.
# HOW CLOSE A READ MUST BE BEFORE IT IS THE SAME NAME.
#
# This corrects CORRUPTION of a port name; it is not a nearest-neighbour search over a
# world atlas. Every string on screen gets offered to it, and most of them are not ports —
# so a weak match is evidence the read was never a port name at all, not evidence about
# which port it is.
#
# Calibrated on the three reads this has actually seen:
#     'Amsterdamads!' -> Amsterdam   0.818   genuine corruption, must survive
#     'tac'           -> Tacoma      0.67    a fragment (also caught by the length rule)
#     'Herring'       -> Peking      0.62    a TRADE GOOD, on a screen covered in them
#
# The last one cost a leg: the fleet stood in Amsterdam, which sources the Iron the mission
# had just asked for, and left without buying because it believed it was in Peking (live
# 2026-08-29). Returning None is cheap — the caller reads again — and a wrong port identity
# is the input to deciding where the fleet is and where it sails next.
# Corruption that ADDS text still CONTAINS the name, and that containment is the evidence.
# Without it a match is only a nearest neighbour in an atlas.
_CUTOFF = 0.6
# A CLEAR WINNER, NOT JUST A BEST ONE. Corruption of a real name leaves it far ahead of the
# field; a word that is not a port at all sits in a flat crowd of near-ties, and the top of
# a noise distribution is not evidence.
#
#     'fdinhuroh'     -> Edinburgh 0.667  runner-up Diu     0.500   margin 0.167  genuine
#     'amsterdamads!' -> Amsterdam 0.818  runner-up Las P.  0.522   margin 0.296  genuine
#     'herring'       -> Peking    0.615  runner-up Kuching 0.571   margin 0.044  a GOOD
#     'tac'           -> Tacoma    0.667  runner-up Aceh    0.571   margin 0.095  a fragment
#
# Substitution corruption ('Fdinhuroh') does not CONTAIN the name, so containment alone
# rejected a read this has always corrected. The margin admits it and still refuses Herring.
_MIN_MARGIN = 0.12


# Generic UI titles that the game uses as the top-left label on
# certain screens, NOT as a place name.  Fuzzy-matching any of these
# against a city or village catalogue will produce a false positive
# (`Village → Seville @ 0.71`, `City → Cordoba @ 0.55`, etc.) — the
# resulting wrong canonicalisation then poisons every downstream
# check.  Treat them as a hard reject before any fuzzy match runs.
# Origin: 2026-05-23 Berber sail.  In-game village screens show
# `Village` literally as the top-left title; `correct_port_name`
# canonicalised that to `Seville`, then `correct_village_name`
# turned `Seville` into `Svear Village`, and the bot believed it
# was in the wrong village.
_GENERIC_TITLE_WORDS = frozenset({
    "village", "town", "port", "city", "harbor", "harbour",
    "settlement", "outpost", "island",
})


# Market / sub-menu action words AND port-building menu titles that appear as
# the top-left title on non-overworld screens.  Same failure mode as the generic
# titles above: fuzzy-matching them against the port catalogue mis-canonicalises
# them (or read_port_name passes the raw word through), which `_is_on_overworld`
# then reads as a visible port name and falsely confirms as port_overworld.
# Origin: 2026-08-12 London↔Amsterdam run — 'Sell' → 'Seville' @ 0.73 (buy_all
# skipped as if 'at Seville'); 'Market'/'Purchase' passed through raw and each
# falsely confirmed a building/tab screen as overworld.  Hard-reject before any
# fuzzy match.
_UI_ACTION_WORDS = frozenset({
    # market tab / action words
    "sell", "buy", "purchase", "supply", "supplies",
    # port building / menu titles (top-left label on those screens)
    "market", "shipyard", "bank", "inn", "cathedral", "union",
    "trade", "shop", "item", "warehouse", "guild",
})


def _is_generic_title(raw: str) -> bool:
    """True when *raw* is a single bare word that is never a place name — a
    generic UI title ('Village', 'Port') or a market action/tab label ('Sell',
    'Purchase', 'Supply').

    Multi-word titles ('Las Palmas', 'Berber Village') do not match — only the
    single-token case is rejected.
    """
    if not raw:
        return False
    s = raw.strip().lower()
    if not s or " " in s:
        return False
    return s in _GENERIC_TITLE_WORDS or s in _UI_ACTION_WORDS


def _best_match(raw: str, candidates: list[str]) -> Tuple[Optional[str], float]:
    """Find the candidate with the highest similarity ratio against raw.

    Returns (best_candidate, ratio).  Comparison is case-insensitive
    but the returned candidate preserves its original display case.
    Ratio uses difflib.SequenceMatcher (Ratcliff-Obershelp).
    """
    if not raw:
        return None, 0.0
    raw_lower = raw.lower()
    best: Optional[str] = None
    best_ratio = 0.0
    for c in candidates:
        ratio = SequenceMatcher(None, raw_lower, c.lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = c
    return best, best_ratio


# A read shorter than this fraction of the candidate is a FRAGMENT, not a corrupted name —
# unless the similarity is near-perfect anyway.
_MIN_FRAGMENT_RATIO = 0.6
_SHORT_READ_RATIO = 0.85


def _strip(text: str) -> str:
    """Casefolded and accent-free, for asking whether one name sits inside another."""
    import unicodedata
    from memory.places import fold_name          # one fold, shared — see its docstring
    return fold_name(text)


def _match_margin(raw: str, match: str) -> float:
    """How far the winner beats the next-best port. A flat field means no signal."""
    import difflib
    low = (raw or "").lower()
    scores = sorted(difflib.SequenceMatcher(None, low, p.lower()).ratio()
                    for p in _known_ports() if p != match)
    return (difflib.SequenceMatcher(None, low, match.lower()).ratio()
            - (scores[-1] if scores else 0.0))


def correct_port_name(raw: str) -> Tuple[Optional[str], float]:
    """Canonicalise a noisy OCR read against the known port list.

    Args:
        raw: the text OCR returned (e.g. 'Amsterdamads!').

    Returns:
        (canonical_name, similarity).  When similarity >= 0.6,
        canonical_name is the matched port from the known set.
        Otherwise canonical_name is None — caller decides whether to
        use the raw read, retry, or escalate.

    Examples:
        >>> correct_port_name("Amsterdam")
        ('Amsterdam', 1.0)
        >>> correct_port_name("Amsterdamads!")
        ('Amsterdam', 0.818...)
        >>> correct_port_name("home -")
        (None, ...)   # below cutoff
    """
    if _is_generic_title(raw):
        logger.debug(
            f"[text_correction] port {raw!r} is a generic UI title — "
            "skipping port canonicalisation"
        )
        return None, 0.0
    candidates = _known_ports()
    match, ratio = _best_match(raw, candidates)
    # A FRAGMENT MUST NOT BE STRETCHED ONTO A LONGER NAME. Live 2026-08-26 the fleet was at
    # BARCELONA, OCR caught the three letters 'tac', and this matched 'Tacoma' at 0.67 — a
    # port on the Pacific coast of North America. The caller then logged "Overworld
    # confirmed: port name 'Tacoma' visible". A wrong port identity is not a cosmetic error;
    # it is the input to deciding where the fleet is and where it must sail next.
    #
    # Short reads cannot simply be banned — Diu, Edo, Ezo and Goa are real ports. What is
    # not plausible is a read barely half the length of the name it supposedly is. Real
    # corruption ADDS characters ('Amsterdamads!' → Amsterdam), it does not halve them.
    if match is not None and len(raw or "") < _MIN_FRAGMENT_RATIO * len(match) \
            and ratio < _SHORT_READ_RATIO:
        logger.debug(
            f"[text_correction] port {raw!r} is too short to be {match!r} "
            f"({len(raw or '')} vs {len(match)} chars, ratio {ratio:.2f}) — rejecting"
        )
        return None, ratio
    # DOES THE READ ACTUALLY CONTAIN THE NAME? 'Amsterdamez familyl' does — OCR fused the
    # neighbouring label onto it, and the port is still in there. 'Herring' contains nothing
    # of 'Peking'; it is a different word that merely scores close, which is what every label
    # on a busy screen does against a 224-port atlas.
    contains = bool(match) and _strip(match) in _strip(raw or "")
    if match is not None and not contains and _match_margin(raw, match) < _MIN_MARGIN:
        logger.debug(
            f"[text_correction] port {raw!r} -> {match!r} at {ratio:.2f} but the field is "
            f"flat (margin {_match_margin(raw, match):.3f}) — not a port name"
        )
        return None, ratio
    if match is None or ratio < _CUTOFF:
        if raw:
            logger.debug(
                f"[text_correction] port {raw!r} → no match above {_CUTOFF} "
                f"(best={match!r} ratio={ratio:.2f})"
            )
        return None, ratio
    if match.lower() != (raw or "").lower():
        logger.info(
            f"[text_correction] port {raw!r} → {match!r} (similarity {ratio:.2f})"
        )
    return match, ratio


_VILLAGE_CATALOGUE_PATH = Path("memory/knowledge/world_map/village_coordinates.json")
_village_cache: Optional[list[str]] = None


def _known_villages() -> list[str]:
    """Return display names from the village catalogue, lazily loaded.
    Each entry's 'name' field is the human-facing village name
    (e.g. 'Berber Village')."""
    global _village_cache
    if _village_cache is None:
        try:
            data = json.loads(_VILLAGE_CATALOGUE_PATH.read_text(encoding="utf-8"))
            villages = data.get("villages", {}) or {}
            _village_cache = [
                (entry.get("name") or "").strip()
                for entry in villages.values()
                if (entry.get("name") or "").strip()
            ]
        except Exception as exc:
            logger.debug(f"[text_correction] village catalogue load failed: {exc}")
            _village_cache = []
    return list(_village_cache)


def correct_village_name(raw: str) -> Tuple[Optional[str], float]:
    """Canonicalise a noisy OCR read against the known village list.

    Returns (canonical_name, similarity).  When similarity >= _CUTOFF,
    canonical_name is the matched village display name (e.g.
    'Berber Village').  Otherwise (None, ratio).

    Useful for distinguishing village interior screens from
    port_overworld in the classifier — they share the chrome shape but
    the top-left text matches a different catalogue.
    """
    if _is_generic_title(raw):
        logger.debug(
            f"[text_correction] village {raw!r} is a generic UI title — "
            "skipping village canonicalisation"
        )
        return None, 0.0
    candidates = _known_villages()
    if not candidates:
        return None, 0.0
    match, ratio = _best_match(raw, candidates)
    # DOES THE READ ACTUALLY CONTAIN THE NAME? 'Amsterdamez familyl' does — OCR fused the
    # neighbouring label onto it, and the port is still in there. 'Herring' contains nothing
    # of 'Peking'; it is a different word that merely scores close, which is what every label
    # on a busy screen does against a 224-port atlas.
    contains = bool(match) and _strip(match) in _strip(raw or "")
    if match is not None and not contains and _match_margin(raw, match) < _MIN_MARGIN:
        logger.debug(
            f"[text_correction] port {raw!r} -> {match!r} at {ratio:.2f} but the field is "
            f"flat (margin {_match_margin(raw, match):.3f}) — not a port name"
        )
        return None, ratio
    if match is None or ratio < _CUTOFF:
        if raw:
            logger.debug(
                f"[text_correction] village {raw!r} → no match above {_CUTOFF} "
                f"(best={match!r} ratio={ratio:.2f})"
            )
        return None, ratio
    if match.lower() != (raw or "").lower():
        logger.info(
            f"[text_correction] village {raw!r} → {match!r} (similarity {ratio:.2f})"
        )
    return match, ratio


def correct_waters_name(raw: str) -> Tuple[Optional[str], float]:
    """Canonicalise a noisy OCR read against the known waters/sea-region list.

    Same semantics as `correct_port_name` but matches against the
    waters list.  Handles the `Lauless Watters → Lawless Waters` case.
    """
    match, ratio = _best_match(raw, list(_SEED_WATERS))
    # DOES THE READ ACTUALLY CONTAIN THE NAME? 'Amsterdamez familyl' does — OCR fused the
    # neighbouring label onto it, and the port is still in there. 'Herring' contains nothing
    # of 'Peking'; it is a different word that merely scores close, which is what every label
    # on a busy screen does against a 224-port atlas.
    contains = bool(match) and _strip(match) in _strip(raw or "")
    if match is not None and not contains and _match_margin(raw, match) < _MIN_MARGIN:
        logger.debug(
            f"[text_correction] port {raw!r} -> {match!r} at {ratio:.2f} but the field is "
            f"flat (margin {_match_margin(raw, match):.3f}) — not a port name"
        )
        return None, ratio
    if match is None or ratio < _CUTOFF:
        if raw:
            logger.debug(
                f"[text_correction] waters {raw!r} → no match above {_CUTOFF} "
                f"(best={match!r} ratio={ratio:.2f})"
            )
        return None, ratio
    if match.lower() != (raw or "").lower():
        logger.info(
            f"[text_correction] waters {raw!r} → {match!r} (similarity {ratio:.2f})"
        )
    return match, ratio
