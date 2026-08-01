"""Phase 1.5 — `_project_lyap_state` unit tests.

The projection lets the Lyapunov regulator keep running through short
shore-blind gaps by reusing the most recent valid state with bounded
heading-delta correction.  See docs/shore_following_design.md §12.5.2.

These tests cover the two acceptance contracts:

1. **Bounded projection works** — within age/heading caps, the
   projection rotates `theta_err` by the heading delta and preserves
   `d`, `d_star`.
2. **Caps refuse confident-but-wrong projection** — outside the caps,
   the projection returns None so the policy falls back to today's
   shore-blind behaviour rather than acting on a fantasy.
"""
from __future__ import annotations

import pytest

from brain.goals.hug_shore import (
    _project_lyap_state,
    LYAPUNOV_MEM_MAX_AGE_TICKS,
    LYAPUNOV_MEM_MAX_HEADING_DELTA_DEG,
)


def _mem(tick=10, heading=90.0, d=0.10, d_star=0.07, theta_err=5.0):
    return {
        "tick":      tick,
        "heading":   heading,
        "d":         d,
        "d_star":    d_star,
        "theta_err": theta_err,
    }


def test_no_memory_returns_none():
    assert _project_lyap_state(None, 90.0, 11) is None


def test_no_heading_returns_none():
    assert _project_lyap_state(_mem(), None, 11) is None


def test_fresh_same_tick_rejected():
    # age == 0 means "we already had a live reading this tick"; the
    # projection path shouldn't fire (caller would have used live).
    assert _project_lyap_state(_mem(tick=10), 90.0, 10) is None


def test_one_tick_no_rotation_preserves_state():
    memory = _mem(tick=10, heading=90.0, d=0.20, d_star=0.07, theta_err=12.0)
    state = _project_lyap_state(memory, 90.0, 11)
    assert state is not None
    d, d_star, theta_err = state
    assert d == 0.20
    assert d_star == 0.07
    assert theta_err == pytest.approx(12.0)


def test_rotation_rotates_theta_err_signed():
    # Cached theta_err was +12° at heading 90°.  Now heading is 110°
    # (turned right 20°).  Shore tangent therefore appears 20° to the
    # LEFT of new bow, so theta_err drops by 20°.
    memory = _mem(tick=10, heading=90.0, theta_err=12.0)
    state = _project_lyap_state(memory, 110.0, 11)
    assert state is not None
    _, _, theta_err = state
    assert theta_err == pytest.approx(-8.0)


def test_rotation_wraps_around_north():
    # heading 350° → heading 10° is a +20° right turn, not a -340° one.
    memory = _mem(tick=10, heading=350.0, theta_err=0.0)
    state = _project_lyap_state(memory, 10.0, 11)
    assert state is not None
    _, _, theta_err = state
    assert theta_err == pytest.approx(-20.0)


def test_age_cap_drops_old_memory():
    memory = _mem(tick=10)
    # Just inside cap — should project.
    inside = _project_lyap_state(
        memory, 90.0, 10 + LYAPUNOV_MEM_MAX_AGE_TICKS,
    )
    assert inside is not None
    # Just outside cap — should refuse.
    outside = _project_lyap_state(
        memory, 90.0, 10 + LYAPUNOV_MEM_MAX_AGE_TICKS + 1,
    )
    assert outside is None


def test_heading_delta_cap_drops_chaotic_projection():
    memory = _mem(tick=10, heading=90.0)
    # Just inside the rotation cap — should project.
    inside_heading = 90.0 + LYAPUNOV_MEM_MAX_HEADING_DELTA_DEG - 1.0
    assert _project_lyap_state(memory, inside_heading, 11) is not None
    # Just past the rotation cap — should refuse.  The Benghazi spiral
    # is the motivating case: ~200° of rotation across the chaos
    # window made the projection meaningless.
    outside_heading = 90.0 + LYAPUNOV_MEM_MAX_HEADING_DELTA_DEG + 1.0
    assert _project_lyap_state(memory, outside_heading, 11) is None


def test_negative_rotation_also_capped():
    # Cap is symmetric: -46° rotation refused, -44° accepted.
    memory = _mem(tick=10, heading=90.0)
    assert _project_lyap_state(memory, 90.0 - 44.0, 11) is not None
    assert _project_lyap_state(memory, 90.0 - 46.0, 11) is None
