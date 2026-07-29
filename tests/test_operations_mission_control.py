from __future__ import annotations

import json
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest

from opc.core.config import (
    DurableOperationsConfig,
    OperationsConfig,
    ProviderOperationsConfig,
)
from opc.database.store import OPCStore
from opc.layer2_organization.secretary import SecretaryService
from opc.layer4_tools.operations import create_operations_tools
from opc.operations.durable import DurableRunKernel
from opc.operations.mission_control import MissionControlService
from opc.operations.models import (
    AcceptanceCriterion,
    GateStatus,
    GoalContract,
    LearningAsset,
    LearningAssetStatus,
    ResourceBudget,
    RunManifest,
    RunMetrics,
    RunScorecard,
    RunStatus,
    CapabilityKind,
    ProviderCanaryResult,
    utc_now,
)
from opc.operations.repository import OperationsRepository
from opc.operations.service import OperationsService


class MissionControlServiceTests(unittest.IsolatedAsyncioTestCase):
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
                deadlock_after_seconds=10,
            ),
        )
        self.mission = MissionControlService(self.repository, self.kernel)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _seed_risky_portfolio(self):
        now = utc_now()
        await self.repository.save_goal(
            GoalContract(
                goal_id="goal-risk",
                title="Recover production run",
                objective="Restore durable progress.",
                deadline=now - timedelta(hours=1),
                acceptance_criteria=[AcceptanceCriterion("recovery", "Run recovers")],
                budget=ResourceBudget(max_cost_usd=1.0),
            )
        )
        await self.repository.save_manifest(
            RunManifest(
                run_id="run-deadlocked",
                goal_id="goal-risk",
                status=RunStatus.RUNNING,
                started_at=now - timedelta(seconds=60),
            )
        )
        await self.repository.save_manifest(
            RunManifest(
                run_id="run-failed-gate",
                goal_id="goal-risk",
                status=RunStatus.COMPLETED,
                started_at=now - timedelta(seconds=20),
                completed_at=now - timedelta(seconds=5),
            )
        )
        await self.repository.save_scorecard(
            RunScorecard(
                run_id="run-failed-gate",
                goal_id="goal-risk",
                gate_status=GateStatus.FAIL,
                total_score=0.5,
                quality_score=0.4,
                evidence_score=0.5,
                budget_score=0.8,
                reliability_score=0.7,
                autonomy_score=0.6,
                metrics=RunMetrics(cost_usd=0.7),
                violations=["required criterion failed"],
            )
        )
        await self.repository.save_manifest(
            RunManifest(
                run_id="run-unscored",
                goal_id="goal-risk",
                status=RunStatus.FAILED,
                started_at=now - timedelta(seconds=30),
                completed_at=now - timedelta(seconds=2),
            )
        )
        appended = await self.kernel.record_event(
            run_id="run-deadlocked",
            event_type="notification.ready",
            outbox_topic="notify",
            now=now - timedelta(seconds=30),
        )
        assert appended.outbox is not None
        claimed = (
            await self.kernel.claim_outbox(
                worker_id="dispatcher",
                now=now - timedelta(seconds=29),
            )
        )[0]
        await self.kernel.fail_outbox(
            claimed.message_id,
            worker_id="dispatcher",
            lease_token=claimed.lease_token,
            error="consumer unavailable",
            now=now - timedelta(seconds=29),
        )
        await self.repository.save_learning_asset(
            LearningAsset(
                asset_id="asset-candidate",
                name="new-policy",
                kind="policy",
                content={"rule": "verify first"},
                status=LearningAssetStatus.CANDIDATE,
                source_run_ids=["run-failed-gate"],
            )
        )
        await self.repository.db.execute(
            """INSERT INTO execution_checkpoints
               (checkpoint_id, project_id, session_id, checkpoint_type, status,
                task_id, payload, created_at, updated_at)
               VALUES (?, 'default', '', 'tool_approval', 'pending', '', '{}', ?, ?)""",
            ("approval-1", now.isoformat(), now.isoformat()),
        )
        await self.repository.db.commit()
        return now

    async def test_snapshot_prioritizes_deadlock_delivery_and_approval_risks(self) -> None:
        now = await self._seed_risky_portfolio()
        snapshot = await self.mission.snapshot(project_id="default", now=now)

        self.assertEqual(snapshot.active_goals, 1)
        self.assertEqual(snapshot.active_runs, 1)
        self.assertEqual(snapshot.failed_gates, 1)
        self.assertEqual(snapshot.dead_letters, 1)
        self.assertEqual(snapshot.pending_approvals, 1)
        self.assertEqual(snapshot.learning_candidates, 1)
        self.assertAlmostEqual(snapshot.total_cost_usd, 0.7)
        self.assertEqual(
            snapshot.evidence_funnel["all_runs"],
            {
                "started": 3,
                "completed": 2,
                "scored": 1,
                "accepted": 0,
                "awaiting_judgment": 0,
            },
        )
        self.assertEqual(
            snapshot.evidence_funnel["benchmark"],
            {
                "started": 0,
                "completed": 0,
                "scored": 0,
                "accepted": 0,
                "awaiting_judgment": 0,
            },
        )
        kinds = [item.kind for item in snapshot.alerts]
        self.assertIn("deadlock", kinds)
        self.assertIn("dead_letter", kinds)
        self.assertIn("pending_approval", kinds)
        self.assertIn("failed_gate", kinds)
        self.assertIn("missing_scorecard", kinds)
        self.assertIn("overdue_goal", kinds)
        self.assertEqual(snapshot.alerts[0].severity, "critical")

    async def test_snapshot_queues_completed_runs_without_scorecards_for_judgment(self) -> None:
        now = await self._seed_risky_portfolio()
        await self.repository.save_manifest(
            RunManifest(
                run_id="run-awaiting-judgment",
                goal_id="goal-risk",
                status=RunStatus.COMPLETED,
                started_at=now - timedelta(seconds=40),
                completed_at=now - timedelta(seconds=1),
                metadata={"benchmark_slot_id": "slot-7"},
            )
        )

        snapshot = await self.mission.snapshot(project_id="default", now=now)

        queued_ids = [entry["run_id"] for entry in snapshot.judgment_queue]
        self.assertIn("run-awaiting-judgment", queued_ids)
        self.assertNotIn("run-failed-gate", queued_ids)  # already has a scorecard
        self.assertNotIn("run-unscored", queued_ids)  # failed, not completed
        self.assertEqual(
            snapshot.evidence_funnel["benchmark"],
            {
                "started": 1,
                "completed": 1,
                "scored": 0,
                "accepted": 0,
                "awaiting_judgment": 1,
            },
        )
        entry = next(
            item
            for item in snapshot.judgment_queue
            if item["run_id"] == "run-awaiting-judgment"
        )
        self.assertEqual(entry["goal_id"], "goal-risk")
        self.assertEqual(entry["benchmark_slot_id"], "slot-7")
        self.assertEqual(entry["completed_at"], (now - timedelta(seconds=1)).isoformat())
        self.assertLessEqual(len(snapshot.judgment_queue), 20)
        self.assertEqual(snapshot.to_dict()["judgment_queue"], snapshot.judgment_queue)

    async def test_daily_brief_is_deterministic_and_actionable(self) -> None:
        now = await self._seed_risky_portfolio()
        brief = await self.mission.daily_brief(project_id="default", now=now)

        self.assertIn("Mission Control — default", brief)
        self.assertIn("1 dead-letter", brief)
        self.assertIn("Approvals 1 pending", brief)
        self.assertIn("[CRITICAL]", brief)
        self.assertIn("Recommended next actions", brief)

    async def test_snapshot_surfaces_exhausted_subscription_call_quota(self) -> None:
        await self.repository.reserve_provider_call(
            contract_id="quota-contract",
            request_id="quota-request",
            project_id="default",
            provider="codex",
            model="subscription-model",
            limit=1,
            window_seconds=3600,
        )
        mission = MissionControlService(
            self.repository,
            self.kernel,
            provider_config=ProviderOperationsConfig(
                subscription_call_limit=1,
                subscription_window_seconds=3600,
                subscription_providers=["codex"],
            ),
        )

        snapshot = await mission.snapshot(project_id="default")

        self.assertEqual(snapshot.provider_call_quotas["codex"]["remaining"], 0)
        alert = next(
            item
            for item in snapshot.alerts
            if item.kind == "subscription_call_quota"
        )
        self.assertEqual(alert.severity, "critical")

    async def test_snapshot_surfaces_incomplete_provider_readiness_evidence(self) -> None:
        now = utc_now()
        await self.repository.save_provider_canary_result(
            ProviderCanaryResult(
                project_id="default",
                capability_kind=CapabilityKind.LLM,
                provider="codex",
                model="subscription-model",
                success=True,
                available=True,
                credential_ready=True,
                transport_ready=True,
                latency_ms=25.0,
                checked_at=now,
            )
        )
        mission = MissionControlService(
            self.repository,
            self.kernel,
            provider_config=ProviderOperationsConfig(
                slo_min_samples=1,
                slo_trend_window_samples=1,
                readiness_min_observation_seconds=3600,
                readiness_min_time_buckets=2,
                readiness_required_failure_scenarios=["transport_timeout"],
            ),
        )

        snapshot = await mission.snapshot(project_id="default", now=now)

        readiness = snapshot.provider_slo["codex"]
        self.assertTrue(readiness["target_met"])
        self.assertFalse(readiness["production_ready"])
        self.assertIn("transport_timeout", readiness["missing_failure_scenarios"])
        alert = next(
            item for item in snapshot.alerts if item.kind == "provider_readiness"
        )
        self.assertEqual(alert.severity, "medium")

    async def test_operations_tools_expose_mission_and_force_dry_run_routes(self) -> None:
        service = OperationsService(
            self.store,
            OperationsConfig(),
            default_llm_model="openai/test",
            default_llm_api_base="http://127.0.0.1:8000/v1",
        )
        tools = {item.name: item for item in create_operations_tools(service)}

        self.assertEqual(
            set(tools),
            {
                "operations_mission_control",
                "operations_capability_plan",
                "operations_active_learning",
                "operations_pinned_learning",
                "operations_action_plan",
                "operations_action_execute",
                "operations_skill_assembly",
            },
        )
        mission_result = await tools["operations_mission_control"].func(project_id="default")
        route = await tools["operations_capability_plan"].func(
            capability_kind="llm",
            task_type="coding",
            sandboxed_tools=False,
        )
        self.assertEqual(mission_result["project_id"], "default")
        self.assertEqual(route["mode"], "dry_run")
        self.assertTrue(route["allowed"])
        self.assertFalse(tools["operations_capability_plan"].read_only)


