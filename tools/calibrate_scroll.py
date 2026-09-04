"""What does a swipe of N px over D ms actually move the list by?

`adb shell input swipe` has a FIXED 300ms duration, which makes a short swipe a fast one:
150px in 300ms is 500px/s, quick enough that the scroll view flings and coasts past where the
finger stopped. Measured on Cheyenne's trade list, a 150px request delivered 384px — nearly
three rows instead of one, which is how rows go unread while the arithmetic looks correct.

This sweeps distance x duration and reports delivered px per request, so the step can be
chosen from measurement instead of from hope. Read-only: it scrolls a list and looks at it.

    python -m tools.calibrate_scroll "<Village Name>"
"""

from __future__ import annotations

import sys
import time

from loguru import logger

from actions import ui
from actions.adb_actions import swipe
from capture.adb_capture import capture_screen
import tools.learn_village_barter as L
from tools.village_scroll_report import _measure_shift, _scrollbar
from vision.list_position import bar_position

# distance requested, gesture duration
TRIALS = [(150, 300), (150, 800), (300, 300), (300, 800), (450, 300), (450, 1200)]


def _vp():
    from vision.omniparser import parse_fast_cached
    return L._list_viewport(list(parse_fast_cached(capture_screen())))


def _room_below(frame, vp):
    bar = _scrollbar(frame, vp)
    return None if not bar else (vp[1] + 12) - bar[1]


def main(village: str) -> int:
    from actions.sail_actions import open_world_map, _try_village_search, _is_on_world_map
    from actions.village_check import _PANEL_X_MIN
    from tools.village_scroll_report import _reset_list_position

    if not open_world_map():
        return 1
    _reset_list_position(capture_screen, ui, open_world_map, _is_on_world_map)
    if not _try_village_search(village):
        return 1
    ui.settle("screen", why="village selected")
    for _ in range(4):
        if ui.tap_text(capture_screen(), "barter", x_min=_PANEL_X_MIN, dwell="dialog",
                       why="Barter tab"):
            break
        time.sleep(1.5)
    else:
        return 1

    vp = _vp()
    print(f"\nviewport {vp}\n{'asked':>7}{'ms':>6}{'delivered':>11}{'ratio':>8}  note")
    print("-" * 46)
    for dist, ms in TRIALS:
        # REWIND FULLY BEFORE EVERY TRIAL. Cheyenne's list holds only ~1100px of scrollable
        # content, so after one or two trials the swipes land against the bottom stop and
        # under-deliver — the first sweep read 49%, 135%, 8% and was measuring how much room
        # was left, not the gesture. A trial means nothing without the whole list ahead of it.
        rewound = False
        for _ in range(12):
            f = capture_screen()
            at_top, _at_end = bar_position(_scrollbar(f, vp), vp)
            if at_top:
                rewound = True
                break
            # INSIDE THE LIST. y=880+400 is 1280 on a 1080px screen — off the display, and
            # it drags across the "View by Min. Exchange Unit" checkbox on the way.
            swipe(L.SCROLL_X, vp[0] + 25, L.SCROLL_X, vp[1] - 25, duration_ms=700)
            time.sleep(0.7)
        if not rewound:
            print(f"{dist:>7}{ms:>6}{'-':>11}{'-':>8}  could not rewind to the top")
            continue
        before = capture_screen()
        start = min(vp[1] - 25, vp[0] + 25 + dist)     # both ends inside the list
        swipe(L.SCROLL_X, start, L.SCROLL_X, start - dist, duration_ms=ms)
        time.sleep(1.4)                      # let any fling settle
        after = capture_screen()
        # The default 400px search ceiling cannot see a 450px request, let alone an
        # overshoot of one — a capped measurement reads as a short delivery.
        got, err, zero = _measure_shift(before, after, max_px=760)
        ok = got != 0 and err < 0.6 * zero
        ratio = f"{100.0 * abs(got) / dist:.0f}%" if ok else "-"
        note = "" if ok else "no movement / unmeasurable"
        print(f"{dist:>7}{ms:>6}{(abs(got) if ok else 0):>11}{ratio:>8}  {note}", flush=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    raise SystemExit(main(sys.argv[1]))
