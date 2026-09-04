# tests/test_a_village_is_an_overlay_on_the_sea.py
#
# "This is actually the typical scene when arriving at a village, before selecting a sub menu,
# the center part is translucent. Technically village is a chromed overlay on top of sea
# world. What you see through the translucent part is exactly the sea world. That is why when
# idle at village, it says On Standby at Sea." (user, 2026-09-04)
#
# That is the whole diagnosis. A 224x224 CNN looking at a village sees SEA, because sea is
# literally what is behind it, and the arrival screen — before any sub-menu is chosen — is the
# most transparent state there is. So this is the game's design, met at every village, not a
# quirk of one frame.
#
# Retraining is the wrong lever. CLAUDE.md's own rule is that a downscaled image answers
# WHICH FAMILY and never structure, and separating "village panel over sea" from "notice over
# sea" at 224x224 is exactly a structure question. The structural answer already existed and
# was simply never consulted, because a confident `transient` short-circuits ahead of it.
#
# Measured on the five frames that ended the birch run at Svear — barter panel open, amity
# 98,597/100,000, one tap from its purpose:
#
#     frames 332-339   CNN 'transient' @ 0.87-0.96      _has_village_menu -> True (all five)
#
# Confidently wrong every time; the left menu right every time. `transient` is served by the
# notice-tapper, which tapped the amity bar at (432,172) four times and stalled the mission
# with 962 Iron, 324 Matchlock Gun and 920 Candle aboard.

import pathlib

import pytest

pytestmark = pytest.mark.functional

_SESSION = pathlib.Path("data/sessions/trace_barter_cmd_2026-09-03T22-23-54")


def _frame(n: int):
    p = _SESSION / f"frame_{n:04d}.png"
    if not p.exists():
        pytest.skip("the birch session is not in this checkout")
    from PIL import Image
    return Image.open(p)


# The four taps and the last capture, all the Svear arrival screen.
_STALLED = (332, 334, 336, 338, 339)


def test_the_cnn_really_is_confidently_wrong_here():
    """Pins the DEFECT, so the gate cannot be quietly dropped as unnecessary. If a retrained
    CNN ever gets these right this test fails and the gate can be reconsidered — which is the
    point of asserting it rather than describing it."""
    from vision.family_classifier import classify_family
    for n in _STALLED:
        fam = classify_family(_frame(n))
        assert fam.family == "transient" and fam.confidence >= 0.80, \
            f"frame {n}: CNN now says {fam.family}@{fam.confidence:.2f}"


def test_the_left_menu_is_right_on_every_one_of_them():
    from brain.perceive import _has_village_menu
    for n in _STALLED:
        assert _has_village_menu(_frame(n)) is True, f"frame {n}"


def test_the_structure_wins_and_the_screen_reads_village():
    """The regression itself: the mission stalled one tap from bartering."""
    from brain.perceive import _classify_nav_state
    for n in _STALLED:
        got = _classify_nav_state(_frame(n)).get("location")
        assert got == "village", f"frame {n} -> {got!r}"


def test_a_real_transient_with_no_village_menu_is_untouched():
    """A genuine full-screen notice COVERS the menu, so the test goes False and the gate
    never fires. Without this the notice-tapper would lose the screens it exists for."""
    from PIL import Image
    from unittest.mock import patch
    import brain.perceive as P
    blank = Image.new("RGB", (2400, 1080), (20, 20, 20))
    with patch.object(P, "_has_village_menu", return_value=False):
        got = P._classify_nav_state(blank).get("location")
    assert got != "village"
