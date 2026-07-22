from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
import unittest

from opc.core.config import DurableOperationsConfig
from opc.core.events import EventBus
from opc.database.store import OPCStore
from opc.operations.durable import DurableRunKernel
from opc.operations.models import AcceptanceCriterion, GoalContract, RunManifest, RunStatus
from opc.operations.outbox import OutboxDispatcher, event_bus_handler
from opc.operations.repository import OperationsRepository


class _RecordingEventBus:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, event) -> None:
        self.events.append(event)


class OutboxDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.repository = OperationsRepository(self.store)
        self.kernel = DurableRunKernel(
            self.repository,
            DurableOperationsConfig(
                lease_seconds=5,
                outbox_max_attempts=1,
                retry_base_seconds=1,
                retry_max_seconds=1,
            ),
        )
        await self.repository.save_goal(
            GoalContract(
                goal_id="goal-dispatch",
                title="Deliver outbox",
                objective="Prove background delivery semantics.",
                acceptance_criteria=[AcceptanceCriterion("delivery", "Message delivered")],
            )
        )
        await self.repository.save_manifest(
            RunManifest(
                run_id="run-dispatch",
                goal_id="goal-dispatch",
                status=RunStatus.RUNNING,
            )
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _append(self, key: str = "one"):
        result = await self.kernel.record_event(
            run_id="run-dispatch",
            event_type=f"test.{key}",
            payload={"key": key},
            idempotency_key=f"dispatch:{key}",
            outbox_topic="operations.test",
        )
        assert result.outbox is not None
        return result.outbox

    async def test_dispatch_once_publishes_then_acknowledges(self) -> None:
        message = await self._append()
        bus = _RecordingEventBus()
        dispatcher = OutboxDispatcher(
            self.kernel,
            event_bus_handler(bus),
            worker_id="test-worker",
        )

        report = await dispatcher.dispatch_once()
        stored = (await self.repository.list_outbox(run_id="run-dispatch"))[0]

        self.assertEqual(report.to_dict(), {"claimed": 1, "delivered": 1, "failed": 0, "dead_lettered": 0})
        self.assertEqual(stored.status, "delivered")
        self.assertEqual(bus.events[0].event_type, "operations.test")
        self.assertEqual(bus.events[0].payload["outbox_message_id"], message.message_id)

    async def test_handler_failure_uses_existing_dead_letter_policy(self) -> None:
        await self._append()

        async def fail(_message):
            raise RuntimeError("consumer down")

        dispatcher = OutboxDispatcher(self.kernel, fail, worker_id="test-worker")
        report = await dispatcher.dispatch_once()
        stored = (await self.repository.list_outbox(run_id="run-dispatch"))[0]

        self.assertEqual(report.failed, 1)
        self.assertEqual(report.dead_lettered, 1)
        self.assertEqual(stored.status, "dead_letter")
        self.assertIn("consumer down", stored.last_error)

    async def test_event_bus_listener_failure_is_not_acknowledged(self) -> None:
        await self._append("listener-failure")
        bus = EventBus()

        async def fail(_event):
            raise RuntimeError("listener down")

        bus.subscribe("operations.test", fail)
        dispatcher = OutboxDispatcher(
            self.kernel,
            event_bus_handler(bus),
            worker_id="checked-event-worker",
        )

        report = await dispatcher.dispatch_once()
        stored = (await self.repository.list_outbox(run_id="run-dispatch"))[0]

        self.assertEqual(report.failed, 1)
        self.assertEqual(report.delivered, 0)
        self.assertEqual(stored.status, "dead_letter")
        self.assertIn("listener down", stored.last_error)

    async def test_background_worker_drains_and_stops_cleanly(self) -> None:
        await self._append("background")
        delivered = asyncio.Event()

        async def handle(_message):
            delivered.set()

        dispatcher = OutboxDispatcher(
            self.kernel,
            handle,
            worker_id="background-worker",
            poll_seconds=0.05,
        )
        await dispatcher.start()
        await asyncio.wait_for(delivered.wait(), timeout=2)
        await dispatcher.stop()

        self.assertFalse(dispatcher.running)
        stored = (await self.repository.list_outbox(run_id="run-dispatch"))[0]
        self.assertEqual(stored.status, "delivered")


if __name__ == "__main__":
    unittest.main()
