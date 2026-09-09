"""Maintenance, a patch, a lost connection — the session is over, not obstructed.

Met twice on 2026-09-09. Each time the bot spent three ticks, a Qwen call and a Claude Vision
call re-identifying the screen, then stopped with:

    NOTHING CHANGED for 3 ticks (state='unknown') goal=depart for Tripoli

True, useless, and the reason was printed on the screen throughout — "The Uncharted Waters
Origin is currently undergoing maintenance. Maintenance End Time: 13:00(UTC+9)" the first
time, a patch notice the second. Claude read the second one correctly and the answer was
thrown away.

None of these can be dismissed: Back does not take them, a tap achieves nothing, and after a
restart the game needs a login the bot has no support for (user, 2026-09-09). The only right
move is to say so.

WORDING IS THE SIGNAL HERE, and it is the exception that proves the rule. A screen's identity
is read from structure because its words are decoration; here the MESSAGE is the whole fact,
and there is nothing structural to read — an Android dialog is a white rectangle with an OK.
"""

from __future__ import annotations

import unittest
import unittest.mock as mock


class TheNoticesAreRecognised(unittest.TestCase):

    REAL = {
        "maintenance (2026-09-08 19:0x)":
            "Notice | The Uncharted Waters Origin is currently undergoing maintenance. | "
            "For more details, visit the FLOOR website | *Maintenance End Time: 13:00(UTC+9)",
        "patch notice (2026-09-09 15:2x)":
            "Notice | A new patch is available. Returning to the patch screen. | OK",
        "the patch/login screen":
            "This game requires an active Internet connection to run. | You can log in "
            "through your Apple, FLOOR, Google, or LINE account.",
    }

    def test_each_one_seen_live_is_known(self):
        from brain.unexpected_dialog import looks_like_game_unavailable
        for name, text in self.REAL.items():
            with self.subTest(screen=name):
                self.assertTrue(looks_like_game_unavailable(text))

    def test_an_ordinary_screen_is_not(self):
        from brain.unexpected_dialog import looks_like_game_unavailable
        for text in ("Purchase | Cargo 1,875/4,952 | Iron | Matchlock Gun",
                     "Replenish Stock? | Remaining: 33/50 | Cancel | Ok",
                     "Moon Rabbit's Part Gift Package | Time left | Purchase"):
            with self.subTest(text=text[:40]):
                self.assertFalse(looks_like_game_unavailable(text))


class NothingIsTappedAtIt(unittest.TestCase):

    def test_clear_blockers_reports_it_and_touches_nothing(self):
        import brain.unexpected_dialog as ud
        taps, backs, swipes = [], [], []
        ud._ocr_text = lambda f: "The Uncharted Waters Origin is currently undergoing maintenance."
        r = ud.clear_blockers(frame=object(), wake_fn=lambda: None,
                              swipe_fn=lambda *a: swipes.append(a),
                              tap_fn=lambda x, y: taps.append((x, y)),
                              back_fn=lambda: backs.append(True))
        self.assertEqual("game_unavailable", r["kind"])
        self.assertFalse(r["cleared"], "it is not cleared, and saying so is the point")
        self.assertEqual(([], [], []), (taps, backs, swipes),
                         "nothing here can be tapped, backed or swiped away")


class TheModelsAreNotPaidToReadIt(unittest.TestCase):

    def test_it_short_circuits_before_claude_vision(self):
        import brain.perceive as bp
        called = []
        with mock.patch.object(bp, "_qwen_describe_unknown", lambda *a, **k: None), \
             mock.patch.object(bp, "_claude_resolve_unknown",
                               lambda *a, **k: called.append(True)), \
             mock.patch.object(bp, "_unknown_debounce_should_fire", lambda *a, **k: (True, 3)):
            out = bp.handle_unknown_blocking(
                mock.Mock(width=2400, height=1080),
                [("The Uncharted Waters Origin is currently undergoing maintenance.",
                  0.9, 1200, 300)], "")
        self.assertFalse(out)
        self.assertEqual([], called, "a vision model cannot restart the game")


if __name__ == "__main__":
    unittest.main()
