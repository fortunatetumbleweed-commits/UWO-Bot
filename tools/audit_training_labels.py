"""Interactive audit of training-sprite labels.

For each training sprite in the v4 snapshot, compares the labeled
heading against PCA and motion bearing at the source tick.
Presents each flagged sprite in a GUI window with the same compass
overlay as label_hard_ticks so you can eyeball which reading is
right — the training label, PCA, or neither.

Saves a jsonl of your verdicts for each inspected sprite.  Each
record includes the corrected heading so a retrain can use it:
  - "accept":   label is correct                       (corrected = label)
  - "flip":     label is 180° off                      (corrected = label + 180°)
  - "motion":   cyan motion bearing is correct         (corrected = motion_deg)
  - "pca":      magenta PCA is correct                 (corrected = pca_deg)
  - "manual":   type a custom heading                  (corrected = user input)
  - "skip":     too ambiguous, don't touch             (no corrected)

Usage
─────
  python -m tools.audit_training_labels \\
      --output data/heading_labels_human/label_audit_v4.jsonl \\
      --threshold 150      # only show sprites with |label-pca| >= 150°
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tkinter as tk
from tkinter import simpledialog
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageTk

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SNAPSHOT = REPO / "data" / "heading_training_snapshot_v4_2026-06-30"


def _compass_overlay(img: Image.Image, cy: int, cx: int,
                     label_deg: float, pca_deg: float | None,
                     motion_deg: float | None) -> Image.Image:
    out = img.convert("RGB").copy()
    draw = ImageDraw.Draw(out, "RGBA")
    W, H = out.size
    r = min(W, H) // 2 - 15

    # Compass ring + ticks
    draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                 outline=(255, 255, 255, 180), width=2)
    for deg in range(0, 360, 30):
        th = math.radians(deg)
        dx, dy = math.sin(th), -math.cos(th)
        draw.line([(cx + r * dx, cy + r * dy),
                   (cx + (r - 18) * dx, cy + (r - 18) * dy)],
                  fill=(255, 255, 255, 220), width=2)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 12)
    except (OSError, IOError):
        font = ImageFont.load_default()
    for deg, txt in [(0,"N"),(90,"E"),(180,"S"),(270,"W")]:
        th = math.radians(deg)
        dx, dy = math.sin(th), -math.cos(th)
        lx, ly = cx + (r - 32) * dx, cy + (r - 32) * dy
        draw.text((lx - 6, ly - 7), txt, fill=(255, 255, 0, 255), font=font)

    def _arrow(deg, color, length_frac=0.85, offset=(0, 0)):
        th = math.radians(deg)
        dx, dy = math.sin(th), -math.cos(th)
        L = r * length_frac
        ox, oy = offset
        x1, y1 = cx + ox, cy + oy
        x2, y2 = x1 + L * dx, y1 + L * dy
        draw.line([(x1, y1), (x2, y2)], fill=color, width=3)
        # arrowhead
        for sign in (-1, 1):
            ah = th + sign * math.radians(25) + math.pi
            ax = x2 + 12 * math.sin(ah)
            ay = y2 - 12 * math.cos(ah)
            draw.line([(x2, y2), (int(ax), int(ay))], fill=color, width=3)

    # Green = training label (the one under audit)
    _arrow(label_deg, (100, 255, 100, 255), 0.9, (0, 0))
    # Magenta = PCA (the challenger)
    if pca_deg is not None:
        _arrow(pca_deg, (255, 100, 255, 255), 0.7, (3, 3))
    # Cyan = motion bearing (independent signal)
    if motion_deg is not None:
        _arrow(motion_deg, (100, 200, 255, 255), 0.55, (-3, 3))

    return out


class AuditApp:
    def __init__(self, cases, out_path):
        self.cases = cases
        self.out_path = out_path
        self.idx = 0
        self.done = self._load_done()

        # Perception cache
        self._perception_cache = None

        self.root = tk.Tk()
        self.root.title("Training-label audit — flag 180° flips")

        self.img_label = tk.Label(self.root, bg="black")
        self.img_label.grid(row=0, column=0, columnspan=6, padx=8, pady=8)

        self.info = tk.Label(self.root, text="",
                             font=("Menlo", 12), fg="black",
                             justify="left", anchor="w")
        self.info.grid(row=1, column=0, columnspan=6,
                       sticky="ew", padx=8, pady=(0, 4))

        col = 0
        tk.Button(self.root, text="◀ Back  (b)",
                  command=self._back).grid(
            row=2, column=col, padx=4, pady=(0, 8))
        col += 1
        tk.Button(self.root, text="ACCEPT green  (a)",
                  bg="#c8ffc8",
                  command=lambda: self._decide("accept")).grid(
            row=2, column=col, padx=4, pady=(0, 8))
        col += 1
        tk.Button(self.root, text="FLIP 180°  (f)",
                  bg="#ffe0c8",
                  command=lambda: self._decide("flip")).grid(
            row=2, column=col, padx=4, pady=(0, 8))
        col += 1
        tk.Button(self.root, text="MOTION is right (cyan)  (c)",
                  bg="#c8e6ff",
                  command=lambda: self._decide("motion")).grid(
            row=2, column=col, padx=4, pady=(0, 8))
        col += 1
        tk.Button(self.root, text="PCA is right (magenta)  (m)",
                  bg="#ffc8f0",
                  command=lambda: self._decide("pca")).grid(
            row=2, column=col, padx=4, pady=(0, 8))
        col += 1
        tk.Button(self.root, text="Manual…  (t)",
                  command=lambda: self._decide("manual")).grid(
            row=2, column=col, padx=4, pady=(0, 8))
        col += 1
        tk.Button(self.root, text="Skip",
                  command=self._skip).grid(
            row=2, column=col, padx=4, pady=(0, 8))
        col += 1
        tk.Button(self.root, text="Quit",
                  command=self.root.destroy).grid(
            row=2, column=col, padx=8, pady=(0, 8))

        # Keyboard shortcuts
        self.root.bind("a", lambda e: self._decide("accept"))
        self.root.bind("f", lambda e: self._decide("flip"))
        self.root.bind("c", lambda e: self._decide("motion"))
        self.root.bind("m", lambda e: self._decide("pca"))
        self.root.bind("t", lambda e: self._decide("manual"))
        self.root.bind("<space>", lambda e: self._skip())
        self.root.bind("b", lambda e: self._back())
        self.root.bind("<BackSpace>", lambda e: self._back())
        self.root.bind("q", lambda e: self.root.destroy())

        # Undo stack: each entry = (idx_before_action, sprite_id_or_None,
        # wrote_line_to_file: bool). Only supports back within this
        # session, not across restarts.
        self._history: list[tuple[int, str | None, bool]] = []

        # Cache of the readings drawn for the current case
        self._cur_pca_deg = None
        self._cur_motion_deg = None

        self._advance()

    def _get_pca(self):
        if self._perception_cache is False:
            return None
        if self._perception_cache is None:
            try:
                from brain.ai_nav.layers.heading import PCAHeading
                self._perception_cache = PCAHeading()
            except Exception:
                self._perception_cache = False
                return None
        return self._perception_cache

    def _load_done(self):
        done = set()
        if self.out_path.exists():
            for line in self.out_path.open():
                r = json.loads(line)
                done.add(r["sprite_id"])
        print(f"resuming — {len(done)} sprite(s) already audited in "
              f"{self.out_path}")
        return done

    def _advance(self):
        while self.idx < len(self.cases):
            case = self.cases[self.idx]
            if case["sprite_id"] not in self.done:
                self._render_current()
                return
            self.idx += 1
        # Done
        self.info.config(text=f"Audit complete — {len(self.done)} verdicts "
                         f"in {self.out_path}")
        self.img_label.config(image="")

    def _render_current(self):
        case = self.cases[self.idx]
        fp = REPO / "data" / "sessions" / case["source_session"] / f"tick_{case['source_tick']:04d}.png"
        if not fp.exists():
            self._skip()
            return
        img = Image.open(fp).convert("RGB")
        # scale 3x
        img_big = img.resize((img.width * 3, img.height * 3),
                             Image.NEAREST)
        rgb = np.asarray(img_big)
        # ship centroid
        R, G, B = (rgb[..., c].astype(int) for c in range(3))
        mask = (G > 140) & (G - R > 30) & (G - B > 30)
        ys, xs = np.where(mask)
        if len(ys) > 5:
            cy, cx = int(ys.mean()), int(xs.mean())
        else:
            cy, cx = rgb.shape[0] // 2, rgb.shape[1] // 2

        # PCA live
        pca_deg = None
        pca_model = self._get_pca()
        if pca_model:
            try:
                class _F:
                    def __init__(self, mm): self._mm = mm
                    def minimap(self): return self._mm
                    def full_screen(self): return self._mm
                h = pca_model.estimate(_F(img), prior=None)
                if h.confidence > 0:
                    pca_deg = h.bearing_deg
            except Exception:
                pass

        # Motion bearing from adjacent trace ticks
        motion_deg = None
        trace_p = REPO / "data" / "sessions" / case["source_session"] / "trace.jsonl"
        if trace_p.exists():
            prev = curr = None
            for line in trace_p.open():
                r = json.loads(line)
                t = r.get("tick")
                if t == case["source_tick"] - 1:
                    prev = r
                if t == case["source_tick"]:
                    curr = r; break
            if prev and curr and curr.get("lat") and curr.get("lon"):
                dlat = curr["lat"] - prev.get("lat", curr["lat"])
                dlon = curr["lon"] - prev.get("lon", curr["lon"])
                if abs(dlat) > 1e-5 or abs(dlon) > 1e-5:
                    motion_deg = (math.degrees(math.atan2(dlon, dlat))
                                  + 360) % 360

        self._cur_pca_deg = pca_deg
        self._cur_motion_deg = motion_deg
        overlaid = _compass_overlay(img_big, cy, cx,
                                    case["heading_deg"], pca_deg,
                                    motion_deg)
        self._tk_img = ImageTk.PhotoImage(overlaid)
        self.img_label.config(image=self._tk_img)
        pca_s = f"{pca_deg:.0f}°" if pca_deg is not None else "?"
        mot_s = f"{motion_deg:.0f}°" if motion_deg is not None else "?"
        diff_pca = (abs(((case["heading_deg"] - pca_deg + 540) % 360) - 180)
                    if pca_deg is not None else "?")
        diff_pca_s = f"{diff_pca:.0f}°" if diff_pca != "?" else "?"
        self.info.config(text=(
            f"[{self.idx+1}/{len(self.cases)}]  sprite={case['sprite_id']}  "
            f"{case['source_session']} t{case['source_tick']}\n"
            f"GREEN = label {case['heading_deg']:.0f}°     "
            f"MAGENTA = PCA {pca_s}     "
            f"CYAN = motion bearing {mot_s}     "
            f"|label - pca| = {diff_pca_s}"
        ))

    def _decide(self, verdict: str):
        if self.idx >= len(self.cases):
            return
        case = self.cases[self.idx]

        corrected = None
        if verdict == "accept":
            corrected = case["heading_deg"]
        elif verdict == "flip":
            corrected = (case["heading_deg"] + 180.0) % 360.0
        elif verdict == "motion":
            if self._cur_motion_deg is None:
                self.info.config(text=self.info.cget("text")
                                 + "  ← motion bearing unavailable, "
                                 "cannot record 'motion' verdict")
                return
            corrected = self._cur_motion_deg
        elif verdict == "pca":
            if self._cur_pca_deg is None:
                self.info.config(text=self.info.cget("text")
                                 + "  ← PCA unavailable, "
                                 "cannot record 'pca' verdict")
                return
            corrected = self._cur_pca_deg
        elif verdict == "manual":
            val = simpledialog.askfloat(
                "Manual heading",
                f"Correct heading (0–360°) for sprite "
                f"{case['sprite_id']}?",
                parent=self.root,
                minvalue=0.0, maxvalue=360.0)
            if val is None:
                return  # cancelled — don't record
            corrected = val % 360.0

        with self.out_path.open("a") as f:
            f.write(json.dumps({
                "sprite_id": case["sprite_id"],
                "source_session": case["source_session"],
                "source_tick": case["source_tick"],
                "label_heading_deg": case["heading_deg"],
                "pca_heading_deg": self._cur_pca_deg,
                "motion_bearing_deg": self._cur_motion_deg,
                "verdict": verdict,
                "corrected_heading_deg": corrected,
            }) + "\n")
        self.done.add(case["sprite_id"])
        self._history.append((self.idx, case["sprite_id"], True))
        self.idx += 1
        self._advance()

    def _skip(self):
        self._history.append((self.idx, None, False))
        self.idx += 1
        self._advance()

    def _back(self):
        if not self._history:
            self.info.config(text=self.info.cget("text")
                             + "  ← nothing to undo")
            return
        prev_idx, sprite_id, wrote_line = self._history.pop()
        if wrote_line and sprite_id is not None:
            # Strip the last line from the jsonl. Verdicts are appended
            # in order, so the tail line corresponds to this undo.
            lines = self.out_path.read_text().splitlines(keepends=True)
            if lines and sprite_id in lines[-1]:
                self.out_path.write_text("".join(lines[:-1]))
            self.done.discard(sprite_id)
        self.idx = prev_idx
        self._render_current()

    def run(self):
        self.root.mainloop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True,
                    help="jsonl to append verdicts to")
    ap.add_argument("--threshold", type=float, default=150.0,
                    help="only show sprites where |label - pca| >= this "
                         "(ignored if --suspects is given)")
    ap.add_argument("--suspects", type=str, default=None,
                    help="path to jsonl of pre-flagged sprites (from the "
                         "scan_polluted_sprites tool); each line must "
                         "have a 'sprite_id' field.  When set, replaces "
                         "the PCA-disagreement filter.")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap number of cases (for a quick pass)")
    args = ap.parse_args()

    sys.path.insert(0, str(REPO))
    manifest = [json.loads(l) for l in
                (SNAPSHOT / "sprites" / "manifest.jsonl").open()]
    print(f"total sprites in snapshot: {len(manifest)}")
    by_id = {r["sprite_id"]: r for r in manifest}

    if args.suspects:
        # Load pre-flagged suspect IDs — no PCA scan needed.
        cases = []
        for line in Path(args.suspects).open():
            r = json.loads(line)
            rec = by_id.get(r["sprite_id"])
            if rec is not None:
                cases.append(rec)
        print(f"loaded {len(cases)} suspect sprite(s) from {args.suspects}")
    else:
        print("scanning all sprites for label vs PCA disagreement...")
        from brain.ai_nav.layers.heading import PCAHeading
        from brain.ai_nav.vision_input import FileVisionSource
        pca = PCAHeading()
        cases = []
        checked = 0
        for rec in manifest:
            try:
                src = FileVisionSource(f"data/sessions/{rec['source_session']}")
                fr = src.capture(rec['source_tick'])
                h = pca.estimate(fr, prior=None)
                checked += 1
                if h.confidence == 0:
                    continue
                diff = abs(((rec['heading_deg'] - h.bearing_deg + 540)
                            % 360) - 180)
                if diff >= args.threshold:
                    cases.append(rec)
            except Exception:
                continue
        print(f"scanned: {checked}  |  flagged: {len(cases)} "
              f"(|label-pca| >= {args.threshold}°)")

    if args.limit:
        cases = cases[:args.limit]

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    if not cases:
        print("no flagged cases — nothing to review.")
        return

    print(f"launching viewer for {len(cases)} cases")
    print("keys: a=accept, f=flip (label is 180° off), o=other, "
          "space=skip, q=quit")
    app = AuditApp(cases, out)
    app.run()


if __name__ == "__main__":
    main()
