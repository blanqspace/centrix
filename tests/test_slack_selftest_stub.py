from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

REAL_SLACK_ENV = (
    os.getenv("SLACK_ENABLED") == "1"
    and os.getenv("SLACK_SIMULATION") == "0"
    and bool(os.getenv("SLACK_BOT_TOKEN"))
    and bool(os.getenv("SLACK_APP_TOKEN"))
)

pytestmark = pytest.mark.skipif(
    REAL_SLACK_ENV, reason="Real Slack environment configured; skip stub selftest"
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeAuthResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


def _install_slack_sdk_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_web = types.ModuleType("slack_sdk.web")

    class FakeWebClient:
        def __init__(self, *, token: str) -> None:
            self.token = token

        def auth_test(self) -> _FakeAuthResponse:
            return _FakeAuthResponse({"ok": True, "user_id": "U123", "team": "T999"})

    fake_web.WebClient = FakeWebClient
    monkeypatch.setitem(sys.modules, "slack_sdk.web", fake_web)

    fake_errors = types.ModuleType("slack_sdk.errors")

    class FakeSlackError(Exception):
        pass

    fake_errors.SlackApiError = FakeSlackError
    fake_errors.SlackClientError = FakeSlackError
    monkeypatch.setitem(sys.modules, "slack_sdk.errors", fake_errors)


def _reload_slack(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "src"))
    for module in ("centrix.services.slack", "centrix.settings"):
        sys.modules.pop(module, None)
    return importlib.import_module("centrix.services.slack")


def test_slack_selftest_success_and_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_ENABLED", "1")
    monkeypatch.setenv("SLACK_SIMULATION", "0")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test-token")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shh")
    monkeypatch.setenv("SLACK_CHANNEL_CONTROL", "#control")
    monkeypatch.setenv("SLACK_CHANNEL_LOGS", "#logs")
    monkeypatch.delenv("SLACK_CHANNEL_ALERTS", raising=False)
    monkeypatch.setenv("SLACK_CHANNEL_ORDERS", "#orders")

    _install_slack_sdk_stubs(monkeypatch)
    slack = _reload_slack(monkeypatch, tmp_path)

    calls: list[tuple[str, str]] = []

    def fake_post_probe(client: Any, channel: str, text: str) -> dict[str, Any]:
        calls.append((channel, text))
        return {"ok": True, "ts": "123.456", "channel": channel, "error": None, "code": None}

    monkeypatch.setattr(slack, "post_probe", fake_post_probe)
    monkeypatch.setattr(
        slack, "socket_mode_probe", lambda: {"ok": True, "error": None, "code": None}
    )

    result = slack.slack_selftest()

    assert result["status"] == "PASS"
    assert result["overall_ok"] is True
    assert result["precheck_ok"] is True

    # ensure messages were attempted for configured channels
    sent_channels = {channel for channel, _ in calls}
    assert sent_channels == {"#control", "#logs", "#orders"}

    # channel entries should include skip for missing alerts channel
    channel_map = {entry["kind"]: entry for entry in result["channels"]}
    assert channel_map["alerts"]["ok"] is None
    assert channel_map["alerts"]["detail"] == "skipped (not configured)"

    # tokens masked in output
    assert result["masked_tokens"]["bot_token"] == "xoxb-****"
    assert result["masked_tokens"]["app_token"] == "xapp-****"

    # checks table contains expected rows
    checks = {(entry["check"], entry["target"]): entry for entry in result["checks"]}
    assert checks[("env", "tokens/channels")]["ok"] is True
    assert checks[("post", "ALERTS")]["ok"] is None
    assert checks[("post", "CONTROL")]["ok"] is True


def test_slack_selftest_precheck_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_ENABLED", "1")
    monkeypatch.setenv("SLACK_SIMULATION", "0")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shh")

    _install_slack_sdk_stubs(monkeypatch)
    slack = _reload_slack(monkeypatch, tmp_path)

    result = slack.slack_selftest()

    assert result["precheck_ok"] is False
    assert result["overall_ok"] is False
    assert result["status"] == "FAIL"
    env_row = next(entry for entry in result["checks"] if entry["check"] == "env")
    assert env_row["ok"] is False
    assert "missing_app_token" in env_row["detail"]
