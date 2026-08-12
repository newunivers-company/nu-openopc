from __future__ import annotations

import sqlite3

import pytest

from opc.database import store as store_module
from opc.database.store import _SQLiteConnectionAdapter


@pytest.mark.asyncio
async def test_sqlite_adapter_retries_transient_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _SQLiteConnectionAdapter(":memory:")
    attempts = 0

    def eventually_succeeds() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    monkeypatch.setattr(store_module, "_SQLITE_LOCK_RETRY_BASE_DELAY_SECONDS", 0.001)
    try:
        assert await adapter._call(eventually_succeeds) == "ok"
        assert attempts == 3
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_sqlite_adapter_does_not_retry_other_operational_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _SQLiteConnectionAdapter(":memory:")
    attempts = 0

    def always_fails() -> None:
        nonlocal attempts
        attempts += 1
        raise sqlite3.OperationalError("no such table: missing")

    monkeypatch.setattr(store_module, "_SQLITE_LOCK_RETRY_BASE_DELAY_SECONDS", 0.001)
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            await adapter._call(always_fails)
        assert attempts == 1
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_sqlite_adapter_stops_at_exact_lock_retry_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _SQLiteConnectionAdapter(":memory:")
    attempts = 0

    def always_locked() -> None:
        nonlocal attempts
        attempts += 1
        raise sqlite3.OperationalError("database table is locked")

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(store_module, "_SQLITE_LOCK_RETRY_BASE_DELAY_SECONDS", 0.001)
    monkeypatch.setattr(store_module.asyncio, "sleep", no_wait)
    try:
        with pytest.raises(sqlite3.OperationalError, match="database table is locked"):
            await adapter._call(always_locked)
        assert attempts == store_module._SQLITE_LOCK_RETRY_ATTEMPTS + 1
    finally:
        await adapter.close()
