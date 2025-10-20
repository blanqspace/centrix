from __future__ import annotations

import json

from centrix.core.logging import ensure_runtime_dirs, log_event


def test_logging_jsonl_structure(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    ensure_runtime_dirs()

    log_event("test", "unit", "hello world", level="INFO", foo="bar")
    log_event("test", "unit", "another", level="WARN", corr_id="abc123")

    json_path = tmp_path / "runtime/logs/centrix.jsonl"
    lines = json_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["svc"] == "test"
    assert first["topic"] == "unit"
    assert first["msg"] == "hello world"
    assert "pid" in first
    assert "extra" in first and first["extra"]["foo"] == "bar"

    second = json.loads(lines[1])
    assert second["corr_id"] == "abc123"
