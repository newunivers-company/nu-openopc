"""Dry-run-first storage inventory and generated-backup retention."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_GENERATED_BACKUP_PREFIX = "tasks.db.backup-"


@dataclass(frozen=True)
class RetentionCandidate:
    path: str
    size_bytes: int
    modified_at: str
    device: int
    inode: int


@dataclass
class StorageRetentionReport:
    root: str
    applied: bool
    total_bytes: int = 0
    database_bytes: int = 0
    backup_bytes: int = 0
    log_bytes: int = 0
    file_count: int = 0
    database_count: int = 0
    backup_count: int = 0
    candidate_bytes: int = 0
    candidates: list[RetentionCandidate] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    largest_files: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_generated_backup(path: Path) -> bool:
    return path.name.startswith(_GENERATED_BACKUP_PREFIX)


def _is_database(path: Path) -> bool:
    return (
        not _is_generated_backup(path)
        and path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
    )


def _regular_files(root: Path) -> list[tuple[Path, os.stat_result]]:
    files: list[tuple[Path, os.stat_result]] = []
    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            files.append((path, path.stat()))
        except (FileNotFoundError, PermissionError):
            continue
    return files


def inspect_storage_retention(
    root: str | Path,
    *,
    keep_latest: int = 3,
    max_age_days: int = 30,
    now: datetime | None = None,
) -> StorageRetentionReport:
    """Inventory an OPC root and plan safe pruning without changing files."""

    if keep_latest < 1:
        raise ValueError("keep_latest must be at least 1")
    if max_age_days < 1:
        raise ValueError("max_age_days must be at least 1")
    storage_root = Path(root).expanduser().resolve()
    if not storage_root.is_dir():
        raise FileNotFoundError(f"storage root does not exist: {storage_root}")
    effective_now = now or datetime.now(timezone.utc)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=timezone.utc)
    cutoff = effective_now - timedelta(days=max_age_days)

    files = _regular_files(storage_root)
    report = StorageRetentionReport(root=str(storage_root), applied=False)
    report.file_count = len(files)
    report.total_bytes = sum(item.st_size for _path, item in files)

    backups_by_directory: dict[Path, list[tuple[Path, os.stat_result]]] = {}
    for path, stat in files:
        if _is_database(path):
            report.database_count += 1
            report.database_bytes += stat.st_size
        if _is_generated_backup(path):
            report.backup_count += 1
            report.backup_bytes += stat.st_size
            backups_by_directory.setdefault(path.parent, []).append((path, stat))
        if path.suffix.lower() in {".log", ".jsonl"}:
            report.log_bytes += stat.st_size

    candidates: list[RetentionCandidate] = []
    for backups in backups_by_directory.values():
        ordered = sorted(backups, key=lambda item: item[1].st_mtime_ns, reverse=True)
        for path, stat in ordered[keep_latest:]:
            modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if modified > cutoff:
                continue
            candidates.append(
                RetentionCandidate(
                    path=str(path),
                    size_bytes=stat.st_size,
                    modified_at=modified.isoformat(),
                    device=stat.st_dev,
                    inode=stat.st_ino,
                )
            )
    report.candidates = sorted(candidates, key=lambda item: item.modified_at)
    report.candidate_bytes = sum(item.size_bytes for item in report.candidates)
    report.largest_files = [
        {
            "path": str(path.relative_to(storage_root)),
            "size_bytes": stat.st_size,
        }
        for path, stat in sorted(
            files,
            key=lambda item: item[1].st_size,
            reverse=True,
        )[:20]
    ]
    return report


def apply_storage_retention(report: StorageRetentionReport) -> StorageRetentionReport:
    """Delete only unchanged generated backups from a prior retention plan."""

    storage_root = Path(report.root).resolve()
    removed: list[str] = []
    for candidate in report.candidates:
        path = Path(candidate.path)
        resolved = path.resolve()
        if (
            resolved.parent != path.parent.resolve()
            or not resolved.is_relative_to(storage_root)
        ):
            raise RuntimeError(f"retention candidate escaped storage root: {path}")
        if path.is_symlink() or not _is_generated_backup(path):
            raise RuntimeError(f"refusing unsafe retention candidate: {path}")
        stat = path.stat()
        if (stat.st_dev, stat.st_ino, stat.st_size) != (
            candidate.device,
            candidate.inode,
            candidate.size_bytes,
        ):
            raise RuntimeError(f"retention candidate changed after inspection: {path}")
        path.unlink()
        removed.append(str(path))
    report.applied = True
    report.removed = removed
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path(".opc"))
    parser.add_argument("--keep-latest", type=int, default=3)
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete the exact unchanged candidates printed by the dry-run policy",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        report = inspect_storage_retention(
            args.root,
            keep_latest=args.keep_latest,
            max_age_days=args.max_age_days,
        )
        if args.apply:
            apply_storage_retention(report)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **report.to_dict()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
