"""CLI helper to push control-channel approvals to Slack."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from centrix.services.slack_blocks import approve_block
from centrix.services.slack_service import build_app, post_control
from centrix.utils.logging_setup import setup_logging

log = logging.getLogger("centrix.post_control")


def main() -> int:
    load_dotenv()
    setup_logging()

    order_id = sys.argv[1] if len(sys.argv) > 1 else "ORD-TEST"

    try:
        build_app()
        ts = post_control(f"Approve order {order_id}?", approve_block(order_id))
    except Exception as exc:  # pragma: no cover - command-line guard
        log.error("post_control failed: %s", exc)
        return 1

    print(f"posted ts={ts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
