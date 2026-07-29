"""Unified, policy-aware planning across LLM, agent, and resource routes."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
import time
from datetime import timedelta
from typing import Any, Awaitable, Callable, Mapping, Sequence

from opc.operations.models import (
    CapabilityAttempt,
    CapabilityKind,
    CapabilityRequest,
    CapabilityRoute,
    ProviderUsageEvent,
    RouteExecutionContract,
    utc_now,
)
from opc.operations.repository import (
    OperationsRepository,
    ProviderCallQuotaExceeded,
)


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
        execution_contract_ttl_seconds: float = 300.0,
        subscription_call_limit: int = 200,
        subscription_window_seconds: int = 86_400,
        subscription_providers: Sequence[str] = ("codex", "claude", "grok"),
    ) -> None:
        self.repository = repository
        self.llm_router = llm_router
        self.resource_bridge = resource_bridge
        self.adapter_registry = adapter_registry
        self.default_llm_model = str(default_llm_model or "")
        self.default_llm_api_base = str(default_llm_api_base or "")
        self.default_llm_credential_ready = bool(default_llm_credential_ready)
        self.default_llm_transport_ready = bool(default_llm_transport_ready)
        self.execution_contract_ttl_seconds = max(1.0, float(execution_contract_ttl_seconds))
        self.subscription_call_limit = max(0, int(subscription_call_limit))
        self.subscription_window_seconds = max(1, int(subscription_window_seconds))
        self.subscription_providers = tuple(
            str(item).strip().lower()
            for item in subscription_providers
            if str(item).strip()
        )

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
        parsed, route, contract, started = await self.begin_execution(request)
        try:
            result = executor(route, parsed)
            if inspect.isawaitable(result):
                result = await result
        except BaseException as exc:
            await self.fail_execution(parsed, route, contract, started, exc)
            raise
        return await self.complete_execution(parsed, route, contract, started, result)

    async def begin_execution(
        self,
        request: CapabilityRequest | Mapping[str, Any],
    ) -> tuple[CapabilityRequest, CapabilityRoute, RouteExecutionContract, float]:
        """Persist and start one live execution contract before provider I/O."""

        parsed = request if isinstance(request, CapabilityRequest) else CapabilityRequest.from_dict(request)
        if not parsed.allow_live:
            raise PermissionError("capability execution requires allow_live=true")
        route, contract = await self.plan_execution(parsed)
        if not route.allowed or route.mode not in {"live", "delegate"}:
            blockers = "; ".join(route.blockers) or f"route mode is {route.mode}"
            raise PermissionError(blockers)
        if contract.expires_at and utc_now() >= contract.expires_at:
            contract.status = "expired"
            contract.completed_at = utc_now()
            contract.error = "execution contract expired before execution"
            await self.repository.save_route_execution_contract(contract)
            raise PermissionError(contract.error)
        if self._subscription_quota_applies(route):
            try:
                reservation = await self.repository.reserve_provider_call(
                    contract_id=contract.contract_id,
                    request_id=parsed.request_id,
                    project_id=parsed.project_id,
                    provider=route.provider,
                    model=route.model,
                    limit=self.subscription_call_limit,
                    window_seconds=self.subscription_window_seconds,
                )
            except ProviderCallQuotaExceeded as exc:
                contract.status = "quota_exceeded"
                contract.error = str(exc)
                contract.completed_at = utc_now()
                await self.repository.save_route_execution_contract(contract)
                await self.record_attempt(
                    parsed,
                    route,
                    status="quota_exceeded",
                    error=str(exc),
                    metadata={"contract_id": contract.contract_id},
                )
                raise PermissionError(str(exc)) from exc
            contract.result_metadata["subscription_call_quota"] = reservation
        contract.status = "executing"
        contract.started_at = utc_now()
        await self.repository.save_route_execution_contract(contract)
        return parsed, route, contract, time.monotonic()

    async def fail_execution(
        self,
        request: CapabilityRequest,
        route: CapabilityRoute,
        contract: RouteExecutionContract,
        started: float,
        exc: BaseException,
    ) -> None:
        """Close an executing contract after provider failure or stream cancellation."""

        detail = str(exc).strip() or type(exc).__name__
        contract.status = "failed"
        contract.error = detail[:4000]
        contract.completed_at = utc_now()
        await self.repository.save_route_execution_contract(contract)
        await self._finish_subscription_reservation(contract, status="failed")
        await self.record_attempt(
            request,
            route,
            status="failed",
            latency_ms=(time.monotonic() - started) * 1000,
            error=detail,
            metadata={"contract_id": contract.contract_id},
        )

    async def complete_execution(
        self,
        request: CapabilityRequest,
        route: CapabilityRoute,
        contract: RouteExecutionContract,
        started: float,
        result: Any,
    ) -> tuple[CapabilityRoute, Any]:
        """Persist actual route, usage evidence, and terminal contract state."""

        actual = _actual_route_identity(result, route)
        identity_error = _validate_actual_route(route, actual)
        cost = _result_cost(result)
        usage = _usage_event(request, contract, actual, result, cost_usd=cost)
        await self.repository.save_provider_usage_event(usage)
        contract.actual_provider = actual["provider"]
        contract.actual_candidate_id = actual["candidate_id"]
        contract.actual_model = actual["model"]
        contract.actual_cost_usd = cost
        contract.usage_event_id = usage.usage_event_id
        contract.completed_at = utc_now()
        quota_metadata = dict(
            contract.result_metadata.get("subscription_call_quota", {}) or {}
        )
        contract.result_metadata = _result_metadata(result)
        if quota_metadata:
            contract.result_metadata["subscription_call_quota"] = quota_metadata
        if identity_error:
            contract.status = "contract_violation"
            contract.error = identity_error
            await self.repository.save_route_execution_contract(contract)
            await self._finish_subscription_reservation(
                contract, status="contract_violation"
            )
            await self.record_attempt(
                request,
                route,
                status="contract_violation",
                latency_ms=(time.monotonic() - started) * 1000,
                cost_usd=cost,
                error=identity_error,
                metadata={
                    "contract_id": contract.contract_id,
                    "usage_event_id": usage.usage_event_id,
                },
            )
            raise CapabilityBrokerError(identity_error)
        if request.max_cost_usd is not None and cost is not None and cost > request.max_cost_usd:
            contract.status = "budget_exceeded"
            contract.error = (
                f"actual cost {cost:.6f} exceeds contract ceiling {request.max_cost_usd:.6f}"
            )
        else:
            contract.status = "completed"
        await self.repository.save_route_execution_contract(contract)
        await self._finish_subscription_reservation(
            contract, status=contract.status
        )
        await self.record_attempt(
            request,
            route,
            status=contract.status,
            latency_ms=(time.monotonic() - started) * 1000,
            cost_usd=cost,
            error=contract.error,
            metadata={
                "contract_id": contract.contract_id,
                "usage_event_id": usage.usage_event_id,
                "usage_measured": usage.measured,
                "usage_source": usage.source,
            },
        )
        if contract.status == "budget_exceeded":
            raise CapabilityBrokerError(contract.error)
        return route, result

    def _subscription_quota_applies(self, route: CapabilityRoute) -> bool:
        if self.subscription_call_limit <= 0:
            return False
        transport_kind = ""
        for target in route.diagnostics.get("targets", []) or []:
            if not isinstance(target, Mapping):
                continue
            if str(target.get("provider", "") or "") == route.provider:
                transport_kind = str(target.get("transport_kind", "") or "")
                break
        return bool(
            transport_kind == "subscription_cli"
            and _provider_matches(route.provider, self.subscription_providers)
        )

    async def _finish_subscription_reservation(
        self,
        contract: RouteExecutionContract,
        *,
        status: str,
    ) -> None:
        quota = contract.result_metadata.get("subscription_call_quota", {})
        reservation_id = (
            str(quota.get("reservation_id", "") or "")
            if isinstance(quota, Mapping)
            else ""
        )
        if reservation_id:
            await self.repository.finish_provider_call_reservation(
                reservation_id,
                status=status,
                completed_at=contract.completed_at,
            )

    async def plan_execution(
        self,
        request: CapabilityRequest | Mapping[str, Any],
    ) -> tuple[CapabilityRoute, RouteExecutionContract]:
        """Persist an immutable plan snapshot before any owner executes it."""

        parsed = request if isinstance(request, CapabilityRequest) else CapabilityRequest.from_dict(request)
        parsed.validate()
        route = await self.plan(parsed, record_attempt=False)
        fallback_order = [
            {
                "provider": route.provider,
                "candidate_id": route.candidate_id,
                "model": route.model,
            },
            *[
                {
                    "provider": str(item.get("provider", "") or ""),
                    "candidate_id": str(item.get("candidate_id", "") or ""),
                    "model": str(item.get("model", "") or ""),
                }
                for item in route.alternatives
            ],
        ]
        contract = RouteExecutionContract(
            request_id=parsed.request_id,
            route_id=route.route_id,
            capability_kind=parsed.capability_kind,
            project_id=parsed.project_id,
            run_id=parsed.run_id,
            status="planned" if route.allowed else "blocked",
            mode=route.mode,
            planned_provider=route.provider,
            planned_candidate_id=route.candidate_id,
            planned_model=route.model,
            fallback_order=[item for item in fallback_order if any(item.values())],
            readiness=dict(route.readiness),
            max_cost_usd=parsed.max_cost_usd,
            request_snapshot=parsed.to_dict(),
            route_snapshot=route.to_dict(),
            error="; ".join(route.blockers) if not route.allowed else "",
            expires_at=utc_now() + timedelta(seconds=self.execution_contract_ttl_seconds),
        )
        await self.repository.save_route_execution_contract(contract)
        await self.record_attempt(
            parsed,
            route,
            status=contract.status,
            error=contract.error,
            metadata={"contract_id": contract.contract_id, "mode": route.mode},
        )
        return route, contract

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
                        preferred_providers=request.preferred_providers,
                    )
                    targets = [item.safe_dict() for item in raw_targets]
                    if has_tools:
                        tool_incompatible = [
                            str(item.get("provider", "") or "")
                            for item in targets
                            if not _target_supports_tools(item)
                        ]
                        if tool_incompatible:
                            diagnostics["tool_incompatible_targets"] = (
                                tool_incompatible
                            )
                        targets = [
                            item for item in targets if _target_supports_tools(item)
                        ]
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
        selected_provider = ""
        if request.preferred_providers:
            selected_provider = next(
                (
                    available
                    for preferred in request.preferred_providers
                    for available in selection_order
                    if available == preferred
                    or _provider_matches(available, [preferred])
                ),
                "",
            )
            if not selected_provider and request.require_preferred_provider:
                selected_provider = request.preferred_providers[0]
                preferred_readiness: list[dict[str, Any]] = []
                status_reader = getattr(
                    self.llm_router,
                    "provider_readiness",
                    None,
                )
                if callable(status_reader):
                    for preferred in request.preferred_providers:
                        preferred_readiness.append(
                            await asyncio.to_thread(
                                status_reader,
                                preferred,
                            )
                        )
                diagnostics["preferred_provider_readiness"] = (
                    preferred_readiness
                )
                status_by_provider = {
                    str(item.get("provider", "")): str(
                        item.get("category", "unavailable")
                    )
                    for item in preferred_readiness
                }
                blockers.append(
                    "preferred LLM providers are unavailable: "
                    + ", ".join(
                        (
                            f"{preferred} "
                            f"({status_by_provider[preferred]})"
                            if preferred in status_by_provider
                            else preferred
                        )
                        for preferred in request.preferred_providers
                    )
                )
            elif not selected_provider:
                selected_provider = _preferred_value(
                    selection_order,
                    request.preferred_providers,
                )
        else:
            selected_provider = _preferred_value(selection_order, ())
        selected_target = target_by_provider.get(selected_provider)
        if (
            not selected_provider
            and not request.require_preferred_provider
            and self.default_llm_model
        ):
            selected_provider = "openopc_config"
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
        subscription_call_quota: dict[str, Any] = {}
        if (
            str((selected_target or {}).get("transport_kind", ""))
            == "subscription_cli"
            and _provider_matches(selected_provider, self.subscription_providers)
        ):
            subscription_call_quota = await self.repository.provider_call_quota_status(
                project_id=request.project_id,
                provider=selected_provider,
                limit=self.subscription_call_limit,
                window_seconds=self.subscription_window_seconds,
            )
            if request.allow_live and not subscription_call_quota["allowed"]:
                blockers.append(
                    f"subscription call quota exhausted for {selected_provider}: "
                    f"{subscription_call_quota['used']}/"
                    f"{subscription_call_quota['limit']} calls"
                )
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
                "preferred LLM provider is unavailable"
                if request.preferred_providers and selected_target is None
                else "selected from NU route diagnostics"
                if diagnostics.get("available")
                and selected_provider != "openopc_config"
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
                "subscription_call_quota": subscription_call_quota,
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
            execution_purpose = str(
                request.metadata.get("execution_purpose", "") or ""
            ).strip()
            if execution_purpose == "artifact_quality":
                quality_status_probe = getattr(
                    self.resource_bridge, "quality_execution_status", None
                )
                if not callable(quality_status_probe):
                    blockers.append("resource bridge has no local quality execution policy")
                else:
                    try:
                        quality_status = await asyncio.to_thread(
                            quality_status_probe, candidate_id
                        )
                    except Exception as exc:
                        blockers.append(
                            f"local quality policy failed: {type(exc).__name__}: {exc}"
                        )
                    else:
                        if not bool(quality_status.get("allowed", False)):
                            blockers.extend(
                                str(item)
                                for item in quality_status.get("blockers", []) or []
                            )
            else:
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
        hardware_readiness = _resource_hardware_readiness(selected, request)
        readiness_probe = getattr(self.resource_bridge, "candidate_readiness", None)
        readiness_argument = candidate_id
        if not callable(readiness_probe):
            readiness_probe = getattr(self.resource_bridge, "provider_readiness", None)
            readiness_argument = provider
        if selected and callable(readiness_probe):
            try:
                provider_readiness = await asyncio.to_thread(
                    readiness_probe, readiness_argument
                )
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
        if hardware_readiness["compatible"] is False:
            transport_ready = False
        if request.allow_live and selected:
            if not credential_ready:
                blockers.append("selected resource provider has no configured credentials")
            if not transport_ready:
                blockers.append("selected resource provider is not transport-ready")
            if hardware_readiness["required"] and hardware_readiness["compatible"] is None:
                blockers.append(
                    "live GPU resource requires explicit hardware_profile and gpu_free_vram_mib"
                )
            elif hardware_readiness["compatible"] is False:
                blockers.extend(hardware_readiness["blockers"])
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
                "hardware_readiness": hardware_readiness,
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


def _provider_matches(provider: str, configured: Sequence[str]) -> bool:
    normalized = str(provider or "").strip().lower()
    return any(
        normalized == item
        or normalized.startswith(f"{item}-")
        or normalized.startswith(f"{item}_")
        for item in configured
    )


def _target_supports_tools(target: Mapping[str, Any]) -> bool:
    """Interpret missing capability metadata using the transport contract."""

    explicit = target.get("supports_tools")
    if explicit is not None:
        return bool(explicit)
    return str(target.get("transport_kind", "") or "") not in {
        "subscription_cli",
        "nu_native",
    }


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


def _resource_hardware_readiness(
    candidate: Mapping[str, Any],
    request: CapabilityRequest,
) -> dict[str, Any]:
    required_arch = str(candidate.get("requires_gpu_arch", "") or "").strip().lower()
    loaded_vram = candidate.get("approx_loaded_vram_gb")
    try:
        required_vram_mib = (
            int(float(loaded_vram) * 1024) if loaded_vram is not None else 0
        )
    except (TypeError, ValueError):
        required_vram_mib = 0
    required = bool(required_arch or required_vram_mib)
    blockers: list[str] = []
    missing_evidence: list[str] = []
    if required_arch:
        if request.hardware_profile:
            actual_arch = request.hardware_profile.strip().lower()
            if required_arch not in actual_arch and actual_arch not in required_arch:
                blockers.append(
                    f"candidate requires GPU architecture {required_arch}; got {actual_arch}"
                )
        else:
            missing_evidence.append("hardware_profile")
    if required_vram_mib:
        if request.gpu_free_vram_mib > 0:
            if request.gpu_free_vram_mib < required_vram_mib:
                blockers.append(
                    f"candidate requires about {required_vram_mib} MiB free VRAM; "
                    f"got {request.gpu_free_vram_mib} MiB"
                )
        else:
            missing_evidence.append("gpu_free_vram_mib")
    compatible: bool | None
    if blockers:
        compatible = False
    elif missing_evidence:
        compatible = None
    else:
        compatible = True
    return {
        "required": required,
        "compatible": compatible,
        "requires_gpu_arch": required_arch,
        "required_vram_mib": required_vram_mib,
        "reported_hardware_profile": request.hardware_profile,
        "reported_free_vram_mib": request.gpu_free_vram_mib,
        "missing_evidence": missing_evidence,
        "blockers": blockers,
    }


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
        accounting = result.get("usage_accounting")
        if isinstance(accounting, Mapping):
            if accounting.get("cost_usd") is None:
                return None
            try:
                value = float(accounting["cost_usd"])
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) and value >= 0 else None
        value = result.get("cost_usd", result.get("cost"))
        unit = str(result.get("cost_unit", "usd") or "usd").lower()
        if value is not None and unit == "usd":
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return None
            return parsed if math.isfinite(parsed) and parsed >= 0 else None
    return None


def _actual_route_identity(result: Any, route: CapabilityRoute) -> dict[str, str]:
    payload = result if isinstance(result, Mapping) else {}
    provider = str(payload.get("provider", "") or route.provider)
    planned = [
        {
            "provider": route.provider,
            "candidate_id": route.candidate_id,
            "model": route.model,
        },
        *[dict(item) for item in route.alternatives],
    ]
    matching_plan = next(
        (item for item in planned if str(item.get("provider", "") or "") == provider),
        {},
    )
    return {
        "provider": provider,
        "candidate_id": str(
            payload.get("candidate_id", "") or matching_plan.get("candidate_id", "")
        ),
        "model": str(payload.get("model", "") or matching_plan.get("model", "")),
    }


def _validate_actual_route(route: CapabilityRoute, actual: Mapping[str, str]) -> str:
    allowed = [
        {
            "provider": route.provider,
            "candidate_id": route.candidate_id,
            "model": route.model,
        },
        *[dict(item) for item in route.alternatives],
    ]
    provider = str(actual.get("provider", "") or "")
    provider_matches = [
        item for item in allowed if str(item.get("provider", "") or "") == provider
    ]
    if not provider_matches:
        return f"actual provider {provider!r} was not present in the planned fallback order"
    for item in provider_matches:
        if all(
            not str(item.get(field, "") or "")
            or str(actual.get(field, "") or "") == str(item.get(field, "") or "")
            for field in ("candidate_id", "model")
        ):
            return ""
        if _resolved_native_model_matches(route, item, actual):
            return ""
    identity = {
        field: str(actual.get(field, "") or "")
        for field in ("candidate_id", "model")
    }
    return f"actual route identity {identity!r} was not planned for provider {provider!r}"


def _resolved_native_model_matches(
    route: CapabilityRoute,
    planned: Mapping[str, Any],
    actual: Mapping[str, str],
) -> bool:
    """Accept a native provider's concrete model only when it resolves its alias."""

    provider = str(planned.get("provider", "") or "")
    target = next(
        (
            item
            for item in route.diagnostics.get("targets", []) or []
            if isinstance(item, Mapping)
            and str(item.get("provider", "") or "") == provider
        ),
        {},
    )
    if str(target.get("transport_kind", "") or "") not in {
        "subscription_cli",
        "nu_native",
    }:
        return False
    planned_candidate = str(planned.get("candidate_id", "") or "")
    if (
        planned_candidate
        and str(actual.get("candidate_id", "") or "") != planned_candidate
    ):
        return False
    planned_model = str(planned.get("model", "") or "").lower()
    actual_model = str(actual.get("model", "") or "").lower()
    if not planned_model or not actual_model:
        return False
    planned_tokens = {
        item for item in re.split(r"[-_./:]+", planned_model) if item
    }
    actual_tokens = {
        item for item in re.split(r"[-_./:]+", actual_model) if item
    }
    return bool(planned_tokens and planned_tokens.issubset(actual_tokens))


