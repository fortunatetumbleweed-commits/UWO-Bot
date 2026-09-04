"""The repository owns the screen: one observation, generational staleness, one acquirer.

Every rule here was decided from a live failure — see `docs/perceive_repository.md`.
"""
import unittest

from vision.perceive_repository import Blind, Observation, PerceiveRepository, Stale


class _Frame:
    """Stands in for a PIL image: identity matters, and it can be cropped."""

    def __init__(self, tag):
        self.tag = tag
        self.crops = []

    def crop(self, box):
        self.crops.append(box)
        return f"{self.tag}@{box}"


def _repo(frames=None, parse=None, ocr=None, capture_fails=0):
    """A repository whose collaborators are all fakes — no device, no models."""
    made = {"n": 0}
    seq = list(frames or [])

    def capture():
        made["n"] += 1
        if made["n"] <= capture_fails:
            raise RuntimeError("device '31101JEHN26098' not found")
        return seq[made["n"] - capture_fails - 1] if seq else _Frame(f"F{made['n']}")

    r = PerceiveRepository(
        capture_fn=capture,
        parse_fn=parse or (lambda f: [f"el-of-{getattr(f, 'tag', f)}"]),
        ocr_fn=ocr or (lambda img: f"text-of-{img}"),
        clock=lambda: 1000.0,
        sleep_fn=lambda s: None,
    )
    r._made = made
    return r


class OneObservationShared(unittest.TestCase):
    def test_two_readers_get_the_same_frame_object(self):
        r = _repo()
        a, b = r.get(), r.get()
        self.assertIs(a.frame, b.frame, "the SAME frame, not two captures of one screen")
        self.assertEqual(r._made["n"], 1, "and only one capture was paid for")

    def test_the_parse_is_derived_once_per_observation(self):
        calls = {"n": 0}

        def parse(f):
            calls["n"] += 1
            return ["el"]

        r = _repo(parse=parse)
        r.get()
        r.elements(); r.elements(); r.elements()
        self.assertEqual(calls["n"], 1, "three readers, one OmniParser pass")

    def test_a_region_is_cropped_once_per_observation(self):
        r = _repo()
        obs = r.get()
        r.read_region((0, 0, 10, 10)); r.read_region((0, 0, 10, 10))
        self.assertEqual(len(obs.frame.crops), 1, "the second read is served from the first")


class StalenessIsGenerationNotAge(unittest.TestCase):
    def test_a_new_observation_supersedes_the_old(self):
        r = _repo()
        first = r.get()
        r.invalidate("tapped Exchange")
        second = r.get()
        self.assertEqual((first.generation, second.generation), (1, 2))
        self.assertIsNot(first.frame, second.frame)

    def test_an_old_observation_stays_current_while_nothing_newer_exists(self):
        # The sea pacing sleeps for up to 1200 s between ticks. Nothing newer has been taken,
        # so what we hold IS the current word — age says nothing about it.
        r = _repo()
        held = r.get()
        again = r.get()
        self.assertIs(held, again)
        self.assertEqual(r._made["n"], 1, "age alone never forces a re-capture")

    def test_generations_only_go_forward(self):
        r = _repo()
        seen = []
        for _ in range(4):
            seen.append(r.get().generation)
            r.invalidate("acted")
        self.assertEqual(seen, [1, 2, 3, 4])


class ExactlyOneMemberAcquires(unittest.TestCase):
    def test_read_region_never_captures(self):
        r = _repo()
        r.get()
        before = r._made["n"]
        r.read_region((0, 0, 5, 5))
        self.assertEqual(r._made["n"], before)

    def test_for_logging_never_captures_even_with_nothing_held(self):
        r = _repo()
        self.assertIsNone(r.for_logging())
        self.assertEqual(r._made["n"], 0, "a log line must never cost a frame")

    def test_for_logging_returns_the_stale_one_rather_than_refusing(self):
        r = _repo()
        r.get()
        r.invalidate("tapped")
        self.assertIsNotNone(r.for_logging(), "logging takes what is there, stale or not")
        self.assertEqual(r._made["n"], 1)


class AStaleReadIsADiagnosis(unittest.TestCase):
    """A caller asking about a superseded frame acted and did not look again."""

    def test_read_region_after_an_action_refuses(self):
        r = _repo()
        r.get()
        r.invalidate("tapped the Luxuries tile")
        with self.assertRaises(Stale) as caught:
            r.read_region((0, 0, 5, 5))
        self.assertIn("tapped the Luxuries tile", str(caught.exception),
                      "the refusal names what superseded it")

    def test_it_does_not_quietly_re_acquire(self):
        r = _repo()
        r.get()
        r.invalidate("tapped")
        with self.assertRaises(Stale):
            r.read_region((0, 0, 5, 5))
        self.assertEqual(r._made["n"], 1,
                         "capturing behind the caller's back would hide the bug")

    def test_elements_before_anything_is_observed_refuses(self):
        with self.assertRaises(Stale):
            _repo().elements()


class SettleIsPartOfInvalidation(unittest.TestCase):
    def test_an_action_can_hold_off_the_next_look(self):
        waited = []
        r = PerceiveRepository(capture_fn=lambda: _Frame("F"), parse_fn=lambda f: [],
                               ocr_fn=lambda i: "", clock=lambda: 1000.0,
                               sleep_fn=waited.append)
        r.get()
        r.invalidate("tapped a dialog", settle_s=2.5)
        r.get()
        self.assertTrue(waited and waited[0] > 0,
                        "capturing mid-animation is worse than not capturing")


class ABlindBotStops(unittest.TestCase):
    def test_a_transient_failure_is_retried(self):
        r = _repo(capture_fails=2)
        self.assertEqual(r.get().generation, 1, "two hiccups, then a frame")

    def test_a_dead_device_raises_rather_than_serving_memories(self):
        r = _repo(capture_fails=99)
        with self.assertRaises(Blind):
            r.get()

    def test_it_does_not_fall_back_to_the_last_good_frame(self):
        r = _repo(capture_fails=0)
        r.get()
        r._capture_fn = lambda: (_ for _ in ()).throw(RuntimeError("device not found"))
        r.invalidate("acted")
        with self.assertRaises(Blind):
            r.get()   # acting on remembered pixels is the failure this prevents


if __name__ == "__main__":
    unittest.main()
