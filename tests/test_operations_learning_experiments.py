from __future__ import annotations

import copy

import pytest

from opc.operations.learning_experiments import (
    RELEASE_EXPERIMENT_COHORT_KEY,
    build_release_playbook_experiment,
    verify_release_playbook_experiment,
)
from opc.operations.models import LearningAsset


def _asset(*, source_run_ids: list[str] | None = None) -> LearningAsset:
    return LearningAsset(
        asset_id="asset-release-playbook-v1",
        name="release-evidence-playbook",
        kind="release_playbook",
        project_id="demo",
        version=1,
        content={
            "required_steps": [
                "validate rights and consent",
                "verify checksums and platform constraints",
                "record approval receipt",
            ]
        },
        source_run_ids=(
            ["release-run-17"] if source_run_ids is None else source_run_ids
        ),
    )


def test_release_playbook_plan_pins_asset_and_alternates_execution_order() -> None:
    plan = build_release_playbook_experiment(
        _asset(),
        experiment_id="release-playbook-2026q3",
        pairs=5,
    )

    assert plan["automatic_promotion"] is False
    assert plan["asset"]["asset_id"] == "asset-release-playbook-v1"
    assert len(plan["asset"]["content_digest"]) == 64
    assert plan["design"]["pair_count"] == 5
    assert plan["design"]["slot_count"] == 10
    assert [slot["arm"] for slot in plan["slots"][:4]] == [
        "control",
        "treated",
        "treated",
        "control",
    ]
    treated = next(slot for slot in plan["slots"] if slot["arm"] == "treated")
    control = next(slot for slot in plan["slots"] if slot["arm"] == "control")
    assert treated["metadata"]["learning_activation"]["assets"][0][
        "content_digest"
    ] == plan["asset"]["content_digest"]
    assert control["metadata"]["learning_activation"]["assets"] == []
    assert (
        RELEASE_EXPERIMENT_COHORT_KEY
        in treated["metadata"]
    )
    assert verify_release_playbook_experiment(plan) == plan


def test_release_playbook_plan_requires_self_grown_source_provenance() -> None:
    with pytest.raises(ValueError, match="source run provenance"):
        build_release_playbook_experiment(
            _asset(source_run_ids=[]),
            experiment_id="unproven-playbook",
        )


def test_release_playbook_plan_digest_detects_activation_tampering() -> None:
    plan = build_release_playbook_experiment(
        _asset(),
        experiment_id="tamper-check",
        pairs=3,
    )
    tampered = copy.deepcopy(plan)
    tampered["slots"][0]["metadata"]["learning_activation"]["assets"] = [
        {"asset_id": "leaked-treated-asset"}
    ]

    with pytest.raises(ValueError, match="digest mismatch"):
        verify_release_playbook_experiment(tampered)
