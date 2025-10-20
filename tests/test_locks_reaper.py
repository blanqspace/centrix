from __future__ import annotations

from centrix.core.logging import ensure_runtime_dirs
from centrix.shared.locks import acquire_lock, list_lock_files, reaper_sweep


def test_lock_reaper_removes_expired(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    ensure_runtime_dirs()

    assert acquire_lock("demo", ttl=1) is True
    locks = list_lock_files()
    assert locks
    expires_at = locks[0]["expires_at"]

    removed = reaper_sweep(expires_at + 1)
    assert removed == 1
    assert not list_lock_files(expires_at + 1)
