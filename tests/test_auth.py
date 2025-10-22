from __future__ import annotations

import asyncio
import base64
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from centrix.core.logging import ensure_runtime_dirs
from centrix.core.metrics import METRICS
from centrix.core.orders import clear_orders
from centrix.ipc.bus import Bus
from centrix.settings import get_settings


def _load_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
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
    server = importlib.import_module("centrix.dashboard.server")
    Bus(server.settings.ipc_db)
    return server


def test_api_status_public_when_auth_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _load_server(monkeypatch, tmp_path, auth_required=False)
    identity = server._require_token(SimpleNamespace(headers={}))
    assert identity.role == "admin"

    response = asyncio.run(server.api_status(_identity=identity))
    assert response.status_code == 200
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["ok"] is True


def test_api_status_requires_token_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = "abc123"
    server = _load_server(
        monkeypatch,
        tmp_path,
        token=token,
        role_map={"U1": "operator"},
        auth_required=True,
    )

    with pytest.raises(server.DashboardUnauthorized) as err:
        server._require_token(SimpleNamespace(headers={}))
    assert err.value.reason == "missing_or_bad_token"

    identity = server._require_token(SimpleNamespace(headers={"X-Dashboard-Token": token}))
    assert identity.role == "admin"
    success = asyncio.run(server.api_status(_identity=identity))
    assert success.status_code == 200

    basic_header = "Basic " + base64.b64encode(b"U1:operator").decode("ascii")
    basic_identity = server._require_token(SimpleNamespace(headers={"Authorization": basic_header}))
    assert basic_identity.role == "operator"


def test_websocket_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = "topsecret"
    server = _load_server(
        monkeypatch,
        tmp_path,
        token=token,
        role_map={"U2": "admin"},
        auth_required=True,
    )

    ws_ok = SimpleNamespace(
        query_params={"token": token},
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    identity = server._ws_authorized(ws_ok)
    assert identity.role == "admin"

    ws_basic = SimpleNamespace(
        query_params={},
        headers={"Authorization": "Basic " + base64.b64encode(b"U2:admin").decode("ascii")},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    basic_identity = server._ws_authorized(ws_basic)
    assert basic_identity.user == "U2"

    ws_bad = SimpleNamespace(query_params={}, headers={}, client=SimpleNamespace(host="127.0.0.1"))
    with pytest.raises(server.DashboardUnauthorized) as err:
        server._ws_authorized(ws_bad)
    assert err.value.reason == "missing_or_bad_token"
