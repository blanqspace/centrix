"""SQLite-backed event bus implementation."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import string
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from centrix.core.logging import ensure_runtime_dirs
from centrix.settings import get_settings

from .migrate import ensure_db, epoch_ms

_TOKEN_ALPHABET = string.ascii_uppercase + string.digits
_SETTINGS = get_settings()
STATE_FILE = Path(_SETTINGS.state_file)
PID_DIR = Path("runtime/pids")


def _dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _loads(data: str) -> dict[str, Any]:
    loaded = json.loads(data)
    if isinstance(loaded, dict):
        return loaded
    return {"raw": loaded}


class Bus:
    """SQLite-based command and event bus."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        ensure_db(db_path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured SQLite connection."""

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
        finally:
            conn.close()

    def emit(self, topic: str, level: str, data: dict[str, Any], corr_id: str | None = None) -> int:
        """Persist an event entry."""

        now = epoch_ms()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO events(topic, level, data, corr_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (topic, level, _dumps(data), corr_id, now),
            )
            event_id = cursor.lastrowid
            conn.commit()
        if event_id is None:
            raise RuntimeError("Failed to insert event record.")
        return int(event_id)

    def enqueue(self, cmd_type: str, payload: dict[str, Any], corr_id: str | None = None) -> int:
        """Persist a command entry."""

        now = epoch_ms()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO commands(type, payload, corr_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (cmd_type, _dumps(payload), corr_id, now),
            )
            command_id = cursor.lastrowid
            conn.commit()
        if command_id is None:
            raise RuntimeError("Failed to insert command record.")
        return int(command_id)

    def tail_events(
        self,
        limit: int = 100,
        level: str | None = None,
        topic: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the newest events filtered by level/topic."""

        clauses: list[str] = []
        params: list[Any] = []
        if level:
            clauses.append("level = ?")
            params.append(level)
        if topic:
            clauses.append("topic = ?")
            params.append(topic)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT id, topic, level, data, corr_id, created_at
            FROM events
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        """
        params.append(limit)

        with self.connect() as conn:
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

        events: list[dict[str, Any]] = []
        for row in rows:
            event: dict[str, Any] = dict(row)
            event["data"] = _loads(event["data"])
            events.append(event)
        events.reverse()
        return events

    def new_approval(self, command_id: int, ttl_sec: int, token_len: int = 6) -> dict[str, Any]:
        """Create a new approval record with a random token."""

        token = self._generate_token(token_len)
        now = epoch_ms()
        expires_at = now + ttl_sec * 1000
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO approvals(command_id, token, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (command_id, token, expires_at, now),
            )
            approval_id = cursor.lastrowid
            conn.commit()
        if approval_id is None:
            raise RuntimeError("Failed to insert approval record.")
        return {
            "id": approval_id,
            "command_id": command_id,
            "token": token,
            "status": "PENDING",
            "expires_at": expires_at,
            "created_at": now,
        }

    def fulfill_approval(self, token: str, approver: str) -> bool:
        """Attempt to mark an approval as fulfilled."""

        _ = approver  # Approver recorded via external audit in later phases.
        now = epoch_ms()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                SELECT id, status, expires_at
                FROM approvals
                WHERE token = ?
                """,
                (token,),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            if row["status"] != "PENDING":
                return False
            if row["expires_at"] <= now:
                conn.execute(
                    "UPDATE approvals SET status = 'EXPIRED' WHERE id = ?",
                    (row["id"],),
                )
                conn.commit()
                return False

            conn.execute(
                "UPDATE approvals SET status = 'OK' WHERE id = ?",
                (row["id"],),
            )
            conn.commit()
            return True

    def expire_approvals(self, now_ms: int) -> int:
        """Expire approvals whose TTL has elapsed."""

        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = 'EXPIRED'
                WHERE status = 'PENDING' AND expires_at <= ?
                """,
                (now_ms,),
            )
            conn.commit()
            return int(cursor.rowcount)

    def count_pending_commands(self) -> int:
        """Return the number of queued commands awaiting processing."""

        with self.connect() as conn:
            cursor = conn.execute(
                "SELECT COUNT(1) AS total FROM commands WHERE status = 'NEW'",
            )
            row = cursor.fetchone()
            return int(row["total"]) if row else 0

    def count_pending_approvals(self) -> int:
        """Return the number of approvals in pending state."""

        with self.connect() as conn:
            cursor = conn.execute(
                "SELECT COUNT(1) AS total FROM approvals WHERE status = 'PENDING'",
            )
            row = cursor.fetchone()
            return int(row["total"]) if row else 0

    def set_kv(self, key: str, value: str) -> None:
        """Upsert a key/value pair."""

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO kv(k, v)
                VALUES (?, ?)
                ON CONFLICT(k) DO UPDATE SET v=excluded.v
                """,
                (key, value),
            )
            conn.commit()

    def get_kv(self, key: str) -> str | None:
        """Retrieve a value from the key/value store."""

        with self.connect() as conn:
            cursor = conn.execute(
                "SELECT v FROM kv WHERE k = ?",
                (key,),
            )
            row = cursor.fetchone()
            return row["v"] if row else None

    def record_heartbeat(self, component: str, ts_ms: int) -> None:
        """Record a heartbeat timestamp for a component."""

        self.set_kv(f"heartbeat:{component}", str(ts_ms))

    def get_heartbeat(self, component: str) -> int | None:
        """Fetch the last recorded heartbeat for a component."""

        value = self.get_kv(f"heartbeat:{component}")
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def get_services_status(self, services: list[str]) -> dict[str, dict[str, Any]]:
        """Return runtime status information for the given services."""

        result: dict[str, dict[str, Any]] = {}
        for name in services:
            path = pidfile(name)
            pid: int | None = None
            running = False
            if path.exists():
                try:
                    pid = int(path.read_text(encoding="utf-8").strip())
                except ValueError:
                    path.unlink(missing_ok=True)
                    pid = None
            if pid and is_running(pid):
                running = True
            else:
                if path.exists():
                    path.unlink(missing_ok=True)
                pid = None
            entry: dict[str, Any] = {"pid": pid, "running": running}
            heartbeat = self.get_heartbeat(name)
            if heartbeat is not None:
                entry["last_heartbeat"] = heartbeat
            result[name] = entry
        return result

    def _generate_token(self, length: int) -> str:
        return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(length))


def _default_state() -> dict[str, Any]:
    return {"mode": "mock", "mode_mock": True, "paused": False}


def read_state() -> dict[str, Any]:
    """Read the persisted control state, creating defaults if necessary."""

    ensure_runtime_dirs()
    if not STATE_FILE.exists():
        state = _default_state()
        STATE_FILE.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
        return state

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):  # pragma: no cover - defensive
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        data = _default_state()
    merged = _default_state()
    merged.update(data)
    return merged


def write_state(**fields: Any) -> dict[str, Any]:
    """Update the control state with the provided fields."""

    state = read_state()
    state.update(fields)
    ensure_runtime_dirs()
    STATE_FILE.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    return state


def pidfile(name: str) -> Path:
    """Return the pidfile path for a named service."""

    ensure_runtime_dirs()
    PID_DIR.mkdir(parents=True, exist_ok=True)
    safe = name.replace("/", "_")
    return PID_DIR / f"{safe}.pid"


def is_running(pid: int) -> bool:
    """Return whether the provided PID appears active."""

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
