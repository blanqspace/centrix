from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from centrix.settings import get_settings


def _reload_cli() -> None:
    sys.modules.pop("centrix.cli", None)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    importlib.import_module("centrix.cli")


def test_mode_and_state_commands(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _reload_cli()
    from centrix import cli  # type: ignore

    runner = CliRunner()

    result = runner.invoke(cli.app, ["mode", "set", "mock"], catch_exceptions=False)
    assert result.exit_code == 0
    state_file = Path("runtime/state.json")
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["mode"] == "mock"
    assert state["mode_mock"] is True

    result = runner.invoke(cli.app, ["state", "pause"], catch_exceptions=False)
    assert result.exit_code == 0
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["paused"] is True

    result = runner.invoke(cli.app, ["state", "resume"], catch_exceptions=False)
    assert result.exit_code == 0
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["paused"] is False

    result = runner.invoke(
        cli.app,
        ["order", "new", "--symbol", "TEST", "--qty", "2", "--px", "1.5"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert '"symbol":"TEST"' in result.stdout
