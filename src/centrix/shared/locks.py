"""File-based lock helpers used for control operations."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from centrix.core.logging import ensure_runtime_dirs

LOCK_DIR = Path("runtime/locks")


def _lock_path(name: str) -> Path:
    safe = name.replace("/", "_")
    return LOCK_DIR / f"{safe}.lock"


def acquire_lock(name: str, ttl: int = 30) -> bool:
    """Attempt to acquire a cooperative lock returning ``True`` on success."""

    ensure_runtime_dirs()
    path = _lock_path(name)
    now = int(time.time() * 1000)
    payload = {"pid": os.getpid(), "expires_at": now + ttl * 1000}
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError:
        owner = lock_owner(name)
        if owner and owner.get("expires_at", 0) < now:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return acquire_lock(name, ttl=ttl)
        return False

    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return True


def release_lock(name: str) -> None:
    """Release a previously acquired lock if still held."""

    path = _lock_path(name)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def lock_owner(name: str) -> dict[str, Any] | None:
    """Return the stored lock payload, if any."""

    path = _lock_path(name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
