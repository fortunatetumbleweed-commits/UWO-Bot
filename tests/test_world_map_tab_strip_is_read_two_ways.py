"""Reading the world map's tab strip fails in two independent ways, both live.

1. OmniParser merges a tab label with whatever sits beside it — the Explore tab came back
   as 'NarExplore'. Matching labels by EQUALITY dropped it, the strip read
   ['port','route','trade'], and `selected_tab_index` mapped the highlight to the wrong slot.
2. The world map's selected tab is a near-WHITE panel. The warmth (R−B) test is tuned for
   the PORT strip's GOLD highlight, so it scored the lit tab LOWEST and said "cannot tell".

Together they made the new tab guard unable to confirm any switch: three taps at the right
coordinate, each reported "the tap was swallowed", and the run stalled on the world map.
"""
from pathlib import Path

from PIL import Image

from actions.sail_actions import _world_map_tab_strip, active_world_map_tab

FRAME = Path(__file__).parent / "stage_suite" / "frames" / "world_map_port_tab_lit_white.png"


def _frame():
    return Image.open(FRAME).convert("RGB")


def test_a_merged_label_still_yields_its_tab():
    """'NarExplore' contains 'explore'. An exact match on OCR text is a guess about the OCR,
    not about the game."""
    names = [n for n, *_ in _world_map_tab_strip(_frame())]
    assert names == ["port", "explore", "route", "trade"]


def test_the_lit_tab_is_found_though_it_is_white_not_gold():
    assert active_world_map_tab(_frame()) == "port"


def test_the_warmth_test_alone_would_still_fail_here():
    """Pins the defect, not just the fix: warmth ranks the lit tab LAST on this strip."""
    import numpy as np
    frame = _frame()
    warmth = []
    for _n, cx, cy, _y2 in _world_map_tab_strip(frame):
        cell = np.asarray(frame.crop((cx - 35, cy - 28, cx + 35, cy + 28))
                          .convert("RGB")).astype(float)
        warmth.append(cell[..., 0].mean() - cell[..., 2].mean())
    assert warmth[0] == min(warmth), "port is lit yet coldest — that is why warmth alone fails"


def test_brightness_is_relative_not_a_calibrated_threshold():
    """Compared within the strip on the same frame, so it rides dimming and re-skinning."""
    import inspect

    from actions import sail_actions
    src = inspect.getsource(sail_actions._brightest_tab)
    assert "median" in src and "_TAB_BRIGHT_RATIO" in src
