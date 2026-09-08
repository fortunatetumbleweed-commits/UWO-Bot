"""A test must not tell the bot where the fleet is.

`brain.observation` persists the last detected settlement to
`memory/knowledge/state/last_settlement.json` so the next process knows where it woke up.
`test_perceive_observation_wire` drives `port_overworld` / `port='Amsterdam'` through the
GENUINE wiring — which is the point of that test — and the genuine wiring saves to disk.

So every full-suite run rewrote the file:

    {"name": "Amsterdam", "at": ..., "saved_at": "2026-09-07T21:50:34"}

Six suite runs on 2026-09-07, and the fleet had not been near Amsterdam in 24 hours. It was
not inert: the San mission opened with `[observation] loaded persisted settlement:
'Amsterdam'` and stamped every market result that leg with a port it was not standing in.

It matters more now than it did. `current_position` compares the two settlement records by
AGE and takes the fresher, so a value written by the test suite is not merely wrong — it is
the most recent thing on disk, and it wins.

Same rule as the ADB guard: the suite may exercise any path it likes, but the world it
writes to has to be a scratch one. conftest points `_SETTLEMENT_PATH` at tmp_path for every
test; this asserts that guard is in place rather than trusting it.
"""

from __future__ import annotations

import unittest
from pathlib import Path


class TheGuardIsInPlace(unittest.TestCase):

    def test_the_settlement_path_is_not_the_production_one(self):
        from brain import observation
        live = Path("memory/knowledge/state/last_settlement.json").resolve()
        self.assertNotEqual(Path(observation._SETTLEMENT_PATH).resolve(), live,
                            "conftest must point this at a scratch file")

    def test_saving_here_does_not_touch_the_production_file(self):
        from brain import observation
        live = Path("memory/knowledge/state/last_settlement.json")
        before = live.read_text() if live.exists() else None
        observation._save_persisted_settlement("Atlantis")
        after = live.read_text() if live.exists() else None
        self.assertEqual(before, after, "the suite wrote to the real settlement record")

    def test_and_the_scratch_file_really_did_get_it(self):
        """The path is redirected, not disabled — the save/load logic still runs."""
        from brain import observation
        observation._save_persisted_settlement("Atlantis")
        self.assertTrue(Path(observation._SETTLEMENT_PATH).exists())
        observation.reset()
        self.assertEqual(observation._ensure_persisted_loaded(), "Atlantis")


if __name__ == "__main__":
    unittest.main()
