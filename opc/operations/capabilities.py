"""Unified, policy-aware planning across LLM, agent, and resource routes."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any, Awaitable, Callable, Mapping, Sequence

from opc.operations.models import (
    CapabilityAttempt,
    CapabilityKind,
    CapabilityRequest,
    CapabilityRoute,
)
from opc.operations.repository import OperationsRepository


CapabilityExecutor = Callable[[CapabilityRoute, CapabilityRequest], Awaitable[Any] | Any]


class CapabilityBrokerError(RuntimeError):
    """Raised when no policy-compliant capability route can be produced."""


class UnifiedCapabilityBroker:
    """Produce auditable routes without bypassing provider execution policy."""

    def __init__(
        self,
        repository: OperationsRepository,
        *,
        llm_router: Any | None = None,
        resource_bridge: Any | None = None,
        adapter_registry: Any | None = None,
        default_llm_model: str = "",
        default_llm_api_base: str = "",
        default_llm_credential_ready: bool = False,
        default_llm_transport_ready: bool = False,
    ) -> None:
        self.repository = repository
        self.llm_router = llm_router
        self.resource_bridge = resource_bridge
        self.adapter_registry = adapter_registry
        self.default_llm_model = str(default_llm_model or "")
        self.default_llm_api_base = str(default_llm_api_base or "")
        self.default_llm_credential_ready = bool(default_llm_credential_ready)
        self.default_llm_transport_ready = bool(default_llm_transport_ready)

    def bind_adapter_registry(self, registry: Any | None) -> None:
        self.adapter_registry = registry

    async def plan(
        self,
        request: CapabilityRequest | Mapping[str, Any],
        *,
        record_attempt: bool = True,
    ) -> CapabilityRoute:
        parsed = request if isinstance(request, CapabilityRequest) else CapabilityRequest.from_dict(request)
        parsed.validate()
        if parsed.capability_kind == CapabilityKind.LLM:
            route = await self._plan_llm(parsed)
        elif parsed.capability_kind == CapabilityKind.EXTERNAL_AGENT:
            route = await self._plan_external_agent(parsed)
        elif parsed.capability_kind == CapabilityKind.RESOURCE:
            route = await self._plan_resource(parsed)
        else:
            raise CapabilityBrokerError(f"unsupported capability kind: {parsed.capability_kind}")
        if record_attempt:
            await self.record_attempt(
                parsed,
                route,
                status="planned" if route.allowed else "blocked",
                metadata={"mode": route.mode, "blockers": list(route.blockers)},
            )
        return route

    async def execute(
        self,
        request: CapabilityRequest | Mapping[str, Any],
        executor: CapabilityExecutor,
    ) -> tuple[CapabilityRoute, Any]:
        """Execute only an explicitly live-authorized route through an injected owner."""
        parsed = request if isinstance(request, CapabilityRequest) else CapabilityRequest.from_dict(request)
        if not parsed.allow_live:
            raise PermissionError("capability execution requires allow_live=true")
        route = await self.plan(parsed, record_attempt=False)
        if not route.allowed or route.mode not in {"live", "delegate"}:
            blockers = "; ".join(route.blockers) or f"route mode is {route.mode}"
            await self.record_attempt(parsed, route, status="blocked", error=blockers)
            raise PermissionError(blockers)
        started = time.monotonic()
        try:
            result = executor(route, parsed)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            await self.record_attempt(
                parsed,
                route,
                status="failed",
                latency_ms=(time.monotonic() - started) * 1000,
                error=str(exc),
            )
            raise
        cost = _result_cost(result)
        await self.record_attempt(
            parsed,
            route,
            status="completed",
            latency_ms=(time.monotonic() - started) * 1000,
            cost_usd=cost,
        )
        return route, result

    async def record_attempt(
        self,
        request: CapabilityRequest,
        route: CapabilityRoute,
        *,
        status: str,
        latency_ms: float = 0.0,
        cost_usd: float | None = None,
        error: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> CapabilityAttempt:
        attempt = CapabilityAttempt(
            request_id=request.request_id,
            route_id=route.route_id,
            capability_kind=request.capability_kind,
            project_id=request.project_id,
            run_id=request.run_id,
            provider=route.provider,
            candidate_id=route.candidate_id,
            status=str(status or "unknown"),
            latency_ms=max(0.0, float(latency_ms)),
            cost_usd=cost_usd,
            error=str(error or "")[:4000],
            metadata={
                "task_type": request.task_type,
                "modality": request.modality,
                **dict(metadata or {}),
            },
        )
        return await self.repository.save_capability_attempt(attempt)

    async def _plan_llm(self, request: CapabilityRequest) -> CapabilityRoute:
        has_tools = (
            request.task_type.strip().lower() in {"agentic_tools", "tool_use", "tools"}
            or "tool_use" in {item.strip().lower() for item in request.required_capabilities}
        )
        workload = "agentic_tools" if has_tools else str(request.task_type or "dialogue").strip()
        blockers: list[str] = []
        if has_tools and not request.sandboxed_tools:
            blockers.append("tool-use LLM routing requires sandboxed_tools=true")
        diagnostics: dict[str, Any] = {}
        targets: list[dict[str, Any]] = []
        if self.llm_router is not None and bool(getattr(self.llm_router, "enabled", False)):
            diagnostics = await asyncio.to_thread(
                self.llm_router.diagnostics,
                workload=workload,
                tags=request.tags,
                sandboxed_tools=request.sandboxed_tools,
                gpu_free_vram_mib=request.gpu_free_vram_mib,
                hardware_profile=request.hardware_profile or None,
                include_status=True,
            )
            if diagnostics.get("available"):
                try:
                    raw_targets = self.llm_router.targets(
                        task_type=request.task_type,
                        has_tools=has_tools,
                    )
                    targets = [item.safe_dict() for item in raw_targets]
                except Exception as exc:
                    diagnostics["target_error"] = str(exc)
            else:
                blockers.append(str(diagnostics.get("error") or "NU LLM router unavailable"))

        ordered = [str(item) for item in diagnostics.get("final_order", []) or []]
        target_by_provider = {
            str(item.get("provider", "")): item
            for item in targets
            if str(item.get("provider", "")).strip()
        }
        usable_order = [item for item in ordered if item in target_by_provider]
        usable_order.extend(
            item for item in target_by_provider if item not in set(usable_order)
        )
        hard_free = request.require_free or request.max_cost_usd == 0
        free_order = [
            item
            for item in usable_order
            if _is_local_llm(item, target_by_provider[item])
        ]
        if hard_free and free_order:
            selection_order = free_order
        elif request.local_first:
            selection_order = [*free_order, *(item for item in usable_order if item not in free_order)]
        else:
            selection_order = usable_order
        selected_provider = _preferred_value(selection_order, request.preferred_providers)
        selected_target = target_by_provider.get(selected_provider)
        if not selected_provider and self.default_llm_model:
            selected_provider = "openopc"
            selected_target = {
                "provider": selected_provider,
                "model": self.default_llm_model,
                "api_base": self.default_llm_api_base,
                "credential_configured": self.default_llm_credential_ready,
                "transport_ready": self.default_llm_transport_ready,
            }
            diagnostics.setdefault("fallback", "OpenOPC default LLM transport")
        if not selected_provider:
            blockers.append("no usable LLM provider was found")

        local = _is_local_llm(selected_provider, selected_target or {})
        if request.require_free and not local:
            blockers.append("require_free=true but the selected LLM route is not verified local/free")
        estimated_cost = 0.0 if local else _known_usd_cost(selected_target or {})
        if request.max_cost_usd is not None:
            if estimated_cost is None:
                blockers.append("LLM route cost is unknown under an explicit cost ceiling")
            elif estimated_cost > request.max_cost_usd:
                blockers.append(
                    f"LLM route cost {estimated_cost:.6f} exceeds ceiling "
                    f"{request.max_cost_usd:.6f} USD"
                )
        model = str((selected_target or {}).get("model", ""))
        credential_ready = bool((selected_target or {}).get("credential_configured", False))
        transport_ready = bool((selected_target or {}).get("transport_ready", False))
        if request.allow_live and not transport_ready:
            blockers.append("selected LLM route is not transport-ready for live execution")
        plan_allowed = not blockers
        alternatives = [
            {
                "provider": item,
                "model": str(target_by_provider[item].get("model", "")),
            }
            for item in usable_order
            if item != selected_provider
        ]
        return CapabilityRoute(
            request_id=request.request_id,
            capability_kind=request.capability_kind,
            provider=selected_provider,
            candidate_id=model or selected_provider,
            model=model,
            mode="live" if request.allow_live else "dry_run",
            allowed=plan_allowed,
            reason=(
                "selected from NU route diagnostics"
                if diagnostics.get("available") and selected_provider != "openopc"
                else "selected OpenOPC default LLM transport"
            ),
            blockers=_dedupe(blockers),
            alternatives=alternatives,
            diagnostics={
                **diagnostics,
                "targets": targets,
                "usable_order": usable_order,
                "selection_order": selection_order,
                "unusable_ordered_providers": [
                    item for item in ordered if item not in target_by_provider
                ],
                "workload": workload,
                "sandboxed_tools": request.sandboxed_tools,
                "gpu_free_vram_mib": request.gpu_free_vram_mib,
            },
            readiness={
                "plan_allowed": plan_allowed,
                "credential_ready": credential_ready,
                "transport_ready": transport_ready,
                "live_allowed": bool(request.allow_live and plan_allowed and transport_ready),
            },
            estimated_cost_usd=estimated_cost,
        )

    async def _plan_external_agent(self, request: CapabilityRequest) -> CapabilityRoute:
        blockers: list[str] = []
        profiles: list[dict[str, Any]] = []
        if self.adapter_registry is None:
            available: list[str] = []
            blockers.append("external agent registry is unavailable")
        else:
            available = [str(item) for item in self.adapter_registry.list_available()]
            describe = getattr(self.adapter_registry, "describe_available", None)
            if callable(describe):
                profiles = [dict(item) for item in describe()]
        profile_by_name = {
            str(
                item.get("agent_type")
                or item.get("agent")
                or item.get("name")
                or item.get("id")
                or ""
            ): item
            for item in profiles
        }
        required = {item.strip().lower() for item in request.required_capabilities if item.strip()}
        compatible: list[str] = []
        for name in available:
            profile = profile_by_name.get(name, {})
            haystack = json.dumps(profile, ensure_ascii=False, sort_keys=True).lower()
            if required and not all(item in haystack for item in required):
                continue
            compatible.append(name)
        selected = _preferred_value(compatible, request.preferred_providers)
        if not selected and compatible:
            selected = compatible[0]
        if not selected:
            blockers.append("no available external agent satisfies the requested capabilities")
        if request.require_free:
            profile = profile_by_name.get(selected, {})
            if not bool(profile.get("free") or profile.get("local")):
                blockers.append("selected external agent is not explicitly marked local/free")
        selected_profile = profile_by_name.get(selected, {})
        health = dict(selected_profile.get("health", {}) or {})
        credential_ready = bool(health.get("credential_ready", selected in compatible))
        transport_ready = bool(health.get("transport_ready", selected in compatible))
        if request.allow_live and selected and not transport_ready:
            blockers.append("selected external agent is not transport-ready")
        estimated_cost = _known_agent_cost(selected_profile)
        if request.max_cost_usd is not None:
            if estimated_cost is None:
                blockers.append("external-agent cost is unknown under an explicit cost ceiling")
            elif estimated_cost > request.max_cost_usd:
                blockers.append(
                    f"external-agent cost {estimated_cost:.6f} exceeds ceiling "
                    f"{request.max_cost_usd:.6f} USD"
                )
        alternatives = [
            {"agent": name, "profile": profile_by_name.get(name, {})}
            for name in compatible
            if name != selected
        ]
        plan_allowed = not blockers
        return CapabilityRoute(
            request_id=request.request_id,
            capability_kind=request.capability_kind,
            provider=selected,
            candidate_id=selected,
            mode="delegate" if request.allow_live else "dry_run",
            allowed=plan_allowed,
            reason="selected from currently available OpenOPC external-agent adapters",
            blockers=_dedupe(blockers),
            alternatives=alternatives,
            diagnostics={"available": available, "compatible": compatible},
            readiness={
                "plan_allowed": plan_allowed,
                "credential_ready": credential_ready,
                "transport_ready": transport_ready,
                "live_allowed": bool(request.allow_live and plan_allowed and transport_ready),
            },
            estimated_cost_usd=estimated_cost,
        )

    async def _plan_resource(self, request: CapabilityRequest) -> CapabilityRoute:
        blockers: list[str] = []
        if self.resource_bridge is None or not bool(getattr(self.resource_bridge, "enabled", False)):
            return CapabilityRoute(
                request_id=request.request_id,
                capability_kind=request.capability_kind,
                provider="",
                candidate_id="",
                mode="dry_run",
                allowed=False,
                reason="NU Resource Gen bridge is unavailable",
                blockers=["NU Resource Gen is disabled or unavailable"],
            )
        try:
            if request.candidate_id:
                catalog = await asyncio.to_thread(
                    self.resource_bridge.list_candidates,
                    candidate_id=request.candidate_id,
                    limit=1,
                )
            else:
                catalog = await asyncio.to_thread(
                    self.resource_bridge.list_candidates,
                    category=request.task_type or None,
                    limit=None,
                )
                if not catalog.get("candidates"):
                    catalog = await asyncio.to_thread(
                        self.resource_bridge.list_candidates,
                        limit=None,
                    )
        except Exception as exc:
            return CapabilityRoute(
                request_id=request.request_id,
                capability_kind=request.capability_kind,
                provider="",
                candidate_id=request.candidate_id,
                mode="dry_run",
                allowed=False,
                reason="NU Resource Gen catalog lookup failed",
                blockers=[str(exc)],
            )
        candidates = [
            dict(item)
            for item in catalog.get("candidates", []) or []
            if isinstance(item, Mapping)
        ]
        candidates = [item for item in candidates if _resource_compatible(item, request)]
        candidates.sort(key=lambda item: _resource_rank(item, request))
        selected = candidates[0] if candidates else {}
        candidate_id = str(selected.get("candidate_id", ""))
        provider = str(selected.get("provider", ""))
        if not selected:
            blockers.append("no catalog candidate satisfies the resource request")
        verified_free = _verified_free_resource(selected)
        cost = _known_cost(selected)
        if request.require_free and not verified_free:
            blockers.append("require_free=true but candidate cost is not verified free/local")
        if request.max_cost_usd is not None:
            if cost is None:
                blockers.append("candidate cost is unknown under an explicit cost ceiling")
            elif cost > request.max_cost_usd:
                blockers.append(
                    f"candidate cost {cost:.6f} exceeds ceiling {request.max_cost_usd:.6f} USD"
                )
        if selected.get("deprecated"):
            blockers.append("selected resource candidate is deprecated")
        if selected.get("simulation_only") and request.allow_live:
            blockers.append("simulation-only candidate cannot execute live")

        plan: dict[str, Any] = {}
        if selected and not blockers:
            try:
                plan = await asyncio.to_thread(
                    self.resource_bridge.plan,
                    candidate_id=candidate_id,
                    prompt=request.prompt,
                    params=request.parameters,
                    media=dict(request.metadata.get("media", {}) or {}),
                    task_type=request.task_type or None,
                    quality_preset=str(request.metadata.get("quality_preset", "") or "") or None,
                )
            except Exception as exc:
                blockers.append(f"resource dry-run policy failed: {exc}")
        mode = "dry_run"
        if request.allow_live:
            mode = "live"
            status = self.resource_bridge.status()
            if not status.get("allow_live"):
                blockers.append("NU Resource Gen live execution is disabled")
            if candidate_id not in set(status.get("allowed_live_candidates", []) or []):
                blockers.append(f"candidate {candidate_id!r} is not live-allowlisted")
            if not bool(request.metadata.get("confirm_live", False)):
                blockers.append("live resource route requires confirm_live=true")
            policy = dict(plan.get("policy", {}) or {})
            if bool(policy.get("blocked", False)):
                policy_blockers = [
                    str(item)
                    for item in policy.get("blockers", []) or []
                    if str(item).strip()
                ]
                blockers.append(
                    "NU Resource Gen execution policy blocked the live route"
                    + (f": {', '.join(policy_blockers)}" if policy_blockers else "")
                )
        alternatives = [
            _compact_resource(item)
            for item in candidates[1:10]
        ]
        provider_readiness: dict[str, Any] = {}
        readiness_probe = getattr(self.resource_bridge, "provider_readiness", None)
        if selected and callable(readiness_probe):
            try:
                provider_readiness = await asyncio.to_thread(readiness_probe, provider)
            except Exception as exc:
                provider_readiness = {
                    "credential_ready": False,
                    "transport_ready": False,
                    "detail": f"readiness probe failed: {type(exc).__name__}: {exc}"[:500],
                }
        credential_ready = bool(
            provider_readiness.get(
                "credential_ready",
                selected.get("credentials_configured", False)
                or provider.strip().lower() in {"local", "comfyui"},
            )
        )
        transport_ready = bool(provider_readiness.get("transport_ready", credential_ready))
        if request.allow_live and selected:
            if not credential_ready:
                blockers.append("selected resource provider has no configured credentials")
            if not transport_ready:
                blockers.append("selected resource provider is not transport-ready")
        plan_allowed = not blockers
        return CapabilityRoute(
            request_id=request.request_id,
            capability_kind=request.capability_kind,
            provider=provider,
            candidate_id=candidate_id,
            model=str(selected.get("model", "")),
            mode=mode,
            allowed=plan_allowed,
            reason=(
                "selected from the installed NU Resource Gen catalog using local/free-first policy"
                if request.local_first or request.require_free
                else "selected from the installed NU Resource Gen catalog"
            ),
            blockers=_dedupe(blockers),
            alternatives=alternatives,
            diagnostics={
                "catalog_count": catalog.get("count", len(candidates)),
                "compatible_count": len(candidates),
                "selected": _compact_resource(selected),
                "dry_run_plan": plan,
                "provider_readiness": provider_readiness,
            },
            readiness={
                "plan_allowed": plan_allowed,
                "credential_ready": credential_ready,
                "transport_ready": transport_ready,
                "live_allowed": bool(request.allow_live and plan_allowed and transport_ready),
            },
            estimated_cost_usd=cost,
        )


def _preferred_value(values: Sequence[str], preferred: Sequence[str]) -> str:
    value_set = set(values)
    return next((item for item in preferred if item in value_set), values[0] if values else "")


def _is_local_llm(provider: str, target: Mapping[str, Any]) -> bool:
    normalized = provider.lower()
    api_base = str(target.get("api_base", "")).lower()
    return (
        any(token in normalized for token in ("local", "ollama", "vllm"))
        or api_base.startswith("http://127.0.0.1")
        or api_base.startswith("http://localhost")
    )


def _resource_compatible(candidate: Mapping[str, Any], request: CapabilityRequest) -> bool:
    if request.candidate_id and str(candidate.get("candidate_id", "")) != request.candidate_id:
        return False
    if request.preferred_providers and str(candidate.get("provider", "")) not in request.preferred_providers:
        return False
    if request.task_type:
        category = str(candidate.get("category", ""))
        supported = {str(item) for item in candidate.get("supported_task_types", []) or []}
        if category != request.task_type and request.task_type not in supported:
            return False
    required = {item.strip().lower() for item in request.required_capabilities if item.strip()}
    if required:
        haystack = json.dumps(candidate, ensure_ascii=False, sort_keys=True).lower()
        if not all(item in haystack for item in required):
            return False
    return True


def _resource_rank(candidate: Mapping[str, Any], request: CapabilityRequest) -> tuple[Any, ...]:
    provider = str(candidate.get("provider", "")).lower()
    preferred_index = (
        request.preferred_providers.index(provider)
        if provider in request.preferred_providers
        else len(request.preferred_providers)
    )
    return (
        bool(candidate.get("deprecated", False)),
        bool(candidate.get("simulation_only", False)),
        (
            0
            if not (request.local_first or request.require_free)
            or _verified_free_resource(candidate)
            else 1
        ),
        (
            0
            if not request.local_first
            or any(token in provider for token in ("local", "comfyui"))
            else 1
        ),
        preferred_index,
        _known_cost(candidate) if _known_cost(candidate) is not None else float("inf"),
        str(candidate.get("candidate_id", "")),
    )


def _verified_free_resource(candidate: Mapping[str, Any]) -> bool:
    cost = _known_cost(candidate)
    unit = str(candidate.get("cost_unit", "")).strip().lower()
    provider = str(candidate.get("provider", "")).strip().lower()
    return cost == 0.0 and (
        unit in {"local", "usd", "request", "image", "second", "minute"}
        or provider in {"local", "comfyui"}
    )


def _known_cost(candidate: Mapping[str, Any]) -> float | None:
    value = candidate.get("cost")
    unit = str(candidate.get("cost_unit", "")).strip().lower()
    if value is None or unit in {"", "unknown"}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _known_usd_cost(value: Mapping[str, Any]) -> float | None:
    raw = value.get("estimated_cost_usd")
    if raw is None and str(value.get("cost_unit", "usd") or "usd").lower() == "usd":
        raw = value.get("cost")
    if raw is None:
        return None
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _known_agent_cost(profile: Mapping[str, Any]) -> float | None:
    if bool(profile.get("free", False) or profile.get("local", False)):
        return 0.0
    return _known_usd_cost(profile)


def _compact_resource(candidate: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "candidate_id",
        "provider",
        "model",
        "category",
        "cost",
        "cost_unit",
        "status",
        "credentials_configured",
        "simulation_only",
        "deprecated",
    )
    return {key: candidate.get(key) for key in keys if key in candidate}


def _result_cost(result: Any) -> float | None:
    if isinstance(result, Mapping):
        value = result.get("cost_usd", result.get("cost"))
        unit = str(result.get("cost_unit", "usd") or "usd").lower()
        if value is not None and unit == "usd":
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                return None
    return None


def _dedupe(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in values if str(item).strip()))
