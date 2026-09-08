"""What we consult an LLM about must be the GAME, organised — not everything on the glass.

Live 2026-09-08, Qwen reported the fleet "sailing in the Atlantic Ocean" twenty times about
a bot standing in a market in Jakarta. It was reading the account watermark off the bottom
of the screen: `4.0803.091.322 2608211115 Atlantic Ocean`, a build number, a player UID and
a server label, none of which are the game.

Three places had already spotted this and all three chose to EXPLAIN the noise rather than
remove it — the OCR list carried it, the chrome glossary warned 'BAD: "sailing in the
<server> server"', and `_universal.md` spent 35 lines on the row, including a paragraph
saying the server name and the sea region "can coincide". Telling a 1.5B model to disregard
noise does not work, and naming a phrase in order to forbid it puts the phrase in its
context anyway.

Measured over 40 frames: below 0.975 of the frame height there are exactly three things —
the device clock, the Wi-Fi indicator and that watermark. The band just above carries real
game text (`LV 93`, `Bureau`), so that is where the line goes.
"""

from __future__ import annotations

import unittest


class TheDeviceStripNeverReachesTheModel(unittest.TestCase):

    def _prompt(self, frame_h):
        import vision.qwen_perception as q
        q._CHROME_GLOSSARY = None
        tokens = [
            ("Purchase", 0.99, 118, 163),                      # the screen's own title
            ("Ebony", 0.98, 573, 317),                         # a good on the shelf
            ("12.33", 0.99, 71, 1064),                         # the PHONE's clock
            ("Wi-Fi", 1.00, 168, 1063),                        # the PHONE's radio
            ("4.0803.091.322 2608211115 Atlantic Ocean",       # build + UID + server
             0.88, 2153, 1067),
        ]
        return q._build_prompt("sub_menu", "sub_menu: purchase", tokens, frame_h=frame_h)

    def test_the_watermark_the_clock_and_the_radio_are_all_gone(self):
        prompt = self._prompt(1080)
        for junk in ("Atlantic Ocean", "Wi-Fi", "12.33", "2608211115"):
            self.assertNotIn(junk, prompt,
                             f"{junk!r} is the phone or the account, never the game")

    def test_the_game_survives_the_filter(self):
        prompt = self._prompt(1080)
        self.assertIn("Purchase", prompt)
        self.assertIn("Ebony", prompt)

    def test_an_unknown_frame_height_filters_nothing(self):
        """We cannot place the line without knowing the frame — so we do not guess."""
        self.assertIn("Atlantic Ocean", self._prompt(0))

    def test_the_tokens_arrive_positioned_and_in_reading_order(self):
        """Organised, not lumped: the coordinates used to be dropped on the floor."""
        prompt = self._prompt(1080)
        self.assertIn("(118,163)", prompt, "each token keeps its position")
        self.assertLess(prompt.index("Purchase"), prompt.index("Ebony"),
                        "and they arrive top-to-bottom")


class TheGlossaryDoesNotPlantWhatWeRemoved(unittest.TestCase):
    """A warning about a phrase still supplies the phrase."""

    def test_no_section_describes_the_filtered_row(self):
        import vision.qwen_perception as q
        q._CHROME_GLOSSARY = None
        text = q._load_chrome_glossary()
        for planted in ("Atlantic Ocean", "Wi-Fi", "battery", "UID"):
            self.assertNotIn(planted, text,
                             f"the glossary must not name {planted!r} — it is not sent")

    def test_the_universal_layout_composes_to_nothing_when_absent(self):
        """It was deleted; a layout file that is gone must not break composition."""
        from vision.scene_layouts import load_layout
        out = load_layout("port_overworld", "") or ""
        for planted in ("Atlantic Ocean", "Wi-Fi", "player UID"):
            self.assertNotIn(planted, out)


class TheFilterIsByPositionNotByWording(unittest.TestCase):
    """A player on another server must be filtered too, and a real place name kept."""

    def test_a_place_name_high_on_the_screen_is_kept(self):
        from vision.qwen_perception import _above_the_device_strip
        toks = [("Atlantic Ocean", 0.9, 300, 200)]       # a sea region, said by the game
        self.assertEqual(1, len(_above_the_device_strip(toks, 1080, lambda r: r[3])))

    def test_another_servers_watermark_is_dropped_all_the_same(self):
        from vision.qwen_perception import _above_the_device_strip
        toks = [("4.0803.091.322 2608211115 Pacific", 0.9, 2153, 1067)]
        self.assertEqual([], _above_the_device_strip(toks, 1080, lambda r: r[3]))


if __name__ == "__main__":
    unittest.main()
