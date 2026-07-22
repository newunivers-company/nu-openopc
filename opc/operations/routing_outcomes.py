"""Turn measured route outcomes into governed, shadow-only policy candidates."""

from __future__ import annotations

from collections import defaultdict
from statistics import fmean
from typing import Any

from opc.operations.learning import LearningAssetManager
from opc.operations.models import GateStatus, LearningAsset
from opc.operations.repository import OperationsRepository


class RoutingOutcomeService:
    def __init__(
        self,
        repository: OperationsRepository,
        learning: LearningAssetManager,
    ) -> None:
        self.repository = repository
        self.learning = learning

    async def summarize(
        self,
        *,
        project_id: str = "default",
        min_samples: int = 3,
        minimum_success_rate: float = 0.9,
        minimum_quality_score: float = 0.8,
        minimum_measurement_rate: float = 0.8,
    ) -> dict[str, Any]:
        contracts = await self.repository.list_route_execution_contracts(
            project_id=project_id,
            limit=5000,
        )
        usage_rows = await self.repository.list_provider_usage_events(
            project_id=project_id,
            limit=5000,
        )
        attempts = await self.repository.list_capability_attempts(
            project_id=project_id,
            limit=5000,
        )
        scorecards = await self.repository.list_scorecards(project_id=project_id, limit=5000)
        usage_by_contract = {item.contract_id: item for item in usage_rows}
        attempts_by_route: dict[str, list[Any]] = defaultdict(list)
        for item in attempts:
            attempts_by_route[item.route_id].append(item)
        scorecard_by_run = {item.run_id: item for item in scorecards}
        groups: dict[tuple[str, str, str, str, str], list[Any]] = defaultdict(list)
        for contract in contracts:
            task_type = str(contract.request_snapshot.get("task_type", "") or "")
            groups[
                (
                    contract.capability_kind.value,
                    task_type,
                    contract.actual_provider or contract.planned_provider,
                    contract.actual_candidate_id or contract.planned_candidate_id,
                    contract.actual_model or contract.planned_model,
                )
            ].append(contract)

        routes: list[dict[str, Any]] = []
        for (kind, task_type, provider, candidate_id, model), values in groups.items():
            successful = [item for item in values if item.status == "completed"]
            measured = [item for item in values if usage_by_contract.get(item.contract_id) and usage_by_contract[item.contract_id].measured]
            quality_rows = [
                scorecard_by_run[item.run_id]
                for item in values
                if item.run_id in scorecard_by_run
            ]
            passing_runs = [
                item.run_id
                for item in values
                if item.run_id in scorecard_by_run
                and scorecard_by_run[item.run_id].gate_status == GateStatus.PASS
            ]
            known_costs = [
                usage_by_contract[item.contract_id].cost_usd
                for item in values
                if item.contract_id in usage_by_contract
                and usage_by_contract[item.contract_id].cost_usd is not None
            ]
            latencies = [
                attempt.latency_ms
                for item in values
                for attempt in attempts_by_route.get(item.route_id, [])
                if attempt.status in {"completed", "failed", "contract_violation", "budget_exceeded"}
            ]
            samples = len(values)
            success_rate = len(successful) / samples
            measurement_rate = len(measured) / samples
            average_quality = (
                fmean(item.total_score for item in quality_rows) if quality_rows else None
            )
            eligible = bool(
                samples >= max(1, int(min_samples))
                and success_rate >= minimum_success_rate
                and average_quality is not None
                and average_quality >= minimum_quality_score
                and measurement_rate >= minimum_measurement_rate
                and len(passing_runs) >= max(1, int(min_samples))
            )
            routes.append(
                {
                    "capability_kind": kind,
                    "task_type": task_type,
                    "provider": provider,
                    "candidate_id": candidate_id,
                    "model": model,
                    "samples": samples,
                    "success_rate": round(success_rate, 6),
                    "measurement_rate": round(measurement_rate, 6),
                    "average_quality_score": (
                        None if average_quality is None else round(average_quality, 6)
                    ),
                    "average_cost_usd": (
                        None if not known_costs else round(fmean(known_costs), 8)
                    ),
                    "average_latency_ms": (
                        None if not latencies else round(fmean(latencies), 3)
                    ),
                    "passing_run_ids": list(dict.fromkeys(passing_runs)),
                    "contract_ids": [item.contract_id for item in values],
                    "eligible_for_shadow": eligible,
                    "ineligible_reasons": _ineligible_reasons(
                        samples=samples,
                        min_samples=min_samples,
                        success_rate=success_rate,
                        minimum_success_rate=minimum_success_rate,
                        average_quality=average_quality,
                        minimum_quality_score=minimum_quality_score,
                        measurement_rate=measurement_rate,
                        minimum_measurement_rate=minimum_measurement_rate,
                        passing_runs=len(passing_runs),
                    ),
                }
            )
        routes.sort(
            key=lambda item: (
                not item["eligible_for_shadow"],
                -item["success_rate"],
                -(item["average_quality_score"] or 0.0),
                item["average_cost_usd"] if item["average_cost_usd"] is not None else float("inf"),
                item["provider"],
            )
        )
        return {
            "project_id": project_id,
            "contract_count": len(contracts),
            "thresholds": {
                "min_samples": max(1, int(min_samples)),
                "minimum_success_rate": minimum_success_rate,
                "minimum_quality_score": minimum_quality_score,
                "minimum_measurement_rate": minimum_measurement_rate,
            },
            "eligible_route_count": sum(item["eligible_for_shadow"] for item in routes),
            "routes": routes,
        }

    async def propose_learning_candidate(
        self,
        *,
        project_id: str = "default",
        name: str = "outcome-routing-policy",
        min_samples: int = 3,
    ) -> tuple[LearningAsset | None, dict[str, Any]]:
        summary = await self.summarize(project_id=project_id, min_samples=min_samples)
        eligible = [item for item in summary["routes"] if item["eligible_for_shadow"]]
        if not eligible:
            return None, summary
        source_runs = list(
            dict.fromkeys(
                run_id
                for item in eligible
                for run_id in item["passing_run_ids"]
            )
        )
        confidence = min(
            1.0,
            fmean(
                (item["success_rate"] + item["average_quality_score"] + item["measurement_rate"])
                / 3
                for item in eligible
            ),
        )
        asset = await self.learning.create_candidate(
            name=name,
            kind="routing_policy",
            project_id=project_id,
            content={
                "application_mode": "shadow_only",
                "automatic_promotion": False,
                "priority_order": [
                    {
                        key: item[key]
                        for key in (
                            "capability_kind",
                            "task_type",
                            "provider",
                            "candidate_id",
                            "model",
                            "success_rate",
                            "average_quality_score",
                            "average_cost_usd",
                            "average_latency_ms",
                        )
                    }
                    for item in eligible
                ],
                "thresholds": summary["thresholds"],
            },
            source_run_ids=source_runs,
            confidence=confidence,
            metadata={
                "source": "route_execution_outcomes",
                "requires_release_gate": True,
                "contract_ids": [
                    contract_id for item in eligible for contract_id in item["contract_ids"]
                ],
            },
        )
        return asset, summary


def _ineligible_reasons(
    *,
    samples: int,
    min_samples: int,
    success_rate: float,
    minimum_success_rate: float,
    average_quality: float | None,
    minimum_quality_score: float,
    measurement_rate: float,
    minimum_measurement_rate: float,
    passing_runs: int,
) -> list[str]:
    reasons: list[str] = []
    if samples < min_samples:
        reasons.append(f"samples {samples} < {min_samples}")
    if success_rate < minimum_success_rate:
        reasons.append(f"success_rate {success_rate:.3f} < {minimum_success_rate:.3f}")
    if average_quality is None:
        reasons.append("no scorecard quality evidence")
    elif average_quality < minimum_quality_score:
        reasons.append(f"quality {average_quality:.3f} < {minimum_quality_score:.3f}")
    if measurement_rate < minimum_measurement_rate:
        reasons.append(
            f"measurement_rate {measurement_rate:.3f} < {minimum_measurement_rate:.3f}"
        )
    if passing_runs < min_samples:
        reasons.append(f"passing_runs {passing_runs} < {min_samples}")
    return reasons
