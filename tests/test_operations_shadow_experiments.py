from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest
from nu_llm_routing_lib.routing_events import (
    RevisionSet,
    RoutingDecisionEvent,
    RoutingOutcomeEvent,
)
from nu_llm_routing_lib.sqlite_event_store import SQLiteEventStore
from opc.operations.experiments import (
    ShadowQualityObservation,
    bind_shadow_observation_to_transport,
    evaluate_shadow_promotion,
    shadow_review_queue,
    shadow_transport_report,
)


def _row(index: int, *, workload: str = "coding", actual: bool = True):
    return ShadowQualityObservation(
        experiment_id="exp-v1",
        decision_id=f"decision-{workload}-{index}",
        request_fingerprint=hashlib.sha256(f"{workload}-{index}".encode()).hexdigest(),
        workload=workload,
        served_provider="codex",
        served_model="served",
        challenger_provider="claude",
        challenger_model="challenger",
        served_ok=True,
        challenger_ok=True,
        served_quality=0.82,
        challenger_quality=0.9,
        served_latency_ms=100,
        challenger_latency_ms=120,
        actual_run=actual,
        authority="independent_judge" if actual else "simulation",
        judge_revision="judge-v1",
        evidence=(f"artifact://review/{workload}/{index}",),
        served_artifact_digest=hashlib.sha256(
            f"served-{workload}-{index}".encode()
        ).hexdigest(),
        challenger_artifact_digest=hashlib.sha256(
            f"challenger-{workload}-{index}".encode()
        ).hexdigest(),
    )


def test_shadow_quality_and_transport_evidence_pass_together() -> None:
    rows = [
        *[_row(index, workload="coding") for index in range(3)],
        *[_row(index, workload="dialogue") for index in range(3)],
    ]
    transport = {
        "decision_count": 6,
        "decisions_with_shadow": 6,
        "shadow_attempts": 6,
        "blockers": [],
        "decision_ids": [item.decision_id for item in rows],
    }

    report = evaluate_shadow_promotion(
        rows,
        experiment_id="exp-v1",
        minimum_samples_per_workload=3,
        transport_report=transport,
    )

    assert report["promotion_ready"] is True
    assert report["status"] == "pass"
    assert report["trusted_observation_count"] == 6
    assert all(item["quality_delta"]["lower"] > 0 for item in report["workloads"].values())


def test_transport_success_alone_cannot_promote() -> None:
    report = evaluate_shadow_promotion(
        [],
        experiment_id="exp-v1",
        minimum_samples_per_workload=3,
        transport_report={
            "decision_count": 30,
            "decisions_with_shadow": 30,
            "shadow_attempts": 30,
            "blockers": [],
            "decision_ids": ["unused"],
        },
    )

    assert report["promotion_ready"] is False
    assert "no workload has trusted shadow quality observations" in report["blockers"]


def test_simulated_or_duplicate_shadow_samples_fail_closed() -> None:
    first = _row(1, actual=False)
    duplicate = ShadowQualityObservation.from_dict(
        first.to_dict() | {"actual_run": True, "authority": "human_confirmed"}
    )

    report = evaluate_shadow_promotion(
        [first, duplicate],
        experiment_id="exp-v1",
        minimum_samples_per_workload=2,
        transport_report={
            "decision_count": 1,
            "decisions_with_shadow": 1,
            "blockers": [],
            "decision_ids": [first.decision_id],
        },
    )

    assert report["promotion_ready"] is False
    assert any("lacks actual independent evidence" in item for item in report["blockers"])
    assert any("duplicate decision_id" in item for item in report["blockers"])


def test_shadow_observation_binds_to_immutable_transport_inventory() -> None:
    with tempfile.TemporaryDirectory() as raw_root:
        database = Path(raw_root) / "events.sqlite3"
        store = SQLiteEventStore(database)
        fingerprint = hashlib.sha256(b"request").hexdigest()
        decision_id = "decision-1"
        store.append(
            RoutingDecisionEvent(
                decision_id=decision_id,
                occurred_at="2026-07-28T00:00:00+00:00",
                request_fingerprint=fingerprint,
                revisions=RevisionSet(
                    config_revision="config-v1",
                    policy_revision="policy-v1",
                    feature_schema_revision="features-v1",
                ),
                context={"workload": "coding"},
                eligible_candidates=("codex", "claude"),
                excluded_candidates={},
                selected_route=("codex", "claude"),
                candidate_estimates={},
                objective={},
            )
        )
        store.append(
            RoutingOutcomeEvent(
                event_id="served-event",
                decision_id=decision_id,
                occurred_at="2026-07-28T00:00:01+00:00",
                attempt_index=0,
                provider="codex",
                model_revision="served",
                transport_ok=True,
                contract_ok=True,
                latency_s=0.1,
                terminal=True,
                remediation_action="initial",
            )
        )
        store.append(
            RoutingOutcomeEvent(
                event_id="shadow-event",
                decision_id=decision_id,
                occurred_at="2026-07-28T00:00:02+00:00",
                attempt_index=1,
                provider="claude",
                model_revision="challenger",
                transport_ok=True,
                contract_ok=True,
                latency_s=0.12,
                terminal=False,
                remediation_action="shadow",
            )
        )
        row = ShadowQualityObservation.from_dict(
            _row(1).to_dict()
            | {
                "decision_id": decision_id,
                "request_fingerprint": fingerprint,
            }
        )

        bound = bind_shadow_observation_to_transport(row, database)
        queue = shadow_review_queue(
            database,
            experiment_id="exp-v1",
            observations=[bound],
        )
        report = shadow_transport_report(database, minimum_decisions=1)

        assert bound.trusted is True
        assert bound.metadata["transport_bound"] is True
        assert len(bound.metadata["transport_snapshot_sha256"]) == 64
        assert queue["observed_count"] == 1
        assert queue["items"][0]["review_state"] == "observed"
        assert report["decision_ids"] == [decision_id]
        assert report["decisions"][0]["review_ready"] is True

        mismatched = ShadowQualityObservation.from_dict(
            row.to_dict() | {"served_model": "wrong-model"}
        )
        with pytest.raises(ValueError, match="does not match transport"):
            bind_shadow_observation_to_transport(mismatched, database)
