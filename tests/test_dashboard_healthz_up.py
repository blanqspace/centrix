from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from centrix.dashboard.server import SERVICE_NAMES, Bus, healthz


def test_healthz_up(monkeypatch) -> None:
    def fake_status(self: Bus, services: list[str]) -> dict[str, dict[str, Any]]:
        return {name: {"running": True} for name in services}

    monkeypatch.setattr(Bus, "get_services_status", fake_status, raising=False)

    payload = asyncio.run(healthz())
    assert payload["ok"] is True
    assert set(payload["services"]) == set(SERVICE_NAMES)