class _FakeMemory:
    async def build_project_knowledge_context(self, project_id=None):
        return "project context"

    async def build_session_prompt_context(self, session_id, include_latest_user_turn=False):
        return "session context"


class _FakeStore:
    async def get_events(self, limit=12):
        return []


class _FakeMission:
    async def summary(self, *, project_id):
        return {"project_id": project_id, "alerts": [{"severity": "critical"}]}

    async def daily_brief(self, *, project_id):
        return f"brief:{project_id}"


class SecretaryMissionContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_secretary_prompt_and_direct_brief_include_mission_control(self) -> None:
        secretary = SecretaryService(
            llm=SimpleNamespace(),
            store=_FakeStore(),
            memory=_FakeMemory(),
            preferences=SimpleNamespace(load_merged=lambda project_id=None: {}),
            skills=SimpleNamespace(
                list_skills=lambda: [],
                projects_dir=Path("/tmp/openopc-secretary-test/projects"),
            ),
            policies=SimpleNamespace(summarize_policies=lambda project_id=None: "none"),
            mission_control=_FakeMission(),
            operator_actions=SimpleNamespace(),
            skill_assembly=SimpleNamespace(
                recommend=lambda **kwargs: {
                    "goal": kwargs["goal"],
                    "roles": [{"role_id": "qa"}],
                    "mutations_applied": False,
                }
            ),
            role_provider=lambda: [{"role_id": "qa"}],
        )

        prompt = json.loads(
            await secretary._build_prompt(
                "status and skill readiness",
                project_id="alpha",
                session_id="secretary-session",
            )
        )
        brief = await secretary.mission_brief("alpha")

        self.assertEqual(prompt["mission_control"]["project_id"], "alpha")
        self.assertEqual(prompt["mission_control"]["alerts"][0]["severity"], "critical")
        self.assertEqual(prompt["skill_assembly_preview"]["roles"][0]["role_id"], "qa")
        self.assertFalse(
            prompt["operator_action_policy"]["automatic_execution_allowed"]
        )
        self.assertEqual(brief, "brief:alpha")


if __name__ == "__main__":
    unittest.main()
