"""Consistent, verifiable SQLite backups for the OpenOPC operations store."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opc.operations.repository import OPERATIONS_SCHEMA_VERSION


class OperationsBackupError(RuntimeError):
    """Raised when a backup is corrupt, incompatible, or unsafe to replace."""


class OperationsBackupManager:
    """Create and restore atomic SQLite snapshots with integrity evidence."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path).expanduser().resolve()

    async def create_backup(
        self,
        destination: Path | str,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        target = Path(destination).expanduser().resolve()
        return await asyncio.to_thread(
            _create_backup_sync,
            self.database_path,
            target,
            overwrite,
        )

    @staticmethod
    async def inspect_backup(path: Path | str) -> dict[str, Any]:
        source = Path(path).expanduser().resolve()
        return await asyncio.to_thread(_inspect_backup_sync, source, True)

    @staticmethod
    async def restore_backup(
        backup_path: Path | str,
        destination: Path | str,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        source = Path(backup_path).expanduser().resolve()
        target = Path(destination).expanduser().resolve()
        return await asyncio.to_thread(_restore_backup_sync, source, target, overwrite)


def _create_backup_sync(source: Path, destination: Path, overwrite: bool) -> dict[str, Any]:
    if not source.is_file():
        raise FileNotFoundError(f"operations database not found: {source}")
    if source == destination:
        raise OperationsBackupError("backup destination must differ from the live database")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(destination)
    try:
        with sqlite3.connect(str(source), timeout=30.0) as live, sqlite3.connect(
            str(temporary), timeout=30.0
        ) as snapshot:
            live.backup(snapshot)
            snapshot.commit()
        details = _inspect_backup_sync(temporary, verify_manifest=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        details = {
            **details,
            "path": str(destination),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sha256": _sha256(destination),
        }
        _write_manifest(destination, details)
        return details
    finally:
        temporary.unlink(missing_ok=True)


def _restore_backup_sync(source: Path, destination: Path, overwrite: bool) -> dict[str, Any]:
    if not source.is_file():
        raise FileNotFoundError(f"backup database not found: {source}")
    if source == destination:
        raise OperationsBackupError("restore destination must differ from the backup file")
    source_details = _inspect_backup_sync(source, verify_manifest=True)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"restore destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(destination)
    try:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30.0) as backup, sqlite3.connect(
            str(temporary), timeout=30.0
        ) as restored:
            backup.backup(restored)
            restored.commit()
        restored_details = _inspect_backup_sync(temporary, verify_manifest=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        return {
            **restored_details,
            "path": str(destination),
            "restored_from": str(source),
            "source_sha256": source_details["sha256"],
            "sha256": _sha256(destination),
        }
    finally:
        temporary.unlink(missing_ok=True)


def _inspect_backup_sync(path: Path, verify_manifest: bool) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"backup database not found: {path}")
    digest = _sha256(path)
    if verify_manifest:
        manifest_path = _manifest_path(path)
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise OperationsBackupError(f"invalid backup manifest: {manifest_path}") from exc
            expected = str(manifest.get("sha256", "") or "")
            if expected and expected != digest:
                raise OperationsBackupError("backup checksum does not match its manifest")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0) as connection:
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if integrity.lower() != "ok":
                raise OperationsBackupError(f"SQLite quick_check failed: {integrity}")
            row = connection.execute(
                "SELECT version FROM operations_schema WHERE component = 'operating_kernel'"
            ).fetchone()
            if row is None:
                raise OperationsBackupError("backup has no operating_kernel schema marker")
            schema_version = int(row[0])
            if schema_version > OPERATIONS_SCHEMA_VERSION:
                raise OperationsBackupError(
                    f"backup schema {schema_version} is newer than supported "
                    f"schema {OPERATIONS_SCHEMA_VERSION}"
                )
            counts = {}
            for table in (
                "goal_contracts",
                "run_manifests",
                "operating_events",
                "outbox_messages",
                "route_execution_contracts",
                "provider_usage_events",
                "provider_canary_results",
                "provider_call_reservations",
                "resource_approval_uses",
                "operator_actions",
            ):
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                counts[table] = (
                    int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    if exists
                    else 0
                )
    except sqlite3.DatabaseError as exc:
        raise OperationsBackupError(f"cannot inspect SQLite backup: {exc}") from exc
    return {
        "path": str(path),
        "integrity": integrity,
        "operations_schema_version": schema_version,
        "table_counts": counts,
        "sha256": digest,
    }


def _temporary_path(destination: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    os.close(descriptor)
    return Path(raw_path)


def _manifest_path(database_path: Path) -> Path:
    return database_path.with_name(f"{database_path.name}.manifest.json")


def _write_manifest(database_path: Path, details: dict[str, Any]) -> None:
    manifest = _manifest_path(database_path)
    temporary = _temporary_path(manifest)
    try:
        temporary.write_text(
            json.dumps(details, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, manifest)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
