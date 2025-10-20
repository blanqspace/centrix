from __future__ import annotations

from centrix.core.alerts import alert_counters, emit_alert, reset_alerts
from centrix.core.metrics import METRICS, snapshot_kpis
from centrix.settings import get_settings


def test_alert_dedupe_counts(monkeypatch) -> None:
    reset_alerts()
    METRICS.reset()
    assert emit_alert("ERROR", "svc.test", "failure", "fp-1") is True
    assert emit_alert("ERROR", "svc.test", "failure", "fp-1") is False
    counters = alert_counters()
    assert counters["emitted"] == 1
    assert counters["deduped"] == 1
    snapshot = snapshot_kpis()
    assert snapshot["alerts_dedup_1m"] >= 1


def test_alert_throttle(monkeypatch) -> None:
    reset_alerts()
    METRICS.reset()
    monkeypatch.setenv("ALERT_RATE_PER_MIN", "1")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        assert emit_alert("ERROR", "svc.test", "first", "fp-1") is True
        assert emit_alert("ERROR", "svc.test", "second", "fp-2") is False
        counters = alert_counters()
        assert counters["throttled"] == 1
        snapshot = snapshot_kpis()
        assert snapshot["alerts_throttle_1m"] >= 1
    finally:
        monkeypatch.delenv("ALERT_RATE_PER_MIN", raising=False)
        get_settings.cache_clear()  # type: ignore[attr-defined]
