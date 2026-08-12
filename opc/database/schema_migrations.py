"""Shared component-version boundary for additive SQLite migrations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


SCHEMA_COMPONENTS_TABLE = "schema_components"


@dataclass(frozen=True)
class SchemaMigrationState:
    component: str
    current_version: int
    target_version: int

    @property
    def upgrade_required(self) -> bool:
        return self.current_version < self.target_version


async def prepare_component_migration(
    db: Any,
    *,
    component: str,
    target_version: int,
) -> SchemaMigrationState:
    """Read a component version and fail before applying an unsafe downgrade."""

    normalized_component = str(component or "").strip()
    if not normalized_component:
        raise ValueError("schema component is required")
    if target_version < 1:
        raise ValueError("target schema version must be positive")

    await db.execute(
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA_COMPONENTS_TABLE} (
               component TEXT PRIMARY KEY,
               version INTEGER NOT NULL,
               updated_at TEXT NOT NULL
           )"""
    )
    async with db.execute(
        f"SELECT version FROM {SCHEMA_COMPONENTS_TABLE} WHERE component = ?",
        (normalized_component,),
    ) as cursor:
        row = await cursor.fetchone()
    current_version = int(row[0]) if row is not None else 0
    if current_version > target_version:
        raise RuntimeError(
            f"{normalized_component} schema {current_version} is newer than supported "
            f"schema {target_version}; upgrade OpenOPC before opening this database"
        )
    return SchemaMigrationState(
        component=normalized_component,
        current_version=current_version,
        target_version=target_version,
    )


async def complete_component_migration(
    db: Any,
    state: SchemaMigrationState,
) -> None:
    """Record success only after every idempotent migration step completes."""

    await db.execute(
        f"""INSERT INTO {SCHEMA_COMPONENTS_TABLE}(component, version, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(component) DO UPDATE SET
                version = excluded.version,
                updated_at = excluded.updated_at""",
        (
            state.component,
            state.target_version,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    await db.commit()
