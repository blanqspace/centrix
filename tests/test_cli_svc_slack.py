from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from centrix.settings import get_settings


def _reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    sys.modules.pop("centrix.cli", None)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    importlib.import_module("centrix.cli")


def test_cli_handles_slack_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _reload(tmp_path, monkeypatch)
    from centrix import cli  # type: ignore

    runner = CliRunner()

    monkeypatch.setattr(cli, "_start_service", lambda name: True)
    monkeypatch.setattr(cli, "_stop_service", lambda name: True)

    result = runner.invoke(cli.app, ["svc", "start", "slack"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "slack" in result.stdout

    result = runner.invoke(cli.app, ["svc", "status", "slack"], catch_exceptions=False)
    assert result.exit_code == 0

    result = runner.invoke(cli.app, ["svc", "stop", "slack"], catch_exceptions=False)
    assert result.exit_code == 0
