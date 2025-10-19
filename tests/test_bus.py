from __future__ import annotations

from pathlib import Path

from centrix.ipc.bus import Bus
from centrix.ipc.migrate import epoch_ms


def test_emit_enqueue_and_tail(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db_path = Path("runtime") / "ctl.db"
    bus = Bus(str(db_path))

    first_id = bus.emit("state.init", "INFO", {"ok": True})
    second_id = bus.emit("state.pause", "WARN", {"reason": "manual"})

    events = bus.tail_events()
    assert [event["id"] for event in events] == [first_id, second_id]

    warn_events = bus.tail_events(level="WARN")
    assert len(warn_events) == 1
    assert warn_events[0]["topic"] == "state.pause"

    topic_events = bus.tail_events(topic="state.init")
    assert len(topic_events) == 1
    assert topic_events[0]["level"] == "INFO"

    command_id = bus.enqueue("order.submit", {"symbol": "XYZ"})
    approval = bus.new_approval(command_id, ttl_sec=1, token_len=6)
    assert approval["command_id"] == command_id
    assert len(approval["token"]) == 6

    assert bus.fulfill_approval(approval["token"], "tester") is True
    assert bus.fulfill_approval(approval["token"], "tester") is False

    expiring = bus.new_approval(command_id, ttl_sec=0)
    expired = bus.expire_approvals(epoch_ms())
    assert expired >= 1
    assert bus.fulfill_approval(expiring["token"], "tester") is False
