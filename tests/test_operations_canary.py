from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
import unittest

from opc.core.config import OperationsConfig, ProviderOperationsConfig
from opc.database.store import OPCStore
from opc.operations.canary import (
    ProviderCanaryScheduler,
    ProviderCanaryService,
    build_readiness_campaign_plan,
    run_readiness_campaign,
    summarize_provider_readiness,
    summarize_provider_slo,
    validate_failure_drill_result,
    verify_readiness_campaign_plan,
)
from opc.operations.models import (
    CapabilityKind,
    CapabilityRequest,
    CapabilityRoute,
    ProviderCanaryResult,
)
from opc.operations.repository import OperationsRepository
from opc.operations.service import OperationsService


class _Broker:
    async def plan(self, request, *, record_attempt=False):
        assert record_attempt is False
        return CapabilityRoute(
            request_id=request.request_id,
            capability_kind=request.capability_kind,
            provider="ollama-local",
            candidate_id="qwen3:14b",
            model="qwen3:14b",
            mode="dry_run",
            allowed=True,
            readiness={
                "plan_allowed": True,
                "credential_ready": True,
                "transport_ready": True,
                "live_allowed": False,
            },
        )


class _ShadowLifecycleRouter:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start_background_shadow(self):
        self.started += 1
        return {"running": True}

    def stop_background_shadow(self):
        self.stopped += 1
        return {"running": False, "drained": True}


class ProviderCanaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.repository = OperationsRepository(self.store)
        self.service = ProviderCanaryService(self.repository, _Broker())  # type: ignore[arg-type]

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def test_status_canary_persists_transport_readiness_and_model_drift(self) -> None:
        result = await self.service.status_canary(
            CapabilityRequest(
                capability_kind=CapabilityKind.LLM,
                task_type="dialogue",
                project_id="default",
            ),
            expected_model="qwen3:8b",
        )

        self.assertTrue(result.success)
        self.assertTrue(result.transport_ready)
        self.assertTrue(result.model_drift)
        stored = await self.repository.list_provider_canary_results(project_id="default")
        self.assertEqual(stored[0].canary_id, result.canary_id)

    async def test_slo_summary_tracks_availability_latency_and_consecutive_failures(self) -> None:
        for success, latency in ((True, 10.0), (False, 30.0), (False, 20.0)):
            await self.repository.save_provider_canary_result(
                ProviderCanaryResult(
                    project_id="default",
                    capability_kind=CapabilityKind.LLM,
                    provider="codex",
                    success=success,
                    latency_ms=latency,
                    error_category="quota" if not success else "",
                )
            )

        summary = await self.service.slo_summary(
            project_id="default",
            provider="codex",
            availability_target=0.9,
        )
        codex = summary["providers"]["codex"]
        self.assertEqual(codex["samples"], 3)
        self.assertAlmostEqual(codex["availability"], 1 / 3, places=6)
        self.assertEqual(codex["p95_latency_ms"], 30.0)
        self.assertEqual(codex["consecutive_failures"], 2)
        self.assertFalse(codex["target_met"])

    async def test_slo_fails_when_latency_target_is_missed(self) -> None:
        await self.repository.save_provider_canary_result(
            ProviderCanaryResult(
                project_id="default",
                capability_kind=CapabilityKind.LLM,
                provider="codex",
                success=True,
                latency_ms=250.0,
            )
        )

        summary = await self.service.slo_summary(
            project_id="default",
            availability_target=0.95,
            p95_latency_target_ms=100.0,
        )

        codex = summary["providers"]["codex"]
        self.assertTrue(codex["availability_target_met"])
        self.assertFalse(codex["latency_target_met"])
        self.assertFalse(codex["target_met"])

    async def test_slo_attainment_requires_the_configured_sample_floor(self) -> None:
        for latency in (10.0, 12.0):
            await self.repository.save_provider_canary_result(
                ProviderCanaryResult(
                    project_id="default",
                    capability_kind=CapabilityKind.LLM,
                    provider="codex",
                    success=True,
                    latency_ms=latency,
                )
            )

        insufficient = await self.service.slo_summary(
            project_id="default",
            provider="codex",
            minimum_samples=3,
        )
        codex = insufficient["providers"]["codex"]
        self.assertEqual(codex["attainment_state"], "insufficient_samples")
        self.assertFalse(codex["sample_target_met"])
        self.assertFalse(codex["target_met"])
        self.assertFalse(codex["promotion_ready"])

        await self.repository.save_provider_canary_result(
            ProviderCanaryResult(
                project_id="default",
                capability_kind=CapabilityKind.LLM,
                provider="codex",
                success=True,
                latency_ms=11.0,
            )
        )
        attained = await self.service.slo_summary(
            project_id="default",
            provider="codex",
            minimum_samples=3,
        )
        codex = attained["providers"]["codex"]
        self.assertEqual(codex["attainment_state"], "met")
        self.assertTrue(codex["target_met"])
        self.assertFalse(codex["trend"]["ready"])
        self.assertFalse(codex["promotion_ready"])

        for latency in (10.0, 12.0, 11.0):
            await self.repository.save_provider_canary_result(
                ProviderCanaryResult(
                    project_id="default",
                    capability_kind=CapabilityKind.LLM,
                    provider="codex",
                    success=True,
                    latency_ms=latency,
                )
            )
        trend_ready = await self.service.slo_summary(
            project_id="default",
            provider="codex",
            minimum_samples=3,
            trend_window_samples=3,
        )
        codex = trend_ready["providers"]["codex"]
        self.assertTrue(codex["trend"]["ready"])
        self.assertEqual(codex["trend"]["state"], "stable")
        self.assertTrue(codex["promotion_ready"])

    async def test_slo_trend_detects_a_degrading_recent_window(self) -> None:
        # Rows are returned newest first, so persist the healthy prior window
        # before the degraded current window.
        for success, latency in (
            (True, 10.0),
            (True, 11.0),
            (True, 12.0),
            (False, 80.0),
            (False, 90.0),
            (False, 100.0),
        ):
            await self.repository.save_provider_canary_result(
                ProviderCanaryResult(
                    project_id="default",
                    capability_kind=CapabilityKind.LLM,
                    provider="codex",
                    success=success,
                    latency_ms=latency,
                )
            )

        summary = await self.service.slo_summary(
            project_id="default",
            provider="codex",
            minimum_samples=3,
            trend_window_samples=3,
        )

        codex = summary["providers"]["codex"]
        self.assertTrue(codex["trend"]["ready"])
        self.assertEqual(codex["trend"]["state"], "degrading")
        self.assertFalse(codex["promotion_ready"])

    async def test_periodic_scheduler_runs_status_only_canary_and_stops(self) -> None:
        scheduler = ProviderCanaryScheduler(
            self.service,
            lambda: CapabilityRequest(
                capability_kind=CapabilityKind.LLM,
                task_type="dialogue",
            ),
            interval_seconds=0.01,
            project_id="default",
        )

        await scheduler.start()
        for _ in range(50):
            if scheduler.run_count:
                break
            await asyncio.sleep(0.01)
        await scheduler.stop()

        self.assertGreaterEqual(scheduler.run_count, 1)
        self.assertFalse(scheduler.running)
        self.assertEqual(scheduler.last_result.mode, "status")
        self.assertTrue(scheduler.last_slo["providers"]["ollama-local"]["target_met"])

    async def test_provider_monitoring_owns_background_shadow_lifecycle(self) -> None:
        router = _ShadowLifecycleRouter()
        operations = OperationsService(
            self.store,
            OperationsConfig(
                providers=ProviderOperationsConfig(status_canary_enabled=False)
            ),
            llm_router=router,
        )

        await operations.start_provider_monitoring()
        self.assertEqual(router.started, 1)
        self.assertFalse(operations.canary_scheduler.running)

        await operations.stop_provider_monitoring()
        self.assertEqual(router.stopped, 1)

    async def test_failure_drills_do_not_inflate_or_reduce_slo_samples(self) -> None:
        operational = ProviderCanaryResult(
            project_id="default",
            capability_kind=CapabilityKind.LLM,
            provider="codex",
            mode="status",
            success=True,
            latency_ms=10,
        )
        drill = ProviderCanaryResult(
            project_id="default",
            capability_kind=CapabilityKind.LLM,
            provider="codex",
            mode="drill",
            success=False,
            latency_ms=5000,
        )

        summary = summarize_provider_slo([drill, operational])

        self.assertEqual(summary["codex"]["samples"], 1)
        self.assertEqual(summary["codex"]["availability"], 1.0)

    async def test_production_readiness_requires_time_coverage_and_verified_drills(self) -> None:
        start = datetime.now(timezone.utc) - timedelta(hours=42)
        for index in range(8):
            await self.repository.save_provider_canary_result(
                ProviderCanaryResult(
                    project_id="default",
                    capability_kind=CapabilityKind.LLM,
                    provider="codex",
                    mode="status",
                    success=True,
                    latency_ms=10 + index,
                    checked_at=start + timedelta(hours=index * 6),
                )
            )
        scenarios = (
            "credential_expiry",
            "transport_timeout",
            "quota_exhaustion",
            "model_drift",
        )
        for scenario in scenarios:
            row = await self.service.record_failure_drill(
                project_id="default",
                provider="codex",
                scenario=scenario,
                result={
                    "actual_injection": True,
                    "expected_failure_observed": True,
                    "fallback_verified": True,
                    "alert_verified": True,
                    "recovery_verified": True,
                    "authority": "independent_observer",
                    "evidence": [f"artifact://drill/{scenario}"],
                },
            )
            self.assertTrue(row.success)

        summary = await self.service.readiness_summary(
            project_id="default",
            provider="codex",
            minimum_samples=4,
            trend_window_samples=2,
            minimum_observation_seconds=24 * 3600,
            time_bucket_seconds=6 * 3600,
            minimum_time_buckets=4,
            required_failure_scenarios=scenarios,
        )

        codex = summary["providers"]["codex"]
        self.assertTrue(codex["promotion_ready"])
        self.assertTrue(codex["observation_target_met"])
        self.assertTrue(codex["time_bucket_target_met"])
        self.assertTrue(codex["freshness_target_met"])
        self.assertTrue(codex["gap_target_met"])
        self.assertTrue(codex["failure_drill_target_met"])
        self.assertTrue(codex["production_ready"])
        self.assertEqual(codex["missing_failure_scenarios"], [])
        self.assertEqual(codex["blockers"], [])

    async def test_failure_drill_without_independent_evidence_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "independent authority"):
            await self.service.record_failure_drill(
                project_id="default",
                provider="codex",
                scenario="transport_timeout",
                result={
                    "actual_injection": True,
                    "expected_failure_observed": True,
                    "fallback_verified": True,
                    "alert_verified": True,
                    "recovery_verified": True,
                    "authority": "simulation",
                    "evidence": ["artifact://simulation"],
                },
            )

    async def test_readiness_rejects_stale_samples_large_gaps_and_old_drills(self) -> None:
        current = datetime(2026, 7, 28, tzinfo=timezone.utc)
        rows = [
            ProviderCanaryResult(
                project_id="default",
                capability_kind=CapabilityKind.LLM,
                provider="codex",
                mode="status",
                success=True,
                latency_ms=10,
                checked_at=checked_at,
            )
            for checked_at in (
                current - timedelta(hours=30),
                current - timedelta(hours=24),
                current - timedelta(hours=1),
            )
        ]
        rows.append(
            ProviderCanaryResult(
                project_id="default",
                capability_kind=CapabilityKind.LLM,
                provider="codex",
                mode="drill",
                success=True,
                latency_ms=0,
                checked_at=current - timedelta(days=31),
                metadata={
                    "drill_scenario": "credential_expiry",
                    "authority": "independent_observer",
                    "evidence": ["artifact://old-drill"],
                    "actual_injection": True,
                    "expected_failure_observed": True,
                    "fallback_verified": True,
                    "alert_verified": True,
                    "recovery_verified": True,
                },
            )
        )

        readiness = summarize_provider_readiness(
            rows,
            minimum_samples=2,
            trend_window_samples=1,
            minimum_observation_seconds=24 * 3600,
            time_bucket_seconds=6 * 3600,
            minimum_time_buckets=3,
            maximum_sample_age_seconds=1800,
            maximum_gap_seconds=8 * 3600,
            failure_drill_max_age_seconds=30 * 24 * 3600,
            required_failure_scenarios=["credential_expiry"],
            now=current,
        )["codex"]

        self.assertFalse(readiness["freshness_target_met"])
        self.assertFalse(readiness["gap_target_met"])
        self.assertEqual(
            readiness["missing_failure_scenarios"],
            ["credential_expiry"],
        )
        self.assertFalse(readiness["production_ready"])
        self.assertIn(
            "latest canary sample is stale or future-dated",
            readiness["blockers"],
        )

    async def test_readiness_campaign_plan_is_status_only_and_deterministic(self) -> None:
        start = datetime(2026, 7, 28, tzinfo=timezone.utc)
        arguments = {
            "campaign_id": "codex-2026q3",
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "start_at": start,
            "interval_seconds": 300,
            "observation_seconds": 86_400,
            "time_bucket_seconds": 21_600,
            "minimum_time_buckets": 4,
            "required_failure_scenarios": [
                "credential_expiry",
                "transport_timeout",
            ],
        }

        first = build_readiness_campaign_plan(**arguments)
        second = build_readiness_campaign_plan(**arguments)

        self.assertEqual(first, second)
        self.assertFalse(first["status_canary"]["generation_allowed"])
        self.assertEqual(
            first["status_canary"]["expected_minimum_samples"],
            289,
        )
        self.assertFalse(first["automatic_failure_injection"])
        self.assertEqual(len(first["failure_drills"]), 2)
        self.assertEqual(len(first["plan_digest"]), 64)

        verified = verify_readiness_campaign_plan(first)
        self.assertEqual(verified["plan_digest"], first["plan_digest"])
        tampered = {**first, "provider": "other"}
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            verify_readiness_campaign_plan(tampered)

    async def test_campaign_runner_records_real_samples_with_plan_provenance(self) -> None:
        plan = build_readiness_campaign_plan(
            campaign_id="ollama-readiness",
            provider="ollama-local",
            start_at=datetime.now(timezone.utc),
            interval_seconds=1,
            observation_seconds=60,
            required_failure_scenarios=["transport_timeout"],
        )
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        request = CapabilityRequest(
            capability_kind=CapabilityKind.LLM,
            task_type="dialogue",
            allow_live=True,
        )
        report = await run_readiness_campaign(
            self.service,
            request,
            plan,
            iterations=2,
            sleep=fake_sleep,
        )

        self.assertEqual(report["samples"], 2)
        self.assertFalse(report["generation_allowed"])
        self.assertEqual(sleeps, [1.0])
        self.assertFalse(request.allow_live)
        self.assertEqual(request.preferred_providers, ["ollama-local"])
        rows = await self.repository.list_provider_canary_results(project_id="default")
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row.metadata["readiness_campaign_id"], "ollama-readiness")
            self.assertEqual(row.metadata["readiness_campaign_plan_digest"], plan["plan_digest"])

    async def test_drill_pack_validation_requires_every_observed_check(self) -> None:
        payload = {
            "authority": "independent_observer",
            "evidence": ["artifact://drill/transport-timeout"],
            "actual_injection": True,
            "expected_failure_observed": True,
            "fallback_verified": True,
            "alert_verified": True,
            "recovery_verified": False,
        }
        with self.assertRaisesRegex(ValueError, "recovery_verified"):
            validate_failure_drill_result(
                "transport_timeout", payload, require_passed=True,
            )
        payload["recovery_verified"] = True
        validated = validate_failure_drill_result(
            "transport_timeout", payload, require_passed=True,
        )
        self.assertTrue(validated["passed"])


if __name__ == "__main__":
    unittest.main()
