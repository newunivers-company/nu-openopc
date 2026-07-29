from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from opc.operations.storage_maintenance import (
    compact_project_storage,
    inspect_project_storage,
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _seed_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            payload TEXT DEFAULT '{}',
            timestamp TEXT NOT NULL
        );
        CREATE TABLE runtime_events (
            event_id TEXT PRIMARY KEY,
            runtime_session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            metadata TEXT DEFAULT '{}',
            context_snapshot TEXT DEFAULT '{}'
        );
        CREATE TABLE delegation_work_items (
            work_item_id TEXT PRIMARY KEY,
            metadata TEXT DEFAULT '{}'
        );
        CREATE TABLE work_item_runtime_links (
            work_item_id TEXT PRIMARY KEY,
            runtime_task_id TEXT NOT NULL UNIQUE
        );
        CREATE TABLE execution_checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            checkpoint_type TEXT NOT NULL,
            payload TEXT DEFAULT '{}'
        );
        CREATE TABLE runtime_sessions (
            runtime_session_id TEXT PRIMARY KEY,
            metadata TEXT DEFAULT '{}'
        );
        """
    )
    inbox_base = {
        "type": "member_inbox_updated",
        "member_session_id": "member-1",
        "role_id": "executor",
        "pending_count": 0,
        "latest_notification": {
            "msg_id": "message-1",
            "status": "delivered",
            "timestamp": "2026-01-01T00:00:01",
            "processed_at": None,
            "comms_path": "/tmp/first-projection.md",
        },
    }
    event_rows = [
        (
            "native-copy",
            "runtime_event",
            _json(
                {
                    "type": "turn_completed",
                    "runtime_session_id": "runtime-1",
                }
            ),
            "2026-01-01T00:00:00",
        ),
        (
            "inbox-1",
            "runtime_event",
            _json({**inbox_base, "timestamp_ms": 1}),
            "2026-01-01T00:00:01",
        ),
        (
            "inbox-2",
            "runtime_event",
            _json(
                {
                    **inbox_base,
                    "timestamp_ms": 2,
                    "latest_notification": {
                        **inbox_base["latest_notification"],
                        "timestamp": "2026-01-01T00:00:02",
                        "comms_path": "/tmp/reprojected.md",
                    },
                }
            ),
            "2026-01-01T00:00:02",
        ),
        (
            "inbox-change",
            "runtime_event",
            _json({**inbox_base, "pending_count": 1, "timestamp_ms": 3}),
            "2026-01-01T00:00:03",
        ),
        (
            "durable",
            "task_updated",
            _json({"task_id": "task-1"}),
            "2026-01-01T00:00:04",
        ),
    ]
    connection.executemany(
        "INSERT INTO events VALUES (?, ?, ?, ?)",
        event_rows,
    )
    connection.executemany(
        "INSERT INTO runtime_events VALUES (?, ?, ?, ?, ?)",
        [
            (
                "delta-1",
                "runtime-1",
                "thinking_delta",
                _json({"text": "partial"}),
                "2026-01-01T00:00:00",
            ),
            (
                "delta-2",
                "runtime-1",
                "assistant_delta",
                _json({"text": "partial"}),
                "2026-01-01T00:00:01",
            ),
            (
                "turn-1",
                "runtime-1",
                "turn_completed",
                _json({"status": "done"}),
                "2026-01-01T00:00:02",
            ),
        ],
    )
    evidence = {
        "status": "provided",
        "verdict": "pass",
        "summary": "summary",
        "checks": [],
        "raw_output": "e" * 100_000,
    }
    connection.execute(
        "INSERT INTO tasks VALUES (?, ?, ?)",
        (
            "task-1",
            _json(
                {
                    "progress_log": ["legacy progress"],
                    "runtime_verification_evidence": evidence,
                }
            ),
            _json({"latest_artifacts": {"verification_evidence": evidence}}),
        ),
    )
    work_item_metadata = {
        "employee_assignment": {"employee_id": "employee-1"},
        "verification_evidence": evidence,
        "delegation_playbook": {"instructions": "preserve authoritative row"},
    }
    connection.execute(
        "INSERT INTO delegation_work_items VALUES (?, ?)",
        ("work-item-1", _json(work_item_metadata)),
    )
    connection.execute(
        "INSERT INTO work_item_runtime_links VALUES (?, ?)",
        ("work-item-1", "task-1"),
    )
    checkpoint_payload = {
        "version": 2,
        "active_work_items": [
            {
                "work_item_id": "work-item-1",
                "metadata": work_item_metadata,
            }
        ],
        "task_snapshots": [
            {
                "task_id": "task-1",
                "work_item": {
                    "work_item_id": "work-item-1",
                    "metadata": work_item_metadata,
                },
            }
        ],
    }
    connection.execute(
        "INSERT INTO execution_checkpoints VALUES (?, ?, ?)",
        (
            "checkpoint-1",
            "company_runtime_interrupted",
            _json(checkpoint_payload),
        ),
    )
    connection.execute(
        "INSERT INTO runtime_sessions VALUES (?, ?)",
        (
            "runtime-1",
            _json({"verification_evidence": evidence}),
        ),
    )
    connection.commit()
    connection.execute("VACUUM")
    connection.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_storage_compaction_dry_run_is_read_only(tmp_path: Path) -> None:
    database = tmp_path / "tasks.db"
    _seed_database(database)
    digest_before = _sha256(database)

    report = inspect_project_storage(database)

    assert report.applied is False
    assert report.quick_check_before == "ok"
    assert report.quick_check_after == "ok"
    assert report.generic_runtime_duplicates == 1
    assert report.redundant_inbox_events == 1
    assert report.transient_runtime_events == 2
    assert report.json_bytes_reduced > 0
    assert _sha256(database) == digest_before


def test_storage_compaction_keeps_backup_and_is_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tasks.db"
    backup = tmp_path / "tasks.db.before-compaction"
    _seed_database(database)
    original_digest = _sha256(database)
    original_size = database.stat().st_size

    report = compact_project_storage(database, backup_path=backup)

    assert report.applied is True
    assert report.quick_check_after == "ok"
    assert backup.is_file()
    assert _sha256(backup) == original_digest
    assert database.stat().st_size < original_size
    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 3
    assert (
        connection.execute("SELECT count(*) FROM runtime_events").fetchone()[0]
        == 1
    )
    work_item = json.loads(
        connection.execute(
            "SELECT metadata FROM delegation_work_items"
        ).fetchone()[0]
    )
    assert work_item["delegation_playbook"] == {
        "instructions": "preserve authoritative row"
    }
    assert work_item["progress_log"] == ["legacy progress"]
    assert len(work_item["verification_evidence"]["raw_output"]) <= 12_000
    task_metadata = json.loads(
        connection.execute("SELECT metadata FROM tasks").fetchone()[0]
    )
    assert "progress_log" not in task_metadata
    assert report.stripped_task_metadata_rows == 1
    assert report.backfilled_work_item_metadata_rows == 1
    checkpoint = json.loads(
        connection.execute(
            "SELECT payload FROM execution_checkpoints"
        ).fetchone()[0]
    )
    assert checkpoint["version"] == 3
    assert checkpoint["active_work_items"][0]["metadata"] == {
        "employee_assignment": {"employee_id": "employee-1"}
    }
    assert checkpoint["task_snapshots"][0]["work_item"]["metadata"] == {
        "employee_assignment": {"employee_id": "employee-1"}
    }
    connection.close()

    second_pass = inspect_project_storage(database)
    assert second_pass.eligible_event_rows == 0
    assert second_pass.json_bytes_reduced == 0
    assert second_pass.compacted_json_rows == {}
