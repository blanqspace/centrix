from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


def _reset_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORDER_APPROVAL_TTL_SEC", "1")
    if "centrix.settings" in sys.modules:
        from centrix.settings import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]
    sys.modules.pop("centrix.settings", None)
    importlib.import_module("centrix.settings")


def test_two_man_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_env(tmp_path, monkeypatch)
    approvals = importlib.import_module("centrix.core.approvals")
    from centrix.ipc.bus import Bus
    from centrix.settings import get_settings

    bus = Bus(get_settings().ipc_db)
    order_id = bus.enqueue("order.submit", {"symbol": "DEMO"})
    token = approvals.request_approval(order_id=order_id, initiator="U_INIT", ttl_s=2)
    ok, msg = approvals.confirm(order_id=order_id, approver="U_INIT", token=token)
    assert ok is False
    assert "initiator" in msg
    ok, msg = approvals.confirm(order_id=order_id, approver="U_CONF", token=token)
    assert ok is True
    ok, msg = approvals.confirm(order_id=order_id, approver="U_OTHER", token=token)
    assert ok is False
    assert "expired" in msg or "already" in msg


def test_reject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_env(tmp_path, monkeypatch)
    approvals = importlib.import_module("centrix.core.approvals")
    from centrix.ipc.bus import Bus
    from centrix.settings import get_settings

    bus = Bus(get_settings().ipc_db)
    order_id = bus.enqueue("order.submit", {"symbol": "DEMO"})
    approvals.request_approval(order_id=order_id, initiator="U_INIT", ttl_s=5)
    ok, _msg = approvals.reject(order_id=order_id, approver="U_INIT")
    assert ok is False
    ok, _msg = approvals.reject(order_id=order_id, approver="U_CONF")
    assert ok is True
