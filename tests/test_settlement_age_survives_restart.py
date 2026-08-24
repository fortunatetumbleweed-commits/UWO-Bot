"""A settlement loaded from disk must report a real age, not "seen this tick".

`last_known_settlement` is persisted so a bot restarted mid-voyage still has an origin to
dead-reckon from. Its age, though, was `last_known_settlement_age_ticks`, and ticks reset to
zero every run — so the seeding branch set age 0 with the comment "unknown age across
restart; treat as fresh-from-disk". A settlement saved days earlier was indistinguishable
from one observed on the current tick.

The file already wrote `saved_at`, but as a local-time string with no zone, and nothing ever
read it back (user, 2026-08-22: "using ticks count is fine if the data is only in one
session, but I think using time stamp is better").
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from brain import observation


class PersistedSettlementCarriesItsAge(unittest.TestCase):

    def setUp(self):
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "last_settlement.json"
        self._p = patch.object(observation, "_SETTLEMENT_PATH", self.path)
        self._p.start()
        self.addCleanup(self._p.stop)
        observation.reset()

    def _write(self, name, at):
        self.path.write_text(json.dumps({"name": name, "at": at, "saved_at": "whenever"}))
        observation.reset()

    def test_saving_records_an_epoch(self):
        observation._save_persisted_settlement("Kolkata")
        data = json.loads(self.path.read_text())
        self.assertIsInstance(data.get("at"), float)
        self.assertAlmostEqual(data["at"], time.time(), delta=30)

    def test_a_day_old_settlement_is_not_reported_as_fresh(self):
        day_ago = time.time() - 86_400
        self._write("Kolkata", day_ago)
        self.assertEqual(observation._load_persisted_settlement(), "Kolkata")
        self.assertAlmostEqual(observation._persisted_settlement_at, day_ago, delta=1)

    def test_a_file_without_a_stamp_reports_unknown_not_zero(self):
        """Older files predate the field; unknown must not read as 'just now'."""
        self.path.write_text(json.dumps({"name": "Kolkata"}))
        observation.reset()
        self.assertEqual(observation._load_persisted_settlement(), "Kolkata")
        self.assertIsNone(observation._persisted_settlement_at)

    def test_a_corrupt_stamp_does_not_break_the_load(self):
        self.path.write_text(json.dumps({"name": "Kolkata", "at": "not-a-number"}))
        observation.reset()
        self.assertEqual(observation._load_persisted_settlement(), "Kolkata")
        self.assertIsNone(observation._persisted_settlement_at)


class ObservationReportsSettlementAge(unittest.TestCase):

    def test_age_is_None_when_never_seen(self):
        obs = observation.BotObservation(tick=0, timestamp=None)
        self.assertIsNone(obs.settlement_age_s())

    def test_age_is_measured_from_the_sighting(self):
        obs = observation.BotObservation(tick=0, timestamp=None,
                                         last_known_settlement="Kolkata",
                                         last_known_settlement_seen_at=1000.0)
        self.assertAlmostEqual(obs.settlement_age_s(now=1120.0), 120.0)


if __name__ == "__main__":
    unittest.main()
