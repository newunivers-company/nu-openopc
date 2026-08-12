from __future__ import annotations

import asyncio
from contextlib import nullcontext

import pytest

from opc.core.models import Task
from opc.layer2_organization.company_execution_lifecycle import (
    run_governed_company_execution,
)
from opc.layer2_organization.company_policy import (
    LeanCompanyPolicy,
    apply_lean_company_policy,
    assess_company_readiness,
    company_execution_telemetry,
)
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemDeliveryPolicy,
    WorkItemGatePolicy,
    WorkItemProjectionSpec,
)


def _projection(
    projection_id: str,
    role_id: str,
    *,
    authoritative: bool = False,
) -> WorkItemProjectionSpec:
    return WorkItemProjectionSpec(
        projection_id=projection_id,
        turn_type="integrate" if authoritative else "execute",
        role_id=role_id,
        title=projection_id,
        gate_policy=WorkItemGatePolicy(max_retries=5),
        delivery_policy=WorkItemDeliveryPolicy(
            authoritative_output=authoritative
        ),
    )


def _task(*, strict: bool = False) -> Task:
    return Task(
        title="Governed company run",
        assigned_to="ceo",
        metadata={
            "company_execution_policy": {
                "require_non_fallback_deciders": strict,
                "max_review_reworks": 2,
            },
            "runtime_topology": {
                "seats": [
                    {
                        "role_id": "ceo",
                        "employee_assignment": {
                            "metadata": {"is_fallback_employee": True}
                        },
                    },
                    {
                        "role_id": "engineer",
                        "employee_assignment": {"metadata": {}},
                    },
                ]
            },
        },
    )


def test_readiness_surfaces_fallback_final_decider_without_silent_block() -> None:
    plan = CompanyWorkItemRuntimePlan(
        root_projection_id="delivery",
        projections=[
            _projection("implementation", "engineer"),
            _projection("delivery", "ceo", authoritative=True),
        ],
    )
    report = assess_company_readiness(plan, [_task()])

    assert report["ready"] is True
    assert report["fallback_decider_role_ids"] == ["ceo"]
    assert report["warnings"][0]["code"] == "fallback_final_decider"


def test_strict_readiness_blocks_fallback_final_decider() -> None:
    plan = CompanyWorkItemRuntimePlan(
        root_projection_id="delivery",
        projections=[_projection("delivery", "ceo", authoritative=True)],
    )

    with pytest.raises(ValueError, match="fallback staff"):
        apply_lean_company_policy(plan, [_task(strict=True)])


def test_lean_policy_caps_rework_and_gate_retries() -> None:
    task = _task()
    task.metadata["runtime_topology"]["seats"][0][
        "employee_assignment"
    ]["metadata"] = {}
    projection = _projection("delivery", "ceo", authoritative=True)
    plan = CompanyWorkItemRuntimePlan(
        root_projection_id="delivery",
        projections=[projection],
    )

    report = apply_lean_company_policy(plan, [task])

    assert report["ready"] is True
    assert projection.metadata["max_review_reworks"] == 2
    assert projection.gate_policy is not None
    assert projection.gate_policy.max_retries == 2
    assert task.metadata["max_review_reworks"] == 2


def test_role_cap_can_warn_or_fail_closed() -> None:
    plan = CompanyWorkItemRuntimePlan(
        root_projection_id="p0",
        projections=[_projection(f"p{i}", f"role-{i}") for i in range(3)],
    )
    warned = assess_company_readiness(
        plan,
        [],
        policy=LeanCompanyPolicy(max_active_roles=2),
    )
    blocked = assess_company_readiness(
        plan,
        [],
        policy=LeanCompanyPolicy(max_active_roles=2, enforce_role_cap=True),
    )

    assert warned["ready"] is True
    assert warned["warnings"][0]["code"] == "active_role_cap_exceeded"
    assert blocked["ready"] is False
    assert blocked["blockers"][0]["code"] == "active_role_cap_exceeded"


def test_execution_telemetry_reports_coordination_shape() -> None:
    plan = CompanyWorkItemRuntimePlan(
        projections=[
            _projection("build", "engineer"),
            _projection("delivery", "ceo", authoritative=True),
        ],
        metadata={"company_execution_policy": {"max_review_reworks": 2}},
    )
    report = company_execution_telemetry(
        plan,
        duration_seconds=12.3456789,
        status="completed",
    )

    assert report["duration_seconds"] == 12.345679
    assert report["projected_work_items"] == 2
    assert report["active_roles"] == 2
    assert report["turn_counts"] == {"execute": 1, "integrate": 1}


def test_readiness_accepts_durable_task_owner_when_projection_is_absent() -> None:
    task = _task()
    task.metadata["work_item_turn_type"] = "deliver"

    report = assess_company_readiness(CompanyWorkItemRuntimePlan(), [task])

    assert report["ready"] is True
    assert report["accountable_role_ids"] == ["ceo"]
    assert report["active_role_ids"] == ["ceo"]


def test_review_only_plan_has_accountable_owner_without_active_worker() -> None:
    projection = _projection("review", "reviewer")
    projection.turn_type = "review"
    plan = CompanyWorkItemRuntimePlan(projections=[projection])

    report = assess_company_readiness(plan, [])

    assert report["ready"] is True
    assert report["accountable_role_ids"] == ["reviewer"]
    assert report["active_role_ids"] == []


def test_governed_execution_releases_ownership_and_persists_telemetry() -> None:
    events: list[str] = []
    saved: list[Task] = []

    class Ownership:
        def bind(self):
            events.append("bound")
            return nullcontext()

        def release(self) -> bool:
            events.append("released")
            return True

    async def scenario() -> str:
        task = _task()
        plan = CompanyWorkItemRuntimePlan(
            projections=[_projection("delivery", "ceo", authoritative=True)]
        )

        async def execute_scheduler(
            _plan: CompanyWorkItemRuntimePlan,
            _tasks: list[Task],
        ) -> str:
            events.append("executed")
            return "done"

        async def save_task(task_to_save: Task) -> None:
            saved.append(task_to_save)

        return await run_governed_company_execution(
            plan,
            [task],
            acquire_ownership=lambda _tasks: Ownership(),
            execute_scheduler=execute_scheduler,
            save_task=save_task,
        )

    assert asyncio.run(scenario()) == "done"
    assert events == ["bound", "executed", "released"]
    assert saved[0].metadata["company_execution_telemetry"]["status"] == "completed"
