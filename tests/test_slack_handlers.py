from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

from centrix.settings import get_settings


def _reload_slack(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    for module in ["centrix.services.slack", "centrix.settings"]:
        sys.modules.pop(module, None)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    importlib.import_module("centrix.settings")
    return importlib.import_module("centrix.services.slack")


def test_slash_command_ack_before_handler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    slack = _reload_slack(monkeypatch, tmp_path)
    ack_calls: list[str] = []
    responses: list[str] = []

    def ack() -> None:
        ack_calls.append("ack")

    def handler(*, user_id: str, text: str) -> dict[str, Any]:
        assert ack_calls == ["ack"]
        return {"text": f"{user_id}:{text}"}

    def respond(message: str) -> None:
        responses.append(message)

    body = {"user_id": "U1", "text": "status"}
    slack.process_slash_command_request(body, ack, respond, handler=handler)
    assert ack_calls == ["ack"]
    assert responses == ["U1:status"]


def test_slash_command_error_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    slack = _reload_slack(monkeypatch, tmp_path)
    ack_calls: list[str] = []
    responses: list[str] = []

    def ack() -> None:
        ack_calls.append("ack")

    def handler(*, user_id: str, text: str) -> dict[str, Any]:
        raise RuntimeError("boom")

    slack.process_slash_command_request(
        {"user_id": "U2", "text": "status"},
        ack,
        responses.append,
        handler=handler,
    )
    assert ack_calls == ["ack"]
    assert responses[-1] == "error processing command"


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