def _usage_event(
    request: CapabilityRequest,
    contract: RouteExecutionContract,
    actual: Mapping[str, str],
    result: Any,
    *,
    cost_usd: float | None,
) -> ProviderUsageEvent:
    payload = result if isinstance(result, Mapping) else {}
    accounting = payload.get("usage_accounting")
    accounting_map = dict(accounting) if isinstance(accounting, Mapping) else {}
    legacy = payload.get("usage")
    usage_map = dict(legacy) if isinstance(legacy, Mapping) else {}

    input_tokens = _optional_nonnegative_int(
        accounting_map.get("input_tokens", usage_map.get("prompt_tokens"))
    )
    output_tokens = _optional_nonnegative_int(
        accounting_map.get("output_tokens", usage_map.get("completion_tokens"))
    )
    total_tokens = _optional_nonnegative_int(accounting_map.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    quota = accounting_map.get("subscription_quota")
    quota_map = dict(quota) if isinstance(quota, Mapping) else {}
    claimed_measured = (
        bool(accounting_map.get("measured")) if accounting_map else bool(usage_map)
    )
    has_concrete_measurement = any(
        value is not None
        for value in (input_tokens, output_tokens, total_tokens, cost_usd)
    ) or any(value is not None for value in quota_map.values())
    measured = bool(claimed_measured and has_concrete_measurement)
    source = str(
        accounting_map.get("source")
        if measured and accounting_map.get("source")
        else "provider_reported"
        if measured
        else "unknown"
    )
    return ProviderUsageEvent(
        contract_id=contract.contract_id,
        request_id=request.request_id,
        route_id=contract.route_id,
        capability_kind=request.capability_kind,
        project_id=request.project_id,
        run_id=request.run_id,
        provider=str(actual.get("provider", "") or ""),
        model=str(actual.get("model", "") or ""),
        measured=measured,
        source=source,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        subscription_quota=quota_map,
        metadata={
            "finish_reason": str(payload.get("finish_reason", "") or ""),
            "cost_known": cost_usd is not None,
        },
    )


def _result_metadata(result: Any) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {"result_type": type(result).__name__}
    return {
        key: result[key]
        for key in ("status", "finish_reason", "artifact_path", "quality_score")
        if key in result and isinstance(result[key], (str, int, float, bool, type(None)))
    }


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _dedupe(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in values if str(item).strip()))
