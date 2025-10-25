"""Interactive Brokers adapter facade used by Centrix services."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from dotenv import load_dotenv

from centrix.bus import touch_service
from centrix.core.metrics import KPIStore, METRICS
from centrix.settings import AppSettings
from centrix.utils.logging_setup import setup_logging

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_PATH = Path("runtime/logs/ibkr.log")
LOGGER_NAME = "centrix.ibkr"

BASE_LOGGER = logging.getLogger(LOGGER_NAME)
CLIENT_LOG = BASE_LOGGER.getChild("client")
RUNNER_LOG = BASE_LOGGER.getChild("runner")

_SEVERITY_TO_LEVEL = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _ensure_log_handler() -> logging.Logger:
    """Ensure the dedicated IBKR file handler is installed."""

    setup_logging()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    target = LOG_PATH.resolve()
    for handler in BASE_LOGGER.handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                existing = Path(handler.baseFilename).resolve()
            except OSError:
                continue
            if existing == target:
                break
    else:
        handler = logging.FileHandler(target)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.setLevel(logging.INFO)
        BASE_LOGGER.addHandler(handler)

    if BASE_LOGGER.level == logging.NOTSET:
        BASE_LOGGER.setLevel(logging.INFO)
    return BASE_LOGGER


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
        self._hb_lock = threading.Lock()
        self._hb_thread: threading.Thread | None = None
        self._hb_stop: threading.Event | None = None
        self._hb_details: dict[str, Any] | None = None

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

        host = self._settings.tws_host
        port = self._settings.tws_port
        client_id = self._settings.ibkr_client_id
        max_attempts = max(1, retries)

        for attempt in range(1, max_attempts + 1):
            start = self._time()
            try:
                CLIENT_LOG.info(
                    "Connecting to IBKR gateway host=%s port=%s client_id=%s attempt=%s/%s",
                    host,
                    port,
                    client_id,
                    attempt,
                    max_attempts,
                )
                self._gateway.connect(
                    host=host,
                    port=port,
                    client_id=client_id,
                    timeout_ms=self._settings.ibkr_req_timeout_ms,
                )
                if self._gateway.is_connected():
                    self._connected = True
                    self._last_error = None
                    self._start_heartbeat({"host": host, "port": port})
                    CLIENT_LOG.info(
                        "IBKR gateway connection established host=%s port=%s client_id=%s",
                        host,
                        port,
                        client_id,
                    )
                    return True
                CLIENT_LOG.warning(
                    "IBKR gateway reported disconnected state immediately after connect"
                )
                self.record_error(code=-1, message="Gateway reported disconnected state")
            except Exception as exc:  # pragma: no cover - exercised in tests
                CLIENT_LOG.exception("IBKR gateway connection attempt failed: %s", exc)
                self._record_exception(exc)
            finally:
                elapsed_ms = max(0.0, (self._time() - start) * 1000.0)
                self._metrics.update_ibkr_latency(elapsed_ms)

            if attempt < max_attempts:
                self._sleep(retry_delay_sec)

        CLIENT_LOG.error(
            "Failed to connect to IBKR gateway after %s attempt(s) host=%s port=%s client_id=%s",
            max_attempts,
            host,
            port,
            client_id,
        )
        self._connected = False
        self._mark_down({"error": "connect-failed"})
        return False

    def disconnect(self) -> None:
        """Disconnect from the gateway."""

        if not self.enabled or self._gateway is None:
            self._connected = False
            return
        CLIENT_LOG.info("Disconnecting from IBKR gateway")
        self._gateway.disconnect()
        self._connected = False
        self._stop_heartbeat()
        self._mark_down({"reason": "disconnect"})
        CLIENT_LOG.info("IBKR gateway disconnected")

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
        log_level = _SEVERITY_TO_LEVEL.get(info.severity.upper(), logging.ERROR)
        detail = message or info.hint or "unknown IBKR error"
        CLIENT_LOG.log(
            log_level,
            "IBKR gateway error code=%s severity=%s detail=%s",
            code,
            info.severity,
            detail,
        )
        extra = {"error": detail}
        if code != -1:
            extra["code"] = code
        self._mark_down(extra)
        return info

    def map_error(self, code: int) -> IbkrErrorInfo:
        """Return metadata describing the supplied error code."""

        return self._error_map.get(code, DEFAULT_ERROR_INFO)

    def _record_exception(self, exc: Exception) -> IbkrErrorInfo:
        code = getattr(exc, "code", -1)
        message = str(exc)
        return self.record_error(code=code, message=message)

    def _start_heartbeat(self, details: dict[str, Any]) -> None:
        if not details:
            details = {}
        payload = dict(details)
        with self._hb_lock:
            if self._hb_stop is not None:
                self._hb_stop.set()
            if self._hb_thread is not None and self._hb_thread.is_alive():
                self._hb_thread.join(timeout=0.5)
            stop_event = threading.Event()
            self._hb_stop = stop_event
            self._hb_details = payload

            def _heartbeat_loop() -> None:
                try:
                    while not stop_event.is_set():
                        touch_service("ibkr", "up", payload)
                        if stop_event.wait(3.0):
                            break
                except Exception:  # pragma: no cover - defensive
                    CLIENT_LOG.exception("IBKR heartbeat loop failed")

            thread = threading.Thread(
                target=_heartbeat_loop,
                name="centrix-ibkr-heartbeat",
                daemon=True,
            )
            self._hb_thread = thread
            touch_service("ibkr", "up", payload)
            thread.start()

    def _stop_heartbeat(self) -> None:
        with self._hb_lock:
            stop_event = self._hb_stop
            thread = self._hb_thread
            self._hb_stop = None
            self._hb_thread = None
            self._hb_details = None
        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def _mark_down(self, extra: dict[str, Any] | None = None) -> None:
        details: dict[str, Any] = {
            "host": self._settings.tws_host,
            "port": self._settings.tws_port,
        }
        if extra:
            details.update(extra)
        touch_service("ibkr", "down", details)


def _probe_gateway(host: str, port: int, timeout: float) -> tuple[bool, str | None]:
    """Attempt a TCP connection to the IBKR gateway."""

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except OSError as exc:
        return False, str(exc)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        RUNNER_LOG.warning("Invalid %s=%s; falling back to %s", name, value, default)
        return default


def _run_monitor(settings: AppSettings, *, interval: float, timeout: float) -> int:
    host = settings.tws_host
    port = settings.tws_port
    RUNNER_LOG.info(
        "Monitoring IBKR gateway host=%s port=%s client_id=%s",
        host,
        port,
        settings.ibkr_client_id,
    )
    reachable: bool | None = None

    try:
        while True:
            ok, detail = _probe_gateway(host, port, timeout)
            if ok and reachable is not True:
                RUNNER_LOG.info("Gateway reachable at %s:%s", host, port)
            if ok:
                touch_service("ibkr", "up", {"host": host, "port": port})
            elif not ok and reachable is not False:
                RUNNER_LOG.warning(
                    "Gateway unreachable at %s:%s (%s)",
                    host,
                    port,
                    detail or "connection failed",
                )
            if not ok:
                down_payload = {"host": host, "port": port}
                if detail:
                    down_payload["error"] = detail
                touch_service("ibkr", "down", down_payload)
            reachable = ok
            time.sleep(interval)
    except KeyboardInterrupt:
        RUNNER_LOG.info("IBKR adapter interrupted; shutting down.")
        return 0
    except Exception:  # pragma: no cover - defensive
        RUNNER_LOG.exception("Unhandled error in IBKR adapter loop")
        return 1


def main() -> int:
    """Run the standalone IBKR adapter monitor."""

    load_dotenv()
    _ensure_log_handler()

    settings = AppSettings()
    RUNNER_LOG.info(
        "IBKR adapter starting (enabled=%s)",
        settings.ibkr_enabled,
    )

    if not settings.ibkr_enabled:
        touch_service(
            "ibkr",
            "down",
            {"reason": "disabled", "host": settings.tws_host, "port": settings.tws_port},
        )
        RUNNER_LOG.warning("IBKR adapter disabled via IBKR_ENABLED=0; sleeping.")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            RUNNER_LOG.info("Shutdown requested while adapter disabled.")
            return 0

    interval = max(1.0, _env_float("IBKR_HEALTH_INTERVAL", 3.0))
    timeout = max(0.5, _env_float("IBKR_CONNECT_TIMEOUT", 2.5))
    result = _run_monitor(settings, interval=interval, timeout=timeout)
    touch_service(
        "ibkr",
        "down",
        {"reason": "adapter-stop", "host": settings.tws_host, "port": settings.tws_port},
    )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
