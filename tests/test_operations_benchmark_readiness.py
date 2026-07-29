from __future__ import annotations

import copy
from pathlib import Path

import pytest

from opc.operations.benchmarks import (
    OutcomeBenchmarkSuite,
    build_campaign_plan,
    load_suite,
    suite_execution_readiness,
)


V2_SUITE_PATH = (
    Path(__file__).resolve().parents[1]
    / "opc"
    / "benchmark_assets"
    / "openopc_core_outcomes_v2.json"
)


def test_default_suite_binds_complete_inline_inputs() -> None:
    suite = load_suite()

    report = suite_execution_readiness(suite)

    assert (
        suite.digest
        == "b3a8ecd772af04868e78fa71410f28bfd0841a8b5d92a0ad7bb2e7c6a0f8862a"
    )
    assert report["execution_ready"] is True
    assert len(report["input_binding_digest"]) == 64
    assert report["ready_case_count"] == 12
    assert report["blocked_case_count"] == 0
    assert all(item["input_contract_digest"] for item in report["cases"])
    assert sum(item["artifact_count"] for item in report["cases"]) >= 12


def test_candidate_v2_suite_is_execution_ready_with_all_inputs_sealed() -> None:
    suite = load_suite(V2_SUITE_PATH)

    report = suite_execution_readiness(suite)

    assert report["execution_ready"] is True
    assert report["ready_case_count"] == 18
    assert report["blocked_case_count"] == 0
    assert report["blockers"] == []
    plan = build_campaign_plan(
        suite,
        campaign_id="strict-v2",
        require_execution_ready=True,
    )
    assert plan["execution_preflight"]["execution_ready"] is True
    assert plan["pair_count"] == 54
    assert plan["slot_count"] == 108


def test_plan_seals_fixture_content_and_digests_into_both_arms() -> None:
    suite = load_suite()
    plan = build_campaign_plan(
        suite,
        campaign_id="fixture-bound",
        require_execution_ready=True,
    )
    case = suite.cases[0]
    slots = [item for item in plan["slots"] if item["case_id"] == case.case_id]

    assert plan["execution_preflight"]["execution_ready"] is True
    assert {item["mode"] for item in slots} == {"task", "company"}
    assert len({item["prompt"] for item in slots}) == 1
    assert "## Sealed benchmark inputs" in slots[0]["prompt"]
    assert slots[0]["input_contract_digest"] == case.input_contract.digest
    assert slots[0]["input_artifact_digests"] == {
        item.name: item.sha256 for item in case.input_contract.artifacts
    }


def test_tampered_inline_fixture_digest_fails_suite_loading() -> None:
    payload = copy.deepcopy(load_suite().to_dict())
    payload["cases"][0]["input_contract"]["artifacts"][0]["sha256"] = "0" * 64

    with pytest.raises(ValueError, match="does not match its SHA-256"):
        OutcomeBenchmarkSuite.from_dict(payload)


def test_input_contract_does_not_coerce_network_policy_strings() -> None:
    payload = copy.deepcopy(load_suite().to_dict())
    payload["cases"][0]["input_contract"]["network_allowed"] = "false"

    with pytest.raises(ValueError, match="network_allowed must be a boolean"):
        OutcomeBenchmarkSuite.from_dict(payload)


def test_fixture_revision_changes_plan_not_versioned_outcome_digest() -> None:
    original = load_suite()
    payload = copy.deepcopy(original.to_dict())
    artifact = payload["cases"][0]["input_contract"]["artifacts"][0]
    artifact["content"] += "\n# fixture revision\n"
    artifact.pop("sha256")
    revised = OutcomeBenchmarkSuite.from_dict(payload)

    assert revised.digest == original.digest
    assert (
        suite_execution_readiness(revised)["input_binding_digest"]
        != suite_execution_readiness(original)["input_binding_digest"]
    )
    assert (
        build_campaign_plan(revised, campaign_id="fixture-revision")["plan_digest"]
        != build_campaign_plan(original, campaign_id="fixture-revision")["plan_digest"]
    )


def test_self_contained_contract_cannot_hide_a_supplied_pack_reference() -> None:
    payload = copy.deepcopy(load_suite().to_dict())
    payload["cases"][0]["prompt"] = "Using the supplied source pack, write a report."
    payload["cases"][0]["input_contract"] = {
        "kind": "self_contained",
        "network_allowed": False,
        "artifacts": [],
    }
    suite = OutcomeBenchmarkSuite.from_dict(payload)

    report = suite_execution_readiness(suite)

    first = next(
        item for item in report["cases"] if item["case_id"] == suite.cases[0].case_id
    )
    assert first["execution_ready"] is False
    assert "not bundled" in first["blockers"][0]
