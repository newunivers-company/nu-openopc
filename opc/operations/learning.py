"""Governed lifecycle for self-grown memories, skills, and policies."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from opc.core.config import LearningOperationsConfig
from opc.operations.models import (
    LearningAsset,
    LearningAssetEvaluation,
    LearningAssetStatus,
    clamp_score,
    utc_now,
)
from opc.operations.repository import OperationsRepository


class LearningLifecycleError(RuntimeError):
    """Raised when a learning asset attempts an invalid lifecycle transition."""


_EVALUATION_PHASE_THRESHOLDS = {
    "offline": "minimum_offline_score",
    "shadow": "minimum_shadow_score",
    "canary": "minimum_canary_score",
}


class LearningAssetManager:
    """Move learned operating assets through evidence-backed release phases."""

    def __init__(
        self,
        repository: OperationsRepository,
        config: LearningOperationsConfig | None = None,
    ) -> None:
        self.repository = repository
        self.config = config or LearningOperationsConfig()

    async def create_candidate(
        self,
        *,
        name: str,
        kind: str,
        content: Mapping[str, Any],
        project_id: str = "default",
        organization_id: str = "",
        employee_id: str = "",
        role_id: str = "",
        source_run_ids: Sequence[str] | None = None,
        confidence: float = 0.0,
        expires_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> LearningAsset:
        clean_name = str(name or "").strip()
        clean_kind = str(kind or "").strip()
        if not clean_name or not clean_kind:
            raise ValueError("learning asset name and kind are required")
        normalized_project = str(project_id or "default")
        normalized_organization = str(organization_id or "")
        async with self.repository.transaction_lock:
            await _begin(self.repository)
            try:
                existing = await self.repository.list_learning_assets(
                    project_id=normalized_project,
                    organization_id=normalized_organization,
                    name=clean_name,
                    limit=1000,
                )
                version = max((item.version for item in existing), default=0) + 1
                previous = next(
                    (item for item in existing if item.status == LearningAssetStatus.PROMOTED),
                    None,
                )
                asset = LearningAsset(
                    name=clean_name,
                    kind=clean_kind,
                    content=dict(content),
                    project_id=normalized_project,
                    organization_id=normalized_organization,
                    employee_id=str(employee_id or ""),
                    role_id=str(role_id or ""),
                    version=version,
                    previous_asset_id=previous.asset_id if previous else "",
                    source_run_ids=list(
                        dict.fromkeys(
                            str(item)
                            for item in source_run_ids or []
                            if str(item).strip()
                        )
                    ),
                    confidence=clamp_score(confidence),
                    expires_at=expires_at,
                    metadata={
                        "provenance_complete": bool(source_run_ids),
                        **dict(metadata or {}),
                    },
                )
                await self.repository.save_learning_asset(asset, commit=False)
                await self.repository.db.commit()
                return asset
            except Exception:
                await self.repository.db.rollback()
                raise

    async def create_from_employee_feedback(
        self,
        *,
        organization_id: str,
        project_id: str,
        employee_id: str,
        role_id: str,
        name: str,
        kind: str,
        feedback_patch: Mapping[str, Any],
        source_run_id: str,
        confidence: float,
    ) -> LearningAsset:
        if not source_run_id.strip():
            raise ValueError("source_run_id is required for employee learning provenance")
        return await self.create_candidate(
            name=name,
            kind=kind,
            content={"employee_feedback_patch": dict(feedback_patch)},
            project_id=project_id,
            organization_id=organization_id,
            employee_id=employee_id,
            role_id=role_id,
            source_run_ids=[source_run_id],
            confidence=confidence,
            metadata={"source": "employee_evolution", "requires_release_gate": True},
        )

    async def record_evaluation(
        self,
        asset_id: str,
        *,
        phase: str,
        score: float,
        baseline_score: float | None = None,
        sample_size: int,
        evidence: Sequence[str],
        violations: Sequence[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> LearningAssetEvaluation:
        normalized_phase = str(phase or "").strip().lower()
        if normalized_phase not in _EVALUATION_PHASE_THRESHOLDS:
            raise ValueError(f"unsupported learning evaluation phase: {phase!r}")
        clean_evidence = [str(item).strip() for item in evidence if str(item).strip()]
        clean_violations = [str(item).strip() for item in violations or [] if str(item).strip()]
        normalized_score = clamp_score(score)
        normalized_baseline = None if baseline_score is None else clamp_score(baseline_score)
        threshold = float(getattr(self.config, _EVALUATION_PHASE_THRESHOLDS[normalized_phase]))
        regression = (
            0.0
            if normalized_baseline is None
            else max(0.0, normalized_baseline - normalized_score)
        )
        async with self.repository.transaction_lock:
            await _begin(self.repository)
            try:
                asset = await self._require_asset(asset_id)
                allowed_status = {
                    "offline": LearningAssetStatus.CANDIDATE,
                    "shadow": LearningAssetStatus.SHADOW,
                    "canary": LearningAssetStatus.CANARY,
                }[normalized_phase]
                if asset.status != allowed_status:
                    raise LearningLifecycleError(
                        f"{normalized_phase} evaluation requires {allowed_status.value} status; "
                        f"asset is {asset.status.value}"
                    )
                effective_violations = list(clean_violations)
                if normalized_phase == "offline" and not asset.source_run_ids:
                    effective_violations.append("learning asset has no source run provenance")
                passed = (
                    normalized_score >= threshold
                    and int(sample_size) >= self.config.minimum_sample_size
                    and bool(clean_evidence)
                    and not effective_violations
                    and regression <= self.config.maximum_regression
                )
                evaluation = LearningAssetEvaluation(
                    asset_id=asset.asset_id,
                    phase=normalized_phase,
                    score=normalized_score,
                    baseline_score=normalized_baseline,
                    sample_size=max(0, int(sample_size)),
                    evidence=clean_evidence,
                    violations=effective_violations,
                    passed=passed,
                    metadata={
                        "threshold": threshold,
                        "maximum_regression": self.config.maximum_regression,
                        "observed_regression": regression,
                        **dict(metadata or {}),
                    },
                )
                await self.repository.save_learning_evaluation(evaluation, commit=False)
                if normalized_phase == "offline":
                    asset.status = (
                        LearningAssetStatus.EVALUATED
                        if passed
                        else LearningAssetStatus.REJECTED
                    )
                    asset.metadata["latest_evaluation_id"] = evaluation.evaluation_id
                    asset.metadata["latest_evaluation_passed"] = passed
                    await self.repository.save_learning_asset(asset, commit=False)
                await self.repository.db.commit()
                return evaluation
            except Exception:
                await self.repository.db.rollback()
                raise

    async def enter_shadow(self, asset_id: str) -> LearningAsset:
        async with self.repository.transaction_lock:
            await _begin(self.repository)
            try:
                asset = await self._require_asset(asset_id)
                if asset.status != LearningAssetStatus.EVALUATED:
                    raise LearningLifecycleError(
                        f"shadow requires evaluated status; asset is {asset.status.value}"
                    )
                offline = await self._latest_passed_evaluation(asset_id, "offline")
                if offline is None:
                    raise LearningLifecycleError("shadow requires a passing offline evaluation")
                asset.status = LearningAssetStatus.SHADOW
                asset.metadata["shadow_started_at"] = utc_now().isoformat()
                await self.repository.save_learning_asset(asset, commit=False)
                await self.repository.db.commit()
                return asset
            except Exception:
                await self.repository.db.rollback()
                raise

    async def enter_canary(self, asset_id: str) -> LearningAsset:
        async with self.repository.transaction_lock:
            await _begin(self.repository)
            try:
                asset = await self._require_asset(asset_id)
                if asset.status != LearningAssetStatus.SHADOW:
                    raise LearningLifecycleError(
                        f"canary requires shadow status; asset is {asset.status.value}"
                    )
                shadow = await self._latest_passed_evaluation(asset_id, "shadow")
                if shadow is None:
                    raise LearningLifecycleError("canary requires a passing shadow evaluation")
                asset.status = LearningAssetStatus.CANARY
                asset.metadata["canary_started_at"] = utc_now().isoformat()
                await self.repository.save_learning_asset(asset, commit=False)
                await self.repository.db.commit()
                return asset
            except Exception:
                await self.repository.db.rollback()
                raise

    async def promote(self, asset_id: str) -> LearningAsset:
        async with self.repository.transaction_lock:
            await _begin(self.repository)
            try:
                asset = await self._require_asset(asset_id)
                if asset.status != LearningAssetStatus.CANARY:
                    raise LearningLifecycleError(
                        f"promotion requires canary status; asset is {asset.status.value}"
                    )
                if not asset.source_run_ids:
                    raise LearningLifecycleError("promotion requires source run provenance")
                if asset.expires_at is not None and asset.expires_at <= utc_now():
                    raise LearningLifecycleError("cannot promote an expired learning asset")
                for phase in ("offline", "shadow", "canary"):
                    if await self._latest_passed_evaluation(asset_id, phase) is None:
                        raise LearningLifecycleError(
                            f"promotion requires a passing {phase} evaluation"
                        )
                active = await self.repository.list_learning_assets(
                    project_id=asset.project_id,
                    organization_id=asset.organization_id,
                    name=asset.name,
                    statuses=[LearningAssetStatus.PROMOTED.value],
                    limit=100,
                )
                for previous in active:
                    if previous.asset_id == asset.asset_id:
                        continue
                    previous.status = LearningAssetStatus.RETIRED
                    previous.metadata["superseded_by_asset_id"] = asset.asset_id
                    await self.repository.save_learning_asset(previous, commit=False)
                    if not asset.previous_asset_id:
                        asset.previous_asset_id = previous.asset_id
                asset.status = LearningAssetStatus.PROMOTED
                asset.metadata["promoted_at"] = utc_now().isoformat()
                await self.repository.save_learning_asset(asset, commit=False)
                await self.repository.db.commit()
            except Exception:
                await self.repository.db.rollback()
                raise
        return asset

    async def reject(self, asset_id: str, *, reason: str) -> LearningAsset:
        async with self.repository.transaction_lock:
            await _begin(self.repository)
            try:
                asset = await self._require_asset(asset_id)
                if asset.status in {
                    LearningAssetStatus.PROMOTED,
                    LearningAssetStatus.ROLLED_BACK,
                    LearningAssetStatus.RETIRED,
                }:
                    raise LearningLifecycleError(
                        f"cannot reject asset in {asset.status.value} status"
                    )
                asset.status = LearningAssetStatus.REJECTED
                asset.metadata["rejection_reason"] = str(reason or "unspecified")
                asset.metadata["rejected_at"] = utc_now().isoformat()
                await self.repository.save_learning_asset(asset, commit=False)
                await self.repository.db.commit()
                return asset
            except Exception:
                await self.repository.db.rollback()
                raise

    async def rollback(
        self,
        asset_id: str,
        *,
        reason: str,
    ) -> tuple[LearningAsset, LearningAsset | None]:
        async with self.repository.transaction_lock:
            await _begin(self.repository)
            try:
                asset = await self._require_asset(asset_id)
                if asset.status != LearningAssetStatus.PROMOTED:
                    raise LearningLifecycleError(
                        f"rollback requires promoted status; asset is {asset.status.value}"
                    )
                restored = (
                    await self.repository.get_learning_asset(asset.previous_asset_id)
                    if asset.previous_asset_id
                    else None
                )
                asset.status = LearningAssetStatus.ROLLED_BACK
                asset.metadata["rollback_reason"] = str(reason or "unspecified")
                asset.metadata["rolled_back_at"] = utc_now().isoformat()
                await self.repository.save_learning_asset(asset, commit=False)
                if restored is not None:
                    restored.status = LearningAssetStatus.PROMOTED
                    restored.metadata.pop("superseded_by_asset_id", None)
                    restored.metadata["restored_at"] = utc_now().isoformat()
                    await self.repository.save_learning_asset(restored, commit=False)
                await self.repository.db.commit()
            except Exception:
                await self.repository.db.rollback()
                raise
        return asset, restored

    async def list_active(
        self,
        *,
        project_id: str,
        organization_id: str = "",
        now: datetime | None = None,
    ) -> list[LearningAsset]:
        timestamp = now or utc_now()
        assets = await self.repository.list_learning_assets(
            project_id=project_id,
            organization_id=organization_id,
            statuses=[LearningAssetStatus.PROMOTED.value],
            limit=1000,
        )
        return [item for item in assets if item.expires_at is None or item.expires_at > timestamp]

    async def retire_expired(
        self,
        *,
        project_id: str,
        organization_id: str = "",
        now: datetime | None = None,
    ) -> list[LearningAsset]:
        timestamp = now or utc_now()
        active = await self.repository.list_learning_assets(
            project_id=project_id,
            organization_id=organization_id,
            statuses=[LearningAssetStatus.PROMOTED.value],
            limit=1000,
        )
        retired: list[LearningAsset] = []
        for asset in active:
            if asset.expires_at is None or asset.expires_at > timestamp:
                continue
            asset.status = LearningAssetStatus.RETIRED
            asset.metadata["retired_reason"] = "expired"
            asset.metadata["retired_at"] = timestamp.isoformat()
            retired.append(await self.repository.save_learning_asset(asset))
        return retired

    async def _require_asset(self, asset_id: str) -> LearningAsset:
        asset = await self.repository.get_learning_asset(asset_id)
        if asset is None:
            raise KeyError(f"learning asset not found: {asset_id}")
        return asset

    async def _latest_passed_evaluation(
        self,
        asset_id: str,
        phase: str,
    ) -> LearningAssetEvaluation | None:
        evaluations = await self.repository.list_learning_evaluations(asset_id)
        return next(
            (
                item
                for item in reversed(evaluations)
                if item.phase == phase and item.passed
            ),
            None,
        )


async def _begin(repository: OperationsRepository) -> None:
    async with repository.db.execute("BEGIN IMMEDIATE"):
        return
