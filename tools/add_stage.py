"""Add a captured frame to the stage suite, so a stage can be checked without a live run.

    python -m tools.add_stage <stage_name> <path/to/frame.png> ["what the bot should do here"]

COPIES the frame into tests/stage_suite/frames/ rather than pointing at it. Run traces under
data/sessions/ are working artifacts — they get pruned, renamed, and regenerated — and a suite
that referenced them would rot silently and take its evidence with it. The suite owns its
frames (user, 2026-08-26: "kept and will not be affected or forgotten").

Perception costs ~60s a frame here, because a frame that no fingerprint settles falls through
to the Qwen pass. That is far too slow for a test, so the reading is taken ONCE, now, and
stored beside the frame. The fast suite asserts DECISIONS against the stored reading; the slow
opt-in pass re-perceives and checks the reading itself still holds.
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SUITE = ROOT / "tests" / "stage_suite"


def add(stage: str, frame_path: str, expect: str = "") -> None:
    src = Path(frame_path)
    if not src.is_absolute():
        src = ROOT / src
    if not src.exists():
        raise SystemExit(f"no such frame: {src}")

    dest = SUITE / "frames" / f"{stage}.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)

    from brain.perceive import perceive
    from PIL import Image
    loc = perceive(Image.open(dest)).to_location_dict()

    where, detail = loc.get("location"), (loc.get("detail") or "")
    if where in ("building", "sub_menu") and ":" in detail:
        name = detail.split(":", 1)[1].strip().split(" — ", 1)[0].strip().lower()
        if name:
            where = f"{where}:{name}"

    (SUITE / "perceived").mkdir(parents=True, exist_ok=True)
    (SUITE / "perceived" / f"{stage}.json").write_text(
        json.dumps({"state": where, "port": loc.get("port"), "detail": detail,
                    "raw": loc}, indent=1, default=str))

    manifest = SUITE / "stages.json"
    stages = json.loads(manifest.read_text()) if manifest.exists() else {}
    stages[stage] = {"frame": f"frames/{stage}.png",
                     "state": where,
                     "port": loc.get("port"),
                     "expect": expect,
                     "from": str(src.relative_to(ROOT)) if str(src).startswith(str(ROOT))
                             else str(src)}
    manifest.write_text(json.dumps(dict(sorted(stages.items())), indent=1))

    print(f"{stage}\n  state : {where}\n  port  : {loc.get('port')}\n"
          f"  detail: {detail[:90]}\n  from  : {src.name}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    add(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
