"""Tests for the remote village readers (gaps A + B). Element data is the REAL
OmniParser output captured live 2026-08-15 (Melanesian Village barter + Coral source)."""
import types
import unittest

from actions.village_remote_reader import read_barter_ratios, read_material_sources


def _el(label, cx, cy):
    return types.SimpleNamespace(label=label, cx=cx, cy=cy)


# Real OmniParser elements — Village Info → Barter Trade List (toggle OFF).
_BARTER_ELS = [
    _el("Box of Nutmeg", 1946, 425), _el("591", 1762, 463), _el("Spices", 2025, 501),
    _el("Ebony", 1880, 568), _el("136", 1776, 587), _el("Wares", 1993, 613),
    _el("Coral", 1873, 677), _el("204", 1774, 699), _el("Jewelry", 1993, 723),
    _el("Textiles", 1893, 789), _el("180", 1775, 808), _el("Fabrics", 1996, 833),
]

# Real OmniParser elements — Coral 'Source' panel.
_SOURCE_ELS = [
    _el("Coral", 616, 295), _el("Jewelry", 526, 298),
    _el("Male", 393, 463), _el("Atuona", 406, 514), _el("Guam", 400, 565),
    _el("Las Palmas", 432, 616), _el("Market", 617, 641), _el("Samarai", 413, 666),
    _el("Santo Domingo", 622, 713), _el("Sofala", 613, 765), _el("Whanganui", 433, 820),
]


class BarterRatiosTests(unittest.TestCase):
    def test_reads_output_and_material_ratios(self):
        self.assertEqual(read_barter_ratios(_BARTER_ELS), {
            "Box of Nutmeg": 591, "Ebony": 136, "Coral": 204, "Textiles": 180,
        })

    def test_excludes_category_chrome(self):
        r = read_barter_ratios(_BARTER_ELS)
        for chrome in ("Spices", "Wares", "Jewelry", "Fabrics"):
            self.assertNotIn(chrome, r)


class MaterialSourcesTests(unittest.TestCase):
    def test_reads_all_source_ports_in_order(self):
        """'Malé', not 'Male': every line is now resolved against the world-map catalogue,
        and what comes back is the CANONICAL spelling — the panel renders accent-free, and
        the name downstream sails to has to match the catalogue rather than the read.

        Nothing else about this panel changes: all eight are real ports, so all eight
        survive the check in the order they were read."""
        self.assertEqual(read_material_sources(_SOURCE_ELS), [
            "Malé", "Atuona", "Guam", "Las Palmas", "Samarai",
            "Santo Domingo", "Sofala", "Whanganui",
        ])

    def test_excludes_good_category_and_market_header(self):
        ports = read_material_sources(_SOURCE_ELS)
        for chrome in ("Coral", "Jewelry", "Market"):
            self.assertNotIn(chrome, ports)

    def test_keeps_multiword_port_whole(self):
        self.assertIn("Santo Domingo", read_material_sources(_SOURCE_ELS))


if __name__ == "__main__":
    unittest.main()
