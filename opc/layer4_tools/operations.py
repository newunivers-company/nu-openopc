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

    async def pinned_learning(
        run_id: str,
        role_id: str = "",
        employee_id: str = "",
    ) -> dict[str, Any]:
        return await service.learning_activations.render_for_run(
            run_id,
            role_id=role_id,
            employee_id=employee_id,
        )

    async def action_plan(
        kind: str,
        target_id: str,
        reason: str,
        project_id: str = "default",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return await service.operator_actions.plan(
            project_id=project_id,
            kind=kind,
            target_id=target_id,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    async def action_execute(
        action_id: str,
        plan_digest: str,
        operator_id: str,
        confirmed: bool,
        project_id: str = "default",
    ) -> dict[str, Any]:
        return await service.operator_actions.execute(
            project_id=project_id,
            action_id=action_id,
            plan_digest=plan_digest,
            operator_id=operator_id,
            confirmed=confirmed,
        )

    async def skill_assembly(
        goal: str,
        roles: list[dict[str, Any]],
        required_capabilities: list[str] | None = None,
        project_id: str = "default",
        max_additions_per_role: int = 4,
    ) -> dict[str, Any]:
        service.repository.assert_project(project_id)
        return service.skill_assembly.recommend(
            goal=goal,
            roles=roles,
            required_capabilities=required_capabilities,
            project_id=project_id,
            max_additions_per_role=max_additions_per_role,
        )

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
        ToolDefinition(
            name="operations_pinned_learning",
            description=(
                "Inspect the immutable, content-addressed self-grown assets pinned "
                "to a run, optionally filtered for one role and employee."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "role_id": {"type": "string"},
                    "employee_id": {"type": "string"},
                },
                "required": ["run_id"],
            },
            func=pinned_learning,
            category="operations",
            concurrency_safe=True,
            read_only=True,
            persist_large_results=False,
            max_result_chars=16_000,
        ),
        ToolDefinition(
            name="operations_action_plan",
            description=(
                "Create a short-lived, project-scoped Mission Control action plan. "
                "This does not perform the recovery or learning mutation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "default": "default"},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "recover_run",
                            "replay_dead_letter",
                            "rollback_learning_asset",
                            "retire_learning_asset",
                        ],
                    },
                    "target_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["kind", "target_id", "reason"],
            },
            func=action_plan,
            category="operations",
            concurrency_safe=False,
            read_only=False,
            persist_large_results=False,
            max_result_chars=12_000,
        ),
        ToolDefinition(
            name="operations_action_execute",
            description=(
                "Execute one exact Mission Control plan after the operator confirms "
                "its SHA-256 digest. The action is single-use and durably audited."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "default": "default"},
                    "action_id": {"type": "string"},
                    "plan_digest": {"type": "string"},
                    "operator_id": {"type": "string"},
                    "confirmed": {"type": "boolean"},
                },
                "required": [
                    "action_id",
                    "plan_digest",
                    "operator_id",
                    "confirmed",
                ],
            },
            func=action_execute,
            category="operations",
            requires_confirmation=True,
            concurrency_safe=False,
            read_only=False,
            persist_large_results=False,
            max_result_chars=18_000,
        ),
        ToolDefinition(
            name="operations_skill_assembly",
            description=(
                "Recommend installed, content-addressed skills for each role from "
                "a goal and explicit capability requirements. Never installs or "
                "changes role configuration."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "default": "default"},
                    "goal": {"type": "string"},
                    "roles": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role_id": {"type": "string"},
                                "name": {"type": "string"},
                                "responsibility": {"type": "string"},
                                "capabilities": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "skill_refs": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["role_id"],
                        },
                    },
                    "required_capabilities": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "max_additions_per_role": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 12,
                        "default": 4,
                    },
                },
                "required": ["goal", "roles"],
            },
            func=skill_assembly,
            category="operations",
            concurrency_safe=True,
            read_only=True,
            persist_large_results=False,
            max_result_chars=24_000,
        ),
    ]
