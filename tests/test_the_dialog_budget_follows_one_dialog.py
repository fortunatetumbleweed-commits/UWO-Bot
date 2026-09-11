"""The budget stops us grinding at ONE stuck dialog. It counted every dialog.

`_dialog_looks` incremented on any tick with any dialog up and reset only on a tick with
none — so a market visit's ordinary chain spent it on progress rather than on a screen that
would not close.

It has been failing safe by a single look for a long time. Across the session logs the
generic answer ("nothing owns the system dialog — the game rules say 'Ok'") fired 69 times:

    1 of 3 :  4
    2 of 3 : 34
    3 of 3 : 31      <- the last look it had

Live 2026-09-09 at Bordeaux the hold was full of Birch Tree, which put a `cargo_full_notice`
at the front of the chain — notice, negotiation, result, three DIFFERENT dialogs, each
answered successfully — and the purchase result card arrived at the guard at 4/3. It was
reported as "a system dialog will not close" without being answered once. Nothing about that
card had changed; it simply never got its turn.
"""

from __future__ import annotations

import types
import unittest


def _dialog(title, *actions):
    return types.SimpleNamespace(
        title=types.SimpleNamespace(text=title),
        actions=[types.SimpleNamespace(label=a) for a in actions])


class OneDialogAtATime(unittest.TestCase):

    def _fresh(self):
        from brain.dispatcher import Dispatcher
        d = Dispatcher.__new__(Dispatcher)
        d._dialog_looks, d._dialog_seen = 0, None
        return d

    def _look(self, d, dialog):
        """What `step` does to the counter for one tick."""
        if dialog is None:
            d._dialog_looks, d._dialog_seen = 0, None
            return d._dialog_looks
        here = d._dialog_signature(dialog)
        d._dialog_looks = (d._dialog_looks + 1) if here == d._dialog_seen else 1
        d._dialog_seen = here
        return d._dialog_looks

    def test_the_bordeaux_chain_never_exhausts_the_budget(self):
        """Three different dialogs, each answered. That is progress, not a stuck screen."""
        d = self._fresh()
        seen = [self._look(d, _dialog("Notice", "Ok")),                    # cargo full
                self._look(d, _dialog("Negotiation", "Cancel", "Ok")),     # negotiation
                self._look(d, _dialog("Result", "Ok"))]                    # purchase result
        self.assertEqual([1, 1, 1], seen)
        self.assertLessEqual(max(seen), d._MAX_DIALOG_LOOKS,
                             "each new dialog starts its own count")

    def test_one_dialog_that_will_not_close_still_runs_out(self):
        """The guard's real job is untouched."""
        d = self._fresh()
        stuck = _dialog("Result", "Ok")
        counts = [self._look(d, stuck) for _ in range(5)]
        self.assertEqual([1, 2, 3, 4, 5], counts)
        self.assertGreater(counts[-1], d._MAX_DIALOG_LOOKS)

    def test_a_dialog_free_tick_clears_it(self):
        d = self._fresh()
        self._look(d, _dialog("Result", "Ok"))
        self.assertEqual(0, self._look(d, None))
        self.assertIsNone(d._dialog_seen)

    def test_returning_to_the_same_dialog_after_another_starts_over(self):
        d = self._fresh()
        self._look(d, _dialog("Result", "Ok"))
        self._look(d, _dialog("Result", "Ok"))
        self._look(d, _dialog("Negotiation", "Cancel", "Ok"))
        self.assertEqual(1, self._look(d, _dialog("Result", "Ok")))


class ALandedTransactionClearsEveryCounter(unittest.TestCase):
    """A successful buy or sell is progress, and progress is not a stall.

    Every counter in the dispatcher answers one question — has this stopped moving? — and a
    transaction landing is the plainest No there is. At Bordeaux three dialogs were answered
    and two purchases went through, and the budget still ran out, because it had been
    counting since before any of it (user, 2026-09-09).
    """

    def _dispatcher(self):
        from brain.dispatcher import Dispatcher
        d = Dispatcher.__new__(Dispatcher)
        d._last_transaction = (0, 0)
        d._dialog_looks, d._dialog_seen = 3, ("Result", ("ok",))
        d._in_flight_looks = d._standing_looks = d._undrawn_looks = 2
        return d

    def _result(self, **observed):
        from brain.dispatcher import ActivityResult, WORKING
        return ActivityResult(WORKING, observed)

    def test_a_purchase_clears_them_all(self):
        d = self._dispatcher()
        d._note_any_progress(self._result(bought_total=529, port="Bordeaux"))
        self.assertEqual(0, d._dialog_looks)
        self.assertIsNone(d._dialog_seen)
        self.assertEqual((0, 0, 0), (d._in_flight_looks, d._standing_looks, d._undrawn_looks))

    def test_a_sale_clears_them_all(self):
        d = self._dispatcher()
        d._note_any_progress(self._result(sold=["Birch Tree"], port="Bordeaux"))
        self.assertEqual(0, d._dialog_looks)
        self.assertEqual(0, d._in_flight_looks)

    def test_a_tick_that_bought_nothing_leaves_them_standing(self):
        """Only progress clears them; otherwise the guard would never fire at all."""
        d = self._dispatcher()
        d._note_any_progress(self._result(port="Bordeaux", did="scrolled"))
        self.assertEqual(3, d._dialog_looks)
        self.assertEqual(2, d._in_flight_looks)

    def test_the_same_total_reported_twice_is_not_new_progress(self):
        d = self._dispatcher()
        d._note_any_progress(self._result(bought_total=529))
        d._dialog_looks = 3
        d._note_any_progress(self._result(bought_total=529))
        self.assertEqual(3, d._dialog_looks, "the same purchase does not clear it twice")


class TheSignatureSeparatesWhatItMust(unittest.TestCase):

    def _sig(self, dialog):
        from brain.dispatcher import Dispatcher
        return Dispatcher._dialog_signature(dialog)

    def test_the_same_card_re_read_is_the_same_dialog(self):
        """It must survive a re-read, or the budget never accumulates on a stuck card."""
        self.assertEqual(self._sig(_dialog("Result", "Ok")),
                         self._sig(_dialog("Result", "Ok")))

    def test_different_cards_are_different(self):
        for other in (_dialog("Negotiation", "Cancel", "Ok"),
                      _dialog("Notice", "Ok"),
                      _dialog("Result", "Cancel", "Ok")):
            with self.subTest(other=other.title.text):
                self.assertNotEqual(self._sig(_dialog("Result", "Ok")), self._sig(other))

    def test_a_dialog_with_no_title_or_actions_still_yields_something(self):
        self.assertIsInstance(self._sig(types.SimpleNamespace()), tuple)


if __name__ == "__main__":
    unittest.main()
