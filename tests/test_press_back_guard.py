"""Test the press_back-on-top-level-screen guard.

The obstruction consult sometimes recommends press_back as a dismissal
for unfamiliar popups.  On screens where back means LEAVING the
location (village, port_overworld), the guard must downgrade
press_back → tap_close_x so the fleet doesn't accidentally exit the
village / port.
"""
import unittest
from unittest.mock import patch, MagicMock

from brain.perceive import _is_top_level_screen


def _frame():
    f = MagicMock()
    f.width = 2400
    f.height = 1080
    return f


def _chrome(back=False):
    """The real ChromeState has has_back_arrow / has_home / etc."""
    c = MagicMock()
    c.has_back_arrow = back
    c.has_home = False
    c.has_hamburger = False
    c.has_right_panel = False
    return c


def _detector_returning(chrome):
    """Mocks get_chrome_detector() → .detect(frame) → chrome."""
    detector = MagicMock()
    detector.detect.return_value = chrome
    return detector


class IsTopLevelScreenTests(unittest.TestCase):

    def test_village_screen_is_top_level(self):
        with patch("vision.ocr.read_port_name", return_value="Berber Village"), \
             patch("vision.text_correction.correct_village_name",
                   return_value=("Berber Village", 0.99)), \
             patch("vision.text_correction.correct_port_name",
                   return_value=(None, 0.0)), \
             patch("vision.chrome_detector.get_chrome_detector",
                   return_value=_detector_returning(_chrome(back=True))):
            self.assertTrue(_is_top_level_screen(_frame()))

    def test_port_overworld_is_top_level(self):
        # Top-left is a port name AND no back arrow → port_overworld.
        with patch("vision.ocr.read_port_name", return_value="Lisbon"), \
             patch("vision.text_correction.correct_village_name",
                   return_value=(None, 0.0)), \
             patch("vision.text_correction.correct_port_name",
                   return_value=("Lisbon", 0.99)), \
             patch("vision.chrome_detector.get_chrome_detector",
                   return_value=_detector_returning(_chrome(back=False))):
            self.assertTrue(_is_top_level_screen(_frame()))

    def test_building_inside_port_is_not_top_level(self):
        # Back arrow present + top-left matches port → it's a building
        # inside the port (back means going back to overworld, safe).
        with patch("vision.ocr.read_port_name", return_value="Lisbon"), \
             patch("vision.text_correction.correct_village_name",
                   return_value=(None, 0.0)), \
             patch("vision.text_correction.correct_port_name",
                   return_value=("Lisbon", 0.99)), \
             patch("vision.chrome_detector.get_chrome_detector",
                   return_value=_detector_returning(_chrome(back=True))):
            self.assertFalse(_is_top_level_screen(_frame()))

    def test_unrecognised_top_left_is_not_top_level(self):
        # Sub-menu titles ("Explore", "Barter", "Supply") aren't ports
        # or villages — must not classify as top-level.
        with patch("vision.ocr.read_port_name", return_value="Explore"), \
             patch("vision.text_correction.correct_village_name",
                   return_value=(None, 0.0)), \
             patch("vision.text_correction.correct_port_name",
                   return_value=(None, 0.0)), \
             patch("vision.chrome_detector.get_chrome_detector",
                   return_value=_detector_returning(_chrome(back=True))):
            self.assertFalse(_is_top_level_screen(_frame()))

    def test_empty_port_text_is_not_top_level(self):
        with patch("vision.ocr.read_port_name", return_value=""):
            self.assertFalse(_is_top_level_screen(_frame()))


if __name__ == "__main__":
    unittest.main()
