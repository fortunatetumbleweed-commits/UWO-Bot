"""Tests for the village barter-panel reader (#12).

Token lists are REAL OCR output from the barter walkthrough:
frame_0002 = Apache Village, Neutral amity, Camas selected;
frame_0013 = same village at Friendly, Wampum selected.
The parser is pure over (text, conf, cx, cy) so these run without OCR.
"""
import unittest

from actions.barter_reader import _parse_barter_panel, BarterMaterial


# Real tokens (frame_0002): Neutral amity, Camas recipe.
_F0002 = [
    ("Barter", 0.68, 289, 49),
    ("Amity(Neutral)", 0.98, 627, 158), ("60,000/100,000", 0.89, 963, 164),
    ("Camas", 1.0, 2000, 158),
    ("Trade Quantity 709(+109)", 0.82, 2116, 200),
    ("709", 1.0, 1912, 241), ("Total Amity Change+3,153", 0.99, 2121, 241),
    ("200/200", 1.0, 2079, 296),
    ("Tradable Trade Goods", 1.0, 642, 321),
    ("Trade Material", 1.0, 1924, 638),
    ("881/170", 0.99, 2013, 809), ("862/170", 0.74, 1881, 810),
    ("Luxuries", 0.98, 1862, 852), ("Food", 1.0, 1995, 853),
    ("Trade Count (Current)", 0.93, 641, 860),
]

# Real tokens (frame_0013): Friendly amity, Wampum selected.
_F0013 = [
    ("Amity(Friendly)", 0.90, 635, 159), ("100,000/100,000", 0.93, 956, 164),
    ("Wampum", 0.81, 2019, 162),
    ("Trade Quantity 2", 0.81, 2059, 200),
    ("Total Amity Change-107", 0.79, 2106, 242),
    ("Trade Material", 1.0, 1924, 638),
]


class BarterPanelParseTests(unittest.TestCase):
    def test_amity_grade_and_points(self):
        r = _parse_barter_panel(_F0002)
        self.assertEqual(r.amity_grade, "Neutral")
        self.assertEqual(r.amity_points, (60000, 100000))

    def test_selected_good_and_output(self):
        r = _parse_barter_panel(_F0002)
        self.assertEqual(r.selected_good, "Camas")
        self.assertEqual(r.output_quantity, 709)

    def test_total_amity_change_signed(self):
        self.assertEqual(_parse_barter_panel(_F0002).total_amity_change, 3153)
        self.assertEqual(_parse_barter_panel(_F0013).total_amity_change, -107)

    def test_materials_have_need_with_labels(self):
        r = _parse_barter_panel(_F0002)
        mats = {m.label: (m.have, m.need) for m in r.materials}
        self.assertEqual(mats, {"Luxuries": (862, 170), "Food": (881, 170)})

    def test_friendly_frame(self):
        r = _parse_barter_panel(_F0013)
        self.assertEqual(r.amity_grade, "Friendly")
        self.assertEqual(r.amity_points, (100000, 100000))
        self.assertEqual(r.selected_good, "Wampum")
        self.assertEqual(r.output_quantity, 2)

    def test_none_when_not_barter_panel(self):
        self.assertIsNone(_parse_barter_panel([("City", 1.0, 500, 100),
                                               ("Info", 1.0, 600, 100)]))


class KbConversionTests(unittest.TestCase):
    def test_to_village(self):
        v = _parse_barter_panel(_F0002).to_village("Apache Village")
        self.assertEqual(v.name, "Apache Village")
        self.assertEqual(v.amity, "Neutral")
        self.assertEqual(v.amity_points, 60000)

    def test_to_recipe_partial(self):
        r = _parse_barter_panel(_F0002).to_recipe(villages=["Apache Village"])
        self.assertEqual(r.good, "Camas")
        self.assertEqual(r.output_per_round, {"Neutral": 709})
        self.assertEqual({i.material: i.ratio for i in r.inputs},
                         {"Luxuries": 170, "Food": 170})
        self.assertEqual(r.villages, ["Apache Village"])


if __name__ == "__main__":
    unittest.main()
