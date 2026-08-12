from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest

from opc.core.models import RecruitmentNeed
from opc.database.store import OPCStore
from opc.layer2_organization.recruiter import CompanyRecruiter
from opc.operations.models import (
    AcceptanceCriterion,
    GateStatus,
    GoalContract,
    RoleOutcome,
    RunManifest,
    RunScorecard,
    RunStatus,
    StaffingCandidate,
)
from opc.operations.repository import OperationsRepository
from opc.operations.staffing import StaffingOptimizer


class StaffingOptimizerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.repository = OperationsRepository(self.store)
        self.optimizer = StaffingOptimizer(self.repository)
        await self.repository.save_goal(
            GoalContract(
                goal_id="goal-staffing",
                title="Choose strong staff",
                objective="Use outcome evidence for staffing.",
                acceptance_criteria=[AcceptanceCriterion("quality", "Quality is acceptable")],
            )
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _save_outcome(
        self,
        *,
        run_id: str,
        employee_id: str,
        quality: float,
        reliability: float,
        python_score: float,
    ) -> None:
        await self.repository.save_manifest(
            RunManifest(
                run_id=run_id,
                goal_id="goal-staffing",
                status=RunStatus.COMPLETED,
            )
        )
        await self.repository.save_scorecard(
            RunScorecard(
                run_id=run_id,
                goal_id="goal-staffing",
                gate_status=GateStatus.PASS,
                total_score=quality,
                quality_score=quality,
                evidence_score=1.0,
                budget_score=1.0,
                reliability_score=reliability,
                autonomy_score=1.0,
                role_outcomes=[
                    RoleOutcome(
                        role_id="engineer",
                        employee_id=employee_id,
                        quality_score=quality,
                        reliability_score=reliability,
                        domain_scores={"python": python_score},
                        accepted_work_items=2,
                    )
                ],
            )
        )

    async def test_historical_role_outcomes_drive_selection(self) -> None:
        await self._save_outcome(
            run_id="history-1",
            employee_id="alice",
            quality=0.96,
            reliability=0.94,
            python_score=0.95,
        )
        await self._save_outcome(
            run_id="history-2",
            employee_id="alice",
            quality=0.92,
            reliability=0.96,
            python_score=0.93,
        )
        decision = await self.optimizer.recommend(
            role_id="engineer",
            required_domains=["python"],
            candidates=[
                StaffingCandidate(
                    employee_id="alice",
                    role_ids=["engineer"],
                    domains=["python"],
                    experience_score=8,
                ),
                StaffingCandidate(
                    employee_id="bob",
                    role_ids=["engineer"],
                    domains=["python"],
                    experience_score=2,
                    quality_score=0.8,
                    reliability_score=0.8,
                ),
            ],
        )

        self.assertEqual(decision.selected_employee_id, "alice")
        self.assertEqual(decision.metadata["historical_evidence_count"], 2)
        self.assertGreater(decision.predicted_score, decision.alternatives[0]["predicted_score"])

    async def test_tie_break_is_deterministic_and_cold_start_is_visible(self) -> None:
        decision = await self.optimizer.recommend(
            role_id="designer",
            candidates=[
                StaffingCandidate(employee_id="zoe", role_ids=["designer"]),
                StaffingCandidate(employee_id="amy", role_ids=["designer"]),
            ],
        )

        self.assertEqual(decision.selected_employee_id, "amy")
        self.assertTrue(any("cold-start" in line for line in decision.rationale))

    async def test_cost_ceiling_penalizes_expensive_candidate(self) -> None:
        decision = await self.optimizer.recommend(
            role_id="analyst",
            max_cost_usd=1.0,
            candidates=[
                StaffingCandidate(
                    employee_id="expensive",
                    role_ids=["analyst"],
                    quality_score=0.9,
                    reliability_score=0.9,
                    expected_cost_usd=2.0,
                ),
                StaffingCandidate(
                    employee_id="free",
                    role_ids=["analyst"],
                    quality_score=0.9,
                    reliability_score=0.9,
                    expected_cost_usd=0.0,
                ),
            ],
        )

        self.assertEqual(decision.selected_employee_id, "free")
        self.assertEqual(decision.component_scores["cost"], 1.0)
        self.assertEqual(decision.metadata["excluded_over_budget"], ["expensive"])

    async def test_cost_ceiling_fails_when_no_candidate_is_eligible(self) -> None:
        with self.assertRaisesRegex(ValueError, "fits the cost ceiling"):
            await self.optimizer.recommend(
                role_id="analyst",
                max_cost_usd=0.5,
                candidates=[
                    StaffingCandidate(
                        employee_id="expensive",
                        role_ids=["analyst"],
                        expected_cost_usd=1.0,
                    )
                ],
            )

    async def test_observation_records_counterfactual_regret(self) -> None:
        decision = await self.optimizer.recommend(
            role_id="writer",
            candidates=[
                StaffingCandidate(employee_id="a", role_ids=["writer"], quality_score=0.9),
                StaffingCandidate(employee_id="b", role_ids=["writer"], quality_score=0.8),
            ],
        )
        observed = await self.optimizer.observe(
            decision.decision_id,
            observed_score=0.60,
            alternative_observed_scores={"b": 0.85},
        )

        self.assertAlmostEqual(observed.regret, 0.25)
        self.assertEqual(observed.metadata["regret_source"], "observed_counterfactual")
        loaded = await self.repository.get_staffing_decision(decision.decision_id)
        assert loaded is not None
        self.assertAlmostEqual(loaded.regret, 0.25)

    async def test_observe_from_scorecard_uses_selected_role_outcome(self) -> None:
        decision = await self.optimizer.recommend(
            role_id="engineer",
            candidates=[StaffingCandidate(employee_id="alice", role_ids=["engineer"])],
        )
        await self._save_outcome(
            run_id="observed-run",
            employee_id="alice",
            quality=0.9,
            reliability=0.8,
            python_score=0.85,
        )

        observed = await self.optimizer.observe_from_scorecard(
            decision.decision_id,
            run_id="observed-run",
        )
        self.assertAlmostEqual(observed.observed_score, 0.87)
        self.assertEqual(observed.regret, 0.0)

    async def test_company_recruiter_heuristic_consumes_optimizer_decision(self) -> None:
        employees = [
            SimpleNamespace(
                employee_id="alice",
                name="Alice",
                template_id="t1",
                role_id="engineer",
                category="coding",
                domains=["python"],
                metadata={},
                description="",
            ),
            SimpleNamespace(
                employee_id="bob",
                name="Bob",
                template_id="t2",
                role_id="engineer",
                category="coding",
                domains=["python"],
                metadata={},
                description="",
            ),
        ]
        fake_org = SimpleNamespace(
            employee_evolution=None,
            employee_role_ids=lambda employee: [employee.role_id],
        )
        recruiter = CompanyRecruiter(None, fake_org, None, staffing_optimizer=self.optimizer)
        decision = await self.optimizer.recommend(
            role_id="engineer",
            candidates=[
                StaffingCandidate(employee_id="alice", role_ids=["engineer"], quality_score=0.9),
                StaffingCandidate(employee_id="bob", role_ids=["engineer"], quality_score=0.7),
            ],
        )
        proposal = recruiter._heuristic_proposal_for_prepared_need(
            {
                "need": RecruitmentNeed(role_id="engineer", role_name="Engineer"),
                "existing_employees": employees,
                "employee_pool": employees,
                "candidates": [],
                "triage_action": "category_screening",
                "selected_categories": ["python"],
                "category_rationale": "specialized work",
                "staffing_optimizer_decision": decision,
            },
            project_id="default",
        )

        self.assertEqual(proposal.existing_employee.employee_id, "alice")
        self.assertEqual(proposal.metadata["selection_source"], "staffing_optimizer")
        self.assertEqual(
            proposal.metadata["staffing_optimizer"]["decision_id"],
            decision.decision_id,
        )


if __name__ == "__main__":
    unittest.main()
