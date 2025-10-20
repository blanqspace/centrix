"""FastAPI dashboard endpoints providing health and metrics."""

from __future__ import annotations

import platform
import sys
from datetime import datetime
from typing import Any

import uvicorn
from fastapi import FastAPI

from centrix import __version__
from centrix.core.alerts import alert_counters
from centrix.core.metrics import snapshot_kpis
from centrix.ipc import Bus, is_running, pidfile
from centrix.settings import get_settings

settings = get_settings()
SERVICE_NAMES = ["tui", "dashboard", "worker"]

app = FastAPI(title=settings.app_brand, version=__version__)


def _read_pid(name: str) -> int | None:
    path = pidfile(name)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        path.unlink(missing_ok=True)
        return None


def _service_status(bus: Bus) -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = {}
    for name in SERVICE_NAMES:
        pid = _read_pid(name)
        running = bool(pid and is_running(pid))
        info: dict[str, Any] = {"pid": pid, "running": running}
        if name == "worker":
            info["last_heartbeat"] = bus.get_heartbeat(name)
        services[name] = info
    return services


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Health-check endpoint used by operators and systemd."""

    bus = Bus(settings.ipc_db)
    services = _service_status(bus)
    ok = all(item["running"] for item in services.values())
    return {"ok": ok, "services": services}


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    """Return runtime KPIs and build metadata."""

    bus = Bus(settings.ipc_db)
    services = _service_status(bus)
    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kpi": snapshot_kpis(),
        "alerts": alert_counters(),
        "services": services,
        "build": {
            "version": __version__,
            "py": sys.version.split()[0],
            "platform": platform.platform(),
        },
    }


def main() -> None:
    """Launch the dashboard service via uvicorn."""

    uvicorn.run(
        "centrix.dashboard.server:app",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
