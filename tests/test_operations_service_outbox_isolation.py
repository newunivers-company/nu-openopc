from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from opc.core.config import OperationsConfig
from opc.core.models import OPCEvent
from opc.database.store import OPCStore
from opc.operations.models import (
    AcceptanceCriterion,
    GoalContract,
    RunManifest,
    RunStatus,
)
from opc.operations.service import OperationsService


class _EventBus:
    def __init__(self) -> None:
        self.delivered = asyncio.Event()
        self.events: list[OPCEvent] = []

    async def publish_checked(self, event: OPCEvent) -> None:
        self.events.append(event)
        self.delivered.set()

    async def publish(self, event: OPCEvent) -> None:
        await self.publish_checked(event)


@pytest.mark.asyncio
async def test_background_outbox_uses_connection_isolated_from_task_writes() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = OPCStore(Path(tmpdir) / "tasks.db")
        await store.initialize()
        service = OperationsService(store, OperationsConfig())
        try:
            await service.repository.save_goal(
                GoalContract(
                    goal_id="goal-1",
                    title="Outbox isolation",
                    objective="Deliver while the main connection is busy.",
                    acceptance_criteria=[
                        AcceptanceCriterion("delivery", "Event is delivered")
                    ],
                )
            )
            await service.repository.save_manifest(
                RunManifest(
                    run_id="run-1",
                    goal_id="goal-1",
                    status=RunStatus.RUNNING,
                )
            )
            await service.durable.record_event(
                run_id="run-1",
                event_type="probe.ready",
                outbox_topic="probe.ready",
            )

            # Leave an implicit write transaction open on the application
            # connection. The former shared-connection dispatcher raised
            # "cannot start a transaction within a transaction" here.
            await store._require_db().execute(
                """INSERT INTO events(event_id, event_type, payload, timestamp)
                   VALUES ('main-write', 'probe', '{}', '2026-01-01T00:00:00')"""
            )
            bus = _EventBus()
            await service.start_outbox_dispatcher(bus)
            await asyncio.sleep(0.05)
            assert service.outbox_dispatcher is not None
            assert service.outbox_dispatcher.running
            await store._require_db().rollback()
            await asyncio.wait_for(bus.delivered.wait(), timeout=2)

            assert service._outbox_store is not None
            assert (
                service._outbox_store._require_db()
                is not store._require_db()
            )
            assert [event.event_type for event in bus.events] == ["probe.ready"]
        finally:
            await store._require_db().rollback()
            await service.stop_outbox_dispatcher()
            await store.close()
