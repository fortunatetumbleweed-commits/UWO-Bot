"""Smoke test for tools/run_ai_nav_live.py — Phase 1 runner.

Runs the runner against a saved session with the noop action layer.
Verifies the output schema is compatible with tick_viewer.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SAVED = REPO / "data/sessions/live_centerline_2026-06-17T11-53-04"


@pytest.fixture
def out_session(tmp_path: Path) -> Path:
    return tmp_path / "ai_nav_smoke"


@pytest.mark.skipif(not (SAVED / "tick_0005.png").exists(),
                    reason="saved replay session unavailable")
def test_runner_main_replay_dry_run(monkeypatch, out_session):
    """Drive tools.run_ai_nav_live.main() against a saved replay,
    NoOp action layer, no sail_start.  Verify it produces a valid
    trace.jsonl and one tick_NNNN.png per executed tick."""
    from tools import run_ai_nav_live

    argv = [
        "run_ai_nav_live",
        "--source", f"file:{SAVED}",
        "--action", "noop",
        "--no-sail-start",
        "--max-ticks", "5",
        "--out-dir", str(out_session),
        "--start-lat", "30.10",
        "--start-lon", "30.46",
        "--log-level", "WARNING",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    run_ai_nav_live.main()

    trace_path = out_session / "trace.jsonl"
    assert trace_path.exists(), "trace.jsonl was not written"

    records = [json.loads(l) for l in trace_path.read_text().splitlines()
               if l.strip()]
    assert len(records) == 5, f"expected 5 ticks, got {len(records)}"

    # Schema sanity — every record should have these fields, used by
    # tick_viewer.py and diagnose_shore_mask.py.
    required = {"tick", "wall_iso", "crop", "note", "phase", "action"}
    for rec in records:
        assert required.issubset(rec.keys()), (
            f"missing fields in trace record: {required - set(rec.keys())}")

    # The crop named in each record must exist on disk.
    for rec in records:
        crop = out_session / rec["crop"]
        assert crop.exists(), f"missing crop: {crop}"

    # Tick numbering monotonic + 1-indexed.
    assert [r["tick"] for r in records] == [1, 2, 3, 4, 5]

    # phase tag identifies this as an ai_nav-produced trace.
    assert all(r["phase"] == "AI_NAV" for r in records)


def test_resolve_source_file_spec(tmp_path):
    """Parser accepts file:<path> spec."""
    from tools.run_ai_nav_live import _resolve_source
    from brain.ai_nav.vision_input import FileVisionSource
    src = _resolve_source(f"file:{tmp_path}")
    assert isinstance(src, FileVisionSource)


def test_resolve_action_noop():
    from tools.run_ai_nav_live import _resolve_action_layer
    from brain.ai_nav import NoOpActionLayer
    a = _resolve_action_layer("noop")
    assert isinstance(a, NoOpActionLayer)


def test_resolve_unknown_specs_raise():
    from tools.run_ai_nav_live import _resolve_source, _resolve_action_layer
    with pytest.raises(ValueError):
        _resolve_source("invalid")
    with pytest.raises(ValueError):
        _resolve_action_layer("invalid")
