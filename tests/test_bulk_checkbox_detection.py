"""The 'Put in Bulk' checkbox must be FOUND, not assumed to sit at an offset.

Measured on the live sell page 2026-08-22: the "Put in Bulk" label anchors at x=603 and the
green checkmark occupies x 493-517 — about 86-110px to its left. The detector sampled
x 533..598 ("~30-60px to the left"), which has ZERO overlap with the mark, so it always
reported False.

That is not a harmless miss. `_ensure_bulk_mode(False, …)` cannot tell "OFF" from
"detection failed", so it treats not-detected as already-OFF and returns True WITHOUT
tapping. Bulk stayed ON; `sell_down_to` then tapped a good expecting a quantity dialog and
bulk-loaded the entire 1,681-unit Ebony stack into the sell cart instead. The trim never
ran and the hold stayed at 3823/4108.

The old tap offset was wrong in the same way: put_x - 40 = 563 lands between the box and
the label, so even a correct decision would have missed.
"""
import numpy as np
from PIL import Image

from actions import market_actions as ma


LABEL_X, LABEL_Y = 603, 1011          # measured anchor of the "Put in Bulk" label
BOX_X, BOX_Y = 505, 1007              # measured centre of the checkmark


def _frame(checked: bool):
    """A sell-page-shaped frame with the checkbox where the game actually draws it."""
    a = np.zeros((1080, 2400, 3), dtype=np.uint8)
    a[:, :] = (40, 40, 40)
    if checked:
        a[BOX_Y - 12:BOX_Y + 12, BOX_X - 12:BOX_X + 12] = (60, 200, 60)   # green check
    return Image.fromarray(a)


class _El:
    """OmniParser reports 'Put In Bulk' as a BUTTON whose bbox INCLUDES the checkbox —
    measured live at (481,986)-(684,1030) with the tick at (504,1007)."""
    label, element_type = "Put In Bulk", "button"
    x1, y1, x2, y2 = 481, 986, 684, 1030


def _patch_anchor(monkeypatch):
    monkeypatch.setattr("vision.omniparser.parse_fast_cached", lambda _f: [_El()])


class TestDetection:
    def test_a_checked_box_is_detected(self, monkeypatch):
        _patch_anchor(monkeypatch)
        assert ma._is_bulk_mode_on(_frame(True)) is True

    def test_an_unchecked_box_reads_false(self, monkeypatch):
        _patch_anchor(monkeypatch)
        assert ma._is_bulk_mode_on(_frame(False)) is False

    def test_the_old_narrow_window_would_have_missed_it(self, monkeypatch):
        """Documents the regression: the old offset window x 533..598 vs a mark at 493..517."""
        _patch_anchor(monkeypatch)
        a = np.array(_frame(True))
        old_window = a[LABEL_Y - 25:LABEL_Y + 25, LABEL_X - 70:LABEL_X - 5]
        r, g, b = (old_window[:, :, i].astype(int) for i in range(3))
        assert int(((g - r > 50) & (g - b > 50) & (g > 100)).sum()) == 0

    def test_no_control_found_means_no_detection(self, monkeypatch):
        monkeypatch.setattr("vision.omniparser.parse_fast_cached", lambda _f: [])
        assert ma._is_bulk_mode_on(_frame(True)) is False

    def test_the_tick_is_read_from_inside_the_buttons_bbox(self, monkeypatch):
        """No offset from the label: the checkbox lives INSIDE the detected bbox."""
        _patch_anchor(monkeypatch)
        el = _El()
        assert el.x1 <= BOX_X <= el.x2 and el.y1 <= BOX_Y <= el.y2


class TestTapTarget:
    def test_the_tap_follows_the_detected_mark(self, monkeypatch):
        _patch_anchor(monkeypatch)
        ma._BULK_CHECKBOX_POS[0] = None
        assert ma._is_bulk_mode_on(_frame(True))
        x, y = ma._BULK_CHECKBOX_POS[0]
        assert abs(x - BOX_X) <= 15 and abs(y - BOX_Y) <= 15, \
            f"tap target {(x, y)} must land on the box, not an assumed offset"

    def test_the_old_offset_would_have_missed_the_box(self):
        """put_x - 40 = 563, while the box centre is ~505."""
        assert abs((LABEL_X - 40) - BOX_X) > 40


class TestRestoreTurnsBulkBackOn:
    """`_restore_bulk` must actually restore it.

    The old policy never tapped when targeting ON — "we can't safely distinguish OFF from
    detection failed, so assume it is already ON". That made restore a no-op. A sell-trim
    turns bulk OFF to get the quantity dialog, so the market was handed back with bulk OFF
    and the next buy silently loaded nothing: live 2026-08-22 at Kolkata, "[Kolkata] tap
    Purchase @ (1313,943) cost=0" repeating, because with bulk off a tile tap opens the
    quantity dialog instead of loading the stack.

    That premise no longer holds: the checkbox is read from inside the detected "Put In
    Bulk" button's bbox, so finding the control but no green tick means genuinely OFF.
    """

    def _run(self, checked_before, checked_after):
        from unittest import mock
        states = [checked_before, checked_after]
        taps = []
        with mock.patch.object(ma, "_find_put_in_bulk_anchor", lambda _f: (LABEL_X, LABEL_Y)), \
             mock.patch.object(ma, "_is_bulk_mode_on", side_effect=lambda _f: states.pop(0)
                               if states else checked_after), \
             mock.patch.object(ma, "tap", side_effect=lambda x, y: taps.append((x, y))), \
             mock.patch.object(ma, "capture_screen", return_value=_frame(True)), \
             mock.patch("time.sleep"):
            ma._BULK_CHECKBOX_POS[0] = (BOX_X, BOX_Y)
            ok = ma._ensure_bulk_mode(True, _frame(checked_before))
        return ok, taps

    def test_an_off_checkbox_is_tapped_back_on(self):
        _ok, taps = self._run(checked_before=False, checked_after=True)
        assert taps, "restore did not tap — bulk stays OFF and the next buy loads nothing"
        assert abs(taps[0][0] - BOX_X) <= 15

    def test_an_already_on_checkbox_is_left_alone(self):
        """Tapping an ON checkbox would turn it OFF — the failure this policy guarded."""
        _ok, taps = self._run(checked_before=True, checked_after=True)
        assert taps == []
