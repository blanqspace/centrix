"""Confirmation worker emitting heartbeat events and managing approvals."""

from __future__ import annotations

import signal
import sys
import time

from centrix.core.logging import ensure_runtime_dirs, log_event
from centrix.ipc.bus import Bus, read_state
from centrix.ipc.migrate import epoch_ms
from centrix.settings import get_settings


def _install_signal_handlers() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit(0))


def run() -> None:
    """Run the worker loop, logging a heartbeat once per second."""

    ensure_runtime_dirs()
    settings = get_settings()
    bus = Bus(settings.ipc_db)
    _install_signal_handlers()

    log_event("worker", "startup", "confirm worker starting")

    heartbeat_interval = 5.0
    next_heartbeat = time.monotonic()

    while True:
        now_ms = epoch_ms()
        expired = bus.expire_approvals(now_ms)
        if expired:
            log_event("worker", "approvals", "expired approvals", count=expired)

        state = read_state()
        paused = bool(state.get("paused"))

        log_event("worker", "heartbeat", "worker alive", expired=expired, paused=paused)
        if paused:
            log_event("worker", "state", "holding", level="INFO")

        if time.monotonic() >= next_heartbeat:
            bus.emit(
                "svc.worker.alive",
                "INFO",
                {"component": "confirm", "expired": expired, "ts": now_ms},
            )
            next_heartbeat = time.monotonic() + heartbeat_interval

        time.sleep(1)


def main() -> None:
    """Entrypoint for systemd and CLI starts."""

    run()


if __name__ == "__main__":
    main()
