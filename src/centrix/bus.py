"""SQLite-backed command bus for Centrix."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("centrix.bus")

_DB_LOCK = threading.RLock()
_DB_PATH: Path | None = None

_CREATE_COMMANDS = """
CREATE TABLE IF NOT EXISTS commands(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cmd_id TEXT UNIQUE,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'NEW',
    requested_by TEXT,
    role TEXT,
    ttl_sec INTEGER,
    corr_id TEXT,
    created_at REAL NOT NULL
);
"""

_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eid TEXT UNIQUE,
    cmd_id TEXT,
    topic TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT,
    data TEXT NOT NULL,
    corr_id TEXT,
    created_at REAL NOT NULL
);
"""

_CREATE_STATUS = """
CREATE TABLE IF NOT EXISTS svc_status(
    service TEXT PRIMARY KEY,
    last_seen REAL NOT NULL,
    state TEXT NOT NULL,
    details TEXT
);
"""

_CREATE_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_commands_status_created ON commands(status, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_events_cmd_created ON events(cmd_id, created_at);",
)


def init_db(path: str | Path = "runtime/ctl.db") -> Path:
    """Initialise the SQLite database storing commands and events."""
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _DB_LOCK:
        global _DB_PATH
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=OFF;")
            _ensure_table(
                conn,
                "commands",
                _CREATE_COMMANDS,
                {"id", "cmd_id", "type", "payload", "status", "requested_by", "role", "created_at", "ttl_sec", "corr_id"},
            )
            _ensure_table(
                conn,
                "events",
                _CREATE_EVENTS,
                {"id", "eid", "cmd_id", "topic", "level", "data", "created_at", "corr_id"},
            )
            _ensure_table(
                conn,
                "svc_status",
                _CREATE_STATUS,
                {"service", "last_seen", "state", "details"},
            )
            for ddl in _CREATE_IDX:
                conn.execute(ddl)
            conn.commit()
        finally:
            conn.close()
        _DB_PATH = db_path
    log.info("Command bus initialised at %s", db_path)
    return db_path


def _ensure_db() -> Path:
    with _DB_LOCK:
        if _DB_PATH is None:
            return init_db()
        return _DB_PATH


def _connect() -> sqlite3.Connection:
    path = _ensure_db()
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table(
    conn: sqlite3.Connection,
    name: str,
    create_sql: str,
    required_columns: set[str],
) -> None:
    existing_cols = _get_columns(conn, name)
    if existing_cols is None:
        conn.execute(create_sql.strip())
        return
    if required_columns.issubset(existing_cols):
        return

    suffix = 1
    while True:
        legacy_name = f"{name}_legacy{suffix if suffix > 1 else ''}"
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (legacy_name,),
        ).fetchone()
        if not exists:
            break
        suffix += 1

    log.warning("Renaming incompatible table %s -> %s for schema upgrade", name, legacy_name)
    conn.execute(f"ALTER TABLE {name} RENAME TO {legacy_name}")
    conn.execute(create_sql.strip())


def _get_columns(conn: sqlite3.Connection, name: str) -> set[str] | None:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    if cursor.fetchone() is None:
        return None
    info = conn.execute(f"PRAGMA table_info({name})").fetchall()
    return {row[1] for row in info}


def _json(payload: dict[str, Any] | None) -> str:
    if payload is None:
        return "{}"
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def _status_db_path() -> Path:
    with _DB_LOCK:
        global _DB_PATH
        if _DB_PATH is None:
            _DB_PATH = Path("runtime/ctl.db")
        path = _DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def enqueue_command(
    type: str,
    payload: dict[str, Any],
    requested_by: str,
    role: str,
    ttl_sec: int | None,
) -> str:
    """Persist a NEW command and return its identifier."""
    cmd_id = str(uuid.uuid4())
    created_at = time.time()
    record = (
        cmd_id,
        type.upper(),
        _json(payload) or "{}",
        "NEW",
        requested_by,
        role,
        created_at,
        ttl_sec,
    )
    conn = _connect()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO commands(cmd_id, type, payload, status, requested_by, role, created_at, ttl_sec, corr_id)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (*record, None),
            )
    finally:
        conn.close()
    log.info("Enqueued command %s type=%s by=%s role=%s", cmd_id, type, requested_by, role)
    return cmd_id


def append_event(
    cmd_id: str,
    level: str,
    message: str,
    data: dict[str, Any] | None = None,
    *,
    topic: str | None = None,
) -> None:
    """Append an event for a command."""
    eid = str(uuid.uuid4())
    created_at = time.time()
    payload = _json(data)
    event_topic = topic or f"cmd.{level.lower()}"
    conn = _connect()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO events(eid, cmd_id, topic, level, message, data, created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (eid, cmd_id, event_topic, level.upper(), message, payload, created_at),
            )
    finally:
        conn.close()
    log.debug("Appended event %s -> %s %s", eid, cmd_id, message)


def touch_service(name: str, state: str = "up", details: dict[str, Any] | None = None) -> None:
    """Upsert the status record for a service."""

    path = _status_db_path()
    now = time.time()
    payload = _json(details) if details is not None else None
    try:
        with sqlite3.connect(str(path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(_CREATE_STATUS.strip())
            conn.execute(
                """
                INSERT INTO svc_status(service, last_seen, state, details)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(service) DO UPDATE SET
                    last_seen=excluded.last_seen,
                    state=excluded.state,
                    details=excluded.details
                """,
                (name, now, state, payload),
            )
    except Exception:  # pragma: no cover - defensive
        log.exception("Failed to update svc_status for %s", name)


def _parse_details(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else {"value": value}


def get_services() -> dict[str, dict[str, Any]]:
    """Return a snapshot of recorded service statuses."""

    path = _status_db_path()
    try:
        with sqlite3.connect(str(path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(_CREATE_STATUS.strip())
            rows = conn.execute(
                "SELECT service, last_seen, state, details FROM svc_status ORDER BY service"
            ).fetchall()
    except Exception:  # pragma: no cover - defensive
        log.exception("Failed to query svc_status")
        return {}

    snapshot: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row["service"])
        last_seen_raw = row["last_seen"]
        try:
            last_seen = float(last_seen_raw)
        except (TypeError, ValueError):
            last_seen = 0.0
        entry: dict[str, Any] = {
            "last_seen": last_seen,
            "state": str(row["state"]),
        }
        details = _parse_details(row["details"])
        if details is not None:
            entry["details"] = details
        snapshot[name] = entry
    return snapshot
