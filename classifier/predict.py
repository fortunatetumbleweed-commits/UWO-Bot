# classifier/predict.py
# Screen type inference — load the trained model once, call predict() per frame.
#
# Standalone usage (classify a single image):
#   python -m classifier.predict path/to/screenshot.png
#
# Live usage (classify latest ADB screenshot):
#   python -m classifier.predict --live
#   python -m classifier.predict --live --watch   # continuous, 1s interval
#
# API usage (import into bot):
#   from classifier.predict import ScreenClassifier
#   clf = ScreenClassifier()               # loads model once
#   result = clf.predict("screen.png")
#   print(result.screen_type, result.confidence)

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import torch
from PIL import Image
from torchvision import models, transforms

# ── paths ─────────────────────────────────────────────────────────────────────

_ROOT       = Path(__file__).parent.parent
_MODELS_DIR = _ROOT / "models" / "screen_classifier"

# ── image transform (must match train.py) ─────────────────────────────────────

_TRANSFORM = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406],
                         std=[0.229, 0.224, 0.225]),
])


# ── result type ───────────────────────────────────────────────────────────────

@dataclass
class Prediction:
    screen_type: str          # e.g. "port_overworld"
    confidence:  float        # 0.0 – 1.0 (softmax probability of top class)
    top3: list[tuple[str, float]]  # [(class_id, prob), ...] top 3 candidates


# ── classifier ────────────────────────────────────────────────────────────────

class ScreenClassifier:
    """
    Thin wrapper around the trained MobileNetV3-small checkpoint.
    Load once, call predict() repeatedly — model stays in memory.
    """

    def __init__(self, model_dir: Path = _MODELS_DIR, device: str | None = None):
        model_dir = Path(model_dir)

        # Load class names
        names_path = model_dir / "class_names.json"
        if not names_path.exists():
            raise FileNotFoundError(
                f"class_names.json not found at {names_path}\n"
                "Run `python -m classifier.train` first."
            )
        with open(names_path) as f:
            raw = json.load(f)
        # raw is { "0": "port_overworld", "1": "port_map", ... }
        self.class_names: list[str] = [raw[str(i)] for i in range(len(raw))]
        num_classes = len(self.class_names)

        # Device selection
        if device is None:
            if torch.backends.mps.is_available():
                device = "mps"
            elif torch.cuda.is_available():
                device = "cuda"
            else:
                device = "cpu"
        self.device = torch.device(device)

        # Build model and load weights
        self._model = models.mobilenet_v3_small(weights=None)
        in_features = self._model.classifier[-1].in_features
        self._model.classifier[-1] = torch.nn.Linear(in_features, num_classes)
        self._model.load_state_dict(
            torch.load(model_dir / "model.pt",
                       map_location=self.device,
                       weights_only=True)
        )
        self._model.to(self.device)
        self._model.eval()

    def predict(self, image: str | Path | Image.Image) -> Prediction:
        """
        Classify a screenshot.

        Args:
            image: file path (str/Path) or a PIL Image already loaded.

        Returns:
            Prediction with screen_type, confidence, and top3 list.
        """
        if not isinstance(image, Image.Image):
            image = Image.open(image).convert("RGB")
        else:
            image = image.convert("RGB")

        tensor = _TRANSFORM(image).unsqueeze(0).to(self.device)

        with torch.no_grad():
            logits = self._model(tensor)
            probs  = torch.softmax(logits, dim=1)[0].cpu()

        top3_idx  = probs.topk(min(3, len(self.class_names))).indices.tolist()
        top3      = [(self.class_names[i], round(probs[i].item(), 4)) for i in top3_idx]
        best_idx  = top3_idx[0]

        return Prediction(
            screen_type=self.class_names[best_idx],
            confidence=round(probs[best_idx].item(), 4),
            top3=top3,
        )

    def predict_adb(self) -> Prediction:
        """Capture a screenshot via ADB and classify it."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp = Path(f.name)
        try:
            subprocess.run(
                ["adb", "exec-out", "screencap", "-p"],
                stdout=open(tmp, "wb"),
                check=True,
                timeout=10,
            )
            return self.predict(tmp)
        finally:
            tmp.unlink(missing_ok=True)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _fmt(pred: Prediction, path: str = "") -> str:
    bar = "█" * int(pred.confidence * 20)
    label = f"{pred.screen_type:<26} {pred.confidence:.1%}  {bar}"
    if path:
        label = f"{Path(path).name:<35}  {label}"
    alts = "  alts: " + "  ".join(f"{c} {p:.1%}" for c, p in pred.top3[1:])
    return label + "\n" + (" " * (len(path) + 2 if path else 0)) + alts


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Classify UWO screenshots")
    parser.add_argument("images", nargs="*", help="Screenshot file(s) to classify")
    parser.add_argument("--live",  action="store_true",
                        help="Capture via ADB and classify once")
    parser.add_argument("--watch", action="store_true",
                        help="With --live: repeat every second until Ctrl+C")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="Seconds between captures in --watch mode (default 1.0)")
    parser.add_argument("--device", default=None,
                        help="Force device: cpu / cuda / mps")
    args = parser.parse_args()

    clf = ScreenClassifier(device=args.device)
    print(f"Model loaded  ·  {len(clf.class_names)} classes  ·  device={clf.device}\n")

    if args.live:
        if args.watch:
            print("Watching ADB (Ctrl+C to stop)…\n")
            try:
                while True:
                    pred = clf.predict_adb()
                    ts = time.strftime("%H:%M:%S")
                    print(f"[{ts}]  {_fmt(pred)}")
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\nStopped.")
        else:
            pred = clf.predict_adb()
            print(_fmt(pred))

    elif args.images:
        for path in args.images:
            try:
                pred = clf.predict(path)
                print(_fmt(pred, path))
            except Exception as e:
                print(f"{path}: ERROR — {e}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
