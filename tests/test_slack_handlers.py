from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from centrix.core.logging import ensure_runtime_dirs
from centrix.core.metrics import METRICS
from centrix.core.orders import clear_orders
from centrix.ipc.bus import Bus
from centrix.settings import get_settings


def _reload_slack(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    for module in ["centrix.services.slack", "centrix.settings"]:
        sys.modules.pop(module, None)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    importlib.import_module("centrix.settings")
    return importlib.import_module("centrix.services.slack")


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


def test_cx_ack_fast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    slack = _reload_slack(monkeypatch, tmp_path)
    ack_deltas: list[float] = []
    responses: list[dict[str, Any]] = []
    start = time.perf_counter()

    def ack(payload: dict[str, Any]) -> None:
        assert payload["response_type"] == "ephemeral"
        ack_deltas.append(time.perf_counter() - start)

    def handler(*, user_id: str, channel: str, text: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return (
            {"response_type": "ephemeral", "text": "ok"},
            {"ok": True, "action": "status", "http_status": 200},
        )

    def respond(message: dict[str, Any]) -> None:
        responses.append(message)

    body = {"user_id": "U1", "channel_id": "C1", "text": "status"}
    slack.process_slash_command_request(body, ack, respond, handler=handler)
    assert ack_deltas and ack_deltas[0] < 1.0
    assert responses == [{"response_type": "ephemeral", "text": "ok"}]


def test_cx_role_denied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SLACK_ROLE_MAP", json.dumps({"U1": "observer"}))
    slack = _reload_slack(monkeypatch, tmp_path)
    monkeypatch.setattr(
        slack,
        "_control_api_call",
        lambda *args, **kwargs: pytest.fail("control call should not execute"),
    )

    ack_calls: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []

    slack.process_slash_command_request(
        {"user_id": "U1", "channel_id": "C1", "text": "pause"},
        lambda payload: ack_calls.append(payload),
        responses.append,
    )

    assert ack_calls and ack_calls[0]["response_type"] == "ephemeral"
    assert "access denied" in responses[-1]["text"]


def test_cx_status_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SLACK_ROLE_MAP", json.dumps({"U2": "admin"}))
    slack = _reload_slack(monkeypatch, tmp_path)

    snapshot = {
        "mode": "mock",
        "paused": False,
        "connectivity": {"slack": "up"},
        "risk": {"pnl_day": 1.23, "pnl_open": 0.0, "margin_used_pct": 5.0},
    }

    monkeypatch.setattr(
        slack,
        "_status_api_call",
        lambda user_id, role: (True, snapshot, "", 200),
    )

    responses: list[dict[str, Any]] = []
    slack.process_slash_command_request(
        {"user_id": "U2", "channel_id": "C1", "text": "status"},
        lambda _: None,
        responses.append,
    )

    assert responses
    payload = responses[-1]
    assert payload["response_type"] == "ephemeral"
    assert any(block.get("type") == "section" for block in payload.get("blocks", []))


def test_cx_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SLACK_ROLE_MAP", json.dumps({"U3": "admin"}))
    slack = _reload_slack(monkeypatch, tmp_path)

    monkeypatch.setattr(
        slack,
        "_status_api_call",
        lambda user_id, role: (False, None, "timeout", 504),
    )

    responses: list[dict[str, Any]] = []
    slack.process_slash_command_request(
        {"user_id": "U3", "channel_id": "C1", "text": "status"},
        lambda _: None,
        responses.append,
    )

    assert "dashboard timeout" in responses[-1]["text"]


def test_cx_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    slack = _reload_slack(monkeypatch, tmp_path)
    responses: list[dict[str, Any]] = []
    slack.process_slash_command_request(
        {"user_id": "U4", "channel_id": "C1", "text": "foo"},
        lambda _: None,
        responses.append,
    )
    assert "Centrix commands" in responses[-1]["text"]


def test_action_request_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    slack = _reload_slack(monkeypatch, tmp_path)
    ack_calls: list[str] = []
    responses: list[str] = []

    def ack() -> None:
        ack_calls.append("ack")

    def handler(action_id: str, *, user: str, order_id: int, token: str | None) -> dict[str, Any]:
        assert action_id == "confirm"
        assert user == "U9"
        assert order_id == 42
        assert token == "abc"
        assert ack_calls == ["ack"]
        return {"text": "ok"}

    body = {"user": {"id": "U9"}, "message": {"metadata": {"order_id": "42", "token": "abc"}}}
    slack.process_action_request("confirm", body, ack, responses.append, handler=handler)
    assert responses == ["ok"]


def test_action_request_missing_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    slack = _reload_slack(monkeypatch, tmp_path)
    responses: list[str] = []

    def ack() -> None:
        pass

    def handler(action_id: str, *, user: str, order_id: int, token: str | None) -> dict[str, Any]:
        assert action_id == "reject"
        assert user == "U5"
        assert order_id == 0
        assert token is None
        return {"text": "handled"}

    body = {"user": {"id": "U5"}, "message": {}}
    slack.process_action_request("reject", body, ack, responses.append, handler=handler)
    assert responses == ["handled"]


def test_action_request_handler_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    slack = _reload_slack(monkeypatch, tmp_path)
    responses: list[str] = []

    def ack() -> None:
        pass

    def handler(action_id: str, *, user: str, order_id: int, token: str | None) -> dict[str, Any]:
        raise RuntimeError("boom")

    slack.process_action_request("confirm", {}, ack, responses.append, handler=handler)
    assert responses[-1] == "error"


def test_control_pause_emits_slack_notify(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = _load_server(monkeypatch, tmp_path, token=None, auth_required=False)
    bus = Bus(server.settings.ipc_db)

    identity = server.ControlIdentity(principal="test", user="tester", role="admin")
    server.api_control("pause", identity=identity, body={})

    events = bus.tail_events(topic="slack.notify")
    assert events, "expected slack.notify events"
    notify_event = events[-1]
    payload = notify_event["data"]
    assert payload["action"] == "pause"
    assert payload["status"] == "ok"
    assert payload["type"] == "control-action"


def test_dispatch_notifications_respects_channel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SLACK_CHANNEL_CONTROL", "#control")
    monkeypatch.setenv("SLACK_CHANNEL_LOGS", "#logs")
    monkeypatch.setenv("SLACK_ENABLED", "1")
    monkeypatch.setenv("SLACK_SIMULATION", "1")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    slack = _reload_slack(monkeypatch, tmp_path)
    get_settings.cache_clear()  # type: ignore[attr-defined]

    bus = Bus(get_settings().ipc_db)
    bus.emit(
        "slack.notify",
        "INFO",
        {
            "type": "control-action",
            "action": "pause",
            "status": "ok",
            "ts": "2024-01-01T00:00:00Z",
            "user": "U42",
            "role": "admin",
        },
    )

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def post_message(self, channel: str, text: str, **_: Any) -> dict[str, Any]:
            self.calls.append((channel, text))
            return {"ok": True}

    recorder = Recorder()
    slack.dispatch_notifications(recorder, bus, last_event_id=0)
    assert recorder.calls
    channel, text = recorder.calls[-1]
    assert channel == "#control"
    assert "Control pause" in text


def test_selftest_cycle_updates_bus(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SLACK_CHANNEL_LOGS", "#logs")
    monkeypatch.setenv("SLACK_ENABLED", "1")
    monkeypatch.setenv("SLACK_SIMULATION", "1")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    slack = _reload_slack(monkeypatch, tmp_path)
    get_settings.cache_clear()  # type: ignore[attr-defined]

    bus = Bus(get_settings().ipc_db)

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def post_message(self, channel: str, text: str, **_: Any) -> dict[str, Any]:
            self.calls.append((channel, text))
            return {"ok": True}

    recorder = Recorder()

    run_at = "2024-01-01T00:00:00"
    fake_result = {
        "run_at": run_at,
        "status": "PASS",
        "overall_ok": True,
        "summary": {},
    }

    monkeypatch.setattr(slack, "slack_selftest", lambda: fake_result)

    slack.run_selftest_cycle(recorder, bus)

    detail = bus.get_service_detail("slack")
    assert detail and "up" in detail

    latest = Path("runtime/reports/slack_selftest.json")
    assert latest.exists()
    saved = json.loads(latest.read_text(encoding="utf-8"))
    assert saved["status"] == "PASS"

    assert recorder.calls
    channel, _ = recorder.calls[-1]
    assert channel == "#logs"
