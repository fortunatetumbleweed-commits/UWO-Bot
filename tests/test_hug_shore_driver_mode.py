"""Phase 2 — A/B switch tests for `HugShoreGoal.driver_mode`.

Covers:
1. Mode validation (bad value rejected)
2. Env-var resolution (UWO_HUG_SHORE_DRIVER fills in when caller
   passes None; constructor arg wins when explicit)
3. Default fallback (no env, no arg → HUG_SHORE_DRIVER_DEFAULT)

These tests don't exercise the policy itself — `test_hug_shore_
regression.py` covers VFH+ behaviour and the live runs validate
Lyapunov + lyapunov_safe.
"""
from __future__ import annotations

import os

import pytest

from brain.goals.hug_shore import (
    HugShoreGoal,
    HUG_SHORE_DRIVER_DEFAULT,
    HUG_SHORE_DRIVER_ENV,
    HUG_SHORE_DRIVER_MODES,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(HUG_SHORE_DRIVER_ENV, raising=False)


def test_default_mode_when_no_env_no_arg():
    g = HugShoreGoal(side="starboard")
    assert g.driver_mode == HUG_SHORE_DRIVER_DEFAULT


def test_env_var_fills_in_when_arg_is_none(monkeypatch):
    monkeypatch.setenv(HUG_SHORE_DRIVER_ENV, "vfh")
    g = HugShoreGoal(side="starboard")
    assert g.driver_mode == "vfh"


def test_constructor_arg_beats_env_var(monkeypatch):
    # Explicit construction must NOT be overridden by env — protects
    # the regression tests from CI/local shell pollution.
    monkeypatch.setenv(HUG_SHORE_DRIVER_ENV, "lyapunov")
    g = HugShoreGoal(side="starboard", driver_mode="vfh")
    assert g.driver_mode == "vfh"


def test_invalid_mode_rejected_via_arg():
    with pytest.raises(ValueError, match="driver_mode"):
        HugShoreGoal(side="starboard", driver_mode="nonsense")


def test_invalid_mode_rejected_via_env(monkeypatch):
    monkeypatch.setenv(HUG_SHORE_DRIVER_ENV, "nonsense")
    with pytest.raises(ValueError, match="driver_mode"):
        HugShoreGoal(side="starboard")


@pytest.mark.parametrize("mode", HUG_SHORE_DRIVER_MODES)
def test_all_listed_modes_construct(mode):
    g = HugShoreGoal(side="starboard", driver_mode=mode)
    assert g.driver_mode == mode
