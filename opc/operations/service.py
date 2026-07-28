"""Facade that binds every operations subsystem to one project store."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

from opc.core.config import OperationsConfig
from opc.operations.capabilities import UnifiedCapabilityBroker
from opc.operations.canary import ProviderCanaryScheduler, ProviderCanaryService
from opc.operations.durable import DurableRunKernel
from opc.operations.evaluation import OutcomeEvaluator
from opc.operations.learning import LearningAssetManager
from opc.operations.learning_activation import LearningActivationResolver
from opc.operations.mission_control import MissionControlService
from opc.operations.models import CapabilityKind, CapabilityRequest
from opc.operations.outbox import OutboxDispatcher, event_bus_handler
from opc.operations.operator_actions import OperatorActionService
from opc.operations.repository import OperationsRepository
from opc.operations.resource_pipeline import ApprovedResourcePipeline, ResourceApprovalTokenIssuer
from opc.operations.routing_outcomes import RoutingOutcomeService
from opc.operations.skill_assembly import SkillAssemblyService
from opc.operations.staffing import StaffingOptimizer


class OperationsService:
    """One bound goal-to-learning operating loop for an ``OPCStore``."""

    def __init__(
        self,
        store: Any,
        config: OperationsConfig | None = None,
        *,
        llm_router: Any | None = None,
        resource_bridge: Any | None = None,
        adapter_registry: Any | None = None,
        default_llm_model: str = "",
        default_llm_api_base: str = "",
        default_llm_credential_ready: bool = False,
        default_llm_transport_ready: bool = False,
    ) -> None:
        self.config = config or OperationsConfig()
        self.llm_router = llm_router
        self.project_id = str(getattr(store, "project_id", "") or "default")
        self.repository = OperationsRepository(store)
        self.evaluator = OutcomeEvaluator(self.repository, self.config.evaluation)
        self.durable = DurableRunKernel(self.repository, self.config.durable)
        self.learning = LearningAssetManager(self.repository, self.config.learning)
        self.learning_activations = LearningActivationResolver(self.repository)
        self.operator_actions = OperatorActionService(
            self.repository,
            self.durable,
            self.learning,
        )
        self.skill_assembly = SkillAssemblyService()
        self.capabilities = UnifiedCapabilityBroker(
            self.repository,
            llm_router=llm_router,
            resource_bridge=resource_bridge,
            adapter_registry=adapter_registry,
            default_llm_model=default_llm_model,
            default_llm_api_base=default_llm_api_base,
            default_llm_credential_ready=default_llm_credential_ready,
            default_llm_transport_ready=default_llm_transport_ready,
            subscription_call_limit=self.config.providers.subscription_call_limit,
            subscription_window_seconds=(
                self.config.providers.subscription_window_seconds
            ),
            subscription_providers=self.config.providers.subscription_providers,
        )
        self.staffing = StaffingOptimizer(self.repository, self.config.staffing)
        self.canaries = ProviderCanaryService(self.repository, self.capabilities)
        self.canary_scheduler = ProviderCanaryScheduler(
            self.canaries,
            lambda: CapabilityRequest(
                capability_kind=CapabilityKind.LLM,
                task_type="dialogue",
                project_id=self.project_id,
                allow_live=False,
                local_first=True,
            ),
            interval_seconds=(
                self.config.providers.status_canary_interval_seconds
            ),
            expected_model=self.config.providers.canary_expected_model,
            availability_target=self.config.providers.slo_availability_target,
            p95_latency_target_ms=(
                self.config.providers.slo_p95_latency_target_ms
            ),
            minimum_samples=self.config.providers.slo_min_samples,
            trend_window_samples=(
                self.config.providers.slo_trend_window_samples
            ),
            project_id=self.project_id,
        )
        self.routing_outcomes = RoutingOutcomeService(self.repository, self.learning)
        approval_issuer = _resource_approval_issuer_from_environment()
        self.resource_pipeline = (
            ApprovedResourcePipeline(
                self.capabilities,
                resource_bridge,
                artifact_root=Path(store.db_path).parent / "operations" / "resource_pipelines",
                approval_issuer=approval_issuer,
                quality_executor=getattr(
                    resource_bridge, "evaluate_artifact_quality", None
                ),
            )
            if resource_bridge is not None
            else None
        )
        self.mission_control = MissionControlService(
            self.repository,
            self.durable,
            provider_config=self.config.providers,
        )
        self.outbox_dispatcher: OutboxDispatcher | None = None

    def rebind(self, store: Any) -> None:
        self.repository.rebind(store)
        self.project_id = str(getattr(store, "project_id", "") or "default")
        self.canary_scheduler.project_id = self.project_id

    def bind_adapter_registry(self, registry: Any | None) -> None:
        self.capabilities.bind_adapter_registry(registry)

    def bind_skill_library(self, skill_library: Any) -> None:
        self.skill_assembly.bind(skill_library)

    async def start_run(self, manifest: Any, *, now: Any = None) -> tuple[Any, Any]:
        """Start a run after pinning its goal identity and promoted learning assets."""
        existing = await self.repository.get_manifest(manifest.run_id)
        if existing is None:
            goal = await self.repository.get_goal(manifest.goal_id)
            if goal is None:
                raise KeyError(f"goal contract not found: {manifest.goal_id}")
            if not manifest.organization_id:
                manifest.organization_id = goal.organization_id
            if manifest.goal_version == 0:
                manifest.goal_version = goal.version
            await self.learning_activations.pin_manifest(manifest)
        return await self.durable.start_run(manifest, now=now)

    async def execute_llm(
        self,
        request: Any,
        llm_provider: Any,
        messages: Sequence[Mapping[str, Any]],
        **chat_kwargs: Any,
    ) -> tuple[Any, Any]:
        """Execute an LLM through the persisted capability route contract."""

        async def execute(route: Any, parsed: Any) -> Any:
            return await llm_provider.chat(
                [dict(item) for item in messages],
                task_type=parsed.task_type,
                route_contract=route.to_dict(),
                **chat_kwargs,
            )

        return await self.capabilities.execute(request, execute)

    def create_llm_request(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        task_type: str | None,
        tools: Sequence[Mapping[str, Any]] | None,
        context: Mapping[str, Any] | None = None,
    ) -> CapabilityRequest:
        """Build a privacy-minimal request for an actual OpenOPC LLM call."""

        details = dict(context or {})
        has_tools = bool(tools)
        workload = str(task_type or details.get("workload") or "").strip()
        if has_tools:
            workload = "agentic_tools"
        elif not workload:
            workload = "dialogue"
        max_cost = details.get("max_cost_usd")
        metadata_keys = (
            "task_id",
            "session_id",
            "runtime_session_id",
            "conversation_turn_id",
            "iteration",
            "caller",
        )
        return CapabilityRequest(
            capability_kind=CapabilityKind.LLM,
            task_type=workload,
            project_id=str(details.get("project_id") or "default"),
            run_id=str(details.get("run_id") or ""),
            # Prompts can contain credentials or user data. Contracts retain
            # structural evidence, never raw message bodies.
            prompt="",
            required_capabilities=["tool_use"] if has_tools else [],
            preferred_providers=[
                str(item)
                for item in details.get("preferred_providers", []) or []
                if str(item).strip()
            ],
            tags=[
                str(item)
                for item in details.get("tags", ["openopc-runtime"]) or []
                if str(item).strip()
            ],
            max_cost_usd=None if max_cost is None else float(max_cost),
            allow_live=True,
            local_first=bool(details.get("local_first", True)),
            require_free=bool(details.get("require_free", False)),
            sandboxed_tools=bool(details.get("sandboxed_tools", has_tools)),
            gpu_free_vram_mib=max(0, int(details.get("gpu_free_vram_mib", 0) or 0)),
            hardware_profile=str(details.get("hardware_profile") or ""),
            parameters={
                "message_count": len(messages),
                "tool_count": len(tools or []),
            },
            metadata={
                key: details[key]
                for key in metadata_keys
                if key in details and details[key] not in (None, "")
            },
        )

    async def execute_llm_stream(
        self,
        request: CapabilityRequest,
        llm_provider: Any,
        messages: Sequence[Mapping[str, Any]],
        **chat_kwargs: Any,
    ) -> AsyncIterator[Any]:
        """Stream an LLM call while durably closing its execution contract."""

        parsed, route, contract, started = await self.capabilities.begin_execution(request)
        result: dict[str, Any] = {
            "provider": route.provider,
            "candidate_id": route.candidate_id,
            "model": route.model,
            "finish_reason": "",
            "usage_accounting": {
                "measured": False,
                "source": "unknown",
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "cost_usd": None,
                "subscription_quota": {},
            },
        }
        provider_stream = llm_provider.chat_stream(
            [dict(item) for item in messages],
            task_type=parsed.task_type,
            route_contract=contract.to_dict(),
            **chat_kwargs,
        )
        try:
            async for event in provider_stream:
                payload = dict(getattr(event, "payload", {}) or {})
                if payload.get("provider"):
                    result["provider"] = str(payload["provider"])
                if getattr(event, "model", ""):
                    result["model"] = str(event.model)
                if getattr(event, "event_type", "") == "usage":
                    accounting = payload.get("usage_accounting")
                    if isinstance(accounting, Mapping):
                        result["usage_accounting"] = dict(accounting)
                elif getattr(event, "event_type", "") == "message_stop":
                    result["finish_reason"] = str(payload.get("finish_reason") or "stop")
                yield event
        except BaseException as exc:
            await self.capabilities.fail_execution(parsed, route, contract, started, exc)
            raise
        finally:
            await provider_stream.aclose()
        await self.capabilities.complete_execution(parsed, route, contract, started, result)

    async def start_outbox_dispatcher(self, event_bus: Any) -> None:
        if not self.config.durable.outbox_dispatcher_enabled:
            return
        if self.outbox_dispatcher is None:
            self.outbox_dispatcher = OutboxDispatcher(
                self.durable,
                event_bus_handler(event_bus),
                batch_size=self.config.durable.outbox_dispatch_batch_size,
                poll_seconds=self.config.durable.outbox_dispatch_poll_seconds,
            )
        await self.outbox_dispatcher.start()

    async def stop_outbox_dispatcher(self) -> None:
        if self.outbox_dispatcher is not None:
            await self.outbox_dispatcher.stop()

    async def start_provider_monitoring(self) -> None:
        start_shadow = getattr(self.llm_router, "start_background_shadow", None)
        if callable(start_shadow):
            await asyncio.to_thread(start_shadow)
        if self.config.providers.status_canary_enabled:
            await self.canary_scheduler.start()

    async def stop_provider_monitoring(self) -> None:
        await self.canary_scheduler.stop()
        stop_shadow = getattr(self.llm_router, "stop_background_shadow", None)
        if callable(stop_shadow):
            await asyncio.to_thread(stop_shadow)


def _resource_approval_issuer_from_environment(
) -> ResourceApprovalTokenIssuer | None:
    raw_keyring = str(
        os.environ.get("OPENOPC_RESOURCE_APPROVAL_KEYS", "") or ""
    ).strip()
    if raw_keyring:
        try:
            parsed = json.loads(raw_keyring)
        except json.JSONDecodeError as exc:
            raise ValueError("OPENOPC_RESOURCE_APPROVAL_KEYS must be a JSON object") from exc
        if not isinstance(parsed, Mapping) or not parsed:
            raise ValueError("OPENOPC_RESOURCE_APPROVAL_KEYS must be a non-empty JSON object")
        keys = {
            str(key).strip(): str(value)
            for key, value in parsed.items()
            if str(key).strip()
        }
        active_key_id = str(
            os.environ.get("OPENOPC_RESOURCE_APPROVAL_ACTIVE_KEY_ID", "") or ""
        ).strip()
        if not active_key_id:
            if len(keys) != 1:
                raise ValueError(
                    "OPENOPC_RESOURCE_APPROVAL_ACTIVE_KEY_ID is required for a multi-key keyring"
                )
            active_key_id = next(iter(keys))
        if active_key_id not in keys:
            raise ValueError("active resource approval key_id is absent from the keyring")
        return ResourceApprovalTokenIssuer(
            keys[active_key_id],
            key_id=active_key_id,
            verification_keys=keys,
        )

    secret = str(
        os.environ.get("OPENOPC_RESOURCE_APPROVAL_SECRET", "") or ""
    )
    if len(secret.encode("utf-8")) < 16:
        return None
    key_id = str(
        os.environ.get("OPENOPC_RESOURCE_APPROVAL_KEY_ID", "legacy") or "legacy"
    ).strip()
    return ResourceApprovalTokenIssuer(secret, key_id=key_id)
