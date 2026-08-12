from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from opc.database.store import CORE_SCHEMA_VERSION, OPCStore


@pytest.mark.asyncio
async def test_fresh_store_records_core_schema_version(tmp_path: Path) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        async with store._require_db().execute(
            "SELECT version FROM schema_components WHERE component = 'openopc_core'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] == CORE_SCHEMA_VERSION
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_legacy_store_is_upgraded_without_losing_tasks(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """CREATE TABLE tasks (
                   id TEXT PRIMARY KEY,
                   title TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               );
               INSERT INTO tasks(id, title, created_at, updated_at)
               VALUES ('legacy-task', 'Keep me', '2026-01-01', '2026-01-01');"""
        )

    store = OPCStore(path)
    await store.initialize()
    try:
        task = await store.get_task("legacy-task")
        assert task is not None
        assert task.title == "Keep me"
        async with store._require_db().execute(
            "SELECT version FROM schema_components WHERE component = 'openopc_core'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] == CORE_SCHEMA_VERSION
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_newer_core_schema_fails_before_application_tables_are_created(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tasks.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """CREATE TABLE schema_components (
                   component TEXT PRIMARY KEY,
                   version INTEGER NOT NULL,
                   updated_at TEXT NOT NULL
               );
               INSERT INTO schema_components VALUES (
                   'openopc_core', 999, '2026-01-01T00:00:00+00:00'
               );"""
        )

    store = OPCStore(path)
    try:
        with pytest.raises(RuntimeError, match="newer than supported"):
            await store.initialize()
    finally:
        await store.close()

    with sqlite3.connect(path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "tasks" not in tables
