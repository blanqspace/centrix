from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from centrix.settings import get_settings


def _reload_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    for module in ["centrix.cli", "centrix.settings"]:
        sys.modules.pop(module, None)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    importlib.import_module("centrix.settings")
    return importlib.import_module("centrix.cli")


def test_svc_status_formats_elapsed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cli = _reload_cli(monkeypatch, tmp_path)
    runner = CliRunner()
    snapshot = {"tui": {"pid": 4321, "running": True, "elapsed_ms": 1500}}
    monkeypatch.setattr(cli, "_service_snapshot", lambda: snapshot)

    result = runner.invoke(cli.app, ["svc", "status", "tui"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "tui: running" in result.stdout
    assert "pid=4321" in result.stdout
    assert "elapsed=1.5s" in result.stdout
    assert "running=1/1" in result.stdout
