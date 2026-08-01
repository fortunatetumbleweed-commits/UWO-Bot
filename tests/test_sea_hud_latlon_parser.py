"""Tests for the lat/lon parser inside `vision/sea_hud.read_latlon`.

The parser is exposed as `_parse_latlon_text` so we can test the
joined-OCR-tokens → (lat, lon) logic without invoking EasyOCR.
"""
from __future__ import annotations

from vision.sea_hud import _parse_latlon_text


def test_clean_decimals_path_a():
    """Standard happy-path: clean OCR output with periods intact."""
    assert _parse_latlon_text("8.50,33.16") == (8.50, 33.16)


def test_negative_value_path_a():
    """Negative lat or lon should parse — game range is signed."""
    assert _parse_latlon_text("-12.34,-56.78") == (-12.34, -56.78)


def test_space_separated_decimals_dropped_path_b():
    """OCR dropped decimal points — '31 63 17 77' → (31.63, 17.77)."""
    assert _parse_latlon_text("31 63 17 77") == (31.63, 17.77)


def test_mixed_decimal_and_int_legacy_fails_path_c_recovers():
    """Three pieces with one decimal already present — the legacy
    Path B requires 4 pieces and returns None.  Path C recovers it
    when given a prior."""
    # Without prior: legacy paths fail.
    assert _parse_latlon_text("31 63 17.77") is None
    # With prior: Path C combines digit runs and lands on (31.63, 17.77).
    out = _parse_latlon_text("31 63 17.77", prev_latlon=(31.0, 17.0))
    assert out is not None
    assert abs(out[0] - 31.63) < 0.01
    assert abs(out[1] - 17.77) < 0.01


def test_path_a_rejects_out_of_range_values():
    """A garbled value like '218.99' isn't a valid lat — Path A must
    not accept it just because the regex matched."""
    # Without prior, can't disambiguate, returns None.
    assert _parse_latlon_text("218.99,32.69") is None


def test_path_c_recovers_t336_diamond_marker_garble():
    """§§Reference incident: at hug_debug_20260602_111404 t336 a mini-
    map diamond marker overlapped the "." in "8.73" and EasyOCR
    returned "8773,32,69" — Path A finds only one valid decimal,
    Path B has fewer than 4 pieces, and the original parser returned
    None.  With `prev_latlon` hint, Path C should recover."""
    out = _parse_latlon_text("8773,32,69", prev_latlon=(8.50, 33.16))
    assert out is not None
    lat, lon = out
    # Best candidate is (8.773, 32.69) — both within range, closest to
    # the prior (8.50, 33.16).
    assert abs(lat - 8.773) < 0.01
    assert abs(lon - 32.69) < 0.01


def test_path_c_requires_prior_to_disambiguate():
    """Without a prior, ambiguous OCR garble has multiple plausible
    candidates — return None rather than guess wrong."""
    assert _parse_latlon_text("8773,32,69", prev_latlon=None) is None


def test_path_c_uses_prior_for_tiebreak_not_unconditionally():
    """When the OCR is clean (Path A succeeds) and the result is
    plausible given prior, prior shouldn't override OCR.

    Updated 2026-06-02: motion budget added.  If the prior is far from
    truth (e.g. cached prior is stale after a long blackout) Path A's
    candidate may be rejected by motion budget; Path C's Pass 3
    fallback should still ultimately return the closest-to-prev value.
    Here we use a prior near truth so Path A passes through cleanly.
    """
    out = _parse_latlon_text("8.50,33.16", prev_latlon=(8.45, 33.20))
    assert out == (8.50, 33.16)


def test_path_c_rejects_implausible_candidates():
    """A 4-digit run like '8773' has interpretations 8.773 / 87.73 /
    877.3.  877.3 is > 90 (invalid lat); 87.73 is far from prior;
    8.773 is closest to prior.  Verify 877.3 isn't ever returned."""
    out = _parse_latlon_text("8773,32,69", prev_latlon=(8.50, 33.16))
    assert out is not None
    lat, _ = out
    assert abs(lat) <= 90.0


def test_path_c_garble_far_from_prior_still_picks_closest_in_range():
    """Even if no candidate is *near* the prior, return the closest one
    among the in-range candidates.  Plausibility tiebreak should never
    silently return None when there is a valid candidate."""
    # Prior is (0, 0), OCR garble has only (8.773, 32.69) etc. in range.
    out = _parse_latlon_text("8773,32,69", prev_latlon=(0.0, 0.0))
    assert out is not None
    lat, lon = out
    # Within valid ranges:
    assert -90.0 <= lat <= 90.0
    assert -180.0 <= lon <= 180.0


def test_empty_input_returns_none():
    assert _parse_latlon_text("") is None
    assert _parse_latlon_text("", prev_latlon=(8.0, 33.0)) is None


def test_garbage_input_returns_none():
    """Pure non-numeric garbage."""
    assert _parse_latlon_text("hello world") is None


def test_path_a_accepts_clean_parse_even_when_far_from_prev():
    """§ dest8 voyage 2026-06-01.  Motion budget used to gate Path A,
    which created a cascade lock-in: once a bad cached value entered
    prev, every subsequent CORRECT clean read got rejected as "too
    far from prev", Path C reconstructed a new bad value close to the
    bad prev, and the cache stayed wrong for 100+ reads.

    Fix: Path A trusts two in-range decimals unconditionally; one-shot
    OCR garbles (e.g. the t331 leading-digit case) self-correct on
    the next clean read instead of poisoning the cache forever.
    """
    # t331-style OCR garble: "28.73,32.82" instead of "8.73,32.82".
    # New behavior: Path A returns (28.73, 32.82); next read will
    # naturally recover when OCR is clean again.
    out = _parse_latlon_text("28,73,32,82", prev_latlon=(8.67, 32.88))
    assert out == (28.73, 32.82)


def test_path_a_motion_budget_pass_through_when_close_to_prev():
    """A clean read close to the prior should still go through Path A
    unchanged, even with prev supplied."""
    out = _parse_latlon_text("8.50,33.16", prev_latlon=(8.45, 33.20))
    assert out == (8.50, 33.16)


def test_path_a_no_motion_budget_when_no_prior():
    """First read (no prev) — motion budget can't apply, fall back to
    range gate only.  Should accept a plausible value."""
    out = _parse_latlon_text("8.50,33.16", prev_latlon=None)
    assert out == (8.50, 33.16)


def test_path_c_drop_trailing_digit_variant():
    """Coverage for the symmetric case where OCR appended a spurious
    trailing digit (less common than prepended but possible at the
    other side of the text region)."""
    # If true value is 8.73 and OCR appended a trailing "5" making
    # "8735", Path C's drop-trailing-digit variant should recover.
    out = _parse_latlon_text("8735,32,69", prev_latlon=(8.50, 33.16))
    assert out is not None
    lat, lon = out
    assert abs(lat - 8.73) < 0.05
