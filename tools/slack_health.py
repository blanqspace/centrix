"""CLI utility to probe Slack connectivity."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from centrix.services.slack_service import build_app, healthcheck
from centrix.utils.logging_setup import setup_logging

log = logging.getLogger("centrix.slack.health")


def main() -> int:
    load_dotenv()
    setup_logging()

    try:
        build_app()
    except Exception:
        log.exception("Failed to initialise Slack app")
        return 1

    result = healthcheck()
    print(json.dumps(result, separators=(",", ":")))

    if result.get("auth_ok") and result.get("post_ok"):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
