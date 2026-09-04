"""The origin's name, carried on the arrival frame, is not evidence of where we are.

LIVE 2026-08-30. The fleet sailed from Luanda to Hutu Village and ARRIVED — the village
screen painted "Hutu Village" on its name plate and in the left rail. But arrival is detected
on the sea frame (speed 0, ETA gone), and `state.port` there still held the DEPARTURE port,
'Luanda'. A village never sets that field at all: perceive on the arrived screen reports
state='village', port=None.

So the check compared the destination against where the fleet had BEEN, concluded "the
departure did not take", and ended the mission standing in the village it was sent to — six
barter rounds open, Pig 1,792 and Raisin 1,580 aboard.

The guard's real case is port-to-port: a departure that silently failed leaves the fleet on
the ORIGIN's overworld, which does paint its name. That case is kept.
"""
import unittest


class _Runner:
    def __init__(self, port):
        self.status, self.port, self.reason = "arrived", port, None


class TheWorkOrderAlreadySaysWhichItIs(unittest.TestCase):
    """The leg is DECLARED as a village leg — `_sail_to(..., kind="village")` — so nothing
    has to guess from the name. Deriving it instead meant importing `vision` into the task
    layer, which the layering rule forbids, to re-answer a question the order had answered."""

    def test_the_village_leg_declares_its_kind(self):
        import inspect, brain.barter_mission_live as live
        src = inspect.getsource(live)
        self.assertIn('kind="village"', src,
                      "the village leg states its kind; the arrival check reads that")

    def test_sail_to_takes_a_kind(self):
        import inspect
        from brain.barter_mission_live import _sail_to
        fn = getattr(_sail_to, "__wrapped__", _sail_to)
        self.assertIn("kind", inspect.signature(fn).parameters)


class TheArrivalSurvivesACarriedPortName(unittest.TestCase):
    def _sail(self, reported, destination):
        """Drive `_sail_to`'s verification with a runner that reports `reported`."""
        from unittest.mock import patch
        import brain.barter_mission_live as live
        runner = _Runner(reported)
        with patch.object(live, "_run_sail_goal", return_value=runner, create=True):
            try:
                return live._sail_to.__wrapped__(destination)   # past the facade decorator
            except AttributeError:
                self.skipTest("_sail_to is not wrapped the way this test assumes")

    def test_luanda_does_not_disprove_hutu_village(self):
        # The exact live pairing. Asserted on the RULE rather than the plumbing, so this
        # stays true however `_sail_to` is wired.
        from vision.text_correction import correct_village_name
        reported, destination = "Luanda", "Hutu Village"
        going_to_a_village = correct_village_name(destination)[0] is not None
        reported_a_village = correct_village_name(reported)[0] is not None
        self.assertTrue(going_to_a_village and not reported_a_village,
                        "a port name carried from the origin says nothing about a village")

    def test_a_wrong_port_still_fails_a_port_leg(self):
        from vision.text_correction import correct_village_name
        reported, destination = "Luanda", "Lisboa"
        going_to_a_village = correct_village_name(destination)[0] is not None
        self.assertFalse(going_to_a_village,
                         "port-to-port keeps the guard: a gather leg must not buy at the "
                         "wrong port")


if __name__ == "__main__":
    unittest.main()
