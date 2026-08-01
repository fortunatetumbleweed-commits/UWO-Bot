"""Scenario-based regression tests for LookaheadTactical.

Each scenario runs the tactical layer against a real session's tick
images (real mask geometry) but with a specified initial commit_deg
(desired direction).  Tests the WP behavior, flip counts, and edge
tracking through a specific tricky river geometry.

Scenarios cover:
  - cairo_start: wide-water departure, start point can drift far
  - nubia_bend: sticky-anchor stress at a river bend where bot edge
    run disappears mid-tick
  - (future) y_fork, y_tip_deadend, lake

Run all scenarios via pytest:
    pytest tests/test_tactical_scenarios.py

Run a single scenario interactively (with per-tick output):
    python -m tools.run_tactical_scenario nubia_bend
"""
