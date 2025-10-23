"""Environment helpers with masking and role map support."""

from __future__ import annotations

import json
import logging
import os
from typing import Mapping

_TRUE_VALUES = {"1", "true", "TRUE", "yes", "on", "ON"}
log = logging.getLogger("centrix.env")


def get_env_str(
    key: str,
    *,
    required: bool = False,
    default: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Fetch a string from the environment, raising if required and missing."""
    source = env if env is not None else os.environ
    value = source.get(key, default)
    if required and not value:
        raise RuntimeError(f"missing env: {key}")
    return value


def get_env_bool(key: str, default: bool = False) -> bool:
    """Interpret an env var as bool with common truthy values."""
    value = os.getenv(key)
    if value is None:
        return default
    return value in _TRUE_VALUES


def get_role_map(key: str = "SLACK_ROLE_MAP") -> dict[str, str]:
    """Decode a mapping of Slack user IDs to roles."""
    raw = os.getenv(key, "")
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Failed to parse %s; falling back to empty role map", key)
        return {}

    if not isinstance(parsed, dict):
        log.warning("%s must be a JSON object; got %s", key, type(parsed).__name__)
        return {}

    cleaned: dict[str, str] = {}
    for user_id, role in parsed.items():
        if not isinstance(user_id, str) or not isinstance(role, str):
            continue
        cleaned[user_id] = role
    return cleaned


def mask(secret: str | None, *, show: int = 6) -> str | None:
    """Mask all but the first `show` characters of a secret."""
    if secret is None:
        return None
    show = max(show, 0)
    if len(secret) <= show:
        return secret if secret else ""
    return secret[:show] + "*" * (len(secret) - show)
