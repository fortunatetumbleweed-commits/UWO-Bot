"""RiverExploreMission stuck-in-junction detection.

Regression for the 2026-07-16 live voyage t369-t400 pathology:
ship spent 30+ ticks oscillating inside one junction cell with net
displacement ~17 km / cumulative ~177 km, because the mission's
JunctionGraph was consulted only on channel→junction transitions.
Once committed to a dead-end exit, no re-evaluation.

Fix under test: after STUCK_WINDOW ticks in the same cell with net
displacement < STUCK_NET_DEG, mission marks the current exit failed
and forces a re-visit.
"""
from __future__ import annotations

import pytest

from brain.ai_nav.mission import RiverExploreMission
from brain.ai_nav.state import Heading, NavState
from brain.ai_nav.vision_input import VisionFrame
from PIL import Image


def _blank_frame():
    return VisionFrame(raw=Image.new("RGB", (2400, 1080)), tick=0)


def _tick_state(*, tick, lat, lon, topology, exits, hdg=0.0):
    st = NavState(tick=tick, lat=lat, lon=lon,
                  topology=topology,
                  junction_exits_compass=tuple(exits))
    st.heading = Heading(bearing_deg=hdg, confidence=1.0, source="test")
    return st


def test_stuck_in_junction_marks_exit_failed_and_repicks():
    """Ship enters a junction, mission picks an exit, ship oscillates
    inside the cell for STUCK_WINDOW ticks → mission marks the exit
    failed and re-consults the graph, which picks a different exit."""
    m = RiverExploreMission(default_bearing_deg=180.0)
    frame = _blank_frame()
    exits = (0.0, 90.0, 180.0, 270.0)          # N, E, S(entrance), W

    # tick 1: channel (approach)
    st = _tick_state(tick=1, lat=10.10, lon=32.00,
                     topology="channel", exits=(), hdg=0.0)
    st = m.update(frame, st)
    first_bearing = None

    # tick 2: enters junction — new_visit fires, graph picks exit
    st = _tick_state(tick=2, lat=10.05, lon=32.00,
                     topology="junction", exits=exits, hdg=0.0)
    st = m.update(frame, st)
    assert st.commit_direction is not None
    assert st.commit_direction.reason == "river_explore_junction"
    first_bearing = st.commit_direction.bearing_deg

    # ticks 3..N: oscillate at (10.05, 32.00) — same cell, tiny jitter.
    # Each tick's Δlat and Δlon are tiny; net stays < STUCK_NET_DEG.
    for i in range(3, 3 + m.STUCK_WINDOW + 1):
        # Small ±0.001° jitter — deep inside cell, net displacement
        # over the window is ~0.001° << STUCK_NET_DEG (0.03°).
        jitter_lat = 10.05 + (0.001 if i % 2 else -0.001)
        jitter_lon = 32.00 + (0.001 if i % 3 else -0.001)
        st = _tick_state(tick=i, lat=jitter_lat, lon=jitter_lon,
                         topology="junction", exits=exits, hdg=0.0)
        st = m.update(frame, st)

    # Now the mission should have detected stuck, marked the first
    # exit failed, and picked a different one on this tick's re-visit.
    assert st.commit_direction is not None
    second_bearing = st.commit_direction.bearing_deg
    assert second_bearing != first_bearing, (
        f"stuck-detector did not repick an exit: "
        f"first={first_bearing:.0f}°, second={second_bearing:.0f}°"
    )


def test_moving_through_junction_does_not_trigger_stuck():
    """If the ship makes real progress (net displacement > threshold)
    inside a junction, the stuck-detector must NOT fire.  Regression
    guard against over-triggering on legitimate wide-junction traversal.
    """
    m = RiverExploreMission(default_bearing_deg=180.0)
    frame = _blank_frame()
    exits = (0.0, 90.0, 180.0, 270.0)

    # Enter junction and pick exit
    _tick_state(tick=1, lat=10.10, lon=32.00,
                topology="channel", exits=(), hdg=0.0)
    st = _tick_state(tick=2, lat=10.05, lon=32.00,
                     topology="junction", exits=exits, hdg=0.0)
    m.update(frame, st)
    st = m.update(frame, st)                    # get initial commit
    first_bearing = st.commit_direction.bearing_deg

    # Ship makes steady southward progress inside a wide junction —
    # net displacement over the window is 25 * 0.005° = 0.125° >> threshold.
    for i in range(3, 3 + m.STUCK_WINDOW + 5):
        lat = 10.05 - (i - 2) * 0.005
        st = _tick_state(tick=i, lat=lat, lon=32.00,
                         topology="junction", exits=exits, hdg=180.0)
        st = m.update(frame, st)

    # Commit bearing should be unchanged (no re-pick fired).
    assert st.commit_direction.bearing_deg == first_bearing, \
        "stuck-detector fired on legitimate through-junction progress"


def test_clears_recent_positions_on_topology_exit():
    """When topology leaves 'junction', the position deque must reset
    so subsequent junction entries don't inherit stale data.
    """
    m = RiverExploreMission(default_bearing_deg=180.0)
    frame = _blank_frame()

    # Fill up the deque inside a junction
    exits = (0.0, 90.0, 180.0, 270.0)
    for i in range(1, m.STUCK_WINDOW):
        st = _tick_state(tick=i, lat=10.0, lon=32.0,
                         topology="junction", exits=exits, hdg=0.0)
        m.update(frame, st)

    # Now leave the junction into a channel — deque should clear
    # (append happens before the topology-exit check).
    st = _tick_state(tick=100, lat=9.5, lon=32.0,
                     topology="channel", exits=(), hdg=180.0)
    m.update(frame, st)
    assert len(m._recent_positions) == 0
