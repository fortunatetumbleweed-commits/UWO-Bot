"""Sim viewer — path-based, no borrowed frames.

The standard `tick_viewer.py` shows per-tick screenshots — fine for live
voyages where the image and the heading both come from the same frame.
For sim sessions that's misleading: the per-tick image is borrowed from
the reference voyage's nearest frame, but the sim's tracked heading is
whatever the goal commanded.  Viewers conflated the two.

This viewer instead shows:

  • Reference path  — drawn once at startup, gray.  The ground-truth
                       trajectory the human pilot took.
  • Sim path        — drawn progressively up to the current tick.
                       Colored.  Lets you watch where the sim went vs.
                       the reference, tick by tick.
  • Tick marker     — the current sim position, big colored dot.
  • Heading arrow   — small arrow at the marker showing sim's tracked
                       heading.
  • Info panel      — sim's tracked heading, perceived heading,
                       waypoint, action, phase, divergence.

Keys
  →   /  ↓     next tick
  ←   /  ↑     prev tick
  PgDn / PgUp  ±10 ticks
  Home / End   first / last tick
  g            type a tick number, jump there
  r            reset view (re-fit axes)
  q            quit

Usage
  python -m tools.sim_viewer data/sessions/sim_<name>
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Optional

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch


ROOT = Path(__file__).resolve().parent.parent
REF_DIR = ROOT / "data" / "reference"


def _load_jsonl(p: Path):
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _load_reference_path(session_name: str):
    """Load reference voyage's outbound (lat, lon) trajectory."""
    # Walk reference config files in data/reference/standard_*.json
    # for the one whose .session matches.  Cheap (one or two files).
    for cfg_path in REF_DIR.glob("standard_*.json"):
        cfg = json.loads(cfg_path.read_text())
        if cfg.get("session") == session_name:
            return cfg
    return None


def _find_region_for_sim(sim_dir: Path):
    """Sim session dir doesn't store the region name explicitly.  Best
    effort: try standard_nile first (only region today), fall back to
    asking the user via stdin."""
    cfg_path = REF_DIR / "standard_nile.json"
    if not cfg_path.exists():
        return None
    cfg = json.loads(cfg_path.read_text())
    ref_session = cfg["session"]
    ref_trace_path = ROOT / "data" / "sessions" / ref_session / "trace.jsonl"
    if not ref_trace_path.exists():
        return None
    ref_rows = []
    outbound = cfg.get("outbound_end_tick", 99999)
    for line in ref_trace_path.read_text().splitlines():
        if not line.strip(): continue
        d = json.loads(line)
        t = d.get("tick")
        if t is None or t > outbound: continue
        if d.get("lat") is None or d.get("lon") is None: continue
        ref_rows.append((d["lat"], d["lon"], t))
    return {"name": cfg["name"], "rows": ref_rows,
            "start": (ref_rows[0][0], ref_rows[0][1]),
            "end":   cfg.get("landmarks", [{}])[-1]}


