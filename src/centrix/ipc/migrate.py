"""SQLite schema management for the Centrix IPC database."""

from __future__ import annotations

import time
from pathlib import Path
from sqlite3 import Connection, connect

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def epoch_ms() -> int:
    """Return the current epoch milliseconds."""

    return int(time.time() * 1000)


def ensure_db(db_path: str) -> None:
    """Initialise the SQLite database with pragmas and schema if required."""

    path = Path(db_path)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    with connect(path) as conn:
        _apply_pragmas(conn)
        if _needs_initialisation(conn):
            _apply_schema(conn)
        conn.commit()


def _apply_pragmas(conn: Connection) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA temp_store=MEMORY;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()


def _needs_initialisation(conn: Connection) -> bool:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta';",
    )
    exists = cursor.fetchone() is not None
    cursor.close()
    return not exists


def _apply_schema(conn: Connection) -> None:
    schema_sql = SCHEMA_FILE.read_text(encoding="utf-8")
    conn.executescript(schema_sql)
