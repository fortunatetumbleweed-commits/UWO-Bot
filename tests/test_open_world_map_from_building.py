"""`open_world_map` must WALK OUT of a screen it is inside, not wait for it to clear.

The wait-and-re-perceive branch exists for genuinely transient states: a busy port carries
NPC chatter bubbles that the family CNN labels 'transient', and those drift away on their
own. A building or sub_menu is not like that — no amount of waiting turns a Market into an
overworld.

Live 2026-08-21: a mission was started while the fleet sat on the Market's Purchase screen
at Jakarta. `open_world_map` logged "loc='sub_menu' not port/sea — waiting to re-perceive"
on attempt after attempt, never once trying to leave, and spent its entire budget.
"""
from unittest import mock

import actions.sail_actions as sa


def _run(states, opens=False):
    """Drive open_world_map over a scripted sequence of where_am_i locations.

    `opens=True` makes the map actually open once tapped, so the call can RETURN — otherwise
    the loop keeps going and later rounds pollute what the test is trying to observe.
    """
    seq = list(states)
    exits = []

    def fake_where(*_a, **_kw):
        return {"location": seq.pop(0) if seq else "unknown", "detail": "", "port": "Jakarta"}

    def fake_exit():
        exits.append(1)
        return True

    with mock.patch.object(sa, "where_am_i", side_effect=fake_where), \
         mock.patch.object(sa, "exit_to_overworld", side_effect=fake_exit), \
         mock.patch.object(sa, "capture_screen", return_value=object()), \
         mock.patch.object(sa, "_is_on_world_map", return_value=opens), \
         mock.patch.object(sa, "_world_map_port_labels_visible", return_value=False), \
         mock.patch.object(sa, "_ocr_frame", return_value=[]), \
         mock.patch.object(sa, "tap"), \
         mock.patch("time.sleep"), \
         mock.patch("random.uniform", return_value=0):
        ok = sa.open_world_map()
    return ok, exits


class TestExitsRatherThanWaits:
    def test_a_sub_menu_triggers_an_exit(self):
        _ok, exits = _run(["sub_menu"] * 10)
        assert exits, "open_world_map waited on a sub_menu instead of leaving it"

    def test_a_building_triggers_an_exit(self):
        _ok, exits = _run(["building"] * 10)
        assert exits

    def test_a_brief_transient_is_waited_out_not_exited(self):
        """Chatter bubbles clear on their own — walking out of a port would be wrong."""
        _ok, exits = _run(["unknown", "unknown", "port_overworld"], opens=True)
        assert exits == [], "a passing overlay must be waited out, not exited"

    def test_a_persistent_unknown_stops_waiting_and_tries_to_leave(self):
        """A screen that is simply OPEN never clears by waiting.

        Live 2026-08-21: the fleet/cargo panel was left open, the family CNN labelled it
        'transient' → 'unknown', and open_world_map re-perceived it ten times and gave up —
        ending the run with ZERO actions."""
        _ok, exits = _run(["unknown"] * 10)
        assert exits, "a persistent unrecognised screen must eventually be exited"


class TestNeverLeavesAPlace:
    """A settlement is a PLACE, not a screen to escape.

    Live 2026-08-22, run 27: the fleet stood IN Melanesian Village — the mission's own
    destination, all three materials aboard — and this loop judged 'village' "not port/sea",
    waited three re-perceives, then pressed Back until it was at sea:

        [open_world_map] loc='village' not port/sea — waiting to re-perceive (attempt 1/10)
        [open_world_map] loc='village' has persisted 3 re-perceives — trying to exit
        Exiting to port overworld...  Not on overworld — pressing back
        [classify] -> sea

    Leaving a village IS the only route to the world map from one — but that is the task's
    call, not this primitive's. Leaving an unrecognised PANEL costs nothing and stays.
    """

    def test_a_village_is_reported_not_escaped(self):
        ok, exits = _run(["village"] * 10)
        assert ok is False
        assert not exits, "pressing Back out of a village throws away the voyage that reached it"

    def test_an_unknown_panel_is_still_escaped(self):
        """The 2026-08-21 zero-actions failure must stay fixed."""
        _ok, exits = _run(["unknown"] * 10)
        assert exits, "an unrecognised OPEN screen never clears by waiting"
