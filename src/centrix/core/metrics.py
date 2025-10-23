"""In-memory KPI tracking for Centrix services."""

from __future__ import annotations

import time
from collections import deque
from statistics import median
from threading import Lock
from typing import Any

ERROR_WINDOW_SEC = 60.0
ALERT_WINDOW_SEC = 60.0


class KPIStore:
    """Thread-safe store for lightweight KPIs."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[str, int] = {}
        self._errors: deque[float] = deque()
        self._alert_dedup: deque[float] = deque()
        self._alert_throttle: deque[float] = deque()
        self._open_approvals = 0
        self._queue_depth = 0
        self._ibkr_latency_ms: deque[float] = deque(maxlen=50)
        self._risk: dict[str, float] = {
            "pnl_day": 0.0,
            "pnl_open": 0.0,
            "margin_used_pct": 0.0,
        }
        self._initialize_default_counters()

    def _initialize_default_counters(self) -> None:
        self._counters.setdefault("ibkr_errors_total", 0)
        self._counters.setdefault("ibkr_pacing_violations_total", 0)

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

    def update_risk(
        self,
        pnl_day: float | None = None,
        pnl_open: float | None = None,
        margin_used_pct: float | None = None,
    ) -> None:
        """Update risk snapshot fields used for dashboard simulations."""

        with self._lock:
            if pnl_day is not None:
                self._risk["pnl_day"] = float(pnl_day)
            if pnl_open is not None:
                self._risk["pnl_open"] = float(pnl_open)
            if margin_used_pct is not None:
                self._risk["margin_used_pct"] = float(margin_used_pct)

    def update_ibkr_latency(self, ms: float) -> None:
        """Record latest IBKR latency sample in milliseconds."""

        if ms < 0:
            return
        with self._lock:
            self._ibkr_latency_ms.append(float(ms))

    def _ibkr_latency_median(self) -> float | None:
        if not self._ibkr_latency_ms:
            return None
        return float(median(self._ibkr_latency_ms))

    def snapshot(self) -> dict[str, Any]:
        now_ts = time.time()
        with self._lock:
            self._prune(self._errors, now_ts, ERROR_WINDOW_SEC)
            self._prune(self._alert_dedup, now_ts, ALERT_WINDOW_SEC)
            self._prune(self._alert_throttle, now_ts, ALERT_WINDOW_SEC)
            ibkr_latency_median = self._ibkr_latency_median()
            snapshot: dict[str, Any] = {
                "open_approvals": self._open_approvals,
                "queue_depth": self._queue_depth,
                "errors_1m": len(self._errors),
                "alerts_dedup_1m": len(self._alert_dedup),
                "alerts_throttle_1m": len(self._alert_throttle),
                "risk": dict(self._risk),
            }
            snapshot["ibkr_latency_ms_median"] = ibkr_latency_median
            snapshot["counters"] = dict(self._counters)
            return snapshot

    def reset(self) -> None:
        """Reset stored data (test helper)."""

        with self._lock:
            self._errors.clear()
            self._alert_dedup.clear()
            self._alert_throttle.clear()
            self._open_approvals = 0
            self._queue_depth = 0
            self._counters.clear()
            self._ibkr_latency_ms.clear()
            self._risk = {
                "pnl_day": 0.0,
                "pnl_open": 0.0,
                "margin_used_pct": 0.0,
            }
            self._initialize_default_counters()

    def increment_counter(self, key: str, amount: int = 1) -> None:
        """Increment a named counter used for diagnostics."""

        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def get_counter(self, key: str) -> int:
        """Return a counter value (defaults to zero)."""

        with self._lock:
            return self._counters.get(key, 0)


METRICS = KPIStore()


def snapshot_kpis() -> dict[str, Any]:
    """Return a snapshot of current KPI values."""

    return METRICS.snapshot()
