from __future__ import annotations

import pytest

from opc.operations.mode_advisor import (
    ModeAssessmentRequest,
    assess_execution_mode,
)


def _request(**overrides: object) -> ModeAssessmentRequest:
    payload: dict[str, object] = {
        "goal": "Ship a governed release",
        "deliverable_count": 1,
        "role_count": 1,
        "independent_workstreams": 1,
        "dependency_count": 0,
        "bounded_scope": True,
        "input_complete": True,
        "requirements_stable": True,
        "estimated_duration_minutes": 20,
    }
    payload.update(overrides)
    return ModeAssessmentRequest.from_dict(payload)


def test_bounded_single_owner_work_recommends_task_mode() -> None:
    report = assess_execution_mode(_request())

    assert report["recommendation"] == "task"
    assert report["confidence"] == "high"
    assert report["advisory_only"] is True
    assert report["company_benefit_score"] == 0
    assert any(
        item["code"] == "bounded_single_owner" for item in report["factors"]
    )


def test_parallel_reviewed_integrated_work_recommends_company_mode() -> None:
    report = assess_execution_mode(
        _request(
            deliverable_count=4,
            role_count=4,
            independent_workstreams=3,
            dependency_count=3,
            bounded_scope=False,
            independent_review_required=True,
            final_integration_required=True,
            risk_level="high",
            estimated_duration_minutes=240,
        )
    )

    assert report["recommendation"] == "company"
    assert report["confidence"] == "high"
    assert report["company_benefit_score"] == 10
    assert "independent reviewer distinct from the executor" in report[
        "recommended_controls"
    ]
    assert any(item["code"] == "final_integration" for item in report["factors"])


def test_incomplete_inputs_fail_closed_before_mode_selection() -> None:
    report = assess_execution_mode(
        _request(
            input_complete=False,
            requirements_stable=False,
            role_count=4,
            independent_workstreams=3,
            independent_review_required=True,
        )
    )

    assert report["recommendation"] == "clarify"
    assert [item["code"] for item in report["blockers"]] == [
        "input_incomplete",
        "requirements_unstable",
    ]
    assert report["advisory_only"] is True


def test_required_boolean_evidence_is_not_coerced_from_strings() -> None:
    with pytest.raises(ValueError, match="input_complete.*explicit boolean"):
        ModeAssessmentRequest.from_dict(
            {
                "goal": "Do work",
                "deliverable_count": 1,
                "role_count": 1,
                "independent_workstreams": 1,
                "dependency_count": 0,
                "bounded_scope": True,
                "input_complete": "yes",
                "requirements_stable": True,
            }
        )


def test_assessment_is_deterministic() -> None:
    request = _request(
        deliverable_count=3,
        role_count=2,
        independent_workstreams=2,
        final_integration_required=True,
    )

    assert assess_execution_mode(request) == assess_execution_mode(request)
