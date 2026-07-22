from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from opc.database.store import OPCStore
from opc.operations.learning import LearningAssetManager
from opc.operations.models import (
    AcceptanceCriterion,
    CapabilityAttempt,
    CapabilityKind,
    GateStatus,
    GoalContract,
    LearningAssetStatus,
    ProviderUsageEvent,
    RouteExecutionContract,
    RunManifest,
    RunScorecard,
    RunStatus,
)
from opc.operations.repository import OperationsRepository
from opc.operations.routing_outcomes import RoutingOutcomeService


class RoutingOutcomeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.repository = OperationsRepository(self.store)
        self.learning = LearningAssetManager(self.repository)
        self.service = RoutingOutcomeService(self.repository, self.learning)
        await self.repository.save_goal(
            GoalContract(
                goal_id="routing-goal",
                title="Learn a route",
                objective="Use only passing measured outcomes.",
                acceptance_criteria=[AcceptanceCriterion("quality", "Quality passes")],
            )
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _seed_outcome(self, index: int, *, measured: bool = True) -> None:
        run_id = f"route-run-{index}"
        await self.repository.save_manifest(
            RunManifest(
                run_id=run_id,
                goal_id="routing-goal",
                status=RunStatus.COMPLETED,
            )
        )
        await self.repository.save_scorecard(
            RunScorecard(
                run_id=run_id,
                goal_id="routing-goal",
                gate_status=GateStatus.PASS,
                total_score=0.94,
                quality_score=0.94,
            )
        )
        contract = RouteExecutionContract(
            contract_id=f"contract-{index}",
            request_id=f"request-{index}",
            route_id=f"route-{index}",
            capability_kind=CapabilityKind.LLM,
            run_id=run_id,
            status="completed",
            mode="live",
            planned_provider="ollama-local",
            planned_model="qwen3:14b",
            actual_provider="ollama-local",
            actual_model="qwen3:14b",
            request_snapshot={"task_type": "dialogue"},
        )
        await self.repository.save_route_execution_contract(contract)
        await self.repository.save_provider_usage_event(
            ProviderUsageEvent(
                usage_event_id=f"usage-{index}",
                contract_id=contract.contract_id,
                request_id=contract.request_id,
                route_id=contract.route_id,
                capability_kind=CapabilityKind.LLM,
                run_id=run_id,
                provider="ollama-local",
                model="qwen3:14b",
                measured=measured,
                source="provider_reported" if measured else "unknown",
                input_tokens=10 if measured else None,
                output_tokens=5 if measured else None,
                total_tokens=15 if measured else None,
                cost_usd=0.0,
            )
        )
        await self.repository.save_capability_attempt(
            CapabilityAttempt(
                request_id=contract.request_id,
                route_id=contract.route_id,
                capability_kind=CapabilityKind.LLM,
                run_id=run_id,
                provider="ollama-local",
                status="completed",
                latency_ms=20 + index,
                cost_usd=0.0,
            )
        )

    async def test_passing_measured_outcomes_create_shadow_only_candidate(self) -> None:
        for index in range(3):
            await self._seed_outcome(index)

        asset, summary = await self.service.propose_learning_candidate(min_samples=3)

        assert asset is not None
        self.assertEqual(summary["eligible_route_count"], 1)
        self.assertEqual(asset.status, LearningAssetStatus.CANDIDATE)
        self.assertEqual(asset.content["application_mode"], "shadow_only")
        self.assertFalse(asset.content["automatic_promotion"])
        self.assertEqual(len(asset.source_run_ids), 3)
        self.assertTrue(asset.metadata["requires_release_gate"])

    async def test_unmeasured_outcomes_cannot_become_policy_candidate(self) -> None:
        for index in range(3):
            await self._seed_outcome(index, measured=False)

        asset, summary = await self.service.propose_learning_candidate(min_samples=3)

        self.assertIsNone(asset)
        self.assertEqual(summary["eligible_route_count"], 0)
        self.assertTrue(
            any("measurement_rate" in item for item in summary["routes"][0]["ineligible_reasons"])
        )


if __name__ == "__main__":
    unittest.main()
