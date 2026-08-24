"""Tests for the fleet-state / company-level readers (#14).

Currency values mirror the real HUD: ducat 56,358,188,631 · blue_gem 112,327 ·
red_gem 1,667 · green/action 559. The ducat is identified by MAGNITUDE, so the
reader is robust to OmniParser missing the huge ducat token (the off-by-one bug).
"""
import types
import unittest

from vision.hud_readers import read_currencies, _parse_company_level


def _el(label, cx, cy):
    return types.SimpleNamespace(label=label, cx=cx, cy=cy)


class ReadCurrenciesTests(unittest.TestCase):
    def test_full_cluster_with_ducat(self):
        els = [
            _el("56,358,188,631", 1560, 50), _el("112,327", 1720, 50),
            _el("1,667", 1850, 50), _el("559", 1990, 50),
        ]
        self.assertEqual(read_currencies(els), {
            "ducat": 56358188631, "blue_gem": 112327,
            "red_gem": 1667, "action_points": 559,
        })

    def test_ducat_missing_does_not_mislabel_gems(self):
        # The real failure: OmniParser dropped the ducat token. Gems must NOT shift
        # into the 'ducat' slot — they map to blue/red/green, ducat omitted.
        els = [_el("112,327", 1720, 50), _el("1,667", 1850, 50), _el("559", 1990, 50)]
        self.assertEqual(read_currencies(els), {
            "blue_gem": 112327, "red_gem": 1667, "action_points": 559,
        })

    def test_ignores_non_currency_top_left(self):
        # A number outside the top-right band (e.g. a port label) is ignored.
        els = [_el("56,358,188,631", 1560, 50), _el("112,327", 1720, 50),
               _el("999", 100, 50)]
        out = read_currencies(els)
        self.assertEqual(out["ducat"], 56358188631)
        self.assertEqual(out["blue_gem"], 112327)
        self.assertNotIn("red_gem", out)   # 999 was out of band, not counted

    def test_empty_when_no_cluster(self):
        self.assertEqual(read_currencies([_el("Portobelo", 343, 59)]), {})


class ParseCompanyLevelTests(unittest.TestCase):
    def test_level_and_pct(self):
        self.assertEqual(_parse_company_level("LV 92 68.81%"), (92, 68.81))

    def test_level_only(self):
        self.assertEqual(_parse_company_level("LV 92"), (92, None))

    def test_lowercase_and_spacing(self):
        self.assertEqual(_parse_company_level("lv92   12.5 %"), (92, 12.5))

    def test_none_without_level(self):
        self.assertIsNone(_parse_company_level("68.81%"))
        self.assertIsNone(_parse_company_level(""))


if __name__ == "__main__":
    unittest.main()
