from __future__ import annotations

import importlib
import json
import sys

import pytest


def _set_roles(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    monkeypatch.setenv("SLACK_ROLE_MAP", json.dumps(mapping))
    if "centrix.settings" in sys.modules:
        from centrix.settings import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]
    sys.modules.pop("centrix.settings", None)
    sys.modules.pop("centrix.core.rbac", None)
    importlib.import_module("centrix.settings")


def test_role_of(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_roles(monkeypatch, {"U1": "admin", "U2": "operator"})
    rbac = importlib.import_module("centrix.core.rbac")
    assert rbac.role_of("U1") == "admin"
    assert rbac.role_of("U2") == "operator"
    assert rbac.role_of("ux") == "observer"


def test_allow_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_roles(monkeypatch, {})
    rbac = importlib.import_module("centrix.core.rbac")
    assert rbac.allow("status", "observer") is True
    assert rbac.allow("pause", "observer") is False
    assert rbac.allow("pause", "operator") is True
    assert rbac.allow("restart", "operator") is False
    assert rbac.allow("restart", "admin") is True
