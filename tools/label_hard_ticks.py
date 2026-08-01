"""Interactive GUI labeling tool for hard perception cases.

Shows one minimap frame at a time with a compass overlay so you can
read off the ship's true heading visually.  Type the angle (or a
compass shortcut) in the text field, click Save-Next or press Enter,
and it advances to the next tick.

Compass overlay:
  - concentric circles at fixed radii around the ship centroid
  - tick marks every 10°
  - labels at N (0°), NE (45°), E (90°), SE (135°), S (180°),
    SW (225°), W (270°), NW (315°)

Saves labels to a jsonl for the sprite extractor to pick up as an
additional source alongside the mining-derived labels.

Usage
─────
  python -m tools.label_hard_ticks \\
      --spec "ai_nav_2026-06-30T19-32-28:405,406,407,408,409,410" \\
      --spec "ai_nav_2026-06-30T15-47-03:474,478,479,480,481,482" \\
      --output data/heading_labels_human/v5_dead_end.jsonl
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageTk

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


COMPASS = {
    "N": 0.0, "NE": 45.0, "E": 90.0, "SE": 135.0,
    "S": 180.0, "SW": 225.0, "W": 270.0, "NW": 315.0,
    "NNE": 22.5, "ENE": 67.5, "ESE": 112.5, "SSE": 157.5,
    "SSW": 202.5, "WSW": 247.5, "WNW": 292.5, "NNW": 337.5,
}


def _green_centroid(rgb):
    import numpy as np
    R, G, B = (rgb[..., c].astype(int) for c in range(3))
    mask = (G > 140) & (G - R > 30) & (G - B > 30)
    ys, xs = np.where(mask)
    if len(ys) < 5:
        return rgb.shape[0] // 2, rgb.shape[1] // 2
    return int(ys.mean()), int(xs.mean())


def _draw_compass(img: Image.Image, cy: int, cx: int,
                  hint_cnn_deg: float | None,
                  hint_motion_deg: float | None) -> Image.Image:
    """Draw a compass overlay centered on (cy, cx).  Two rings of
    tick marks (major every 30°, minor every 10°) + cardinal labels
    + optional hint arrows for CNN and motion bearing."""
    out = img.convert("RGB").copy()
    draw = ImageDraw.Draw(out, "RGBA")

    W, H = out.size
    r_outer = min(W, H) // 2 - 15
    r_minor = r_outer - 12
    r_major = r_outer - 22
    r_label = r_outer - 40

    # Draw the ring
    draw.ellipse(
        [cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer],
        outline=(255, 255, 255, 180), width=2,
    )

    # Ticks
    for deg in range(0, 360, 10):
        th = math.radians(deg)
        # 0° = N = up in image coords
        dx, dy = math.sin(th), -math.cos(th)
        is_major = (deg % 30 == 0)
        r1 = r_outer
        r2 = r_major if is_major else r_minor
        x1, y1 = cx + r1 * dx, cy + r1 * dy
        x2, y2 = cx + r2 * dx, cy + r2 * dy
        color = ((255, 255, 255, 220) if is_major
                 else (255, 255, 255, 140))
        draw.line([(x1, y1), (x2, y2)], fill=color,
                  width=2 if is_major else 1)

    # Cardinal + intercardinal labels
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 16)
        font_small = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 12)
    except (OSError, IOError):
        font = ImageFont.load_default()
        font_small = font
    labels = [(0, "N"), (45, "NE"), (90, "E"), (135, "SE"),
              (180, "S"), (225, "SW"), (270, "W"), (315, "NW")]
    for deg, txt in labels:
        th = math.radians(deg)
        dx, dy = math.sin(th), -math.cos(th)
        lx, ly = cx + r_label * dx, cy + r_label * dy
        bbox = draw.textbbox((0, 0), txt, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        # Yellow background chip for readability
        draw.rectangle(
            [lx - w // 2 - 2, ly - h // 2 - 2,
             lx + w // 2 + 2, ly + h // 2 + 2],
            fill=(0, 0, 0, 150),
        )
        draw.text((lx - w // 2, ly - h // 2), txt,
                  fill=(255, 255, 0, 255), font=font)

    # Degree numbers every 30°
    for deg in (30, 60, 120, 150, 210, 240, 300, 330):
        th = math.radians(deg)
        dx, dy = math.sin(th), -math.cos(th)
        lx, ly = cx + r_label * dx, cy + r_label * dy
        txt = f"{deg}"
        bbox = draw.textbbox((0, 0), txt, font=font_small)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.rectangle(
            [lx - w // 2 - 1, ly - h // 2 - 1,
             lx + w // 2 + 1, ly + h // 2 + 1],
            fill=(0, 0, 0, 130),
        )
        draw.text((lx - w // 2, ly - h // 2), txt,
                  fill=(200, 200, 200, 220), font=font_small)

    # Hint arrows
    def _arrow(deg, color, length_frac=0.85):
        th = math.radians(deg)
        dx, dy = math.sin(th), -math.cos(th)
        L = r_outer * length_frac
        x2, y2 = cx + L * dx, cy + L * dy
        draw.line([(cx, cy), (x2, y2)], fill=color, width=3)
        # arrowhead
        for sign in (-1, 1):
            ah = th + sign * math.radians(25) + math.pi
            ax = x2 + 12 * math.sin(ah)
            ay = y2 - 12 * math.cos(ah)
            draw.line([(x2, y2), (int(ax), int(ay))], fill=color, width=3)

    if hint_cnn_deg is not None:
        _arrow(hint_cnn_deg, (0, 220, 255, 255), 0.85)
    if hint_motion_deg is not None:
        _arrow(hint_motion_deg, (255, 100, 255, 255), 0.65)

    return out


class LabelerApp:
    def __init__(self, targets, out_path):
        self.targets = targets
        self.out_path = out_path
        self.idx = 0
        self.done = self._load_done()
        self.pending_label = None

        self.root = tk.Tk()
        self.root.title("Hard-tick heading labeler")

        # Layout: top = image, bottom = controls
        self.img_label = tk.Label(self.root, bg="black")
        self.img_label.grid(row=0, column=0, columnspan=5, padx=8, pady=8)

        self.info = tk.Label(self.root, text="", font=("Menlo", 12),
                             fg="black", justify="left", anchor="w")
        self.info.grid(row=1, column=0, columnspan=5, sticky="ew",
                       padx=8, pady=(0, 4))

        tk.Label(self.root, text="Heading (deg or N/NE/S/…):",
                 font=("Menlo", 12)).grid(row=2, column=0, padx=8,
                                           pady=(0, 8), sticky="e")
        self.entry = tk.Entry(self.root, font=("Menlo", 14), width=10)
        self.entry.grid(row=2, column=1, sticky="w", pady=(0, 8))
        self.entry.bind("<Return>", lambda e: self._save_and_next())

        tk.Button(self.root, text="Save & Next  (⏎)",
                  command=self._save_and_next).grid(
            row=2, column=2, padx=4, pady=(0, 8))
        tk.Button(self.root, text="Skip", command=self._skip).grid(
            row=2, column=3, padx=4, pady=(0, 8))
        tk.Button(self.root, text="Quit", command=self.root.destroy).grid(
            row=2, column=4, padx=8, pady=(0, 8))

        self._advance_to_undone()
        if self.idx < len(self.targets):
            self._render_current()
            self.entry.focus_set()

    def _load_done(self):
        done = set()
        if self.out_path.exists():
            for line in self.out_path.open():
                r = json.loads(line)
                done.add((r["source_session"], r["source_tick"]))
        return done

    def _advance_to_undone(self):
        while (self.idx < len(self.targets)
               and self.targets[self.idx] in self.done):
            self.idx += 1

    def _current(self):
        return self.targets[self.idx]

    def _render_current(self):
        import numpy as np
        sess, tick = self._current()
        fp = REPO / "data" / "sessions" / sess / f"tick_{tick:04d}.png"
        if not fp.exists():
            self._skip()
            return

        img = Image.open(fp).convert("RGB")
        # Scale up 3x
        scale = 3
        img_big = img.resize((img.width * scale, img.height * scale),
                             Image.NEAREST)
        # Find ship centroid in the scaled image
        rgb = np.asarray(img_big)
        cy, cx = _green_centroid(rgb)

        # Hints from trace
        cnn_deg = motion_deg = None
        hint = f"[{self.idx+1}/{len(self.targets)}]  {sess}  t{tick}"
        trace_p = REPO / "data" / "sessions" / sess / "trace.jsonl"
        if trace_p.exists():
            prev = None
            for line in trace_p.open():
                r = json.loads(line)
                if r.get("tick") == tick - 1:
                    prev = r
                if r.get("tick") == tick:
                    cnn_deg = r.get("heading_deg")
                    hint += (f"    cnn={cnn_deg}° "
                             f"conf={r.get('heading_conf',0):.2f}   "
                             f"speed={r.get('speed_kt',0):.1f} kt   "
                             f"lat={r.get('lat'):.2f} lon={r.get('lon'):.2f}")
                    # Motion bearing
                    if prev:
                        dlat = (r.get('lat', 0) - prev.get('lat', r.get('lat', 0)))
                        dlon = (r.get('lon', 0) - prev.get('lon', r.get('lon', 0)))
                        if abs(dlat) > 1e-5 or abs(dlon) > 1e-5:
                            motion_deg = (math.degrees(math.atan2(dlon, dlat))
                                          + 360) % 360
                            hint += f"   motion={motion_deg:.0f}°"
                    break

        annotated = _draw_compass(img_big, cy, cx, cnn_deg, motion_deg)
        self._tk_img = ImageTk.PhotoImage(annotated)
        self.img_label.config(image=self._tk_img)
        self.info.config(text=hint + "\n"
                         "cyan arrow = CNN's guess    magenta arrow = motion bearing (unreliable near bounces)")
        self.entry.delete(0, tk.END)

    def _save_and_next(self):
        val = self.entry.get().strip().upper()
        if not val:
            return
        if val in COMPASS:
            hdg = COMPASS[val]
        else:
            try:
                hdg = float(val) % 360.0
            except ValueError:
                self.info.config(text=self.info.cget("text")
                                 + f"\n[unparsed input '{val}']")
                return
        sess, tick = self._current()
        with self.out_path.open("a") as f:
            f.write(json.dumps({
                "source_session": sess,
                "source_tick": tick,
                "heading_deg": round(hdg, 1),
                "source": "human_v5",
            }) + "\n")
        self.done.add((sess, tick))
        self._next()

    def _skip(self):
        self._next()

    def _next(self):
        self.idx += 1
        self._advance_to_undone()
        if self.idx >= len(self.targets):
            self.info.config(text=f"Done — {len(self.done)} labels in {self.out_path}")
            self.img_label.config(image="")
            return
        self._render_current()
        self.entry.focus_set()

    def run(self):
        self.root.mainloop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", action="append", required=True,
                    help="'session_name:t1,t2,t3'  (may repeat)")
    ap.add_argument("--output", required=True,
                    help="jsonl to append labels to")
    args = ap.parse_args()

    targets = []
    for spec in args.spec:
        sess, ticks_csv = spec.split(":", 1)
        for t in ticks_csv.split(","):
            targets.append((sess, int(t.strip())))

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    app = LabelerApp(targets, out)
    app.run()


if __name__ == "__main__":
    main()
