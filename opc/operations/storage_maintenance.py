"""Offline, recoverable compaction for project-scoped OpenOPC databases.

Dry-run is the default. Applying compaction builds and validates a separate
database, renames the untouched original to a timestamped backup, and only
then atomically installs the compacted copy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opc.core.checkpoint_storage import (
    compact_execution_checkpoint_payload,
)
from opc.core.evidence import compact_verification_evidence
from opc.core.event_persistence import TRANSIENT_RUNTIME_EVENT_TYPES
from opc.layer2_organization.metadata_ownership import (
    EXECUTION_COPY_KEYS,
    WORK_ITEM_OWNED_KEYS,
    supports_legacy_task_fallback,
)


_EVIDENCE_KEYS = frozenset(
    {
        "runtime_verification_evidence",
        "verification_evidence",
    }
)
_JSON_COLUMNS = (
    ("tasks", "id", "metadata"),
    ("tasks", "id", "context_snapshot"),
    ("delegation_work_items", "work_item_id", "metadata"),
    ("execution_checkpoints", "checkpoint_id", "payload"),
    ("runtime_sessions", "runtime_session_id", "metadata"),
)
_VOLATILE_INBOX_EVENT_KEYS = frozenset(
    {
        "comms_path",
        "processed_at",
        "timestamp",
        "timestamp_ms",
    }
)


@dataclass
class StorageCompactionReport:
    db_path: str
    applied: bool
    before_bytes: int
    after_bytes: int
    quick_check_before: str
    quick_check_after: str
    backup_path: str | None = None
    generic_runtime_duplicates: int = 0
    transient_runtime_events: int = 0
    redundant_inbox_events: int = 0
    deleted_payload_bytes: int = 0
    stripped_task_metadata_rows: int = 0
    backfilled_work_item_metadata_rows: int = 0
    compacted_json_rows: dict[str, int] = field(default_factory=dict)
    json_bytes_reduced: int = 0
    invalid_json_rows: int = 0

    @property
    def eligible_event_rows(self) -> int:
        return (
            self.generic_runtime_duplicates
            + self.transient_runtime_events
            + self.redundant_inbox_events
        )

    @property
    def estimated_payload_bytes_reclaimed(self) -> int:
        return self.deleted_payload_bytes + self.json_bytes_reduced

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["eligible_event_rows"] = self.eligible_event_rows
        result["estimated_payload_bytes_reclaimed"] = (
            self.estimated_payload_bytes_reclaimed
        )
        result["file_bytes_reclaimed"] = max(
            0,
            self.before_bytes - self.after_bytes,
        )
        return result


def _quick_check(connection: sqlite3.Connection) -> str:
    rows = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
    return "\n".join(rows) or "no result"


def _require_valid_database(connection: sqlite3.Connection, *, label: str) -> str:
    result = _quick_check(connection)
    if result != "ok":
        raise RuntimeError(f"{label} SQLite quick_check failed: {result}")
    return result


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _column_exists(
    connection: sqlite3.Connection,
    table: str,
    column: str,
) -> bool:
    if not _table_exists(connection, table):
        return False
    return any(
        str(row[1]) == column
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    )


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _compact_evidence_tree(value: Any) -> tuple[Any, int]:
    compacted_fields = 0
    if isinstance(value, dict):
        for key in list(value):
            child = value[key]
            if key in _EVIDENCE_KEYS and isinstance(child, dict):
                compacted = compact_verification_evidence(child)
                if compacted != child:
                    value[key] = compacted
                    compacted_fields += 1
                continue
            compacted_child, child_count = _compact_evidence_tree(child)
            if child_count:
                value[key] = compacted_child
                compacted_fields += child_count
        return value, compacted_fields
    if isinstance(value, list):
        for index, child in enumerate(value):
            compacted_child, child_count = _compact_evidence_tree(child)
            if child_count:
                value[index] = compacted_child
                compacted_fields += child_count
    return value, compacted_fields


def _stable_inbox_event_value(value: Any) -> Any:
    """Remove projection-only fields from an inbox event at every depth."""

    if isinstance(value, dict):
        return {
            key: _stable_inbox_event_value(child)
            for key, child in value.items()
            if key not in _VOLATILE_INBOX_EVENT_KEYS
        }
    if isinstance(value, list):
        return [_stable_inbox_event_value(child) for child in value]
    return value


def _event_fingerprint(payload: dict[str, Any]) -> str:
    stable = _stable_inbox_event_value(payload)
    encoded = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _delete_rowids(
    connection: sqlite3.Connection,
    table: str,
    rowids: list[int],
) -> None:
    for start in range(0, len(rowids), 900):
        batch = rowids[start : start + 900]
        placeholders = ",".join("?" for _item in batch)
        connection.execute(
            f'DELETE FROM "{table}" WHERE rowid IN ({placeholders})',
            batch,
        )


def _scan_generic_events(
    connection: sqlite3.Connection,
    *,
    apply: bool,
    report: StorageCompactionReport,
) -> None:
    if not _column_exists(connection, "events", "payload"):
        return

    duplicate_runtime_rowids: list[int] = []
    redundant_inbox_rowids: list[int] = []
    last_inbox_fingerprint: dict[str, str] = {}
    cursor = connection.execute(
        """
        SELECT rowid, payload, length(payload)
        FROM events
        WHERE event_type = 'runtime_event'
        ORDER BY timestamp ASC, rowid ASC
        """
    )
    for rowid, raw_payload, payload_size in cursor:
        try:
            payload = json.loads(raw_payload or "{}")
        except (json.JSONDecodeError, TypeError):
            report.invalid_json_rows += 1
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("runtime_session_id", "") or "").strip():
            duplicate_runtime_rowids.append(int(rowid))
            report.generic_runtime_duplicates += 1
            report.deleted_payload_bytes += int(payload_size or 0)
            continue
        if str(payload.get("type", "") or "").strip() != "member_inbox_updated":
            continue
        member_key = str(payload.get("member_session_id", "") or "").strip()
        if not member_key:
            member_key = "::".join(
                (
                    str(payload.get("role_id", "") or "").strip(),
                    str(payload.get("employee_id", "") or "").strip(),
                )
            )
        fingerprint = _event_fingerprint(payload)
        if last_inbox_fingerprint.get(member_key) == fingerprint:
            redundant_inbox_rowids.append(int(rowid))
            report.redundant_inbox_events += 1
            report.deleted_payload_bytes += int(payload_size or 0)
        else:
            last_inbox_fingerprint[member_key] = fingerprint

    if apply:
        _delete_rowids(connection, "events", duplicate_runtime_rowids)
        _delete_rowids(connection, "events", redundant_inbox_rowids)


def _scan_runtime_events(
    connection: sqlite3.Connection,
    *,
    apply: bool,
    report: StorageCompactionReport,
) -> None:
    if not _column_exists(connection, "runtime_events", "event_type"):
        return
    placeholders = ",".join("?" for _item in TRANSIENT_RUNTIME_EVENT_TYPES)
    params = sorted(TRANSIENT_RUNTIME_EVENT_TYPES)
    count, payload_bytes = connection.execute(
        f"""
        SELECT count(*), coalesce(sum(length(payload)), 0)
        FROM runtime_events
        WHERE event_type IN ({placeholders})
        """,
        params,
    ).fetchone()
    report.transient_runtime_events += int(count or 0)
    report.deleted_payload_bytes += int(payload_bytes or 0)
    if apply and count:
        connection.execute(
            f"DELETE FROM runtime_events WHERE event_type IN ({placeholders})",
            params,
        )


def _compact_json_columns(
    connection: sqlite3.Connection,
    *,
    apply: bool,
    report: StorageCompactionReport,
) -> None:
    checkpoint_type_available = _column_exists(
        connection,
        "execution_checkpoints",
        "checkpoint_type",
    )
    checkpoint_status_available = _column_exists(
        connection,
        "execution_checkpoints",
        "status",
    )
    for table, primary_key, column in _JSON_COLUMNS:
        if not _column_exists(connection, table, column):
            continue
        changed_rows = 0
        select_columns = f'"{primary_key}", "{column}"'
        if table == "execution_checkpoints" and checkpoint_type_available:
            select_columns += ', "checkpoint_type"'
            if checkpoint_status_available:
                select_columns += ', "status"'
        rows = connection.execute(
            f'SELECT {select_columns} FROM "{table}"'
        )
        for row in rows:
            row_id = str(row[0])
            raw_value = row[1]
            checkpoint_type = str(row[2] or "") if len(row) > 2 else ""
            checkpoint_status = (
                str(row[3] or "pending")
                if len(row) > 3
                else "pending"
            )
            try:
                value = json.loads(raw_value or "{}")
            except (json.JSONDecodeError, TypeError):
                report.invalid_json_rows += 1
                continue
            compacted, evidence_fields = _compact_evidence_tree(value)
            if table == "execution_checkpoints" and checkpoint_type:
                compacted = compact_execution_checkpoint_payload(
                    checkpoint_type,
                    compacted,
                    status=checkpoint_status,
                )
            encoded = _json_text(compacted)
            if encoded == (raw_value or ""):
                continue
            if not evidence_fields and compacted == value:
                continue
            changed_rows += 1
            report.json_bytes_reduced += max(
                0,
                len((raw_value or "").encode("utf-8"))
                - len(encoded.encode("utf-8")),
            )
            if apply:
                connection.execute(
                    f'UPDATE "{table}" SET "{column}"=? WHERE "{primary_key}"=?',
                    (encoded, row_id),
                )
        if changed_rows:
            report.compacted_json_rows[f"{table}.{column}"] = changed_rows


def _has_metadata_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def _repair_linked_metadata_ownership(
    connection: sqlite3.Connection,
    *,
    apply: bool,
    report: StorageCompactionReport,
) -> None:
    required_tables = {
        "tasks",
        "delegation_work_items",
        "work_item_runtime_links",
    }
    if not all(_table_exists(connection, table) for table in required_tables):
        return
    rows = connection.execute(
        """
        SELECT tasks.id,
               tasks.metadata,
               delegation_work_items.work_item_id,
               delegation_work_items.metadata
        FROM work_item_runtime_links
        JOIN tasks
          ON tasks.id = work_item_runtime_links.runtime_task_id
        JOIN delegation_work_items
          ON delegation_work_items.work_item_id =
             work_item_runtime_links.work_item_id
        """
    )
    for task_id, raw_task_metadata, work_item_id, raw_work_item_metadata in rows:
        try:
            task_metadata = json.loads(raw_task_metadata or "{}")
            work_item_metadata = json.loads(raw_work_item_metadata or "{}")
        except (json.JSONDecodeError, TypeError):
            report.invalid_json_rows += 1
            continue
        if not isinstance(task_metadata, dict) or not isinstance(
            work_item_metadata,
            dict,
        ):
            continue
        disallowed_keys = [
            key
            for key in WORK_ITEM_OWNED_KEYS
            if key not in EXECUTION_COPY_KEYS and key in task_metadata
        ]
        if not disallowed_keys:
            continue
        original_bytes = len((raw_task_metadata or "").encode("utf-8")) + len(
            (raw_work_item_metadata or "").encode("utf-8")
        )
        backfilled = False
        for key in disallowed_keys:
            task_value = task_metadata.pop(key)
            if (
                supports_legacy_task_fallback(key)
                and _has_metadata_value(task_value)
                and not _has_metadata_value(work_item_metadata.get(key))
            ):
                work_item_metadata[key] = task_value
                backfilled = True
        encoded_task = _json_text(task_metadata)
        encoded_work_item = _json_text(work_item_metadata)
        report.stripped_task_metadata_rows += 1
        if backfilled:
            report.backfilled_work_item_metadata_rows += 1
        report.json_bytes_reduced += max(
            0,
            original_bytes
            - len(encoded_task.encode("utf-8"))
            - len(encoded_work_item.encode("utf-8")),
        )
        if apply:
            connection.execute(
                "UPDATE tasks SET metadata=? WHERE id=?",
                (encoded_task, str(task_id)),
            )
            if backfilled:
                connection.execute(
                    """
                    UPDATE delegation_work_items
                    SET metadata=?
                    WHERE work_item_id=?
                    """,
                    (encoded_work_item, str(work_item_id)),
                )
    if report.stripped_task_metadata_rows:
        report.compacted_json_rows[
            "tasks.metadata_ownership"
        ] = report.stripped_task_metadata_rows
    if report.backfilled_work_item_metadata_rows:
        report.compacted_json_rows[
            "delegation_work_items.metadata_backfill"
        ] = report.backfilled_work_item_metadata_rows


def _process_database(
    connection: sqlite3.Connection,
    *,
    apply: bool,
    report: StorageCompactionReport,
) -> None:
    if apply:
        connection.execute("BEGIN IMMEDIATE")
    try:
        _scan_generic_events(connection, apply=apply, report=report)
        _scan_runtime_events(connection, apply=apply, report=report)
        _repair_linked_metadata_ownership(
            connection,
            apply=apply,
            report=report,
        )
        _compact_json_columns(connection, apply=apply, report=report)
    except Exception:
        if apply:
            connection.rollback()
        raise
    if apply:
        connection.commit()


def _sidecar_paths(db_path: Path) -> tuple[Path, Path]:
    return (
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
    )


def _source_signature(db_path: Path) -> tuple[Any, ...]:
    stat = db_path.stat()
    sidecars: list[tuple[str, int, int]] = []
    # A read-only SQLite connection may create or refresh ``-shm`` even
    # though no database page changed. Only a non-empty WAL carries durable
    # source changes that are not represented by the main-file signature.
    for sidecar in _sidecar_paths(db_path)[:1]:
        if sidecar.exists() and sidecar.stat().st_size > 0:
            item_stat = sidecar.stat()
            sidecars.append(
                (sidecar.name, item_stat.st_size, item_stat.st_mtime_ns)
            )
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        tuple(sidecars),
    )


def _open_process_ids(db_path: Path) -> list[int]:
    """Best-effort Linux check for processes holding the database inode."""

    proc = Path("/proc")
    if not proc.is_dir():
        return []
    target_stat = db_path.stat()
    current_pid = os.getpid()
    matches: set[int] = set()
    for process_dir in proc.iterdir():
        if not process_dir.name.isdigit():
            continue
        pid = int(process_dir.name)
        if pid == current_pid:
            continue
        fd_dir = process_dir / "fd"
        try:
            descriptors = fd_dir.iterdir()
        except (FileNotFoundError, PermissionError):
            continue
        try:
            for descriptor in descriptors:
                try:
                    descriptor_stat = descriptor.stat()
                except (FileNotFoundError, PermissionError):
                    continue
                if (
                    descriptor_stat.st_dev == target_stat.st_dev
                    and descriptor_stat.st_ino == target_stat.st_ino
                ):
                    matches.add(pid)
                    break
        except (FileNotFoundError, PermissionError):
            continue
    return sorted(matches)


def _require_offline_database(db_path: Path) -> None:
    process_ids = _open_process_ids(db_path)
    if process_ids:
        joined = ", ".join(str(pid) for pid in process_ids)
        raise RuntimeError(
            "database is open in another process; stop the OpenOPC runtime "
            f"before applying compaction (pids: {joined})"
        )


def _checkpoint_offline_database(db_path: Path) -> None:
    """Merge an offline WAL and remove sidecars before an atomic file swap."""

    _require_offline_database(db_path)
    connection = sqlite3.connect(db_path, timeout=5)
    try:
        busy, _log_pages, _checkpointed_pages = connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        if int(busy or 0):
            raise RuntimeError(
                "SQLite WAL checkpoint is busy; stop the OpenOPC runtime "
                "before applying compaction"
            )
        _require_valid_database(connection, label="source")
    finally:
        connection.close()

    wal_path, shm_path = _sidecar_paths(db_path)
    if wal_path.exists() and wal_path.stat().st_size > 0:
        raise RuntimeError(
            "SQLite WAL still contains pages after an offline checkpoint: "
            f"{wal_path}"
        )
    # With no process holding the database and an empty/truncated WAL, these
    # files are disposable SQLite transport state. Leaving an old SHM beside
    # the newly installed database could make its first open ambiguous.
    for sidecar in (wal_path, shm_path):
        if sidecar.exists():
            sidecar.unlink()
    _fsync_directory(db_path.parent)


def _default_backup_path(db_path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = db_path.with_name(f"{db_path.name}.backup-{timestamp}")
    if not candidate.exists():
        return candidate
    return db_path.with_name(
        f"{db_path.name}.backup-{timestamp}-{uuid.uuid4().hex[:8]}"
    )


def _copy_database(source_path: Path, target_path: Path) -> None:
    source = sqlite3.connect(
        f"file:{source_path}?mode=ro",
        uri=True,
        timeout=5,
    )
    target = sqlite3.connect(target_path)
    try:
        source.backup(target, pages=4096)
    finally:
        target.close()
        source.close()


def _fsync_path(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def inspect_project_storage(db_path: str | Path) -> StorageCompactionReport:
    """Analyze compaction candidates without changing the database."""

    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"project database does not exist: {path}")
    before_bytes = path.stat().st_size
    connection = sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
        timeout=5,
    )
    try:
        quick_check = _require_valid_database(connection, label="source")
        report = StorageCompactionReport(
            db_path=str(path),
            applied=False,
            before_bytes=before_bytes,
            after_bytes=before_bytes,
            quick_check_before=quick_check,
            quick_check_after=quick_check,
        )
        _process_database(connection, apply=False, report=report)
        return report
    finally:
        connection.close()


def compact_project_storage(
    db_path: str | Path,
    *,
    backup_path: str | Path | None = None,
) -> StorageCompactionReport:
    """Build, validate, and atomically install a compacted database copy."""

    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"project database does not exist: {path}")
    _require_offline_database(path)
    _checkpoint_offline_database(path)
    before_bytes = path.stat().st_size
    free_bytes = shutil.disk_usage(path.parent).free
    required_bytes = before_bytes + min(before_bytes, 512 * 1024 * 1024)
    if free_bytes < required_bytes:
        raise RuntimeError(
            "insufficient free disk space for recoverable compaction: "
            f"need at least {required_bytes} bytes, have {free_bytes}"
        )

    destination_backup = (
        Path(backup_path).expanduser().resolve()
        if backup_path is not None
        else _default_backup_path(path)
    )
    if destination_backup.exists():
        raise FileExistsError(f"backup target already exists: {destination_backup}")
    if destination_backup.parent != path.parent:
        raise ValueError(
            "backup must be in the database directory so the source rename is atomic"
        )

    temporary = path.with_name(f".{path.name}.compacting-{uuid.uuid4().hex}.tmp")
    source_signature = _source_signature(path)
    installed = False
    try:
        _copy_database(path, temporary)
        os.chmod(temporary, path.stat().st_mode & 0o777)
        connection = sqlite3.connect(temporary, timeout=30)
        try:
            quick_check_before = _require_valid_database(
                connection,
                label="working copy",
            )
            report = StorageCompactionReport(
                db_path=str(path),
                applied=True,
                before_bytes=before_bytes,
                after_bytes=before_bytes,
                quick_check_before=quick_check_before,
                quick_check_after="pending",
                backup_path=str(destination_backup),
            )
            _process_database(connection, apply=True, report=report)
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("VACUUM")
            report.quick_check_after = _require_valid_database(
                connection,
                label="compacted copy",
            )
        finally:
            connection.close()

        _fsync_path(temporary)
        _require_offline_database(path)
        if _source_signature(path) != source_signature:
            raise RuntimeError(
                "source database changed during compaction; refusing to replace it"
            )

        os.replace(path, destination_backup)
        try:
            os.replace(temporary, path)
            installed = True
        except Exception:
            os.replace(destination_backup, path)
            raise
        _fsync_directory(path.parent)

        installed_connection = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=5,
        )
        try:
            report.quick_check_after = _require_valid_database(
                installed_connection,
                label="installed compacted database",
            )
        finally:
            installed_connection.close()
        report.after_bytes = path.stat().st_size
        return report
    finally:
        if not installed and temporary.exists():
            temporary.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or safely compact an offline OpenOPC project database. "
            "Without --apply, no files are changed."
        )
    )
    parser.add_argument("database", type=Path, help="Path to tasks.db")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Install a validated compacted copy and retain the original as backup",
    )
    parser.add_argument(
        "--backup",
        type=Path,
        help="Backup path in the same directory (apply mode only)",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        report = (
            compact_project_storage(args.database, backup_path=args.backup)
            if args.apply
            else inspect_project_storage(args.database)
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(
        json.dumps(
            {"ok": True, **report.to_dict()},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
