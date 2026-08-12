from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from opc.core.models import Task, TaskStatus
from opc.layer2_organization.company_runtime import CompanyRuntime


class _EmptyInbox:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def read_inbox(self, **_kwargs):
        return list(self.messages)


class CompanyRuntimeInboxDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_inbox_poll_does_not_persist_or_emit_again(self) -> None:
        saved = AsyncMock()
        emitted: list[tuple[str, dict]] = []
        communication = _EmptyInbox()

        async def _emit(event_type: str, payload: dict) -> None:
            emitted.append((event_type, payload))

        runtime = CompanyRuntime(
            org_engine=None,
            communication=communication,
            save_runtime_session=saved,
            emit_runtime_event=_emit,
        )
        task = Task(
            id="task-1",
            session_id="session-1",
            project_id="project-1",
            title="Execute",
            assigned_to="executor",
            status=TaskStatus.PENDING,
            metadata={
                "work_item_projection_id": "execution",
                "work_item_role_id": "executor",
            },
        )

        await runtime.bootstrap([task])
        saves_after_bootstrap = saved.await_count
        inbox_events_after_bootstrap = sum(
            event_type == "member_inbox_updated" for event_type, _payload in emitted
        )

        await runtime.refresh_inbox_state([task])

        self.assertEqual(saved.await_count, saves_after_bootstrap)
        self.assertEqual(
            sum(
                event_type == "member_inbox_updated" for event_type, _payload in emitted
            ),
            inbox_events_after_bootstrap,
        )

        communication.messages.append(
            {
                "msg_id": "message-1",
                "from_agent": "reviewer",
                "subject": "Need an update",
                "body": "Please report progress.",
                "reply_needed": True,
            }
        )
        await runtime.refresh_inbox_state([task])

        self.assertEqual(saved.await_count, saves_after_bootstrap + 1)
        self.assertEqual(
            sum(
                event_type == "member_inbox_updated" for event_type, _payload in emitted
            ),
            inbox_events_after_bootstrap + 1,
        )


if __name__ == "__main__":
    unittest.main()
