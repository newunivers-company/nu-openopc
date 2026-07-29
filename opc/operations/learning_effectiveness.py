"""Measure whether promoted Self-Grown assets improve later run outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Any, Mapping, Sequence

from opc.operations.models import RunManifest, RunScorecard
from opc.operations.repository import OperationsRepository


_SNAPSHOT_METADATA_KEY = "learning_activation"


@dataclass(frozen=True)
class LearningEffectivenessPolicy:
    """Evidence floors for a learning-effectiveness comparison."""

    minimum_samples_per_arm: int = 3
    maximum_quality_regression: float = 0.02
    minimum_quality_improvement: float = 0.0

    def validate(self) -> None:
        if self.minimum_samples_per_arm < 1:
            raise ValueError("minimum_samples_per_arm must be positive")
        if not 0 <= self.maximum_quality_regression <= 1:
            raise ValueError("maximum_quality_regression must be between 0 and 1")
        if not 0 <= self.minimum_quality_improvement <= 1:
            raise ValueError("minimum_quality_improvement must be between 0 and 1")


class LearningEffectivenessService:
    """Compare runs with a pinned learning asset against comparable controls."""

    def __init__(self, repository: OperationsRepository) -> None:
        self.repository = repository

    async def report(
        self,
        asset_id: str,
        *,
        project_id: str,
        cohort_key: str = "",
        policy: LearningEffectivenessPolicy | None = None,
    ) -> dict[str, Any]:
        asset = await self.repository.get_learning_asset(asset_id)
        if asset is None:
            raise KeyError(f"learning asset not found: {asset_id}")
        if asset.project_id != project_id:
            raise ValueError(
                f"learning asset project {asset.project_id!r} does not match "
                f"requested project {project_id!r}"
            )
        manifests = await self.repository.list_manifests(
            project_id=project_id,
            limit=1000,
        )
        scorecards = await self.repository.list_scorecards(
            project_id=project_id,
            limit=1000,
        )
        return build_learning_effectiveness_report(
            asset_id,
            manifests=manifests,
            scorecards=scorecards,
            cohort_key=cohort_key,
            policy=policy,
            asset_identity={
                "asset_id": asset.asset_id,
                "name": asset.name,
                "kind": asset.kind,
                "version": asset.version,
                "status": asset.status.value,
            },
        )


def build_learning_effectiveness_report(
    asset_id: str,
    *,
    manifests: Sequence[RunManifest],
    scorecards: Sequence[RunScorecard],
    cohort_key: str = "",
    policy: LearningEffectivenessPolicy | None = None,
    asset_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a fail-closed treated-versus-control outcome report.

    Controls are admitted only from a cohort that also contains a run with the
    exact pinned asset. By default a benchmark case id, explicit workload
    cohort, or goal id is used. ``cohort_key`` can require a caller-owned
    manifest/scorecard metadata key for stricter experiments.
    """

    clean_asset_id = str(asset_id or "").strip()
    if not clean_asset_id:
        raise ValueError("asset_id is required")
    effective_policy = policy or LearningEffectivenessPolicy()
    effective_policy.validate()
    manifests_by_run = {item.run_id: item for item in manifests}

    rows: list[dict[str, Any]] = []
    excluded: dict[str, int] = {
        "missing_manifest": 0,
        "missing_cohort": 0,
    }
    for scorecard in scorecards:
        manifest = manifests_by_run.get(scorecard.run_id)
        if manifest is None:
            excluded["missing_manifest"] += 1
            continue
        cohort = _cohort_identity(
            manifest,
            scorecard,
            cohort_key=cohort_key,
        )
        if not cohort:
            excluded["missing_cohort"] += 1
            continue
        rows.append(
            {
                "cohort": cohort,
                "treated": clean_asset_id in _activated_asset_ids(manifest),
                "scorecard": scorecard,
            }
        )

    treated_cohorts = {
        str(row["cohort"])
        for row in rows
        if bool(row["treated"])
    }
    comparable = [row for row in rows if row["cohort"] in treated_cohorts]
    treated = [
        row["scorecard"]
        for row in comparable
        if bool(row["treated"])
    ]
    control = [
        row["scorecard"]
        for row in comparable
        if not bool(row["treated"])
    ]
    treated_summary = _summarize(treated)
    control_summary = _summarize(control)
    enough_samples = (
        len(treated) >= effective_policy.minimum_samples_per_arm
        and len(control) >= effective_policy.minimum_samples_per_arm
    )
    quality_delta = _delta(treated_summary, control_summary, "quality_score")
    total_delta = _delta(treated_summary, control_summary, "total_score")
    success_delta = _delta(treated_summary, control_summary, "success_rate")
    intervention_delta = _delta(
        treated_summary,
        control_summary,
        "mean_interventions",
    )
    rework_delta = _delta(
        treated_summary,
        control_summary,
        "mean_rework_cycles",
    )
    regressed = bool(
        enough_samples
        and (
            quality_delta < -effective_policy.maximum_quality_regression
            or success_delta < -effective_policy.maximum_quality_regression
        )
    )
    if not enough_samples:
        status = "insufficient_evidence"
    elif regressed:
        status = "regressed"
    elif quality_delta > effective_policy.minimum_quality_improvement:
        status = "improved"
    else:
        status = "non_regressed"

    blockers: list[str] = []
    if len(treated) < effective_policy.minimum_samples_per_arm:
        blockers.append(
            f"treated samples {len(treated)} < "
            f"{effective_policy.minimum_samples_per_arm}"
        )
    if len(control) < effective_policy.minimum_samples_per_arm:
        blockers.append(
            f"control samples {len(control)} < "
            f"{effective_policy.minimum_samples_per_arm}"
        )
    if regressed:
        blockers.append(
            "quality or success regression exceeds the configured ceiling"
        )

    return {
        "schema_version": 1,
        "asset": {
            "asset_id": clean_asset_id,
            **dict(asset_identity or {}),
        },
        "status": status,
        "evidence_ready": enough_samples,
        "promotion_evidence_eligible": (
            enough_samples and status in {"improved", "non_regressed"}
        ),
        "policy": {
            "minimum_samples_per_arm": effective_policy.minimum_samples_per_arm,
            "maximum_quality_regression": (
                effective_policy.maximum_quality_regression
            ),
            "minimum_quality_improvement": (
                effective_policy.minimum_quality_improvement
            ),
        },
        "cohort": {
            "metadata_key": str(cohort_key or ""),
            "comparable_cohorts": sorted(treated_cohorts),
        },
        "treated": treated_summary,
        "control": control_summary,
        "delta": {
            "total_score": total_delta,
            "quality_score": quality_delta,
            "success_rate": success_delta,
            "mean_interventions": intervention_delta,
            "mean_rework_cycles": rework_delta,
            "mean_duration_seconds": _delta(
                treated_summary,
                control_summary,
                "mean_duration_seconds",
            ),
            "mean_cost_usd": _delta(
                treated_summary,
                control_summary,
                "mean_cost_usd",
            ),
        },
        "blockers": blockers,
        "excluded": excluded,
        "provenance": {
            "treated_run_ids": sorted(item.run_id for item in treated),
            "control_run_ids": sorted(item.run_id for item in control),
        },
    }


