from __future__ import annotations

from opc.operations.learning_effectiveness import (
    LearningEffectivenessPolicy,
    build_learning_effectiveness_report,
)
from opc.operations.models import (
    GateStatus,
    RunManifest,
    RunMetrics,
    RunScorecard,
)


ASSET_ID = "asset-improved-review"


def _simple_manifest(run_id: str, goal_id: str, *, treated: bool) -> RunManifest:
    metadata = {"learning_evaluation_cohort": goal_id}
    if treated:
        metadata["learning_activation"] = {
            "assets": [{"asset_id": ASSET_ID}],
        }
    return RunManifest(
        run_id=run_id,
        goal_id=goal_id,
        project_id="demo",
        metadata=metadata,
    )


def _scorecard(
    run_id: str,
    goal_id: str,
    quality: float,
    *,
    interventions: int,
    rework: int,
) -> RunScorecard:
    return RunScorecard(
        run_id=run_id,
        goal_id=goal_id,
        project_id="demo",
        gate_status=GateStatus.PASS if quality >= 0.75 else GateStatus.FAIL,
        total_score=quality,
        quality_score=quality,
        evidence_score=quality,
        reliability_score=quality,
        autonomy_score=max(0.0, 1.0 - interventions / 10),
        metrics=RunMetrics(
            interventions=interventions,
            rework_cycles=rework,
            duration_seconds=100,
            cost_usd=1,
        ),
    )


def test_effectiveness_report_measures_quality_and_operating_lift() -> None:
    manifests = []
    scorecards = []
    for index in range(3):
        control_id = f"control-{index}"
        treated_id = f"treated-{index}"
        manifests.extend(
            [
                _simple_manifest(control_id, "same-goal", treated=False),
                _simple_manifest(treated_id, "same-goal", treated=True),
            ]
        )
        scorecards.extend(
            [
                _scorecard(
                    control_id,
                    "same-goal",
                    0.8,
                    interventions=2,
                    rework=2,
                ),
                _scorecard(
                    treated_id,
                    "same-goal",
                    0.9,
                    interventions=1,
                    rework=0,
                ),
            ]
        )

    report = build_learning_effectiveness_report(
        ASSET_ID,
        manifests=manifests,
        scorecards=scorecards,
    )

    assert report["status"] == "improved"
    assert report["promotion_evidence_eligible"] is True
    assert report["delta"]["quality_score"] == 0.1
    assert report["delta"]["mean_interventions"] == -1.0
    assert report["delta"]["mean_rework_cycles"] == -2.0
    assert len(report["provenance"]["treated_run_ids"]) == 3


def test_effectiveness_report_fails_closed_without_matched_controls() -> None:
    manifests = [_simple_manifest("treated", "goal-a", treated=True)]
    scorecards = [
        _scorecard("treated", "goal-a", 0.95, interventions=0, rework=0),
        _scorecard("unmatched", "goal-b", 0.2, interventions=5, rework=5),
    ]

    report = build_learning_effectiveness_report(
        ASSET_ID,
        manifests=manifests,
        scorecards=scorecards,
        policy=LearningEffectivenessPolicy(minimum_samples_per_arm=1),
    )

    assert report["status"] == "insufficient_evidence"
    assert report["promotion_evidence_eligible"] is False
    assert report["control"]["samples"] == 0
    assert report["blockers"] == ["control samples 0 < 1"]


def test_effectiveness_report_rejects_regression() -> None:
    manifests = [
        _simple_manifest("control", "same-goal", treated=False),
        _simple_manifest("treated", "same-goal", treated=True),
    ]
    scorecards = [
        _scorecard("control", "same-goal", 0.9, interventions=0, rework=0),
        _scorecard("treated", "same-goal", 0.7, interventions=0, rework=0),
    ]

    report = build_learning_effectiveness_report(
        ASSET_ID,
        manifests=manifests,
        scorecards=scorecards,
        policy=LearningEffectivenessPolicy(minimum_samples_per_arm=1),
    )

    assert report["status"] == "regressed"
    assert report["promotion_evidence_eligible"] is False
    assert report["delta"]["quality_score"] == -0.2


def test_effectiveness_report_counts_missing_evidence_violations() -> None:
    manifests = [
        _simple_manifest("control", "same-goal", treated=False),
        _simple_manifest("treated", "same-goal", treated=True),
    ]
    control = _scorecard(
        "control", "same-goal", 0.8, interventions=1, rework=1
    )
    control.violations = [
        "criterion rights is missing required evidence",
        "criterion checksum is missing required evidence",
    ]
    treated = _scorecard(
        "treated", "same-goal", 0.9, interventions=0, rework=0
    )

    report = build_learning_effectiveness_report(
        ASSET_ID,
        manifests=manifests,
        scorecards=[control, treated],
        policy=LearningEffectivenessPolicy(minimum_samples_per_arm=1),
    )

    assert report["control"]["mean_missing_required_evidence"] == 2.0
    assert report["treated"]["mean_missing_required_evidence"] == 0.0
    assert report["delta"]["mean_missing_required_evidence"] == -2.0
