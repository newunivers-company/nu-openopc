from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from opc.database.store import OPCStore
from opc.operations.canary import ProviderCanaryService
from opc.operations.models import (
    CapabilityKind,
    CapabilityRequest,
    CapabilityRoute,
    ProviderCanaryResult,
)
from opc.operations.repository import OperationsRepository


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


if __name__ == "__main__":
    unittest.main()
