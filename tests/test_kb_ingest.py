"""Tests for the AI KB-ingestion compiler (#28) — LLM injected, no live model."""
import json
import tempfile
import unittest
from pathlib import Path

from memory import barter_kb as kb
from memory.barter_kb import BarterRecipe, PortPreference
from brain.kb_ingest import (
    build_prompt, parse_ingest_json, ingest_barter_knowledge,
)


_LLM_JSON = json.dumps({
    "recipes": [{
        "good": "Camas",
        "inputs": [{"material": "Luxuries", "ratio": 170, "source_ports": ["Portobelo"]},
                   {"material": "Food", "ratio": 170, "source_ports": ["Veracruz"]}],
        "villages": ["Apache Village"],
        "output_per_round": {"Neutral": 709, "Favorable": 744, "Friendly": 813},
        "preconditions": {"amity_min": "Favorable"},
    }],
    "preferences": [
        {"port": "Edinburgh", "preferences": {"Food": 30, "Textile": 50}},
    ],
})


class ParseTests(unittest.TestCase):
    def test_parses_records_and_stamps_season(self):
        out = parse_ingest_json(_LLM_JSON, season="7")
        self.assertEqual(len(out["recipes"]), 1)
        r = out["recipes"][0]
        self.assertIsInstance(r, BarterRecipe)
        self.assertEqual(r.good, "Camas")
        self.assertEqual(r.season, "7")
        self.assertEqual(len(r.inputs), 2)
        self.assertEqual(r.inputs[0].source_ports, ["Portobelo"])
        self.assertEqual(r.output_per_round["Friendly"], 813)
        self.assertEqual(r.preconditions.amity_min, "Favorable")
        p = out["preferences"][0]
        self.assertIsInstance(p, PortPreference)
        self.assertEqual(p.preferences["Food"], 30)
        self.assertEqual(p.season, "7")

    def test_tolerates_markdown_fences_and_prose(self):
        raw = "Sure! Here it is:\n```json\n" + _LLM_JSON + "\n```\nHope that helps."
        out = parse_ingest_json(raw, season="7")
        self.assertEqual(out["recipes"][0].good, "Camas")

    def test_bad_json_returns_empty(self):
        out = parse_ingest_json("not json at all", season="7")
        self.assertEqual(out, {"recipes": [], "preferences": []})

    def test_skips_malformed_entry_keeps_good_ones(self):
        raw = json.dumps({"recipes": [{"good": "Ok"}, 12345],
                          "preferences": []})
        out = parse_ingest_json(raw, season="7")
        self.assertEqual([r.good for r in out["recipes"]], ["Ok"])

    def test_prompt_includes_source(self):
        self.assertIn("SEASON-XYZ-SOURCE", build_prompt("SEASON-XYZ-SOURCE"))


class IngestSaveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._orig = (kb._RECIPES_PATH, kb._PREFERENCES_PATH)
        kb._RECIPES_PATH = base / "recipes.json"
        kb._PREFERENCES_PATH = base / "preferences.json"

    def tearDown(self):
        kb._RECIPES_PATH, kb._PREFERENCES_PATH = self._orig
        self._tmp.cleanup()

    def test_ingest_compiles_and_saves(self):
        out = ingest_barter_knowledge("some article text", season="7",
                                      llm_fn=lambda prompt: _LLM_JSON, save=True)
        self.assertEqual(out["saved"], 2)
        # persisted + reloadable through the KB
        self.assertEqual(kb.load_recipe("Camas").season, "7")
        self.assertEqual(kb.load_preference("Edinburgh").preferences["Textile"], 50)

    def test_ingest_no_save(self):
        out = ingest_barter_knowledge("text", season="7",
                                      llm_fn=lambda p: _LLM_JSON, save=False)
        self.assertEqual(out["saved"], 0)
        self.assertIsNone(kb.load_recipe("Camas"))


if __name__ == "__main__":
    unittest.main()
