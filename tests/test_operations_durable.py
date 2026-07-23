from __future__ import annotations

import asyncio
import multiprocessing
import tempfile
from datetime import timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

from opc.core.config import DurableOperationsConfig
from opc.database.store import OPCStore
from opc.operations.durable import (
    AggregateVersionConflict,
    BudgetExceeded,
    DurableOperationsError,
    DurableRunKernel,
    LeaseUnavailable,
    StaleFencingToken,
)
from opc.operations.models import (
    AcceptanceCriterion,
    GoalContract,
    ResourceBudget,
    RunManifest,
    RunMetrics,
    RunStatus,
    utc_now,
)
from opc.operations.repository import OperationsRepository


def _append_from_process(db_path: str, event_type: str, start_event, result_queue) -> None:
    async def run() -> None:
        store = OPCStore(Path(db_path))
        await store.initialize()
        try:
            start_event.wait(timeout=10)
            kernel = DurableRunKernel(
                OperationsRepository(store),
                DurableOperationsConfig(),
            )
            await kernel.record_event(
                run_id="run-durable",
                event_type=event_type,
                expected_version=0,
            )
            result_queue.put(("ok", event_type))
        except Exception as exc:
            result_queue.put((type(exc).__name__, str(exc)))
        finally:
            await store.close()

    asyncio.run(run())


class DurableRunKernelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "tasks.db"
        self.store = OPCStore(self.db_path)
        await self.store.initialize()
        self.repository = OperationsRepository(self.store)
        self.kernel = DurableRunKernel(
            self.repository,
            DurableOperationsConfig(
                lease_seconds=10,
                outbox_max_attempts=2,
                retry_base_seconds=1,
                retry_max_seconds=4,
                deadlock_after_seconds=30,
            ),
        )
        await self.repository.save_goal(
            GoalContract(
                goal_id="goal-durable",
                title="Survive interruptions",
                objective="Keep every transition durable and recoverable.",
                acceptance_criteria=[
                    AcceptanceCriterion("durability", "No event or delivery is lost")
                ],
                budget=ResourceBudget(max_cost_usd=1.0, max_failed_attempts=1),
            )
        )
        await self.repository.save_manifest(
            RunManifest(
                run_id="run-durable",
                goal_id="goal-durable",
                status=RunStatus.RUNNING,
                started_at=utc_now(),
            )
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def test_event_and_outbox_are_atomic_and_idempotent(self) -> None:
        first = await self.kernel.record_event(
            run_id="run-durable",
            event_type="work.started",
            payload={"work_item_id": "w1"},
            idempotency_key="start:w1",
            outbox_topic="work.events",
        )
        duplicate = await self.kernel.record_event(
            run_id="run-durable",
            event_type="work.started",
            payload={"work_item_id": "changed"},
            idempotency_key="start:w1",
            outbox_topic="work.events",
        )

        self.assertFalse(first.duplicate)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.event.event_id, first.event.event_id)
        self.assertEqual(duplicate.outbox.message_id, first.outbox.message_id)
        with self.assertRaisesRegex(DurableOperationsError, "idempotency key collision"):
            await self.kernel.record_event(
                run_id="run-durable",
                event_type="work.finished",
                idempotency_key="start:w1",
            )
        self.assertEqual(len(await self.repository.list_events("run-durable")), 1)
        self.assertEqual(len(await self.repository.list_outbox(run_id="run-durable")), 1)

    async def test_expected_aggregate_version_prevents_lost_updates(self) -> None:
        await self.kernel.record_event(
            run_id="run-durable",
            event_type="work.started",
            expected_version=0,
        )
        with self.assertRaises(AggregateVersionConflict):
            await self.kernel.record_event(
                run_id="run-durable",
                event_type="work.finished",
                expected_version=0,
                outbox_topic="work.events",
            )

        events = await self.repository.list_events("run-durable")
        self.assertEqual([item["event_type"] for item in events], ["work.started"])
        self.assertEqual(await self.repository.list_outbox(run_id="run-durable"), [])

    async def test_fencing_rejects_worker_after_lease_takeover(self) -> None:
        now = utc_now()
        lease_a = await self.kernel.acquire_lease(
            "run-durable", owner="worker-a", lease_seconds=5, now=now
        )
        with self.assertRaises(LeaseUnavailable):
            await self.kernel.acquire_lease(
                "run-durable", owner="worker-b", lease_seconds=5, now=now
            )
        lease_b = await self.kernel.acquire_lease(
            "run-durable",
            owner="worker-b",
            lease_seconds=5,
            now=now + timedelta(seconds=6),
        )

        self.assertGreater(lease_b.fencing_token, lease_a.fencing_token)
        with self.assertRaises(StaleFencingToken):
            await self.kernel.record_event(
                run_id="run-durable",
                event_type="stale.write",
                lease_owner=lease_a.owner,
                fencing_token=lease_a.fencing_token,
                now=now + timedelta(seconds=6),
            )
        accepted = await self.kernel.record_event(
            run_id="run-durable",
            event_type="current.write",
            lease_owner=lease_b.owner,
            fencing_token=lease_b.fencing_token,
            now=now + timedelta(seconds=6),
        )
        self.assertEqual(accepted.event.event_type, "current.write")

    async def test_released_lease_keeps_fencing_token_monotonic(self) -> None:
        now = utc_now()
        first = await self.kernel.acquire_lease(
            "run-durable",
            owner="worker-a",
            now=now,
        )
        await self.kernel.release_lease(first, now=now + timedelta(seconds=1))
        second = await self.kernel.acquire_lease(
            "run-durable",
            owner="worker-a",
            now=now + timedelta(seconds=1),
        )

        self.assertGreater(second.fencing_token, first.fencing_token)
        with self.assertRaises(StaleFencingToken):
            await self.kernel.assert_fence(
                "run-durable",
                owner=first.owner,
                fencing_token=first.fencing_token,
                now=now + timedelta(seconds=1),
            )

    async def test_run_lifecycle_state_and_events_commit_atomically(self) -> None:
        manifest = RunManifest(
            run_id="run-atomic",
            goal_id="goal-durable",
        )
        with patch.object(
            self.kernel,
            "_append_event_locked",
            side_effect=RuntimeError("simulated event failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated event failure"):
                await self.kernel.start_run(manifest)
        self.assertIsNone(await self.repository.get_manifest("run-atomic"))

        started, start_result = await self.kernel.start_run(manifest)
        self.assertEqual(started.status, RunStatus.RUNNING)
        self.assertEqual(start_result.event.event_type, "run.started")
        with patch.object(
            self.kernel,
            "_append_event_locked",
            side_effect=RuntimeError("simulated delivery failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated delivery failure"):
                await self.kernel.finish_run(
                    "run-atomic",
                    status=RunStatus.COMPLETED,
                )
        unchanged = await self.repository.get_manifest("run-atomic")
        assert unchanged is not None
        self.assertEqual(unchanged.status, RunStatus.RUNNING)
        self.assertIsNone(unchanged.completed_at)

        finished, finish_result = await self.kernel.finish_run(
            "run-atomic",
            status=RunStatus.COMPLETED,
        )
        duplicate, duplicate_result = await self.kernel.finish_run(
            "run-atomic",
            status=RunStatus.COMPLETED,
        )
        self.assertEqual(finished.status, RunStatus.COMPLETED)
        self.assertEqual(duplicate.completed_at, finished.completed_at)
        self.assertFalse(finish_result.duplicate)
        self.assertTrue(duplicate_result.duplicate)
        events = await self.repository.list_events("run-atomic")
        self.assertEqual(
            [item["event_type"] for item in events],
            ["run.started", "run.completed"],
        )
        self.assertEqual(len(await self.repository.list_outbox(run_id="run-atomic")), 1)

    async def test_outbox_retries_then_dead_letters_with_delivery_fence(self) -> None:
        base = utc_now()
        appended = await self.kernel.record_event(
            run_id="run-durable",
            event_type="artifact.ready",
            outbox_topic="artifact.publish",
            now=base,
        )
        assert appended.outbox is not None
        first = (await self.kernel.claim_outbox(worker_id="dispatcher", now=base))[0]
        self.assertEqual(first.attempts, 1)
        with self.assertRaises(StaleFencingToken):
            await self.kernel.acknowledge_outbox(
                first.message_id,
                worker_id="other",
                lease_token=first.lease_token,
                now=base,
            )
        failed = await self.kernel.fail_outbox(
            first.message_id,
            worker_id="dispatcher",
            lease_token=first.lease_token,
            error="temporary",
            now=base,
        )
        self.assertEqual(failed.status, "pending")

        second = (
            await self.kernel.claim_outbox(
                worker_id="dispatcher",
                now=base + timedelta(seconds=2),
            )
        )[0]
        dead = await self.kernel.fail_outbox(
            second.message_id,
            worker_id="dispatcher",
            lease_token=second.lease_token,
            error="permanent",
            now=base + timedelta(seconds=2),
        )
        self.assertEqual(dead.status, "dead_letter")
        self.assertEqual(dead.attempts, 2)

        replayed, audit = await self.kernel.replay_dead_letter(
            dead.message_id,
            reason="consumer repaired",
            now=base + timedelta(seconds=3),
        )
        self.assertEqual(replayed.status, "pending")
        self.assertEqual(replayed.attempts, 0)
        self.assertEqual(audit.event_type, "outbox.replayed")
        self.assertEqual(audit.payload["message_id"], dead.message_id)
        with self.assertRaisesRegex(DurableOperationsError, "not dead_letter"):
            await self.kernel.replay_dead_letter(
                dead.message_id,
                reason="duplicate replay",
                now=base + timedelta(seconds=4),
            )

    async def test_expired_delivery_claim_is_recovered(self) -> None:
        base = utc_now()
        await self.kernel.record_event(
            run_id="run-durable",
            event_type="notification.ready",
            outbox_topic="notify",
            now=base,
        )
        claimed = await self.kernel.claim_outbox(
            worker_id="crashed-worker",
            lease_seconds=2,
            now=base,
        )
        self.assertEqual(claimed[0].status, "processing")

        recovered = await self.kernel.recover_expired_outbox(
            now=base + timedelta(seconds=3)
        )
        pending = await self.repository.list_outbox(statuses=["pending"])
        self.assertEqual(recovered, 1)
        self.assertEqual([item.message_id for item in pending], [claimed[0].message_id])

    async def test_budget_guard_blocks_event_before_commit(self) -> None:
        with self.assertRaises(BudgetExceeded) as raised:
            await self.kernel.record_event(
                run_id="run-durable",
                event_type="expensive.step",
                metrics=RunMetrics(cost_usd=1.5, total_attempts=1),
                outbox_topic="work.events",
            )
        self.assertIn("cost_usd exceeded", str(raised.exception))
        self.assertEqual(await self.repository.list_events("run-durable"), [])

    async def test_deadlock_detection_and_recovery_emit_one_alert_per_window(self) -> None:
        manifest = await self.repository.get_manifest("run-durable")
        assert manifest is not None
        base = utc_now() - timedelta(seconds=60)
        manifest.started_at = base
        await self.repository.save_manifest(manifest)
        now = base + timedelta(seconds=40)

        report = await self.kernel.recover_run(
            "run-durable",
            owner="recovery-worker",
            now=now,
        )
        duplicate = await self.kernel.recover_run(
            "run-durable",
            owner="recovery-worker",
            now=now,
        )

        self.assertTrue(report.deadlock.deadlocked)
        self.assertFalse(duplicate.deadlock.deadlocked)
        events = await self.repository.list_events("run-durable")
        self.assertEqual([item["event_type"] for item in events], ["run.deadlock_detected"])
        self.assertEqual(len(await self.repository.list_outbox(statuses=["pending"])), 1)

    async def test_two_connections_serialize_compare_and_append(self) -> None:
        second_store = OPCStore(self.db_path)
        await second_store.initialize()
        second_kernel = DurableRunKernel(
            OperationsRepository(second_store),
            self.kernel.config,
        )
        try:
            results = await asyncio.gather(
                self.kernel.record_event(
                    run_id="run-durable",
                    event_type="writer.one",
                    expected_version=0,
                ),
                second_kernel.record_event(
                    run_id="run-durable",
                    event_type="writer.two",
                    expected_version=0,
                ),
                return_exceptions=True,
            )
        finally:
            await second_store.close()

        self.assertEqual(sum(not isinstance(item, Exception) for item in results), 1)
        self.assertEqual(sum(isinstance(item, AggregateVersionConflict) for item in results), 1)
        self.assertEqual(len(await self.repository.list_events("run-durable")), 1)

    async def test_two_processes_serialize_compare_and_append(self) -> None:
        context = multiprocessing.get_context("spawn")
        start_event = context.Event()
        result_queue = context.Queue()
        processes = [
            context.Process(
                target=_append_from_process,
                args=(str(self.db_path), f"process.{index}", start_event, result_queue),
            )
            for index in (1, 2)
        ]
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(timeout=20)
            self.assertFalse(process.is_alive(), "multiprocess writer did not terminate")
            self.assertEqual(process.exitcode, 0)
        results = [result_queue.get(timeout=5) for _ in processes]

        self.assertEqual(sum(status == "ok" for status, _ in results), 1)
        self.assertEqual(
            sum(status == "AggregateVersionConflict" for status, _ in results),
            1,
        )
        self.assertEqual(len(await self.repository.list_events("run-durable")), 1)


if __name__ == "__main__":
    unittest.main()
