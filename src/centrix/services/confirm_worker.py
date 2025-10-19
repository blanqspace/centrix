"""Confirmation worker emitting heartbeat events and managing approvals."""

from __future__ import annotations

import signal
import sys
import time

from centrix.core.logging import ensure_runtime_dirs, get_text_logger, log_json
from centrix.ipc.bus import Bus
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
    logger = get_text_logger("centrix.worker.confirm")
    _install_signal_handlers()

    logger.info("confirm worker starting")
    log_json("INFO", "worker starting", component="confirm")

    heartbeat_interval = 5.0
    next_heartbeat = time.monotonic()

    while True:
        now_ms = epoch_ms()
        expired = bus.expire_approvals(now_ms)
        if expired:
            logger.info("expired %s approvals", expired)
            log_json("INFO", "approvals expired", component="confirm", count=expired)

        logger.info("worker alive")
        log_json("INFO", "worker alive", component="confirm", expired=expired)

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
