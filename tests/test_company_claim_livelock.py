"""Regression tests for the dispatcher claim livelock (2026-07-28 pilot).

Signature: the store CAS refuses a claim (held/reworked card), the
dispatcher refetches the fresh row into a per-call map — but the CALLER's
shared work-item object stays stale, so the next tick's enqueue gate sees
a dispatchable card again and retries forever (observed at ~1 loss/second
for two hours). The fix patches the shared object in place so the very
next tick filters the card out and the dispatcher quiesces.
"""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from opc.core.models import (
    CompanyMemberSession,
    DelegationWorkItem,
    Phase,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer2_organization.company_runtime import CompanyRuntime
from opc.layer2_organization.work_item_links import set_linked_work_item_id


class _FakeStore:
    """CAS always refuses; fresh reads reveal the authoritative hold."""

    is_ready = True

    def __init__(self, fresh_item: DelegationWorkItem) -> None:
        self.fresh_item = fresh_item
        self.claim_calls = 0

    async def claim_delegation_work_item_if_dispatchable(self, *args, **kwargs):
        self.claim_calls += 1
        return None

    async def get_delegation_work_item(self, work_item_id: str):
        return self.fresh_item

    async def hydrate_task_work_item_links(self, tasks):
        return None


def _work_item(*, with_hold: bool) -> DelegationWorkItem:
    metadata = {"session_scope_id": "scope-1"}
    if with_hold:
        metadata["dispatch_hold"] = "company_runtime_suspended"
    return DelegationWorkItem(
        work_item_id="wi-livelock",
        run_id="run-1",
        title="Repair the reservation race",
        role_id="senior_engineer",
        phase=Phase.READY,
        metadata=metadata,
    )


class ClaimLivelockRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # The dispatcher's stale projection: no hold visible.
        self.stale_item = _work_item(with_hold=False)
        # The durable truth: the card is held and must not dispatch.
        self.store = _FakeStore(_work_item(with_hold=True))
        self.runtime = CompanyRuntime(
            org_engine=None, communication=None, store=self.store
        )
        self.runtime._ensure_role_session = lambda task: SimpleNamespace(
            role_session_id="rs-1",
            background_work_item_ids=[],
            focused_work_item_id="",
            status="idle",
            updated_at=None,
        )
        session = CompanyMemberSession(
            member_session_id="ms-1",
            role_id="senior_engineer",
            status="idle",
            metadata={"session_scope_id": "scope-1"},
        )
        self.runtime.member_sessions[session.member_session_id] = session
        self.task = Task(
            id="task-1",
            title="Repair the reservation race",
            description="",
            status=TaskStatus.PENDING,
        )
        set_linked_work_item_id(self.task, "wi-livelock")

    async def _tick(self) -> list:
        self.runtime.enqueue_runnable_work_items(
            [self.stale_item],
            task_by_work_item_id={"wi-livelock": self.task},
        )
        return await self.runtime.claim_runnable_tasks(
            [self.task], [self.stale_item]
        )

    async def test_failed_claim_patches_shared_object_and_quiesces(self) -> None:
        first = await self._tick()
        self.assertEqual(first, [])
        self.assertEqual(self.store.claim_calls, 1)
        # The fix: the SHARED object now carries the authoritative hold.
        self.assertEqual(
            self.stale_item.metadata.get("dispatch_hold"),
            "company_runtime_suspended",
        )

        # Next tick: the enqueue gate filters the held card — no retry loop.
        second = await self._tick()
        self.assertEqual(second, [])
        self.assertEqual(self.store.claim_calls, 1)

    async def test_repeated_losses_track_consecutive_counter(self) -> None:
        await self._tick()
        self.assertEqual(
            self.runtime._claim_loss_counts.get("wi-livelock"), 1
        )
        # Simulate a projection that keeps resurrecting the stale view: the
        # counter must keep climbing so the WARNING escalation can fire.
        self.stale_item.metadata.pop("dispatch_hold", None)
        await self._tick()
        self.assertEqual(
            self.runtime._claim_loss_counts.get("wi-livelock"), 2
        )

    async def test_startup_sweep_repairs_metadata_only_claim_on_ready_item(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tasks.db"
            first = OPCStore(db_path)
            await first.initialize()
            await first.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="ready-with-metadata-claim",
                    run_id="run-1",
                    title="Synthesis",
                    role_id="coo",
                    phase=Phase.READY,
                    metadata={
                        "claimed_by_role_session_id": "dead-role-session",
                        "claimed_task_id": "dead-task",
                    },
                )
            )
            await first.close()

            reopened = OPCStore(db_path)
            await reopened.initialize()
            try:
                repaired = await reopened.get_delegation_work_item(
                    "ready-with-metadata-claim"
                )
                self.assertIsNotNone(repaired)
                self.assertEqual(
                    repaired.metadata.get("claimed_by_role_session_id"),
                    "",
                )
                self.assertEqual(repaired.metadata.get("claimed_task_id"), "")
            finally:
                await reopened.close()


if __name__ == "__main__":
    unittest.main()
