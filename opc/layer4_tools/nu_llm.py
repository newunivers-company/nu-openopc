"""Read-only NU LLM routing diagnostics for OpenOPC agents."""

from __future__ import annotations

import asyncio
from typing import Any

from opc.layer4_tools.registry import ToolDefinition


def create_nu_llm_tools(llm: Any) -> list[ToolDefinition]:
    bridge = getattr(llm, "nu_router", None)
    if bridge is None or not bool(getattr(bridge, "enabled", False)):
        return []

    async def route_diagnostics(
        workload: str = "agentic_tools",
        tags: list[str] | None = None,
        sandboxed_tools: bool | None = None,
        gpu_free_vram_mib: int | None = None,
        hardware_profile: str | None = None,
        include_status: bool = False,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            bridge.diagnostics,
            workload=workload,
            tags=tags,
            sandboxed_tools=sandboxed_tools,
            gpu_free_vram_mib=gpu_free_vram_mib,
            hardware_profile=hardware_profile,
            include_status=include_status,
        )

    return [
        ToolDefinition(
            name="nu_llm_route_diagnostics",
            description=(
                "Plan an NU LLM provider route without generating text. Returns the matched "
                "profile, initial/benchmark/final order, exclusions, and optional health checks."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workload": {
                        "type": "string",
                        "description": (
                            "Routing workload such as dialogue, structured_output, coding, "
                            "agentic_tools, summarization, or creative_writing."
                        ),
                        "default": "agentic_tools",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional policy/profile tags such as korean or json.",
                    },
                    "sandboxed_tools": {
                        "type": "boolean",
                        "description": "Whether any routed tool execution is actually sandboxed.",
                    },
                    "gpu_free_vram_mib": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Explicit free VRAM for deterministic local-model policy checks.",
                    },
                    "hardware_profile": {
                        "type": "string",
                        "description": "Optional hardware profile such as rtx5090.",
                    },
                    "include_status": {
                        "type": "boolean",
                        "default": False,
                        "description": "Probe provider availability without generating text.",
                    },
                },
            },
            func=route_diagnostics,
            category="routing",
            concurrency_safe=True,
            read_only=True,
            persist_large_results=False,
            max_result_chars=12_000,
        )
    ]
