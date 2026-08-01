# memory/logger.py
# Structured logging for every bot action.
# Each record captures: timestamp, FSM state, action taken, outcome, and a
# screenshot path — building a dataset for future ML training.

from __future__ import annotations

from pathlib import Path

from loguru import logger

from config.settings import LOG_DIR, LOG_LEVEL


def setup_logging() -> None:
    """Configure loguru sinks. Call once at startup."""
    log_path = Path(LOG_DIR)
    log_path.mkdir(parents=True, exist_ok=True)

    logger.remove()                              # remove default stderr sink
    logger.add(
        log_path / "bot_{time:YYYY-MM-DD}.log",
        level=LOG_LEVEL,
        rotation="00:00",                        # new file each day
        retention="30 days",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {message}",
        serialize=False,
    )
    logger.add(
        sink=lambda msg: print(msg, end=""),     # also print to console
        level="INFO",
        colorize=True,
    )


def log_action(
    fsm_state: str,
    action: str,
    outcome: str,
    screenshot_path: str = "",
    extra: dict | None = None,
) -> None:
    """Log a single bot action with full context."""
    raise NotImplementedError