class SimViewer:
    def __init__(self, sim_dir: Path):
        self.sim_dir = sim_dir
        self.trace = _load_jsonl(sim_dir / "trace.jsonl")
        self.div   = _load_jsonl(sim_dir / "divergence.jsonl")
        self.div_by_tick = {d["tick"]: d for d in self.div}
        if not self.trace:
            raise SystemExit(f"no trace.jsonl in {sim_dir}")
        self.ref = _find_region_for_sim(sim_dir)
        self.idx = 0
        self._build_ui()
        self._redraw()

    def _build_ui(self):
        self.fig = plt.figure(figsize=(14, 9))
        self.fig.canvas.manager.set_window_title(f"sim viewer — {self.sim_dir.name}")
        # Left: path plot.  Right: info panel.
        self.ax_path = self.fig.add_axes([0.05, 0.08, 0.6, 0.85])
        self.ax_info = self.fig.add_axes([0.68, 0.08, 0.30, 0.85])
        self.ax_info.axis("off")
        self._draw_reference()
        # Sim path artists — empty to start
        self.sim_line, = self.ax_path.plot(
            [], [], "-", color="tab:orange", linewidth=1.5,
            alpha=0.9, label="sim path")
        self.tick_dot = self.ax_path.scatter(
            [], [], color="tab:red", s=120, zorder=5, edgecolor="black",
            linewidth=1.0, label="current tick")
        self.hdg_arrow = None
        self.ax_path.legend(loc="upper right", fontsize=9)
        self.ax_path.set_xlabel("longitude")
        self.ax_path.set_ylabel("latitude")
        self.ax_path.grid(True, alpha=0.3)
        self.ax_path.set_aspect("equal", adjustable="datalim")

        self.info_txt = self.ax_info.text(
            0.0, 1.0, "", fontsize=11, fontfamily="monospace",
            verticalalignment="top", horizontalalignment="left",
            transform=self.ax_info.transAxes)
        self.fig.canvas.mpl_connect("key_press_event", self._on_key)

    def _draw_reference(self):
        if self.ref is None:
            self.ax_path.set_title(self.sim_dir.name + "  (reference path not found)")
            return
        rows = self.ref["rows"]
        self.ax_path.plot([lo for la, lo, _ in rows],
                          [la for la, lo, _ in rows],
                          color="lightgray", linewidth=2.5,
                          label=f"REFERENCE ({self.ref['name']})", zorder=1)
        # Start + end markers
        s = self.ref["start"]
        self.ax_path.scatter([s[1]], [s[0]], color="black", s=80,
                             marker="o", zorder=4, edgecolor="white", linewidth=1)
        # Set viewport to reference extents + small pad
        lats = [la for la, lo, _ in rows]
        lons = [lo for la, lo, _ in rows]
        pad_lat = (max(lats) - min(lats)) * 0.08
        pad_lon = (max(lons) - min(lons)) * 0.08
        self.ax_path.set_xlim(min(lons) - pad_lon, max(lons) + pad_lon)
        self.ax_path.set_ylim(min(lats) - pad_lat, max(lats) + pad_lat)

    def _redraw(self):
        rec = self.trace[self.idx]
        # Sim line up to current tick
        xs = [r["lon"] for r in self.trace[:self.idx + 1]]
        ys = [r["lat"] for r in self.trace[:self.idx + 1]]
        self.sim_line.set_data(xs, ys)
        # Current tick marker
        self.tick_dot.set_offsets([[rec["lon"], rec["lat"]]])
        # Heading arrow
        if self.hdg_arrow is not None:
            self.hdg_arrow.remove()
            self.hdg_arrow = None
        hdg = rec.get("heading_deg")
        if hdg is not None:
            arrow_len_deg = 0.5
            rad = math.radians(hdg)
            dlat = arrow_len_deg * math.cos(rad)
            cos_lat = max(math.cos(math.radians(rec["lat"])), 1e-6)
            dlon = arrow_len_deg * math.sin(rad) / cos_lat
            self.hdg_arrow = FancyArrowPatch(
                (rec["lon"], rec["lat"]),
                (rec["lon"] + dlon, rec["lat"] + dlat),
                arrowstyle="->", color="tab:red", linewidth=2.0,
                mutation_scale=18, zorder=6)
            self.ax_path.add_patch(self.hdg_arrow)
        # Info panel
        self.info_txt.set_text(self._format_info(rec))
        self.ax_path.set_title(
            f"sim t{rec['tick']}/{len(self.trace)}   "
            f"lat={rec['lat']:.3f}  lon={rec['lon']:.3f}  "
            f"hdg(tracked)={hdg:.1f}°", fontsize=11)
        self.fig.canvas.draw_idle()

    def _format_info(self, rec):
        lines = []
        lines.append(f"tick    {rec['tick']} / {len(self.trace)}")
        lines.append("")
        lines.append("POSITION")
        lines.append(f"  lat   {rec['lat']:>8.4f}")
        lines.append(f"  lon   {rec['lon']:>8.4f}")
        lines.append("")
        lines.append("HEADING")
        lines.append(f"  tracked    {rec.get('heading_deg', 0):>6.1f}°")
        raw = rec.get("heading_deg_raw")
        lines.append(f"  perceived  {raw:>6.1f}°" if raw is not None
                     else "  perceived    -")
        lines.append("")
        lines.append("DECISION")
        lines.append(f"  phase   {rec.get('phase')}")
        lines.append(f"  action  {rec.get('action')}")
        lyap = rec.get("lyapunov", {}) or {}
        if lyap.get("pp_waypoint"):
            wp = lyap["pp_waypoint"]
            lines.append(f"  pp_wp   ({wp[0]:.2f}, {wp[1]:.2f})")
        if lyap.get("desired_heading_deg") is not None:
            lines.append(f"  des_h   {lyap['desired_heading_deg']:.1f}°")
        # Divergence (if present)
        d = self.div_by_tick.get(rec["tick"])
        if d:
            lines.append("")
            lines.append("DIVERGENCE")
            rvp = d.get("ref_voyage_at_pos", {})
            lines.append(f"  ref tick      {rvp.get('ref_tick')}")
            if rvp.get("ref_heading") is not None:
                lines.append(f"  ref hdg       {rvp['ref_heading']:.1f}°")
            sdev = d.get("steering_dev_deg")
            if sdev is not None:
                lines.append(f"  steering dev  {sdev:>5.1f}°")
            a, s = d.get("active", {}), d.get("shadow", {})
            lines.append(f"  active mode   {d.get('active_mode')}")
            if a.get("waypoint"):
                lines.append(f"    wp          ({a['waypoint'][0]:.2f}, {a['waypoint'][1]:.2f})")
            if a.get("bearing_deg") is not None:
                lines.append(f"    bearing     {a['bearing_deg']:.1f}°")
            if a.get("none_reason"):
                lines.append(f"    None: {a['none_reason']}")
            lines.append(f"  shadow mode   {d.get('shadow_mode')}")
            if s.get("waypoint"):
                lines.append(f"    wp          ({s['waypoint'][0]:.2f}, {s['waypoint'][1]:.2f})")
            wp_dev = d.get("wp_dev_km_active_vs_shadow")
            if wp_dev is not None:
                lines.append(f"  wp dev (km)   {wp_dev:>5.1f}")
        lines.append("")
        lines.append("KEYS")
        lines.append("  → ↓ next     ← ↑ prev")
        lines.append("  PgDn/Up ±10  Home/End")
        lines.append("  g jump  r reset  q quit")
        return "\n".join(lines)

    def _on_key(self, event):
        k = event.key
        n = len(self.trace)
        if k in ("right", "down"):
            self.idx = min(self.idx + 1, n - 1)
        elif k in ("left", "up"):
            self.idx = max(self.idx - 1, 0)
        elif k == "pagedown":
            self.idx = min(self.idx + 10, n - 1)
        elif k == "pageup":
            self.idx = max(self.idx - 10, 0)
        elif k == "home":
            self.idx = 0
        elif k == "end":
            self.idx = n - 1
        elif k == "g":
            try:
                v = input("jump to tick: ").strip()
                target = int(v)
                # Find the row whose tick == target
                for i, r in enumerate(self.trace):
                    if r["tick"] == target:
                        self.idx = i
                        break
            except (ValueError, KeyboardInterrupt, EOFError):
                pass
        elif k == "r":
            self._draw_reference()
        elif k == "q":
            plt.close(self.fig); return
        else:
            return
        self._redraw()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("session_dir", type=Path)
    args = ap.parse_args()
    SimViewer(args.session_dir)
    plt.show()


if __name__ == "__main__":
    main()