def _activated_asset_ids(manifest: RunManifest) -> set[str]:
    snapshot = manifest.metadata.get(_SNAPSHOT_METADATA_KEY)
    if not isinstance(snapshot, Mapping):
        return set()
    return {
        str(item.get("asset_id", "") or "").strip()
        for item in snapshot.get("assets", []) or []
        if isinstance(item, Mapping) and str(item.get("asset_id", "") or "").strip()
    }


def _cohort_identity(
    manifest: RunManifest,
    scorecard: RunScorecard,
    *,
    cohort_key: str,
) -> str:
    clean_key = str(cohort_key or "").strip()
    if clean_key:
        value = manifest.metadata.get(clean_key)
        if value is None or value == "":
            value = scorecard.metadata.get(clean_key)
        return str(value or "").strip()
    for value in (
        manifest.metadata.get("benchmark_case_id"),
        scorecard.metadata.get("learning_evaluation_cohort"),
        manifest.metadata.get("learning_evaluation_cohort"),
        scorecard.goal_id,
    ):
        clean = str(value or "").strip()
        if clean:
            return clean
    return ""


def _summarize(scorecards: Sequence[RunScorecard]) -> dict[str, Any]:
    if not scorecards:
        return {
            "samples": 0,
            "total_score": 0.0,
            "quality_score": 0.0,
            "evidence_score": 0.0,
            "reliability_score": 0.0,
            "autonomy_score": 0.0,
            "success_rate": 0.0,
            "mean_interventions": 0.0,
            "mean_rework_cycles": 0.0,
            "mean_duration_seconds": 0.0,
            "mean_cost_usd": 0.0,
        }

    def mean(values: Sequence[float]) -> float:
        return round(float(fmean(values)), 6)

    return {
        "samples": len(scorecards),
        "total_score": mean([item.total_score for item in scorecards]),
        "quality_score": mean([item.quality_score for item in scorecards]),
        "evidence_score": mean([item.evidence_score for item in scorecards]),
        "reliability_score": mean(
            [item.reliability_score for item in scorecards]
        ),
        "autonomy_score": mean([item.autonomy_score for item in scorecards]),
        "success_rate": mean([1.0 if item.accepted else 0.0 for item in scorecards]),
        "mean_interventions": mean(
            [float(item.metrics.interventions) for item in scorecards]
        ),
        "mean_rework_cycles": mean(
            [float(item.metrics.rework_cycles) for item in scorecards]
        ),
        "mean_duration_seconds": mean(
            [item.metrics.duration_seconds for item in scorecards]
        ),
        "mean_cost_usd": mean([item.metrics.cost_usd for item in scorecards]),
    }


def _delta(
    treated: Mapping[str, Any],
    control: Mapping[str, Any],
    key: str,
) -> float:
    return round(float(treated.get(key, 0.0)) - float(control.get(key, 0.0)), 6)
