"""Interactive Brokers adapter facade used by Centrix services."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from centrix.core.metrics import KPIStore, METRICS
from centrix.settings import AppSettings


class IbkrGateway(Protocol):
    """Protocol describing the gateway surface the adapter relies on."""

    def connect(self, host: str, port: int, client_id: int, timeout_ms: int) -> bool:
        ...

    def disconnect(self) -> None:
        ...

    def is_connected(self) -> bool:
        ...

    def health(self) -> dict[str, Any]:
        ...

    def fetch_account(self) -> dict[str, Any]:
        ...

    def fetch_positions(self) -> list[dict[str, Any]]:
        ...

    def stream_market_data(self, symbol: str, snapshot_sec: int) -> dict[str, Any]:
        ...

    def send_order(self, contract: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
        ...


@dataclass(frozen=True, slots=True)
class IbkrErrorInfo:
    """Metadata describing an IBKR failure."""

    severity: str
    hint: str


DEFAULT_ERROR_INFO = IbkrErrorInfo(severity="ERROR", hint="Unknown IBKR error")

DEFAULT_ERROR_MAP: dict[int, IbkrErrorInfo] = {
    10167: IbkrErrorInfo(severity="WARN", hint="Market data pacing violation"),
    10168: IbkrErrorInfo(severity="WARN", hint="Max rate of messages exceeded"),
    1100: IbkrErrorInfo(severity="WARN", hint="Connectivity between IB and TWS is broken"),
    1101: IbkrErrorInfo(severity="INFO", hint="Connectivity restored"),
    1300: IbkrErrorInfo(severity="ERROR", hint="TWS returned a generic error"),
}

DEFAULT_PACING_CODES: frozenset[int] = frozenset({10167, 10168})


class IbkrClient:
    """High-level wrapper around IBKR transport implementations."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        gateway: IbkrGateway | None = None,
        metrics: KPIStore | None = None,
        error_map: dict[int, IbkrErrorInfo] | None = None,
        pacing_codes: set[int] | frozenset[int] | None = None,
        time_provider: Callable[[], float] = time.time,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._gateway = gateway
        self._metrics = metrics or METRICS
        self._error_map = dict(DEFAULT_ERROR_MAP)
        if error_map:
            self._error_map.update(error_map)
        self._pacing_codes = set(DEFAULT_PACING_CODES)
        if pacing_codes:
            self._pacing_codes.update(pacing_codes)
        self._time = time_provider
        self._sleep = sleep_fn
        self._connected = False
        self._last_error: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        """Indicate whether IBKR integration is enabled via settings."""

        return bool(self._settings.ibkr_enabled)

    def is_connected(self) -> bool:
        """Return the most recent connection state."""

        if not self.enabled:
            return False
        if self._gateway is None:
            return False
        if not self._connected:
            return False
        return self._gateway.is_connected()

    def connect(self, retries: int = 3, retry_delay_sec: float = 0.5) -> bool:
        """Try connecting to the configured IBKR endpoint with retry support."""

        if not self.enabled:
            self._connected = False
            return False
        if self._gateway is None:
            raise RuntimeError("IBKR gateway not provided")

        for attempt in range(1, max(1, retries) + 1):
            start = self._time()
            try:
                self._gateway.connect(
                    host=self._settings.tws_host,
                    port=self._settings.tws_port,
                    client_id=self._settings.ibkr_client_id,
                    timeout_ms=self._settings.ibkr_req_timeout_ms,
                )
                if self._gateway.is_connected():
                    self._connected = True
                    self._last_error = None
                    return True
                self.record_error(code=-1, message="Gateway reported disconnected state")
            except Exception as exc:  # pragma: no cover - exercised in tests
                self._record_exception(exc)
            finally:
                elapsed_ms = max(0.0, (self._time() - start) * 1000.0)
                self._metrics.update_ibkr_latency(elapsed_ms)

            if attempt < retries:
                self._sleep(retry_delay_sec)

        self._connected = False
        return False

    def disconnect(self) -> None:
        """Disconnect from the gateway."""

        if not self.enabled or self._gateway is None:
            self._connected = False
            return
        self._gateway.disconnect()
        self._connected = False

    def health(self) -> dict[str, Any]:
        """Return a health snapshot for diagnostics endpoints."""

        if not self.enabled:
            return {"enabled": False, "connected": False, "latency_ms_median": None}

        gateway_health: dict[str, Any] = {}
        if self._gateway is not None:
            gateway_health = dict(self._gateway.health())
            gateway_health.setdefault("connected", self._gateway.is_connected())
        health_snapshot = {
            "enabled": True,
            "connected": self.is_connected(),
            "latency_ms_median": self._metrics.snapshot().get("ibkr_latency_ms_median"),
            "last_error": self._last_error,
        }
        return {**gateway_health, **health_snapshot}

    def account(self) -> dict[str, Any]:
        """Return the current account snapshot or an empty payload when disabled."""

        if not self.enabled or self._gateway is None:
            return {}
        return dict(self._gateway.fetch_account())

    def positions(self) -> list[dict[str, Any]]:
        """Return open positions from the gateway."""

        if not self.enabled or self._gateway is None:
            return []
        positions = self._gateway.fetch_positions()
        return [dict(item) for item in positions]

    def watch(self, symbol: str) -> dict[str, Any]:
        """Request a market data snapshot for the provided symbol."""

        if not self.enabled or self._gateway is None:
            return {"symbol": symbol, "snapshot": None}
        start = self._time()
        snapshot = self._gateway.stream_market_data(symbol, self._settings.ibkr_md_snapshot_sec)
        elapsed_ms = max(0.0, (self._time() - start) * 1000.0)
        self._metrics.update_ibkr_latency(elapsed_ms)
        return dict(snapshot)

    def send_order(self, contract: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
        """Submit a synthetic order to the gateway."""

        if not self.enabled or self._gateway is None:
            return {"status": "disabled"}
        start = self._time()
        result = self._gateway.send_order(contract, order)
        elapsed_ms = max(0.0, (self._time() - start) * 1000.0)
        self._metrics.update_ibkr_latency(elapsed_ms)
        return dict(result)

    def record_error(self, *, code: int, message: str | None = None) -> IbkrErrorInfo:
        """Track a gateway error and update metrics counters."""

        info = self.map_error(code)
        self._metrics.increment_counter("ibkr_errors_total", 1)
        if code in self._pacing_codes:
            self._metrics.increment_counter("ibkr_pacing_violations_total", 1)
        self._last_error = {
            "code": code,
            "message": message,
            "severity": info.severity,
            "hint": info.hint,
        }
        return info

    def map_error(self, code: int) -> IbkrErrorInfo:
        """Return metadata describing the supplied error code."""

        return self._error_map.get(code, DEFAULT_ERROR_INFO)

    def _record_exception(self, exc: Exception) -> IbkrErrorInfo:
        code = getattr(exc, "code", -1)
        message = str(exc)
        return self.record_error(code=code, message=message)
