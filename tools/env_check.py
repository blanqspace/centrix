#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from typing import Any

from centrix.settings import get_settings


def main() -> None:
    settings = get_settings()
    missing: list[str] = []
    notices: list[str] = []

    if settings.slack_enabled:
        if not settings.slack_bot_token:
            missing.append("SLACK_BOT_TOKEN")
        if not settings.slack_app_token:
            missing.append("SLACK_APP_TOKEN")
        if not settings.slack_signing_secret:
            missing.append("SLACK_SIGNING_SECRET")
        if not settings.slack_role_map:
            notices.append("SLACK_ROLE_MAP (empty)")

    payload: dict[str, Any] = {
        "slack_enabled": settings.slack_enabled,
        "slack_simulation": settings.slack_simulation,
        "ok": not missing,
        "missing": missing,
        "notices": notices,
    }

    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    if missing:
        sys.exit(1)


if __name__ == "__main__":
    main()
