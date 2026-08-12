from __future__ import annotations

import json
from pathlib import Path

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


def test_trusted_underperformance_vetoes_structural_company_recommendation() -> None:
    report = assess_execution_mode(
        _request(
            deliverable_count=4,
            role_count=4,
            independent_workstreams=3,
            dependency_count=3,
            bounded_scope=False,
            independent_review_required=True,
            final_integration_required=True,
            workload_key="research",
            outcome_observations=[
                {
                    "workload_key": "research",
                    "authority": "human_confirmed",
                    "task_quality": 0.94,
                    "company_quality": 0.91,
                    "task_duration_seconds": 196,
                    "company_duration_seconds": 1870,
                    "task_external_calls": 2,
                    "company_external_calls": 28,
                }
            ],
        )
    )

    assert report["structural_company_benefit_score"] == 10
    assert report["recommendation"] == "task"
    assert report["evidence_veto"] is True
    assert report["observed_evidence"]["matched_trusted_pairs"] == 1
    assert report["observed_evidence"]["mean_duration_ratio"] > 9
    assert any(
        item["code"] == "observed_company_underperformance"
        for item in report["factors"]
    )


def test_drafts_and_foreign_workloads_never_influence_advice() -> None:
    report = assess_execution_mode(
        _request(
            workload_key="content",
            outcome_observations=[
                {
                    "workload_key": "content",
                    "authority": "llm_draft",
                    "task_quality": 0.1,
                    "company_quality": 1.0,
                    "task_duration_seconds": 1,
                    "company_duration_seconds": 1,
                    "task_external_calls": 1,
                    "company_external_calls": 1,
                },
                {
                    "workload_key": "research",
                    "authority": "independent_judge",
                    "task_quality": 0.1,
                    "company_quality": 1.0,
                    "task_duration_seconds": 1,
                    "company_duration_seconds": 1,
                    "task_external_calls": 1,
                    "company_external_calls": 1,
                },
            ],
        )
    )

    assert report["recommendation"] == "task"
    assert report["observed_evidence"]["matched_trusted_pairs"] == 0
    assert report["observed_evidence"]["ignored_observations"] == 2
    assert report["evidence_veto"] is False


def test_trusted_quality_lift_within_budget_strengthens_company_advice() -> None:
    observations = [
        {
            "workload_key": "software",
            "authority": "independent_judge",
            "task_quality": 0.82,
            "company_quality": 0.90,
            "task_duration_seconds": 100,
            "company_duration_seconds": 220,
            "task_external_calls": 2,
            "company_external_calls": 8,
        }
        for _ in range(3)
    ]
    report = assess_execution_mode(
        _request(
            role_count=2,
            independent_workstreams=2,
            deliverable_count=2,
            bounded_scope=False,
            estimated_duration_minutes=120,
            workload_key="software",
            outcome_observations=observations,
        )
    )

    assert report["recommendation"] == "company"
    assert report["observed_evidence"]["confidence"] == "medium"
    assert report["observed_evidence"]["within_provisional_budget"] is True
    assert report["observed_evidence"]["mean_quality_delta"] == 0.08


def test_shared_frontend_backend_parity_cases() -> None:
    fixture = Path(__file__).parent / "fixtures" / "mode_advisor_parity.json"
    cases = json.loads(fixture.read_text(encoding="utf-8"))

    for case in cases:
        report = assess_execution_mode(
            ModeAssessmentRequest.from_dict(case["request"])
        )
        expected = case["expected"]
        assert report["recommendation"] == expected["recommendation"], case["name"]
        assert (
            report["company_benefit_score"]
            == expected["company_benefit_score"]
        ), case["name"]
        assert report["evidence_veto"] is expected["evidence_veto"], case["name"]
