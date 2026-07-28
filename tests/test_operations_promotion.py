from __future__ import annotations

from opc.operations.promotion import build_promotion_dossier


def _reports(*, passed: bool):
    return {
        "dependency_report": {
            "ok": passed,
            "release_id": "release-v1",
            "error": "" if passed else "dependency mismatch",
        },
        "outcome_report": {
            "status": "pass" if passed else "blocked",
            "promotion_eligible": passed,
            "product_claim_ready": passed,
            "trusted_pair_count": 36 if passed else 0,
            "blockers": [] if passed else ["missing outcome pairs"],
        },
        "shadow_report": {
            "status": "pass" if passed else "blocked",
            "promotion_ready": passed,
            "trusted_observation_count": 90 if passed else 0,
            "blockers": [] if passed else ["missing shadow judgments"],
        },
        "canary_report": {
            "providers": {
                "codex": {
                    "production_ready": passed,
                    "readiness_state": "ready" if passed else "pending_evidence",
                    "blockers": [] if passed else ["latest sample is stale"],
                }
            }
        },
    }


def test_promotion_dossier_passes_only_when_every_independent_gate_passes() -> None:
    report = build_promotion_dossier(**_reports(passed=True))

    assert report["status"] == "pass"
    assert report["promotion_ready"] is True
    assert report["product_claim_ready"] is True
    assert report["blockers"] == []
    assert all(item["passed"] for item in report["gates"].values())
    assert all(len(item) == 64 for item in report["input_digests"].values())


def test_promotion_dossier_preserves_source_blockers() -> None:
    report = build_promotion_dossier(**_reports(passed=False))

    assert report["status"] == "blocked"
    assert report["promotion_ready"] is False
    assert "dependencies:dependency mismatch" in report["blockers"]
    assert "outcomes:missing outcome pairs" in report["blockers"]
    assert "shadow:missing shadow judgments" in report["blockers"]
    assert "canary:codex:latest sample is stale" in report["blockers"]
