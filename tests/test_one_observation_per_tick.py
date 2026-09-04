"""Staleness is GENERATION, not age: a newer frame overrides what the old one said.

Ticking keeps capturing because the game moves without us — an arrival, a notice, an idle
lock. A sub-loop captures because it ACTS: while it runs, the dispatcher is blocked and the
sub-loop's own frames are the only observations there are. Both refreshes are legitimate.

What is NOT legitimate is two readers in one moment looking at different frames. LIVE
2026-08-30 at Faro: the dispatcher classified one capture while `sell_goods` took another, so
the chromed title read 'Sell' while the goods grid was still 'Purchase' — each true of its own
frame — and the bot loaded goods it had never bought into a sell basket. Nothing downstream
could catch it, because both readings were correct.

AGE IS THE WRONG TEST, in both directions. At sea an observation twenty minutes old is still
the current one, because the pacing sleep means nothing newer has been taken. In a market a
two-second-old one is superseded the instant a tap produces a new frame. What matters is
whether a NEWER frame exists.
"""
import unittest


class GenerationDecides(unittest.TestCase):
    def setUp(self):
        from brain import perceive as P
        self.P = P
        self._saved = (P._PERCEIVE_LAST_FRAME, P._PERCEIVE_LAST_RESULT,
                       P._PERCEIVE_LAST_AT, P._PERCEIVE_GENERATION)

    def tearDown(self):
        P = self.P
        (P._PERCEIVE_LAST_FRAME, P._PERCEIVE_LAST_RESULT,
         P._PERCEIVE_LAST_AT, P._PERCEIVE_GENERATION) = self._saved

    def _observe(self, frame, result="R", age_s=0.0):
        import time
        P = self.P
        P._PERCEIVE_LAST_FRAME, P._PERCEIVE_LAST_RESULT = frame, result
        P._PERCEIVE_LAST_AT = time.monotonic() - age_s
        P._PERCEIVE_GENERATION += 1
        return P._PERCEIVE_GENERATION

    def test_two_readers_in_one_moment_get_the_same_frame(self):
        self._observe("FRAME_A")
        a = self.P.current_observation()
        b = self.P.current_observation()
        self.assertIs(a[0], b[0], "the SAME frame object, not two captures of one screen")
        self.assertEqual(a[2], b[2], "and the same generation")

    def test_a_newer_frame_overrides_the_older_one(self):
        gen_a = self._observe("FRAME_A")
        self.assertTrue(self.P.is_current(gen_a))
        gen_b = self._observe("FRAME_B")
        self.assertFalse(self.P.is_current(gen_a),
                         "data read off FRAME_A is superseded the moment FRAME_B exists")
        self.assertTrue(self.P.is_current(gen_b))
        self.assertIs(self.P.current_observation()[0], "FRAME_B")

    def test_an_old_observation_is_current_while_nothing_newer_exists(self):
        # The sea pacing sleeps for minutes between ticks. Nothing newer has been taken, so
        # what we hold IS the current word — age says nothing about it.
        gen = self._observe("FRAME_AT_SEA", age_s=1200.0)
        self.assertTrue(self.P.is_current(gen),
                        "twenty minutes old and still the newest frame there is")
        self.assertGreater(self.P.observation_age_s(), 1000, "age is still answerable")

    def test_nothing_observed_yet_is_not_current(self):
        self.P._PERCEIVE_LAST_FRAME = None
        self.P._PERCEIVE_LAST_RESULT = None
        self.assertEqual(self.P.current_observation()[:2], (None, None))
        self.assertFalse(self.P.is_current(self.P.observation_generation()))

    def test_generations_never_go_backwards(self):
        seen = [self._observe(f"F{i}") for i in range(4)]
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(set(seen)), 4, "each observation is its own generation")


if __name__ == "__main__":
    unittest.main()
