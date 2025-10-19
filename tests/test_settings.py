"""Tests for the settings loader."""
from __future__ import annotations

from pathlib import Path
from shutil import copyfile

from centrix.settings import AppSettings


def test_env_example_loads_defaults(tmp_path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parent.parent
    copyfile(project_root / ".env.example", tmp_path / ".env")
    monkeypatch.chdir(tmp_path)

    settings = AppSettings()

    assert settings.app_brand == "Centrix"
    assert settings.dashboard_port == 8787
