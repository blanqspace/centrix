"""In-memory order storage for dashboard display."""

from __future__ import annotations

import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

_ORDERS: deque[dict[str, Any]] = deque(maxlen=50)
_LOCK = threading.Lock()


def add_order(data: dict[str, Any]) -> dict[str, Any]:
    """Add an order payload to the ring buffer."""

    record = {
        "ts": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        **data,
    }
    with _LOCK:
        _ORDERS.appendleft(record)
    return record


def list_orders() -> list[dict[str, Any]]:
    """Return the current order list (newest first)."""

    with _LOCK:
        return list(_ORDERS)


def clear_orders() -> None:
    """Reset stored orders (primarily for tests)."""

    with _LOCK:
        _ORDERS.clear()
