from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from opc.database.store import OPCStore
from opc.operations.models import (
    AcceptanceCriterion,
    GoalContract,
    LearningAsset,
    LearningAssetStatus,
    RunManifest,
)
from opc.operations.service import OperationsService


class LearningActivationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.service = OperationsService(self.store)
        await self.service.repository.save_goal(
            GoalContract(
                goal_id="goal-learning-runtime",
                project_id="default",
                organization_id="org-a",
                title="Use only released learning",
                objective="Pin promoted learning before work starts.",
                acceptance_criteria=[
                    AcceptanceCriterion("pinned", "Learning is content-addressed")
                ],
            )
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _save_promoted(
        self,
        asset_id: str,
        name: str,
        kind: str,
        content: dict,
        *,
        organization_id: str = "",
        role_id: str = "",
        employee_id: str = "",
        version: int = 1,
    ) -> LearningAsset:
        asset = LearningAsset(
            asset_id=asset_id,
            name=name,
            kind=kind,
            content=content,
            project_id="default",
            organization_id=organization_id,
            role_id=role_id,
            employee_id=employee_id,
            version=version,
            status=LearningAssetStatus.PROMOTED,
            source_run_ids=["source-run"],
            confidence=0.9,
        )
        return await self.service.repository.save_learning_asset(asset)

    async def test_start_pins_global_and_matching_org_assets_with_exact_versions(self) -> None:
        await self._save_promoted("global-policy", "release", "policy", {"rule": "verify"})
        await self._save_promoted(
            "role-memory",
            "domain-notes",
            "memory",
            {"fact": "role-specific"},
            organization_id="org-a",
            role_id="qa",
        )
        await self._save_promoted(
            "employee-skill",
            "release",
            "skill",
            {"steps": ["inspect", "test"]},
            organization_id="org-a",
            role_id="qa",
            employee_id="employee-1",
            version=3,
        )
        await self._save_promoted(
            "other-org",
            "secret",
            "policy",
            {"rule": "must not leak"},
            organization_id="org-b",
        )

        manifest, _ = await self.service.start_run(
            RunManifest(run_id="run-learning", goal_id="goal-learning-runtime")
        )
        snapshot = await self.service.learning_activations.snapshot_for_run(
            manifest.run_id
        )

        self.assertEqual(manifest.organization_id, "org-a")
        self.assertEqual(
            {item.asset_id for item in snapshot.assets},
            {"global-policy", "role-memory", "employee-skill"},
        )
        self.assertEqual(
            manifest.skill_versions["learning:release:employee-skill"],
            next(
                f"3@{item.content_digest[:12]}"
                for item in snapshot.assets
                if item.asset_id == "employee-skill"
            ),
        )
        rendered = await self.service.learning_activations.render_for_run(
            manifest.run_id,
            role_id="qa",
            employee_id="employee-1",
        )
        self.assertEqual(
            rendered["asset_ids"],
            ["role-memory", "global-policy", "employee-skill"],
        )
        self.assertIn("cannot grant permissions", rendered["runtime_policy_messages"][0]["content"])

    async def test_running_snapshot_survives_rollback_and_new_run_gets_new_state(self) -> None:
        old = await self._save_promoted(
            "policy-v1", "deploy", "policy", {"rule": "old"}, organization_id="org-a"
        )
        first, _ = await self.service.start_run(
            RunManifest(run_id="run-before-rollback", goal_id="goal-learning-runtime")
        )
        before = await self.service.learning_activations.render_for_run(first.run_id)

        old.status = LearningAssetStatus.ROLLED_BACK
        await self.service.repository.save_learning_asset(old)
        await self._save_promoted(
            "policy-v2",
            "deploy",
            "policy",
            {"rule": "new"},
            organization_id="org-a",
            version=2,
        )

        still_pinned = await self.service.learning_activations.render_for_run(first.run_id)
        second, _ = await self.service.start_run(
            RunManifest(run_id="run-after-rollback", goal_id="goal-learning-runtime")
        )
        newly_pinned = await self.service.learning_activations.render_for_run(second.run_id)

        self.assertEqual(before, still_pinned)
        self.assertEqual(still_pinned["asset_ids"], ["policy-v1"])
        self.assertEqual(newly_pinned["asset_ids"], ["policy-v2"])

    async def test_snapshot_tampering_is_rejected(self) -> None:
        await self._save_promoted("policy", "safe", "policy", {"rule": "keep"})
        manifest, _ = await self.service.start_run(
            RunManifest(run_id="run-tamper", goal_id="goal-learning-runtime")
        )
        manifest.metadata["learning_activation"]["assets"][0]["content"]["rule"] = "changed"
        await self.service.repository.save_manifest(manifest)

        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            await self.service.learning_activations.snapshot_for_run(manifest.run_id)


if __name__ == "__main__":
    unittest.main()
