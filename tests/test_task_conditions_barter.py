"""Tests for the barter-domain done-conditions + combinators (#17)."""
import types
import unittest

from brain.task_conditions import (
    all_of, any_of, negate,
    amity_increased, red_note_cleared, dialog_absent,
)


def _obs(frame="F"):
    return types.SimpleNamespace(frame=frame, perceived=None, hud={})


_TRUE = lambda wm, obs: True
_FALSE = lambda wm, obs: False


class CombinatorTests(unittest.TestCase):
    def test_all_of(self):
        self.assertTrue(all_of(_TRUE, _TRUE)(None, _obs()))
        self.assertFalse(all_of(_TRUE, _FALSE)(None, _obs()))

    def test_any_of(self):
        self.assertTrue(any_of(_FALSE, _TRUE)(None, _obs()))
        self.assertFalse(any_of(_FALSE, _FALSE)(None, _obs()))

    def test_negate(self):
        self.assertTrue(negate(_FALSE)(None, _obs()))
        self.assertFalse(negate(_TRUE)(None, _obs()))


class AmityIncreasedTests(unittest.TestCase):
    def _reader(self, pts):
        return lambda frame: types.SimpleNamespace(amity_points=pts)

    def test_increase_detected(self):
        done = amity_increased(709, read_panel=self._reader(744))
        self.assertTrue(done(None, _obs()))

    def test_no_increase(self):
        self.assertFalse(amity_increased(744, read_panel=self._reader(744))(None, _obs()))
        self.assertFalse(amity_increased(744, read_panel=self._reader(700))(None, _obs()))

    def test_none_baseline_never_done(self):
        self.assertFalse(amity_increased(None, read_panel=self._reader(744))(None, _obs()))

    def test_unreadable_panel(self):
        self.assertFalse(amity_increased(709, read_panel=lambda f: None)(None, _obs()))


class RedNoteClearedTests(unittest.TestCase):
    def test_done_when_note_gone(self):
        done = red_note_cleared(box=(0, 0, 10, 10), has_badge=lambda f, b: False)
        self.assertTrue(done(None, _obs()))

    def test_not_done_while_note_present(self):
        done = red_note_cleared(box=(0, 0, 10, 10), has_badge=lambda f, b: True)
        self.assertFalse(done(None, _obs()))


class DialogAbsentTests(unittest.TestCase):
    def test_overflow_dialog_present(self):
        done = dialog_absent("insufficient empty space",
                             ocr_text_fn=lambda f: "Insufficient Empty Space  Receive")
        self.assertFalse(done(None, _obs()))

    def test_overflow_dialog_cleared(self):
        done = dialog_absent("insufficient empty space",
                             ocr_text_fn=lambda f: "Barter  Amity(Friendly)")
        self.assertTrue(done(None, _obs()))


if __name__ == "__main__":
    unittest.main()
