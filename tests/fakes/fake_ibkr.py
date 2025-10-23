from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List


class FakeIbkrError(RuntimeError):
    """Exception carrying an IBKR-style error code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class FakeOrder:
    """Captured order payload."""

    order_id: int
    contract: dict[str, Any]
    order: dict[str, Any]
    timestamp: float


class FakeIbkrGateway:
    """Deterministic fake IBKR gateway used for adapter tests."""

    def __init__(
        self,
        *,
        connection_failures: Iterable[int] | None = None,
        account_snapshot: dict[str, Any] | None = None,
        positions: List[dict[str, Any]] | None = None,
        market_data: Dict[str, List[dict[str, Any]]] | None = None,
        time_provider: Callable[[], float],
    ) -> None:
        self._connection_failures = list(connection_failures or [])
        self._account_snapshot = account_snapshot or {
            "cash": 125_000.0,
            "equity": 150_000.0,
            "leverage": 1.2,
        }
        self._positions = [dict(pos) for pos in (positions or [])]
        self._positions = self._positions or [
            {"symbol": "AAPL", "quantity": 10, "avg_price": 170.25},
            {"symbol": "MSFT", "quantity": 5, "avg_price": 320.10},
        ]
        self._market_data = {symbol: [dict(frame) for frame in frames] for symbol, frames in (market_data or {}).items()}
        if not self._market_data:
            self._market_data = {
                "AAPL": [{"symbol": "AAPL", "bid": 170.0, "ask": 170.1}],
            }
        self._market_cursor: dict[str, int] = {symbol: 0 for symbol in self._market_data}
        self._connected = False
        self._time = time_provider
        self.connection_attempts = 0
        self.last_connection_params: dict[str, Any] | None = None
        self.orders: list[FakeOrder] = []
        self._next_order_id = 1

    def connect(self, *, host: str, port: int, client_id: int, timeout_ms: int) -> bool:
        self.connection_attempts += 1
        if self._connection_failures:
            code = self._connection_failures.pop(0)
            raise FakeIbkrError(code=code, message=f"forced failure: {code}")
        self._connected = True
        self.last_connection_params = {
            "host": host,
            "port": port,
            "client_id": client_id,
            "timeout_ms": timeout_ms,
        }
        return True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def health(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "connection_attempts": self.connection_attempts,
        }

    def fetch_account(self) -> dict[str, Any]:
        return dict(self._account_snapshot)

    def fetch_positions(self) -> list[dict[str, Any]]:
        return [dict(position) for position in self._positions]

    def stream_market_data(self, symbol: str, snapshot_sec: int) -> dict[str, Any]:
        frames = self._market_data.get(symbol)
        if not frames:
            payload = {"symbol": symbol, "snapshot_sec": snapshot_sec, "timestamp": self._time()}
            return payload
        index = self._market_cursor.get(symbol, 0) % len(frames)
        self._market_cursor[symbol] = index + 1
        payload = dict(frames[index])
        payload.setdefault("symbol", symbol)
        payload["snapshot_sec"] = snapshot_sec
        payload["timestamp"] = self._time()
        return payload

    def send_order(self, contract: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
        order_id = self._next_order_id
        self._next_order_id += 1
        snapshot = FakeOrder(order_id=order_id, contract=dict(contract), order=dict(order), timestamp=self._time())
        self.orders.append(snapshot)
        return {
            "order_id": snapshot.order_id,
            "status": "accepted",
            "timestamp": snapshot.timestamp,
        }

