"""OpenOPC tool definitions for policy-gated NU resource generation."""

from __future__ import annotations

import asyncio
from typing import Any

from opc.integrations.nu_resource_gen import NUResourceGenBridge
from opc.layer4_tools.registry import ToolDefinition


def create_nu_resource_gen_tools(bridge: NUResourceGenBridge) -> list[ToolDefinition]:
    if not bridge.enabled:
        return []

    async def list_candidates(
        candidate_id: str | None = None,
        provider: str | None = None,
        category: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            bridge.list_candidates,
            candidate_id=candidate_id,
            provider=provider,
            category=category,
            limit=limit,
        )

    async def health(detail: bool = True) -> dict[str, Any]:
        return await asyncio.to_thread(bridge.health, detail=detail)

    async def plan(
        candidate_id: str,
        prompt: str,
        params: dict[str, Any] | None = None,
        media: dict[str, Any] | None = None,
        task_type: str | None = None,
        quality_preset: str | None = None,
        task: Any = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            bridge.plan,
            candidate_id=candidate_id,
            prompt=prompt,
            params=params,
            media=media,
            task_type=task_type,
            quality_preset=quality_preset,
            task=task,
        )

    async def generate(
        candidate_id: str,
        prompt: str,
        params: dict[str, Any] | None = None,
        media: dict[str, Any] | None = None,
        task_type: str | None = None,
        quality_preset: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 2,
        confirm_live: bool = False,
        task: Any = None,
        on_progress: Any = None,
    ) -> dict[str, Any]:
        if on_progress:
            await on_progress(f"[NU Resource Gen] starting candidate={candidate_id}")
        result = await asyncio.to_thread(
            bridge.generate,
            candidate_id=candidate_id,
            prompt=prompt,
            params=params,
            media=media,
            task_type=task_type,
            quality_preset=quality_preset,
            timeout=timeout,
            max_retries=max_retries,
            confirm_live=confirm_live,
            task=task,
        )
        if on_progress:
            await on_progress(f"[NU Resource Gen] completed candidate={candidate_id}")
        return result

    request_properties = {
        "candidate_id": {"type": "string"},
        "prompt": {"type": "string"},
        "params": {"type": "object", "additionalProperties": True},
        "media": {"type": "object", "additionalProperties": True},
        "task_type": {"type": "string"},
        "quality_preset": {"type": "string"},
    }

    return [
        ToolDefinition(
            name="nu_resource_candidates",
            description="List or inspect NU Resource Gen candidates without provider calls.",
            parameters={
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "provider": {"type": "string"},
                    "category": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
            func=list_candidates,
            category="resource_generation",
            concurrency_safe=True,
            read_only=True,
            persist_large_results=False,
            max_result_chars=16_000,
        ),
        ToolDefinition(
            name="nu_resource_health",
            description="Check configured NU Resource Gen provider credentials without generation.",
            parameters={
                "type": "object",
                "properties": {"detail": {"type": "boolean", "default": True}},
            },
            func=health,
            category="resource_generation",
            concurrency_safe=True,
            read_only=True,
            persist_large_results=False,
            max_result_chars=12_000,
        ),
        ToolDefinition(
            name="nu_resource_plan",
            description=(
                "Create a dry-run NU resource request, evaluate billing/identity/publication "
                "policy, and record a non-success ledger row without contacting a provider."
            ),
            parameters={
                "type": "object",
                "properties": request_properties,
                "required": ["candidate_id", "prompt"],
            },
            func=plan,
            category="resource_generation",
            concurrency_safe=False,
            read_only=False,
            persist_large_results=False,
            max_result_chars=16_000,
        ),
        ToolDefinition(
            name="nu_resource_generate",
            description=(
                "Generate a live NU resource. Disabled by default and requires candidate "
                "allowlisting, confirm_live=true, human approval, and provider policy clearance."
            ),
            parameters={
                "type": "object",
                "properties": {
                    **request_properties,
                    "timeout": {"type": "number", "minimum": 1},
                    "max_retries": {"type": "integer", "minimum": 0, "maximum": 5},
                    "confirm_live": {"type": "boolean", "const": True},
                },
                "required": ["candidate_id", "prompt", "confirm_live"],
            },
            func=generate,
            category="resource_generation",
            requires_confirmation=True,
            concurrency_safe=False,
            read_only=False,
            persist_large_results=True,
            max_result_chars=20_000,
        ),
    ]
