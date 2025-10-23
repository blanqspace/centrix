"""Run the Centrix command worker."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from centrix.utils.logging_setup import setup_logging
from centrix.worker import run_worker

log = logging.getLogger("centrix.worker.cli")


def main() -> int:
    load_dotenv()
    setup_logging()

    try:
        run_worker()
    except Exception:
        log.exception("Worker terminated with error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
