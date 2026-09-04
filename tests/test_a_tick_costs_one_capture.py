"""One tick, one capture — perceive and the readers share it.

A tick used to pay twice for one screen: `perceive` captured for itself, and any reader that
went through the repository captured again. Two captures of one moment is not only the waste
this repository exists to remove — it is the DISAGREEMENT it exists to make impossible (live
2026-08-30 at Faro: the title read 'Sell' off one capture while the goods grid was still
'Purchase' on another, both correct).

Perceive is not privileged. It is the FIRST caller of the tick, and what it looks at is what
everyone else that tick reads.
"""
import unittest
from unittest.mock import patch

from actions.perception import reset_for_tests, screen


class _Frame:
    def __init__(self, n): self.n = n
    def crop(self, box): return f"crop{self.n}"


class OneCapturePerTick(unittest.TestCase):
    def setUp(self):
        reset_for_tests()
        self.addCleanup(reset_for_tests)
        self.n = 0

        def capture():
            self.n += 1
            return _Frame(self.n)

        screen()._capture_fn = capture
        screen()._parse_fn = lambda f: []
        screen()._ocr_fn = lambda i: ""
        screen()._sleep = lambda s: None

    def test_perceive_and_a_reader_share_one_capture(self):
        first = screen().get(why="perceive")          # what perceive does now
        second = screen().get(why="a market reader")  # what a migrated reader does
        self.assertIs(first.frame, second.frame)
        self.assertEqual(self.n, 1, "one tick, one capture, one screen")

    def test_a_fresh_look_is_asked_for_once_per_tick(self):
        screen().get(why="perceive")                             # tick 1
        screen().expect_changed("the dispatcher is taking a fresh look")
        screen().get(why="perceive")                             # tick 2
        screen().get(why="a reader")
        screen().get(why="another reader")
        self.assertEqual(self.n, 2, "two ticks, two captures — not two per tick")

    def test_perceive_takes_its_frame_from_the_repository(self):
        """Pin the wiring: perceive must not capture beside the repository."""
        import inspect
        from brain import perceive as P
        src = inspect.getsource(P.perceive)
        self.assertIn("actions.perception", src)
        self.assertIn("screen()", src)


class AnInjectedFrameStillWins(unittest.TestCase):
    """Callers that already hold a frame keep passing it — the repository is for callers
    that would otherwise capture, not a mandatory hop."""

    def setUp(self):
        reset_for_tests()
        self.addCleanup(reset_for_tests)
        self.n = 0
        screen()._capture_fn = lambda: (setattr(self, "n", self.n + 1) or _Frame(self.n))
        screen()._parse_fn = lambda f: []
        screen()._ocr_fn = lambda i: ""

    def test_passing_a_frame_costs_no_capture(self):
        from brain import perceive as P
        with patch.object(P, "_perceive_uncached", return_value="RESULT"):
            P.perceive(frame=_Frame(99))
        self.assertEqual(self.n, 0, "a caller with a frame in hand does not need us")


if __name__ == "__main__":
    unittest.main()
