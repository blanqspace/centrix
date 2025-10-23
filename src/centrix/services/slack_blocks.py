"""Reusable Slack Block Kit snippets."""

from __future__ import annotations

from typing import Any


def approve_block(order_id: str) -> list[dict[str, Any]]:
    """Compose a Block Kit message asking to approve the given order."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Approve order *{order_id}*?",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Approve",
                    },
                    "style": "primary",
                    "action_id": "approve_btn",
                    "value": order_id,
                }
            ],
        },
    ]
