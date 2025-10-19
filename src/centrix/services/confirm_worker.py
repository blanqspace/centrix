"""Confirmation worker stub emitting liveliness logs."""
from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path

LOG_PATH = Path("runtime/logs/centrix.log")


def _configure_logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("centrix.worker.confirm")
    if not logger.handlers:
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def _install_signal_handlers() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit(0))


def run() -> None:
    """Run the worker loop, logging a heartbeat once per second."""

    logger = _configure_logger()
    _install_signal_handlers()
    logger.info("confirm worker starting")
    while True:
        logger.info("worker alive")
        time.sleep(1)


def main() -> None:
    """Entrypoint for systemd and CLI starts."""

    run()


if __name__ == "__main__":
    main()
