from __future__ import annotations

import importlib
import types

import pytest
from typer.testing import CliRunner


def _reload_cli(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    import centrix.cli  # type: ignore

    monkeypatch.setenv("SLACK_ENABLED", "0")
    monkeypatch.setenv("SLACK_SIMULATION", "1")
    importlib.reload(centrix.cli)
    return centrix.cli


def test_svc_wait_dashboard_success(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _reload_cli(monkeypatch)
    runner = CliRunner()

    attempts = {"count": 0}

    class _FakeResponse:
        status = 200

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None  # pragma: no cover - no cleanup needed

    def fake_urlopen(url: str, timeout: float = 1.0):
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise cli.urlerror.URLError("not ready")
        return _FakeResponse()

    monkeypatch.setattr(cli.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        cli,
        "time",
        types.SimpleNamespace(
            monotonic=lambda: attempts["count"],
            sleep=lambda _: None,
        ),
    )

    result = runner.invoke(
        cli.app,
        ["svc", "wait", "dashboard", "--timeout", "3"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert attempts["count"] >= 2
