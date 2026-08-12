"""Persisted status/live canaries and deterministic provider SLO summaries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from opc.operations.capabilities import CapabilityExecutor, UnifiedCapabilityBroker
from opc.operations.models import CapabilityKind, CapabilityRequest, ProviderCanaryResult
from opc.operations.repository import OperationsRepository


_CAMPAIGN_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def summarize_provider_slo(
    rows: list[ProviderCanaryResult],
    *,
    availability_target: float = 0.95,
    p95_latency_target_ms: float = 30_000.0,
    minimum_samples: int = 1,
    trend_window_samples: int = 3,
) -> dict[str, dict[str, Any]]:
    """Build an attainment and trend gate from newest-first canary rows."""

    sample_target = max(1, int(minimum_samples))
    trend_window = max(1, int(trend_window_samples))
    grouped: dict[str, list[ProviderCanaryResult]] = defaultdict(list)
    # Controlled failure drills prove fallback/alert behavior and must not
    # lower the production availability denominator.
    for row in rows:
        if row.mode not in {"status", "live"}:
            continue
        grouped[row.provider].append(row)
    summaries: dict[str, dict[str, Any]] = {}
    for name, values in sorted(grouped.items()):
        latencies = sorted(float(item.latency_ms) for item in values)
        successes = sum(bool(item.success) for item in values)
        consecutive_failures = 0
        for item in values:
            if item.success:
                break
            consecutive_failures += 1
        availability = successes / len(values)
        p95_latency = round(_percentile(latencies, 0.95), 3)
        availability_met = availability >= availability_target
        latency_met = p95_latency <= p95_latency_target_ms
        sample_target_met = len(values) >= sample_target
        attainment_state = (
            "insufficient_samples"
            if not sample_target_met
            else "met"
            if availability_met and latency_met
            else "missed"
        )
        trend = _trend_summary(values, window_samples=trend_window)
        model_drift_count = sum(bool(item.model_drift) for item in values)
        target_met = attainment_state == "met"
        summaries[name] = {
            "samples": len(values),
            "sample_target": sample_target,
            "sample_target_met": sample_target_met,
            "successes": successes,
            "availability": round(availability, 6),
            "availability_target": availability_target,
            "p95_latency_target_ms": p95_latency_target_ms,
            "availability_target_met": availability_met,
            "latency_target_met": latency_met,
            "attainment_state": attainment_state,
            "target_met": target_met,
            "promotion_ready": (
                target_met
                and model_drift_count == 0
                and trend["ready"]
                and trend["state"] != "degrading"
            ),
            "p50_latency_ms": round(_percentile(latencies, 0.50), 3),
            "p95_latency_ms": p95_latency,
            "consecutive_failures": consecutive_failures,
            "model_drift_count": model_drift_count,
            "last_model": values[0].model,
            "last_error_category": values[0].error_category,
            "trend": trend,
        }
    return summaries


def summarize_provider_readiness(
    rows: list[ProviderCanaryResult],
    *,
    availability_target: float = 0.95,
    p95_latency_target_ms: float = 30_000.0,
    minimum_samples: int = 30,
    trend_window_samples: int = 10,
    minimum_observation_seconds: int = 86_400,
    time_bucket_seconds: int = 21_600,
    minimum_time_buckets: int = 4,
    minimum_samples_per_bucket: int = 1,
    maximum_sample_age_seconds: int = 1_800,
    maximum_gap_seconds: int = 28_800,
    failure_drill_max_age_seconds: int = 2_592_000,
    required_failure_scenarios: list[str] | tuple[str, ...] = (),
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Add observation-window and controlled-failure evidence to the SLO gate."""

    operational = [row for row in rows if row.mode in {"status", "live"}]
    base = summarize_provider_slo(
        operational,
        availability_target=availability_target,
        p95_latency_target_ms=p95_latency_target_ms,
        minimum_samples=minimum_samples,
        trend_window_samples=trend_window_samples,
    )
    providers = sorted({row.provider for row in rows if row.provider})
    required = tuple(
        dict.fromkeys(
            str(item).strip()
            for item in required_failure_scenarios
            if str(item).strip()
        )
    )
    result: dict[str, dict[str, Any]] = {}
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("readiness now must include a timezone")
    for provider in providers:
        provider_operational = [
            row for row in operational if row.provider == provider
        ]
        timestamps = sorted(row.checked_at for row in provider_operational)
        observation_seconds = (
            max(0.0, (timestamps[-1] - timestamps[0]).total_seconds())
            if len(timestamps) >= 2
            else 0.0
        )
        bucket_size = max(60, int(time_bucket_seconds))
        bucket_counts: dict[int, int] = {}
        for item in provider_operational:
            bucket = int(item.checked_at.timestamp() // bucket_size)
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        sample_floor = max(1, int(minimum_samples_per_bucket))
        covered_buckets = {
            bucket: count
            for bucket, count in bucket_counts.items()
            if count >= sample_floor
        }
        gaps = [
            max(0.0, (later - earlier).total_seconds())
            for earlier, later in zip(timestamps, timestamps[1:])
        ]
        largest_gap_seconds = max(gaps, default=0.0)
        latest_sample_age_seconds = (
            max(0.0, (current - timestamps[-1]).total_seconds())
            if timestamps
            else float("inf")
        )
        future_sample_count = sum(
            item.checked_at > current for item in provider_operational
        )
        drills = [
            row
            for row in rows
            if row.provider == provider and row.mode == "drill"
        ]
        drill_age_limit = max(60, int(failure_drill_max_age_seconds))
        verified_scenarios = {
            str(row.metadata.get("drill_scenario", "") or "")
            for row in drills
            if _verified_failure_drill(row)
            and row.checked_at <= current
            and (current - row.checked_at).total_seconds() <= drill_age_limit
        }
        missing_drills = [
            item for item in required if item not in verified_scenarios
        ]
        slo = dict(
            base.get(
                provider,
                {
                    "samples": 0,
                    "sample_target": max(1, int(minimum_samples)),
                    "sample_target_met": False,
                    "attainment_state": "insufficient_samples",
                    "target_met": False,
                    "promotion_ready": False,
                    "trend": {"ready": False, "state": "insufficient_samples"},
                },
            )
        )
        observation_target_met = observation_seconds >= max(
            0,
            int(minimum_observation_seconds),
        )
        time_bucket_target_met = len(covered_buckets) >= max(
            1,
            int(minimum_time_buckets),
        )
        freshness_target_met = bool(
            timestamps
            and future_sample_count == 0
            and latest_sample_age_seconds
            <= max(0, int(maximum_sample_age_seconds))
        )
        gap_target_met = bool(
            timestamps
            and largest_gap_seconds <= max(60, int(maximum_gap_seconds))
        )
        drill_target_met = not missing_drills
        production_ready = bool(
            slo.get("promotion_ready")
            and observation_target_met
            and time_bucket_target_met
            and freshness_target_met
            and gap_target_met
            and drill_target_met
        )
        blockers = [
            message
            for condition, message in (
                (
                    not slo.get("promotion_ready"),
                    "provider SLO/trend evidence is not promotion-ready",
                ),
                (
                    not observation_target_met,
                    "minimum observation span is not met",
                ),
                (
                    not time_bucket_target_met,
                    "minimum dense time-bucket coverage is not met",
                ),
                (
                    not freshness_target_met,
                    "latest canary sample is stale or future-dated",
                ),
                (
                    not gap_target_met,
                    "maximum gap between canary samples is exceeded",
                ),
                (
                    not drill_target_met,
                    "required fresh failure drills are missing",
                ),
            )
            if condition
        ]
        result[provider] = {
            **slo,
            "observation_seconds": round(observation_seconds, 3),
            "minimum_observation_seconds": max(
                0,
                int(minimum_observation_seconds),
            ),
            "observation_target_met": observation_target_met,
            "time_buckets": len(bucket_counts),
            "dense_time_buckets": len(covered_buckets),
            "minimum_time_buckets": max(1, int(minimum_time_buckets)),
            "minimum_samples_per_bucket": sample_floor,
            "time_bucket_sample_counts": {
                str(bucket): count
                for bucket, count in sorted(bucket_counts.items())
            },
            "time_bucket_seconds": bucket_size,
            "time_bucket_target_met": time_bucket_target_met,
            "latest_sample_age_seconds": (
                round(latest_sample_age_seconds, 3)
                if timestamps
                else None
            ),
            "maximum_sample_age_seconds": max(
                0,
                int(maximum_sample_age_seconds),
            ),
            "freshness_target_met": freshness_target_met,
            "largest_gap_seconds": round(largest_gap_seconds, 3),
            "maximum_gap_seconds": max(60, int(maximum_gap_seconds)),
            "gap_target_met": gap_target_met,
            "future_sample_count": future_sample_count,
            "verified_failure_scenarios": sorted(verified_scenarios),
            "required_failure_scenarios": list(required),
            "missing_failure_scenarios": missing_drills,
            "failure_drill_max_age_seconds": drill_age_limit,
            "failure_drill_target_met": drill_target_met,
            "production_ready": production_ready,
            "readiness_state": "ready" if production_ready else "pending_evidence",
            "blockers": blockers,
        }
    return result


def build_readiness_campaign_plan(
    *,
    campaign_id: str,
    provider: str,
    start_at: datetime,
    model: str = "",
    interval_seconds: float = 300.0,
    observation_seconds: int = 86_400,
    time_bucket_seconds: int = 21_600,
    minimum_time_buckets: int = 4,
    maximum_sample_age_seconds: int = 1_800,
    maximum_gap_seconds: int = 28_800,
    failure_drill_max_age_seconds: int = 2_592_000,
    required_failure_scenarios: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Create an auditable schedule without executing failure injection."""

    campaign = str(campaign_id or "").strip()
    selected_provider = str(provider or "").strip()
    if not _CAMPAIGN_ID.fullmatch(campaign):
        raise ValueError("readiness campaign_id has invalid characters")
    if not selected_provider:
        raise ValueError("readiness campaign provider is required")
    if start_at.tzinfo is None:
        raise ValueError("readiness campaign start_at must include a timezone")
    interval = max(1.0, float(interval_seconds))
    duration = max(0, int(observation_seconds))
    end_at = start_at + timedelta(seconds=duration)
    scenarios = list(
        dict.fromkeys(
            str(item).strip()
            for item in required_failure_scenarios
            if str(item).strip()
        )
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": campaign,
        "provider": selected_provider,
        "model": str(model or "").strip(),
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "status_canary": {
            "generation_allowed": False,
            "interval_seconds": interval,
            "minimum_observation_seconds": duration,
            "expected_minimum_samples": (
                int(duration // interval) + 1
            ),
            "time_bucket_seconds": max(60, int(time_bucket_seconds)),
            "minimum_time_buckets": max(1, int(minimum_time_buckets)),
            "maximum_sample_age_seconds": max(
                0,
                int(maximum_sample_age_seconds),
            ),
            "maximum_gap_seconds": max(60, int(maximum_gap_seconds)),
        },
        "failure_drills": [
            {
                "scenario": scenario,
                "maximum_evidence_age_seconds": max(
                    60,
                    int(failure_drill_max_age_seconds),
                ),
                "requires_explicit_operator_coordination": True,
                "result_contract": {
                    "actual_injection": True,
                    "expected_failure_observed": True,
                    "fallback_verified": True,
                    "alert_verified": True,
                    "recovery_verified": True,
                    "authority": "human_confirmed_or_independent_observer",
                    "evidence": ["durable artifact URI required"],
                },
            }
            for scenario in scenarios
        ],
        "automatic_failure_injection": False,
        "operator_note": (
            "This plan schedules status-only observation. Failure drills are "
            "never injected automatically and require coordinated, scoped execution."
        ),
    }
    report["plan_digest"] = hashlib.sha256(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return report


def verify_readiness_campaign_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate campaign integrity and its status-only safety contract."""
    payload = dict(plan or {})
    supplied_digest = str(payload.pop("plan_digest", "") or "").strip()
    calculated_digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if not supplied_digest or supplied_digest != calculated_digest:
        raise ValueError("readiness campaign plan digest mismatch")
    campaign_id = str(payload.get("campaign_id", "") or "").strip()
    provider = str(payload.get("provider", "") or "").strip()
    if not _CAMPAIGN_ID.fullmatch(campaign_id) or not provider:
        raise ValueError("readiness campaign identity is invalid")
    status_canary = payload.get("status_canary")
    if not isinstance(status_canary, Mapping):
        raise ValueError("readiness campaign status_canary contract is missing")
    if status_canary.get("generation_allowed") is not False:
        raise ValueError("readiness campaign must prohibit generation")
    if payload.get("automatic_failure_injection") is not False:
        raise ValueError("readiness campaign must prohibit automatic failure injection")
    return {**payload, "plan_digest": supplied_digest}


def validate_failure_drill_result(
    scenario: str,
    result: Mapping[str, Any],
    *,
    require_passed: bool = False,
) -> dict[str, Any]:
    """Normalize independently evidenced drill results without inventing proof."""
    drill = str(scenario or "").strip()
    authority = str(result.get("authority", "") or "").strip()
    evidence = [
        str(item).strip()
        for item in result.get("evidence", []) or []
        if str(item).strip()
    ]
    checks = {
        "actual_injection": bool(result.get("actual_injection", False)),
        "expected_failure_observed": bool(result.get("expected_failure_observed", False)),
        "fallback_verified": bool(result.get("fallback_verified", False)),
        "alert_verified": bool(result.get("alert_verified", False)),
        "recovery_verified": bool(result.get("recovery_verified", False)),
    }
    if not drill:
        raise ValueError("failure drill scenario is required")
    if authority not in {"human_confirmed", "independent_observer"}:
        raise ValueError("failure drill requires independent authority")
    if not evidence:
        raise ValueError("failure drill requires durable evidence")
    passed = all(checks.values())
    if require_passed and not passed:
        missing = [name for name, value in checks.items() if not value]
        raise ValueError(
            f"failure drill {drill!r} is incomplete: {', '.join(missing)}"
        )
    return {
        "scenario": drill,
        "authority": authority,
        "evidence": evidence,
        "passed": passed,
        **checks,
    }


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
        evidence_metadata: Mapping[str, Any] | None = None,
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
                model_drift=bool(
                    expected_model
                    and route.model
                    and route.model != expected_model
                ),
                error_category=_error_category(error),
                error=error[:4000],
                metadata={
                    "route_id": route.route_id,
                    "mode": route.mode,
                    "readiness": dict(route.readiness),
                    **dict(evidence_metadata or {}),
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
                metadata=dict(evidence_metadata or {}),
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

    async def record_failure_drill(
        self,
        *,
        project_id: str,
        provider: str,
        scenario: str,
        result: Mapping[str, Any],
        model: str = "",
    ) -> ProviderCanaryResult:
        """Persist an independently evidenced, controlled failure exercise."""

        drill = str(scenario or "").strip()
        if not drill or not str(provider or "").strip():
            raise ValueError("failure drill provider and scenario are required")
        normalized = validate_failure_drill_result(drill, result)
        passed = bool(normalized.pop("passed"))
        normalized.pop("scenario", None)
        row = ProviderCanaryResult(
            project_id=project_id,
            capability_kind=CapabilityKind.LLM,
            provider=str(provider).strip(),
            model=str(model or "").strip(),
            mode="drill",
            success=passed,
            available=True,
            credential_ready=True,
            transport_ready=True,
            error_category="" if passed else drill,
            error="" if passed else "controlled failure drill did not verify all required behavior",
            metadata={
                "drill_scenario": drill,
                **normalized,
            },
        )
        return await self.repository.save_provider_canary_result(row)

    async def slo_summary(
        self,
        *,
        project_id: str = "default",
        provider: str | None = None,
        limit: int = 100,
        availability_target: float = 0.95,
        p95_latency_target_ms: float = 30_000.0,
        minimum_samples: int = 1,
        trend_window_samples: int = 3,
    ) -> dict[str, Any]:
        rows = await self.repository.list_provider_canary_results(
            project_id=project_id,
            provider=provider,
            limit=limit,
        )
        summaries = summarize_provider_slo(
            rows,
            availability_target=availability_target,
            p95_latency_target_ms=p95_latency_target_ms,
            minimum_samples=minimum_samples,
            trend_window_samples=trend_window_samples,
        )
        return {
            "project_id": project_id,
            "availability_target": availability_target,
            "p95_latency_target_ms": p95_latency_target_ms,
            "sample_count": len(rows),
            "providers": summaries,
        }

    async def readiness_summary(
        self,
        *,
        project_id: str = "default",
        provider: str | None = None,
        limit: int = 5000,
        availability_target: float = 0.95,
        p95_latency_target_ms: float = 30_000.0,
        minimum_samples: int = 30,
        trend_window_samples: int = 10,
        minimum_observation_seconds: int = 86_400,
        time_bucket_seconds: int = 21_600,
        minimum_time_buckets: int = 4,
        minimum_samples_per_bucket: int = 1,
        maximum_sample_age_seconds: int = 1_800,
        maximum_gap_seconds: int = 28_800,
        failure_drill_max_age_seconds: int = 2_592_000,
        required_failure_scenarios: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        rows = await self.repository.list_provider_canary_results(
            project_id=project_id,
            provider=provider,
            limit=limit,
        )
        return {
            "project_id": project_id,
            "sample_count": len(rows),
            "providers": summarize_provider_readiness(
                rows,
                availability_target=availability_target,
                p95_latency_target_ms=p95_latency_target_ms,
                minimum_samples=minimum_samples,
                trend_window_samples=trend_window_samples,
                minimum_observation_seconds=minimum_observation_seconds,
                time_bucket_seconds=time_bucket_seconds,
                minimum_time_buckets=minimum_time_buckets,
                minimum_samples_per_bucket=minimum_samples_per_bucket,
                maximum_sample_age_seconds=maximum_sample_age_seconds,
                maximum_gap_seconds=maximum_gap_seconds,
                failure_drill_max_age_seconds=failure_drill_max_age_seconds,
                required_failure_scenarios=required_failure_scenarios,
            ),
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
        minimum_samples: int = 1,
        trend_window_samples: int = 3,
        project_id: str = "default",
    ) -> None:
        self.service = service
        self.request_factory = request_factory
        self.interval_seconds = max(0.01, float(interval_seconds))
        self.expected_model = str(expected_model or "")
        self.availability_target = float(availability_target)
        self.p95_latency_target_ms = max(1.0, float(p95_latency_target_ms))
        self.minimum_samples = max(1, int(minimum_samples))
        self.trend_window_samples = max(1, int(trend_window_samples))
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
                minimum_samples=self.minimum_samples,
                trend_window_samples=self.trend_window_samples,
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


def _verified_failure_drill(row: ProviderCanaryResult) -> bool:
    metadata = dict(row.metadata or {})
    return bool(
        row.success
        and metadata.get("actual_injection") is True
        and metadata.get("expected_failure_observed") is True
        and metadata.get("fallback_verified") is True
        and metadata.get("alert_verified") is True
        and metadata.get("recovery_verified") is True
        and str(metadata.get("authority", "") or "")
        in {"human_confirmed", "independent_observer"}
        and list(metadata.get("evidence", []) or [])
    )


def _trend_summary(
    values: list[ProviderCanaryResult],
    *,
    window_samples: int,
) -> dict[str, Any]:
    current = values[:window_samples]
    previous = values[window_samples : window_samples * 2]
    if len(current) < window_samples or len(previous) < window_samples:
        return {
            "ready": False,
            "state": "insufficient_samples",
            "window_samples": window_samples,
        }

    def metrics(window: list[ProviderCanaryResult]) -> tuple[float, float]:
        availability = sum(bool(item.success) for item in window) / len(window)
        latency = _percentile(sorted(float(item.latency_ms) for item in window), 0.95)
        return availability, latency

    current_availability, current_p95 = metrics(current)
    previous_availability, previous_p95 = metrics(previous)
    availability_delta = current_availability - previous_availability
    latency_delta = current_p95 - previous_p95
    meaningful_latency = max(10.0, previous_p95 * 0.10)
    if availability_delta < -0.02 or latency_delta > meaningful_latency:
        state = "degrading"
    elif availability_delta > 0.02 or latency_delta < -meaningful_latency:
        state = "improving"
    else:
        state = "stable"
    return {
        "ready": True,
        "state": state,
        "window_samples": window_samples,
        "current_availability": round(current_availability, 6),
        "previous_availability": round(previous_availability, 6),
        "availability_delta": round(availability_delta, 6),
        "current_p95_latency_ms": round(current_p95, 3),
        "previous_p95_latency_ms": round(previous_p95, 3),
        "p95_latency_delta_ms": round(latency_delta, 3),
    }


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


async def run_status_canary_loop(
    canaries: "ProviderCanaryService",
    request: CapabilityRequest,
    *,
    expected_model: str = "",
    interval_seconds: float = 300.0,
    iterations: int = 1,
    sleep: Callable[[float], Any] = asyncio.sleep,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    campaign_id: str = "",
    plan_digest: str = "",
) -> dict[str, Any]:
    """Drive periodic status canaries without a resident engine process.

    This is the operational answer to the 24h readiness window: an external
    scheduler (cron/systemd) or a long-lived CLI invocation keeps samples
    flowing while the engine is down. ``iterations=0`` runs until cancelled;
    every sample is persisted through the normal status-canary path, so it
    counts toward the same SLO/readiness evidence as engine-driven samples.
    """

    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if iterations < 0:
        raise ValueError("iterations must be non-negative")
    samples = 0
    failures = 0
    last: dict[str, Any] | None = None
    while True:
        evidence_metadata = {
            key: value
            for key, value in {
                "readiness_campaign_id": str(campaign_id or "").strip(),
                "readiness_campaign_plan_digest": str(plan_digest or "").strip(),
            }.items()
            if value
        }
        status_kwargs: dict[str, Any] = {"expected_model": expected_model}
        if evidence_metadata:
            status_kwargs["evidence_metadata"] = evidence_metadata
        result = await canaries.status_canary(request, **status_kwargs)
        last = result.to_dict()
        samples += 1
        if not result.success:
            failures += 1
        if on_result is not None:
            on_result(last)
        if iterations and samples >= iterations:
            break
        await sleep(interval_seconds)
    return {
        "samples": samples,
        "failures": failures,
        "interval_seconds": interval_seconds,
        "last": last,
        "campaign_id": str(campaign_id or "").strip(),
        "plan_digest": str(plan_digest or "").strip(),
    }


async def run_readiness_campaign(
    canaries: ProviderCanaryService,
    request: CapabilityRequest,
    plan: Mapping[str, Any],
    *,
    iterations: int = 1,
    interval_seconds: float | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Execute only the status-canary portion of a verified campaign plan."""
    verified = verify_readiness_campaign_plan(plan)
    provider = str(verified["provider"])
    request.allow_live = False
    request.preferred_providers = [provider]
    request.require_preferred_provider = True
    expected_model = str(verified.get("model", "") or "")
    configured_interval = float(
        dict(verified["status_canary"]).get("interval_seconds", 300.0)
    )
    report = await run_status_canary_loop(
        canaries,
        request,
        expected_model=expected_model,
        interval_seconds=(
            configured_interval
            if interval_seconds is None
            else float(interval_seconds)
        ),
        iterations=iterations,
        sleep=sleep,
        on_result=on_result,
        campaign_id=str(verified["campaign_id"]),
        plan_digest=str(verified["plan_digest"]),
    )
    return {
        **report,
        "provider": provider,
        "generation_allowed": False,
        "automatic_failure_injection": False,
    }


_DRILL_RUNBOOKS: dict[str, list[str]] = {
    "credential_expiry": [
        "Coordinate a window with the provider owner; announce the drill.",
        "Temporarily rotate or invalidate the provider credential (actual injection).",
        "Confirm the expected authentication failure is observed by a canary call.",
        "Confirm routing falls back to the next healthy target.",
        "Confirm the readiness/Mission Control alert fired.",
        "Restore the credential and confirm recovery with a passing canary.",
    ],
    "transport_timeout": [
        "Insert a controlled delay (proxy or firewall rule) in front of the provider endpoint.",
        "Confirm the expected timeout failure is observed.",
        "Confirm fallback routing and alerting, then remove the delay.",
        "Confirm recovery with a passing canary.",
    ],
    "quota_exhaustion": [
        "Drive the provider to its quota ceiling in a controlled scope "
        "(or coordinate a temporary quota reduction).",
        "Confirm the expected quota rejection is observed and categorized.",
        "Confirm fallback routing and alerting.",
        "Wait for or restore quota; confirm recovery.",
    ],
    "model_drift": [
        "Pin an expected model revision that intentionally mismatches the "
        "provider's served model.",
        "Confirm the drift is detected and surfaced.",
        "Confirm alerting, restore the correct pin, and confirm recovery.",
    ],
}


def build_drill_result_template(
    scenario: str,
    *,
    provider: str = "",
    model: str = "",
) -> dict[str, Any]:
    """Emit a record-drill result skeleton with the scenario's runbook.

    Every verification flag starts ``False`` and the evidence list starts
    empty, so an unedited template can never be recorded as a passing drill.
    """

    drill = str(scenario or "").strip()
    if drill not in _DRILL_RUNBOOKS:
        raise ValueError(
            f"unknown drill scenario: {drill!r}; expected one of "
            f"{sorted(_DRILL_RUNBOOKS)}"
        )
    return {
        "provider": str(provider or "").strip(),
        "model": str(model or "").strip(),
        "scenario": drill,
        "actual_injection": False,
        "expected_failure_observed": False,
        "fallback_verified": False,
        "alert_verified": False,
        "recovery_verified": False,
        "authority": "",
        "evidence": [],
        "runbook": list(_DRILL_RUNBOOKS[drill]),
        "notes": (
            "Set authority to human_confirmed or independent_observer, add at "
            "least one durable evidence URI, and flip each verification flag "
            "only after the behavior was actually observed."
        ),
    }
