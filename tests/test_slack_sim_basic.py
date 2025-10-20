from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLACK_ENABLED", "1")
    monkeypatch.setenv("SLACK_SIMULATION", "1")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    if "centrix.settings" in sys.modules:
        from centrix.settings import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]
    sys.modules.pop("centrix.settings", None)
    sys.modules.pop("centrix.services.slack", None)
    importlib.import_module("centrix.settings")


def test_slack_sim_writes_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch)
    slack = importlib.import_module("centrix.services.slack")
    slack._SLACK_OUT = None  # reset
    out = slack.get_slack_out()
    assert out.simulation is True
    out.post_message("#test", "hello world", metadata={"kind": "test"})
    sim_file = slack.SIM_LOG
    assert sim_file.exists()
    lines = sim_file.read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[-1])
    assert record["channel"] == "#test"
    assert record["text"] == "hello world"
    assert record["metadata"]["kind"] == "test"
