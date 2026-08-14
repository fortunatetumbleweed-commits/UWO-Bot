"""HUD numeric readers — ducats / cargo / crew (the task-executor ground truth)."""
import unittest

import vision.hud_readers as hud
from vision.hud_readers import read_ducats, read_cargo, read_crew, _to_int_sep


class _E:
    """Minimal OmniParser-element stand-in."""
    def __init__(self, label, cx, cy, element_type="text"):
        self.label, self.cx, self.cy, self.element_type = label, cx, cy, element_type


class ToIntSepTests(unittest.TestCase):
    def test_comma_and_period_both_thousands(self):
        self.assertEqual(_to_int_sep("2,275"), 2275)
        self.assertEqual(_to_int_sep("2.275"), 2275)   # OCR reads comma as period
        self.assertEqual(_to_int_sep("55,437,159,939"), 55437159939)
        self.assertIsNone(_to_int_sep("abc"))


class DucatTests(unittest.TestCase):
    def _cluster(self):
        # top-right currency row: ducat(leftmost) blue red energy
        return [_E("55,437,159,939", 1565, 50), _E("99,180", 1801, 50),
                _E("1,633", 1965, 50), _E("555", 2079, 46, "button")]

    def test_leftmost_topright_is_ducat(self):
        self.assertEqual(read_ducats(self._cluster()), 55437159939)

    def test_keyword_fallback_when_not_topright(self):
        # Company-Overview style: 'Ducat' label + value in a left panel
        els = [_E("Ducat", 200, 120), _E("55,436,918,020", 420, 120),
               _E("Blue Gem", 200, 200), _E("99,180", 420, 200)]
        self.assertEqual(read_ducats(els), 55436918020)

    def test_none_when_no_currency(self):
        self.assertIsNone(read_ducats([_E("Purchase", 2100, 990, "button")]))


class CargoTests(unittest.TestCase):
    def test_pair_near_cargo_label(self):
        els = [_E("Cargo", 1700, 200), _E("18/4,108", 1900, 200)]
        self.assertEqual(read_cargo(els), (18, 4108))

    def test_load_capacity_label(self):
        els = [_E("Total Load Capacity", 700, 300), _E("885/4,108", 1000, 300)]
        self.assertEqual(read_cargo(els), (885, 4108))

    def test_none_without_cargo_anchor(self):
        # a crew pair on the recruit screen must NOT be read as cargo
        self.assertIsNone(read_cargo([_E("953/2,275", 2078, 660, "button")]))


class CrewTests(unittest.TestCase):
    def _patch_tokens(self, tokens):
        hud._ocr_tokens = lambda frame: tokens   # (text, conf, cx, cy)

    def test_fleet_total_not_per_ship(self):
        # anchor + fleet total (largest cap) + per-ship pairs
        self._patch_tokens([
            ("Fleet Crew Size", 0.9, 1900, 205),
            ("953/2.275", 0.9, 2078, 229),        # fleet total (comma read as .)
            ("171/337", 0.9, 1719, 250),          # per-ship — must be ignored
            ("114/540", 0.9, 1720, 394),
        ])
        self.assertEqual(read_crew(object()), (953, 2275))

    def test_none_without_anchor(self):
        self._patch_tokens([("18/4,108", 0.9, 1900, 200)])   # cargo, no crew anchor
        self.assertIsNone(read_crew(object()))

    def test_rejects_garbled_current_gt_capacity(self):
        self._patch_tokens([
            ("Fleet Crew Size", 0.9, 1900, 205),
            ("9531/2.275", 0.9, 2078, 229),       # garbled cur>cap → skip
            ("953/2,275", 0.9, 2078, 260),        # valid
        ])
        self.assertEqual(read_crew(object()), (953, 2275))


if __name__ == "__main__":
    unittest.main()
