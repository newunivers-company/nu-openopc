"""Deterministic Task-versus-Company execution-mode advice.

The advisor does not start work or change project defaults.  It makes the
product contract explainable before execution: bounded single-owner work stays
in Task Mode, while decomposition, parallel ownership, independent review, and
integration contribute explicit evidence for Company Mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


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

    company_benefit_score = max(
        0, min(10, sum(int(item["weight"]) for item in factors))
    )
    if blockers:
        recommendation = "clarify"
        confidence = "high"
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
        "decision_threshold": 5,
        "blockers": blockers,
        "factors": factors,
        "recommended_controls": controls,
        "summary": _summary(recommendation, blockers),
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


def _summary(
    recommendation: str, blockers: list[dict[str, str]]
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
