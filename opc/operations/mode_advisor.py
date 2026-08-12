"""Deterministic Task-versus-Company execution-mode advice.

The advisor does not start work or change project defaults.  It makes the
product contract explainable before execution: bounded single-owner work stays
in Task Mode, while decomposition, parallel ownership, independent review, and
integration contribute explicit evidence for Company Mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Any, Mapping


_TRUSTED_OUTCOME_AUTHORITIES = {"human_confirmed", "independent_judge"}
_PROVISIONAL_MAX_DURATION_RATIO = 3.0
_PROVISIONAL_MAX_EXTERNAL_CALL_RATIO = 8.0


@dataclass(frozen=True)
class ModeOutcomeObservation:
    """One trusted Task/Company comparison for a similar workload."""

    workload_key: str
    authority: str
    task_quality: float
    company_quality: float
    task_duration_seconds: float
    company_duration_seconds: float
    task_external_calls: int
    company_external_calls: int

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModeOutcomeObservation":
        observation = cls(
            workload_key=str(data.get("workload_key", "") or "").strip().lower(),
            authority=str(data.get("authority", "") or "").strip().lower(),
            task_quality=float(data.get("task_quality", 0.0) or 0.0),
            company_quality=float(data.get("company_quality", 0.0) or 0.0),
            task_duration_seconds=float(
                data.get("task_duration_seconds", 0.0) or 0.0
            ),
            company_duration_seconds=float(
                data.get("company_duration_seconds", 0.0) or 0.0
            ),
            task_external_calls=int(data.get("task_external_calls", 0) or 0),
            company_external_calls=int(
                data.get("company_external_calls", 0) or 0
            ),
        )
        observation.validate()
        return observation

    def validate(self) -> None:
        if not self.workload_key:
            raise ValueError("mode outcome observation requires workload_key")
        if self.authority not in {
            *_TRUSTED_OUTCOME_AUTHORITIES,
            "llm_draft",
            "simulation",
        }:
            raise ValueError("unsupported mode outcome observation authority")
        for name, value in (
            ("task_quality", self.task_quality),
            ("company_quality", self.company_quality),
        ):
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        for name, value in (
            ("task_duration_seconds", self.task_duration_seconds),
            ("company_duration_seconds", self.company_duration_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.task_external_calls < 0 or self.company_external_calls < 0:
            raise ValueError("external call counts must be non-negative")

    @property
    def trusted(self) -> bool:
        return self.authority in _TRUSTED_OUTCOME_AUTHORITIES


@dataclass(frozen=True)
class ModeAssessmentRequest:
    goal: str
    deliverable_count: int
    role_count: int
    independent_workstreams: int
    dependency_count: int
    bounded_scope: bool
    input_complete: bool
    requirements_stable: bool
    independent_review_required: bool = False
    final_integration_required: bool = False
    risk_level: str = "low"
    estimated_duration_minutes: int | None = None
    workload_key: str = ""
    outcome_observations: tuple[ModeOutcomeObservation, ...] = ()
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModeAssessmentRequest":
        request = cls(
            goal=str(data.get("goal", "") or "").strip(),
            deliverable_count=int(data.get("deliverable_count", 0) or 0),
            role_count=int(data.get("role_count", 0) or 0),
            independent_workstreams=int(
                data.get("independent_workstreams", 0) or 0
            ),
            dependency_count=int(data.get("dependency_count", 0) or 0),
            bounded_scope=_required_bool(data, "bounded_scope"),
            input_complete=_required_bool(data, "input_complete"),
            requirements_stable=_required_bool(data, "requirements_stable"),
            independent_review_required=_optional_bool(
                data, "independent_review_required", False
            ),
            final_integration_required=_optional_bool(
                data, "final_integration_required", False
            ),
            risk_level=str(data.get("risk_level", "low") or "low")
            .strip()
            .lower(),
            estimated_duration_minutes=(
                None
                if data.get("estimated_duration_minutes") is None
                else int(data["estimated_duration_minutes"])
            ),
            workload_key=str(data.get("workload_key", "") or "").strip().lower(),
            outcome_observations=tuple(
                ModeOutcomeObservation.from_dict(item)
                for item in data.get("outcome_observations", []) or []
                if isinstance(item, Mapping)
            ),
            schema_version=int(data.get("schema_version", 1) or 1),
        )
        request.validate()
        return request

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ValueError("mode assessment schema_version must be 1")
        if not self.goal:
            raise ValueError("mode assessment requires a goal")
        for name, value in (
            ("deliverable_count", self.deliverable_count),
            ("role_count", self.role_count),
            ("independent_workstreams", self.independent_workstreams),
            ("dependency_count", self.dependency_count),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.deliverable_count < 1:
            raise ValueError("deliverable_count must be positive")
        if self.role_count < 1:
            raise ValueError("role_count must be positive")
        if self.independent_workstreams < 1:
            raise ValueError("independent_workstreams must be positive")
        if self.risk_level not in {"low", "medium", "high"}:
            raise ValueError("risk_level must be low, medium, or high")
        if (
            self.estimated_duration_minutes is not None
            and self.estimated_duration_minutes < 1
        ):
            raise ValueError("estimated_duration_minutes must be positive")
        if self.outcome_observations and not self.workload_key:
            raise ValueError(
                "workload_key is required when outcome observations are supplied"
            )


def assess_execution_mode(request: ModeAssessmentRequest) -> dict[str, Any]:
    """Return an explainable recommendation without causing execution."""

    request.validate()
    blockers: list[dict[str, str]] = []
    if not request.input_complete:
        blockers.append(
            {
                "code": "input_incomplete",
                "message": "Required source material or fixture evidence is incomplete.",
            }
        )
    if not request.requirements_stable:
        blockers.append(
            {
                "code": "requirements_unstable",
                "message": "Acceptance boundaries are not stable enough to assign safely.",
            }
        )

    factors: list[dict[str, Any]] = []

    def factor(code: str, weight: int, message: str) -> None:
        factors.append({"code": code, "weight": weight, "message": message})

    if request.independent_workstreams >= 2:
        factor(
            "parallel_workstreams",
            3 if request.independent_workstreams >= 3 else 2,
            f"{request.independent_workstreams} independent workstreams can run in parallel.",
        )
    if request.role_count >= 2:
        factor(
            "specialized_roles",
            2 if request.role_count >= 3 else 1,
            f"{request.role_count} distinct accountable roles are expected.",
        )
    if request.independent_review_required:
        factor(
            "independent_review",
            2,
            "The deliverable requires review independent from its executor.",
        )
    if request.final_integration_required:
        factor(
            "final_integration",
            2,
            "Multiple outputs require one accountable final integrator.",
        )
    if request.dependency_count >= 2:
        factor(
            "dependency_graph",
            1,
            f"{request.dependency_count} dependencies require ordered handoffs.",
        )
    if request.deliverable_count >= 3:
        factor(
            "multiple_deliverables",
            1,
            f"{request.deliverable_count} deliverables increase coordination value.",
        )
    if request.risk_level == "high":
        factor(
            "high_risk",
            1,
            "High-risk work benefits from explicit ownership and review gates.",
        )
    if (
        request.bounded_scope
        and request.role_count == 1
        and request.independent_workstreams == 1
    ):
        factor(
            "bounded_single_owner",
            -3,
            "The scope is bounded and has one natural execution owner.",
        )
    if (
        request.estimated_duration_minutes is not None
        and request.estimated_duration_minutes <= 30
        and request.deliverable_count == 1
    ):
        factor(
            "short_direct_path",
            -1,
            "The estimated duration and single deliverable favor a direct path.",
        )

    structural_company_benefit_score = max(
        0, min(10, sum(int(item["weight"]) for item in factors))
    )
    observed_evidence = _observed_evidence(request)
    evidence_veto = False
    if observed_evidence["matched_trusted_pairs"]:
        quality_delta = float(observed_evidence["mean_quality_delta"])
        within_budget = bool(observed_evidence["within_provisional_budget"])
        if quality_delta < 0 or (not within_budget and quality_delta < 0.03):
            factor(
                "observed_company_underperformance",
                -4,
                (
                    "Trusted matched outcomes show Company Mode does not repay "
                    "its observed coordination cost for this workload."
                ),
            )
            evidence_veto = True
        elif quality_delta >= 0.03 and within_budget:
            factor(
                "observed_company_lift",
                2,
                "Trusted matched outcomes show material Company quality lift within budget.",
            )
        elif within_budget:
            factor(
                "observed_company_non_regression",
                1,
                "Trusted matched outcomes show Company non-regression within budget.",
            )
    company_benefit_score = max(
        0, min(10, sum(int(item["weight"]) for item in factors))
    )
    if blockers:
        recommendation = "clarify"
        confidence = "high"
    elif evidence_veto:
        recommendation = "task"
        confidence = (
            "high"
            if int(observed_evidence["matched_trusted_pairs"]) >= 3
            else "medium"
        )
    elif company_benefit_score >= 5:
        recommendation = "company"
        confidence = "high" if company_benefit_score >= 7 else "medium"
    else:
        recommendation = "task"
        confidence = "high" if company_benefit_score <= 2 else "medium"

    controls = _recommended_controls(request, recommendation)
    return {
        "schema_version": 1,
        "recommendation": recommendation,
        "confidence": confidence,
        "advisory_only": True,
        "company_benefit_score": company_benefit_score,
        "structural_company_benefit_score": structural_company_benefit_score,
        "decision_threshold": 5,
        "evidence_veto": evidence_veto,
        "observed_evidence": observed_evidence,
        "blockers": blockers,
        "factors": factors,
        "recommended_controls": controls,
        "summary": _summary(
            recommendation,
            blockers,
            evidence_veto=evidence_veto,
        ),
    }


def _recommended_controls(
    request: ModeAssessmentRequest, recommendation: str
) -> list[str]:
    if recommendation == "clarify":
        return [
            "bind all required inputs and their provenance",
            "define deliverables and acceptance criteria before execution",
            "rerun the deterministic mode assessment",
        ]
    controls = ["versioned goal contract", "evidence-backed final scorecard"]
    if recommendation == "task":
        controls.insert(0, "one explicit execution owner")
        return controls
    controls.insert(0, "work-item dependency graph with accountable role owners")
    if request.independent_review_required or request.risk_level == "high":
        controls.append("independent reviewer distinct from the executor")
    if request.final_integration_required:
        controls.append("one final integrator accountable for the combined delivery")
    controls.extend(["bounded intervention and rework budget", "durable recovery checkpoints"])
    return controls


def _observed_evidence(request: ModeAssessmentRequest) -> dict[str, Any]:
    matching = [
        item
        for item in request.outcome_observations
        if item.trusted and item.workload_key == request.workload_key
    ]
    ignored = len(request.outcome_observations) - len(matching)
    if not matching:
        return {
            "workload_key": request.workload_key,
            "matched_trusted_pairs": 0,
            "ignored_observations": ignored,
            "confidence": "none",
            "mean_quality_delta": 0.0,
            "mean_duration_ratio": None,
            "mean_external_call_ratio": None,
            "within_provisional_budget": False,
            "provisional_budget": {
                "maximum_duration_ratio": _PROVISIONAL_MAX_DURATION_RATIO,
                "maximum_external_call_ratio": (
                    _PROVISIONAL_MAX_EXTERNAL_CALL_RATIO
                ),
            },
            "expected": {},
        }

    duration_ratios = [
        item.company_duration_seconds / item.task_duration_seconds
        for item in matching
    ]
    call_ratios = [
        item.company_external_calls / max(1, item.task_external_calls)
        for item in matching
    ]
    quality_deltas = [
        item.company_quality - item.task_quality for item in matching
    ]
    mean_duration_ratio = fmean(duration_ratios)
    mean_call_ratio = fmean(call_ratios)
    sample_count = len(matching)
    return {
        "workload_key": request.workload_key,
        "matched_trusted_pairs": sample_count,
        "ignored_observations": ignored,
        "confidence": (
            "high" if sample_count >= 5 else "medium" if sample_count >= 3 else "low"
        ),
        "mean_quality_delta": round(fmean(quality_deltas), 6),
        "mean_duration_ratio": round(mean_duration_ratio, 6),
        "mean_external_call_ratio": round(mean_call_ratio, 6),
        "within_provisional_budget": bool(
            mean_duration_ratio <= _PROVISIONAL_MAX_DURATION_RATIO
            and mean_call_ratio <= _PROVISIONAL_MAX_EXTERNAL_CALL_RATIO
        ),
        "provisional_budget": {
            "maximum_duration_ratio": _PROVISIONAL_MAX_DURATION_RATIO,
            "maximum_external_call_ratio": _PROVISIONAL_MAX_EXTERNAL_CALL_RATIO,
        },
        "expected": {
            "task_quality": _range(item.task_quality for item in matching),
            "company_quality": _range(item.company_quality for item in matching),
            "task_duration_seconds": _range(
                item.task_duration_seconds for item in matching
            ),
            "company_duration_seconds": _range(
                item.company_duration_seconds for item in matching
            ),
            "task_external_calls": _range(
                float(item.task_external_calls) for item in matching
            ),
            "company_external_calls": _range(
                float(item.company_external_calls) for item in matching
            ),
        },
    }


def _range(values: Any) -> dict[str, float]:
    samples = [float(value) for value in values]
    return {
        "minimum": round(min(samples), 6),
        "mean": round(fmean(samples), 6),
        "maximum": round(max(samples), 6),
    }


def _summary(
    recommendation: str,
    blockers: list[dict[str, str]],
    *,
    evidence_veto: bool = False,
) -> str:
    if recommendation == "clarify":
        return (
            "Do not start Task or Company Mode until the input and acceptance "
            f"contract is complete ({len(blockers)} blocker(s))."
        )
    if recommendation == "company":
        return (
            "Company Mode is recommended because coordination, independent "
            "ownership, review, or integration is expected to add material value."
        )
    if evidence_veto:
        return (
            "Task Mode is recommended because trusted matched outcomes show "
            "that the current Company topology does not repay its coordination cost."
        )
    return (
        "Task Mode is recommended because one execution owner can take the "
        "bounded work directly without paying a coordination tax."
    )


def _required_bool(data: Mapping[str, Any], name: str) -> bool:
    if name not in data or not isinstance(data[name], bool):
        raise ValueError(f"mode assessment {name} must be an explicit boolean")
    return bool(data[name])


def _optional_bool(
    data: Mapping[str, Any], name: str, default: bool
) -> bool:
    if name not in data:
        return default
    if not isinstance(data[name], bool):
        raise ValueError(f"mode assessment {name} must be a boolean")
    return bool(data[name])
