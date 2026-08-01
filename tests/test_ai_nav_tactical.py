"""Tests for the L4 tactical layer.

Covers:
  - direction-token parsing (single words + free-text answers)
  - direction → commit_heading mapping
  - cadence gate (heartbeat + no_shore + low_conf triggers)
  - MoondreamTactical with the underlying LocalVision mocked
  - NoOp pass-through

All tests run without Ollama / Moondream — the LocalVision is mocked
so they're fast and reproducible.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from brain.ai_nav.layers.tactical import (
    BaseTactical, MoondreamTactical, NoOpTactical,
    direction_to_commit_heading, parse_direction,
)
from brain.ai_nav.state import (
    CommitDirection, Heading, NavState, PlannerOutput, TacticalDecision,
)
from brain.ai_nav.vision_input import VisionFrame


def _make_frame(tick: int = 1, wall_ts: float | None = None) -> VisionFrame:
    img = Image.new("RGB", (2400, 1080), (0, 0, 0))
    return VisionFrame(raw=img, tick=tick, wall_ts=wall_ts or time.time())


def _make_state(tick: int = 1, *, no_shore: bool = False,
                heading_deg: float = 180.0, heading_conf: float = 0.5,
                commit_deg: float = 180.0) -> NavState:
    s = NavState(tick=tick)
    s.heading = Heading(heading_deg, heading_conf, "test")
    s.commit_direction = CommitDirection(commit_deg, "test_init", 0)
    s.planner_output = PlannerOutput(
        skip_reason="no_shore" if no_shore else None,
    )
    return s


# ── Direction parsing ─────────────────────────────────────────────────


@pytest.mark.parametrize("answer, expected", [
    ("forward", "forward"),
    ("left", "left"),
    ("right", "right"),
    ("back", "back"),
    ("FORWARD", "forward"),
    ("  Left.", "left"),
    ("The ship should turn left", "left"),
    ("go right!", "right"),
    ("keep going straight", "forward"),
    ("ahead", "forward"),
    ("reverse direction", "back"),
    ("u-turn", "back"),
    ("turn around", "back"),
    ("port", "left"),
    ("starboard", "right"),
    ("", None),
    ("I'm not sure", None),
    ("left or right", None),   # ambiguous — both patterns match
])
def test_parse_direction(answer, expected):
    assert parse_direction(answer) == expected


@pytest.mark.parametrize("direction, current, expected", [
    ("forward", 180.0, None),
    ("left",    180.0, 90.0),
    ("right",   180.0, 270.0),
    ("back",    180.0, 0.0),
    ("left",    10.0,  280.0),   # wraps around 0
    ("right",   350.0, 80.0),    # wraps around 360
    ("back",    270.0, 90.0),
    ("garbage", 180.0, None),
])
def test_direction_to_commit_heading(direction, current, expected):
    assert direction_to_commit_heading(direction, current) == expected


# ── Cadence gate ──────────────────────────────────────────────────────


class _StubConsult(BaseTactical):
    """Subclass that records every _consult invocation in self.consult_calls.

    Records inside _consult so we don't double-trigger _should_fire (which
    increments the streak counters with side effects).
    """
    name = "stub"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.consult_calls: list[int] = []

    def _consult(self, frame, state):
        self.consult_calls.append(state.tick)
        return TacticalDecision(
            classification="stub_forward",
            new_commit_heading=None,
            rationale="test",
            decided_at_tick=state.tick,
        )


def test_heartbeat_fires_at_first_tick():
    """Heartbeat sec elapsed since init (ts=0) → fires on tick 1."""
    layer = _StubConsult(heartbeat_sec=1.0)
    state = _make_state(tick=1)
    layer.maybe_consult(_make_frame(tick=1, wall_ts=100.0), state)
    assert layer.consult_calls == [1]


def test_heartbeat_does_not_fire_within_interval():
    """Second call within heartbeat window — no consult."""
    layer = _StubConsult(heartbeat_sec=60.0)
    base = 1000.0
    layer.maybe_consult(_make_frame(1, wall_ts=base), _make_state(1))
    layer.maybe_consult(_make_frame(2, wall_ts=base + 5), _make_state(2))
    assert layer.consult_calls == [1]


def test_no_shore_streak_trigger():
    """N consecutive no_shore ticks → fires on Nth."""
    layer = _StubConsult(
        heartbeat_sec=99999.0,
        consecutive_no_shore_trigger=3,
    )
    for t in range(1, 4):
        state = _make_state(t, no_shore=True)
        # Use wall_ts that DOESN'T satisfy heartbeat.
        frame = _make_frame(t, wall_ts=-1.0)
        layer.maybe_consult(frame, state)
    assert layer.consult_calls == [3]


def test_no_shore_streak_resets_on_success():
    layer = _StubConsult(
        heartbeat_sec=99999.0,
        consecutive_no_shore_trigger=3,
    )
    # 2 no_shore, then success, then 2 more no_shore → no trigger.
    seq = [True, True, False, True, True]
    for t, ns in enumerate(seq, 1):
        state = _make_state(t, no_shore=ns)
        layer.maybe_consult(_make_frame(t, wall_ts=-1.0), state)
    assert layer.consult_calls == []


def test_low_conf_streak_trigger():
    layer = _StubConsult(
        heartbeat_sec=99999.0,
        consecutive_low_conf_trigger=2,
        low_conf_floor=0.3,
    )
    for t in range(1, 3):
        state = _make_state(t, heading_conf=0.1)
        layer.maybe_consult(_make_frame(t, wall_ts=-1.0), state)
    assert layer.consult_calls == [2]


def test_commit_direction_updated_on_left_decision():
    """When a consult returns a new_commit_heading, the state's
    commit_direction must be replaced with reason='tactical(...)'."""

    class _LeftConsult(BaseTactical):
        name = "left"
        def _consult(self, frame, state):
            return TacticalDecision(
                classification="left",
                new_commit_heading=90.0,
                rationale="test",
                decided_at_tick=state.tick,
            )

    layer = _LeftConsult(heartbeat_sec=0.0)
    state = _make_state(tick=5, commit_deg=180.0)
    layer.maybe_consult(_make_frame(5, wall_ts=100.0), state)
    assert state.commit_direction.bearing_deg == 90.0
    assert state.commit_direction.reason == "tactical(left)"
    assert state.commit_direction.set_at_tick == 5


def test_commit_direction_unchanged_when_decision_is_none_or_forward():
    """Decision with new_commit_heading=None (e.g. 'forward') must
    NOT overwrite commit_direction."""

    class _ForwardConsult(BaseTactical):
        name = "fwd"
        def _consult(self, frame, state):
            return TacticalDecision(
                classification="forward",
                new_commit_heading=None,
                rationale="",
                decided_at_tick=state.tick,
            )

    layer = _ForwardConsult(heartbeat_sec=0.0)
    state = _make_state(tick=3, commit_deg=180.0)
    original = state.commit_direction
    layer.maybe_consult(_make_frame(3, wall_ts=100.0), state)
    assert state.commit_direction is original   # not replaced
    # But tactical history was updated.
    assert state.tactical is not None
    assert state.tactical.classification == "forward"


# ── MoondreamTactical with vision mocked ──────────────────────────────


def _make_moondream(answer: str = "left", available: bool = True
                    ) -> MoondreamTactical:
    """Build a MoondreamTactical whose internal vision is mocked
    to return the given answer."""
    layer = MoondreamTactical(side="port", heartbeat_sec=0.0)
    vision = MagicMock()
    vision.check_available.return_value = available
    vision.ask.return_value = answer
    layer._vision = vision   # bypass lazy init
    return layer


def test_moondream_left_updates_commit_direction():
    layer = _make_moondream(answer="left")
    state = _make_state(tick=10, commit_deg=180.0)
    layer.maybe_consult(_make_frame(10, wall_ts=100.0), state)
    assert state.commit_direction.bearing_deg == 90.0   # 180 - 90
    assert state.commit_direction.reason.startswith("tactical(moondream_left")


def test_moondream_forward_no_change():
    layer = _make_moondream(answer="forward")
    state = _make_state(tick=10, commit_deg=180.0)
    orig = state.commit_direction
    layer.maybe_consult(_make_frame(10, wall_ts=100.0), state)
    assert state.commit_direction is orig
    assert state.tactical.classification == "moondream_forward"


def test_moondream_unparseable_records_decision_but_no_commit_change():
    layer = _make_moondream(answer="I have no idea")
    state = _make_state(tick=10, commit_deg=180.0)
    orig = state.commit_direction
    layer.maybe_consult(_make_frame(10, wall_ts=100.0), state)
    assert state.commit_direction is orig
    assert state.tactical.classification == "moondream_unparsed"


def test_moondream_unavailable_returns_none():
    """Moondream unavailable → no tactical decision recorded at all."""
    layer = _make_moondream(answer="left", available=False)
    state = _make_state(tick=10, commit_deg=180.0)
    layer.maybe_consult(_make_frame(10, wall_ts=100.0), state)
    assert state.tactical is None
    assert state.commit_direction.bearing_deg == 180.0   # untouched


def test_moondream_ollama_exception_returns_none():
    """Ollama raises → layer recovers gracefully, no decision."""
    layer = _make_moondream(answer="left")
    layer._vision.ask.side_effect = RuntimeError("ollama crashed")
    state = _make_state(tick=10, commit_deg=180.0)
    layer.maybe_consult(_make_frame(10, wall_ts=100.0), state)
    assert state.tactical is None


# ── NoOp ───────────────────────────────────────────────────────────────


def test_noop_does_not_touch_state():
    layer = NoOpTactical()
    state = _make_state(tick=5, commit_deg=180.0)
    orig = state.commit_direction
    result = layer.maybe_consult(_make_frame(5, wall_ts=999.0), state)
    assert result.commit_direction is orig
    assert result.tactical is None
