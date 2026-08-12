from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from opc.operations.storage_retention import (
    apply_storage_retention,
    inspect_storage_retention,
)


def _write_with_age(path: Path, *, age_days: int, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(path.name.encode("utf-8"))
    modified = (now - timedelta(days=age_days)).timestamp()
    os.utime(path, (modified, modified))


def test_retention_is_dry_run_and_keeps_latest_backup(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    project = tmp_path / "projects" / "demo"
    newest = project / "tasks.db.backup-new"
    old = project / "tasks.db.backup-old"
    _write_with_age(newest, age_days=2, now=now)
    _write_with_age(old, age_days=90, now=now)

    report = inspect_storage_retention(
        tmp_path,
        keep_latest=1,
        max_age_days=30,
        now=now,
    )

    assert report.applied is False
    assert [Path(item.path).name for item in report.candidates] == [old.name]
    assert newest.exists()
    assert old.exists()


def test_inventory_counts_all_sqlite_database_files(tmp_path: Path) -> None:
    (tmp_path / "global.db").write_bytes(b"global")
    (tmp_path / "ui_state.sqlite3").write_bytes(b"ui")
    project = tmp_path / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "tasks.db").write_bytes(b"tasks")
    (project / "tasks.db.backup-old").write_bytes(b"backup")

    report = inspect_storage_retention(tmp_path)

    assert report.database_count == 3
    assert report.database_bytes == len(b"globaluitasks")
    assert report.backup_count == 1


def test_retention_apply_deletes_only_planned_generated_backups(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    project = tmp_path / "projects" / "demo"
    newest = project / "tasks.db.backup-new"
    old = project / "tasks.db.backup-old"
    unrelated = project / "manual-backup.db"
    _write_with_age(newest, age_days=2, now=now)
    _write_with_age(old, age_days=90, now=now)
    _write_with_age(unrelated, age_days=90, now=now)

    report = inspect_storage_retention(
        tmp_path,
        keep_latest=1,
        max_age_days=30,
        now=now,
    )
    apply_storage_retention(report)

    assert report.applied is True
    assert not old.exists()
    assert newest.exists()
    assert unrelated.exists()


def test_retention_refuses_candidate_changed_after_inspection(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    project = tmp_path / "projects" / "demo"
    _write_with_age(project / "tasks.db.backup-new", age_days=2, now=now)
    old = project / "tasks.db.backup-old"
    _write_with_age(old, age_days=90, now=now)
    report = inspect_storage_retention(
        tmp_path,
        keep_latest=1,
        max_age_days=30,
        now=now,
    )
    old.write_bytes(b"changed")

    try:
        apply_storage_retention(report)
    except RuntimeError as exc:
        assert "changed after inspection" in str(exc)
    else:
        raise AssertionError("changed backup should not be deleted")

    assert old.exists()
