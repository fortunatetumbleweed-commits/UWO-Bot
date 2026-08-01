# main.py
# Entry point — initialises all subsystems and runs the main bot loop.
#
# Each tick:
#   1. Capture screen from device
#   2. Classify current screen (trained neural classifier)
#   3a. Known screen  → update game state, let FSM decide and act
#   3b. Unknown screen → pause, ask user to label it, save discovery, idle

from __future__ import annotations

import time

from capture.adb_capture import capture_screen
from classifier.predict import ScreenClassifier
from memory.screen_discovery import handle_unknown_screen, list_unimplemented, is_implemented
from state.game_state import GameState
from brain.fsm import BotFSM
from memory.logger import setup_logging
from config.settings import CAPTURE_FPS

from loguru import logger

# Minimum classifier confidence to treat a prediction as "known".
# Below this the screen is handled as unknown.
_CLASSIFIER_MIN_CONFIDENCE = 0.70


def _report_unimplemented() -> None:
    """At startup, log any pending discovery records so the dev knows what to implement."""
    pending = list_unimplemented()
    if pending:
        logger.warning(f"{len(pending)} unimplemented screen(s) in memory/discoveries/:")
        for rec in pending:
            logger.warning(f"  [{rec['id']}] {rec['label']} — {rec['description']}")


def main() -> None:
    setup_logging()
    logger.info("UWO Bot starting up")
    _report_unimplemented()

    game_state = GameState()
    bot = BotFSM(game_state)
    classifier = ScreenClassifier()
    logger.info(f"Screen classifier loaded ({len(classifier.class_names)} classes)")

    tick_interval = 1.0 / CAPTURE_FPS

    while True:
        loop_start = time.monotonic()

        # 1. Capture
        frame = capture_screen()

        # 2. Classify screen
        pred = classifier.predict(frame)
        if pred.confidence >= _CLASSIFIER_MIN_CONFIDENCE:
            screen = pred.screen_type
            logger.debug(
                f"[tick {game_state.tick}] screen={screen!r} "
                f"({pred.confidence:.0%})  alts={pred.top3[1:]}"
            )
        else:
            screen = None
            logger.debug(
                f"[tick {game_state.tick}] classifier low confidence "
                f"{pred.confidence:.0%} for {pred.screen_type!r} — treating as unknown"
            )
        game_state.current_screen = screen

        if screen is None:
            # 3b. Unknown — pause and ask user to label it
            screen_id = handle_unknown_screen(frame, prior_fsm_state=bot.state)
            if screen_id:
                logger.info(f"Discovery saved as '{screen_id}'. Idling until handler is implemented.")
            # Stay in idle — do not advance FSM this tick
            game_state.update_tick()
            time.sleep(tick_interval)
            continue

        # 3a. Known screen — but only act if handler is implemented
        if not is_implemented(screen):
            logger.debug(f"Screen '{screen}' recognised but handler not yet implemented — idling.")
            game_state.update_tick()
            time.sleep(tick_interval)
            continue

        game_state.update_tick()
        bot.tick()

        # 4. Sleep to maintain target FPS
        elapsed = time.monotonic() - loop_start
        sleep_for = max(0.0, tick_interval - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
