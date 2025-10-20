from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from centrix.settings import get_settings


def _reload_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    sys.modules.pop("centrix.cli", None)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    importlib.import_module("centrix.cli")


def test_parse_targets_all_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _reload_cli(monkeypatch, tmp_path)
    from centrix import cli  # type: ignore

    runner = CliRunner()
    monkeypatch.setattr(cli, "_start_service", lambda name: True)
    monkeypatch.setattr(cli, "_stop_service", lambda name: True)

    result = runner.invoke(
        cli.app,
        ["svc", "start", "tui,dashboard"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0


def test_parse_targets_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _reload_cli(monkeypatch, tmp_path)
    from centrix import cli  # type: ignore

    runner = CliRunner()
    result = runner.invoke(cli.app, ["svc", "start", "foo"])
    assert result.exit_code != 0
    assert "unknown target" in result.output
