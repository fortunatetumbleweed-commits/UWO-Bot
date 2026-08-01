"""Rotational template matcher with separate axis/direction confidence.

Adapted from `ships/ship_reconstruct/reconstruct.py` — the standalone
tool that hit 1.9° mean error on the hard-case set and correctly flags
information-limited fragments (t082/t083) instead of guessing.

Algorithm:
  1. Segment ship pixels with a strict green rule.
  2. Rotate the canonical template through 0..359° (3° coarse + 1°
     refine), translate over ±R pixels, score
     `coverage × fill^0.3` (visible-explained × template-filled).
  3. Report best (angle, shift) along with:
       - axis_confidence: robust even under heavy occlusion because
         L/R symmetry doesn't carry direction information.
       - dir_confidence:  margin over the 180° flip; drops when the
         bow/stern asymmetry is invisible.

The template must be pre-built (bow-up, L/R folded).  See
`build_template()` for how to construct one from clean synth crops.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

_CANVAS = 80
_CENTER = (40.0, 40.0)


# ---------- segmentation ----------

def _ship_mask(rgb: np.ndarray, loose: bool = False) -> np.ndarray:
    """Bright saturated ship-green (rejects duller grass-green).

    Matches the extract_ship_fragments.py rule that produced the
    ground-truth extractions our template was built against.
    """
    r = rgb[..., 0].astype(np.float32)
    g = rgb[..., 1].astype(np.float32)
    b = rgb[..., 2].astype(np.float32)
    if loose:
        return (g > 90) & (g > r + 30) & (g > b + 30)
    return (g > 110) & (g - r > 60) & (g - b > 50)


# ---------- template build (offline) ----------

def build_template(synth_dir: Path) -> Image.Image:
    """Canonical ship alpha at heading 0 from de-rotated clean synth
    images.  Left/right folded to remove sampling noise.

    Only used to (re)generate `data/reference/ship_template_v11.png`
    when the clean synth pool changes.  Runtime code loads the saved
    template directly.
    """
    import glob
    import re
    files = sorted(glob.glob(str(synth_dir / "synth_*truth*.png")))
    if not files:
        raise RuntimeError(f"no synth_*truth*.png found in {synth_dir}")
    stack = []
    for f in files:
        t = int(re.search(r"truth(\d+)", f).group(1))
        rgb = np.asarray(Image.open(f).convert("RGB")).astype(np.float32)
        mask = _ship_mask(rgb, loose=True).astype(np.float32)
        rgba = np.dstack([rgb, mask * 255]).astype(np.uint8)
        der = Image.fromarray(rgba, "RGBA").rotate(
            +t, resample=Image.BICUBIC, center=_CENTER)
        stack.append(np.asarray(der)[..., 3].astype(np.float32))
    alpha = np.median(np.stack(stack), axis=0)
    alpha = np.maximum(alpha, np.fliplr(alpha))
    alpha = (alpha > 60).astype(np.uint8) * 255
    return Image.fromarray(alpha)


# ---------- rendering ----------

def _render(template_L: Image.Image, angle_deg: float) -> np.ndarray:
    rot = template_L.rotate(-angle_deg % 360, resample=Image.BICUBIC,
                            center=_CENTER)
    return (np.asarray(rot).astype(np.float32) / 255.0 > 0.5).astype(np.float32)


# ---------- matching ----------

def _best_shift_score(S: np.ndarray, T: np.ndarray, R: int
                      ) -> tuple[float, int, int]:
    """Best integer shift in ±R.  Returns (score, dy, dx).

    Score = coverage × fill^0.3.  Coverage rewards explaining more of
    the visible ship; fill (with a mild 0.3 exponent) prevents the
    optimizer from stretching a tiny template over a huge fragment.
    """
    n_vis = float(S.sum())
    if n_vis == 0:
        return 0.0, 0, 0
    best = (-1.0, 0, 0)
    for dy in range(-R, R + 1):
        Ty = np.roll(T, dy, axis=0)
        for dx in range(-R, R + 1):
            Ts = np.roll(Ty, dx, axis=1)
            inter = float((S * Ts).sum())
            if inter == 0:
                continue
            cov = inter / n_vis
            fill = inter / float(Ts.sum())
            sc = cov * (fill ** 0.3)
            if sc > best[0]:
                best = (sc, dy, dx)
    return best


def _circ_err(a: float, b: float, mod: float = 360.0) -> float:
    return abs((a - b + mod / 2) % mod - mod / 2)


@dataclass
class MatchResult:
    angle_deg: float                # 0 = up/north, CW
    axis_deg: float                 # angle_deg mod 180
    shift: tuple[int, int]          # (dy, dx) after best-fit registration
    score: float                    # cov × fill^0.3 at best pose
    visible_px: int                 # ship-mask pixel count
    axis_confidence: float          # 0..1
    dir_confidence: float           # 0..1

    @property
    def confidence(self) -> float:
        """Overall = need BOTH axis AND direction."""
        return round(self.axis_confidence * self.dir_confidence, 3)


def _margin(best: float, competitor: float, threshold: float) -> float:
    return min(max((best - competitor) / max(best, 1e-6) / threshold, 0.0), 1.0)


class ShipTemplateMatcher:
    """Rotational template matcher, loaded once, called per tick.

    Constructor loads `template.png` (bow-up canonical, L/R folded).
    Call `estimate(rgb_80x80)` per frame.

    Latency: ~40-80ms on a modern CPU at coarse=3° + 1° refine and
    R=10 pixel shift search.  Dominant cost is the shift loop
    (441 shifts × 120 coarse angles = ~53k mask multiplies).
    """

    def __init__(self, template_path: Optional[Path] = None):
        if template_path is None:
            template_path = (Path(__file__).resolve().parents[3]
                             / "data/reference/ship_template_v11.png")
        self._template = Image.open(template_path).convert("L")
        # Pre-rotate the coarse angles once, so per-tick estimate only
        # needs to reload from cache.  ~1.2 MB total.
        self._coarse_step = 3
        self._coarse_cache = {
            a: _render(self._template, a)
            for a in range(0, 360, self._coarse_step)
        }

    def estimate(self, rgb_80x80: np.ndarray, shift_radius: int = 10
                 ) -> MatchResult:
        """Estimate heading + confidence from an 80×80 RGB minimap crop
        centered on the ship."""
        S = _ship_mask(rgb_80x80).astype(np.float32)
        n_vis = int(S.sum())

        scores: dict[int, float] = {}
        best = (-1.0, 0, 0, 0)   # (score, angle, dy, dx)
        for a, T in self._coarse_cache.items():
            sc, dy, dx = _best_shift_score(S, T, shift_radius)
            scores[a] = sc
            if sc > best[0]:
                best = (sc, a, dy, dx)

        # 1° refine around the coarse peak
        for a in range(best[1] - self._coarse_step,
                       best[1] + self._coarse_step + 1):
            a_mod = a % 360
            sc, dy, dx = _best_shift_score(
                S, _render(self._template, a_mod), shift_radius)
            if sc > best[0]:
                best = (sc, a_mod, dy, dx)

        score, angle, dy, dx = best
        angle = float(angle)

        # Competitor pointing the *opposite* way but same axis
        # (i.e. bow/stern flip risk).
        flip_score = max(
            (s for a2, s in scores.items() if _circ_err(a2, angle) >= 150),
            default=0.0,
        )
        # Competitor on a genuinely different axis
        # (i.e. axis ambiguity itself).
        def axis_diff(a2: int) -> float:
            return abs((a2 - angle + 90) % 180 - 90)
        cross_score = max(
            (s for a2, s in scores.items() if axis_diff(a2) >= 40),
            default=0.0,
        )

        # Axis: robust to occlusion because L/R symmetry carries no
        # direction info.  Needs less signal than direction.
        vis_term = min(n_vis / 90.0, 1.0)
        fit_term = min(score / 0.6, 1.0)
        axis_conf = float(vis_term * fit_term
                          * _margin(score, cross_score, 0.15))

        # Direction: margin over the 180° flip.  Small margin = fragment
        # doesn't distinguish bow from stern.
        dir_conf = float(_margin(score, flip_score, 0.15))

        return MatchResult(
            angle_deg=angle,
            axis_deg=angle % 180,
            shift=(int(dy), int(dx)),
            score=float(score),
            visible_px=n_vis,
            axis_confidence=round(axis_conf, 3),
            dir_confidence=round(dir_conf, 3),
        )


# ---------- worker-pool interface ----------
#
# ProcessPoolExecutor gives true parallelism (bypasses the GIL) so the
# matcher's ~630ms cost overlaps with the CNN's ~2ms per tick.  Each
# worker loads the template once via `_worker_init` and reuses it for
# every submission.

_WORKER_MATCHER: Optional[ShipTemplateMatcher] = None


def _worker_init(template_path: Optional[str] = None) -> None:
    """Executor initializer — load the matcher once per worker."""
    global _WORKER_MATCHER
    tpl = Path(template_path) if template_path else None
    _WORKER_MATCHER = ShipTemplateMatcher(template_path=tpl)


def _worker_estimate(rgb: np.ndarray) -> dict:
    """Executor task — return a plain dict so pickling doesn't require
    importing MatchResult in the worker's parent namespace."""
    global _WORKER_MATCHER
    if _WORKER_MATCHER is None:
        _worker_init(None)
    r = _WORKER_MATCHER.estimate(rgb)
    return {
        "angle_deg": r.angle_deg,
        "axis_deg": r.axis_deg,
        "shift": r.shift,
        "score": r.score,
        "visible_px": r.visible_px,
        "axis_confidence": r.axis_confidence,
        "dir_confidence": r.dir_confidence,
        "confidence": r.confidence,
    }
