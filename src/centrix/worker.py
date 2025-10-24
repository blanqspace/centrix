"""Background worker consuming queued commands."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from .bus import append_event, init_db

log = logging.getLogger("centrix.worker")


def run_worker(poll_sec: float = 1.0) -> None:
    """Continuously pull commands from the queue and execute them."""
    db_path = init_db()
    log.info("Worker started (db=%s poll=%.1fs)", db_path, poll_sec)
    try:
        while True:
            try:
                processed = _process_once()
                if not processed:
                    time.sleep(poll_sec)
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Worker loop error; backing off")
                time.sleep(poll_sec)
    except KeyboardInterrupt:
        log.info("Worker interrupted, shutting down")
    log.info("Worker stopped")


def _connect() -> sqlite3.Connection:
    path = init_db()
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _expire_stale(conn: sqlite3.Connection, now: float) -> None:
    rows = conn.execute(
        """
        SELECT cmd_id, ttl_sec, type FROM commands
        WHERE status='NEW' AND ttl_sec IS NOT NULL AND created_at + ttl_sec <= ?
        """,
        (now,),
    ).fetchall()

    for row in rows:
        cmd_id = row["cmd_id"]
        ttl = row["ttl_sec"]
        cmd_type = (row["type"] or "unknown").lower()
        with conn:
            conn.execute("UPDATE commands SET status='EXPIRED' WHERE cmd_id=?", (cmd_id,))
        append_event(
            cmd_id,
            "WARN",
            "Command expired",
            {"ttl_sec": ttl},
            topic=f"cmd.{cmd_type}.expired",
        )
        log.info("Expired command %s (ttl=%s)", cmd_id, ttl)


def _process_once() -> bool:
    now = time.time()
    conn = _connect()
    try:
        _expire_stale(conn, now)
        row = conn.execute(
            """
            SELECT cmd_id, type, payload, requested_by, role
            FROM commands
            WHERE status='NEW' AND (ttl_sec IS NULL OR created_at + ttl_sec > ?)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()
        if row is None:
            return False

        cmd_id = row["cmd_id"]
        with conn:
            updated = conn.execute(
                "UPDATE commands SET status='RUNNING' WHERE cmd_id=? AND status='NEW'",
                (cmd_id,),
            ).rowcount
        if updated == 0:
            return True

        payload = _load_json(row["payload"])
        log.info("Executing command %s type=%s by=%s", cmd_id, row["type"], row["requested_by"])

        cmd_type = (row["type"] or "unknown").lower()
        try:
            time.sleep(0.2)
            append_event(
                cmd_id,
                "INFO",
                "EXEC_OK",
                {"payload": payload},
                topic=f"cmd.{cmd_type}.ok",
            )
            with conn:
                conn.execute("UPDATE commands SET status='DONE' WHERE cmd_id=?", (cmd_id,))
            log.info("Command %s completed", cmd_id)
        except Exception as exc:
            append_event(
                cmd_id,
                "ERROR",
                "EXEC_FAIL",
                {"error": str(exc)},
                topic=f"cmd.{cmd_type}.fail",
            )
            with conn:
                conn.execute("UPDATE commands SET status='FAIL' WHERE cmd_id=?", (cmd_id,))
            log.exception("Command %s failed: %s", cmd_id, exc)
        return True
    finally:
        conn.close()


def _load_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
