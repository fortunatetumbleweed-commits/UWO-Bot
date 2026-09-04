"""A stale barter panel takes the tap and does nothing.

Measured at Svear Village on 2026-08-26. The panel had been open since about 09:22, its
refresh timer was counting BACKWARDS (-1:45:13), and its own numbers had quietly degraded —
trade quantity 448(+98) -> 336(-14), negotiation 9%/9.2% -> 5%/5% — with no exchange having
happened. Exchange raised the confirmation dialog as normal and OK closed it, and nothing
occurred: amity stayed at Neutral 60,000 against the 61,840/Favorable the dialog had promised.

Closing the panel and reopening it, with nothing else changed and the same tap coordinate
(1313, 832), committed on the first attempt:

    amity      Neutral(60000)  ->  Favorable(61615)
    Wares      445 -> 340        (spent 105)
    Firearms   146 ->  93        (spent  53)
    Sundries   438 -> 333        (spent 105)

The existing guard had it half right:

    [barter_commit] NO PROGRESS: tapped [exchange, ok], no amity/cargo change
                    — escalate, don't re-tap

"Don't re-tap" is correct — re-tapping a stale panel never works. Escalating was wrong,
because the remedy is to get a LIVE panel, which is this call's own precondition.
"""

from __future__ import annotations

import unittest

from actions.barter_executor import barter_commit_verified


def _panel(amity, *, good="Birch Tree", out=689):
    """An OPEN barter panel. `good`/`out` are what make it open — a stale panel is still
    ON SCREEN, which is precisely what distinguishes it from the submenu the game CLOSES
    when the day's rounds run out. Pass good=None for that closed case."""
    class _P:
        amity_points = amity

    _P.good = good
    _P.out = out
    return _P()


class TheClosedSubmenuIsNotAStalePanel(unittest.TestCase):
    """When the day's rounds run out the game CLOSES the barter submenu itself. A commit
    that changed nothing there changed nothing because there was no panel to commit ON —
    reopening it is neither needed nor possible. Live 2026-08-27 at Hutu Village this was
    read as staleness and sent down the refresh path, which crashed the run."""

    def test_a_closed_panel_reports_exhausted_and_never_refreshes(self):
        calls = {"refresh": 0}

        def refresh(*_a):
            calls["refresh"] += 1
            return True

        res = barter_commit_verified(
            capture_fn=lambda: object(),
            read_panel_fn=lambda _f: _panel((100, 1000), good=None, out=None),
            read_cargo_fn=lambda _f: 500,
            commit_fn=lambda: [("exchange", 0.9, 0.9)],
            confirm_fn=lambda *_a, **_k: True,
            refresh_fn=refresh)
        self.assertTrue(res.get("exhausted"))
        self.assertEqual(calls["refresh"], 0, "there is no panel to reopen")
        self.assertIn("rounds are spent", res["reason"])


