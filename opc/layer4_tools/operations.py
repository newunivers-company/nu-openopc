"""Agent-safe tools for the outcome-driven operating loop."""

from __future__ import annotations

from typing import Any

from opc.layer4_tools.registry import ToolDefinition
from opc.operations.models import CapabilityKind, CapabilityRequest
from opc.operations.service import OperationsService


def create_operations_tools(service: OperationsService) -> list[ToolDefinition]:
    if not service.config.enabled:
        return []

    async def mission_control(project_id: str = "default") -> dict[str, Any]:
        service.repository.assert_project(project_id)
        return await service.mission_control.summary(project_id=project_id)

    async def capability_plan(
        capability_kind: str,
        task_type: str,
        project_id: str = "default",
        prompt: str = "",
        modality: str = "text",
        required_capabilities: list[str] | None = None,
        preferred_providers: list[str] | None = None,
        tags: list[str] | None = None,
        max_cost_usd: float | None = None,
        require_free: bool = False,
        sandboxed_tools: bool = False,
        gpu_free_vram_mib: int = 0,
        hardware_profile: str = "",
        candidate_id: str = "",
        parameters: dict[str, Any] | None = None,
        task: Any = None,
    ) -> dict[str, Any]:
        resolved_project = str(getattr(task, "project_id", None) or project_id or "default")
        service.repository.assert_project(resolved_project)
        request = CapabilityRequest(
            capability_kind=CapabilityKind(capability_kind),
            task_type=task_type,
            project_id=resolved_project,
            run_id=str(getattr(task, "metadata", {}).get("operations_run_id", "") or ""),
            prompt=prompt,
            modality=modality,
            required_capabilities=list(required_capabilities or []),
            preferred_providers=list(preferred_providers or []),
            tags=list(tags or []),
            max_cost_usd=max_cost_usd,
            allow_live=False,
            require_free=require_free,
            sandboxed_tools=sandboxed_tools,
            gpu_free_vram_mib=gpu_free_vram_mib,
            hardware_profile=hardware_profile,
            candidate_id=candidate_id,
            parameters=dict(parameters or {}),
        )
        return (await service.capabilities.plan(request)).to_dict()

    async def active_learning_assets(
        project_id: str = "default",
        organization_id: str = "",
    ) -> list[dict[str, Any]]:
        service.repository.assert_project(project_id)
        assets = await service.learning.list_active(
            project_id=project_id,
            organization_id=organization_id,
        )
        return [item.to_dict() for item in assets]

    return [
        ToolDefinition(
            name="operations_mission_control",
            description=(
                "Inspect goal, run, outcome-gate, outbox, approval, and learning alerts "
                "for one OpenOPC project without invoking a model."
            ),
            parameters={
                "type": "object",
                "properties": {"project_id": {"type": "string", "default": "default"}},
            },
            func=mission_control,
            category="operations",
            concurrency_safe=True,
            read_only=True,
            persist_large_results=False,
            max_result_chars=16_000,
        ),
        ToolDefinition(
            name="operations_capability_plan",
            description=(
                "Plan a dry-run LLM, external-agent, or NU resource route using actual "
                "catalog/availability data and record the decision; never executes live."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "capability_kind": {
                        "type": "string",
                        "enum": [item.value for item in CapabilityKind],
                    },
                    "task_type": {"type": "string"},
                    "project_id": {"type": "string", "default": "default"},
                    "prompt": {"type": "string"},
                    "modality": {"type": "string", "default": "text"},
                    "required_capabilities": {"type": "array", "items": {"type": "string"}},
                    "preferred_providers": {"type": "array", "items": {"type": "string"}},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "max_cost_usd": {"type": "number", "minimum": 0},
                    "require_free": {"type": "boolean", "default": False},
                    "sandboxed_tools": {"type": "boolean", "default": False},
                    "gpu_free_vram_mib": {"type": "integer", "minimum": 0},
                    "hardware_profile": {"type": "string"},
                    "candidate_id": {"type": "string"},
                    "parameters": {"type": "object", "additionalProperties": True},
                },
                "required": ["capability_kind", "task_type"],
            },
            func=capability_plan,
            category="operations",
            concurrency_safe=False,
            read_only=False,
            persist_large_results=False,
            max_result_chars=18_000,
        ),
        ToolDefinition(
            name="operations_active_learning",
            description="List only promoted, non-expired learning assets for a project.",
            parameters={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "default": "default"},
                    "organization_id": {"type": "string"},
                },
            },
            func=active_learning_assets,
            category="operations",
            concurrency_safe=True,
            read_only=True,
            persist_large_results=False,
            max_result_chars=12_000,
        ),
    ]
