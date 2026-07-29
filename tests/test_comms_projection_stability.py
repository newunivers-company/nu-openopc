from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from opc.core.events import EventBus
from opc.core.models import AgentMessage, MessageStatus, Task
from opc.database.store import OPCStore
from opc.layer2_organization import comms as file_comms
from opc.layer2_organization.communication import CommunicationManager


@pytest.mark.asyncio
async def test_file_projection_preserves_source_identity_and_skips_unchanged_write(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        layout = file_comms.resolve_layout(tmp_path, "project-1", "session-1")
        file_comms.ensure_layout(layout, ["sender", "reader"])
        file_comms.send_message(
            layout,
            from_role="sender",
            to_role="reader",
            subject="Stable notification",
            body="The durable result is ready.",
            message_id="message-1",
            sent_at="2026-07-29T06:07:12",
            extra_frontmatter={
                "msg_type": "inform",
                "reply_needed": False,
                "task_id": "source-task",
                "context_ref": "source-context",
                "refs": {
                    "task_id": "source-task",
                    "projection_id": "source-projection",
                    "session_id": "source-session",
                },
            },
        )
        manager = CommunicationManager(store, EventBus())
        original_save = store.save_message
        store.save_message = AsyncMock(wraps=original_save)  # type: ignore[method-assign]

        first_reader = Task(
            id="reader-task-1",
            title="Read",
            assigned_to="reader",
            project_id="project-1",
        )
        second_reader = Task(
            id="reader-task-2",
            title="Read again",
            assigned_to="reader",
            project_id="project-1",
        )

        first = await manager._project_comms_messages(
            layout,
            role_id="reader",
            task=first_reader,
            unread_only=True,
            limit=10,
            mark_read=False,
        )
        second = await manager._project_comms_messages(
            layout,
            role_id="reader",
            task=second_reader,
            unread_only=True,
            limit=10,
            mark_read=False,
        )

        assert first == second
        assert store.save_message.await_count == 1
        projected = await store.get_message("message-1")
        assert projected is not None
        assert projected.msg_type == "inform"
        assert projected.task_id == "source-task"
        assert projected.context_ref == "source-context"
        assert projected.timestamp == datetime.fromisoformat("2026-07-29T06:07:12")
        assert projected.refs == {
            "task_id": "source-task",
            "projection_id": "source-projection",
            "session_id": "source-session",
        }
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_mark_read_updates_database_in_same_projection_pass(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        layout = file_comms.resolve_layout(tmp_path, "project-1", "session-1")
        file_comms.ensure_layout(layout, ["sender", "reader"])
        file_comms.send_message(
            layout,
            from_role="sender",
            to_role="reader",
            subject="Acknowledge",
            body="Please archive this.",
            message_id="message-1",
            sent_at="2026-07-29T06:07:12",
        )
        manager = CommunicationManager(store, EventBus())

        projected = await manager._project_comms_messages(
            layout,
            role_id="reader",
            task=None,
            unread_only=True,
            limit=10,
            mark_read=True,
        )

        assert projected[0]["status"] == MessageStatus.READ.value
        stored = await store.get_message("message-1")
        assert stored is not None
        assert stored.status == MessageStatus.READ
        assert stored.processed_at is not None
        assert not file_comms.list_unread(layout, "reader")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_queue_rehydration_is_idempotent_within_manager(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        await store.save_message(
            AgentMessage(
                msg_id="message-1",
                from_agent="sender",
                to_agents=["reader"],
                status=MessageStatus.DELIVERED,
            )
        )
        manager = CommunicationManager(store, EventBus())

        assert await manager.rehydrate_queues() == 1
        assert await manager.rehydrate_queues() == 0
        assert manager._get_queue("reader").qsize() == 1
    finally:
        await store.close()
