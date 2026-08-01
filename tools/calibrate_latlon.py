#!/usr/bin/env python
"""Interactive lat/lon ↔ catalogue calibration.

Goal: derive a one-time affine transform between the game's displayed
lat/lon coordinates and the catalogue (game_x, game_y) coordinates that
`port_coordinates.json` and `village_coordinates.json` use.

The transform lets the bot:
  • Tap any point on water → read (lat, lon) → convert to catalogue
    coords → know exactly where the screen-centre is, with no
    dependence on visible port labels.
  • Plan navigation to arbitrary water points, not just catalogued
    ports/villages.

Workflow (run with the game's world map open, scrcpy mirror in view):

    python tools/calibrate_latlon.py

  1. The script prints a port name to tap near (cycles through a
     diverse set: Lisbon, Tripoli, Port Royal, Calicut, …).
  2. You tap on water adjacent to that port in scrcpy.  The game shows
     the yellow diamond + lat/lon readout + Move button.
  3. Press Enter in the terminal.
  4. The script captures the frame, OCRs the readout, prints what it
     sees, and asks you to confirm.
  5. After ≥3 samples, the script fits an affine transform and writes
     `memory/knowledge/world_map/latlon_to_catalogue.json`.
  6. With ≥4 samples, leave-one-out cross-validation prints residuals
     per port so you can spot a bad catalogue coord.

You can quit early with Ctrl-D or by typing `done`.  Re-run any time
to add more samples — the script appends if a sample file already
exists.

The script never taps anything itself.  All taps are done by you in
scrcpy.  This avoids the chicken-and-egg of needing navigation to
calibrate navigation.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

# Project root on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np

from capture.adb_capture import capture_screen
from actions.water_tap import read_latlon_from_frame
from vision.world_map_parser import load_port_catalogue
from actions.sail_actions import _PORT_ALIASES


_OUTPUT_PATH = Path("memory/knowledge/world_map/latlon_to_catalogue.json")
_SAMPLES_PATH = Path("memory/knowledge/world_map/latlon_samples.json")
_DEBUG_DIR = Path("/tmp/uwo_latlon_debug")


# Suggested calibration points spread across the world map.  Pick ports
# the player can plausibly reach during normal play.  Order matters
# only as a suggestion — the user can choose any catalogued port.
# Use canonical catalogue slugs so the "already sampled?" skip check
# matches.  English variants are still accepted at the prompt via
# _resolve_port's alias table.
_SUGGESTED = [
    "lisboa",         # West Iberia
    "tripoli",        # Central Mediterranean
    "alexandria",     # East Mediterranean
    "port royal",     # Caribbean
    "kozhikode",      # Indian Ocean (a.k.a. Calicut)
    "amsterdam",      # North Sea
]


def _load_samples() -> list:
    if _SAMPLES_PATH.exists():
        return json.loads(_SAMPLES_PATH.read_text())
    return []


def _save_samples(samples: list) -> None:
    _SAMPLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SAMPLES_PATH.write_text(json.dumps(samples, indent=2))


def _resolve_port(catalogue: dict, name: str) -> Optional[dict]:
    """Find a port by case-insensitive name / slug / alias match."""
    key = name.strip().lower()
    if key in catalogue:
        entry = dict(catalogue[key])
        entry["slug"] = key
        return entry
    # Alias: user types "lisbon", catalogue stores "lisboa".
    # _PORT_ALIASES maps canonical → variants; the input may be either.
    for canon, variants in _PORT_ALIASES.items():
        candidates = {canon.lower(), *(v.lower() for v in variants)}
        if key in candidates:
            for c in candidates:
                if c in catalogue:
                    entry = dict(catalogue[c])
                    entry["slug"] = c
                    return entry
    # Loose match: try contains on slug or display name.
    for k, v in catalogue.items():
        if key in k or key in (v.get("name") or "").lower():
            entry = dict(v)
            entry["slug"] = k
            return entry
    return None


def _fit_affine(samples: list) -> dict:
    """Fit catalogue_xy = A @ (lon, lat, 1).

    Returns a dict {a, b, c, d, e, f} where:
        catalogue_x = a*lon + b*lat + c
        catalogue_y = d*lon + e*lat + f
    Also returns residuals per sample.
    """
    # Design matrix: rows = [lon, lat, 1].
    M = np.array([[s["lon"], s["lat"], 1.0] for s in samples])
    bx = np.array([s["cat_x"] for s in samples])
    by = np.array([s["cat_y"] for s in samples])
    # Least squares.
    coeff_x, *_ = np.linalg.lstsq(M, bx, rcond=None)
    coeff_y, *_ = np.linalg.lstsq(M, by, rcond=None)
    a, b, c = coeff_x.tolist()
    d, e, f = coeff_y.tolist()
    # Residuals per sample.
    residuals = []
    for s in samples:
        pred_x = a * s["lon"] + b * s["lat"] + c
        pred_y = d * s["lon"] + e * s["lat"] + f
        residuals.append({
            "port": s["port"],
            "actual": [s["cat_x"], s["cat_y"]],
            "predicted": [round(pred_x, 1), round(pred_y, 1)],
            "error_px": round(((pred_x - s["cat_x"])**2 + (pred_y - s["cat_y"])**2) ** 0.5, 1),
        })
    return {
        "transform": {"a": a, "b": b, "c": c, "d": d, "e": e, "f": f},
        "n_samples": len(samples),
        "residuals": residuals,
        "rms_error_px": round(
            (sum(r["error_px"] ** 2 for r in residuals) / len(residuals)) ** 0.5, 2
        ),
    }


def _loo_cross_validate(samples: list) -> list:
    """Leave-one-out: refit without each sample, predict it, report
    error.  A sample with much larger error than the others is the
    likely outlier (bad catalogue coord, or a mistaken port label).
    """
    if len(samples) < 4:
        return []
    out = []
    for i, held in enumerate(samples):
        train = samples[:i] + samples[i+1:]
        fit = _fit_affine(train)
        t = fit["transform"]
        pred_x = t["a"]*held["lon"] + t["b"]*held["lat"] + t["c"]
        pred_y = t["d"]*held["lon"] + t["e"]*held["lat"] + t["f"]
        err = ((pred_x - held["cat_x"])**2 + (pred_y - held["cat_y"])**2) ** 0.5
        out.append({"port": held["port"], "loo_error_px": round(err, 1)})
    return out


def _capture_one_sample(catalogue: dict, suggested_port: str) -> Optional[dict]:
    """Walk the user through one calibration sample.  Returns None if
    they choose to skip or the OCR didn't yield a readout.
    """
    print()
    print(f"  ▶  Tap on water near port: {suggested_port!r}")
    print(f"     (or any other catalogued port — you'll confirm the name next)")
    print(f"     Once you see the yellow diamond + Move button + coords on screen,")
    print(f"     press Enter here.  Type 'skip' to move on, 'done' to finish, Ctrl-D to abort.")
    try:
        ans = input("     > ").strip().lower()
    except EOFError:
        return "DONE"
    if ans in ("done", "quit", "exit"):
        return "DONE"
    if ans in ("skip", "s"):
        return None

    frame = capture_screen()
    latlon = read_latlon_from_frame(frame, debug_dir=_DEBUG_DIR)
    if latlon is None:
        print(f"     ✗ no lat/lon readout detected.  Debug crop saved to {_DEBUG_DIR}.")
        print(f"       Either the tap was on land, or the crop region needs tuning.")
        return None
    lat, lon = latlon
    print(f"     ✓ read: lat={lat}, lon={lon}")

    # Confirm port name.
    while True:
        try:
            port_input = input(f"     Which port was nearest? [{suggested_port}] > ").strip()
        except EOFError:
            return "DONE"
        port_input = port_input or suggested_port
        info = _resolve_port(catalogue, port_input)
        if info is None:
            print(f"     ✗ {port_input!r} not in catalogue.  Try again, or type a partial match.")
            continue
        print(f"     ✓ matched: {info['slug']} → catalogue ({info['x']}, {info['y']})")
        return {
            "port": info["slug"],
            "lat": lat,
            "lon": lon,
            "cat_x": info["x"],
            "cat_y": info["y"],
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }


def main() -> int:
    print(f"=== UWO lat/lon ↔ catalogue calibration ===")
    print(f"Output: {_OUTPUT_PATH}")
    print(f"Samples file: {_SAMPLES_PATH}")
    print(f"Debug crops: {_DEBUG_DIR}")
    print()
    print(f"Have the world map OPEN in scrcpy before continuing.")
    print(f"You'll tap on water near 3-6 different ports; I capture + OCR the")
    print(f"readout, then fit an affine transform.")
    print()

    catalogue = load_port_catalogue()
    samples = _load_samples()
    if samples:
        print(f"Found {len(samples)} existing sample(s); will append.")
        for s in samples:
            print(f"  - {s['port']:14s} lat={s['lat']:7.2f} lon={s['lon']:7.2f} → ({s['cat_x']}, {s['cat_y']})")

    # Skip suggestions we already have.
    seen = {s["port"] for s in samples}
    pending = [p for p in _SUGGESTED if p not in seen]

    for suggested in pending:
        result = _capture_one_sample(catalogue, suggested)
        if result == "DONE":
            break
        if result is not None:
            samples.append(result)
            _save_samples(samples)

    # If user wants more after suggestions exhausted, allow free entries.
    while True:
        try:
            ans = input("Add another sample? [y/N] > ").strip().lower()
        except EOFError:
            break
        if ans not in ("y", "yes"):
            break
        result = _capture_one_sample(catalogue, "(any port)")
        if result == "DONE":
            break
        if result is not None:
            samples.append(result)
            _save_samples(samples)

    if len(samples) < 3:
        print()
        print(f"Need ≥3 samples for a 2D affine fit.  Got {len(samples)}.")
        print(f"Samples saved to {_SAMPLES_PATH}; re-run to collect more.")
        return 1

    print()
    print(f"Fitting affine transform on {len(samples)} sample(s)…")
    fit = _fit_affine(samples)
    print()
    print(f"  Transform:")
    print(f"    cat_x = {fit['transform']['a']:.4f}*lon + {fit['transform']['b']:.4f}*lat + {fit['transform']['c']:.2f}")
    print(f"    cat_y = {fit['transform']['d']:.4f}*lon + {fit['transform']['e']:.4f}*lat + {fit['transform']['f']:.2f}")
    print()
    print(f"  Per-sample residuals:")
    for r in fit["residuals"]:
        print(f"    {r['port']:14s} predicted={r['predicted']} actual={r['actual']} err={r['error_px']} px")
    print()
    print(f"  RMS error: {fit['rms_error_px']} catalogue-px")
    if fit["rms_error_px"] > 200:
        print(f"  ⚠ RMS > 200 px — the projection may not be a simple affine, or one")
        print(f"     sample is mislabelled.  Inspect the residuals above.")

    loo = _loo_cross_validate(samples)
    if loo:
        print()
        print(f"  Leave-one-out cross-validation:")
        for r in loo:
            flag = "  ⚠" if r["loo_error_px"] > 300 else "   "
            print(f"  {flag} {r['port']:14s} held-out error = {r['loo_error_px']} px")

    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_PATH.write_text(json.dumps({
        **fit,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, indent=2))
    print()
    print(f"✓ Wrote {_OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
