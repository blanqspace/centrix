from __future__ import annotations

from pathlib import Path

from centrix.core.locks import acquire, list_locks, reap, release
from centrix.core.logging import ensure_runtime_dirs
from centrix.ipc.bus import Bus
from centrix.ipc.migrate import epoch_ms
from centrix.settings import get_settings


def test_lock_lifecycle(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    ensure_runtime_dirs()

    _ = Bus("runtime/ctl.db")

    assert acquire("alpha", "owner1", 2) is True
    assert acquire("alpha", "other", 2) is False

    locks = list_locks()
    assert locks[0]["name"] == "alpha"
    assert locks[0]["owner"] == "owner1"

    assert release("alpha", "other") is False
    assert release("alpha", "owner1") is True

    assert not list_locks()


def test_lock_reap(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    ensure_runtime_dirs()

    _ = Bus("runtime/ctl.db")

    assert acquire("beta", "owner", 1) is True
    future = epoch_ms() + 2000
    removed = reap(future)
    assert removed == 1
    assert not list_locks()

    lock_path = Path("runtime/locks/beta.lock")
    assert lock_path.exists() is False
