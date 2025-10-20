"""Alert emission helpers with dedupe and throttling."""

from __future__ import annotations

import time
from collections import deque
from threading import Lock
from typing import Any

from centrix.core.logging import log_event
from centrix.core.metrics import METRICS
from centrix.settings import AppSettings, get_settings

_LOCK = Lock()
_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "CRITICAL": 50}
_RATE_BUCKETS: dict[str, deque[float]] = {level: deque() for level in _LEVEL_ORDER}
_DEDUP: dict[str, dict[str, Any]] = {}
_EMITTED = 0
_DEDUP_TOTAL = 0
_THROTTLE_TOTAL = 0


def _settings() -> AppSettings:
    return get_settings()


def _normalise_level(level: str) -> str:
    upper = level.upper()
    return upper if upper in _LEVEL_ORDER else "INFO"


def _prune(timestamps: deque[float], now: float, window: float) -> None:
    while timestamps and now - timestamps[0] > window:
        timestamps.popleft()


def emit_alert(level: str, topic: str, message: str, fingerprint: str) -> bool:
    """Emit an alert if not deduped/throttled. Returns True if emitted."""

    global _EMITTED, _DEDUP_TOTAL, _THROTTLE_TOTAL

    settings = _settings()
    norm_level = _normalise_level(level)
    min_level = _normalise_level(settings.alert_min_level)
    if _LEVEL_ORDER[norm_level] < _LEVEL_ORDER[min_level]:
        return False

    now = time.time()
    dedup_window = float(settings.alert_dedup_window_sec)
    rate_limit = max(1, int(settings.alert_rate_per_min))

    with _LOCK:
        # Throttle per level
        bucket = _RATE_BUCKETS[norm_level]
        _prune(bucket, now, 60.0)
        if len(bucket) >= rate_limit:
            _THROTTLE_TOTAL += 1
            METRICS.record_alert_throttle(now)
            return False

        # Dedupe by fingerprint
        stale_cutoff = now - dedup_window
        stale_keys = [key for key, data in _DEDUP.items() if float(data["last_ts"]) < stale_cutoff]
        for key in stale_keys:
            _DEDUP.pop(key, None)

        entry = _DEDUP.get(fingerprint)
        if entry is not None and now - float(entry["first_ts"]) <= dedup_window:
            entry["count"] = int(entry.get("count", 1)) + 1
            entry["last_ts"] = now
            _DEDUP_TOTAL += 1
            METRICS.record_alert_dedup(now)
            return False

        _DEDUP[fingerprint] = {"first_ts": now, "last_ts": now, "count": 1, "level": norm_level}
        bucket.append(now)

    _EMITTED += 1
    log_event(
        "alerts",
        topic,
        message,
        level=norm_level,
        corr_id=fingerprint,
        occurrences=1,
    )
    try:
        from centrix.services.slack import route_alert

        route_alert(norm_level, topic, message, fingerprint=fingerprint)
    except Exception:  # pragma: no cover - optional integration
        pass
    return True


def alert_counters() -> dict[str, int]:
    """Return aggregate insight for diagnostics/reporting."""

    with _LOCK:
        return {
            "emitted": _EMITTED,
            "deduped": _DEDUP_TOTAL,
            "throttled": _THROTTLE_TOTAL,
        }


def reset_alerts() -> None:
    """Clear alert state (intended for tests)."""

    with _LOCK:
        global _EMITTED, _DEDUP_TOTAL, _THROTTLE_TOTAL
        _DEDUP.clear()
        for bucket in _RATE_BUCKETS.values():
            bucket.clear()
        _EMITTED = 0
        _DEDUP_TOTAL = 0
        _THROTTLE_TOTAL = 0
