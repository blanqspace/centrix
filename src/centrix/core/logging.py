"""Structured plaintext logging helpers for Centrix."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

RUNTIME_ROOT = Path("runtime")
LOG_DIR = RUNTIME_ROOT / "logs"
LOCK_DIR = RUNTIME_ROOT / "locks"
PID_DIR = RUNTIME_ROOT / "pids"
REPORT_DIR = RUNTIME_ROOT / "reports"

TEXT_LOG = LOG_DIR / "centrix.log"


def ensure_runtime_dirs() -> None:
    """Ensure runtime directories exist."""

    for path in (RUNTIME_ROOT, LOG_DIR, LOCK_DIR, PID_DIR, REPORT_DIR):
        path.mkdir(parents=True, exist_ok=True)


def log_event(
    svc: str,
    topic: str,
    message: str,
    *,
    level: str = "INFO",
    **fields: Any,
) -> None:
    """Write a structured plaintext log entry."""

    ensure_runtime_dirs()
    ts = datetime.now().isoformat(timespec="seconds")
    parts = [f"[{ts}]", f"level={level.upper()}", f"svc={svc}", f"topic={topic}"]
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    parts.append(f'msg="{message}"')
    with TEXT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(" ".join(parts) + "\n")
