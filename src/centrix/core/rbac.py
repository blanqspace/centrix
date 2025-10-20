"""Role-based access helpers for Slack-triggered actions."""

from __future__ import annotations

from typing import Final

from centrix.settings import get_settings

DEFAULT_ROLE: Final[str] = "observer"

# Action to roles mapping; roles inherit if listed.
ROLE_MATRIX: Final[dict[str, set[str]]] = {
    "status": {"observer", "operator", "admin"},
    "pause": {"operator", "admin"},
    "resume": {"operator", "admin"},
    "mode": {"admin"},
    "order": {"operator", "admin"},
    "restart": {"admin"},
    "confirm": {"operator", "admin"},
    "reject": {"operator", "admin"},
    "alert": {"operator", "admin"},
}


def role_of(user_id: str | None) -> str:
    """Return the configured role for the given Slack user ID."""

    if not user_id:
        return DEFAULT_ROLE
    settings = get_settings()
    role_map = settings.slack_role_map
    return role_map.get(user_id, DEFAULT_ROLE).lower()


def allow(action: str, role: str) -> bool:
    """Return whether the provided role may execute the action."""

    permitted = ROLE_MATRIX.get(action.lower())
    if permitted is None:
        return False
    return role.lower() in permitted
