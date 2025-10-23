"""Quick smoke-test for the command bus."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from centrix.bus import enqueue_command, init_db
from centrix.utils.logging_setup import setup_logging

log = logging.getLogger("centrix.bus.smoke")


def main() -> int:
    load_dotenv()
    setup_logging()
    init_db()
    cmd_id = enqueue_command(
        "APPROVE",
        {"id": "TST"},
        requested_by="UXXX",
        role="admin",
        ttl_sec=60,
    )
    print(f"queued {cmd_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
