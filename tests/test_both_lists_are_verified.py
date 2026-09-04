"""Which list is open is CHECKED, for a port goal as well as a village one.

`_on_list` asked "is the village list open?" for a village goal and took the classifier's word
for a port goal:

    if goal.kind == "village" and not self._village_list_is_open():
        ... open ours before reading it

The village branch existed because that case had bitten once — 2026-08-29, reading its own
leftover query out of another list's search box. The port case had not bitten yet, so nothing
checked it.

Live 2026-09-01 it did. A bare world map was classified `destination_list`, the dispatcher
routed to `_on_list`, and with no check on the port path it went straight to reading and
typing into a list that was not there: the prefix landed on the MAP at frames 100 and 103, and
by the time the real list opened at 108 the typing budget was gone.

Both are verified now, and by WHAT IS IN THE RAIL rather than by which icon we believe we
tapped — the icons carry no label, so that belief is exactly the thing needing checking.

Measured on real frames:

    port list open   f114, f120     port True   village False
    village list     f014           port False  village True
    bare map         f099, f102     port False  village False
"""
import types
import unittest
from unittest import mock

from brain.activities.world_map import ChooseDestination, WorldMapActivity


def _act(*, port_open, village_open):
    opened = []
    a = WorldMapActivity(port_list_open_fn=lambda: port_open,
                         village_list_open_fn=lambda: village_open,
                         open_list_fn=lambda *a_, **k: opened.append(True),
                         capture_fn=lambda: object())
    return a, opened


class AListThatIsNotOpenIsOpenedFirst(unittest.TestCase):
    def test_a_port_goal_on_no_list_opens_one(self):
        a, opened = _act(port_open=False, village_open=False)
        res = a._on_list(ChooseDestination("Barcelona", "port"))
        self.assertTrue(opened, "this is what typed 'Barc' into the map")
        self.assertIn("opened", (res.observed or {}).get("did", ""))

    def test_a_village_goal_on_no_list_opens_one(self):
        a, opened = _act(port_open=False, village_open=False)
        a._on_list(ChooseDestination("Svear Village", "village"))
        self.assertTrue(opened)

    def test_a_port_goal_looking_at_the_VILLAGE_list_opens_its_own(self):
        """The two rails both carry a search box; only their rows differ."""
        a, opened = _act(port_open=False, village_open=True)
        a._on_list(ChooseDestination("Barcelona", "port"))
        self.assertTrue(opened, "reading the village list for a port is the 2026-08-29 bug")

    def test_a_village_goal_looking_at_the_PORT_list_opens_its_own(self):
        a, opened = _act(port_open=True, village_open=False)
        a._on_list(ChooseDestination("Svear Village", "village"))
        self.assertTrue(opened)


class AnOpenListIsReadNotReopened(unittest.TestCase):
    def _read(self, kind, *, port_open, village_open):
        a, opened = _act(port_open=port_open, village_open=village_open)
        with mock.patch.object(a, "_find_on_screen", return_value=(500, 400)):
            res = a._on_list(ChooseDestination("Barcelona", kind))
        return opened, res

    def test_the_port_list_is_read(self):
        opened, res = self._read("port", port_open=True, village_open=False)
        self.assertFalse(opened, "reopening a list that is open toggles it shut")
        self.assertIn("tapped", (res.observed or {}).get("did", ""))

    def test_the_village_list_is_read(self):
        opened, res = self._read("village", port_open=False, village_open=True)
        self.assertFalse(opened)


class CannotTellIsNotNo(unittest.TestCase):
    """Both checks must answer an unreadable frame the same way.

    The village check has always returned True there — "reading it as 'not ours' would reopen
    the list on every tick forever" — and tapping the rail while a list IS open toggles it
    shut (measured 2026-08-24). A port check that answered False would make a vision hiccup
    behave oppositely depending on the goal's kind.
    """

    def _unreadable(self, kind, target):
        a = WorldMapActivity(capture_fn=lambda: object())
        with mock.patch(target, side_effect=RuntimeError("vision down")):
            return a._list_is_open_for(kind)

    def test_the_port_check_does_not_reopen_forever(self):
        self.assertTrue(self._unreadable("port", "actions.sail_actions._port_list_open"))

    def test_and_the_village_check_agrees(self):
        self.assertTrue(self._unreadable("village",
                                         "actions.sail_actions._village_list_open"))


class ThePortTestIsNotAListOfPortNames(unittest.TestCase):
    def test_it_asks_whether_it_is_the_other_rail(self):
        """Naming every port in the world would be a second catalogue to keep in step."""
        import inspect
        from actions import sail_actions
        src = inspect.getsource(sail_actions._port_list_open)
        self.assertIn("not _village_list_open", src)
        self.assertIn("search_box_present", src)


if __name__ == "__main__":
    unittest.main()
