"""Decision learn-once cache — record/get/invalidate/persist."""
import pathlib
import tempfile
import unittest

from brain.decision_cache import DecisionCache


def _c(persist=False):
    return DecisionCache(path=pathlib.Path(tempfile.mktemp()), persist=persist)


_SIG = (("panel", None, "market", "Purchase", "none"), ("Buy", "Sell"))


class DecisionCacheTests(unittest.TestCase):
    def test_record_then_get(self):
        c = _c()
        self.assertIsNone(c.get(_SIG))
        c.record(_SIG, {"op": "tap", "arg": "Purchase", "why": "x"})
        self.assertEqual(c.get(_SIG), {"op": "tap", "arg": "Purchase"})

    def test_invalidate_matching_action(self):
        c = _c()
        c.record(_SIG, {"op": "tap", "arg": "X"})
        c.invalidate(_SIG, {"op": "tap", "arg": "X"})
        self.assertIsNone(c.get(_SIG))

    def test_invalidate_ignores_different_action(self):
        c = _c()
        c.record(_SIG, {"op": "tap", "arg": "X"})
        c.invalidate(_SIG, {"op": "tap", "arg": "Y"})     # not the cached one -> keep
        self.assertIsNotNone(c.get(_SIG))

    def test_empty_action_not_recorded(self):
        c = _c()
        c.record(_SIG, {})
        self.assertIsNone(c.get(_SIG))

    def test_persist_and_reload(self):
        p = pathlib.Path(tempfile.mktemp())
        DecisionCache(path=p, persist=True).record(_SIG, {"op": "back", "arg": None})
        reloaded = DecisionCache(path=p, persist=True)
        self.assertEqual(reloaded.get(_SIG), {"op": "back", "arg": None})


if __name__ == "__main__":
    unittest.main()
