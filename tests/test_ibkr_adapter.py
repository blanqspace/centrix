from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from centrix.adapters.ibkr import IbkrClient
from centrix.core.metrics import METRICS, snapshot_kpis
from centrix.settings import AppSettings
from tests.fakes.fake_ibkr import FakeIbkrGateway


class SequenceClock:
    """Deterministic clock returning elapsed times for adapter latency tracking."""

    def __init__(self, durations_ms: Iterable[float]) -> None:
        self._durations = iter(durations_ms)
        self._current = 0.0
        self._phase_start = True
        self._next_duration = 0.0

    def __call__(self) -> float:
        if self._phase_start:
            try:
                self._next_duration = next(self._durations) / 1000.0
            except StopIteration:
                self._next_duration = 0.0
            self._phase_start = False
            return self._current
        self._current += self._next_duration
        self._phase_start = True
        return self._current


def test_ibkr_connect_retry_updates_metrics() -> None:
    METRICS.reset()
    clock = SequenceClock([2.0, 6.0, 10.0])
    fake = FakeIbkrGateway(connection_failures=[10167, 1300], time_provider=lambda: 0.0)
    settings = AppSettings(ibkr_enabled=True)
    client = IbkrClient(settings=settings, gateway=fake, time_provider=clock, sleep_fn=lambda _: None)

    assert client.connect(retries=3, retry_delay_sec=0.0) is True
    assert fake.connection_attempts == 3
    assert client.is_connected() is True

    snapshot = snapshot_kpis()
    assert snapshot["counters"]["ibkr_errors_total"] == 2
    assert snapshot["counters"]["ibkr_pacing_violations_total"] == 1
    assert snapshot["ibkr_latency_ms_median"] == pytest.approx(6.0)

    health = client.health()
    assert health["enabled"] is True
    assert health["connected"] is True
    assert health["connection_attempts"] == 3
    assert health["last_error"] is None


def test_ibkr_account_and_positions_passthrough() -> None:
    METRICS.reset()
    account_snapshot = {"cash": 200_000.0, "equity": 250_000.0}
    positions = [
        {"symbol": "ES", "quantity": 2, "avg_price": 5250.5},
        {"symbol": "NQ", "quantity": -1, "avg_price": 18_250.0},
    ]
    clock = SequenceClock([1.0])
    fake = FakeIbkrGateway(
        connection_failures=[],
        account_snapshot=account_snapshot,
        positions=positions,
        time_provider=lambda: 0.0,
    )
    settings = AppSettings(ibkr_enabled=True)
    client = IbkrClient(settings=settings, gateway=fake, time_provider=clock)

    assert client.connect(retries=1) is True

    account = client.account()
    assert account == account_snapshot
    positions_snapshot = client.positions()
    assert positions_snapshot == positions
    assert positions_snapshot is not positions
    positions_snapshot[0]["quantity"] = 99
    assert positions[0]["quantity"] == 2


def test_ibkr_latency_window_limits_samples() -> None:
    METRICS.reset()

    class LatencyGateway:
        def __init__(self) -> None:
            self._connected = True

        def connect(self, *, host: str, port: int, client_id: int, timeout_ms: int) -> bool:
            self._connected = True
            return True

        def disconnect(self) -> None:
            self._connected = False

        def is_connected(self) -> bool:
            return self._connected

        def health(self) -> dict[str, Any]:
            return {"connected": self._connected}

        def fetch_account(self) -> dict[str, Any]:
            return {}

        def fetch_positions(self) -> list[dict[str, Any]]:
            return []

        def stream_market_data(self, symbol: str, snapshot_sec: int) -> dict[str, Any]:
            return {"symbol": symbol, "snapshot_sec": snapshot_sec}

        def send_order(self, contract: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
            return {"status": "ok"}

    durations = [float(i) for i in range(1, 56)]
    clock = SequenceClock(durations)
    gateway = LatencyGateway()
    settings = AppSettings(ibkr_enabled=True)
    client = IbkrClient(settings=settings, gateway=gateway, time_provider=clock)

    for _ in range(55):
        client.watch("AAPL")

    snapshot = snapshot_kpis()
    assert snapshot["ibkr_latency_ms_median"] == pytest.approx(30.5)


def test_ibkr_error_map_and_health() -> None:
    METRICS.reset()

    class HealthGateway:
        def __init__(self) -> None:
            self._connected = False

        def connect(self, *, host: str, port: int, client_id: int, timeout_ms: int) -> bool:
            self._connected = True
            return True

        def disconnect(self) -> None:
            self._connected = False

        def is_connected(self) -> bool:
            return self._connected

        def health(self) -> dict[str, Any]:
            return {"uptime": 42}

        def fetch_account(self) -> dict[str, Any]:
            return {}

        def fetch_positions(self) -> list[dict[str, Any]]:
            return []

        def stream_market_data(self, symbol: str, snapshot_sec: int) -> dict[str, Any]:
            return {"symbol": symbol}

        def send_order(self, contract: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
            return {"status": "ok"}

    settings = AppSettings(ibkr_enabled=True)
    client = IbkrClient(settings=settings, gateway=HealthGateway())

    info = client.map_error(10167)
    assert info.severity == "WARN"
    assert "pacing" in info.hint.lower()

    default_info = client.map_error(99999)
    assert default_info.severity == "ERROR"

    meta = client.record_error(code=10167, message="rate limited")
    assert meta.severity == "WARN"

    snapshot = snapshot_kpis()
    assert snapshot["counters"]["ibkr_errors_total"] == 1
    assert snapshot["counters"]["ibkr_pacing_violations_total"] == 1

    health = client.health()
    assert health["enabled"] is True
    assert health["connected"] is False
    assert health["uptime"] == 42
    assert health["latency_ms_median"] is None
    assert health["last_error"]["code"] == 10167
    assert "rate limited" in health["last_error"]["message"]
