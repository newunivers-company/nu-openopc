"""Outcome evaluation and regression gates for goal-bound runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from opc.core.config import OutcomeEvaluationConfig
from opc.operations.models import (
    AcceptanceCriterion,
    CriterionResult,
    GateStatus,
    GoalContract,
    GoalContractStatus,
    RoleOutcome,
    RunManifest,
    RunMetrics,
    RunScorecard,
    RunStatus,
    clamp_score,
    utc_now,
)
from opc.operations.repository import OperationsRepository


@dataclass(frozen=True)
class RegressionGateResult:
    passed: bool
    current_run_id: str
    baseline_run_id: str = ""
    regressions: dict[str, float] = field(default_factory=dict)
    violations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "current_run_id": self.current_run_id,
            "baseline_run_id": self.baseline_run_id,
            "regressions": dict(self.regressions),
            "violations": list(self.violations),
        }


class OutcomeEvaluator:
    """Compute a deterministic scorecard from an immutable goal contract."""

    def __init__(
        self,
        repository: OperationsRepository,
        config: OutcomeEvaluationConfig | None = None,
    ) -> None:
        self.repository = repository
        self.config = config or OutcomeEvaluationConfig()

    async def evaluate_run(
        self,
        run_id: str,
        *,
        criterion_scores: Mapping[str, float],
        evidence: Mapping[str, Sequence[str]] | None = None,
        criterion_notes: Mapping[str, str] | None = None,
        metrics: RunMetrics | Mapping[str, Any] | None = None,
        role_outcomes: Sequence[RoleOutcome | Mapping[str, Any]] | None = None,
        baseline_label: str = "",
        metadata: Mapping[str, Any] | None = None,
        persist: bool = True,
    ) -> RunScorecard:
        manifest = await self.repository.get_manifest(run_id)
        if manifest is None:
            raise KeyError(f"run manifest not found: {run_id}")
        goal = (
            await self.repository.get_goal_version(
                manifest.goal_id,
                manifest.goal_version,
            )
            if manifest.goal_version
            else await self.repository.get_goal(manifest.goal_id)
        )
        if goal is None:
            raise KeyError(
                f"goal contract version not found: "
                f"{manifest.goal_id}@{manifest.goal_version}"
            )
        scorecard = self.evaluate(
            goal,
            manifest,
            criterion_scores=criterion_scores,
            evidence=evidence,
            criterion_notes=criterion_notes,
            metrics=metrics,
            role_outcomes=role_outcomes,
            baseline_label=baseline_label,
            metadata=metadata,
        )
        if persist:
            await self.repository.save_scorecard(scorecard)
            if self.config.auto_complete_goal_on_pass and scorecard.gate_status == GateStatus.PASS:
                completed = await self._complete_goal_if_settled(goal, manifest, scorecard)
                if completed:
                    scorecard.metadata["goal_auto_completed"] = True
                    await self.repository.save_scorecard(scorecard)
        return scorecard

    async def _complete_goal_if_settled(
        self,
        evaluated_goal: GoalContract,
        manifest: RunManifest,
        scorecard: RunScorecard,
    ) -> bool:
        completion_requested = bool(
            evaluated_goal.metadata.get("auto_complete_on_pass", False)
            or manifest.metadata.get("complete_goal_on_pass", False)
        )
        if not completion_requested:
            return False
        latest = await self.repository.get_goal(evaluated_goal.goal_id)
        if latest is None or latest.status != GoalContractStatus.ACTIVE:
            return False
        if latest.version != manifest.goal_version:
            return False
        active_runs = await self.repository.list_manifests(
            goal_id=latest.goal_id,
            statuses=[
                RunStatus.PENDING.value,
                RunStatus.RUNNING.value,
                RunStatus.BLOCKED.value,
            ],
            limit=1000,
        )
        if active_runs:
            return False
        completed = GoalContract.from_dict(latest.to_dict())
        completed.version = latest.version + 1
        completed.status = GoalContractStatus.COMPLETED
        completed.metadata = {
            **dict(latest.metadata),
            "completion": {
                "source": "passing_run_scorecard",
                "run_id": manifest.run_id,
                "scorecard_id": scorecard.scorecard_id,
                "score": scorecard.total_score,
            },
        }
        await self.repository.save_goal(completed)
        return True

    def evaluate(
        self,
        goal: GoalContract,
        manifest: RunManifest,
        *,
        criterion_scores: Mapping[str, float],
        evidence: Mapping[str, Sequence[str]] | None = None,
        criterion_notes: Mapping[str, str] | None = None,
        metrics: RunMetrics | Mapping[str, Any] | None = None,
        role_outcomes: Sequence[RoleOutcome | Mapping[str, Any]] | None = None,
        baseline_label: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> RunScorecard:
        goal.validate()
        manifest.validate()
        if goal.goal_id != manifest.goal_id or goal.project_id != manifest.project_id:
            raise ValueError("goal contract and run manifest identity do not match")
        if manifest.goal_version and goal.version != manifest.goal_version:
            raise ValueError("run manifest goal_version does not match the supplied contract")

        run_metrics = metrics if isinstance(metrics, RunMetrics) else RunMetrics.from_dict(metrics)
        run_metrics.validate()
        evidence_map = {
            str(key): [str(item).strip() for item in values if str(item).strip()]
            for key, values in dict(evidence or {}).items()
        }
        notes_map = {str(key): str(value) for key, value in dict(criterion_notes or {}).items()}
        criterion_results = self._criterion_results(
            goal.acceptance_criteria,
            criterion_scores,
            evidence_map,
            notes_map,
        )
        violations = self._criterion_violations(goal.acceptance_criteria, criterion_results)
        quality_score = self._quality_score(goal.acceptance_criteria, criterion_results)
        evidence_score, evidence_violations = self._evidence_score(goal, criterion_results, evidence_map)
        violations.extend(evidence_violations)
        budget_score, budget_violations = self._budget_score(goal, run_metrics)
        violations.extend(budget_violations)
        reliability_score = self._reliability_score(run_metrics)
        autonomy_score = self._autonomy_score(goal, run_metrics)

        component_scores = {
            "quality": quality_score,
            "evidence": evidence_score,
            "budget": budget_score,
            "reliability": reliability_score,
            "autonomy": autonomy_score,
        }
        weights = self.config.normalized_weights()
        total_score = clamp_score(sum(component_scores[name] * weights[name] for name in component_scores))

        warnings: list[str] = []
        if manifest.status not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            warnings.append(f"run is not terminal: {manifest.status.value}")
            gate_status = GateStatus.REVIEW
        else:
            if manifest.status != RunStatus.COMPLETED:
                violations.append(f"run did not complete successfully: {manifest.status.value}")
            if total_score < self.config.minimum_total_score:
                violations.append(
                    "total score below threshold: "
                    f"{total_score:.4f} < {self.config.minimum_total_score:.4f}"
                )
            gate_status = GateStatus.FAIL if violations else GateStatus.PASS

        parsed_role_outcomes = [
            item if isinstance(item, RoleOutcome) else RoleOutcome.from_dict(item)
            for item in role_outcomes or []
        ]
        return RunScorecard(
            run_id=manifest.run_id,
            goal_id=goal.goal_id,
            project_id=goal.project_id,
            gate_status=gate_status,
            total_score=total_score,
            quality_score=quality_score,
            evidence_score=evidence_score,
            budget_score=budget_score,
            reliability_score=reliability_score,
            autonomy_score=autonomy_score,
            criterion_results=criterion_results,
            metrics=run_metrics,
            role_outcomes=parsed_role_outcomes,
            violations=_dedupe(violations),
            warnings=_dedupe(warnings),
            baseline_label=str(baseline_label or ""),
            metadata={
                "evaluation_weights": weights,
                "minimum_total_score": self.config.minimum_total_score,
                **dict(metadata or {}),
            },
            evaluated_at=utc_now(),
        )

    def compare(
        self,
        current: RunScorecard,
        baseline: RunScorecard | None,
        *,
        maximum_regression: float | None = None,
    ) -> RegressionGateResult:
        allowed_drop = self.config.maximum_regression if maximum_regression is None else maximum_regression
        return compare_scorecards(
            current,
            baseline,
            maximum_regression=allowed_drop,
        )

    async def gate_run(
        self,
        run_id: str,
        *,
        baseline_run_id: str | None = None,
        baseline_label: str | None = None,
        maximum_regression: float | None = None,
    ) -> RegressionGateResult:
        current = await self.repository.get_scorecard(run_id)
        if current is None:
            raise KeyError(f"run scorecard not found: {run_id}")
        baseline: RunScorecard | None = None
        if baseline_run_id:
            baseline = await self.repository.get_scorecard(baseline_run_id)
            if baseline is None:
                raise KeyError(f"baseline scorecard not found: {baseline_run_id}")
        elif baseline_label:
            candidates = await self.repository.list_scorecards(
                project_id=current.project_id,
                baseline_label=baseline_label,
                limit=20,
            )
            baseline = next((item for item in candidates if item.run_id != current.run_id), None)
            if baseline is None:
                raise KeyError(f"baseline label has no prior scorecard: {baseline_label}")
        return self.compare(current, baseline, maximum_regression=maximum_regression)

    @staticmethod
    def _criterion_results(
        criteria: Sequence[AcceptanceCriterion],
        scores: Mapping[str, float],
        evidence: Mapping[str, Sequence[str]],
        notes: Mapping[str, str],
    ) -> list[CriterionResult]:
        results: list[CriterionResult] = []
        for criterion in criteria:
            score = clamp_score(scores.get(criterion.criterion_id, 0.0))
            criterion_evidence = list(evidence.get(criterion.criterion_id, ()))
            passed = score >= criterion.minimum_score
            if criterion.evidence_required and not criterion_evidence:
                passed = False
            results.append(
                CriterionResult(
                    criterion_id=criterion.criterion_id,
                    score=score,
                    passed=passed,
                    evidence=criterion_evidence,
                    notes=str(notes.get(criterion.criterion_id, "")),
                )
            )
        return results

    @staticmethod
    def _criterion_violations(
        criteria: Sequence[AcceptanceCriterion],
        results: Sequence[CriterionResult],
    ) -> list[str]:
        result_by_id = {item.criterion_id: item for item in results}
        violations: list[str] = []
        for criterion in criteria:
            result = result_by_id[criterion.criterion_id]
            if criterion.required and result.score < criterion.minimum_score:
                violations.append(
                    f"required criterion {criterion.criterion_id!r} scored "
                    f"{result.score:.4f} below {criterion.minimum_score:.4f}"
                )
            if criterion.required and criterion.evidence_required and not result.evidence:
                violations.append(f"required criterion {criterion.criterion_id!r} has no evidence")
        return violations

    @staticmethod
    def _quality_score(
        criteria: Sequence[AcceptanceCriterion],
        results: Sequence[CriterionResult],
    ) -> float:
        result_by_id = {item.criterion_id: item for item in results}
        total_weight = sum(item.weight for item in criteria)
        if total_weight <= 0:
            return 0.0
        return clamp_score(
            sum(result_by_id[item.criterion_id].score * item.weight for item in criteria)
            / total_weight
        )

    @staticmethod
    def _evidence_score(
        goal: GoalContract,
        results: Sequence[CriterionResult],
        evidence: Mapping[str, Sequence[str]],
    ) -> tuple[float, list[str]]:
        required_results = [
            result
            for criterion, result in zip(goal.acceptance_criteria, results, strict=True)
            if criterion.evidence_required
        ]
        criterion_checks = [bool(item.evidence) for item in required_results]
        global_checks: list[bool] = []
        violations: list[str] = []
        for requirement in goal.evidence_requirements:
            satisfied = bool(evidence.get(requirement))
            global_checks.append(satisfied)
            if not satisfied:
                violations.append(f"goal evidence requirement {requirement!r} is missing")
        checks = [*criterion_checks, *global_checks]
        return (sum(checks) / len(checks) if checks else 1.0), violations

    @staticmethod
    def _budget_score(goal: GoalContract, metrics: RunMetrics) -> tuple[float, list[str]]:
        checks = (
            ("cost_usd", metrics.cost_usd, goal.budget.max_cost_usd),
            ("duration_seconds", metrics.duration_seconds, goal.budget.max_duration_seconds),
            ("tokens", metrics.tokens, goal.budget.max_tokens),
            ("interventions", metrics.interventions, goal.budget.max_interventions),
            ("rework_cycles", metrics.rework_cycles, goal.budget.max_rework_cycles),
            ("failed_attempts", metrics.failed_attempts, goal.budget.max_failed_attempts),
        )
        scores: list[float] = []
        violations: list[str] = []
        for name, actual, maximum in checks:
            if maximum is None:
                continue
            if actual <= maximum:
                scores.append(1.0)
                continue
            score = 0.0 if maximum == 0 else float(maximum) / float(actual)
            scores.append(clamp_score(score))
            violations.append(f"budget {name} exceeded: {actual} > {maximum}")
        return (sum(scores) / len(scores) if scores else 1.0), violations

    @staticmethod
    def _reliability_score(metrics: RunMetrics) -> float:
        attempt_score = (
            1.0
            if metrics.total_attempts == 0
            else 1.0 - (metrics.failed_attempts / metrics.total_attempts)
        )
        resume_score = (
            1.0
            if metrics.resume_attempts == 0
            else metrics.resume_successes / metrics.resume_attempts
        )
        return clamp_score((0.75 * attempt_score) + (0.25 * resume_score))

    @staticmethod
    def _autonomy_score(goal: GoalContract, metrics: RunMetrics) -> float:
        maximum = goal.budget.max_interventions
        if maximum is None:
            return 1.0 / (1.0 + metrics.interventions)
        if maximum == 0:
            return 1.0 if metrics.interventions == 0 else 0.0
        return clamp_score(1.0 - (metrics.interventions / (maximum + 1.0)))


def compare_scorecards(
    current: RunScorecard,
    baseline: RunScorecard | None,
    *,
    maximum_regression: float = 0.05,
) -> RegressionGateResult:
    """Apply the same fail-closed regression rule in runtime and CI scripts."""
    allowed_drop = max(0.0, float(maximum_regression))
    violations: list[str] = []
    regressions: dict[str, float] = {}
    if current.gate_status != GateStatus.PASS:
        violations.append(f"current scorecard gate is {current.gate_status.value}")
    if baseline is not None:
        for name in (
            "total_score",
            "quality_score",
            "evidence_score",
            "budget_score",
            "reliability_score",
            "autonomy_score",
        ):
            drop = float(getattr(baseline, name)) - float(getattr(current, name))
            regressions[name] = round(drop, 8)
            if drop > allowed_drop:
                violations.append(
                    f"{name} regressed by {drop:.4f}; allowed maximum is {allowed_drop:.4f}"
                )
    return RegressionGateResult(
        passed=not violations,
        current_run_id=current.run_id,
        baseline_run_id=baseline.run_id if baseline else "",
        regressions=regressions,
        violations=tuple(violations),
    )


def _dedupe(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in values if str(item).strip()))
