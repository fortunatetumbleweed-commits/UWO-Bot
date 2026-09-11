"""3,129 comes back as 3.129, and every reader spelled the separator `[\\d,]`.

Live 2026-09-05, frames 236/237 of the Svear run. The cargo bar read

    '3.129/4,952'

— one comma rendered as a period, the other not, because OCR flakes per GLYPH. A pattern
that admits only commas cannot match at the '3' (the next character is not a slash), so it
settled on '129/4,952'. The hold reported 129 while the cargo tiles summed to 3,129; the
cross-check correctly refused to buy against an impossible number, and the leg stopped 249
Candle short — two barter rounds, ~1,200 units of product.
"""

from __future__ import annotations

import unittest

from utils.digits import parse_pair, to_int


class TheCargoBarSurvivesAMisreadSeparator(unittest.TestCase):

    def test_the_frame_236_reading(self):
        self.assertEqual(parse_pair("3.129/4,952"), (3129, 4952))

    def test_the_same_bar_read_correctly(self):
        self.assertEqual(parse_pair("3,129/4,952"), (3129, 4952))

    def test_both_halves_may_flake_independently(self):
        for text in ("3.129/4.952", "3,129/4.952", "3129/4952"):
            self.assertEqual(parse_pair(text), (3129, 4952), text)

    def test_a_genuinely_small_hold_is_not_inflated(self):
        """129 really is 129 when that is what the screen says."""
        self.assertEqual(parse_pair("129/4,952"), (129, 4952))


class ToIntTakesTheWholeNumber(unittest.TestCase):

    def test_separators_of_every_kind(self):
        for text in ("3,129", "3.129", "3129", "3'129"):
            self.assertEqual(to_int(text), 3129, text)

    def test_it_does_not_stop_at_the_first_group(self):
        """The bug in one line: taking '3' or '129' instead of 3129."""
        self.assertEqual(to_int("1.234.567"), 1234567)

    def test_no_number_is_None_not_zero(self):
        """An unread counter is NO ANSWER, never zero — zero is a claim about the hold."""
        self.assertIsNone(to_int("Cargo"))
        self.assertIsNone(to_int(""))
        self.assertIsNone(parse_pair("Cargo"))

    def test_it_reads_the_count_beside_other_text(self):
        self.assertEqual(to_int("4108(+5)"), 4108)


class TheReaderUsesIt(unittest.TestCase):

    def test_the_cargo_bar_reader_parses_the_live_token(self):
        """End to end through _read_cargo_used_cap, with the frame 236 token."""
        import actions.buy_materials as B
        from unittest import mock

        tokens = [("3.129/4,952", 0.9, 2000, 300)]
        with mock.patch.dict("sys.modules", {
            "actions.sail_actions": type("m", (), {
                "_ocr_frame": staticmethod(lambda f, min_conf=0.3: tokens)})(),
        }):
            self.assertEqual(B._read_cargo_used_cap(object()), (3129, 4952))

    def test_the_left_hand_trade_points_counter_is_ignored(self):
        """Same shape, wrong panel — the cart is on the RIGHT."""
        import actions.buy_materials as B
        from unittest import mock

        tokens = [("3.129/4,952", 0.9, 400, 300)]      # x < 1850
        with mock.patch.dict("sys.modules", {
            "actions.sail_actions": type("m", (), {
                "_ocr_frame": staticmethod(lambda f, min_conf=0.3: tokens)})(),
        }):
            self.assertIsNone(B._read_cargo_used_cap(object()))


if __name__ == "__main__":
    unittest.main()


class EveryCountReaderTakesTheWholeNumber(unittest.TestCase):
    """The rule (user, 2026-09-05): "for any goods counts we can safely assume the '.' is
    ',' as the count is always a positive integer."

    `vision.hud_readers._to_int_sep` had said exactly this since it was written — and
    `_to_int` beside it, in the same module, stayed comma-only. The insight was found once
    and applied once. These pin it in every reader that turns a screen token into a count.
    """

    def test_hud_readers(self):
        from vision.hud_readers import _INT_RE, _PAIR_RE, _to_int
        self.assertEqual(_to_int("3.129"), 3129)
        self.assertEqual(_to_int("3,129"), 3129)
        self.assertTrue(_INT_RE.match(" 3.129 "))
        m = _PAIR_RE.search("3.129/4,952")
        self.assertEqual((_to_int(m.group(1)), _to_int(m.group(2))), (3129, 4952))

    def test_overflow_dialog(self):
        from actions.overflow_dialog import _INT_RE, _PAIR_RE, _USED_CAP_RE
        self.assertTrue(_INT_RE.match("3.129"))
        self.assertTrue(_PAIR_RE.match("3.129/4,952"))
        self.assertTrue(_USED_CAP_RE.search("3.129/4,952 (63%)"))

    def test_village_remote_reader(self):
        from actions.village_remote_reader import _INT_RE
        self.assertTrue(_INT_RE.match("1.234"))

    def test_sell_goods_quantity_field(self):
        from actions.sell_goods import _QTY_PAIR_RE
        m = _QTY_PAIR_RE.match(" 1.200 / 3.129 ")
        self.assertIsNotNone(m, "the sell quantity field is two counts")

    def test_market_actions_number(self):
        from actions.market_actions import _parse_number
        self.assertEqual(_parse_number("3.129"), 3129)
        self.assertEqual(_parse_number("+1,050"), 1050)

    def test_none_of_them_accepts_punctuation_alone(self):
        """Widening the class must not make ',,,' a number."""
        from actions.overflow_dialog import _INT_RE as O
        from actions.village_remote_reader import _INT_RE as V
        from vision.hud_readers import _INT_RE as H
        for rx, name in ((O, "overflow"), (V, "remote"), (H, "hud")):
            self.assertIsNone(rx.match("..."), name)
            self.assertIsNone(rx.match(",,,"), name)
