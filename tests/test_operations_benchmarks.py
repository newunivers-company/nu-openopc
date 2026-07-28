from __future__ import annotations

import hashlib

import pytest

from opc.operations.benchmarks import (
    OutcomeObservation,
    build_campaign_plan,
    campaign_progress,
    evaluate_outcomes,
    load_suite,
    observation_from_run,
)
from opc.operations.models import (
    CriterionResult,
    GateStatus,
    RunManifest,
    RunMetrics,
    RunScorecard,
    RunStatus,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _observation(
    *,
    case_id: str,
    mode: str,
    repetition: int,
    actual: bool = True,
    score: float = 0.9,
) -> OutcomeObservation:
    suite = load_suite()
    return OutcomeObservation(
        suite_digest=suite.digest,
        case_id=case_id,
        mode=mode,
        repetition=repetition,
        run_id=f"{case_id}-{mode}-{repetition}",
        source="actual_run" if actual else "simulation",
        authority="independent_judge" if actual else "simulation",
        gate_status="pass",
        score=score,
        quality_score=score,
        duration_seconds=10,
        cost_usd=0.1,
        usage_measured=True,
        interventions=0,
        rework_cycles=0,
        evidence=(f"artifact://{case_id}/{mode}/{repetition}",),
        artifact_digest=_digest(f"{case_id}-{mode}-{repetition}"),
    )


def test_builtin_suite_is_versioned_and_has_three_balanced_workloads() -> None:
    suite = load_suite()

    assert suite.suite_id == "openopc-core-outcomes"
    assert suite.version == 1
    assert len(suite.cases) == 12
    assert suite.workloads == ("content", "research", "software")
    assert {
        workload: sum(item.workload == workload for item in suite.cases)
        for workload in suite.workloads
    } == {"content": 4, "research": 4, "software": 4}
    assert len(suite.digest) == 64


def test_simulation_is_reported_but_never_supports_product_claim() -> None:
    suite = load_suite()
    rows = [
        _observation(
            case_id=case.case_id,
            mode=mode,
            repetition=repetition,
            actual=False,
        )
        for case in suite.cases
        for mode in ("task", "company")
        for repetition in range(1, suite.repetitions + 1)
    ]

    report = evaluate_outcomes(suite, rows)

    assert report["status"] == "blocked"
    assert report["promotion_eligible"] is False
    assert report["product_claim_ready"] is False
    assert any("lacks actual-run" in item for item in report["blockers"])


def test_complete_paired_actual_suite_passes_non_regression_gate() -> None:
    suite = load_suite()
    rows = [
        _observation(
            case_id=case.case_id,
            mode=mode,
            repetition=repetition,
            score=0.82 if mode == "task" else 0.91,
        )
        for case in suite.cases
        for mode in ("task", "company")
        for repetition in range(1, suite.repetitions + 1)
    ]

    report = evaluate_outcomes(suite, rows)

    assert report["status"] == "pass"
    assert report["promotion_eligible"] is True
    assert report["trusted_pair_count"] == 36
    assert report["blockers"] == []
    assert all(item["paired_samples"] == 12 for item in report["workloads"].values())
    assert report["overall"]["quality_delta"]["lower"] > 0


def test_duplicate_retry_slot_blocks_promotion() -> None:
    suite = load_suite()
    row = _observation(
        case_id=suite.cases[0].case_id,
        mode="task",
        repetition=1,
    )
    duplicate = OutcomeObservation.from_dict(
        row.to_dict() | {"run_id": "retry-run"}
    )

    report = evaluate_outcomes(suite, [row, duplicate])

    assert report["promotion_eligible"] is False
    assert any("retries are not independent samples" in item for item in report["blockers"])


def test_non_hex_artifact_digest_is_never_trusted() -> None:
    row = OutcomeObservation.from_dict(
        _observation(
            case_id=load_suite().cases[0].case_id,
            mode="task",
            repetition=1,
        ).to_dict()
        | {"artifact_digest": "z" * 64}
    )

    assert row.trusted is False


def test_observation_from_run_requires_completed_governed_evidence() -> None:
    suite = load_suite()
    manifest = RunManifest(
        run_id="run-1",
        goal_id="goal-1",
        goal_version=1,
        status=RunStatus.COMPLETED,
        source_revision="abc123",
    )
    scorecard = RunScorecard(
        run_id="run-1",
        goal_id="goal-1",
        gate_status=GateStatus.PASS,
        total_score=0.92,
        quality_score=0.94,
        metrics=RunMetrics(
            cost_usd=0.12,
            duration_seconds=12,
            interventions=1,
        ),
        criterion_results=[
            CriterionResult(
                criterion_id="evidence",
                score=1.0,
                passed=True,
                evidence=["artifact://verified"],
            )
        ],
        metadata={"usage_measured": True},
    )

    row = observation_from_run(
        suite,
        case_id=suite.cases[0].case_id,
        mode="company",
        repetition=1,
        manifest=manifest,
        scorecard=scorecard,
        authority="human_confirmed",
        artifact_digest=_digest("artifact"),
    )

    assert row.trusted is True
    assert row.cost_usd == 0.12
    manifest.status = RunStatus.RUNNING
    with pytest.raises(ValueError, match="completed run"):
        observation_from_run(
            suite,
            case_id=suite.cases[0].case_id,
            mode="company",
            repetition=1,
            manifest=manifest,
            scorecard=scorecard,
            authority="human_confirmed",
            artifact_digest=_digest("artifact"),
        )


def test_campaign_plan_is_deterministic_complete_and_counterbalanced() -> None:
    first = build_campaign_plan(load_suite(), campaign_id="release-2026q3")
    second = build_campaign_plan(load_suite(), campaign_id="release-2026q3")

    assert first == second
    assert first["pair_count"] == 36
    assert first["slot_count"] == 72
    assert first["counterbalance"] == {
        "strategy": "digest_order_alternating_first_mode",
        "baseline_first_pairs": 18,
        "candidate_first_pairs": 18,
    }
    assert len(first["plan_digest"]) == 64
    assert len({item["slot_id"] for item in first["slots"]}) == 72
    assert len({item["run_id"] for item in first["slots"]}) == 72


def test_campaign_progress_separates_missing_foreign_and_trusted_slots() -> None:
    suite = load_suite()
    campaign = "release-2026q3"
    first_case = suite.cases[0].case_id
    trusted = [
        OutcomeObservation.from_dict(
            _observation(
                case_id=first_case,
                mode=mode,
                repetition=1,
            ).to_dict()
            | {"metadata": {"campaign_id": campaign}}
        )
        for mode in ("task", "company")
    ]
    foreign = OutcomeObservation.from_dict(
        _observation(
            case_id=suite.cases[1].case_id,
            mode="task",
            repetition=1,
        ).to_dict()
        | {"metadata": {"campaign_id": "other-campaign"}}
    )

    report = campaign_progress(
        suite,
        [*trusted, foreign],
        campaign_id=campaign,
    )

    assert report["expected_slots"] == 72
    assert report["observed_slots"] == 2
    assert report["trusted_slots"] == 2
    assert report["completed_pairs"] == 1
    assert report["trusted_pairs"] == 1
    assert report["missing_slot_count"] == 70
    assert report["foreign_run_ids"] == [foreign.run_id]
    assert report["status"] == "collecting"
