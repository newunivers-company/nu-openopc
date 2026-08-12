from __future__ import annotations

import asyncio
import tempfile
from datetime import timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

from opc.core.config import LearningOperationsConfig
from opc.database.store import OPCStore
from opc.operations.learning import LearningAssetManager, LearningLifecycleError
from opc.operations.models import LearningAssetStatus, utc_now
from opc.operations.repository import OperationsRepository


class LearningAssetManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.repository = OperationsRepository(self.store)
        self.manager = LearningAssetManager(
            self.repository,
            LearningOperationsConfig(
                minimum_offline_score=0.75,
                minimum_shadow_score=0.78,
                minimum_canary_score=0.80,
                minimum_sample_size=3,
                maximum_regression=0.02,
            ),
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _promote(self, name: str, *, source_run: str):
        asset = await self.manager.create_candidate(
            name=name,
            kind="skill",
            content={"steps": ["inspect", "verify"]},
            source_run_ids=[source_run],
            confidence=0.9,
        )
        offline = await self.manager.record_evaluation(
            asset.asset_id,
            phase="offline",
            score=0.9,
            baseline_score=0.89,
            sample_size=5,
            evidence=[f"artifact://{source_run}/offline"],
        )
        self.assertTrue(offline.passed)
        await self.manager.enter_shadow(asset.asset_id)
        shadow = await self.manager.record_evaluation(
            asset.asset_id,
            phase="shadow",
            score=0.88,
            baseline_score=0.87,
            sample_size=5,
            evidence=[f"artifact://{source_run}/shadow"],
        )
        self.assertTrue(shadow.passed)
        await self.manager.enter_canary(asset.asset_id)
        canary = await self.manager.record_evaluation(
            asset.asset_id,
            phase="canary",
            score=0.86,
            baseline_score=0.85,
            sample_size=5,
            evidence=[f"artifact://{source_run}/canary"],
        )
        self.assertTrue(canary.passed)
        return await self.manager.promote(asset.asset_id)

    async def test_candidate_to_evaluated_shadow_canary_promoted(self) -> None:
        promoted = await self._promote("release-checklist", source_run="run-1")

        self.assertEqual(promoted.status, LearningAssetStatus.PROMOTED)
        active = await self.manager.list_active(project_id="default")
        self.assertEqual([item.asset_id for item in active], [promoted.asset_id])
        evaluations = await self.repository.list_learning_evaluations(promoted.asset_id)
        self.assertEqual([item.phase for item in evaluations], ["offline", "shadow", "canary"])

    async def test_low_or_unproven_offline_candidate_is_rejected(self) -> None:
        candidate = await self.manager.create_candidate(
            name="unsafe-policy",
            kind="policy",
            content={"allow": "everything"},
            source_run_ids=["run-bad"],
        )
        evaluation = await self.manager.record_evaluation(
            candidate.asset_id,
            phase="offline",
            score=0.95,
            baseline_score=0.90,
            sample_size=1,
            evidence=[],
        )

        self.assertFalse(evaluation.passed)
        loaded = await self.repository.get_learning_asset(candidate.asset_id)
        assert loaded is not None
        self.assertEqual(loaded.status, LearningAssetStatus.REJECTED)
        with self.assertRaises(LearningLifecycleError):
            await self.manager.enter_shadow(candidate.asset_id)

    async def test_missing_source_run_provenance_fails_offline_gate(self) -> None:
        candidate = await self.manager.create_candidate(
            name="unproven-policy",
            kind="policy",
            content={"rule": "looks plausible"},
        )
        evaluation = await self.manager.record_evaluation(
            candidate.asset_id,
            phase="offline",
            score=0.99,
            sample_size=10,
            evidence=["artifact://evaluation"],
        )

        self.assertFalse(evaluation.passed)
        self.assertIn("learning asset has no source run provenance", evaluation.violations)

    async def test_lifecycle_skipping_is_blocked(self) -> None:
        candidate = await self.manager.create_candidate(
            name="no-shortcuts",
            kind="memory",
            content={"fact": "must be validated"},
            source_run_ids=["run-2"],
        )
        with self.assertRaisesRegex(LearningLifecycleError, "shadow requires evaluated"):
            await self.manager.enter_shadow(candidate.asset_id)
        with self.assertRaisesRegex(LearningLifecycleError, "promotion requires canary"):
            await self.manager.promote(candidate.asset_id)

    async def test_material_regression_fails_shadow_gate(self) -> None:
        candidate = await self.manager.create_candidate(
            name="regressing-skill",
            kind="skill",
            content={"steps": ["slow path"]},
            source_run_ids=["run-3"],
        )
        await self.manager.record_evaluation(
            candidate.asset_id,
            phase="offline",
            score=0.9,
            baseline_score=0.9,
            sample_size=3,
            evidence=["artifact://offline"],
        )
        await self.manager.enter_shadow(candidate.asset_id)
        evaluation = await self.manager.record_evaluation(
            candidate.asset_id,
            phase="shadow",
            score=0.8,
            baseline_score=0.9,
            sample_size=5,
            evidence=["artifact://shadow"],
        )

        self.assertFalse(evaluation.passed)
        with self.assertRaisesRegex(LearningLifecycleError, "passing shadow"):
            await self.manager.enter_canary(candidate.asset_id)

    async def test_new_version_retires_previous_and_rollback_restores_it(self) -> None:
        first = await self._promote("deploy-skill", source_run="run-v1")
        second = await self._promote("deploy-skill", source_run="run-v2")

        loaded_first = await self.repository.get_learning_asset(first.asset_id)
        assert loaded_first is not None
        self.assertEqual(loaded_first.status, LearningAssetStatus.RETIRED)
        self.assertEqual(second.version, 2)
        self.assertEqual(second.previous_asset_id, first.asset_id)

        rolled_back, restored = await self.manager.rollback(
            second.asset_id,
            reason="canary incident after wider rollout",
        )
        self.assertEqual(rolled_back.status, LearningAssetStatus.ROLLED_BACK)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.asset_id, first.asset_id)
        self.assertEqual(restored.status, LearningAssetStatus.PROMOTED)

    async def test_employee_feedback_has_run_provenance(self) -> None:
        asset = await self.manager.create_from_employee_feedback(
            organization_id="corporate",
            project_id="default",
            employee_id="employee-1",
            role_id="qa",
            name="qa-review-pattern",
            kind="policy",
            feedback_patch={"strengths": ["finds missing evidence"]},
            source_run_id="run-feedback",
            confidence=0.82,
        )

        self.assertEqual(asset.source_run_ids, ["run-feedback"])
        self.assertEqual(asset.metadata["source"], "employee_evolution")
        self.assertTrue(asset.metadata["provenance_complete"])

    async def test_expired_promoted_asset_is_not_active_and_can_be_retired(self) -> None:
        promoted = await self._promote("temporary-policy", source_run="run-temp")
        promoted.expires_at = utc_now() - timedelta(seconds=1)
        await self.repository.save_learning_asset(promoted)

        self.assertEqual(await self.manager.list_active(project_id="default"), [])
        retired = await self.manager.retire_expired(project_id="default")
        self.assertEqual([item.asset_id for item in retired], [promoted.asset_id])
        self.assertEqual(retired[0].status, LearningAssetStatus.RETIRED)

    async def test_parallel_candidate_creation_allocates_unique_versions(self) -> None:
        second_store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await second_store.initialize()
        second_manager = LearningAssetManager(
            OperationsRepository(second_store),
            self.manager.config,
        )
        try:
            created = await asyncio.gather(
                self.manager.create_candidate(
                    name="parallel-policy",
                    kind="policy",
                    content={"source": "first"},
                ),
                second_manager.create_candidate(
                    name="parallel-policy",
                    kind="policy",
                    content={"source": "second"},
                ),
            )
        finally:
            await second_store.close()

        self.assertEqual(sorted(item.version for item in created), [1, 2])

    async def test_offline_evaluation_and_status_update_are_atomic(self) -> None:
        candidate = await self.manager.create_candidate(
            name="atomic-policy",
            kind="policy",
            content={"rule": "persist together"},
        )
        with patch.object(
            self.repository,
            "save_learning_asset",
            side_effect=RuntimeError("simulated status failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated status failure"):
                await self.manager.record_evaluation(
                    candidate.asset_id,
                    phase="offline",
                    score=0.9,
                    sample_size=3,
                    evidence=["artifact://atomic"],
                )

        loaded = await self.repository.get_learning_asset(candidate.asset_id)
        assert loaded is not None
        self.assertEqual(loaded.status, LearningAssetStatus.CANDIDATE)
        self.assertEqual(
            await self.repository.list_learning_evaluations(candidate.asset_id),
            [],
        )


if __name__ == "__main__":
    unittest.main()
