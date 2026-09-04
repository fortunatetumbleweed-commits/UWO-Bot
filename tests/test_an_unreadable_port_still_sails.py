"""No readable origin is a WORSE ROUTE, not a dead mission.

The origin ORDERS the gather legs by distance. It does not choose which ports to visit, and
it has nothing to do with whether the fleet can sail — picking a destination on the world map
never depended on knowing where we started (user, 2026-08-31: "from port is only for good
logging, for sailing it is really not important").

It used to refuse, and that cost two runs on consecutive days for the same reason, which had
nothing to do with legibility: the REMOTE CHECK that reads the recipe leaves the fleet on the
WORLD MAP, and the world map paints no port name. The step that reads the recipe guaranteed
the next step could not see a port.

What remains forbidden is INVENTING an origin. On 2026-08-21 a made-up one sent the fleet to
Atuona at 5,948 instead of Masulipatnam at 294. Dropping the distance term is not guessing at
it — the legs run by coverage instead, and the fleet sails.
"""
import unittest

from brain.gathering_solver import plan_gathering

SOURCES = {"Raisin": ("Madeira",), "Pig": ("Luanda",)}
COORDS = {"Madeira": (10, 10), "Luanda": (80, 80)}
QTY = {"Raisin": 100, "Pig": 100}


def _route(start):
    got = plan_gathering(["Raisin", "Pig"], SOURCES, COORDS, start, quantities=QTY)
    return list(getattr(got, "route", got) or [])


class WithoutAnOriginItStillPlans(unittest.TestCase):
    def test_every_material_is_still_covered(self):
        route = _route(None)
        self.assertEqual(sorted(route), ["Luanda", "Madeira"],
                         "both ports are still visited; only their ORDER is unconstrained")

    def test_it_does_not_raise(self):
        _route(None)          # `_dist(None, ...)` used to be reached and blow up

    def test_an_origin_still_orders_by_distance(self):
        near_madeira = _route((0, 0))
        near_luanda = _route((100, 100))
        self.assertEqual(near_madeira[0], "Madeira", "nearest first when we know where we are")
        self.assertEqual(near_luanda[0], "Luanda")

    def test_the_two_agree_on_WHAT_to_visit(self):
        self.assertEqual(sorted(_route(None)), sorted(_route((0, 0))),
                         "the origin changes the order, never the set")


class TheMissionNoLongerRefuses(unittest.TestCase):
    def test_the_refusal_is_gone(self):
        import inspect
        from brain import barter_command
        src = inspect.getsource(barter_command)
        self.assertNotIn("the gather route cannot be ordered", src,
                         "an unreadable port must not end the mission")

    def test_but_a_made_up_origin_is_still_refused(self):
        """The 2026-08-21 guard survives: a named port that is NOT in the catalogue is an
        error, because that is a fiction rather than an absence."""
        import inspect
        from brain import barter_command
        src = inspect.getsource(barter_command)
        self.assertIn("is not in the catalogue", src)


if __name__ == "__main__":
    unittest.main()
