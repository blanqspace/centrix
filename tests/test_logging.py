from __future__ import annotations

from centrix.core.logging import ensure_runtime_dirs, log_event


def test_log_event_creates_structured_line(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    ensure_runtime_dirs()

    log_event("test", "unit", "hello", foo="bar", level="INFO")

    log_path = tmp_path / "runtime/logs/centrix.log"
    contents = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert contents
    line = contents[-1]
    assert "svc=test" in line
    assert "topic=unit" in line
    assert 'msg="hello"' in line
    assert "foo=bar" in line
