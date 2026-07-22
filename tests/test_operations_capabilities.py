from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from opc.core.config import NUResourceGenConfig
from opc.database.store import OPCStore
from opc.integrations.nu_resource_gen import NUResourceGenBridge
from opc.operations.capabilities import UnifiedCapabilityBroker
from opc.operations.models import CapabilityKind, CapabilityRequest
from opc.operations.repository import OperationsRepository


class _FakeTarget:
    def __init__(self, provider: str, model: str, api_base: str) -> None:
        self.provider = provider
        self.model = model
        self.api_base = api_base

    def safe_dict(self):
        return {
            "provider": self.provider,
            "model": self.model,
            "api_base": self.api_base,
            "credential_configured": True,
            "transport_ready": True,
        }


class _FakeLLMRouter:
    enabled = True

    def __init__(self) -> None:
        self.calls = []

    def diagnostics(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "available": True,
            "final_order": ["cloud", "ollama-local"],
            "matched_profile_id": "agentic-tools",
        }

    def targets(self, **_kwargs):
        return (
            _FakeTarget("cloud", "openai/cloud-model", "https://cloud.example/v1"),
            _FakeTarget("ollama-local", "openai/local-model", "http://127.0.0.1:11434/v1"),
        )


class _FakeLLMRouterWithUnusableCloud(_FakeLLMRouter):
    def targets(self, **_kwargs):
        return (
            _FakeTarget(
                "ollama-local",
                "openai/local-model",
                "http://127.0.0.1:11434/v1",
            ),
        )


class _FakeResourceBridge:
    enabled = True

    def __init__(self) -> None:
        self.plan_calls = []
        self._candidates = [
            {
                "candidate_id": "paid-image",
                "provider": "cloud",
                "model": "paid",
                "category": "image_generation",
                "cost": 0.04,
                "cost_unit": "usd",
                "credentials_configured": True,
                "supported_task_types": ["image_generation"],
                "deprecated": False,
                "simulation_only": False,
            },
            {
                "candidate_id": "local-image",
                "provider": "comfyui",
                "model": "local",
                "category": "image_generation",
                "cost": 0.0,
                "cost_unit": "local",
                "credentials_configured": True,
                "supported_task_types": ["image_generation"],
                "deprecated": False,
                "simulation_only": False,
            },
            {
                "candidate_id": "unknown-image",
                "provider": "unknown",
                "model": "unknown",
                "category": "image_generation",
                "cost": None,
                "cost_unit": "unknown",
                "credentials_configured": False,
                "supported_task_types": ["image_generation"],
                "deprecated": False,
                "simulation_only": False,
            },
        ]

    def list_candidates(self, *, candidate_id=None, category=None, limit=None):
        values = self._candidates
        if candidate_id:
            values = [item for item in values if item["candidate_id"] == candidate_id]
        if category:
            values = [item for item in values if item["category"] == category]
        if limit:
            values = values[:limit]
        return {"count": len(values), "candidates": values}

    def plan(self, **kwargs):
        self.plan_calls.append(kwargs)
        return {"dry_run": True, "candidate": kwargs["candidate_id"], "policy": {"allowed": True}}

    def status(self):
        return {
            "allow_live": True,
            "allowed_live_candidates": ["local-image"],
        }

    def provider_readiness(self, provider):
        ready = provider in {"cloud", "comfyui"}
        return {
            "credential_ready": ready,
            "transport_ready": ready,
            "detail": "test provider ready" if ready else "test provider unavailable",
        }


class _FakeAdapterRegistry:
    def list_available(self):
        return ["codex", "cursor"]

    def describe_available(self):
        return [
            {
                "agent_type": "codex",
                "capabilities": ["coding", "testing"],
                "local": True,
                "free": True,
            },
            {
                "agent_type": "cursor",
                "capabilities": ["coding"],
                "local": False,
            },
        ]


class UnifiedCapabilityBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = OPCStore(self.root / "tasks.db")
        await self.store.initialize()
        self.repository = OperationsRepository(self.store)
        self.llm_router = _FakeLLMRouter()
        self.resource_bridge = _FakeResourceBridge()
        self.broker = UnifiedCapabilityBroker(
            self.repository,
            llm_router=self.llm_router,
            resource_bridge=self.resource_bridge,
            adapter_registry=_FakeAdapterRegistry(),
            default_llm_model="openai/fallback",
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def test_tool_route_requires_sandbox_and_keeps_zero_gpu_metadata(self) -> None:
        blocked = await self.broker.plan(
            CapabilityRequest(
                capability_kind=CapabilityKind.LLM,
                task_type="agentic_tools",
                required_capabilities=["tool_use"],
                preferred_providers=["ollama-local"],
                sandboxed_tools=False,
                gpu_free_vram_mib=0,
                require_free=True,
            )
        )
        allowed = await self.broker.plan(
            CapabilityRequest(
                capability_kind=CapabilityKind.LLM,
                task_type="agentic_tools",
                required_capabilities=["tool_use"],
                preferred_providers=["ollama-local"],
                sandboxed_tools=True,
                gpu_free_vram_mib=0,
                require_free=True,
            )
        )

        self.assertFalse(blocked.allowed)
        self.assertIn("sandboxed_tools=true", blocked.blockers[0])
        self.assertTrue(allowed.allowed)
        self.assertEqual(
            allowed.readiness,
            {
                "plan_allowed": True,
                "credential_ready": True,
                "transport_ready": True,
                "live_allowed": False,
            },
        )
        self.assertEqual(allowed.provider, "ollama-local")
        self.assertEqual(allowed.estimated_cost_usd, 0.0)
        self.assertEqual(self.llm_router.calls[-1]["gpu_free_vram_mib"], 0)

    async def test_default_llm_selection_prefers_usable_local_target(self) -> None:
        route = await self.broker.plan(
            CapabilityRequest(
                capability_kind=CapabilityKind.LLM,
                task_type="dialogue",
            )
        )
        free_route = await self.broker.plan(
            CapabilityRequest(
                capability_kind=CapabilityKind.LLM,
                task_type="dialogue",
                preferred_providers=["cloud"],
                require_free=True,
            )
        )

        self.assertEqual(route.provider, "ollama-local")
        self.assertEqual(route.diagnostics["selection_order"][0], "ollama-local")
        self.assertTrue(free_route.allowed)
        self.assertEqual(free_route.provider, "ollama-local")

    async def test_resource_route_uses_actual_catalog_and_local_free_first(self) -> None:
        route = await self.broker.plan(
            CapabilityRequest(
                capability_kind=CapabilityKind.RESOURCE,
                task_type="image_generation",
                prompt="A clean vertical storyboard panel",
                require_free=True,
                max_cost_usd=0.0,
            )
        )

        self.assertTrue(route.allowed)
        self.assertEqual(route.candidate_id, "local-image")
        self.assertEqual(route.provider, "comfyui")
        self.assertEqual(route.mode, "dry_run")
        self.assertEqual(self.resource_bridge.plan_calls[0]["candidate_id"], "local-image")
        attempts = await self.repository.list_capability_attempts(project_id="default")
        self.assertEqual(attempts[0].status, "planned")

    async def test_llm_route_ignores_diagnostic_provider_without_usable_target(self) -> None:
        broker = UnifiedCapabilityBroker(
            self.repository,
            llm_router=_FakeLLMRouterWithUnusableCloud(),
        )
        route = await broker.plan(
            CapabilityRequest(
                capability_kind=CapabilityKind.LLM,
                task_type="dialogue",
                preferred_providers=["cloud"],
            )
        )

        self.assertTrue(route.allowed)
        self.assertEqual(route.provider, "ollama-local")
        self.assertEqual(route.diagnostics["unusable_ordered_providers"], ["cloud"])

    async def test_unknown_llm_and_agent_costs_fail_closed_under_ceiling(self) -> None:
        llm = await self.broker.plan(
            CapabilityRequest(
                capability_kind=CapabilityKind.LLM,
                task_type="dialogue",
                preferred_providers=["cloud"],
                max_cost_usd=1.0,
            )
        )
        agent = await self.broker.plan(
            CapabilityRequest(
                capability_kind=CapabilityKind.EXTERNAL_AGENT,
                task_type="coding",
                preferred_providers=["cursor"],
                max_cost_usd=1.0,
            )
        )

        self.assertFalse(llm.allowed)
        self.assertTrue(any("cost is unknown" in item for item in llm.blockers))
        self.assertFalse(agent.allowed)
        self.assertTrue(any("cost is unknown" in item for item in agent.blockers))

    async def test_default_llm_dry_run_distinguishes_plan_from_execution_readiness(self) -> None:
        broker = UnifiedCapabilityBroker(
            self.repository,
            default_llm_model="openai/fallback",
            default_llm_api_base="https://openrouter.ai/api/v1",
        )
        dry_run = await broker.plan(
            CapabilityRequest(capability_kind=CapabilityKind.LLM, task_type="dialogue")
        )
        live = await broker.plan(
            CapabilityRequest(
                capability_kind=CapabilityKind.LLM,
                task_type="dialogue",
                allow_live=True,
            )
        )

        self.assertTrue(dry_run.allowed)
        self.assertTrue(dry_run.readiness["plan_allowed"])
        self.assertFalse(dry_run.readiness["credential_ready"])
        self.assertFalse(dry_run.readiness["transport_ready"])
        self.assertFalse(live.allowed)
        self.assertTrue(any("transport-ready" in item for item in live.blockers))

    async def test_unknown_resource_cost_fails_closed_under_ceiling(self) -> None:
        route = await self.broker.plan(
            CapabilityRequest(
                capability_kind=CapabilityKind.RESOURCE,
                task_type="image_generation",
                candidate_id="unknown-image",
                prompt="test",
                max_cost_usd=1.0,
            )
        )

        self.assertFalse(route.allowed)
        self.assertTrue(any("cost is unknown" in item for item in route.blockers))

    async def test_live_resource_requires_allowlist_and_explicit_confirmation(self) -> None:
        unconfirmed = await self.broker.plan(
            CapabilityRequest(
                capability_kind=CapabilityKind.RESOURCE,
                task_type="image_generation",
                candidate_id="local-image",
                prompt="test",
                allow_live=True,
            )
        )
        confirmed = await self.broker.plan(
            CapabilityRequest(
                capability_kind=CapabilityKind.RESOURCE,
                task_type="image_generation",
                candidate_id="local-image",
                prompt="test",
                allow_live=True,
                metadata={"confirm_live": True},
            )
        )

        self.assertFalse(unconfirmed.allowed)
        self.assertTrue(any("confirm_live" in item for item in unconfirmed.blockers))
        self.assertTrue(confirmed.allowed)
        self.assertEqual(confirmed.mode, "live")

    async def test_external_agent_filters_capabilities_and_preference(self) -> None:
        route = await self.broker.plan(
            CapabilityRequest(
                capability_kind=CapabilityKind.EXTERNAL_AGENT,
                task_type="coding",
                required_capabilities=["testing"],
                preferred_providers=["cursor", "codex"],
                require_free=True,
            )
        )

        self.assertTrue(route.allowed)
        self.assertEqual(route.provider, "codex")
        self.assertEqual(route.diagnostics["compatible"], ["codex"])

    async def test_execute_requires_live_and_records_result(self) -> None:
        request = CapabilityRequest(
            capability_kind=CapabilityKind.EXTERNAL_AGENT,
            task_type="coding",
            required_capabilities=["testing"],
            preferred_providers=["codex"],
            allow_live=True,
        )

        route, result = await self.broker.execute(
            request,
            lambda selected, _request: {
                "agent": selected.provider,
                "status": "done",
                "cost": 0.0,
                "cost_unit": "usd",
            },
        )

        self.assertEqual(route.mode, "delegate")
        self.assertEqual(result["agent"], "codex")
        attempts = await self.repository.list_capability_attempts(project_id="default")
        self.assertEqual(attempts[0].status, "completed")
        self.assertEqual(attempts[0].cost_usd, 0.0)

    async def test_installed_resource_library_can_plan_real_candidate_without_live_call(self) -> None:
        bridge = NUResourceGenBridge(
            NUResourceGenConfig(
                enabled=True,
                record_ledger=False,
                archive_live_artifacts=False,
            ),
            opc_home=self.root,
            project_id="default",
        )
        broker = UnifiedCapabilityBroker(self.repository, resource_bridge=bridge)
        route = await broker.plan(
            CapabilityRequest(
                capability_kind=CapabilityKind.RESOURCE,
                task_type="image_generation",
                candidate_id="gemini_image_flash",
                prompt="Dry-run only: vertical webtoon panel",
            )
        )

        self.assertTrue(route.allowed)
        self.assertEqual(route.candidate_id, "gemini_image_flash")
        self.assertEqual(route.provider, "gemini")
        self.assertTrue(route.diagnostics["dry_run_plan"]["dry_run"])


if __name__ == "__main__":
    unittest.main()
