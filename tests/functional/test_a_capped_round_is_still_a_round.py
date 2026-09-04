"""Amity and cargo both saturate ON SUCCESS, so neither can witness a round alone.

LIVE 2026-08-30 at Hutu Village. Three rounds had run: amity reached its cap of
100,000/100,000 and the hold reached 4,952/4,952, with the overflow being discarded
("460 Bambara Groundnut has not been claimed"). A FOURTH round then ran:

    amity=Friendly(100000, 100000)  materials=[('uxuries', 923, 219), ('Livestock', 1135, 219)]
    amity=Friendly(100000, 100000)  materials=[('Luxuries', 704, 219), ('Livestock',  916, 219)]

Both material counts fell by exactly 219 — the round happened. But the progress test looked
only at amity (capped, cannot move) and cargo (full, cannot rise), reported "no change", and
the caller — seeing a panel hidden behind the discard dialog — ended the mission with
"the day's barter rounds are spent". 3 of 6 rounds used, material for four more aboard,
Exchange still gold and the submenu still open.

The materials were in the SAME snapshot the whole time. They are the witness that cannot
saturate: every round consumes them.
"""
import unittest

from actions.barter_executor import _barter_progressed


def _mat(label, have, need=219):
    return (label, have, need)


class _Obj:
    """The reader's own shape, to prove both forms are handled."""
    def __init__(self, label, have, need=219):
        self.label, self.have, self.need = label, have, need


class BothOldWitnessesSaturate(unittest.TestCase):
    def test_the_live_fourth_round_is_seen(self):
        before = {"amity": 100000, "cargo": 4952,
                  "materials": [_mat("uxuries", 923), _mat("Livestock", 1135)]}
        after = {"amity": 100000, "cargo": 4952,
                 "materials": [_mat("Luxuries", 704), _mat("Livestock", 916)]}
        ok, why = _barter_progressed(before, after)
        self.assertTrue(ok, "the materials fell by 219 each — that is a round")
        self.assertIn("materials", why)

    def test_the_readers_object_form_works_too(self):
        before = {"amity": 100000, "cargo": 4952,
                  "materials": [_Obj("uxuries", 923), _Obj("Livestock", 1135)]}
        after = {"amity": 100000, "cargo": 4952,
                 "materials": [_Obj("Luxuries", 704), _Obj("Livestock", 916)]}
        self.assertTrue(_barter_progressed(before, after)[0])

    def test_the_label_flicker_does_not_invent_a_round(self):
        # 'Luxuries' vs 'uxuries' is the same tile misread; nothing was consumed.
        same = lambda lab: {"amity": 100000, "cargo": 4952,
                            "materials": [_mat(lab, 923), _mat("Livestock", 1135)]}
        self.assertFalse(_barter_progressed(same("uxuries"), same("Luxuries"))[0],
                         "matching by NAME would have called this a round")

    def test_nothing_happening_is_still_nothing(self):
        snap = {"amity": 100000, "cargo": 4952,
                "materials": [_mat("Luxuries", 923), _mat("Livestock", 1135)]}
        ok, why = _barter_progressed(snap, dict(snap))
        self.assertFalse(ok)
        self.assertIn("material", why, "the reason names all three witnesses now")


class TheOldWitnessesStillCount(unittest.TestCase):
    def test_amity_moving_is_still_a_round(self):
        self.assertTrue(_barter_progressed({"amity": 79505}, {"amity": 87660})[0])

    def test_cargo_rising_is_still_a_round(self):
        self.assertTrue(_barter_progressed({"cargo": 100}, {"cargo": 998})[0])


if __name__ == "__main__":
    unittest.main()


class TheStripCountsRoundsOutright(unittest.TestCase):
    """The exchange status bar states the answer; everything else implies it.

    Whole-screen or region diffing cannot substitute: the barter panel is translucent, with
    open sea and moving ships behind it, so the pixels differ constantly whether or not a
    round happened. The strip and the materials are the two structured signals.
    """

    def test_a_new_tile_is_a_round_even_with_both_old_witnesses_capped(self):
        before = {"amity": 100000, "cargo": 4952, "rounds": [859, 859, 898]}
        after = {"amity": 100000, "cargo": 4952, "rounds": [859, 859, 898, 898]}
        ok, why = _barter_progressed(before, after)
        self.assertTrue(ok)
        self.assertIn("trade count", why)

    def test_no_new_tile_and_nothing_else_is_no_round(self):
        snap = {"amity": 100000, "cargo": 4952, "rounds": [859, 859, 898, 898]}
        self.assertFalse(_barter_progressed(snap, dict(snap))[0])

    def test_a_missing_strip_falls_back_to_the_other_witnesses(self):
        before = {"amity": 100000, "cargo": 4952, "rounds": None,
                  "materials": [_mat("Luxuries", 923)]}
        after = {"amity": 100000, "cargo": 4952, "rounds": None,
                 "materials": [_mat("Luxuries", 704)]}
        self.assertTrue(_barter_progressed(before, after)[0])


class AgainstTheRealStrip(unittest.TestCase):
    THREE = "tests/stage_suite/frames/hutu_trade_count_three_slots.png"
    FOUR = "tests/stage_suite/frames/hutu_trade_count_four_slots.png"

    def _slots(self, path):
        import os
        if not os.path.exists(path):
            self.skipTest("stage frame not available")
        from PIL import Image
        from actions.barter_reader import read_trade_count
        return read_trade_count(Image.open(path))

    def test_it_reads_the_yields_of_each_spent_round(self):
        self.assertEqual(self._slots(self.THREE), [859, 859, 898])

    def test_the_fourth_round_the_bot_never_counted(self):
        # The bot reported rounds_committed: 3 and left. The strip says four of six.
        self.assertEqual(self._slots(self.FOUR), [859, 859, 898, 898])