class RefreshingTheStalePanel(unittest.TestCase):

    def _run(self, amities, *, refresh_ok=True, with_refresh=True):
        """`amities` is the amity read at each _state() call, in order."""
        seq = list(amities)
        calls = {"commit": 0, "refresh": 0}

        def capture():
            return object()

        def read_panel(_f):
            return _panel(seq.pop(0) if len(seq) > 1 else seq[0])

        def commit():
            calls["commit"] += 1
            return [("exchange", 0.9, 0.9), ("ok", 0.5, 0.8)]

        def refresh():
            calls["refresh"] += 1
            return refresh_ok

        res = barter_commit_verified(
            settle_secs=0, capture_fn=capture, read_panel_fn=read_panel,
            read_cargo_fn=lambda _f: None, commit_fn=commit,
            confirm_fn=lambda _c: True,
            refresh_fn=refresh if with_refresh else None)
        return res, calls

    def test_a_stale_panel_is_refreshed_and_the_commit_retried(self):
        """before=60000, after=60000 (stale, nothing happened); then after the refresh the
        second attempt moves amity."""
        res, calls = self._run([(60000, 100000), (60000, 100000),
                                (60000, 100000), (61615, 100000)])
        self.assertTrue(res["ok"], res["reason"])
        self.assertEqual(calls["refresh"], 1)
        self.assertEqual(calls["commit"], 2)

    def test_a_commit_that_works_first_time_never_refreshes(self):
        res, calls = self._run([(60000, 100000), (61615, 100000)])
        self.assertTrue(res["ok"])
        self.assertEqual(calls["refresh"], 0)
        self.assertEqual(calls["commit"], 1)

    def test_it_refreshes_only_once(self):
        """A second failure means something other than staleness, and hammering a panel that
        will not commit is exactly what 'don't re-tap' forbids."""
        res, calls = self._run([(60000, 100000)] * 6)
        self.assertFalse(res["ok"])
        self.assertEqual(calls["refresh"], 1)
        self.assertEqual(calls["commit"], 2)

    def test_a_refresh_that_fails_does_not_retry_the_commit(self):
        res, calls = self._run([(60000, 100000)] * 4, refresh_ok=False)
        self.assertFalse(res["ok"])
        self.assertEqual(calls["commit"], 1)

    def test_without_a_refresh_hook_the_old_behaviour_stands(self):
        res, calls = self._run([(60000, 100000)] * 4, with_refresh=False)
        self.assertFalse(res["ok"])
        self.assertEqual(calls["commit"], 1)
        self.assertEqual(res["reason"], "barter commit made no change")


class ADeadButtonIsNotAStalePanel(unittest.TestCase):
    """`commit_via_positive_taps` refuses a button whose background is not the commit yellow,
    so an empty tap list on an open panel means the Exchange button is DEAD — the game saying
    the day's barter rounds are spent.

    Live at Svear on 2026-08-26, after three rounds the stock was down to 5 units at a cost of
    2+2, so the hold "funded" 74 more rounds while the button was grey and the daily slots were
    locked. The two ways a barter ends look identical from the materials alone
    (memory: barter-ends-two-ways), so the button has to be believed over the arithmetic.
    """

    def _run(self, tapped):
        calls = {"refresh": 0}

        def read_panel(_f):
            return _panel((64769, 100000))

        res = barter_commit_verified(
            settle_secs=0, capture_fn=lambda: object(), read_panel_fn=read_panel,
            read_cargo_fn=lambda _f: None, commit_fn=lambda: tapped,
            confirm_fn=lambda _c: True,
            refresh_fn=lambda: calls.__setitem__("refresh", calls["refresh"] + 1) or True)
        return res, calls

    def test_nothing_tapped_means_rounds_REMAIN_and_something_is_short(self):
        """The opposite of what this used to assert.

        The two endings are told apart by the SCREEN (user, 2026-08-27): rounds running out
        makes the game CLOSE the barter submenu, dropping the bot to the village top menu.
        A panel that is still open with Exchange grey therefore means a round REMAINS and
        the barter is blocked — a material at 0, or amity too low.

        Live 2026-08-27 at Svear this reported "the day's barter rounds are spent" after 4
        of 5 rounds, with Matchlock Gun at 0. The 5th round was there, 95 Matchlock away."""
        res, _calls = self._run([])
        self.assertFalse(res.get("exhausted"), "a grey button is not an exhausted day")
        self.assertTrue(res.get("blocked"))
        self.assertIn("Exchange grey", res["reason"])

    def test_it_does_not_refresh_a_panel_whose_button_is_simply_dead(self):
        """Refreshing is for a STALE panel. A greyed Exchange is not staleness, and reopening
        the panel to meet the same grey button is wasted work."""
        _res, calls = self._run([])
        self.assertEqual(calls["refresh"], 0)

    def test_a_commit_that_tapped_but_did_nothing_still_refreshes(self):
        """That IS the stale case — taps landed, nothing changed."""
        _res, calls = self._run([("exchange", 0.9, 0.9), ("ok", 0.5, 0.8)])
        self.assertEqual(calls["refresh"], 1)
