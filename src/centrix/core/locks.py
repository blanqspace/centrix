"""Filesystem and database backed lock management."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from centrix.ipc.bus import Bus
from centrix.ipc.migrate import epoch_ms
from centrix.settings import get_settings

from .logging import ensure_runtime_dirs

LOCK_DIR = Path("runtime/locks")


def _lock_path(name: str) -> Path:
    safe_name = name.replace("/", "_")
    return LOCK_DIR / f"{safe_name}.lock"


def _bus() -> Bus:
    settings = get_settings()
    return Bus(settings.ipc_db)


def acquire(name: str, owner: str, ttl_sec: int) -> bool:
    """Acquire a lock."""

    ensure_runtime_dirs()
    path = _lock_path(name)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError:
        return False

    now = epoch_ms()

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{owner} {now}\n")

        bus = _bus()
        with bus.connect() as conn:
            conn.execute(
                """
                INSERT INTO locks(name, owner, acquired_at, ttl_sec)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    owner=excluded.owner,
                    acquired_at=excluded.acquired_at,
                    ttl_sec=excluded.ttl_sec
                """,
                (name, owner, now, ttl_sec),
            )
            conn.commit()
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise

    return True


def release(name: str, owner: str) -> bool:
    """Release a lock."""

    bus = _bus()
    with bus.connect() as conn:
        cursor = conn.execute(
            "SELECT owner FROM locks WHERE name = ?",
            (name,),
        )
        row = cursor.fetchone()
        if row is None or row["owner"] != owner:
            return False
        conn.execute("DELETE FROM locks WHERE name = ?", (name,))
        conn.commit()

    try:
        _lock_path(name).unlink()
    except FileNotFoundError:
        pass
    return True


def list_locks() -> list[dict[str, Any]]:
    """List all active locks."""

    bus = _bus()
    with bus.connect() as conn:
        cursor = conn.execute(
            "SELECT name, owner, acquired_at, ttl_sec FROM locks ORDER BY name ASC"
        )
        rows = cursor.fetchall()
    locks: list[dict[str, Any]] = [dict(row) for row in rows]
    return locks


def reap(time_ms: int) -> int:
    """Release locks that expired prior to the supplied timestamp."""

    bus = _bus()
    with bus.connect() as conn:
        cursor = conn.execute(
            """
            SELECT name FROM locks
            WHERE acquired_at + (ttl_sec * 1000) <= ?
            """,
            (time_ms,),
        )
        expired = [row["name"] for row in cursor.fetchall()]
        if not expired:
            return 0
        conn.executemany(
            "DELETE FROM locks WHERE name = ?",
            [(name,) for name in expired],
        )
        conn.commit()

    for name in expired:
        try:
            _lock_path(name).unlink()
        except FileNotFoundError:
            pass
    return len(expired)
