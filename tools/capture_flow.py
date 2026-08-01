# tools/capture_flow.py
# Interactive screenshot capture for transaction flow recording.
#
# Usage:
#   python tools/capture_flow.py market_sell
#   python tools/capture_flow.py market_buy
#   python tools/capture_flow.py --dir /path/to/existing/screenshots  market_sell
#
# The script captures one screenshot per keypress. After capturing all steps,
# it runs the flow analyzer to produce a structured JSON flow definition that
# the bot can follow when executing that transaction type.
#
# Workflow:
#   1. Start the script
#   2. Perform the transaction on your phone, step by step
#   3. Press Enter after each distinct screen appears
#   4. Type a short label for each step when prompted (optional)
#   5. Press Ctrl+C or type 'done' when the transaction is complete
#   6. The analyzer runs automatically and saves the flow to
#      memory/knowledge/flows/<flow_name>/flow.json

import sys
import os
import time
from pathlib import Path
from typing import Optional

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.logger import setup_logging
setup_logging()

from loguru import logger
from capture.adb_capture import capture_screen


def capture_flow(flow_name: str, output_dir: Optional[Path] = None) -> Path:
    """
    Interactively capture screenshots for a named transaction flow.
    Returns the directory containing the captured screenshots.
    """
    if output_dir is None:
        output_dir = Path("memory/knowledge/flows") / flow_name / "screenshots"
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = output_dir.parent / "manifest.txt"

    print(f"\n{'='*60}")
    print(f"  Capturing flow: {flow_name!r}")
    print(f"  Screenshots → {output_dir}")
    print(f"{'='*60}")
    print("\nInstructions:")
    print("  • Perform the transaction on your phone, one screen at a time")
    print("  • Press Enter after each new screen appears to capture it")
    print("  • Optionally type a short label before pressing Enter")
    print("  • Type 'done' and press Enter when finished")
    print("  • Press Ctrl+C to abort\n")

    steps: list[tuple[str, str]] = []  # (filename, label)
    step_n = 0

    while True:
        try:
            raw = input(f"Step {step_n:02d} — label (or Enter to skip, 'done' to finish): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            break

        if raw.lower() == "done":
            break

        label = raw or f"step_{step_n:02d}"

        filename = f"step_{step_n:03d}.png"
        filepath = output_dir / filename

        try:
            frame = capture_screen()
            frame.save(filepath)
            steps.append((filename, label))
            logger.info(f"  Captured: {filename}  ({label})")
            step_n += 1
        except Exception as exc:
            logger.error(f"Capture failed: {exc}")

    if not steps:
        logger.warning("No screenshots captured — exiting")
        return output_dir

    # Write manifest
    with open(manifest_path, "w") as f:
        f.write(f"flow: {flow_name}\n")
        f.write(f"captured: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        for fname, label in steps:
            f.write(f"{fname}\t{label}\n")

    logger.info(f"\nCaptured {len(steps)} steps → {output_dir}")
    logger.info(f"Manifest → {manifest_path}")
    return output_dir


if __name__ == "__main__":
    args = sys.argv[1:]

    # Parse --dir override
    output_dir = None
    if "--dir" in args:
        idx = args.index("--dir")
        output_dir = Path(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    if not args:
        print("Usage: python tools/capture_flow.py <flow_name> [--dir <path>]")
        print("Examples:")
        print("  python tools/capture_flow.py market_sell")
        print("  python tools/capture_flow.py market_buy")
        print("  python tools/capture_flow.py market_negotiate")
        sys.exit(1)

    flow_name = args[0]
    screenshots_dir = capture_flow(flow_name, output_dir)

    # Auto-run analyzer
    print(f"\nRunning flow analyzer on {screenshots_dir}…")
    from tools.analyze_flow import analyze_flow
    flow_path = analyze_flow(flow_name, screenshots_dir)
    if flow_path:
        print(f"\nFlow saved → {flow_path}")
        print("The bot will use this flow when executing market transactions.")
