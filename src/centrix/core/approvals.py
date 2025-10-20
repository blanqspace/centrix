"""Approval helpers for two-man confirmation flows."""

from __future__ import annotations

import json
from typing import Any

from centrix.ipc.bus import Bus
from centrix.ipc.migrate import epoch_ms
from centrix.settings import get_settings


def _bus() -> Bus:
    settings = get_settings()
    return Bus(settings.ipc_db)


def _kv_key(order_id: int) -> str:
    return f"approval:order:{order_id}"


def request_approval(order_id: int, initiator: str, ttl_s: int) -> str:
    """Create an approval record for the supplied order and return the token."""

    bus = _bus()
    token_len = get_settings().approval_token_length
    record = bus.new_approval(order_id, ttl_sec=ttl_s, token_len=token_len)
    token = str(record["token"])
    metadata = {
        "order_id": order_id,
        "initiator": initiator,
        "token": token,
        "status": "PENDING",
        "created_at": record["created_at"],
        "expires_at": record["expires_at"],
    }
    bus.set_kv(_kv_key(order_id), json.dumps(metadata))
    return token


def approval_metadata(order_id: int) -> dict[str, Any] | None:
    bus = _bus()
    payload = bus.get_kv(_kv_key(order_id))
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def confirm(order_id: int, approver: str, token: str) -> tuple[bool, str]:
    """Attempt to confirm an approval token for the order."""

    meta = approval_metadata(order_id)
    if meta is None:
        return (False, "approval not found")
    if meta.get("initiator") == approver:
        return (False, "initiator cannot approve")
    if meta.get("token") != token:
        return (False, "invalid token")

    bus = _bus()
    if not bus.fulfill_approval(token, approver):
        bus.expire_approvals(epoch_ms())
        if not bus.fulfill_approval(token, approver):
            return (False, "token expired or already used")

    meta["status"] = "APPROVED"
    meta["approver"] = approver
    bus.set_kv(_kv_key(order_id), json.dumps(meta))
    return (True, "approved")


def reject(order_id: int, approver: str, reason: str | None = None) -> tuple[bool, str]:
    """Mark an approval as rejected by a separate approver."""

    meta = approval_metadata(order_id)
    if meta is None:
        return (False, "approval not found")
    if meta.get("initiator") == approver:
        return (False, "initiator cannot reject")
    meta["status"] = "REJECTED"
    meta["approver"] = approver
    meta["reason"] = reason or "rejected"
    _bus().set_kv(_kv_key(order_id), json.dumps(meta))
    return (True, "rejected")
