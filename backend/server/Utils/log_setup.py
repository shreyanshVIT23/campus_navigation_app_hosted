"""Logging for the server: console, plus a rotating file when LOG_FILE is set.

Levels come from LOG_LEVEL (DEBUG shows every timed stage and cache hit/miss;
INFO shows one summary line per request). Loggers that already exist, such as
uvicorn's, are left enabled (disable_existing_loggers=False, logging.config docs).
"""

import logging
import logging.config
import os
from pathlib import Path

from .loader import resolve_path

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_FILE_BYTES = 1024 * 1024
LOG_FILE_BACKUPS = 5
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def configure_logging(level: str, log_file: Path | None):
    level_name = level.strip().upper()
    if level_name not in LEVELS:
        raise ValueError(f"LOG_LEVEL must be one of {', '.join(LEVELS)}, not {level!r}")

    handlers: dict[str, dict[str, object]] = {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"}
    }
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "plain",
            "filename": str(log_file),
            "maxBytes": LOG_FILE_BYTES,
            "backupCount": LOG_FILE_BACKUPS,
            "encoding": "utf-8",
        }

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"plain": {"format": FORMAT}},
            "handlers": handlers,
            "root": {"level": level_name, "handlers": list(handlers)},
        }
    )


def configure_from_env():
    """LOG_LEVEL (default INFO) and LOG_FILE (relative to server/) from .env."""
    log_file = os.getenv("LOG_FILE")
    configure_logging(
        os.getenv("LOG_LEVEL") or "INFO", resolve_path(log_file) if log_file else None
    )
