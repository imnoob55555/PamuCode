"""Instance logging for the PamuCode runtime.

Logs are written beneath the workspace state directory (``.pamu/logs/``)
which is already excluded from version control by the protective state
``.gitignore``. Console output stays unchanged; the logger is a side
channel for post-hoc inspection, never a user interface.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
LOG_FILE_NAME = "pamu.log"
MAX_BYTES = 2_000_000
BACKUP_COUNT = 5

ROOT_LOGGER_NAME = "pamu"


def get_logger(name: str = ROOT_LOGGER_NAME) -> logging.Logger:
    """Return a logger under the pamu namespace, safe before setup."""
    root = logging.getLogger(ROOT_LOGGER_NAME)
    if not any(
        isinstance(handler, logging.NullHandler) for handler in root.handlers
    ):
        root.addHandler(logging.NullHandler())
    return logging.getLogger(name)


def resolve_level(level: str) -> int:
    """Map a settings level name to a logging level, defaulting to INFO."""
    resolved = getattr(logging, str(level).strip().upper(), None)
    return resolved if isinstance(resolved, int) else logging.INFO


def setup_logging(logs_dir: Path, level: str = "INFO") -> logging.Logger:
    """Attach one rotating file handler for the pamu logger, idempotently."""
    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.setLevel(resolve_level(level))
    if any(isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        return root
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        logs_dir / LOG_FILE_NAME,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
    return root


def reset_logging() -> None:
    """Detach every handler; used by tests to isolate logger state."""
    root = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
