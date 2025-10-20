from __future__ import annotations

from centrix.core.metrics import METRICS, snapshot_kpis


def test_metrics_sliding_window(monkeypatch) -> None:
    METRICS.reset()
    current = {"value": 1_000_000.0}

    def fake_time() -> float:
        return current["value"]

    monkeypatch.setattr("centrix.core.metrics.time.time", fake_time)

    METRICS.update_open_approvals(3)
    METRICS.update_queue_depth(5)

    METRICS.record_error()
    METRICS.record_alert_dedup()
    METRICS.record_alert_throttle()

    snapshot = snapshot_kpis()
    assert snapshot["open_approvals"] == 3
    assert snapshot["queue_depth"] == 5
    assert snapshot["errors_1m"] == 1
    assert snapshot["alerts_dedup_1m"] == 1
    assert snapshot["alerts_throttle_1m"] == 1

    current["value"] += 61
    snapshot_late = snapshot_kpis()
    assert snapshot_late["errors_1m"] == 0
    assert snapshot_late["alerts_dedup_1m"] == 0
    assert snapshot_late["alerts_throttle_1m"] == 0
    assert snapshot_late["open_approvals"] == 3
    assert snapshot_late["queue_depth"] == 5
