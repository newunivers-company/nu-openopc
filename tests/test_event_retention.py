from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opc.core.event_persistence import (
    should_persist_generic_event,
    should_persist_runtime_event,
)
from opc.core.events import EventBus
from opc.core.models import OPCEvent
from opc.engine import OPCEngine
from opc.layer3_agent.runtime_v2.runtime import NativeRuntimeV2


class EventRetentionTests(unittest.IsolatedAsyncioTestCase):
    def test_event_durability_policy_is_explicit_and_shared(self) -> None:
        self.assertFalse(should_persist_runtime_event("assistant_delta"))
        self.assertTrue(should_persist_runtime_event("turn_completed"))
        self.assertFalse(
            should_persist_generic_event(
                "runtime_event",
                {"runtime_session_id": "runtime-1"},
            )
        )
        self.assertTrue(
            should_persist_generic_event(
                "runtime_event",
                {"type": "member_inbox_updated"},
            )
        )

    async def test_event_bus_keeps_only_bounded_recent_history(self) -> None:
        bus = EventBus(history_limit=3)

        for index in range(5):
            await bus.publish(
                OPCEvent(
                    event_type="runtime_event",
                    payload={"index": index},
                )
            )

        self.assertEqual(
            [event.payload["index"] for event in bus.get_history(limit=10)],
            [2, 3, 4],
        )

    async def test_native_runtime_event_is_not_duplicated_in_generic_store(
        self,
    ) -> None:
        store = SimpleNamespace(save_event=AsyncMock())
        engine = SimpleNamespace(store=store)

        await OPCEngine._persist_event(
            engine,
            OPCEvent(
                event_type="runtime_event",
                payload={
                    "type": "thinking_delta",
                    "runtime_session_id": "runtime-1",
                },
            ),
        )
        await OPCEngine._persist_event(
            engine,
            OPCEvent(
                event_type="runtime_event",
                payload={"type": "member_inbox_updated"},
            ),
        )
        await OPCEngine._persist_event(
            engine,
            OPCEvent(
                event_type="task_updated",
                payload={"task_id": "task-1"},
            ),
        )

        self.assertEqual(store.save_event.await_count, 2)
        persisted = [
            call.args[0].event_type for call in store.save_event.await_args_list
        ]
        self.assertEqual(persisted, ["runtime_event", "task_updated"])

    async def test_streaming_deltas_remain_live_but_are_not_durable(self) -> None:
        store = SimpleNamespace(save_runtime_event=AsyncMock())
        event_bus = SimpleNamespace(publish=AsyncMock())
        runtime = object.__new__(NativeRuntimeV2)
        runtime.memory_manager = SimpleNamespace(store=store)
        runtime.event_bus = event_bus

        await runtime._emit_runtime_event(
            "runtime-1",
            None,
            "thinking_delta",
            {"text": "working"},
        )
        await runtime._emit_runtime_event(
            "runtime-1",
            None,
            "turn_completed",
            {"status": "done"},
        )

        self.assertEqual(event_bus.publish.await_count, 2)
        store.save_runtime_event.assert_awaited_once()
        self.assertEqual(
            store.save_runtime_event.await_args.args[1],
            "turn_completed",
        )


if __name__ == "__main__":
    unittest.main()
