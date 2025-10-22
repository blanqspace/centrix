from __future__ import annotations

import pytest

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
    METRICS.update_risk(pnl_day=12.5, pnl_open=-3.2, margin_used_pct=27.5)

    snapshot = snapshot_kpis()
    assert snapshot["open_approvals"] == 3
    assert snapshot["queue_depth"] == 5
    assert snapshot["errors_1m"] == 1
    assert snapshot["alerts_dedup_1m"] == 1
    assert snapshot["alerts_throttle_1m"] == 1
    assert snapshot["risk"]["pnl_day"] == pytest.approx(12.5)
    assert snapshot["risk"]["pnl_open"] == pytest.approx(-3.2)
    assert snapshot["risk"]["margin_used_pct"] == pytest.approx(27.5)

    current["value"] += 61
    snapshot_late = snapshot_kpis()
    assert snapshot_late["errors_1m"] == 0
    assert snapshot_late["alerts_dedup_1m"] == 0
    assert snapshot_late["alerts_throttle_1m"] == 0
    assert snapshot_late["open_approvals"] == 3
    assert snapshot_late["queue_depth"] == 5
    assert snapshot_late["risk"]["pnl_day"] == pytest.approx(12.5)
