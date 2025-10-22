from __future__ import annotations

import base64
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Tuple

import pytest

pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from centrix.core.logging import ensure_runtime_dirs
from centrix.core.metrics import METRICS
from centrix.core.orders import clear_orders
from centrix.ipc.bus import Bus
from centrix.settings import get_settings

def _server_and_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    role_map: dict[str, str],
    token: str | None = None,
) -> Tuple[Any, TestClient]:
    monkeypatch.chdir(tmp_path)
    ensure_runtime_dirs()
    clear_orders()
    METRICS.reset()
    get_settings.cache_clear()  # type: ignore[attr-defined]
    if token is None:
        monkeypatch.delenv("DASHBOARD_AUTH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", token)
    monkeypatch.setenv("SLACK_ROLE_MAP", json.dumps(role_map))
    monkeypatch.setenv("SLACK_SIMULATION", "1")
    sys.modules.pop("centrix.dashboard.server", None)
    server = importlib.import_module("centrix.dashboard.server")
    Bus(server.settings.ipc_db)
    return server, TestClient(server.app)


def _basic_headers(user: str, secret: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{secret}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def test_status_requires_basic_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server, client = _server_and_client(monkeypatch, tmp_path, role_map={"U1": "operator"})

    response = client.get("/api/status")
    assert response.status_code == 401

    headers = _basic_headers("U1", "operator")
    response_ok = client.get("/api/status", headers=headers)
    assert response_ok.status_code == 200
    payload = response_ok.json()
    assert payload["ok"] is True
    assert payload["mode"] in {"mock", "real"}
    assert "risk" in payload
    assert payload["last_action"] is None


def test_control_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server, client = _server_and_client(monkeypatch, tmp_path, role_map={"U9": "admin"})
    headers = _basic_headers("U9", "admin")

    pause_resp = client.post("/api/control", json={"action": "pause"}, headers=headers)
    assert pause_resp.status_code == 200
    pause_payload = pause_resp.json()
    assert pause_payload["paused"] is True
    assert pause_payload["last_action"]["action"] == "pause"
    assert pause_payload["last_action"]["user"] == "U9"

    mode_resp = client.post(
        "/api/control", json={"action": "mode", "value": "real"}, headers=headers
    )
    assert mode_resp.status_code == 200
    assert mode_resp.json()["mode"] == "real"

    status_resp = client.get("/api/status", headers=headers)
    assert status_resp.status_code == 200
    status_payload = status_resp.json()
    assert status_payload["paused"] is True
    assert status_payload["last_action"]["action"] in {"mode", "pause"}


def test_control_forbidden_for_insufficient_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server, client = _server_and_client(monkeypatch, tmp_path, role_map={"UV": "observer"})
    headers = _basic_headers("UV", "observer")

    response = client.post("/api/control", json={"action": "pause"}, headers=headers)
    assert response.status_code == 403
