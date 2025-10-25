"""Entry point for running the Centrix dashboard via uvicorn."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
import uvicorn

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from centrix.settings import AppSettings
from centrix.utils.logging_setup import setup_logging

log = logging.getLogger("centrix.dashboard.runner")


def main() -> int:
    load_dotenv()
    setup_logging()

    settings = AppSettings()
    config = uvicorn.Config(
        "centrix.dashboard.server:create_app",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="info",
        factory=True,
    )
    server = uvicorn.Server(config)

    try:
        server.run()
    except KeyboardInterrupt:
        log.info("Dashboard stopped cleanly")
        return 0
    except Exception:
        log.exception("Dashboard server crashed")
        return 1

    log.info("Dashboard stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
