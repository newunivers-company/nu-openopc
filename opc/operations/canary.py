"""Persisted status/live canaries and deterministic provider SLO summaries."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Mapping

from opc.operations.capabilities import CapabilityExecutor, UnifiedCapabilityBroker
from opc.operations.models import CapabilityRequest, ProviderCanaryResult
from opc.operations.repository import OperationsRepository


class ProviderCanaryService:
    def __init__(
        self,
        repository: OperationsRepository,
        broker: UnifiedCapabilityBroker,
    ) -> None:
        self.repository = repository
        self.broker = broker

    async def status_canary(
        self,
        request: CapabilityRequest | Mapping[str, Any],
        *,
        expected_model: str = "",
    ) -> ProviderCanaryResult:
        parsed = request if isinstance(request, CapabilityRequest) else CapabilityRequest.from_dict(request)
        parsed.allow_live = False
        started = time.monotonic()
        try:
            route = await self.broker.plan(parsed, record_attempt=False)
            transport_ready = bool(route.readiness.get("transport_ready", False))
            success = bool(route.allowed and transport_ready)
            error = "; ".join(route.blockers)
            if route.allowed and not transport_ready:
                error = "planned route is not transport-ready"
            result = ProviderCanaryResult(
                project_id=parsed.project_id,
                capability_kind=parsed.capability_kind,
                provider=route.provider,
                candidate_id=route.candidate_id,
                model=route.model,
                mode="status",
                success=success,
                available=route.allowed,
                credential_ready=bool(route.readiness.get("credential_ready", False)),
                transport_ready=transport_ready,
                latency_ms=(time.monotonic() - started) * 1000,
                expected_model=expected_model,
                model_drift=bool(expected_model and route.model != expected_model),
                error_category=_error_category(error),
                error=error[:4000],
                metadata={
                    "route_id": route.route_id,
                    "mode": route.mode,
                    "readiness": dict(route.readiness),
                },
            )
        except Exception as exc:
            result = ProviderCanaryResult(
                project_id=parsed.project_id,
                capability_kind=parsed.capability_kind,
                provider=parsed.preferred_providers[0] if parsed.preferred_providers else "unknown",
                candidate_id=parsed.candidate_id,
                mode="status",
                success=False,
                latency_ms=(time.monotonic() - started) * 1000,
                expected_model=expected_model,
                error_category=_error_category(str(exc)),
                error=f"{type(exc).__name__}: {exc}"[:4000],
            )
        return await self.repository.save_provider_canary_result(result)

    async def live_canary(
        self,
        request: CapabilityRequest | Mapping[str, Any],
        executor: CapabilityExecutor,
        *,
        confirm_live: bool = False,
        expected_model: str = "",
    ) -> ProviderCanaryResult:
        parsed = request if isinstance(request, CapabilityRequest) else CapabilityRequest.from_dict(request)
        if not confirm_live or not parsed.allow_live:
            raise PermissionError("live canary requires allow_live=true and confirm_live=true")
        started = time.monotonic()
        provider = parsed.preferred_providers[0] if parsed.preferred_providers else "unknown"
        candidate = parsed.candidate_id
        model = ""
        try:
            route, raw = await self.broker.execute(parsed, executor)
            provider = str(raw.get("provider", "") if isinstance(raw, Mapping) else "") or route.provider
            candidate = (
                str(raw.get("candidate_id", "") if isinstance(raw, Mapping) else "")
                or route.candidate_id
            )
            model = str(raw.get("model", "") if isinstance(raw, Mapping) else "") or route.model
            result = ProviderCanaryResult(
                project_id=parsed.project_id,
                capability_kind=parsed.capability_kind,
                provider=provider,
                candidate_id=candidate,
                model=model,
                mode="live",
                success=True,
                available=True,
                credential_ready=True,
                transport_ready=True,
                latency_ms=(time.monotonic() - started) * 1000,
                expected_model=expected_model,
                model_drift=bool(expected_model and model != expected_model),
                metadata={"route_id": route.route_id},
            )
        except Exception as exc:
            result = ProviderCanaryResult(
                project_id=parsed.project_id,
                capability_kind=parsed.capability_kind,
                provider=provider,
                candidate_id=candidate,
                model=model,
                mode="live",
                success=False,
                latency_ms=(time.monotonic() - started) * 1000,
                expected_model=expected_model,
                model_drift=bool(expected_model and model and model != expected_model),
                error_category=_error_category(str(exc)),
                error=f"{type(exc).__name__}: {exc}"[:4000],
            )
        return await self.repository.save_provider_canary_result(result)

    async def slo_summary(
        self,
        *,
        project_id: str = "default",
        provider: str | None = None,
        limit: int = 100,
        availability_target: float = 0.95,
    ) -> dict[str, Any]:
        rows = await self.repository.list_provider_canary_results(
            project_id=project_id,
            provider=provider,
            limit=limit,
        )
        grouped: dict[str, list[ProviderCanaryResult]] = defaultdict(list)
        for row in rows:
            grouped[row.provider].append(row)
        summaries: dict[str, dict[str, Any]] = {}
        for name, values in sorted(grouped.items()):
            latencies = sorted(item.latency_ms for item in values)
            successes = sum(item.success for item in values)
            consecutive_failures = 0
            for item in values:  # repository order is newest first
                if item.success:
                    break
                consecutive_failures += 1
            availability = successes / len(values)
            summaries[name] = {
                "samples": len(values),
                "successes": successes,
                "availability": round(availability, 6),
                "availability_target": availability_target,
                "target_met": availability >= availability_target,
                "p50_latency_ms": round(_percentile(latencies, 0.50), 3),
                "p95_latency_ms": round(_percentile(latencies, 0.95), 3),
                "consecutive_failures": consecutive_failures,
                "model_drift_count": sum(item.model_drift for item in values),
                "last_model": values[0].model,
                "last_error_category": values[0].error_category,
            }
        return {
            "project_id": project_id,
            "availability_target": availability_target,
            "sample_count": len(rows),
            "providers": summaries,
        }


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    index = max(0, min(len(values) - 1, int((len(values) - 1) * fraction + 0.999999)))
    return float(values[index])


def _error_category(error: str) -> str:
    text = str(error or "").lower()
    if not text:
        return ""
    if any(token in text for token in ("credential", "auth", "api key", "unauthorized")):
        return "authentication"
    if any(token in text for token in ("quota", "rate limit", "429")):
        return "quota"
    if any(token in text for token in ("timeout", "timed out")):
        return "timeout"
    if any(token in text for token in ("transport", "connection", "unavailable")):
        return "transport"
    if "cost" in text or "budget" in text:
        return "budget"
    return "other"
