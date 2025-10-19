from __future__ import annotations

import json

from centrix.core.logging import ensure_runtime_dirs, get_text_logger, log_json


def test_log_json_creates_valid_entry(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    ensure_runtime_dirs()

    logger = get_text_logger("centrix.test")
    logger.info("text line")

    log_json("INFO", "hello", foo="bar")

    json_path = tmp_path / "runtime/logs/centrix.jsonl"
    contents = json_path.read_text(encoding="utf-8").strip().splitlines()
    assert contents
    record = json.loads(contents[-1])
    assert record["msg"] == "hello"
    assert record["foo"] == "bar"
