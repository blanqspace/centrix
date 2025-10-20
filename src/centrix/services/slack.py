"""Stub Slack bridge service emitting periodic heartbeats."""

from __future__ import annotations

import signal
import sys
import time

from centrix.core.logging import ensure_runtime_dirs, log_event


def _install_signal_handlers() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit(0))


def run() -> None:
    ensure_runtime_dirs()
    _install_signal_handlers()
    log_event("slack", "startup", "slack bridge stub starting")
    interval = 15
    while True:
        log_event("slack", "heartbeat", "slack bridge alive")
        time.sleep(interval)


def main() -> None:
    run()


if __name__ == "__main__":
    main()
