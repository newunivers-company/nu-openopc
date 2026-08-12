"""Bounded, explainable execution policy for lean Company Mode runs.

This module is intentionally independent from the scheduler state machine.
It gives intake, tests, and the runtime one place to enforce staffing
readiness and coordination budgets without adding more policy branches to
``company_mode.py``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class LeanCompanyPolicy:
    """Fail-closed limits for expensive Company coordination loops."""

    max_active_roles: int = 6
    max_review_reworks: int = 2
    max_gate_retries: int = 2
    require_non_fallback_deciders: bool = False
    enforce_role_cap: bool = False

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any] | None) -> "LeanCompanyPolicy":
        source = dict(metadata or {})
        nested = dict(source.get("company_execution_policy", {}) or {})

        def positive(name: str, default: int) -> int:
            value = int(nested.get(name, default) or default)
            if value < 1:
                raise ValueError(f"company execution policy {name} must be positive")
            return value

        return cls(
            max_active_roles=positive("max_active_roles", 6),
            max_review_reworks=positive("max_review_reworks", 2),
            max_gate_retries=positive("max_gate_retries", 2),
            require_non_fallback_deciders=bool(
                nested.get("require_non_fallback_deciders", False)
            ),
            enforce_role_cap=bool(nested.get("enforce_role_cap", False)),
        )


def assess_company_readiness(
    plan: Any,
    tasks: Sequence[Any],
    *,
    policy: LeanCompanyPolicy | None = None,
) -> dict[str, Any]:
    """Assess role economy and final-decision staffing without invoking an LLM."""

    effective = policy or _policy_from_run(plan, tasks)
    projections = list(getattr(plan, "projections", []) or [])
    projected_role_ids = {
        str(getattr(item, "role_id", "") or "").strip()
        for item in projections
        if str(getattr(item, "role_id", "") or "").strip()
    }
    task_role_ids = {
        str(getattr(task, "assigned_to", "") or "").strip()
        for task in tasks
        if str(getattr(task, "assigned_to", "") or "").strip()
    }
    accountable_role_ids = sorted(projected_role_ids | task_role_ids)
    active_role_ids = sorted(
        {
            str(getattr(item, "role_id", "") or "").strip()
            for item in projections
            if str(getattr(item, "role_id", "") or "").strip()
            and str(getattr(item, "turn_type", "") or "").strip().lower()
            not in {"report", "review"}
        }
        | {
            str(getattr(task, "assigned_to", "") or "").strip()
            for task in tasks
            if str(getattr(task, "assigned_to", "") or "").strip()
            and str(
                dict(getattr(task, "metadata", {}) or {}).get(
                    "work_item_turn_type", ""
                )
                or ""
            )
            .strip()
            .lower()
            not in {"report", "review"}
        }
    )
    decider_role_ids = _decision_role_ids(plan, tasks)
    seats = _runtime_seats(plan, tasks)
    fallback_role_ids = sorted(
        {
            str(item.get("role_id", "") or "").strip()
            for item in seats
            if _seat_is_fallback(item)
            and str(item.get("role_id", "") or "").strip()
        }
    )
    fallback_decider_role_ids = sorted(set(decider_role_ids) & set(fallback_role_ids))

    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if len(active_role_ids) > effective.max_active_roles:
        issue = {
            "code": "active_role_cap_exceeded",
            "message": (
                f"{len(active_role_ids)} active roles exceed the lean-company cap "
                f"of {effective.max_active_roles}."
            ),
            "role_ids": active_role_ids,
        }
        (blockers if effective.enforce_role_cap else warnings).append(issue)
    if fallback_decider_role_ids:
        issue = {
            "code": "fallback_final_decider",
            "message": (
                "Final integration or decision ownership is assigned to fallback "
                "staff without a proven specialist contract."
            ),
            "role_ids": fallback_decider_role_ids,
        }
        (
            blockers
            if effective.require_non_fallback_deciders
            else warnings
        ).append(issue)
    if not accountable_role_ids:
        blockers.append(
            {
                "code": "no_accountable_roles",
                "message": "Company execution has no accountable role owner.",
                "role_ids": [],
            }
        )

    return {
        "schema_version": 1,
        "ready": not blockers,
        "strict": bool(
            effective.require_non_fallback_deciders or effective.enforce_role_cap
        ),
        "policy": asdict(effective),
        "active_role_ids": active_role_ids,
        "active_role_count": len(active_role_ids),
        "accountable_role_ids": accountable_role_ids,
        "accountable_role_count": len(accountable_role_ids),
        "decider_role_ids": decider_role_ids,
        "fallback_role_ids": fallback_role_ids,
        "fallback_decider_role_ids": fallback_decider_role_ids,
        "blockers": blockers,
        "warnings": warnings,
        "recommended_active_role_ids": (
            active_role_ids or accountable_role_ids
        )[: effective.max_active_roles],
    }


def apply_lean_company_policy(
    plan: Any,
    tasks: Sequence[Any],
) -> dict[str, Any]:
    """Apply bounded rework defaults and return the sealed readiness record."""

    policy = _policy_from_run(plan, tasks)
    assessment = assess_company_readiness(plan, tasks, policy=policy)
    if not assessment["ready"]:
        reasons = "; ".join(item["message"] for item in assessment["blockers"])
        raise ValueError(f"Company execution readiness failed: {reasons}")

    for projection in list(getattr(plan, "projections", []) or []):
        metadata = dict(getattr(projection, "metadata", {}) or {})
        metadata.setdefault("max_review_reworks", policy.max_review_reworks)
        projection.metadata = metadata
        gate = getattr(projection, "gate_policy", None)
        if gate is not None:
            gate.max_retries = min(
                max(1, int(getattr(gate, "max_retries", 1) or 1)),
                policy.max_gate_retries,
            )
    plan_metadata = dict(getattr(plan, "metadata", {}) or {})
    plan_metadata["company_execution_policy"] = asdict(policy)
    plan_metadata["company_readiness"] = assessment
    plan.metadata = plan_metadata
    for task in tasks:
        task_metadata = dict(getattr(task, "metadata", {}) or {})
        task_metadata.setdefault("max_review_reworks", policy.max_review_reworks)
        task_metadata["company_execution_policy"] = asdict(policy)
        task_metadata["company_readiness"] = assessment
        task.metadata = task_metadata
    return assessment


def company_execution_telemetry(
    plan: Any,
    *,
    duration_seconds: float,
    status: str,
) -> dict[str, Any]:
    """Summarize the coordination tax in a stable, persistence-safe shape."""

    projections = list(getattr(plan, "projections", []) or [])
    roles = {
        str(getattr(item, "role_id", "") or "").strip()
        for item in projections
        if str(getattr(item, "role_id", "") or "").strip()
    }
    turn_counts: dict[str, int] = {}
    for item in projections:
        turn = str(getattr(item, "turn_type", "") or "execute").strip().lower()
        turn_counts[turn] = turn_counts.get(turn, 0) + 1
    return {
        "schema_version": 1,
        "status": str(status or "unknown"),
        "duration_seconds": round(max(0.0, float(duration_seconds)), 6),
        "projected_work_items": len(projections),
        "active_roles": len(roles),
        "turn_counts": dict(sorted(turn_counts.items())),
        "policy": dict(
            dict(getattr(plan, "metadata", {}) or {}).get(
                "company_execution_policy", {}
            )
            or {}
        ),
    }


def _policy_from_run(plan: Any, tasks: Sequence[Any]) -> LeanCompanyPolicy:
    metadata: dict[str, Any] = dict(getattr(plan, "metadata", {}) or {})
    for task in tasks:
        task_policy = dict(
            dict(getattr(task, "metadata", {}) or {}).get(
                "company_execution_policy", {}
            )
            or {}
        )
        if task_policy:
            metadata["company_execution_policy"] = task_policy
            break
    return LeanCompanyPolicy.from_metadata(metadata)


def _decision_role_ids(plan: Any, tasks: Sequence[Any]) -> list[str]:
    metadata = dict(getattr(plan, "metadata", {}) or {})
    roles = {
        str(metadata.get(key, "") or "").strip()
        for key in (
            "decision_owner_role_id",
            "final_integrator_role_id",
            "delivery_owner_role_id",
        )
    }
    root_id = str(getattr(plan, "root_projection_id", "") or "").strip()
    for projection in list(getattr(plan, "projections", []) or []):
        projection_id = str(getattr(projection, "projection_id", "") or "").strip()
        turn_type = str(getattr(projection, "turn_type", "") or "").strip().lower()
        delivery = getattr(projection, "delivery_policy", None)
        authoritative = bool(getattr(delivery, "authoritative_output", False))
        if projection_id == root_id or turn_type in {"deliver", "integrate"} or authoritative:
            roles.add(str(getattr(projection, "role_id", "") or "").strip())
    for task in tasks:
        task_metadata = dict(getattr(task, "metadata", {}) or {})
        if bool(task_metadata.get("authoritative_output")):
            roles.add(str(getattr(task, "assigned_to", "") or "").strip())
    return sorted(role for role in roles if role)


def _runtime_seats(plan: Any, tasks: Sequence[Any]) -> list[dict[str, Any]]:
    topologies: list[Mapping[str, Any]] = []
    plan_topology = dict(getattr(plan, "metadata", {}) or {}).get("runtime_topology")
    if isinstance(plan_topology, Mapping):
        topologies.append(plan_topology)
    for task in tasks:
        topology = dict(getattr(task, "metadata", {}) or {}).get("runtime_topology")
        if isinstance(topology, Mapping):
            topologies.append(topology)
    seats: list[dict[str, Any]] = []
    for topology in topologies:
        for item in topology.get("seats", []) or []:
            if isinstance(item, Mapping):
                seats.append(dict(item))
    return seats


def _seat_is_fallback(seat: Mapping[str, Any]) -> bool:
    assignment = dict(seat.get("employee_assignment", {}) or {})
    metadata = {
        **dict(assignment.get("metadata", {}) or {}),
        **dict(seat.get("metadata", {}) or {}),
    }
    return bool(
        metadata.get("is_fallback_employee")
        or metadata.get("is_default_employee")
    )
