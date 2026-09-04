# tests/test_a_red_ribbon_is_the_games_refusal.py
#
# "We need to handle the red ribbon on menu items better, as this is a common style in this
# game" (user, 2026-09-04).
#
# A RED RIBBON IS A STYLE, NOT A SENTENCE. `_LOCK_KEYWORDS` was
# ("unavailable", "locked", "requires", "not available") — and the game said **Cannot
# Exchange**. A keyword list is always one wording behind.
#
# Live at Svear, 2026-09-04: the day's barter rounds were spent and the menu carried a red
# `Cannot Exchange` ribbon beside Barter. It matched no keyword, so it was neither filtered
# out of the item list nor collected as an indicator — it became a SIXTH MENU ITEM:
#
#     ['Explore', 'Gifting', 'Loot', 'Recruit Crew', 'Cannot Exchange', 'Barter']
#     Barter.is_locked = False
#
# `_open_barter_panel` therefore skipped the very branch written for this — "NOT A FAILURE,
# this is the game saying the day's barters are used up" — and tapped anyway. Twice: the
# first attempt could not tell "the panel won't open" from "the tap missed", so it retried.
# That second tap raised the notice which began the sequence that ended the mission.
#
# The geometry was never the problem: `Cannot Exchange` cy=587 against `Barter` cy=615 is
# 28px apart, well inside the 50px tolerance. Only the vocabulary failed.
#
# Measured on frame_0344 of trace_barter_cmd_2026-09-04T10-33-18:
#     'Cannot Exchange' ribbon   median R=244 G= 34 B= 69   red 75% of its box
#     'Barter' label             median R= 66 G= 66 B= 68   red  9%
#     'Recruit Crew', 'Loot'                                red  0%

import pathlib

import pytest

pytestmark = pytest.mark.functional

_SESSION = pathlib.Path("data/sessions/trace_barter_cmd_2026-09-04T10-33-18")


def _frame(n: int):
    p = _SESSION / f"frame_{n:04d}.png"
    if not p.exists():
        pytest.skip("the birch session is not in this checkout")
    from PIL import Image
    return Image.open(p)


def _menu(n, with_frame):
    from vision.omniparser import parse_fast_cached
    from vision.region_detectors.left_menu import detect_left_menu
    f = _frame(n)
    els = list(parse_fast_cached(f))
    return detect_left_menu(els, f.width, f.height, frame=f if with_frame else None)


# 344, 349, 352 — the village top menu before, between and after the two wasted taps.
_FRAMES = (344, 349, 352)


def test_the_ribbon_is_not_a_menu_item():
    for n in _FRAMES:
        labels = _menu(n, True).labels()
        assert "Cannot Exchange" not in labels, f"frame {n}: {labels}"
        assert labels == ["Explore", "Gifting", "Loot", "Recruit Crew", "Barter"], f"frame {n}"


def test_the_ribbon_locks_the_item_it_sits_beside():
    for n in _FRAMES:
        assert _menu(n, True).find("Barter")["is_locked"] is True, f"frame {n}"


def test_the_defect_is_pinned_so_the_fix_cannot_be_quietly_dropped():
    """Without the frame there are no pixels, so the old keyword-only path runs — and gets it
    wrong, exactly as it did live. If this ever starts passing, the wording was added to
    `_LOCK_KEYWORDS` and the colour test is no longer carrying the case."""
    m = _menu(344, False)
    assert "Cannot Exchange" in m.labels()
    assert m.find("Barter")["is_locked"] is False


def test_ordinary_labels_are_not_ribbons():
    """The red test must not swallow the menu. `Barter`'s own box is 9% red from the ribbon
    overlapping it; the threshold is 40%."""
    from vision.region_detectors.left_menu import _is_red_ribbon
    from vision.omniparser import parse_fast_cached
    f = _frame(344)
    for e in parse_fast_cached(f):
        if (e.label or "").strip() in ("Explore", "Gifting", "Loot", "Recruit Crew", "Barter"):
            assert _is_red_ribbon(e, f) is False, f"{e.label!r} read as a ribbon"


def test_no_frame_means_no_ribbon_rather_than_a_crash():
    from vision.region_detectors.left_menu import _is_red_ribbon
    from vision.omniparser import parse_fast_cached
    e = next(iter(parse_fast_cached(_frame(344))))
    assert _is_red_ribbon(e, None) is False


def test_the_panel_opener_refuses_without_tapping():
    """The behaviour that matters: two taps became none, and the answer is the same."""
    from unittest.mock import patch
    from actions.barter_panel import _open_barter_panel
    with patch("capture.adb_capture.capture_screen", return_value=_frame(344)), \
         patch("actions.ui.tap_element") as tap:
        result = _open_barter_panel()
    assert result == "unavailable"
    tap.assert_not_called()
