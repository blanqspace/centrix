from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLACK_ENABLED", "1")
    monkeypatch.setenv("SLACK_SIMULATION", "1")
    monkeypatch.setenv("ALERT_MIN_LEVEL", "INFO")
    if "centrix.settings" in sys.modules:
        from centrix.settings import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]
    sys.modules.pop("centrix.settings", None)
    importlib.import_module("centrix.settings")
    sys.modules.pop("centrix.services.slack", None)
    importlib.import_module("centrix.services.slack")


def test_alerts_written_to_sim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(tmp_path, monkeypatch)
    slack = importlib.import_module("centrix.services.slack")
    slack._SLACK_OUT = None
    sim_file = slack.SIM_LOG
    if sim_file.exists():
        sim_file.unlink()
    alerts = importlib.import_module("centrix.core.alerts")
    alerts.emit_alert("WARN", "svc.test", "something happened", "fp-test")
    assert sim_file.exists()
    lines = sim_file.read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines if line.strip()]
    assert entries
    slack_mod = importlib.import_module("centrix.services.slack")
    assert entries[-1]["channel"] == slack_mod.channel_for("alerts")
