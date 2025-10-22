from __future__ import annotations

import asyncio
import base64
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import ClientDisconnect

from centrix.core.logging import ensure_runtime_dirs
from centrix.core.metrics import METRICS
from centrix.core.orders import clear_orders, list_orders
from centrix.ipc.bus import Bus
from centrix.settings import get_settings


def _load_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    token: str | None = None,
    role_map: dict[str, str] | None = None,
    auth_required: bool | None = None,
):
    monkeypatch.chdir(tmp_path)
    ensure_runtime_dirs()
    clear_orders()
    METRICS.reset()
    if role_map is None:
        monkeypatch.delenv("SLACK_ROLE_MAP", raising=False)
    else:
        monkeypatch.setenv("SLACK_ROLE_MAP", json.dumps(role_map))
    if token is None:
        monkeypatch.delenv("DASHBOARD_AUTH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", token)
    if auth_required is None:
        monkeypatch.delenv("DASHBOARD_AUTH_REQUIRED", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_AUTH_REQUIRED", "1" if auth_required else "0")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    sys.modules.pop("centrix.dashboard.server", None)
    sys.modules.pop("centrix.settings", None)
    server_module = importlib.import_module("centrix.dashboard.server")
    Bus(server_module.settings.ipc_db)
    return server_module


def test_status_payload_and_control(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = _load_server(monkeypatch, tmp_path, token=None)
    server._record_dashboard_heartbeat()
    identity = server.ControlIdentity(principal="test", user="tester", role="admin")

    status = server.status_payload()
    required = {"mode", "paused", "connectivity", "risk", "orders_open", "events", "clients", "build", "kpi", "ok"}
    assert required.issubset(status.keys())
    assert status["last_action"] is None
    assert status["connectivity"].get("dashboard") == "up"

    pause_snapshot = server.api_control("pause", identity=identity, body={})
    assert pause_snapshot["paused"] is True
    assert pause_snapshot["last_action"]["action"] == "pause"
    assert pause_snapshot["last_action"]["user"] == "tester"

    mode_snapshot = server.api_control("mode", identity=identity, body={"value": "real"})
    assert mode_snapshot["mode"] == "real"

    order_snapshot = server.api_control(
        "test-order",
        identity=identity,
        body={"symbol": "DEMO", "qty": 1, "px": 0},
    )
    assert list_orders()[0]["symbol"] == "DEMO"
    assert any(order["symbol"] == "DEMO" for order in order_snapshot["orders_open"])
    counters = (order_snapshot["kpi"].get("counters") or {})
    assert counters.get("control.actions_total", 0) >= 3

    monkeypatch.setattr(
        server,
        "_restart_service",
        lambda name: {"service": name, "stopped": True, "started": True},
    )
    restart_snapshot = server.api_control("restart", identity=identity, body={"service": "worker"})
    restart_details = restart_snapshot["last_action"]["details"]["restart"]
    if isinstance(restart_details, list):
        restart_details = restart_details[0]
    assert restart_details["service"] == "worker"

    status_after = server.status_payload()
    assert status_after["last_action"]["action"] == "restart"


def test_token_and_ws_checks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = "secret"
    server = _load_server(monkeypatch, tmp_path, token=token, auth_required=True)

    request = SimpleNamespace(headers={"X-Dashboard-Token": token})
    identity = server._require_token(request)
    assert identity.principal == "dashboard"
    assert identity.role == "admin"

    bad_request = SimpleNamespace(headers={})
    with pytest.raises(server.DashboardUnauthorized):
        server._require_token(bad_request)

    dummy_ws = SimpleNamespace(
        query_params={"token": token},
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    ws_identity = server._ws_authorized(dummy_ws)
    assert ws_identity.role == "admin"

    dummy_ws_bad = SimpleNamespace(
        query_params={},
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    with pytest.raises(server.DashboardUnauthorized):
        server._ws_authorized(dummy_ws_bad)

    server = _load_server(
        monkeypatch,
        tmp_path,
        token=None,
        role_map={"U1": "operator"},
        auth_required=True,
    )
    assert server.settings.slack_role_map.get("U1") == "operator"
    basic = "Basic " + base64.b64encode(b"U1:operator").decode("ascii")
    basic_request = SimpleNamespace(headers={"Authorization": basic})
    basic_identity = server._require_token(basic_request)
    assert basic_identity.principal == "slack"
    assert basic_identity.user == "U1"
    assert basic_identity.role == "operator"


def test_control_endpoint_handles_client_disconnect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _load_server(monkeypatch, tmp_path, token=None, auth_required=False)

    class DisconnectingRequest:
        async def json(self) -> dict[str, str]:
            raise ClientDisconnect()

    identity = server.ControlIdentity(principal="test", user="tester", role="admin")
    response = asyncio.run(server.control_endpoint(DisconnectingRequest(), identity))
    assert response.status_code == 499
    payload = json.loads(response.body.decode("utf-8"))
    assert payload == {"ok": False, "error": "client_disconnected"}
