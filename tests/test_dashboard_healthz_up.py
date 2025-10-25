from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from centrix.dashboard import server


def test_healthz_up(monkeypatch) -> None:
    now = time.time()

    def fake_services() -> dict[str, dict[str, Any]]:
        return {
            "worker": {"last_seen": now, "state": "up"},
            "slack": {"last_seen": now - 5, "state": "up"},
        }

    monkeypatch.setattr(server, "get_services", fake_services, raising=False)

    payload = asyncio.run(server.healthz())
    assert payload["ok"] is True
    assert set(payload["services"]) == {"worker", "slack"}
    assert payload["services"]["worker"]["status"] == "up"
    assert payload["services"]["slack"]["status"] == "up"
