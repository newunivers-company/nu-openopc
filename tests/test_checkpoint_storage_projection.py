from __future__ import annotations

import json
from pathlib import Path

import pytest

from opc.core.checkpoint_storage import (
    compact_execution_checkpoint_payload,
)
from opc.core.models import ExecutionCheckpoint
from opc.database.store import OPCStore


def test_delivery_checkpoint_drops_runtime_mirror_and_large_projection_maps() -> None:
    payload = {
        "waiting_task_id": "task-1",
        "member_session_state": {
            "member_session_id": "member-1",
            "inbox_state": {"messages": ["x" * 50_000]},
        },
        "delivery_package": {
            "executive_summary": "Delivered.",
            "artifact_manifest": [{"path": "result.txt"}],
            "role_task_map": {
                "executor": {"work_items": ["x" * 50_000]},
            },
            "source_projection_refs": [
                {
                    "projection_id": "execution",
                    "employee_assignment": {"prompt_context": "x" * 50_000},
                }
            ],
        },
    }

    compacted = compact_execution_checkpoint_payload(
        "company_delivery_feedback",
        payload,
        status="pending",
    )

    assert "member_session_state" not in compacted
    assert compacted["member_session_id"] == "member-1"
    assert compacted["delivery_package"] == {
        "executive_summary": "Delivered.",
        "artifact_manifest": [{"path": "result.txt"}],
        "role_ids": ["executor"],
        "source_projection_ids": ["execution"],
    }
    assert len(json.dumps(compacted)) < 2_000
    assert compact_execution_checkpoint_payload(
        "company_delivery_feedback",
        compacted,
        status="pending",
    ) == compacted


def test_terminal_runtime_checkpoint_keeps_audit_but_drops_resume_tokens() -> None:
    payload = {
        "version": 3,
        "reason": "operator_stop",
        "task_ids": ["task-1"],
        "active_work_items": [{"work_item_id": "work-1"}],
        "task_snapshots": [{"task_id": "task-1"}],
        "native_runtime_resume": {"task-1": {"token": "secret"}},
        "adapter_session_state": {"role-1": {"token": "secret"}},
        "external_sessions": {"task-1": {"session_id": "secret"}},
    }

    active = compact_execution_checkpoint_payload(
        "company_runtime_interrupted",
        payload,
        status="pending",
    )
    archived = compact_execution_checkpoint_payload(
        "company_runtime_interrupted",
        payload,
        status="resolved",
    )

    assert active["task_snapshots"] == [{"task_id": "task-1"}]
    assert archived["reason"] == "operator_stop"
    assert archived["task_ids"] == ["task-1"]
    assert not {
        "active_work_items",
        "task_snapshots",
        "native_runtime_resume",
        "adapter_session_state",
        "external_sessions",
    } & archived.keys()
    assert archived["archived_runtime_snapshot_counts"] == {
        "active_work_items": 1,
        "task_snapshots": 1,
        "native_runtime_resume": 1,
        "adapter_session_state": 1,
        "external_sessions": 1,
    }


def test_staffing_pool_is_available_while_pending_and_archived_after_resolution() -> None:
    payload = {
        "summary": "Choose staff.",
        "staffing_pool": {
            "employees": [{"employee_id": "employee-1"}],
            "templates": [{"template_id": "template-1"}],
        },
    }

    pending = compact_execution_checkpoint_payload(
        "company_staffing_selection",
        payload,
        status="pending",
    )
    resolved = compact_execution_checkpoint_payload(
        "company_staffing_selection",
        payload,
        status="resolved",
    )

    assert "staffing_pool" in pending
    assert "staffing_pool" not in resolved
    assert resolved["archived_staffing_pool_counts"] == {
        "employees": 1,
        "templates": 1,
    }


@pytest.mark.asyncio
async def test_store_resolution_applies_terminal_projection(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="checkpoint-1",
            project_id="project-1",
            session_id="session-1",
            checkpoint_type="company_staffing_selection",
            payload={
                "summary": "Choose staff.",
                "staffing_pool": {
                    "employees": [{"employee_id": "employee-1"}],
                },
            },
        )
        await store.save_execution_checkpoint(checkpoint)

        await store.resolve_execution_checkpoint(
            checkpoint.checkpoint_id,
            status="resolved",
        )

        rows = await store.get_execution_checkpoints(
            project_id="project-1",
        )
        assert len(rows) == 1
        assert rows[0].status == "resolved"
        assert "staffing_pool" not in rows[0].payload
        assert rows[0].payload["archived_staffing_pool_counts"] == {
            "employees": 1,
        }
    finally:
        await store.close()
