from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path
import unittest

from opc.core.config import OutcomeEvaluationConfig
from opc.database.store import OPCStore
from opc.operations.evaluation import OutcomeEvaluator
from opc.operations.models import (
    AcceptanceCriterion,
    GateStatus,
    GoalContract,
    GoalContractStatus,
    ResourceBudget,
    RunManifest,
    RunMetrics,
    RunStatus,
    utc_now,
)
from opc.operations.repository import OperationsRepository


class OperationsEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.repository = OperationsRepository(self.store)
        self.evaluator = OutcomeEvaluator(
            self.repository,
            OutcomeEvaluationConfig(minimum_total_score=0.75, maximum_regression=0.05),
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _create_run(self, *, run_id: str, status: RunStatus = RunStatus.COMPLETED) -> None:
        goal = await self.repository.get_goal("goal-1")
        if goal is None:
            goal = GoalContract(
                goal_id="goal-1",
                project_id="default",
                title="Ship reliable operating loop",
                objective="Deliver a tested, evidence-backed operating loop.",
                acceptance_criteria=[
                    AcceptanceCriterion(
                        criterion_id="tests",
                        description="All required tests pass",
                        weight=2,
                        minimum_score=0.9,
                    ),
                    AcceptanceCriterion(
                        criterion_id="docs",
                        description="Operator documentation is complete",
                        minimum_score=0.8,
                    ),
                ],
                budget=ResourceBudget(
                    max_cost_usd=1.0,
                    max_duration_seconds=120,
                    max_interventions=1,
                    max_failed_attempts=0,
                ),
                evidence_requirements=["test_report"],
            )
            await self.repository.save_goal(goal)
        now = utc_now()
        await self.repository.save_manifest(
            RunManifest(
                run_id=run_id,
                goal_id=goal.goal_id,
                project_id=goal.project_id,
                status=status,
                started_at=now - timedelta(seconds=30),
                completed_at=now if status in {RunStatus.COMPLETED, RunStatus.FAILED} else None,
                source_revision="abc123",
            )
        )

    async def test_contract_manifest_and_scorecard_round_trip(self) -> None:
        await self._create_run(run_id="run-pass")
        scorecard = await self.evaluator.evaluate_run(
            "run-pass",
            criterion_scores={"tests": 1.0, "docs": 0.9},
            evidence={
                "tests": ["artifact://pytest.xml"],
                "docs": ["artifact://operations.md"],
                "test_report": ["artifact://pytest.xml"],
            },
            metrics=RunMetrics(
                cost_usd=0.2,
                duration_seconds=30,
                total_attempts=1,
            ),
            baseline_label="main",
        )

        self.assertEqual(scorecard.gate_status, GateStatus.PASS)
        self.assertGreaterEqual(scorecard.total_score, 0.9)
        loaded = await self.repository.get_scorecard("run-pass")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.to_dict(), scorecard.to_dict())
        goals = await self.repository.list_goals(project_id="default")
        self.assertEqual([item.goal_id for item in goals], ["goal-1"])

    async def test_required_evidence_and_budget_fail_closed(self) -> None:
        await self._create_run(run_id="run-fail")
        scorecard = await self.evaluator.evaluate_run(
            "run-fail",
            criterion_scores={"tests": 1.0, "docs": 0.9},
            evidence={"docs": ["artifact://operations.md"]},
            metrics=RunMetrics(
                cost_usd=2.0,
                duration_seconds=30,
                failed_attempts=1,
                total_attempts=2,
            ),
        )

        self.assertEqual(scorecard.gate_status, GateStatus.FAIL)
        self.assertTrue(any("has no evidence" in item for item in scorecard.violations))
        self.assertTrue(any("cost_usd exceeded" in item for item in scorecard.violations))
        self.assertTrue(any("failed_attempts exceeded" in item for item in scorecard.violations))

    async def test_declared_final_run_auto_completes_latest_goal_version(self) -> None:
        await self._create_run(run_id="run-final")
        manifest = await self.repository.get_manifest("run-final")
        assert manifest is not None
        manifest.metadata["complete_goal_on_pass"] = True
        await self.repository.save_manifest(manifest)

        scorecard = await self.evaluator.evaluate_run(
            "run-final",
            criterion_scores={"tests": 1.0, "docs": 1.0},
            evidence={
                "tests": ["artifact://pytest.xml"],
                "docs": ["artifact://operations.md"],
                "test_report": ["artifact://pytest.xml"],
            },
            metrics=RunMetrics(total_attempts=1),
        )
        latest = await self.repository.get_goal("goal-1")
        pinned = await self.repository.get_goal_version("goal-1", 1)

        assert latest is not None and pinned is not None
        self.assertEqual(scorecard.gate_status, GateStatus.PASS)
        self.assertTrue(scorecard.metadata["goal_auto_completed"])
        self.assertEqual(latest.status, GoalContractStatus.COMPLETED)
        self.assertEqual(latest.version, 2)
        self.assertEqual(pinned.status, GoalContractStatus.ACTIVE)

    async def test_non_terminal_run_is_review_not_acceptance(self) -> None:
        await self._create_run(run_id="run-live", status=RunStatus.RUNNING)
        scorecard = await self.evaluator.evaluate_run(
            "run-live",
            criterion_scores={"tests": 1.0, "docs": 1.0},
            evidence={
                "tests": ["artifact://pytest.xml"],
                "docs": ["artifact://operations.md"],
                "test_report": ["artifact://pytest.xml"],
            },
            metrics=RunMetrics(total_attempts=1),
        )

        self.assertEqual(scorecard.gate_status, GateStatus.REVIEW)
        self.assertFalse(scorecard.accepted)

    async def test_regression_gate_rejects_material_drop(self) -> None:
        await self._create_run(run_id="baseline")
        baseline = await self.evaluator.evaluate_run(
            "baseline",
            criterion_scores={"tests": 1.0, "docs": 1.0},
            evidence={
                "tests": ["artifact://baseline-tests"],
                "docs": ["artifact://baseline-docs"],
                "test_report": ["artifact://baseline-tests"],
            },
            metrics=RunMetrics(total_attempts=1),
            baseline_label="main",
        )
        await self._create_run(run_id="candidate")
        current = await self.evaluator.evaluate_run(
            "candidate",
            criterion_scores={"tests": 0.93, "docs": 0.81},
            evidence={
                "tests": ["artifact://candidate-tests"],
                "docs": ["artifact://candidate-docs"],
                "test_report": ["artifact://candidate-tests"],
            },
            metrics=RunMetrics(total_attempts=1),
        )

        result = self.evaluator.compare(current, baseline)
        self.assertFalse(result.passed)
        self.assertGreater(result.regressions["quality_score"], 0.05)

    async def test_project_scoped_store_rejects_cross_project_contract(self) -> None:
        scoped_store = OPCStore(Path(self._tmp.name) / "projects" / "alpha" / "tasks.db")
        await scoped_store.initialize()
        try:
            repository = OperationsRepository(scoped_store)
            with self.assertRaisesRegex(RuntimeError, "cross-project"):
                await repository.save_goal(
                    GoalContract(
                        project_id="beta",
                        title="Wrong project",
                        objective="Must not leak across project DBs.",
                        acceptance_criteria=[
                            AcceptanceCriterion("isolation", "No cross-project write")
                        ],
                    )
                )
        finally:
            await scoped_store.close()

    async def test_run_pins_immutable_goal_contract_version(self) -> None:
        version_one = GoalContract(
            goal_id="versioned-goal",
            title="Versioned acceptance",
            objective="Evaluate each run against the contract it started with.",
            acceptance_criteria=[
                AcceptanceCriterion(
                    "quality",
                    "Quality meets the pinned threshold",
                    minimum_score=0.8,
                )
            ],
        )
        await self.repository.save_goal(version_one)
        now = utc_now()
        manifest = RunManifest(
            run_id="versioned-run",
            goal_id=version_one.goal_id,
            status=RunStatus.COMPLETED,
            started_at=now - timedelta(seconds=1),
            completed_at=now,
        )
        await self.repository.save_manifest(manifest)
        self.assertEqual(manifest.goal_version, 1)

        same_version = GoalContract.from_dict(version_one.to_dict())
        same_version.objective = "An in-place rewrite must be rejected."
        with self.assertRaisesRegex(ValueError, "must use version 2"):
            await self.repository.save_goal(same_version)

        version_two = GoalContract.from_dict(version_one.to_dict())
        version_two.version = 2
        version_two.acceptance_criteria[0].minimum_score = 0.95
        await self.repository.save_goal(version_two)

        scorecard = await self.evaluator.evaluate_run(
            manifest.run_id,
            criterion_scores={"quality": 0.85},
            evidence={"quality": ["artifact://pinned-contract"]},
            metrics=RunMetrics(total_attempts=1),
        )
        self.assertEqual(scorecard.gate_status, GateStatus.PASS)
        archived = await self.repository.get_goal_version(version_one.goal_id, 1)
        latest = await self.repository.get_goal(version_one.goal_id)
        assert archived is not None and latest is not None
        self.assertEqual(archived.acceptance_criteria[0].minimum_score, 0.8)
        self.assertEqual(latest.acceptance_criteria[0].minimum_score, 0.95)


if __name__ == "__main__":
    unittest.main()
