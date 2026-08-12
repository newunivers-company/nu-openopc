from __future__ import annotations

from opc.operations.benchmarks import (
    DEFAULT_SUITE_PATH,
    build_campaign_plan,
    load_suite,
)


V2_SUITE_PATH = DEFAULT_SUITE_PATH.parent / "openopc_core_outcomes_v2.json"
_DIFFICULTY_PREFIX = "difficulty:"
_ALLOWED_DIFFICULTIES = {"difficulty:standard", "difficulty:hard"}


def test_v2_suite_loads_validates_and_is_balanced() -> None:
    suite = load_suite(V2_SUITE_PATH)

    assert suite.suite_id == "openopc-core-outcomes"
    assert suite.version == 2
    assert suite.schema_version == 1
    assert suite.status == "candidate_requires_operator_evidence"
    assert suite.repetitions == 3
    assert len(suite.cases) == 18
    assert suite.workloads == ("content", "research", "software")
    assert {
        workload: sum(item.workload == workload for item in suite.cases)
        for workload in suite.workloads
    } == {"content": 6, "research": 6, "software": 6}


def test_v2_gate_policy_matches_v1_and_sample_math_has_slack() -> None:
    v1 = load_suite(DEFAULT_SUITE_PATH)
    v2 = load_suite(V2_SUITE_PATH)

    assert v2.baseline_mode == v1.baseline_mode == "task"
    assert v2.candidate_mode == v1.candidate_mode == "company"
    assert (
        v2.minimum_paired_samples_per_workload
        == v1.minimum_paired_samples_per_workload
        == 10
    )
    assert v2.maximum_quality_regression == v1.maximum_quality_regression
    assert v2.maximum_success_regression == v1.maximum_success_regression
    assert v2.minimum_quality_improvement == v1.minimum_quality_improvement
    for workload in v2.workloads:
        available = (
            sum(item.workload == workload for item in v2.cases) * v2.repetitions
        )
        assert available == 18
        assert available - v2.minimum_paired_samples_per_workload == 8


def test_v2_digest_is_deterministic_and_differs_from_v1() -> None:
    first = load_suite(V2_SUITE_PATH)
    second = load_suite(V2_SUITE_PATH)
    v1 = load_suite(DEFAULT_SUITE_PATH)

    assert len(first.digest) == 64
    assert first.digest == second.digest
    assert first.digest != v1.digest


def test_every_v2_case_carries_exactly_one_known_difficulty_tag() -> None:
    suite = load_suite(V2_SUITE_PATH)

    for case in suite.cases:
        difficulty_tags = [
            tag for tag in case.tags if tag.startswith(_DIFFICULTY_PREFIX)
        ]
        assert len(difficulty_tags) == 1, case.case_id
        assert difficulty_tags[0] in _ALLOWED_DIFFICULTIES, case.case_id


def test_v2_preserves_all_v1_cases_verbatim_except_difficulty_tag() -> None:
    v1 = load_suite(DEFAULT_SUITE_PATH)
    v2 = load_suite(V2_SUITE_PATH)
    v1_ids = {item.case_id for item in v1.cases}
    v2_ids = {item.case_id for item in v2.cases}

    assert v1_ids <= v2_ids
    assert len(v2_ids - v1_ids) == 6
    for case_id in sorted(v1_ids):
        original = v1.case(case_id).to_dict()
        copied = v2.case(case_id).to_dict()
        original_tags = original.pop("tags")
        copied_tags = copied.pop("tags")
        assert copied == original, case_id
        assert copied_tags[: len(original_tags)] == original_tags, case_id
        appended = copied_tags[len(original_tags) :]
        assert len(appended) == 1 and appended[0] in _ALLOWED_DIFFICULTIES, case_id


def test_v2_new_cases_are_two_per_workload_with_unique_goal_ids() -> None:
    v1 = load_suite(DEFAULT_SUITE_PATH)
    v2 = load_suite(V2_SUITE_PATH)
    v1_ids = {item.case_id for item in v1.cases}
    new_cases = [item for item in v2.cases if item.case_id not in v1_ids]

    assert {
        workload: sum(item.workload == workload for item in new_cases)
        for workload in v2.workloads
    } == {"content": 2, "research": 2, "software": 2}
    goal_ids = [item.goal.goal_id for item in v2.cases]
    assert len(goal_ids) == len(set(goal_ids))
    for case in new_cases:
        assert case.goal.project_id == "benchmark"
        assert case.goal.deliverables
        assert case.goal.evidence_requirements
        assert len(case.goal.acceptance_criteria) >= 3


def test_v2_campaign_plan_yields_54_counterbalanced_pairs() -> None:
    first = build_campaign_plan(load_suite(V2_SUITE_PATH), campaign_id="v2-release-2026q3")
    second = build_campaign_plan(load_suite(V2_SUITE_PATH), campaign_id="v2-release-2026q3")

    assert first == second
    assert first["pair_count"] == 54
    assert first["slot_count"] == 108
    assert first["counterbalance"] == {
        "strategy": "digest_order_alternating_first_mode",
        "baseline_first_pairs": 27,
        "candidate_first_pairs": 27,
    }
    assert len(first["plan_digest"]) == 64
    assert len({item["slot_id"] for item in first["slots"]}) == 108
    assert len({item["run_id"] for item in first["slots"]}) == 108
