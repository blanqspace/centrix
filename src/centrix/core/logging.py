"""Logging utilities for Centrix."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from centrix.ipc.migrate import epoch_ms

RUNTIME_ROOT = Path("runtime")
LOG_DIR = RUNTIME_ROOT / "logs"
LOCK_DIR = RUNTIME_ROOT / "locks"
PID_DIR = RUNTIME_ROOT / "pids"
REPORT_DIR = RUNTIME_ROOT / "reports"

TEXT_LOG = LOG_DIR / "centrix.log"
JSON_LOG = LOG_DIR / "centrix.jsonl"


def ensure_runtime_dirs() -> None:
    """Ensure runtime directories exist."""

    for path in (RUNTIME_ROOT, LOG_DIR, LOCK_DIR, PID_DIR, REPORT_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _text_handler_exists(logger: logging.Logger) -> bool:
    for handler in logger.handlers:
        if getattr(handler, "_centrix_text_handler", False):
            return True
    return False


def get_text_logger(name: str = "centrix") -> logging.Logger:
    """Return a logger configured to write to the text log."""

    ensure_runtime_dirs()
    logger = logging.getLogger(name)
    if not _text_handler_exists(logger):
        handler = logging.FileHandler(TEXT_LOG, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler._centrix_text_handler = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def log_json(level: str, message: str, **fields: object) -> None:
    """Append a JSON log record to the JSONL log."""

    ensure_runtime_dirs()
    entry = {
        "ts": epoch_ms(),
        "level": level.upper(),
        "msg": message,
    }
    if fields:
        entry.update(fields)
    with JSON_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False))
        handle.write("\n")
