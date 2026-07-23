"""Persisted status/live canaries and deterministic provider SLO summaries."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any, Callable, Mapping

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
        p95_latency_target_ms: float = 30_000.0,
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
            p95_latency = round(_percentile(latencies, 0.95), 3)
            availability_met = availability >= availability_target
            latency_met = p95_latency <= p95_latency_target_ms
            summaries[name] = {
                "samples": len(values),
                "successes": successes,
                "availability": round(availability, 6),
                "availability_target": availability_target,
                "p95_latency_target_ms": p95_latency_target_ms,
                "availability_target_met": availability_met,
                "latency_target_met": latency_met,
                "target_met": availability_met and latency_met,
                "p50_latency_ms": round(_percentile(latencies, 0.50), 3),
                "p95_latency_ms": p95_latency,
                "consecutive_failures": consecutive_failures,
                "model_drift_count": sum(item.model_drift for item in values),
                "last_model": values[0].model,
                "last_error_category": values[0].error_category,
            }
        return {
            "project_id": project_id,
            "availability_target": availability_target,
            "p95_latency_target_ms": p95_latency_target_ms,
            "sample_count": len(rows),
            "providers": summaries,
        }


class ProviderCanaryScheduler:
    """Run status-only canaries periodically and retain the latest SLO view."""

    def __init__(
        self,
        service: ProviderCanaryService,
        request_factory: Callable[[], CapabilityRequest],
        *,
        interval_seconds: float = 300.0,
        expected_model: str = "",
        availability_target: float = 0.95,
        p95_latency_target_ms: float = 30_000.0,
        project_id: str = "default",
    ) -> None:
        self.service = service
        self.request_factory = request_factory
        self.interval_seconds = max(0.01, float(interval_seconds))
        self.expected_model = str(expected_model or "")
        self.availability_target = float(availability_target)
        self.p95_latency_target_ms = max(1.0, float(p95_latency_target_ms))
        self.project_id = str(project_id or "default")
        self.last_result: ProviderCanaryResult | None = None
        self.last_slo: dict[str, Any] = {}
        self.last_error = ""
        self.run_count = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._run_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run_once(self) -> ProviderCanaryResult:
        async with self._run_lock:
            request = self.request_factory()
            request.project_id = self.project_id
            result = await self.service.status_canary(
                request,
                expected_model=self.expected_model,
            )
            self.last_result = result
            self.last_slo = await self.service.slo_summary(
                project_id=self.project_id,
                availability_target=self.availability_target,
                p95_latency_target_ms=self.p95_latency_target_ms,
            )
            self.last_error = ""
            self.run_count += 1
            return result

    async def start(self) -> None:
        if self.running:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(
            self._run_loop(),
            name=f"provider-canary:{self.project_id}",
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        task = self._task
        self._task = None
        await task

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.interval_seconds
                )
                continue
            except TimeoutError:
                pass
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:1000]


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
