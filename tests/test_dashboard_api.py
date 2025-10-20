from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from centrix.core.logging import ensure_runtime_dirs
from centrix.core.metrics import METRICS
from centrix.core.orders import clear_orders, list_orders
from centrix.ipc.bus import Bus
from centrix.settings import get_settings


def _load_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, token: str | None = None):
    monkeypatch.chdir(tmp_path)
    ensure_runtime_dirs()
    clear_orders()
    METRICS.reset()
    get_settings.cache_clear()  # type: ignore[attr-defined]
    if token is None:
        monkeypatch.delenv("DASHBOARD_AUTH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", token)
    sys.modules.pop("centrix.dashboard.server", None)
    server_module = importlib.import_module("centrix.dashboard.server")
    Bus(server_module.settings.ipc_db)
    return server_module


def test_status_payload_and_control(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = _load_server(monkeypatch, tmp_path, token=None)

    status = server.status_payload()
    required = {"state", "services", "kpi", "orders", "events", "clients", "build"}
    assert required.issubset(status.keys())

    pause = server._handle_control_action({"action": "pause"})
    assert pause["state"]["paused"] is True

    mode = server._handle_control_action({"action": "mode", "value": "real"})
    assert mode["state"]["mode"] == "real"

    order = server._handle_control_action(
        {"action": "test-order", "symbol": "DEMO", "qty": 1, "px": 0}
    )
    assert list_orders()[0]["symbol"] == "DEMO"
    assert order["order"]["source"] == "dashboard"

    monkeypatch.setattr(server, "_stop_service", lambda name: True)
    monkeypatch.setattr(server, "_start_service", lambda name: True)
    restart = server._handle_control_action({"action": "restart", "service": "worker"})
    assert restart["restart"]["service"] == "worker"

    assert METRICS.get_counter("control.actions_total") >= 4


def test_token_and_ws_checks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = "secret"
    server = _load_server(monkeypatch, tmp_path, token=token)

    request = SimpleNamespace(headers={"X-Dashboard-Token": token})
    server._require_token(request)  # should not raise

    bad_request = SimpleNamespace(headers={})
    with pytest.raises(HTTPException):
        server._require_token(bad_request)

    dummy_ws = SimpleNamespace(
        query_params={"token": token},
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    assert server._ws_authorized(dummy_ws) is True

    dummy_ws_bad = SimpleNamespace(
        query_params={},
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    assert server._ws_authorized(dummy_ws_bad) is False
