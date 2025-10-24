"""Entry point for running the Centrix Slack Socket Mode service."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from centrix.services.slack_service import build_app, run_socket_mode
from centrix.utils.env import get_env_str, mask
from centrix.utils.logging_setup import setup_logging


log = logging.getLogger("centrix.slack.runner")


def main() -> int:
    load_dotenv()
    setup_logging()

    if os.getenv("SLACK_ENABLED") != "1":
        print("SLACK_ENABLED!=1 -> exit")
        return 0

    app_token = get_env_str("SLACK_APP_TOKEN", required=True)
    if app_token is None:
        raise RuntimeError("missing env: SLACK_APP_TOKEN")

    app = build_app()
    log.info("Launching Slack Socket Mode (app_token=%s)", mask(app_token, show=4))

    try:
        run_socket_mode(app_token, app)
    except KeyboardInterrupt:
        log.info("Slack Socket Mode interrupted by user")
        return 0
    except Exception:
        log.exception("Slack Socket Mode terminated with error")
        return 1

    log.info("Slack Socket Mode exited normally")
    return 0


if __name__ == "__main__":
    sys.exit(main())
