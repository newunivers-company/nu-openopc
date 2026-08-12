from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path
import unittest

from opc.core.config import DurableOperationsConfig, OperationsConfig
from opc.database.store import OPCStore
from opc.operations.models import (
    AcceptanceCriterion,
    GoalContract,
    LearningAsset,
    LearningAssetStatus,
    RunManifest,
    RunStatus,
    utc_now,
)
from opc.operations.service import OperationsService


class OperatorActionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.service = OperationsService(
            self.store,
            OperationsConfig(
                durable=DurableOperationsConfig(
                    deadlock_after_seconds=10,
                    outbox_max_attempts=1,
                )
            ),
        )
        await self.service.repository.save_goal(
            GoalContract(
                goal_id="goal-actions",
                title="Recover safely",
                objective="Make operator intervention auditable.",
                acceptance_criteria=[
                    AcceptanceCriterion("audit", "Every action has a receipt")
                ],
            )
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _save_run(self, run_id: str, *, started_at=None) -> None:
        await self.service.repository.save_manifest(
            RunManifest(
                run_id=run_id,
                goal_id="goal-actions",
                status=RunStatus.RUNNING,
                started_at=started_at or utc_now(),
            )
        )

    async def test_recovery_requires_exact_digest_and_is_single_use(self) -> None:
        now = utc_now()
        await self._save_run(
            "run-stalled",
            started_at=now - timedelta(seconds=60),
        )
        plan = await self.service.operator_actions.plan(
            project_id="default",
            kind="recover_run",
            target_id="run-stalled",
            reason="operator reviewed the last durable event",
            now=now,
        )

        with self.assertRaises(PermissionError):
            await self.service.operator_actions.execute(
                project_id="default",
                action_id=plan["action_id"],
                plan_digest="0" * 64,
                operator_id="owner",
                confirmed=True,
                now=now,
            )
        executed = await self.service.operator_actions.execute(
            project_id="default",
            action_id=plan["action_id"],
            plan_digest=plan["plan_digest"],
            operator_id="owner",
            confirmed=True,
            now=now,
        )
        duplicate = await self.service.operator_actions.execute(
            project_id="default",
            action_id=plan["action_id"],
            plan_digest=plan["plan_digest"],
            operator_id="owner",
            confirmed=True,
            now=now,
        )

        self.assertEqual(executed["status"], "executed")
        self.assertEqual(executed, duplicate)
        self.assertTrue(executed["result"]["deadlock"]["deadlocked"])
        self.assertEqual(
            await self.service.repository.count_rows("operator_actions"),
            1,
        )

    async def test_dead_letter_replay_has_durable_action_and_event_receipts(self) -> None:
        await self._save_run("run-delivery")
        appended = await self.service.durable.record_event(
            run_id="run-delivery",
            event_type="delivery.ready",
            outbox_topic="delivery",
        )
        assert appended.outbox is not None
        claimed = (await self.service.durable.claim_outbox(worker_id="worker"))[0]
        dead = await self.service.durable.fail_outbox(
            claimed.message_id,
            worker_id="worker",
            lease_token=claimed.lease_token,
            error="consumer unavailable",
        )
        self.assertEqual(dead.status, "dead_letter")
        plan = await self.service.operator_actions.plan(
            project_id="default",
            kind="replay_dead_letter",
            target_id=dead.message_id,
            reason="consumer health check passed",
        )
        receipt = await self.service.operator_actions.execute(
            project_id="default",
            action_id=plan["action_id"],
            plan_digest=plan["plan_digest"],
            operator_id="on-call",
            confirmed=True,
        )

        self.assertEqual(receipt["result"]["message"]["status"], "pending")
        self.assertEqual(
            receipt["result"]["audit_event"]["event_type"],
            "outbox.replayed",
        )

    async def test_learning_rollback_changes_future_state_only(self) -> None:
        previous = LearningAsset(
            asset_id="policy-v1",
            name="release-policy",
            kind="policy",
            content={"rule": "stable"},
            status=LearningAssetStatus.RETIRED,
            source_run_ids=["run-old"],
        )
        current = LearningAsset(
            asset_id="policy-v2",
            name="release-policy",
            kind="policy",
            content={"rule": "regressed"},
            version=2,
            status=LearningAssetStatus.PROMOTED,
            previous_asset_id=previous.asset_id,
            source_run_ids=["run-new"],
        )
        await self.service.repository.save_learning_asset(previous)
        await self.service.repository.save_learning_asset(current)
        plan = await self.service.operator_actions.plan(
            project_id="default",
            kind="rollback_learning_asset",
            target_id=current.asset_id,
            reason="verified quality regression",
        )
        receipt = await self.service.operator_actions.execute(
            project_id="default",
            action_id=plan["action_id"],
            plan_digest=plan["plan_digest"],
            operator_id="quality-owner",
            confirmed=True,
        )

        self.assertEqual(receipt["result"]["rolled_back"]["status"], "rolled_back")
        self.assertEqual(receipt["result"]["restored"]["status"], "promoted")

    async def test_expired_plan_and_nonexpired_asset_fail_closed(self) -> None:
        asset = LearningAsset(
            asset_id="current-policy",
            name="current-policy",
            kind="policy",
            content={"rule": "current"},
            status=LearningAssetStatus.PROMOTED,
            source_run_ids=["run-current"],
            expires_at=utc_now() + timedelta(days=1),
        )
        await self.service.repository.save_learning_asset(asset)
        with self.assertRaisesRegex(ValueError, "before expiry"):
            await self.service.operator_actions.plan(
                project_id="default",
                kind="retire_learning_asset",
                target_id=asset.asset_id,
                reason="too early",
            )

        now = utc_now()
        await self._save_run("run-expiring")
        plan = await self.service.operator_actions.plan(
            project_id="default",
            kind="recover_run",
            target_id="run-expiring",
            reason="short-lived plan",
            expires_in_seconds=30,
            now=now,
        )
        with self.assertRaisesRegex(PermissionError, "expired"):
            await self.service.operator_actions.execute(
                project_id="default",
                action_id=plan["action_id"],
                plan_digest=plan["plan_digest"],
                operator_id="owner",
                confirmed=True,
                now=now + timedelta(seconds=31),
            )
        stored = await self.service.repository.get_operator_action(plan["action_id"])
        assert stored is not None
        self.assertEqual(stored.status, "expired")


if __name__ == "__main__":
    unittest.main()
