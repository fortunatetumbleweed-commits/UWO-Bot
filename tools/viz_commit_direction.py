"""Visualize commit_direction history from one or more ai_nav sessions.

Renders a chart per session showing commit_direction.bearing_deg
over tick number, with each change event marked + annotated by
reason.  Beneath each chart, a strip of minimap thumbnails at the
change-event ticks shows the actual frame perception was looking
at.  Visual count of marker dots = number of rotations; thumbnails
let you see whether the rotation was justified.

Pass multiple session dirs to stack them in one figure for A/B
comparison.

Usage
─────
  # one session
  python -m tools.viz_commit_direction /tmp/ai_nav_dryrun_v3

  # A/B compare two sessions
  python -m tools.viz_commit_direction \\
      /tmp/ai_nav_dryrun_v2 /tmp/ai_nav_dryrun_v3

  # Cap thumbnails per session (default 12 — long parked stretches
  # would otherwise spawn dozens of identical thumbs)
  python -m tools.viz_commit_direction <sess> --max-thumbs 8
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
from PIL import Image


# Color per reason — keep stable across runs.
REASON_COLOR = {
    "init_from_heading": "#3366ff",
    "planner_dead_end_safety_net": "#ff5500",
    "tactical(channel)": "#33aa33",
    "tactical(junction)": "#33aa33",
    "tactical(dead_end)": "#33aa33",
    "tactical(lake)": "#33aa33",
}
DEFAULT_COLOR = "#888888"


def _load_trace(session_dir: Path) -> list[dict]:
    p = session_dir / "trace.jsonl" if session_dir.is_dir() else session_dir
    if not p.exists():
        sys.exit(f"no trace at {p}")
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _changes(records: list[dict]) -> list[dict]:
    out = []
    prev_set = None
    for r in records:
        if r.get("commit_set_at") != prev_set and r.get("commit_set_at") is not None:
            out.append(r)
            prev_set = r["commit_set_at"]
    return out


def _session_dir_for_trace(p: Path) -> Path:
    return p if p.is_dir() else p.parent


def _find_tick_image(session_dir: Path, tick: int,
                     crop_name: str | None) -> Path | None:
    if crop_name:
        cand = session_dir / crop_name
        if cand.exists():
            return cand
    # Fall back to legacy 4-digit name.
    cand = session_dir / f"tick_{tick:04d}.png"
    return cand if cand.exists() else None


def _sample_evenly(items: list, k: int) -> list:
    """Pick k items evenly spaced, always keeping first + last."""
    if len(items) <= k:
        return items
    idxs = np.linspace(0, len(items) - 1, k).round().astype(int)
    return [items[i] for i in idxs]


def render_chart(records: list[dict], ax: plt.Axes, label: str,
                 changes: list[dict]):
    ticks = [r["tick"] for r in records]
    commits = [r.get("commit_deg") for r in records]
    headings = [r.get("heading_deg") for r in records]

    ax.plot(ticks, headings, color="#bbbbbb", linewidth=0.8, alpha=0.7,
            label="heading (PCA)", zorder=1)
    ax.plot(ticks, commits, color="#222244", linewidth=1.6,
            label="commit_direction", zorder=2)

    for r in changes:
        color = REASON_COLOR.get(r.get("commit_reason"), DEFAULT_COLOR)
        ax.scatter([r["tick"]], [r["commit_deg"]], s=70,
                   c=color, edgecolors="black", linewidths=0.7,
                   zorder=4)

    for i, r in enumerate(changes):
        y_off = 18 if (i % 2 == 0) else -22
        color = REASON_COLOR.get(r.get("commit_reason"), DEFAULT_COLOR)
        ax.annotate(
            f"t{r['tick']}\n{r['commit_reason']}",
            xy=(r["tick"], r["commit_deg"]),
            xytext=(0, y_off), textcoords="offset points",
            ha="center", fontsize=7, color=color,
            arrowprops=dict(arrowstyle="-", color=color, lw=0.6, alpha=0.6),
        )

    ax.set_ylim(-10, 380)
    ax.set_xlim(min(ticks) - 5, max(ticks) + 5)
    ax.set_ylabel("bearing (°)", fontsize=9)
    ax.set_xlabel("tick", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=8, framealpha=0.85)

    reason_counts = Counter(r.get("commit_reason") for r in changes)
    summary = " | ".join(f"{r}={n}" for r, n in reason_counts.most_common())
    ax.set_title(f"{label}   ({len(changes)} changes: {summary})",
                 fontsize=10)


def render_thumbnails(records: list[dict], thumb_axes: list[plt.Axes],
                      changes: list[dict], session_dir: Path,
                      max_thumbs: int):
    """Render minimap thumbnails for change-event ticks into the
    provided axes.  If there are more changes than thumb_axes, the
    change list is sampled evenly."""
    sampled = _sample_evenly(changes, len(thumb_axes))
    # Build a quick lookup from tick -> record (for crop name).
    by_tick = {r["tick"]: r for r in records}

    for ax in thumb_axes:
        ax.set_xticks([]); ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)

    for ax, change in zip(thumb_axes, sampled):
        tick = change["tick"]
        rec = by_tick.get(tick, change)
        path = _find_tick_image(session_dir, tick, rec.get("crop"))
        if path is None:
            ax.text(0.5, 0.5, f"t{tick}\n(no img)",
                    ha="center", va="center", fontsize=7, color="#888")
            continue
        img = np.asarray(Image.open(path).convert("RGB"))
        ax.imshow(img)
        color = REASON_COLOR.get(change.get("commit_reason"), DEFAULT_COLOR)
        ax.set_title(
            f"t{tick}\n{change.get('commit_deg'):.0f}°",
            fontsize=7, color=color, pad=2,
        )
        # Thin colored border matching the reason color.
        for spine in ax.spines.values():
            spine.set_visible(True)
            spine.set_color(color)
            spine.set_linewidth(1.2)

    # If there are fewer changes than slots, hide the extras.
    for ax in thumb_axes[len(sampled):]:
        ax.set_visible(False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--max-thumbs", type=int, default=12,
                   help="Max thumbnails per session.  Long parked "
                        "stretches with dozens of identical frames "
                        "get sampled to this cap.")
    args = ap.parse_args()

    n_sessions = len(args.sessions)
    n_cols = max(1, args.max_thumbs)

    # Per session: chart row + thumbnail row.  Width scales with
    # thumbnail count (thumbnails are 400×190 minimap-aspect → wide).
    # Height per session is generous so the thumbnails come out
    # readable.
    fig_w = max(14, 1.6 * n_cols)
    fig_h = 6.5 * n_sessions
    fig = plt.figure(figsize=(fig_w, fig_h))
    gs = gridspec.GridSpec(
        2 * n_sessions, n_cols,
        height_ratios=[3, 2.4] * n_sessions,
        figure=fig, hspace=0.45, wspace=0.18,
    )

    for i, sess in enumerate(args.sessions):
        records = _load_trace(sess)
        changes = _changes(records)
        sess_dir = _session_dir_for_trace(sess)

        chart_ax = fig.add_subplot(gs[2 * i, :])
        render_chart(records, chart_ax, label=str(sess), changes=changes)

        thumb_axes = [fig.add_subplot(gs[2 * i + 1, j])
                      for j in range(n_cols)]
        render_thumbnails(records, thumb_axes, changes, sess_dir,
                          max_thumbs=n_cols)

    out_path = args.out
    if out_path is None:
        first = args.sessions[0]
        anchor = first if first.is_dir() else first.parent
        out_path = anchor / "commit_direction.png" if anchor.exists() \
                   else Path("/tmp/commit_direction.png")

    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"→ {out_path}")


if __name__ == "__main__":
    main()
