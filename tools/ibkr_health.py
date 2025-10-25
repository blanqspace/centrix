"""CLI utility to probe IBKR gateway connectivity."""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


def _probe(host: str, port: int, timeout: float) -> tuple[bool, str | None]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except OSError as exc:
        return False, str(exc)


def main() -> int:
    load_dotenv()

    host = os.getenv("TWS_HOST", "127.0.0.1")
    port = int(os.getenv("TWS_PORT", "4002"))
    timeout = float(os.getenv("IBKR_HEALTH_TIMEOUT", "1.5"))

    reachable, detail = _probe(host, port, timeout=timeout)
    payload: dict[str, object] = {"reachable": reachable}
    if not reachable and detail:
        payload["error"] = detail

    print(json.dumps(payload, separators=(",", ":")))
    return 0 if reachable else 1


if __name__ == "__main__":
    raise SystemExit(main())
