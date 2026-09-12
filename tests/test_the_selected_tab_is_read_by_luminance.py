# tests/test_the_selected_tab_is_read_by_luminance.py
#
# WARMTH IS NOT THE RIGHT MEASURE IN THIS GAME (user, 2026-09-02).
#
# A selected tab is a LIGHTER panel. An unselected one is a dark translucent panel with the
# world showing THROUGH it. So brightness measures the highlight, and warmth (R-B) measures
# whatever happens to lie behind the tabs that are not selected.
#
# Live 2026-09-02 the route tail died on exactly that. The Route tab WAS selected — opaque
# white, which blocks the map — and scored the LOWEST warmth of the four, because white has
# R=G=B. Warm terrain under Explore scored 49.9, so warmth named Explore three times running:
#
#     warmth [19.8, 49.9,   7.7, 13.2]  -> Explore   WRONG
#     luma   [70.6, 64.4, 186.2, 49.0]  -> Route     right, by 2.6x
#
#     'route' did not take — the tap was swallowed      (x3)
#     FAILED at step mission: could not set a course for 'Sans to London'
#
# The taps had all landed. The verifier was wrong, and the mission failed with five barter
# rounds of cargo aboard.
#
# Warmth was ALREADY known to be the wrong style here — the code said "this strip lights
# white, not gold" and carried a brightness fallback. That fallback ran only when warmth
# reported "cannot tell". A fallback for UNCERTAINTY cannot save you from a confident wrong
# answer, so the fix is to ask the better signal FIRST.

import pathlib

import pytest

pytestmark = pytest.mark.functional

_MAP = pathlib.Path("data/sessions/trace_barter_cmd_2026-09-02T20-46-53")
_PORT = pathlib.Path("data/sessions/trace_barter_cmd_2026-09-02T17-50-37/frame_0008.png")

# The world-map strip, at the x the bot itself taps (it tapped route @ 1238,47).
_MAP_TABS = [(860, 47), (1047, 47), (1238, 47), (1425, 47)]     # Port Explore Route Trade


def _img(p):
    if not p.exists():
        pytest.skip(f"{p} not in the repo (real-frame fixture)")
    from PIL import Image
    return Image.open(p)


# frame_0299 is the look taken BEFORE the first 'route' tap. Route is already selected there:
# luma [73, 66, 187, 51]. The bot then tapped an already-selected tab three times because the
# verifier said Explore, and failed the mission. The whole defect is in this one frame.
_ROUTE_FRAME = _MAP / "frame_0299.png"


def test_luminance_answers_the_world_map_strip():
    """187 against 51-73 needs no tuning; warmth on the same frame picks a losing tab."""
    from actions.sail_actions import _brightest_tab
    assert _brightest_tab(_img(_ROUTE_FRAME), _MAP_TABS) == 2, "Route is the selected tab"


def test_warmth_would_have_got_this_frame_WRONG():
    """The regression itself. Warmth ranks the tab with the warmest MAP behind it, and the
    selected tab blocks the map with opaque white, so it scores LOWEST of the four."""
    import numpy as np
    arr = np.asarray(_img(_ROUTE_FRAME).convert("RGB")).astype(float)
    warm = [float(arr[19:75, cx - 35:cx + 35, 0].mean() - arr[19:75, cx - 35:cx + 35, 2].mean())
            for cx, _ in _MAP_TABS]
    assert warm.index(max(warm)) != 2, "fixture no longer demonstrates warmth failing"
    assert warm[2] == min(warm), "the selected white tab should be the LEAST warm"


def test_the_verifier_now_names_route_on_that_frame():
    """End to end through the public function, on the frame that killed the run."""
    from actions.sail_actions import selected_tab_index
    assert selected_tab_index(_img(_ROUTE_FRAME), _MAP_TABS, trailing_toggles=0) == 2


def test_the_port_strip_still_gets_an_answer():
    """Gold is bright, but only 1.18x its neighbours against the 1.8x `_brightest_tab` wants,
    where the map's white tab is 2.89x. So luminance DECLINES here and warmth still answers
    (gold 56.3 vs 22.0 / 21.9). Dropping warmth entirely would leave this strip with no hint.

    It is only ever a hint there: `_ensure_tab` re-orders which tab it tries first, so a wrong
    answer costs one tap rather than a mission."""
    from actions.sail_actions import selected_tab_index
    from actions.port_panel import _tab_strip_candidates
    f = _img(_PORT)
    cands = _tab_strip_candidates(f)
    if len(cands) < 3:
        pytest.skip("tab strip not detected in this fixture")
    assert selected_tab_index(f, cands) == 1, "Buildings is the selected tab on this frame"


def test_luminance_is_consulted_before_warmth():
    """The ordering IS the fix. Pinned structurally so it cannot be swapped back: with a frame
    where the two disagree, the answer must be luminance's."""
    import inspect
    from actions import sail_actions
    src = inspect.getsource(sail_actions.selected_tab_index)
    i_bright = src.find("_brightest_tab")
    i_warm = src.find("cell[..., 0].mean() - cell[..., 2].mean()")
    assert i_bright != -1 and i_warm != -1
    assert i_bright < i_warm, "warmth must not be consulted before luminance"


def test_warmth_is_used_nowhere_else():
    """Surveyed 2026-09-02: R-B appears in this one function and nowhere else in the tree.
    The other colour tests are different measures — `yellow_fraction` is a yellow MASK,
    `cost_currency` separates saturated red from blue, `_tile_looks_sold_out` uses saturation
    and brightness. If warmth spreads, this test should be the thing that notices."""
    import subprocess
    out = subprocess.run(
        ["grep", "-rln", r"\[\.\.\., 0\].mean() - ", "actions", "vision", "brain"],
        capture_output=True, text=True).stdout.split()
    assert out == ["actions/sail_actions.py"], f"warmth has spread to: {out}"


def test_the_independent_pin_must_be_excluded_before_brightness():
    """THE TRAILING TOGGLE MATTERS MORE TO LUMINANCE THAN IT DID TO WARMTH.

    On the port strip two tabs are lit at once (user, 2026-09-02): Tasks/Buildings/Players
    are mutually exclusive, and the location PIN is an independent toggle — switch it off and
    the minimap hides. So "lit" does not mean "selected" there.

    And the pin is the BRIGHTEST thing on the strip:

        tasks 140.8 | buildings 168.2 (selected) | players 144.7 | pin 180.1 (also on)

    Brightness over the whole strip would therefore rank an independent toggle above the
    actual selection. Only the mutually exclusive group may be compared, which is what
    `trailing_toggles` exists for — and it is now protecting the primary signal, not a
    fallback."""
    from actions.sail_actions import _brightest_tab
    from actions.port_panel import _tab_strip_candidates
    f = _img(_PORT)
    cands = _tab_strip_candidates(f)
    if len(cands) < 4:
        pytest.skip("tab strip not fully detected in this fixture")
    # the pin, included, is the brightest — so it must never be in the compared group
    import numpy as np
    arr = np.asarray(f.convert("L")).astype(float)
    lums = [float(arr[cy - 28:cy + 28, cx - 35:cx + 35].mean()) for cx, cy in cands]
    assert lums.index(max(lums)) == 3, "the fixture no longer shows the pin as brightest"
    # excluded, the answer is the real selection
    assert _brightest_tab(f, cands[:3]) in (None, 1), "must never name a non-exclusive tab"
