"""Structured logging helpers for Centrix."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from .metrics import METRICS

RUNTIME_ROOT = Path("runtime")
LOG_DIR = RUNTIME_ROOT / "logs"
LOCK_DIR = RUNTIME_ROOT / "locks"
PID_DIR = RUNTIME_ROOT / "pids"
REPORT_DIR = RUNTIME_ROOT / "reports"

TEXT_LOG = LOG_DIR / "centrix.log"
JSON_LOG = LOG_DIR / "centrix.jsonl"

LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 5
_LOG_LOCK = Lock()

_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "CRITICAL": 50}


def ensure_runtime_dirs() -> None:
    """Ensure runtime directories exist."""

    for path in (RUNTIME_ROOT, LOG_DIR, LOCK_DIR, PID_DIR, REPORT_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _rotate(path: Path) -> None:
    if not path.exists():
        return
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size < LOG_MAX_BYTES:
        return

    def _backup_name(index: int) -> Path:
        return path.with_name(f"{path.name}.{index}")

    # Drop the oldest backup if it exists.
    oldest = _backup_name(LOG_BACKUP_COUNT)
    if oldest.exists():
        oldest.unlink()

    for idx in range(LOG_BACKUP_COUNT - 1, 0, -1):
        src = _backup_name(idx)
        if src.exists():
            src.rename(_backup_name(idx + 1))

    path.rename(_backup_name(1))


def _normalise_level(level: str) -> str:
    upper = level.upper()
    return upper if upper in _LEVEL_ORDER else "INFO"


def log_event(
    svc: str,
    topic: str,
    message: str,
    *,
    level: str = "INFO",
    corr_id: str | None = None,
    **fields: Any,
) -> None:
    """Write a structured log entry to plaintext and JSONL targets."""

    ensure_runtime_dirs()
    ts = datetime.now().isoformat(timespec="seconds")
    pid = os.getpid()
    level_norm = _normalise_level(level)

    event: dict[str, Any] = {
        "ts": ts,
        "level": level_norm,
        "svc": svc,
        "topic": topic,
        "msg": message,
        "pid": pid,
    }
    if corr_id:
        event["corr_id"] = corr_id
    if fields:
        event["extra"] = fields

    parts = [
        f"[{ts}]",
        f"level={level_norm}",
        f"svc={svc}",
        f"topic={topic}",
        f"pid={pid}",
    ]
    if corr_id:
        parts.append(f"corr_id={corr_id}")
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    parts.append(f'msg="{message}"')

    line = " ".join(parts) + "\n"
    json_line = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"

    with _LOG_LOCK:
        _rotate(TEXT_LOG)
        _rotate(JSON_LOG)
        with TEXT_LOG.open("a", encoding="utf-8") as text_handle:
            text_handle.write(line)
        with JSON_LOG.open("a", encoding="utf-8") as json_handle:
            json_handle.write(json_line)

    if _LEVEL_ORDER.get(level_norm, 0) >= _LEVEL_ORDER["ERROR"]:
        METRICS.record_error()
