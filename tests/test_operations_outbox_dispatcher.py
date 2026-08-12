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
from opc.operations.outbox import IndependentOutboxWorker, OutboxDispatcher, event_bus_handler
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

    async def test_independent_worker_persists_receipt_and_deduplicates_after_restart(self) -> None:
        message = await self._append("independent")
        delivered = []

        async def handle(current):
            delivered.append(current.message_id)

        first = IndependentOutboxWorker(
            self.kernel,
            handle,
            consumer_id="analytics-v1",
            worker_id="independent-a",
        )
        first_report = await first.dispatch_once()
        receipt = await self.repository.get_outbox_delivery_receipt(
            message.message_id,
            "analytics-v1",
        )
        self.assertEqual(first_report.delivered, 1)
        self.assertIsNotNone(receipt)
        self.assertEqual(delivered, [message.message_id])

        await self.repository.db.execute(
            """UPDATE outbox_messages SET status = 'pending', delivered_at = NULL,
               next_attempt_at = ?, lease_owner = '', lease_expires_at = NULL
               WHERE message_id = ?""",
            (message.created_at.isoformat(), message.message_id),
        )
        await self.repository.db.commit()

        async def must_not_repeat(_current):
            raise AssertionError("completed receipt must suppress duplicate side effect")

        restarted = IndependentOutboxWorker(
            self.kernel,
            must_not_repeat,
            consumer_id="analytics-v1",
            worker_id="independent-b",
        )
        second_report = await restarted.dispatch_once()
        self.assertEqual(second_report.deduplicated, 1)
        self.assertEqual(second_report.delivered, 0)

    async def test_external_side_effect_crash_window_recovers_with_consumer_idempotency(self) -> None:
        kernel = DurableRunKernel(
            self.repository,
            DurableOperationsConfig(
                lease_seconds=5,
                outbox_max_attempts=2,
                retry_base_seconds=1,
                retry_max_seconds=1,
            ),
        )
        appended = await kernel.record_event(
            run_id="run-dispatch",
            event_type="test.crash-window",
            payload={"key": "crash-window"},
            idempotency_key="dispatch:crash-window",
            outbox_topic="operations.test",
        )
        assert appended.outbox is not None
        message = appended.outbox
        applied_ids: set[str] = set()
        effects: list[str] = []

        async def crash_after_external_effect(current):
            if current.message_id not in applied_ids:
                applied_ids.add(current.message_id)
                effects.append(current.message_id)
            raise asyncio.CancelledError("simulated process crash before receipt")

        crashed = IndependentOutboxWorker(
            kernel,
            crash_after_external_effect,
            consumer_id="billing-v1",
            worker_id="crashed-worker",
        )
        with self.assertRaises(asyncio.CancelledError):
            await crashed.dispatch_once()
        self.assertEqual(effects, [message.message_id])
        self.assertIsNone(
            await self.repository.get_outbox_delivery_receipt(message.message_id, "billing-v1")
        )

        await self.repository.db.execute(
            "UPDATE outbox_messages SET lease_expires_at = ? WHERE message_id = ?",
            ("2000-01-01T00:00:00+00:00", message.message_id),
        )
        await self.repository.db.commit()
        self.assertEqual(await kernel.recover_expired_outbox(), 1)

        async def idempotent_retry(current):
            if current.message_id not in applied_ids:
                applied_ids.add(current.message_id)
                effects.append(current.message_id)

        restarted = IndependentOutboxWorker(
            kernel,
            idempotent_retry,
            consumer_id="billing-v1",
            worker_id="restarted-worker",
        )
        report = await restarted.dispatch_once()
        receipt = await self.repository.get_outbox_delivery_receipt(
            message.message_id,
            "billing-v1",
        )

        self.assertEqual(report.delivered, 1)
        self.assertEqual(effects, [message.message_id])
        self.assertIsNotNone(receipt)


if __name__ == "__main__":
    unittest.main()
