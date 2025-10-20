"""In-memory KPI tracking for Centrix services."""

from __future__ import annotations

import time
from collections import deque
from threading import Lock

ERROR_WINDOW_SEC = 60.0
ALERT_WINDOW_SEC = 60.0


class KPIStore:
    """Thread-safe store for lightweight KPIs."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._errors: deque[float] = deque()
        self._alert_dedup: deque[float] = deque()
        self._alert_throttle: deque[float] = deque()
        self._open_approvals = 0
        self._queue_depth = 0

    def _prune(self, container: deque[float], now: float, window: float) -> None:
        while container and now - container[0] > window:
            container.popleft()

    def record_error(self, now: float | None = None) -> None:
        now_ts = now or time.time()
        with self._lock:
            self._errors.append(now_ts)
            self._prune(self._errors, now_ts, ERROR_WINDOW_SEC)

    def record_alert_dedup(self, now: float | None = None) -> None:
        now_ts = now or time.time()
        with self._lock:
            self._alert_dedup.append(now_ts)
            self._prune(self._alert_dedup, now_ts, ALERT_WINDOW_SEC)

    def record_alert_throttle(self, now: float | None = None) -> None:
        now_ts = now or time.time()
        with self._lock:
            self._alert_throttle.append(now_ts)
            self._prune(self._alert_throttle, now_ts, ALERT_WINDOW_SEC)

    def update_open_approvals(self, value: int) -> None:
        with self._lock:
            self._open_approvals = max(0, value)

    def update_queue_depth(self, value: int) -> None:
        with self._lock:
            self._queue_depth = max(0, value)

    def snapshot(self) -> dict[str, int]:
        now_ts = time.time()
        with self._lock:
            self._prune(self._errors, now_ts, ERROR_WINDOW_SEC)
            self._prune(self._alert_dedup, now_ts, ALERT_WINDOW_SEC)
            self._prune(self._alert_throttle, now_ts, ALERT_WINDOW_SEC)
            return {
                "open_approvals": self._open_approvals,
                "queue_depth": self._queue_depth,
                "errors_1m": len(self._errors),
                "alerts_dedup_1m": len(self._alert_dedup),
                "alerts_throttle_1m": len(self._alert_throttle),
            }

    def reset(self) -> None:
        """Reset stored data (test helper)."""

        with self._lock:
            self._errors.clear()
            self._alert_dedup.clear()
            self._alert_throttle.clear()
            self._open_approvals = 0
            self._queue_depth = 0


METRICS = KPIStore()


def snapshot_kpis() -> dict[str, int]:
    """Return a snapshot of current KPI values."""

    return METRICS.snapshot()
